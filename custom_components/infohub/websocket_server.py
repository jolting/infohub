"""WebSocket server for InfoHub display clients.

Sends two independent message streams to connected clients:

- `layout_update`: the screen/widget structure (see layout.py) for
  *that specific connection's* assigned panel - see the "Panels" section
  below. Sent once right after connect, and again whenever panels change
  (`refresh_layouts`).
- `full_update` / `entity_update` / `reminder`: live data, the same
  shared stream for every connected client (see the implementation
  plan's "Nicht enthalten" section for why data isn't split per panel
  too - a widget only ever reads the group it cares about, so the extra
  groups a given display's layout doesn't use are simply unused there).

Also accepts one message type *from* clients: `{"type": "action",
"entity_id": ..., "command": ..., "value": ...}` (see
_handle_client_message()), for actuator widgets (switch_tile) - the
display taps a switch/cover button or releases a slider, this server
hands it to an injected handler (configure_action_handler()) that the
integration wires up to allowlist-check it and call a Home Assistant
service. `command` is one of "toggle", "open_cover"/"stop_cover"/
"close_cover", or "set_value" (with a numeric `value`) - see
__init__.py's _resolve_action() for the per-domain service dispatch.
This is the one place the "clients need no HA auth" design (below) has a
real consequence: anything reachable on this port can send one, bounded
only by the allowlist - see the actuator-widget plan's "Bekannte offene
Punkte" for the known gap.

And `{"type": "button", "button": ..., "press": "short"|"long"}`: a
display's hardware button (BUTTONS), handed to an injected handler
(configure_button_handler()) with the display's device_id, which the
integration fires as an `infohub_button` event for automations.

Runs as its own asyncio server on its own port (not embedded in Home
Assistant's HTTP server), so display clients need no HA auth/session
handling - they just open a plain websocket connection.

Panels: each connecting client gets up to ~2s to send a
`{"type": "hello", "device_id": ...}` identification message; the
integration resolves that to a specific panel (see panels.resolve_panel_id)
and this server sends *that* panel's layout. Clients that never send a
device_id (or aren't recognized) get the default panel - existing/older
clients keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import websockets
from websockets.server import WebSocketServerProtocol

from .layout import Layout

logger = logging.getLogger(__name__)

HELLO_TIMEOUT = 2.0

ResolvePanelFn = Callable[[str | None], "str | None"]
# (entity_id, command, value) -> None. `value` is only meaningful for
# "set_value" (None otherwise). The integration (__init__.py) injects one
# that allowlist-checks entity_id against configured "aktoren" entities
# before calling any Home Assistant service - this server only parses the
# wire message and hands it off, it has no opinion on what's allowed.
ActionHandlerFn = Callable[[str, str, "float | None"], Awaitable[None]]
# (device_id, button, press) -> None, for `button` messages.
ButtonHandlerFn = Callable[["str | None", str, str], Awaitable[None]]

# The buttons and presses a display may report: a closed vocabulary, as
# for actions.
BUTTONS = ("volume_up", "volume_down", "play_pause", "next", "previous")
PRESSES = ("short", "long")


class InfoHubWebSocketServer:
    """WebSocket server that pushes per-device layout and shared data to display clients."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._clients: set[WebSocketServerProtocol] = set()
        self._server: websockets.WebSocketServer | None = None
        self._current_data: dict[str, Any] = {}

        # Panel-Zuordnung
        self._layouts: dict[str, Layout] = {}
        self._default_panel_id: str | None = None
        self._resolve_panel: ResolvePanelFn | None = None
        self._connections: dict[WebSocketServerProtocol, str | None] = {}
        self._known_devices: dict[str, str] = {}
        self._on_action: ActionHandlerFn | None = None
        self._on_button: ButtonHandlerFn | None = None
        # device_id -> {"connected", "last_seen", "address"}, and who is
        # told when one changes (binary_sensor.py)
        self._device_state: dict[str, dict[str, Any]] = {}
        self._device_listeners: list[Callable[[str], None]] = []

    async def start(self) -> None:
        """Starts the WebSocket server."""
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=10,
        )
        logger.info(
            "InfoHub WebSocket-Server gestartet auf ws://%s:%s", self.host, self.port
        )

    async def stop(self) -> None:
        """Stops the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("InfoHub WebSocket-Server gestoppt")

    def configure_panels(
        self,
        layouts: dict[str, Layout],
        default_panel_id: str | None,
        resolve_panel: ResolvePanelFn,
    ) -> None:
        """Sets which layout belongs to which panel, without pushing anything."""
        self._layouts = layouts
        self._default_panel_id = default_panel_id
        self._resolve_panel = resolve_panel

    async def refresh_layouts(
        self,
        layouts: dict[str, Layout],
        default_panel_id: str | None,
        resolve_panel: ResolvePanelFn,
    ) -> None:
        """Updates the panel map and re-sends layout to every connected client.

        Each already-connected client's device_id (remembered from its
        hello message, possibly None) is re-resolved against the new
        panel map - so re-assigning a device to a different panel, or
        editing the panel it's already on, takes effect immediately.
        """
        self.configure_panels(layouts, default_panel_id, resolve_panel)
        for websocket, device_id in list(self._connections.items()):
            await self._send_layout_for(websocket, device_id)

    def configure_button_handler(self, on_button: ButtonHandlerFn) -> None:
        """Sets the callback invoked for incoming client `button` messages."""
        self._on_button = on_button

    def configure_action_handler(self, on_action: ActionHandlerFn) -> None:
        """Sets the callback invoked for incoming client `action` messages."""
        self._on_action = on_action

    def add_device_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """listener(device_id) is called when a display connects or
        disconnects; returns the function that removes it."""
        self._device_listeners.append(listener)
        return lambda: self._device_listeners.remove(listener)

    def clear_device_listeners(self) -> None:
        """Before stop(): its closing connections then tell no one."""
        self._device_listeners.clear()

    def device_state(self, device_id: str) -> dict[str, Any] | None:
        """{"connected", "last_seen", "address"} of a display seen since
        this server started, or None."""
        return self._device_state.get(device_id)

    def _set_device_state(self, device_id: str, websocket: WebSocketServerProtocol) -> None:
        """Re-derives a display's state from its open connections (a
        display reconnecting may briefly have two) and tells listeners."""
        connected = any(d == device_id for d in self._connections.values())
        address = websocket.remote_address[0] if websocket.remote_address else None
        prev = self._device_state.get(device_id, {})
        self._device_state[device_id] = {
            "connected": connected,
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "address": address or prev.get("address"),
        }
        for listener in list(self._device_listeners):
            try:
                listener(device_id)
            except Exception:  # a listener's bug must not drop the display
                logger.exception("Device listener failed for %s", device_id)

    def get_known_devices(self) -> list[dict[str, Any]]:
        """Devices that have identified themselves at least once (for the panel dropdown)."""
        return [
            {"device_id": device_id, "last_seen": last_seen}
            for device_id, last_seen in self._known_devices.items()
        ]

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        """Handles a single client connection.

        Deliberately *not* added to self._clients (the broadcast target
        set) until its own layout/data are already sent: _await_hello()
        below can take up to HELLO_TIMEOUT seconds, and a client sitting
        in self._clients that whole time could receive an unrelated
        full_update/entity_update broadcast before its own layout_update
        - confusing ordering for no benefit.
        """
        client_addr = websocket.remote_address
        logger.info("Client verbunden: %s", client_addr)

        device_id = await self._await_hello(websocket)
        if device_id:
            self._known_devices[device_id] = datetime.now(timezone.utc).isoformat()
            logger.info("Client identifiziert: %s -> device_id=%s", client_addr, device_id)
        self._connections[websocket] = device_id
        if device_id:
            self._set_device_state(device_id, websocket)

        try:
            await self._send_layout_for(websocket, device_id)
            if self._current_data:
                await self._send_full_update(websocket)
            self._clients.add(websocket)

            async for message in websocket:
                await self._handle_client_message(client_addr, message, device_id)
        except websockets.ConnectionClosed:
            logger.info("Client getrennt: %s", client_addr)
        finally:
            self._clients.discard(websocket)
            self._connections.pop(websocket, None)
            if device_id:
                self._set_device_state(device_id, websocket)

    async def _await_hello(self, websocket: WebSocketServerProtocol) -> str | None:
        """Waits briefly for a {"type": "hello", "device_id": ...} message.

        Older/unmodified clients that never send this simply time out
        here and fall back to the default panel - no behavior change for
        them.
        """
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=HELLO_TIMEOUT)
            data = json.loads(message)
            if data.get("type") == "hello":
                return data.get("device_id")
        except (TimeoutError, ValueError, websockets.ConnectionClosed):
            pass
        return None

    async def _handle_client_message(
        self, client_addr: Any, message: str, device_id: str | None = None
    ) -> None:
        """Handles one message from an already-connected client.

        Only `{"type": "action", "entity_id": ..., "command": ...,
        "value": ...}` is currently understood - a closed vocabulary on
        purpose, not a generic service-call passthrough (see __init__.py's
        allowlist check and command dispatch in the injected handler).
        Anything else is just logged, same as before this existed.
        """
        try:
            data = json.loads(message)
        except ValueError:
            logger.debug("Ungueltige Nachricht von %s: %s", client_addr, message)
            return

        if data.get("type") == "button":
            button = data.get("button")
            press = data.get("press", "short")
            if button not in BUTTONS or press not in PRESSES:
                logger.warning("Unbekannter Button von %s: %s", client_addr, data)
                return
            if self._on_button is None:
                return
            logger.info("Button von %s (%s): %s %s", client_addr, device_id, button, press)
            await self._on_button(device_id, button, press)
            return

        if data.get("type") != "action":
            logger.debug("Nachricht von %s: %s", client_addr, message)
            return

        entity_id = data.get("entity_id")
        command = data.get("command")
        value = data.get("value")
        if not entity_id or not command:
            logger.warning("Unvollstaendige Action von %s: %s", client_addr, data)
            return
        if self._on_action is None:
            logger.warning("Action von %s erhalten, aber kein Handler konfiguriert", client_addr)
            return

        logger.info("Action von %s: %s %s (value=%s)", client_addr, command, entity_id, value)
        await self._on_action(entity_id, command, value)

    def _layout_for_device(self, device_id: str | None) -> Layout | None:
        panel_id = self._resolve_panel(device_id) if self._resolve_panel else None
        layout = self._layouts.get(panel_id) if panel_id else None
        if layout is None:
            layout = self._layouts.get(self._default_panel_id)
        return layout

    async def _send_layout_for(
        self, websocket: WebSocketServerProtocol, device_id: str | None
    ) -> None:
        layout = self._layout_for_device(device_id)
        if layout is None:
            return
        try:
            await websocket.send(json.dumps(layout.as_message()))
        except websockets.ConnectionClosed:
            pass

    async def _send_full_update(self, websocket: WebSocketServerProtocol) -> None:
        message = {
            "type": "full_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": self._current_data,
        }
        try:
            await websocket.send(json.dumps(message))
        except websockets.ConnectionClosed:
            pass

    async def broadcast_full_update(self, data: dict[str, Any]) -> None:
        """Sends a full data update to all clients."""
        self._current_data = data

        if not self._clients:
            return

        message = {
            "type": "full_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        await self._broadcast(json.dumps(message))

    async def broadcast_group_update(
        self, group: str, group_data: dict[str, Any]
    ) -> None:
        """Updates one group in _current_data and broadcasts only that group."""
        if group not in self._current_data:
            self._current_data[group] = {}
        self._current_data[group].update(group_data)

        if not self._clients:
            return

        message = {
            "type": "full_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {group: self._current_data[group]},
        }
        await self._broadcast(json.dumps(message))

    async def broadcast_entity_update(
        self, group: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        """Sends a single entity update to all clients."""
        if group in self._current_data:
            self._current_data[group][entity_id] = data

        if not self._clients:
            return

        message = {
            "type": "entity_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "group": group,
            "entity_id": entity_id,
            "data": data,
        }
        await self._broadcast(json.dumps(message))

    async def broadcast_reminder(self, event_data: dict[str, Any]) -> None:
        """Sends a transient appointment-reminder overlay to all clients."""
        if not self._clients:
            return

        message = {
            "type": "reminder",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": event_data,
        }
        await self._broadcast(json.dumps(message))

    async def _broadcast(self, message: str) -> None:
        if not self._clients:
            return

        disconnected = set()
        for client in list(self._clients):
            try:
                await client.send(message)
            except websockets.ConnectionClosed:
                disconnected.add(client)

        self._clients -= disconnected
        if disconnected:
            logger.debug("%d Client(s) entfernt", len(disconnected))

    @property
    def client_count(self) -> int:
        return len(self._clients)
