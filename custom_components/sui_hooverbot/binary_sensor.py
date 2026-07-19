"""Sui safety/attention entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import SuiEntity
from .runtime import SuiRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: SuiRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SuiNeedsAttentionSensor(runtime)])


class SuiNeedsAttentionSensor(SuiEntity, BinarySensorEntity):
    """Report Sui failures and the current Xiaomi vacuum safety signal."""

    _attr_name = "Needs attention"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, runtime: SuiRuntime) -> None:
        super().__init__(runtime, "needs_attention")

    @property
    def is_on(self) -> bool:
        return bool(self.schedule_data.get("needs_attention"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_job = self.schedule_data.get("last_job")
        return {"last_job": last_job} if isinstance(last_job, dict) else {}
