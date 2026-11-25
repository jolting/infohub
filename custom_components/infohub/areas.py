"""Area screens: a panel's `areas` list, one screen per Home Assistant area.

Each area listed on a panel becomes its own screen on the display, with a
switch_tile of that area's controllable entities, so a display can be
swiped from room to room. Which entities an area screen shows follows
from Home Assistant's own area assignments (an entity's area, or its
device's), so a light added to an area shows up without editing the
panel:

- only lights, fans and covers (AREA_DOMAINS): a switch in an area is as
  often a printer's or a heater's plug, or a device's setting (a Govee
  light's "Dream View" toggle), as a lamp;
- no disabled, hidden, or configuration/diagnostic entities;
- per device, only its main entity (the one named after the device,
  original_name None) when it has one: a Govee string light is one light
  plus ten "Segment" lights, and only the first is the string.

pick_area_entities() is the rule on plain registry entries, free of Home
Assistant imports; area_screens() applies it to the live registries.
"""

from __future__ import annotations

from typing import Any, Iterable

from .const import GROUP_AKTOREN

AREA_DOMAINS = ("light", "fan", "cover", "humidifier", "media_player", "scene")

# The switch_tile control an area entity gets, by domain (coordinator.py
# makes a toggle a dimmer where the entity has a level)
AREA_CONTROLS = {"cover": "cover", "media_player": "media", "scene": "scene"}


def _device_name(device: Any) -> str | None:
    if device is None:
        return None
    return getattr(device, "name_by_user", None) or getattr(device, "name", None)


def pick_area_entities(
    entries: Iterable[Any], devices: dict[str, Any], area_id: str
) -> list[tuple[str, str]]:
    """(entity_id, label) of the entities an area's screen shows.

    `entries` are entity-registry entries (entity_id, domain, area_id,
    device_id, name, original_name, disabled_by, hidden_by,
    entity_category); `devices` maps device ids to device-registry
    entries (area_id, name, name_by_user, disabled_by).
    """
    by_device: dict[str, list[Any]] = {}
    for entry in entries:
        if entry.domain not in AREA_DOMAINS:
            continue
        if entry.disabled_by or entry.hidden_by or entry.entity_category:
            continue
        device = devices.get(entry.device_id) if entry.device_id else None
        if device is not None and device.disabled_by:
            continue
        entry_area = entry.area_id or (device.area_id if device is not None else None)
        if entry_area != area_id:
            continue
        by_device.setdefault(entry.device_id or entry.entity_id, []).append(entry)

    picked: list[tuple[str, str]] = []
    for group in by_device.values():
        main = [e for e in group if e.original_name is None]
        keep = main or group
        for entry in keep:
            device = devices.get(entry.device_id) if entry.device_id else None
            if entry.name:
                label = entry.name
            elif entry.original_name is None or len(keep) == 1:
                # named after its device, or the device's only entity here
                label = _device_name(device) or entry.original_name or entry.entity_id
            else:
                label = entry.original_name
            picked.append((entry.entity_id, label))
    return sorted(picked, key=lambda p: p[1].lower())


def _device_map(registry: Any) -> dict[str, Any]:
    """Device id -> device entry, built once. Looking devices up through
    registry.devices as a mapping is deprecated, and Home Assistant checks
    the caller's stack on every such lookup to report it: done for every
    entity on every panel change, that blocked the event loop for
    seconds. Iterating it gives the entries (the ids on older versions)."""
    out: dict[str, Any] = {}
    for item in registry.devices:
        device = registry.async_get(item) if isinstance(item, str) else item
        if device is not None:
            out[device.id] = device
    return out


def area_screens(hass: Any, area_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The screens for a panel's areas, in the panel's order: each
    {"area_id", "name", "entities": [(entity_id, label), ...]}. An area
    that no longer exists is left out."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    areas = ar.async_get(hass)
    devices = _device_map(dr.async_get(hass))
    entries = list(er.async_get(hass).entities.values())
    screens = []
    for area_id in area_ids:
        area = areas.async_get_area(area_id)
        if area is None:
            continue
        screens.append(
            {
                "area_id": area_id,
                "name": area.name,
                "entities": pick_area_entities(entries, devices, area_id),
            }
        )
    return screens


def area_entity_configs(screens: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entity configs (panels.py's stored shape) the coordinator
    tracks for area screens: each one a switch_tile row, so actionable;
    a cover as open/stop/close buttons, a media player as its own row, a
    scene as a button, the rest as toggles (dimmers where they have a
    level)."""
    return [
        {
            "entity_id": entity_id,
            "label": label,
            "group": GROUP_AKTOREN,
            "unit": None,
            "type": AREA_CONTROLS.get(entity_id.split(".", 1)[0]),
            "min_value": None,
            "max_value": None,
        }
        for screen in screens
        for entity_id, label in screen["entities"]
    ]
