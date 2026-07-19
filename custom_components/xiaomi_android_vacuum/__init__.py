"""Home Assistant integration for deterministic Xiaomi Home Android workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GatewayClient, GatewayError, normalize_base_url
from .const import (
    CONF_BASE_URL,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
    NOTIFICATION_POLL_INTERVAL,
    PLATFORMS,
)
from .coordinator import XiaomiAndroidVacuumCoordinator
from .runtime import RuntimeData
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)


def _validate_yaml_instance(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Require exactly one location form for a YAML-defined bridge."""
    locations = [key for key in (CONF_BASE_URL, CONF_URL, CONF_HOST) if value.get(key)]
    if len(locations) != 1:
        raise vol.Invalid("Specify exactly one of base_url, url, or host")
    return value


YAML_INSTANCE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(CONF_BASE_URL): cv.string,
            vol.Optional(CONF_URL): cv.string,
            vol.Optional(CONF_HOST): cv.string,
            vol.Optional(CONF_PORT): cv.port,
            vol.Required(CONF_TOKEN): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    ),
    _validate_yaml_instance,
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [YAML_INSTANCE_SCHEMA])}, extra=vol.ALLOW_EXTRA
)


async def _async_notification_loop(runtime: RuntimeData) -> None:
    """Turn filtered Android notifications into near-real-time HA updates."""
    initialized = False
    last_sequence = 0
    last_available: bool | None = None
    while True:
        try:
            payload = await runtime.client.async_get_notifications()
            available = payload.get("available") is True
            sequence = int(payload.get("sequence") or 0)
            event = payload.get("latest_event")
            if not isinstance(event, dict):
                event = None

            if not initialized:
                initialized = True
                last_sequence = sequence
                runtime.set_notification(event, sequence)
                runtime.publish_notification_update()
            elif sequence > last_sequence and event:
                last_sequence = sequence
                runtime.set_notification(event, sequence)
                runtime.publish_notification_update()
                runtime.coordinator.async_emit_android_notification(event)
                await runtime.coordinator.async_request_refresh()
                runtime.publish_notification_update()

            if available != last_available:
                last_available = available
                runtime.notification_monitor_available = available
                data = dict(runtime.coordinator.data or {})
                data["notification_monitor_available"] = available
                runtime.coordinator.async_set_updated_data(data)
        except asyncio.CancelledError:
            raise
        except GatewayError as err:
            if last_available is not False:
                last_available = False
                runtime.notification_monitor_available = False
                data = dict(runtime.coordinator.data or {})
                data["notification_monitor_available"] = False
                data["notification_monitor_error"] = str(err)[:256]
                runtime.coordinator.async_set_updated_data(data)
        await asyncio.sleep(NOTIFICATION_POLL_INTERVAL)


def _yaml_base_url(item: Mapping[str, Any]) -> str:
    """Convert a friendly YAML URL/host declaration into the config-flow form."""
    raw_url = item.get(CONF_BASE_URL) or item.get(CONF_URL)
    if raw_url:
        return normalize_base_url(str(raw_url))

    host = str(item[CONF_HOST]).strip()
    if not host:
        raise ValueError("Gateway host cannot be empty")
    # A scheme is accepted on host for a friendlier migration path, though URL
    # is clearer in new YAML.  Preserve an explicitly supplied port.
    if "://" in host:
        base_url = host
    else:
        base_url = f"http://{host}"
    parsed = urlparse(base_url)
    if item.get(CONF_PORT) is not None and parsed.port is None:
        base_url = f"{base_url}:{item[CONF_PORT]}"
    return normalize_base_url(base_url)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Import optional YAML bridge definitions into normal config entries."""
    for item in config.get(DOMAIN, []):
        try:
            source_data = {
                CONF_BASE_URL: _yaml_base_url(item),
                CONF_TOKEN: str(item[CONF_TOKEN]),
            }
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.error("Ignoring invalid %s YAML configuration: %s", DOMAIN, err)
            continue
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=source_data,
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an imported or UI-created gateway entry."""
    client = GatewayClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_TOKEN],
    )
    coordinator = XiaomiAndroidVacuumCoordinator(hass, client, entry.entry_id)
    runtime = RuntimeData(client=client, coordinator=coordinator)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # The coordinator turns a temporary phone/gateway failure into an
    # unavailable entity instead of rejecting the whole config entry.
    await coordinator.async_config_entry_first_refresh()
    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.notification_task = hass.async_create_task(
        _async_notification_loop(runtime),
        f"{DOMAIN} Android notification listener",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and remove services when the final bridge is gone."""
    runtime: RuntimeData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime and runtime.notification_task:
        runtime.notification_task.cancel()
        try:
            await runtime.notification_task
        except asyncio.CancelledError:
            pass
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            await async_unregister_services(hass)
    return unload_ok
