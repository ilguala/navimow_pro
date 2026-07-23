"""Config flow for Navimow (Private).

User enters the EMAIL + PASSWORD of the account that owns (or was shared) the
mower. We recommend a dedicated shared account (see README) so the primary
phone session is never disturbed. The flow:

    passport login -> user/user/login (fresh, persisted device_id)
                    -> auth-list (discover vehicle[s]) -> create entry

Tokens and passwords are never logged. The password is NOT stored in the entry;
only the refresh/access tokens (used for perpetual refresh) are.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import NavimowCloudClient, NavimowError, PassportAuthError, PassportError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_LANGUAGE,
    CONF_MODEL,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_UID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLE_SN,
    CONF_VEHICLE_TYPE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    OPT_ZONES,
)

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


class NavimowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Navimow (Private) config + reauth flows."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._device_id: str | None = None
        self._client: NavimowCloudClient | None = None
        self._vehicles: list[dict] = []
        self._reauth_entry: ConfigEntry | None = None

    # ---------------------------------------------------------------- helpers
    async def _authenticate(self, email: str, password: str) -> list[dict]:
        """Log in and discover vehicles. Raises for the caller to map to errors."""
        device_id = self._device_id or uuid.uuid4().hex
        self._device_id = device_id
        client = NavimowCloudClient(device_id=device_id, language=DEFAULT_LANGUAGE)

        def _do() -> list[dict]:
            client.authenticate(email, password)
            client.mower_login()
            return client.auth_list()

        vehicles = await self.hass.async_add_executor_job(_do)
        self._client = client
        return vehicles

    def _entry_data(self, vehicle: dict) -> dict[str, Any]:
        assert self._client is not None
        state = self._client.session_state()
        return {
            CONF_EMAIL: self._email,
            CONF_ACCESS_TOKEN: state["access_token"],
            CONF_REFRESH_TOKEN: state["refresh_token"],
            CONF_UID: state["uid"],
            CONF_DEVICE_ID: self._client.device_id,
            CONF_REGION: state["region"],
            CONF_LANGUAGE: DEFAULT_LANGUAGE,
            CONF_VEHICLE_SN: str(vehicle.get("vehicle_sn", "")),
            CONF_VEHICLE_TYPE: int(vehicle.get("vehicle_type", 0) or 0),
            CONF_VEHICLE_NAME: str(
                vehicle.get("selfDefinedName") or vehicle.get("vehicle_name") or "Navimow"
            ),
            CONF_MODEL: str(vehicle.get("subType", "")),
        }

    # ------------------------------------------------------------------- user
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            try:
                self._vehicles = await self._authenticate(
                    self._email, user_input[CONF_PASSWORD]
                )
            except PassportAuthError:
                errors["base"] = "invalid_auth"
            except PassportError:
                errors["base"] = "cannot_connect"
            except NavimowError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface anything unexpected safely
                _LOGGER.exception("Unexpected error during Navimow login")
                errors["base"] = "unknown"
            else:
                if not self._vehicles:
                    # Shared/guest accounts: the mower is reachable by serial but is
                    # NOT listed in auth-list (which only returns owned vehicles).
                    # Fall back to letting the user enter the serial manually.
                    return await self.async_step_manual()
                elif len(self._vehicles) == 1:
                    return await self._create(self._vehicles[0])
                else:
                    return await self.async_step_select_vehicle()

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_select_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            sn = user_input[CONF_VEHICLE_SN]
            vehicle = next(
                (v for v in self._vehicles if str(v.get("vehicle_sn")) == sn),
                self._vehicles[0],
            )
            return await self._create(vehicle)

        options = {
            str(v.get("vehicle_sn")): (
                f"{v.get('selfDefinedName') or v.get('vehicle_name') or 'Navimow'} "
                f"({v.get('vehicle_sn')})"
            )
            for v in self._vehicles
        }
        return self.async_show_form(
            step_id="select_vehicle",
            data_schema=vol.Schema({vol.Required(CONF_VEHICLE_SN): vol.In(options)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fallback when auth-list is empty (typically a shared/guest account).

        The mower is reachable by serial even though it is not "owned" by this
        account, so we let the user type the serial and validate it with a live
        read (index2). Find the serial in the Navimow app under device info.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            sn = str(user_input[CONF_VEHICLE_SN]).strip().upper()

            def _probe() -> dict:
                assert self._client is not None
                info = self._client.index2(sn) or {}
                try:
                    dev = self._client.device_info(sn) or {}
                except NavimowError:
                    dev = {}
                return {"index2": info, "device_info": dev}

            try:
                probe = await self.hass.async_add_executor_job(_probe)
            except NavimowError:
                errors["base"] = "vehicle_not_found"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating manual serial")
                errors["base"] = "unknown"
            else:
                idx = probe["index2"]
                if not idx:
                    errors["base"] = "vehicle_not_found"
                else:
                    dev = probe["device_info"]
                    vtype = idx.get("vehicle_type") or idx.get("vehicleType") or 160000001
                    vehicle = {
                        "vehicle_sn": sn,
                        "vehicle_type": int(vtype or 160000001),
                        "vehicle_name": (
                            user_input.get(CONF_VEHICLE_NAME) or dev.get("model") or "Navimow"
                        ),
                        "subType": dev.get("model", ""),
                    }
                    return await self._create(vehicle)

        schema = vol.Schema(
            {
                vol.Required(CONF_VEHICLE_SN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_VEHICLE_NAME, default="Navimow"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def _create(self, vehicle: dict) -> ConfigFlowResult:
        sn = str(vehicle.get("vehicle_sn", ""))
        data = self._entry_data(vehicle)

        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={**self._reauth_entry.data, **data},
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(sn)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_VEHICLE_NAME], data=data)

    # ----------------------------------------------------------------- reauth
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is not None:
            self._email = self._reauth_entry.data.get(CONF_EMAIL)
            self._device_id = self._reauth_entry.data.get(CONF_DEVICE_ID)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = (user_input.get(CONF_EMAIL) or self._email or "").strip()
            self._email = email
            try:
                self._vehicles = await self._authenticate(email, user_input[CONF_PASSWORD])
            except PassportAuthError:
                errors["base"] = "invalid_auth"
            except (PassportError, NavimowError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Navimow reauth")
                errors["base"] = "unknown"
            else:
                sn = self._reauth_entry.data.get(CONF_VEHICLE_SN) if self._reauth_entry else None
                vehicle = next(
                    (v for v in self._vehicles if str(v.get("vehicle_sn")) == sn), None
                )
                if vehicle is None:
                    # Shared account (empty auth-list) or SN not re-listed: reuse the
                    # vehicle identity already stored in the entry.
                    ed = self._reauth_entry.data if self._reauth_entry else {}
                    vehicle = {
                        "vehicle_sn": ed.get(CONF_VEHICLE_SN, ""),
                        "vehicle_type": ed.get(CONF_VEHICLE_TYPE, 160000001),
                        "vehicle_name": ed.get(CONF_VEHICLE_NAME, "Navimow"),
                        "subType": ed.get(CONF_MODEL, ""),
                    }
                return await self._create(vehicle)

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=self._email or ""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD, autocomplete="current-password"
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    # ---------------------------------------------------------------- options
    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return NavimowOptionsFlow(entry)


class NavimowOptionsFlow(OptionsFlow):
    """Let the user configure the zone list used by the zone select entity."""

    def __init__(self, entry: ConfigEntry) -> None:
        # Store on a private attr to stay compatible across HA versions
        # (assigning to self.config_entry is deprecated in newer cores).
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={OPT_ZONES: user_input.get(OPT_ZONES, "")})

        current = self._entry.options.get(OPT_ZONES, "")
        schema = vol.Schema(
            {
                vol.Optional(OPT_ZONES, default=current): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
