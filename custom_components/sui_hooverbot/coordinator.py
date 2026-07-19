"""Push-style coordinator for Sui's durable schedule snapshot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import SuiRuntime


_LOGGER = logging.getLogger(__name__)


class SuiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fan out runtime state to entities; it never polls or triggers cleaning."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}.{entry.entry_id}",
            update_interval=None,
            config_entry=entry,
        )
        self.runtime: SuiRuntime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        if self.runtime is None:
            return {"state": "idle", "jobs": []}
        return self.runtime.snapshot()

    def publish(self) -> None:
        if self.runtime is not None:
            self.async_set_updated_data(self.runtime.snapshot())
