"""The Navimow (Private) integration.

Unofficial, community interoperability integration for Segway Navimow robot
mowers, talking to the private mobile-app cloud (p:101). See README.
"""
from __future__ import annotations

import logging
import os
import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.CAMERA,
    Platform.CALENDAR,
]

# Frontend cards served + auto-loaded so users don't have to add a Lovelace
# resource by hand. Registered once per HA process (guard in hass.data).
# Bump _CARD_VER whenever any card JS changes: it is appended as ?v=<ver> to the
# served URL so browsers fetch the new file instead of a stale cached copy.
_CARDS = ("navimow-scheduler-card.js", "navimow-mow-card.js")
_CARD_VER = "4"
_FRONTEND_KEY = f"{DOMAIN}_frontend_registered"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the custom cards and auto-load them as extra JS modules.

    Primary method (freeze-proof): copy each card into ``<config>/www`` and
    serve it at ``/local/...`` — the ``/local`` static route is registered at
    HTTP startup, so this works even when this integration sets up AFTER Home
    Assistant has started (e.g. a config-entry retry after an auth failure),
    where ``async_register_static_paths`` would raise because the router is
    already frozen. Falls back to a direct static-path registration per card if
    the copy fails. Logs at INFO/WARNING (visible without debug). Idempotent
    per process.
    """
    if hass.data.get(_FRONTEND_KEY):
        return
    here = os.path.dirname(__file__)
    ok_any = False
    for filename in _CARDS:
        src = os.path.join(here, "www", filename)
        if not os.path.isfile(src):
            _LOGGER.warning("Navimow: card asset missing at %s", src)
            continue

        url: str | None = None
        # Primary: copy into <config>/www/navimow_pro and serve via /local.
        try:
            www_dir = hass.config.path("www", DOMAIN)
            dst = os.path.join(www_dir, filename)

            def _copy(_src: str = src, _dst: str = dst, _dir: str = www_dir) -> None:
                os.makedirs(_dir, exist_ok=True)
                shutil.copyfile(_src, _dst)

            await hass.async_add_executor_job(_copy)
            url = f"/local/{DOMAIN}/{filename}?v={_CARD_VER}"
            _LOGGER.info("Navimow: card copied to www, serving at %s", url)
        except Exception:  # noqa: BLE001 - fall back to a static route below.
            _LOGGER.warning("Navimow: could not copy %s into www", filename, exc_info=True)
            url = None

        # Fallback: register a static path straight from the integration folder.
        if url is None:
            try:
                from homeassistant.components.http import StaticPathConfig

                static_url = f"/{DOMAIN}/{filename}"
                await hass.http.async_register_static_paths(
                    [StaticPathConfig(static_url, src, False)]
                )
                url = f"{static_url}?v={_CARD_VER}"
                _LOGGER.info("Navimow: card served at %s", url)
            except Exception:  # noqa: BLE001 - never fail setup over a UI extra.
                _LOGGER.warning(
                    "Navimow: could not register static path for %s", filename, exc_info=True
                )
                continue

        try:
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, url)
            ok_any = True
            _LOGGER.info("Navimow: card JS module registered (%s)", url)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Navimow: could not add JS url for %s", filename, exc_info=True)

    if ok_any:
        hass.data[_FRONTEND_KEY] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Navimow (Private) from a config entry."""
    # Register the frontend card FIRST, before the auth-dependent first refresh,
    # so the card is served even if the mower cloud auth is temporarily failing
    # (the card is generic and does not depend on the mower).
    await _async_register_frontend(hass)

    coordinator = NavimowCoordinator(hass, entry)
    # Restore the persisted mowed trail so the map shows the last pass
    # immediately after a restart/update (before the first cloud refresh).
    await coordinator.async_load_trail()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    async_setup_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. zone mapping)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the persisted mowed-trail store when the integration is removed.

    HA does not auto-remove ``.storage`` files created by an integration, so
    clean up the per-entry trail store here (absent-file-safe).
    """
    from .coordinator import trail_store

    try:
        await trail_store(hass, entry.entry_id).async_remove()
    except Exception:  # noqa: BLE001 - removal is best-effort cleanup
        _LOGGER.debug("Navimow: trail store cleanup failed", exc_info=True)
