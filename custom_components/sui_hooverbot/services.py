"""Narrow trusted-HA skip service for Sui callbacks and manual controls."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, HomeAssistantError, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_SKIP
from .runtime import SuiRuntime


SKIP_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("job_id"): cv.string,
        vol.Required("reaction_event_id"): cv.string,
        vol.Required("reaction"): cv.string,
    },
    extra=vol.PREVENT_EXTRA,
)


async def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SKIP):
        return

    async def handle_skip(call: ServiceCall) -> None:
        runtime: SuiRuntime | None = hass.data.get(DOMAIN, {}).get(call.data["entry_id"])
        if runtime is None:
            raise HomeAssistantError("Sui the Hooverbot entry is not loaded")
        await runtime.async_mark_skipped_local(
            job_id=call.data["job_id"],
            reaction_event_id=call.data["reaction_event_id"],
            reaction=call.data["reaction"],
        )

    hass.services.async_register(DOMAIN, SERVICE_SKIP, handle_skip, schema=SKIP_SCHEMA)


async def async_unregister_services(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE_SKIP)
