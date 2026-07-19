"""Explicit Home Assistant services for safe rectangular vacuum jobs."""

from __future__ import annotations

from uuid import uuid4

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, HomeAssistantError, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .api import (
    GatewayAuthError,
    GatewayBusyError,
    GatewayConnectionError,
    GatewayError,
    GatewayWorkflowError,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRY_RUN,
    ATTR_IDEMPOTENCY_KEY,
    ATTR_MAP_GENERATION,
    ATTR_RECTANGLES,
    ATTR_ZONE_NAME,
    DOMAIN,
    EVENT_JOB_FAILED,
    SERVICE_REFRESH_MAP,
    SERVICE_START_ZONE,
)
from .runtime import RuntimeData


RECTANGLE_SCHEMA = vol.Schema(
    {
        vol.Required("x1"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
        vol.Required("y1"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
        vol.Required("x2"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
        vol.Required("y2"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
    },
    extra=vol.PREVENT_EXTRA,
)

TARGET_SCHEMA = {
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
}

START_ZONE_SCHEMA = vol.Schema(
    {
        **TARGET_SCHEMA,
        vol.Optional(ATTR_ZONE_NAME): cv.string,
        vol.Optional(ATTR_RECTANGLES): vol.All(cv.ensure_list, [RECTANGLE_SCHEMA]),
        vol.Optional(ATTR_MAP_GENERATION): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_IDEMPOTENCY_KEY): cv.string,
    },
    extra=vol.PREVENT_EXTRA,
)

REFRESH_MAP_SCHEMA = vol.Schema(TARGET_SCHEMA, extra=vol.PREVENT_EXTRA)


def _runtime_for_call(hass: HomeAssistant, call: ServiceCall) -> RuntimeData:
    """Resolve a target without ever guessing between multiple gateways."""
    runtimes: dict[str, RuntimeData] = hass.data.get(DOMAIN, {})
    if not runtimes:
        raise HomeAssistantError("No Xiaomi Android Vacuum bridge is configured")

    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        runtime = runtimes.get(entry_id)
        if runtime is None:
            raise HomeAssistantError("The selected Xiaomi Android Vacuum bridge is not loaded")
        return runtime

    raw_entity_ids = call.data.get(ATTR_ENTITY_ID)
    if raw_entity_ids:
        entity_ids = (
            [raw_entity_ids] if isinstance(raw_entity_ids, str) else list(raw_entity_ids)
        )
        registry = er.async_get(hass)
        matching_entry_ids: set[str] = set()
        for entity_id in entity_ids:
            registry_entry = registry.async_get(entity_id)
            if registry_entry is None or registry_entry.config_entry_id not in runtimes:
                raise HomeAssistantError(
                    f"{entity_id} does not belong to a loaded Xiaomi Android Vacuum bridge"
                )
            matching_entry_ids.add(registry_entry.config_entry_id)
        if len(matching_entry_ids) != 1:
            raise HomeAssistantError("Target one Xiaomi Android Vacuum bridge at a time")
        return runtimes[matching_entry_ids.pop()]

    if len(runtimes) != 1:
        raise HomeAssistantError(
            "Specify entity_id or config_entry_id when more than one bridge is configured"
        )
    return next(iter(runtimes.values()))


def _safe_error_code(error: GatewayError) -> str:
    """Do not put raw Android accessibility content into HA's recorder."""
    if isinstance(error, GatewayBusyError):
        return "phone_busy"
    if isinstance(error, GatewayConnectionError):
        return "bridge_unavailable"
    if isinstance(error, GatewayAuthError):
        return "gateway_auth_failed"
    if isinstance(error, GatewayWorkflowError):
        return "workflow_refused"
    return "gateway_error"


def _fire_failed_job_event(
    hass: HomeAssistant, runtime: RuntimeData, action: str, error: GatewayError
) -> None:
    """Leave a compact, recorder-friendly audit signal for automations."""
    hass.bus.async_fire(
        EVENT_JOB_FAILED,
        {
            "config_entry_id": runtime.coordinator.config_entry_id,
            "action": action,
            "error": _safe_error_code(error),
        },
    )


async def _async_handle_start_zone(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for a named routine or map-previewed rectangle."""
    runtime = _runtime_for_call(hass, call)
    zone_name = call.data.get(ATTR_ZONE_NAME)
    rectangles = call.data.get(ATTR_RECTANGLES)
    if bool(zone_name) == bool(rectangles):
        raise HomeAssistantError(
            "Specify exactly one of zone_name or rectangles for start_zone"
        )

    map_generation = call.data.get(ATTR_MAP_GENERATION) or runtime.map_generation
    if not map_generation:
        raise HomeAssistantError("Refresh the map before validating or starting any zone")
    try:
        job = await runtime.client.async_start_zone(
            zone_name=zone_name,
            rectangles=rectangles,
            map_generation=map_generation,
            dry_run=bool(call.data[ATTR_DRY_RUN]),
            idempotency_key=call.data.get(ATTR_IDEMPOTENCY_KEY) or str(uuid4()),
        )
    except GatewayError as err:
        _fire_failed_job_event(hass, runtime, "zone_clean", err)
        # The gateway may have recorded outcome_unknown after a late transport
        # failure; immediately re-read safe observed state rather than waiting
        # for the idle poll interval.
        await runtime.coordinator.async_request_refresh()
        raise HomeAssistantError(
            f"Android vacuum zone job was refused ({_safe_error_code(err)})"
        ) from err
    runtime.set_job(job)
    runtime.publish_job_update()
    await runtime.coordinator.async_request_refresh()


async def _async_handle_refresh_map(hass: HomeAssistant, call: ServiceCall) -> None:
    """Fetch a fresh map only after an explicit HA service request."""
    runtime = _runtime_for_call(hass, call)
    try:
        payload = await runtime.client.async_get_map()
        runtime.set_map(payload)
    except GatewayError as err:
        raise HomeAssistantError(f"Android vacuum map refresh was refused: {err}") from err
    runtime.publish_map_update()
    await runtime.coordinator.async_request_refresh()


async def async_register_services(hass: HomeAssistant) -> None:
    """Install global services once, resolving each call to one config entry."""
    if not hass.services.has_service(DOMAIN, SERVICE_START_ZONE):
        async def handle_start_zone(call: ServiceCall) -> None:
            await _async_handle_start_zone(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_START_ZONE,
            handle_start_zone,
            schema=START_ZONE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_MAP):
        async def handle_refresh_map(call: ServiceCall) -> None:
            await _async_handle_refresh_map(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_MAP,
            handle_refresh_map,
            schema=REFRESH_MAP_SCHEMA,
        )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove global services after the final entry unloads."""
    hass.services.async_remove(DOMAIN, SERVICE_START_ZONE)
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH_MAP)
