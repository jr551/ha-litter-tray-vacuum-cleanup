"""Status and job-audit sensors for the Xiaomi Android Vacuum bridge."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
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
    """Add compact recorder-friendly status entities."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            XiaomiAndroidVacuumWorkflowSensor(runtime),
            XiaomiAndroidVacuumLastJobSensor(runtime),
            XiaomiAndroidVacuumNotificationSensor(runtime),
        ]
    )


class XiaomiAndroidVacuumWorkflowSensor(XiaomiAndroidVacuumEntity, SensorEntity):
    """Expose the observed Android workflow state without a screenshot blob."""

    _attr_name = "Workflow status"
    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "workflow_status")

    @property
    def native_value(self) -> str:
        """Return the raw workflow activity for automations and history."""
        return str(self.bridge_data.get("activity", "unknown"))

    @property
    def available(self) -> bool:
        """This sensor remains available to explain a busy/unreachable bridge."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["bridge_available"] = bool(self.bridge_data.get("bridge_available"))
        attrs["needs_attention"] = bool(self.bridge_data.get("needs_attention"))
        for key in ("attention_reason", "attention_since", "status_reason"):
            if self.bridge_data.get(key) is not None:
                attrs[key] = self.bridge_data[key]
        return attrs


class XiaomiAndroidVacuumLastJobSensor(XiaomiAndroidVacuumEntity, SensorEntity):
    """Expose the last requested job result for HA history and dashboards."""

    _attr_name = "Last Android job"
    _attr_icon = "mdi:clipboard-check-outline"

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "last_job")

    @property
    def native_value(self) -> str | None:
        job = self.runtime.last_job
        return str(job.get("status")) if job and job.get("status") is not None else None

    @property
    def available(self) -> bool:
        """Retain the compact audit result during a temporary phone outage."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Keep audit details small, serializable, and useful in recorder."""
        job = self.runtime.last_job or {}
        allowed = {
            "job_id",
            "action",
            "created_at",
            "completed_at",
            "zone_name",
            "rectangles",
            "workflow_version",
            "error",
        }
        return {key: value for key, value in job.items() if key in allowed}


class XiaomiAndroidVacuumNotificationSensor(XiaomiAndroidVacuumEntity, SensorEntity):
    """Record the latest privacy-filtered Xiaomi Home vacuum notification."""

    _attr_name = "Latest Android notification"
    _attr_icon = "mdi:bell-ring-outline"

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "latest_android_notification")

    @property
    def native_value(self) -> str | None:
        event = self.runtime.latest_notification or {}
        return str(event.get("category")) if event.get("category") else None

    @property
    def available(self) -> bool:
        return bool(self.bridge_data.get("notification_monitor_available", True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        event = self.runtime.latest_notification or {}
        allowed = {"source", "package", "vacuum_id", "title", "text", "event_at"}
        attrs = {key: value for key, value in event.items() if key in allowed}
        attrs["sequence"] = self.runtime.notification_sequence
        attrs["attention"] = self.runtime.notification_attention
        return attrs
