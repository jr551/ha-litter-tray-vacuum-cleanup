"""Sui's human-readable schedule/status entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, FIXED_ZONE_NAME
from .entity import SuiEntity
from .runtime import SuiRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: SuiRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SuiStatusSensor(runtime)])


class SuiStatusSensor(SuiEntity, SensorEntity):
    """Expose durable countdown/skip/start state without bridge identifiers."""

    _attr_name = "Status"
    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, runtime: SuiRuntime) -> None:
        super().__init__(runtime, "status")

    @property
    def native_value(self) -> str:
        return str(self.schedule_data.get("state") or "idle")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.schedule_data
        attrs: dict[str, Any] = {
            "zone_name": FIXED_ZONE_NAME,
            "cat_litter_count": data.get("counter"),
            "pending_jobs": data.get("pending_jobs", 0),
            "control_backend": data.get("control_backend"),
            "map_camera_entity_id": data.get("map_camera_entity_id"),
            "litter_zone_approved": data.get("litter_zone_approved", False),
        }
        if data.get("next_job") is not None:
            attrs["next_job"] = data["next_job"]
        if data.get("last_job") is not None:
            attrs["last_job"] = data["last_job"]
        return attrs
