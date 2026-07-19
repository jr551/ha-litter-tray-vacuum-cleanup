"""On-demand map image entity for the custom zone plotter card."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
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
    """Add a map image that only changes after explicit refresh_map."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([XiaomiAndroidVacuumMapImage(runtime)])


class XiaomiAndroidVacuumMapImage(XiaomiAndroidVacuumEntity, ImageEntity):
    """Serve the most recently requested Xiaomi Home map from memory."""

    _attr_name = "Map"

    def __init__(self, runtime: RuntimeData) -> None:
        super().__init__(runtime, "map")
        # CoordinatorEntity's base class intentionally ends the cooperative
        # init chain, so ImageEntity's token setup must be explicit.
        ImageEntity.__init__(self, runtime.coordinator.hass)

    @property
    def content_type(self) -> str:
        """Return the gateway-provided image MIME type."""
        return self.runtime.map_content_type

    @property
    def available(self) -> bool:
        """A successfully fetched map remains viewable during a later busy poll."""
        return self.runtime.map_image is not None

    @property
    def image_last_updated(self) -> datetime | None:
        """Signal an image refresh only when an explicit map was fetched."""
        return self.runtime.map_updated_at

    async def async_image(self) -> bytes | None:
        """Return the in-memory map, never triggering Android UI activity."""
        return self.runtime.map_image
