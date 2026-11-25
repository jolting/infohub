"""InfoHub - Home Assistant integration.

Hosts the layout protocol (layout.py), starts the display-client
WebSocket server, and runs the data coordinator (coordinator.py) that
reads hass.states and pushes live updates to connected clients.

Panels (panels.py + panel/infohub-panel.js) are managed via a sidebar
config panel, each with its own widget layout and optionally an
`assigned_device_id` - a connecting display client identifies itself
(`{"type": "hello", "device_id": ...}`) and InfoHubWebSocketServer
resolves that to a specific panel's layout (panels.resolve_panel_id()),
falling back to the default panel for unidentified/unassigned devices.
All panels still share one coordinator/data stream (see panels.py's
union_panel_entities() and the implementation plan's "Nicht enthalten"
section for why). Changing a panel takes effect live - no HA reload -
via InfoHubCoordinator.async_apply_entities() (data) and
InfoHubWebSocketServer.refresh_layouts() (layout).

Still open: Medikamente wurden komplett verworfen, kein Ersatz vorgesehen.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import collection
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .areas import area_entity_configs, area_screens

from .const import (
    CONF_CALENDAR_ENTITY_ID,
    CONF_CALENDAR_POLL_INTERVAL,
    CONF_GTS_COUNTRY,
    CONF_GTS_PLZ,
    CONF_GTS_POLL_INTERVAL,
    CONF_WS_PORT,
    DEFAULT_CALENDAR_POLL_INTERVAL,
    DEFAULT_GTS_COUNTRY,
    DEFAULT_GTS_PLZ,
    DEFAULT_GTS_POLL_INTERVAL,
    DEFAULT_WS_PORT,
    DOMAIN,
    GROUP_AKTOREN,
)
from .coordinator import InfoHubCoordinator
from .entities import build_entity_groups
from .layout import layout_from_panel
from .panels import (
    CREATE_FIELDS,
    UPDATE_FIELDS,
    WS_API_PREFIX,
    WS_MODEL_NAME,
    async_setup_panel_collection,
    get_default_panel,
    resolve_panel_id,
    union_panel_entities,
)
from .pairing import DisplayAuth
from .websocket_server import InfoHubWebSocketServer

logger = logging.getLogger(__name__)

PANEL_URL_PATH = "infohub"
BUTTON_EVENT = f"{DOMAIN}_button"
PLATFORMS = ["binary_sensor"]
PANEL_JS_URL = "/api/infohub/panel.js"
PANEL_JS_FILE = Path(__file__).parent / "panel" / "infohub-panel.js"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up InfoHub from a config entry."""
    port = entry.options.get(CONF_WS_PORT, entry.data.get(CONF_WS_PORT, DEFAULT_WS_PORT))

    panel_collection = await async_setup_panel_collection(hass, entry.options)
    panels = panel_collection.async_items()
    default_panel = get_default_panel(panels)
    default_panel_id = default_panel["id"] if default_panel else None

    def _expanded(panels: list[dict]) -> list[tuple[dict, list[dict]]]:
        """Each panel with its areas resolved to screens (areas.py), and
        those screens' entities added to the panel's own."""
        out = []
        for panel in panels:
            screens = area_screens(hass, panel.get("areas") or [])
            entities = list(panel.get("entities", [])) + area_entity_configs(screens)
            out.append(({**panel, "entities": entities}, screens))
        return out

    def _build_layouts(panels: list[dict]) -> dict[str, object]:
        return {p["id"]: layout_from_panel(p, screens) for p, screens in _expanded(panels)}

    def _all_entities(panels: list[dict]) -> list[dict]:
        return union_panel_entities([p for p, _ in _expanded(panels)])

    entity_groups = build_entity_groups(_all_entities(panels))

    def _resolve_panel(device_id: str | None) -> str | None:
        # Frisch aus der Collection lesen statt eine Kopie einzufangen,
        # damit spaeter angelegte/geaenderte Zuordnungen sofort gelten.
        return resolve_panel_id(panel_collection.async_items(), device_id)

    async def _resolve_action(entity_id: str, command: str, value: float | None = None) -> None:
        # Allowlist: nur Entities, die jemand explizit zur "aktoren"-
        # Gruppe eines Panels hinzugefuegt hat, sind ueberhaupt schaltbar -
        # der WS-Server selbst hat keine Auth, das begrenzt zumindest den
        # Schaden (kein beliebiger Service-Call auf x-beliebige Entities).
        allowed_ids = {
            e["entity_id"]
            for e in _all_entities(panel_collection.async_items())
            if e.get("group") == GROUP_AKTOREN
        }
        if entity_id not in allowed_ids:
            logger.warning("Action fuer nicht freigegebene Entity ignoriert: %s", entity_id)
            return

        # Service dispatch is by the entity's actual HA domain, not by the
        # widget's stored "control" type - the allowlist above is the only
        # gate; which commands make sense for an entity follows from its
        # domain the same way the rest of HA works.
        domain = entity_id.split(".", 1)[0]

        if command == "toggle":
            await hass.services.async_call(
                "homeassistant", "toggle", {"entity_id": entity_id}, blocking=False
            )
            return

        if command in ("open_cover", "close_cover", "stop_cover"):
            if domain != "cover":
                logger.warning("Cover-Kommando fuer Nicht-Cover-Entity ignoriert: %s", entity_id)
                return
            await hass.services.async_call(
                "cover", command, {"entity_id": entity_id}, blocking=False
            )
            return

        if command == "set_value":
            if value is None:
                logger.warning("set_value ohne Wert ignoriert: %s", entity_id)
                return
            if domain in ("number", "input_number"):
                await hass.services.async_call(
                    domain, "set_value", {"entity_id": entity_id, "value": value}, blocking=False
                )
            elif domain == "light":
                if value <= 0:
                    await hass.services.async_call(
                        "light", "turn_off", {"entity_id": entity_id}, blocking=False
                    )
                else:
                    await hass.services.async_call(
                        "light",
                        "turn_on",
                        {"entity_id": entity_id, "brightness_pct": value},
                        blocking=False,
                    )
            elif domain == "cover":
                await hass.services.async_call(
                    "cover",
                    "set_cover_position",
                    {"entity_id": entity_id, "position": value},
                    blocking=False,
                )
            else:
                logger.warning("set_value fuer unbekannte Domain ignoriert: %s", entity_id)
            return

        logger.warning("Unbekanntes Action-Kommando ignoriert: %s", command)

    server = InfoHubWebSocketServer(
        host="0.0.0.0", port=port, auth=await _async_display_auth(hass, entry)
    )
    server.configure_panels(_build_layouts(panels), default_panel_id, _resolve_panel)
    server.configure_action_handler(_resolve_action)

    async def _on_button(device_id: str | None, button: str, press: str) -> None:
        # A display's hardware button, for automations: trigger on the
        # event "infohub_button" (data: device_id, button, press).
        hass.bus.async_fire(
            BUTTON_EVENT, {"device_id": device_id, "button": button, "press": press}
        )

    server.configure_button_handler(_on_button)
    await server.start()

    # A lone leftover string from before multi-calendar select existed is
    # read as a one-item list - see config_flow.py's same fallback.
    raw_calendar = entry.options.get(CONF_CALENDAR_ENTITY_ID)
    calendar_entity_ids = (
        raw_calendar if isinstance(raw_calendar, list) else ([raw_calendar] if raw_calendar else [])
    )

    coordinator = InfoHubCoordinator(
        hass,
        server,
        entity_groups=entity_groups,
        gts_plz=entry.options.get(CONF_GTS_PLZ, DEFAULT_GTS_PLZ),
        gts_country=entry.options.get(CONF_GTS_COUNTRY, DEFAULT_GTS_COUNTRY),
        gts_poll_interval=entry.options.get(CONF_GTS_POLL_INTERVAL, DEFAULT_GTS_POLL_INTERVAL),
        calendar_entity_ids=calendar_entity_ids,
        calendar_poll_interval=entry.options.get(
            CONF_CALENDAR_POLL_INTERVAL, DEFAULT_CALENDAR_POLL_INTERVAL
        ),
    )
    await coordinator.async_start()

    last_layouts: dict[str, object] = {
        pid: layout.as_dict() for pid, layout in _build_layouts(panels).items()
    }

    async def _refresh(only_if_changed: bool = False) -> None:
        """Re-applies layouts/entities for all panels, live. With
        only_if_changed, nothing is resent unless a layout changed: a
        display rebuilds (and goes back to its first screen) on every
        layout it gets."""
        current_panels = panel_collection.async_items()
        layouts = _build_layouts(current_panels)
        as_dicts = {pid: layout.as_dict() for pid, layout in layouts.items()}
        if only_if_changed and as_dicts == last_layouts:
            return
        last_layouts.clear()
        last_layouts.update(as_dicts)
        current_default = get_default_panel(current_panels)
        await server.refresh_layouts(
            layouts,
            current_default["id"] if current_default else None,
            _resolve_panel,
        )
        await coordinator.async_apply_entities(build_entity_groups(_all_entities(current_panels)))

    refresh_lock = asyncio.Lock()

    async def _refresh_serialized() -> None:
        """One _refresh() at a time, in the order the edits came."""
        async with refresh_lock:
            await _refresh()

    async def _on_panels_changed(change_type: str, item_id: str, item: dict) -> None:
        """collection.notify_changes() awaits every registered listener via
        asyncio.gather() - it must be a coroutine function, not a plain
        @callback (that crashed with "An asyncio.Future, a coroutine or
        an awaitable is required" the moment a panel was actually edited).

        The push runs as a task of its own: the editor's save waits for
        these listeners, and must not wait on the displays as well.
        """
        hass.async_create_task(_refresh_serialized())

    unsub_panels = panel_collection.async_add_listener(_on_panels_changed)

    # Area screens follow Home Assistant's area assignments: an entity,
    # device or area changed re-resolves them, a couple of seconds after
    # the last change (renaming or moving several things in a row is
    # one refresh).
    pending_refresh: list = [None]

    async def _registry_refresh(_now) -> None:
        pending_refresh[0] = None
        await _refresh(only_if_changed=True)

    @callback
    def _on_registry_updated(event) -> None:
        if not any(p.get("areas") for p in panel_collection.async_items()):
            return
        if pending_refresh[0] is not None:
            pending_refresh[0]()
        pending_refresh[0] = async_call_later(hass, 2, _registry_refresh)

    unsub_registries = [
        hass.bus.async_listen(event_type, _on_registry_updated)
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
        )
    ]

    @callback
    def _unsub_registries() -> None:
        for unsub in unsub_registries:
            unsub()
        if pending_refresh[0] is not None:
            pending_refresh[0]()

    entry.async_on_unload(_unsub_registries)

    collection.DictStorageCollectionWebsocket(
        panel_collection, WS_API_PREFIX, WS_MODEL_NAME, CREATE_FIELDS, UPDATE_FIELDS
    ).async_setup(hass)

    @websocket_api.websocket_command({vol.Required("type"): "infohub/devices/list"})
    @callback
    def _ws_list_devices(hass: HomeAssistant, connection, msg: dict) -> None:
        """Known device_ids (have sent a hello at least once) - for the panel's device dropdown."""
        connection.send_result(msg["id"], server.get_known_devices())

    websocket_api.async_register_command(hass, _ws_list_devices)

    @websocket_api.websocket_command({vol.Required("type"): "infohub/displays/list"})
    @callback
    def _ws_list_displays(hass: HomeAssistant, connection, msg: dict) -> None:
        """Approved displays and those waiting for approval."""
        connection.send_result(msg["id"], server.displays())

    @websocket_api.websocket_command(
        {vol.Required("type"): "infohub/displays/approve", vol.Required("device_id"): str}
    )
    @callback
    def _ws_approve_display(hass: HomeAssistant, connection, msg: dict) -> None:
        server.approve(msg["device_id"])
        connection.send_result(msg["id"], server.displays())

    @websocket_api.websocket_command(
        {vol.Required("type"): "infohub/displays/forget", vol.Required("device_id"): str}
    )
    @websocket_api.async_response
    async def _ws_forget_display(hass: HomeAssistant, connection, msg: dict) -> None:
        await server.forget(msg["device_id"])
        connection.send_result(msg["id"], server.displays())

    for command in (_ws_list_displays, _ws_approve_display, _ws_forget_display):
        websocket_api.async_register_command(hass, command)

    # cache_headers=False, solange das Panel aktiv weiterentwickelt wird -
    # mit True setzt HA hier Cache-Control: max-age=31 Tage, dann sieht
    # der Browser Aenderungen erst nach explizitem Cache-Leeren.
    #
    # Registered at most once per HA process lifetime, guarded via
    # hass.data (survives entry reloads, unlike hass.data[DOMAIN], which
    # async_unload_entry empties per-entry) - the underlying aiohttp
    # route table lives on hass.http itself, outside any config entry's
    # lifecycle, and raises if the same route is added twice. Every
    # options change reloads this entry (_async_options_updated below),
    # so without this guard the second setup_entry of any HA session
    # always failed here - and since that happened *after* the
    # WebSocket server had already (re-)bound its port, the failed
    # attempt's server was never cleaned up and orphaned the port for
    # every subsequent attempt too.
    if not hass.data.get(f"{DOMAIN}_static_path_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_JS_URL, str(PANEL_JS_FILE), False)]
        )
        hass.data[f"{DOMAIN}_static_path_registered"] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="infohub-panel",
        sidebar_title="InfoHub",
        sidebar_icon="mdi:monitor-dashboard",
        module_url=PANEL_JS_URL,
        require_admin=True,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "server": server,
        "coordinator": coordinator,
        "panel_collection": panel_collection,
        "unsub_panels": unsub_panels,
    }

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # each display's connectivity (binary_sensor.py)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_display_auth(hass: HomeAssistant, entry: ConfigEntry) -> DisplayAuth:
    """The pairing store (pairing.py), kept in .storage/infohub_displays.
    At its first start the displays Home Assistant already knows (their
    connectivity sensors) are approved, so they keep working."""
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}_displays")
    data = await store.async_load()

    @callback
    def _save(new_data: dict) -> None:
        store.async_delay_save(lambda: new_data, 1)

    auth = DisplayAuth(data, _save)
    known = [
        reg.unique_id[len("display_") : -len("_connected")]
        for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if reg.domain == "binary_sensor"
        and reg.unique_id.startswith("display_")
        and reg.unique_id.endswith("_connected")
    ]
    auth.grandfather(known)
    return auth


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an InfoHub config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry_data = hass.data[DOMAIN].pop(entry.entry_id)
    entry_data["unsub_panels"]()
    entry_data["coordinator"].async_stop()
    entry_data["server"].clear_device_listeners()
    await entry_data["server"].stop()
    async_remove_panel(hass, PANEL_URL_PATH)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reloads the entry so a changed WS-Port/GTS/Kalender-Einstellung greift.

    Panel-Aenderungen (Entities/Widgets/Geraete-Zuordnung) brauchen das
    nicht - die gehen live ueber async_apply_entities()/refresh_layouts().
    """
    await hass.config_entries.async_reload(entry.entry_id)
