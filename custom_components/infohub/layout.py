"""Layout protocol for InfoHub display clients.

Defines the grid-based screen/widget layout that the integration sends to
display clients as a `layout_update` websocket message, kept strictly
separate from the live data (`full_update` / `entity_update`) messages.
Clients cache the layout and only re-render structure when it changes;
data flows independently and fills the widgets the layout describes.

This module is intentionally free of any Home Assistant imports so it can
be built and inspected (e.g. `Layout.as_message()`) without a running HA
instance. The layout is now built entirely from a panel's stored widget
list (see panels.py) via layout_from_panel() - there is no more
hardcoded default layout here; that lives as widgets.DEFAULT_WIDGETS,
the seed for a freshly created panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .const import DEFAULT_LANGUAGE, GROUP_AKTOREN, GROUP_CUSTOM, PROTOCOL_VERSION
from .widgets import (
    GRID_COLS,
    GRID_ROWS,
    WIDGET_SWITCH_TILE,
    WIDGET_VALUE_TILE,
    widget_label,
)

# Which full_update data group each widget type reads from. "clock" has
# none - it renders local time, not HA state. "value_tile"/"switch_tile"
# each read all of their own group (every entity in it, one per row)
# rather than a fixed set of specific ones like the other types - see
# the client's update_custom()/update_aktoren().
_TYPE_TO_GROUP = {
    "weather_current": "wetter",
    "power_gauge": "strom",
    "waste_next": "abfall",
    "indoor_climate": "raumklima",
    "calendar_month": "kalender",
    WIDGET_VALUE_TILE: GROUP_CUSTOM,
    WIDGET_SWITCH_TILE: GROUP_AKTOREN,
}

_SCREEN_LABELS = {"de": "Übersicht", "en": "Overview"}


@dataclass(frozen=True)
class WidgetPosition:
    """Grid position of a widget within its screen's grid."""

    col: int
    row: int
    colspan: int = 1
    rowspan: int = 1

    def as_dict(self) -> dict[str, int]:
        return {
            "col": self.col,
            "row": self.row,
            "colspan": self.colspan,
            "rowspan": self.rowspan,
        }


@dataclass(frozen=True)
class Widget:
    """A single widget placed on a screen.

    `data_source` binds the widget to a group (and optionally a specific
    entity_id within it) from the existing full_update data payload -
    e.g. {"group": "strom"}. `options` carries widget-specific extras
    (e.g. max_value for a gauge) that don't belong in live data.
    """

    id: str
    type: str
    pos: WidgetPosition
    label: str = ""
    data_source: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "pos": self.pos.as_dict(),
            "label": self.label,
            "data_source": self.data_source,
            "options": self.options,
        }


@dataclass(frozen=True)
class Screen:
    """A screen (page) made up of a grid of widgets."""

    id: str
    label: str
    icon: str
    grid_cols: int
    grid_rows: int
    widgets: tuple[Widget, ...]
    # widget ids in the order a narrow display stacks them; widgets not
    # listed follow in grid order
    compact_order: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "grid": {"cols": self.grid_cols, "rows": self.grid_rows},
            "widgets": [w.as_dict() for w in self.widgets],
            "compact_order": list(self.compact_order),
        }


@dataclass(frozen=True)
class Navigation:
    """Navigation bar configuration - which screens, in which order."""

    order: tuple[str, ...]
    type: str = "tab_bar"

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "order": list(self.order)}


@dataclass(frozen=True)
class Layout:
    """Full layout: all screens plus navigation between them.

    `language` is the panel's display language ("de"/"en") - display
    clients use it to pick between their own built-in string tables for
    things the server doesn't send text for (weekday/month names,
    weather condition terms, etc.); everything the server *does* send
    text for (widget labels) is already localized before it gets here.

    `title` is the panel's own name, which display clients show in their
    header.
    """

    screens: tuple[Screen, ...]
    navigation: Navigation
    language: str = DEFAULT_LANGUAGE
    title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "screens": [s.as_dict() for s in self.screens],
            "navigation": self.navigation.as_dict(),
            "language": self.language,
            "title": self.title,
        }

    def as_message(self) -> dict[str, Any]:
        """Build the full `layout_update` websocket message."""
        return {
            "type": "layout_update",
            "protocol_version": PROTOCOL_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self.as_dict(),
        }


def _widget_label(widget: dict[str, Any], language: str) -> str:
    """A widget's display label - a user-set `alias` (a plain field on
    the widget, like `pos` - not a type-specific "option") always wins
    if present, otherwise just the type's catalog label."""
    alias = (widget.get("alias") or "").strip()
    return alias or widget_label(widget["type"], language)


def _area_screen(area: dict[str, Any]) -> Screen:
    """One area's screen: a switch_tile over the whole grid, its rows
    limited to the area's entities via options["entities"] (the client
    shows only those, not the whole aktoren group)."""
    return Screen(
        id=f"area_{area['area_id']}",
        label=area["name"],
        icon="mdi:sofa",
        grid_cols=GRID_COLS,
        grid_rows=GRID_ROWS,
        widgets=(
            Widget(
                id=f"area_{area['area_id']}_switches",
                type=WIDGET_SWITCH_TILE,
                pos=WidgetPosition(col=1, row=1, colspan=GRID_COLS - 2, rowspan=GRID_ROWS - 2),
                label=area["name"],
                data_source={"group": GROUP_AKTOREN},
                options={"entities": [entity_id for entity_id, _ in area["entities"]]},
            ),
        ),
    )


def layout_from_panel(
    panel: dict[str, Any], area_screens: tuple[dict[str, Any], ...] | list = ()
) -> Layout:
    """Builds the wire-protocol Layout from a panel's stored widget list.

    The "overview" screen is the panel's `widgets` list; it is left out
    when the list is empty and the panel has area screens. Each of
    `area_screens` (areas.area_screens(): the panel's `areas`, resolved)
    follows as a screen of its own, swiped through on the display. Each widget's data_source
    and display label are derived from its type via widgets.widget_label()/
    _TYPE_TO_GROUP (or a user-set `alias`, see _widget_label()), not
    stored per-instance, since those are inherent to the type, not
    something the user configures per widget. The display label is
    resolved to the panel's own `language` (see Layout).
    """
    language = panel.get("language") or DEFAULT_LANGUAGE
    widgets = tuple(
        Widget(
            id=w["id"],
            type=w["type"],
            pos=WidgetPosition(**w["pos"]),
            label=_widget_label(w, language),
            data_source=(
                {"group": _TYPE_TO_GROUP[w["type"]]} if w["type"] in _TYPE_TO_GROUP else {}
            ),
            options=w.get("options", {}),
        )
        for w in panel.get("widgets", [])
    )
    ids = {w.id for w in widgets}
    screen = Screen(
        id="overview",
        label=_SCREEN_LABELS.get(language, _SCREEN_LABELS["en"]),
        icon="mdi:home",
        grid_cols=GRID_COLS,
        grid_rows=GRID_ROWS,
        widgets=widgets,
        compact_order=tuple(i for i in panel.get("compact_order") or () if i in ids),
    )
    screens = tuple(_area_screen(a) for a in area_screens)
    if widgets or not screens:
        screens = (screen,) + screens
    return Layout(
        screens=screens,
        navigation=Navigation(order=tuple(s.id for s in screens)),
        language=language,
        title=panel.get("name") or "",
    )
