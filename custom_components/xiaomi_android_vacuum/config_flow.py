"""Config flow for the Xiaomi Android Vacuum bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GatewayAuthError, GatewayClient, GatewayConnectionError, GatewayError, normalize_base_url
from .const import CONF_BASE_URL, CONF_TOKEN, DEFAULT_BASE_URL, DOMAIN, VACUUM_ID


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up one trusted Android vacuum gateway."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle a user-created configuration entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                user_input = self._normalize_input(user_input)
                client = GatewayClient(
                    async_get_clientsession(self.hass),
                    user_input[CONF_BASE_URL],
                    user_input[CONF_TOKEN],
                )
                await client.async_get_state()
            except GatewayAuthError:
                errors["base"] = "invalid_auth"
            except (GatewayConnectionError, GatewayError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(self._unique_id(user_input[CONF_BASE_URL]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Xiaomi Robot Vacuum X20+ (Android)", data=user_input
                )

        defaults = user_input or {CONF_BASE_URL: DEFAULT_BASE_URL}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)): str,
                    vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(
        self, import_config: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Create an entry from a `xiaomi_android_vacuum:` YAML block.

        YAML import deliberately does not contact the phone during startup.  A
        temporarily busy or unplugged Android device must not block HA startup;
        the coordinator will expose it as unavailable until passive polling
        works again.
        """
        try:
            data = self._normalize_input(import_config)
        except ValueError:
            return self.async_abort(reason="invalid_import")
        await self.async_set_unique_id(self._unique_id(data[CONF_BASE_URL]))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Xiaomi Robot Vacuum X20+ (Android)", data=data
        )

    @staticmethod
    def _normalize_input(raw: dict[str, Any]) -> dict[str, str]:
        """Strip presentation whitespace and reject empty secrets early."""
        base_url = normalize_base_url(str(raw[CONF_BASE_URL]))
        token = str(raw[CONF_TOKEN]).strip()
        if not token:
            raise ValueError("Gateway token cannot be empty")
        return {CONF_BASE_URL: base_url, CONF_TOKEN: token}

    @staticmethod
    def _unique_id(base_url: str) -> str:
        """Stable per-gateway unique id; the token is never part of it."""
        return f"{VACUUM_ID}:{base_url}"
