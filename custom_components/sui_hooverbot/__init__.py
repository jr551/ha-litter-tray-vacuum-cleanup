"""Native Home Assistant integration for Sui the Hooverbot."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bridge import FamilyBridgeClient
from .const import CONF_BRIDGE_TOKEN, CONF_BRIDGE_URL, DOMAIN, PLATFORMS
from .coordinator import SuiCoordinator
from .runtime import SuiRuntime
from .services import async_register_services, async_unregister_services
from .store import SuiScheduleStore


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sui without touching the robot during Home Assistant startup."""
    coordinator = SuiCoordinator(hass, entry)
    bridge = FamilyBridgeClient(
        async_get_clientsession(hass),
        str(entry.data[CONF_BRIDGE_URL]),
        str(entry.data[CONF_BRIDGE_TOKEN]),
    )
    runtime = SuiRuntime(
        hass,
        entry,
        coordinator,
        bridge,
        SuiScheduleStore(hass, entry.entry_id),
    )
    coordinator.runtime = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await runtime.async_setup()
    await coordinator.async_config_entry_first_refresh()
    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop timers/webhooks before releasing the configured bridge token."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    runtime: SuiRuntime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime.async_unload()
    if not hass.data[DOMAIN]:
        await async_unregister_services(hass)
    return True
