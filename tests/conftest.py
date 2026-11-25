"""Loads the integration's modules as the package "infohub" without
running its __init__.py, which needs Home Assistant. The modules tested
here (pairing, areas, layout, the WebSocket server) don't import it."""

import sys
import types
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "infohub"

if "infohub" not in sys.modules:
    package = types.ModuleType("infohub")
    package.__path__ = [str(INTEGRATION)]
    sys.modules["infohub"] = package
