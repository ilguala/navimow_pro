"""Make the integration's HA-free modules importable without Home Assistant.

Importing ``custom_components.navimow_pro`` would run its ``__init__.py``, which
needs Home Assistant. The package is registered by path instead, so a test can
import a module that only depends on ``const`` -- such as ``event_detector`` --
with nothing but pytest installed.
"""
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "custom_components"

for name, path in (
    ("custom_components", _ROOT),
    ("custom_components.navimow_pro", _ROOT / "navimow_pro"),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
