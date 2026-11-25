from types import SimpleNamespace

from infohub.areas import _device_map, pick_area_entities


def entry(entity_id, area_id=None, device_id=None, name=None, original_name=None, **kw):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        area_id=area_id,
        device_id=device_id,
        name=name,
        original_name=original_name,
        disabled_by=kw.get("disabled_by"),
        hidden_by=kw.get("hidden_by"),
        entity_category=kw.get("entity_category"),
    )


def device(device_id, area_id=None, name=None, **kw):
    return SimpleNamespace(
        id=device_id,
        area_id=area_id,
        name=name,
        name_by_user=kw.get("name_by_user"),
        disabled_by=kw.get("disabled_by"),
    )


class Devices:
    """registry.devices as current Home Assistant has it: iterating
    gives the entries; mapping-style lookups are what made Home
    Assistant walk the stack on every call, so they fail here."""

    def __init__(self, devices):
        self._devices = devices

    def __iter__(self):
        return iter(self._devices)

    def get(self, *_):
        raise AssertionError("mapping lookup on registry.devices")

    __getitem__ = get


class Registry:
    def __init__(self, devices, iterate_ids=False):
        self._by_id = {d.id: d for d in devices}
        self.devices = list(self._by_id) if iterate_ids else Devices(devices)

    def async_get(self, device_id):
        return self._by_id.get(device_id)


def test_device_map_iterates_entries():
    d = [device("a"), device("b")]
    assert _device_map(Registry(d)) == {"a": d[0], "b": d[1]}


def test_device_map_iterates_ids_on_older_versions():
    d = [device("a"), device("b")]
    assert _device_map(Registry(d, iterate_ids=True)) == {"a": d[0], "b": d[1]}


def test_area_from_the_device_and_from_the_entity():
    devices = {"lamp": device("lamp", area_id="office", name="Desk lamp")}
    entries = [
        entry("light.desk", device_id="lamp"),
        entry("fan.ceiling", area_id="office", name="Fan"),
        entry("switch.printer", area_id="office", name="Printer plug"),  # switches are left out
        entry("light.hall", area_id="hall", name="Hall"),
    ]
    picked = dict(pick_area_entities(entries, devices, "office"))
    assert picked == {"light.desk": "Desk lamp", "fan.ceiling": "Fan"}


def test_disabled_hidden_and_config_entities_left_out():
    devices = {
        "off": device("off", area_id="office", disabled_by="user"),
    }
    entries = [
        entry("light.a", area_id="office", disabled_by="user", name="A"),
        entry("light.b", area_id="office", hidden_by="user", name="B"),
        entry("switch.c", area_id="office", entity_category="config", name="C"),
        entry("light.d", device_id="off", name="D"),
        entry("sensor.e", area_id="office", name="E"),  # not a domain a room screen shows
    ]
    assert pick_area_entities(entries, devices, "office") == []


def test_area_screens_never_looks_devices_up_as_a_mapping(monkeypatch):
    """area_screens() as Home Assistant calls it: through the registries.
    Mapping lookups on registry.devices made Home Assistant inspect the
    stack on every call, blocking it for seconds; Devices rejects them."""
    import sys
    import types

    from infohub.areas import area_screens

    office = SimpleNamespace(name="Office")
    areas = SimpleNamespace(async_get_area=lambda area_id: office if area_id == "office" else None)
    registry = Registry([device("lamp", area_id="office", name="Desk lamp")])
    entities = SimpleNamespace(entities={"light.desk": entry("light.desk", device_id="lamp")})

    helpers = types.ModuleType("homeassistant.helpers")
    modules = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.area_registry": SimpleNamespace(async_get=lambda hass: areas),
        "homeassistant.helpers.device_registry": SimpleNamespace(async_get=lambda hass: registry),
        "homeassistant.helpers.entity_registry": SimpleNamespace(async_get=lambda hass: entities),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    for short in ("area_registry", "device_registry", "entity_registry"):
        monkeypatch.setattr(helpers, short, modules[f"homeassistant.helpers.{short}"], raising=False)

    screens = area_screens(object(), ["office", "gone"])
    assert screens == [
        {"area_id": "office", "name": "Office", "entities": [("light.desk", "Desk lamp")]}
    ]
