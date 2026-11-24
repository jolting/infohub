"""Display connectivity: one binary sensor per display client.

A display that identifies itself (its hello's device_id) gets a
connectivity sensor, on while it has a connection to InfoHub's WebSocket
server, and a device of its own in Home Assistant's device registry (so
it can be put in an area like any other device). Its attributes say when
it last connected or disconnected and from which address.

Displays seen before are re-created from the entity registry at start,
so one that is offline shows as disconnected after Home Assistant
restarts rather than vanishing until it connects again.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

UNIQUE_PREFIX = "display_"


def _unique_id(device_id: str) -> str:
    return f"{UNIQUE_PREFIX}{device_id}_connected"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    server = hass.data[DOMAIN][entry.entry_id]["server"]
    entities: dict[str, DisplayConnectivity] = {}

    def add(device_id: str) -> None:
        if device_id in entities:
            return
        entities[device_id] = DisplayConnectivity(server, device_id)
        async_add_entities([entities[device_id]])

    # displays known from before (the entity registry outlives restarts)
    for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        uid = reg.unique_id
        if reg.domain == "binary_sensor" and uid.startswith(UNIQUE_PREFIX) and uid.endswith(
            "_connected"
        ):
            add(uid[len(UNIQUE_PREFIX) : -len("_connected")])
    # and any already connected
    for known in server.get_known_devices():
        add(known["device_id"])

    @callback
    def on_change(device_id: str) -> None:
        if device_id in entities:
            if entities[device_id].hass is not None:  # added already
                entities[device_id].async_write_ha_state()
        else:
            add(device_id)

    entry.async_on_unload(server.add_device_listener(on_change))


class DisplayConnectivity(BinarySensorEntity):
    """On while the display is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_name = None  # the device's name: "r1", say
    _attr_should_poll = False

    def __init__(self, server: Any, device_id: str) -> None:
        self._server = server
        self._device_id = device_id
        self._attr_unique_id = _unique_id(device_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_id,
            manufacturer="InfoHub",
            model="Display",
        )

    @property
    def is_on(self) -> bool:
        state = self._server.device_state(self._device_id)
        return bool(state and state["connected"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._server.device_state(self._device_id) or {}
        return {
            "device_id": self._device_id,
            "last_seen": state.get("last_seen"),
            "address": state.get("address"),
        }
