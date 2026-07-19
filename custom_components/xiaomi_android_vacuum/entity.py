"""Shared entity helpers for the Xiaomi Android Vacuum bridge."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL, VACUUM_ID, WORKFLOW_NAME
from .runtime import RuntimeData


class XiaomiAndroidVacuumEntity(CoordinatorEntity):
    """Base class that presents each gateway as one stable HA device."""

    _attr_has_entity_name = True

    def __init__(self, runtime: RuntimeData, suffix: str) -> None:
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._gateway_hash = sha256(
            runtime.client.base_url.encode("utf-8")
        ).hexdigest()[:16]
        self._attr_unique_id = f"{VACUUM_ID}_{self._gateway_hash}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{VACUUM_ID}:{self._gateway_hash}")},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Sui the Hooverbot",
            configuration_url=runtime.client.base_url,
        )

    @property
    def bridge_data(self) -> dict[str, Any]:
        """Return only coordinator-cached data; properties must not perform I/O."""
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        """Keep the main device unavailable when state is not observed safely."""
        return bool(
            super().available
            and self.bridge_data.get("bridge_available")
            and self.bridge_data.get("activity") != "unknown"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose compact, useful workflow metadata and never an image/token."""
        state = self.bridge_data
        attrs: dict[str, Any] = {
            "workflow": WORKFLOW_NAME,
            "workflow_version": state.get("workflow_version"),
            "last_seen": state.get("last_seen"),
            "phone_busy": bool(state.get("phone_busy")),
            "workflow_busy": bool(state.get("workflow_busy")),
            "map_generation": self.runtime.map_generation,
            "zone_ready": self.runtime.map_zone_ready,
            "zone_status_reason": self.runtime.map_zone_status_reason,
            "known_zones": self.runtime.known_zones,
            "needs_attention": bool(
                state.get("needs_attention") or self.runtime.notification_attention
            ),
            "notification_sequence": self.runtime.notification_sequence,
            "notification_attention": self.runtime.notification_attention,
        }
        if state.get("cleaning_area_m2") is not None:
            attrs["cleaning_area_m2"] = state["cleaning_area_m2"]
        if state.get("last_error"):
            attrs["last_error"] = state["last_error"]
        if self.runtime.latest_notification:
            attrs["latest_android_notification"] = dict(
                self.runtime.latest_notification
            )
        for key in (
            "attention_reason",
            "attention_since",
            "status_reason",
            "foreground_package",
            "last_known_activity",
            "status_poll_reopened_xiaomi",
        ):
            if state.get(key) is not None:
                attrs[key] = state[key]
        return {key: value for key, value in attrs.items() if value is not None}
