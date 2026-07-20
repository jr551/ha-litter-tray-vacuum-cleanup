"""Narrow trusted-HA skip service for Sui callbacks and manual controls."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, HomeAssistantError, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_LITTER_ZONE,
    CONF_LITTER_ZONE_APPROVED,
    DOMAIN,
    SERVICE_CONFIGURE_LITTER_ZONE,
    SERVICE_SKIP,
)
from .runtime import SuiRuntime
from .validation import normalise_litter_zone


SKIP_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("job_id"): cv.string,
        vol.Required("reaction_event_id"): cv.string,
        vol.Required("reaction"): cv.string,
    },
    extra=vol.PREVENT_EXTRA,
)

CONFIGURE_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required(CONF_LITTER_ZONE): list,
        vol.Required(CONF_LITTER_ZONE_APPROVED): cv.boolean,
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

    async def handle_configure_litter_zone(call: ServiceCall) -> None:
        runtime: SuiRuntime | None = hass.data.get(DOMAIN, {}).get(call.data["entry_id"])
        if runtime is None:
            raise HomeAssistantError("Sui the Hooverbot entry is not loaded")
        approved = bool(call.data[CONF_LITTER_ZONE_APPROVED])
        try:
            zone = normalise_litter_zone(
                call.data[CONF_LITTER_ZONE], allow_empty=not approved
            )
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc
        hass.config_entries.async_update_entry(
            runtime.entry,
            data={
                **runtime.entry.data,
                CONF_LITTER_ZONE: zone,
                CONF_LITTER_ZONE_APPROVED: approved,
            },
        )

    hass.services.async_register(DOMAIN, SERVICE_SKIP, handle_skip, schema=SKIP_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIGURE_LITTER_ZONE,
        handle_configure_litter_zone,
        schema=CONFIGURE_ZONE_SCHEMA,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE_SKIP)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIGURE_LITTER_ZONE)
