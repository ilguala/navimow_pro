"""Make the integration importable for tests without Home Assistant.

Importing ``custom_components.navimow_pro`` would run its ``__init__.py``, which
sets up the whole integration. The package is registered by path instead, so a
test imports just the module it needs.

Modules such as ``camera`` and ``coordinator`` import Home Assistant at the top,
for base classes and type hints the tested functions never touch. When Home
Assistant is not installed, those names are filled with bare placeholders so the
modules import and their pure logic -- map decoding, label placement, the SVG --
can be tested with only ``requirements_test.txt``. With Home Assistant installed,
the real one is used.
"""
import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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


def _placeholder_module(name: str) -> types.ModuleType:
    """A module whose every attribute is a distinct, subclassable placeholder."""
    module = types.ModuleType(name)
    module.__path__ = []
    cache: dict[str, object] = {}

    def __getattr__(attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        if attr not in cache:
            if attr == "callback":
                cache[attr] = lambda func: func
            else:
                # Generic-subscriptable (DataUpdateCoordinator[dict]) and
                # constructible with anything; exceptions stay exceptions.
                base = Exception if attr.endswith(("Error", "Failed")) else object
                cache[attr] = type(
                    attr,
                    (base,),
                    {
                        "__class_getitem__": classmethod(lambda cls, _item: cls),
                        "__init__": lambda self, *args, **kwargs: None,
                    },
                )
        return cache[attr]

    module.__getattr__ = __getattr__
    return module


if importlib.util.find_spec("homeassistant") is None:
    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.camera",
        "homeassistant.components.event",
        "homeassistant.components.switch",
        "homeassistant.const",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        sys.modules[name] = _placeholder_module(name)
        # "from homeassistant.helpers import device_registry" must find the
        # module, not a placeholder class standing in for an attribute.
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, sys.modules[name])
    # services.py builds its schemas with voluptuous, which ships with Home
    # Assistant; a placeholder lets its lookups be tested without either.
    if importlib.util.find_spec("voluptuous") is None:
        sys.modules["voluptuous"] = _placeholder_module("voluptuous")

    @dataclass(frozen=True, kw_only=True)
    class SwitchEntityDescription:
        key: str
        translation_key: str | None = None
        icon: str | None = None
        entity_category: object = None

    class EntityCategory(Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    sys.modules["homeassistant.components.switch"].SwitchEntityDescription = SwitchEntityDescription
    sys.modules["homeassistant.const"].EntityCategory = EntityCategory
    # The coordinator asks for the local date (the mowing reports' month); a
    # test that cares which date it is patches dt_util itself.
    sys.modules["homeassistant.util.dt"].now = lambda: datetime.now(timezone.utc)
