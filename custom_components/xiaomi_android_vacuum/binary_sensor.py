"""Bridge health and stuck/error binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import XiaomiAndroidVacuumEntity
from .runtime import RuntimeData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add connection and attention state sensors."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            XiaomiAndroidVacuumBridgeConnected(runtime),
            XiaomiAndroidVacuumNeedsAttention(runtime),
        ]
    )


class XiaomiAndroidVacuumBridgeConnected(XiaomiAndroidVacuumEntity, BinarySensorEntity):
    """Whether HA can passively read the dedicated Android bridge."""

    _attr_name = "Android bridge connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "bridge_connected")

    @property
    def is_on(self) -> bool:
        """Return whether the latest passive state request reached the bridge."""
        return bool(self.bridge_data.get("bridge_available"))

    @property
    def available(self) -> bool:
        """Always render the connection sensor, even while it is off."""
        return True


class XiaomiAndroidVacuumNeedsAttention(XiaomiAndroidVacuumEntity, BinarySensorEntity):
    """Whether Xiaomi Home reported an error/stuck condition on the robot."""

    _attr_name = "Needs attention"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "needs_attention")

    @property
    def is_on(self) -> bool | None:
        """Surface an observed Xiaomi fault, never an inferred all-clear."""
        if self.runtime.notification_attention:
            return True
        if not self.bridge_data.get("bridge_available") or self.bridge_data.get("activity") == "unknown":
            return None
        last_job = self.runtime.last_job or {}
        return bool(
            self.bridge_data.get("needs_attention")
            or last_job.get("status") == "outcome_unknown"
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Keep the safe, stable reason and transition time in HA history."""
        attrs = super().extra_state_attributes
        for key in ("attention_reason", "attention_since"):
            if self.bridge_data.get(key) is not None:
                attrs[key] = self.bridge_data[key]
        return attrs
