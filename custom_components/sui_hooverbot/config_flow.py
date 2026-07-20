"""UI config flow for the native Sui the Hooverbot integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_CLEANUP_DELAY_SECONDS,
    CONF_COUNTER_ENTITY_ID,
    CONF_LITTER_ZONE,
    CONF_LITTER_ZONE_APPROVED,
    CONF_MAP_CAMERA_ENTITY_ID,
    CONF_MAX_LATENESS_SECONDS,
    CONF_REACTION_GRACE_SECONDS,
    CONF_VACUUM_ENTITY_ID,
    CONF_WEBHOOK_ID,
    DEFAULT_CLEANUP_DELAY_SECONDS,
    DEFAULT_COUNTER_ENTITY_ID,
    DEFAULT_LITTER_ZONE,
    DEFAULT_LITTER_ZONE_APPROVED,
    DEFAULT_MAP_CAMERA_ENTITY_ID,
    DEFAULT_MAX_LATENESS_SECONDS,
    DEFAULT_REACTION_GRACE_SECONDS,
    DEFAULT_VACUUM_ENTITY_ID,
    DOMAIN,
)
from .validation import litter_zone_text, normalise_config_input, schedule_identity


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create one bridge-backed, fixed-zone Sui schedule."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = self._normalise_input(user_input)
            except ValueError:
                errors["base"] = "invalid_input"
            else:
                await self.async_set_unique_id(schedule_identity(data))
                self._abort_if_unique_id_configured()
                data[CONF_WEBHOOK_ID] = webhook.async_generate_id()
                return self.async_create_entry(title="Sui the Hooverbot", data=data)

        defaults = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTER_ENTITY_ID,
                        default=defaults.get(CONF_COUNTER_ENTITY_ID, DEFAULT_COUNTER_ENTITY_ID),
                    ): str,
                    vol.Required(
                        CONF_VACUUM_ENTITY_ID,
                        default=defaults.get(CONF_VACUUM_ENTITY_ID, DEFAULT_VACUUM_ENTITY_ID),
                    ): str,
                    vol.Required(
                        CONF_MAP_CAMERA_ENTITY_ID,
                        default=defaults.get(
                            CONF_MAP_CAMERA_ENTITY_ID, DEFAULT_MAP_CAMERA_ENTITY_ID
                        ),
                    ): str,
                    vol.Required(
                        CONF_LITTER_ZONE,
                        default=litter_zone_text(
                            defaults.get(CONF_LITTER_ZONE, DEFAULT_LITTER_ZONE)
                        ),
                    ): str,
                    vol.Required(
                        CONF_LITTER_ZONE_APPROVED,
                        default=defaults.get(
                            CONF_LITTER_ZONE_APPROVED, DEFAULT_LITTER_ZONE_APPROVED
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BRIDGE_URL, default=defaults.get(CONF_BRIDGE_URL, "")
                    ): str,
                    vol.Required(CONF_BRIDGE_TOKEN, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        CONF_CLEANUP_DELAY_SECONDS,
                        default=defaults.get(
                            CONF_CLEANUP_DELAY_SECONDS, DEFAULT_CLEANUP_DELAY_SECONDS
                        ),
                    ): int,
                    vol.Required(
                        CONF_REACTION_GRACE_SECONDS,
                        default=defaults.get(
                            CONF_REACTION_GRACE_SECONDS, DEFAULT_REACTION_GRACE_SECONDS
                        ),
                    ): int,
                    vol.Required(
                        CONF_MAX_LATENESS_SECONDS,
                        default=defaults.get(CONF_MAX_LATENESS_SECONDS, DEFAULT_MAX_LATENESS_SECONDS),
                    ): int,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def _normalise_input(raw: dict[str, Any]) -> dict[str, Any]:
        return normalise_config_input(raw)

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SuiOptionsFlow()


class SuiOptionsFlow(config_entries.OptionsFlow):
    """Edit the direct vacuum target and the explicitly approved fixed zone."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = normalise_config_input({**self.config_entry.data, **user_input})
            except ValueError:
                errors["base"] = "invalid_input"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, **data},
                )
                return self.async_create_entry(title="", data={})

        defaults = {**self.config_entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTER_ENTITY_ID,
                        default=defaults.get(
                            CONF_COUNTER_ENTITY_ID, DEFAULT_COUNTER_ENTITY_ID
                        ),
                    ): str,
                    vol.Required(
                        CONF_VACUUM_ENTITY_ID,
                        default=defaults.get(
                            CONF_VACUUM_ENTITY_ID, DEFAULT_VACUUM_ENTITY_ID
                        ),
                    ): str,
                    vol.Required(
                        CONF_MAP_CAMERA_ENTITY_ID,
                        default=defaults.get(
                            CONF_MAP_CAMERA_ENTITY_ID, DEFAULT_MAP_CAMERA_ENTITY_ID
                        ),
                    ): str,
                    vol.Required(
                        CONF_LITTER_ZONE,
                        default=litter_zone_text(defaults.get(CONF_LITTER_ZONE)),
                    ): str,
                    vol.Required(
                        CONF_LITTER_ZONE_APPROVED,
                        default=defaults.get(
                            CONF_LITTER_ZONE_APPROVED, DEFAULT_LITTER_ZONE_APPROVED
                        ),
                    ): bool,
                    vol.Required(
                        CONF_CLEANUP_DELAY_SECONDS,
                        default=defaults.get(
                            CONF_CLEANUP_DELAY_SECONDS, DEFAULT_CLEANUP_DELAY_SECONDS
                        ),
                    ): int,
                    vol.Required(
                        CONF_REACTION_GRACE_SECONDS,
                        default=defaults.get(
                            CONF_REACTION_GRACE_SECONDS,
                            DEFAULT_REACTION_GRACE_SECONDS,
                        ),
                    ): int,
                    vol.Required(
                        CONF_MAX_LATENESS_SECONDS,
                        default=defaults.get(
                            CONF_MAX_LATENESS_SECONDS, DEFAULT_MAX_LATENESS_SECONDS
                        ),
                    ): int,
                }
            ),
            errors=errors,
        )
