from infohub.layout import layout_from_panel


def panel(**kw):
    base = {"id": "p", "name": "R1 Office", "language": "en", "widgets": []}
    base.update(kw)
    return base


WIDGET = {"id": "w1", "type": "clock", "pos": {"col": 1, "row": 1, "colspan": 7, "rowspan": 3}}
ROOM = {"area_id": "office", "name": "Office", "entities": [("light.desk", "Desk")]}


def test_title_is_the_panel_name():
    message = layout_from_panel(panel(widgets=[WIDGET])).as_message()
    assert message["type"] == "layout_update"
    assert message["title"] == "R1 Office"
    assert message["language"] == "en"


def test_overview_left_out_when_only_rooms():
    layout = layout_from_panel(panel(), [ROOM]).as_dict()
    assert [s["id"] for s in layout["screens"]] == ["area_office"]
    assert layout["navigation"]["order"] == ["area_office"]


def test_overview_first_then_rooms():
    layout = layout_from_panel(panel(widgets=[WIDGET]), [ROOM]).as_dict()
    assert [s["id"] for s in layout["screens"]] == ["overview", "area_office"]
    room = layout["screens"][1]
    (switches,) = room["widgets"]
    assert switches["type"] == "switch_tile"
    assert switches["options"]["entities"] == ["light.desk"]


def test_empty_panel_still_has_a_screen():
    layout = layout_from_panel(panel()).as_dict()
    assert [s["id"] for s in layout["screens"]] == ["overview"]


def test_alias_overrides_the_label():
    layout = layout_from_panel(panel(widgets=[{**WIDGET, "alias": "Time"}])).as_dict()
    assert layout["screens"][0]["widgets"][0]["label"] == "Time"


def test_compact_order_keeps_only_widgets_that_exist():
    widgets = [WIDGET, {**WIDGET, "id": "w2", "type": "weather_current"}]
    layout = layout_from_panel(panel(widgets=widgets, compact_order=["w2", "gone", "w1"])).as_dict()
    assert layout["screens"][0]["compact_order"] == ["w2", "w1"]


def test_no_compact_order_by_default():
    layout = layout_from_panel(panel(widgets=[WIDGET])).as_dict()
    assert layout["screens"][0]["compact_order"] == []
