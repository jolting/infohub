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

import unicodedata
from typing import Any, Iterable

from .const import GROUP_AKTOREN

AREA_DOMAINS = ("light", "fan", "cover")


def font_safe(name: str) -> str:
    """A name a display's font can draw: the clients' fonts (LVGL's
    Montserrat) cover Latin text only, and an emoji comes out blank.
    Symbols are dropped from a name that has words besides them
    ("\U0001F384 Den" -> "Den"); a name of symbols only is spelled out
    ("\U0001F384" -> "Christmas Tree")."""
    kept = []
    symbols = []
    for ch in name:
        if ord(ch) <= 0xFF or unicodedata.category(ch)[0] in "LNPZ":
            kept.append(ch)
        elif unicodedata.category(ch) == "So":
            symbols.append(unicodedata.name(ch, "").title())
        # anything else (variation selectors, joiners) is dropped
    text = " ".join("".join(kept).split())
    if text:
        return text
    return " ".join(s for s in symbols if s) or name


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
            picked.append((entry.entity_id, font_safe(label)))
    return sorted(picked, key=lambda p: p[1].lower())


def area_screens(hass: Any, area_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The screens for a panel's areas, in the panel's order: each
    {"area_id", "name", "entities": [(entity_id, label), ...]}. An area
    that no longer exists is left out."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    areas = ar.async_get(hass)
    devices = dr.async_get(hass).devices
    entries = list(er.async_get(hass).entities.values())
    screens = []
    for area_id in area_ids:
        area = areas.async_get_area(area_id)
        if area is None:
            continue
        screens.append(
            {
                "area_id": area_id,
                "name": font_safe(area.name),
                "entities": pick_area_entities(entries, devices, area_id),
            }
        )
    return screens


def area_entity_configs(screens: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entity configs (panels.py's stored shape) the coordinator
    tracks for area screens: each one a switch_tile row, so actionable;
    a cover as open/stop/close buttons, the rest as toggles."""
    return [
        {
            "entity_id": entity_id,
            "label": label,
            "group": GROUP_AKTOREN,
            "unit": None,
            "type": "cover" if entity_id.startswith("cover.") else None,
            "min_value": None,
            "max_value": None,
        }
        for screen in screens
        for entity_id, label in screen["entities"]
    ]
