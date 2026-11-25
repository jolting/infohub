"""The display server over real sockets: pairing, and a stuck display."""

import asyncio
import json
import time

import pytest
import websockets

from infohub import websocket_server
from infohub.layout import layout_from_panel
from infohub.pairing import DisplayAuth
from infohub.websocket_server import InfoHubWebSocketServer

PANEL = {"id": "p", "name": "Office", "language": "en", "widgets": []}


async def start(auth):
    server = InfoHubWebSocketServer("127.0.0.1", 0, auth=auth)
    server.configure_panels({"p": layout_from_panel(PANEL)}, "p", lambda _: "p")
    actions = []

    async def on_action(entity_id, command, value):
        actions.append((entity_id, command))

    server.configure_action_handler(on_action)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}", actions


async def recv(ws, timeout=2.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def hello(url, device_id, token=None):
    ws = await websockets.connect(url)
    message = {"type": "hello", "device_id": device_id}
    if token:
        message["token"] = token
    await ws.send(json.dumps(message))
    return ws


def auth_with(*approved):
    auth = DisplayAuth(None, lambda data: None)
    for device_id in approved:
        auth.approve(device_id)
    return auth


@pytest.mark.asyncio
async def test_unknown_display_waits_then_is_approved():
    server, url, _ = await start(auth_with())
    try:
        ws = await hello(url, "r1")
        assert await recv(ws) == {"type": "pairing_required", "device_id": "r1"}
        with pytest.raises(asyncio.TimeoutError):
            await recv(ws, timeout=0.3)  # nothing else until approved
        assert [d["device_id"] for d in server.displays()] == ["r1"]

        server.approve("r1")
        paired = await recv(ws)
        assert paired["type"] == "paired" and paired["token"]
        assert (await recv(ws))["type"] == "layout_update"
        await ws.close()

        # the token lets it straight in next time
        ws = await hello(url, "r1", paired["token"])
        assert (await recv(ws))["type"] == "layout_update"
        await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_waiting_display_unlisted_when_it_leaves():
    server, url, _ = await start(auth_with())
    try:
        ws = await hello(url, "r1")
        await recv(ws)
        assert [d["device_id"] for d in server.displays()] == ["r1"]
        await ws.close()
        for _ in range(20):
            if not server.displays():
                break
            await asyncio.sleep(0.05)
        assert server.displays() == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_wrong_or_missing_token_waits():
    auth = auth_with("r1")
    auth.issue("r1")
    server, url, _ = await start(auth)
    try:
        for token in (None, "not-the-token"):
            ws = await hello(url, "r1", token)
            assert (await recv(ws))["type"] == "pairing_required"
            await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_approved_display_without_token_gets_one():
    server, url, _ = await start(auth_with("r1"))
    try:
        ws = await hello(url, "r1")
        assert (await recv(ws))["type"] == "paired"
        assert (await recv(ws))["type"] == "layout_update"
        await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_waiting_display_cannot_act():
    server, url, actions = await start(auth_with())
    try:
        ws = await hello(url, "r1")
        await recv(ws)
        await ws.send(json.dumps({"type": "action", "entity_id": "light.x", "command": "toggle"}))
        await asyncio.sleep(0.3)
        assert actions == []
        await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_paired_display_can_act():
    auth = auth_with("r1")
    token = auth.issue("r1")
    server, url, actions = await start(auth)
    try:
        ws = await hello(url, "r1", token)
        await recv(ws)
        await ws.send(json.dumps({"type": "action", "entity_id": "light.x", "command": "toggle"}))
        for _ in range(20):
            if actions:
                break
            await asyncio.sleep(0.05)
        assert actions == [("light.x", "toggle")]
        await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_no_device_id_is_turned_away():
    server, url, _ = await start(auth_with())
    try:
        ws = await websockets.connect(url)
        await ws.send(json.dumps({"type": "hello"}))
        assert (await recv(ws))["type"] == "pairing_required"
        with pytest.raises(websockets.ConnectionClosed):
            await recv(ws)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_forget_disconnects():
    auth = auth_with("r1")
    token = auth.issue("r1")
    server, url, _ = await start(auth)
    try:
        ws = await hello(url, "r1", token)
        await recv(ws)
        await server.forget("r1")
        with pytest.raises(websockets.ConnectionClosed):
            await recv(ws)
        ws = await hello(url, "r1", token)
        assert (await recv(ws))["type"] == "pairing_required"
        await ws.close()
    finally:
        await server.stop()


class _Transport:
    aborted = False

    def abort(self):
        self.aborted = True


class _StuckDisplay:
    """A display that stopped reading: a send never completes."""

    remote_address = ("10.0.0.9", 1)

    def __init__(self):
        self.transport = _Transport()

    async def send(self, message):
        await asyncio.sleep(3600)


class _Display:
    remote_address = ("10.0.0.8", 1)

    def __init__(self):
        self.transport = _Transport()
        self.got = []

    async def send(self, message):
        self.got.append(json.loads(message))


@pytest.mark.asyncio
async def test_stuck_display_does_not_block(monkeypatch):
    monkeypatch.setattr(websocket_server, "SEND_TIMEOUT", 0.2)
    server = InfoHubWebSocketServer("127.0.0.1", 0)
    server.configure_panels({"p": layout_from_panel(PANEL)}, "p", lambda _: "p")
    stuck, good = _StuckDisplay(), _Display()
    server._clients = {stuck, good}
    server._connections = {stuck: "stuck", good: "good"}

    started = time.monotonic()

    async def both():
        await server.broadcast_entity_update("custom", "sensor.x", {"state": "1"})
        await server.refresh_layouts({"p": layout_from_panel(PANEL)}, "p", lambda _: "p")

    await asyncio.wait_for(both(), 3)  # before the fix: forever
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert stuck.transport.aborted
    assert [m["type"] for m in good.got] == ["entity_update", "layout_update"]
    assert stuck not in server._clients
