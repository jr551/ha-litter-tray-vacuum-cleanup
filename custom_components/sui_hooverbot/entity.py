"""Shared entity helpers for Sui the Hooverbot."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .runtime import SuiRuntime


class SuiEntity(CoordinatorEntity):
    """A recorder-safe entity backed by Sui's push coordinator."""

    _attr_has_entity_name = True

    def __init__(self, runtime: SuiRuntime, suffix: str) -> None:
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry.entry_id)},
            name="Sui the Hooverbot",
            manufacturer="Home Assistant",
            model="Litter-tray cleanup scheduler",
        )

    @property
    def schedule_data(self) -> dict[str, object]:
        return self.coordinator.data or self.runtime.snapshot()
