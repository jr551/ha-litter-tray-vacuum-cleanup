"""Vacuum entity backed by the guarded Android UI workflow."""

from __future__ import annotations

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import XiaomiAndroidVacuumEntity
from .runtime import RuntimeData

_ACTIVITY_MAP = {
    "cleaning": VacuumActivity.CLEANING,
    "docked": VacuumActivity.DOCKED,
    "idle": VacuumActivity.IDLE,
    "paused": VacuumActivity.PAUSED,
    "returning": VacuumActivity.RETURNING,
    "error": VacuumActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the main native vacuum entity."""
    runtime: RuntimeData = hass.data["xiaomi_android_vacuum"][entry.entry_id]
    async_add_entities([XiaomiAndroidVacuum(runtime)])


class XiaomiAndroidVacuum(XiaomiAndroidVacuumEntity, StateVacuumEntity):
    """Read-only observed state; mutations are restricted to start_zone."""

    # STATE is required for StateVacuumEntity.  It is not a control feature;
    # specifically omit START, PAUSE, STOP, and RETURN_HOME in v1.
    _attr_supported_features = VacuumEntityFeature.STATE

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "vacuum")

    @property
    def activity(self) -> VacuumActivity:
        """Return the Home Assistant enum corresponding to observed UI state."""
        # `available` becomes false for unknown state, so the conservative
        # fallback can never misreport an unavailable phone as idle in HA.
        return _ACTIVITY_MAP.get(self.bridge_data.get("activity"), VacuumActivity.IDLE)
