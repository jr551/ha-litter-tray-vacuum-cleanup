"""In-memory runtime data shared by the integration platforms."""

from __future__ import annotations

import base64
import binascii
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .api import GatewayClient, GatewayError

if TYPE_CHECKING:
    from .coordinator import XiaomiAndroidVacuumCoordinator


@dataclass(slots=True)
class RuntimeData:
    """State which is intentionally never written to Home Assistant attributes."""

    client: GatewayClient
    coordinator: XiaomiAndroidVacuumCoordinator
    map_image: bytes | None = None
    map_content_type: str = "image/jpeg"
    map_generation: str | None = None
    map_updated_at: datetime | None = None
    map_zone_ready: bool = False
    map_zone_status_reason: str | None = None
    known_zones: dict[str, dict[str, int]] = field(default_factory=dict)
    last_job: dict[str, Any] | None = None
    latest_notification: dict[str, Any] | None = None
    notification_sequence: int = 0
    notification_attention: bool = False
    notification_monitor_available: bool = False
    notification_task: asyncio.Task[Any] | None = None

    def set_map(self, payload: dict[str, Any]) -> None:
        """Store a bounded, decoded image preview in memory only."""
        encoded = payload.get("image_base64")
        if not isinstance(encoded, str) or not encoded:
            raise GatewayError("Gateway map response did not contain an image")
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as err:
            raise GatewayError("Gateway map response contained invalid image data") from err
        if not image or len(image) > 20 * 1024 * 1024:
            raise GatewayError("Gateway map image is empty or too large")

        generation = payload.get("map_generation")
        if not isinstance(generation, str) or not generation:
            raise GatewayError("Gateway map response did not contain a generation token")
        zones = payload.get("known_zones", {})
        if not isinstance(zones, dict):
            zones = {}

        self.map_image = image
        self.map_content_type = str(payload.get("mime_type") or "image/jpeg")
        self.map_generation = generation
        self.map_updated_at = dt_util.utcnow()
        self.map_zone_ready = payload.get("zone_ready") is True
        reason = payload.get("zone_status_reason")
        self.map_zone_status_reason = str(reason)[:256] if reason else None
        self.known_zones = {
            str(name): dict(rect)
            for name, rect in zones.items()
            if isinstance(name, str) and isinstance(rect, dict)
        }

    def set_job(self, job: dict[str, Any]) -> None:
        """Keep the most recent command result available to status entities."""
        self.last_job = dict(job)

    def publish_map_update(self) -> None:
        """Notify coordinator entities that a new in-memory map is available."""
        data = dict(self.coordinator.data or {})
        data["map_generation"] = self.map_generation
        data["known_zones"] = self.known_zones
        data["zone_ready"] = self.map_zone_ready
        data["zone_status_reason"] = self.map_zone_status_reason
        self.coordinator.async_set_updated_data(data)

    def publish_job_update(self) -> None:
        """Notify coordinator entities of a new job without exposing an image."""
        data = dict(self.coordinator.data or {})
        data["last_job_id"] = self.last_job.get("job_id") if self.last_job else None
        data["last_job_status"] = self.last_job.get("status") if self.last_job else None
        self.coordinator.async_set_updated_data(data)

    def set_notification(self, event: dict[str, Any] | None, sequence: int) -> None:
        """Retain one bounded Xiaomi vacuum notification for HA entities."""
        self.notification_sequence = sequence
        if not event:
            return
        allowed = {
            "source",
            "package",
            "vacuum_id",
            "title",
            "text",
            "category",
            "event_at",
            "fingerprint",
        }
        self.latest_notification = {
            key: value for key, value in event.items() if key in allowed
        }
        category = str(self.latest_notification.get("category") or "status")
        if category == "needs_attention":
            self.notification_attention = True
        elif category in {"cleanup_completed", "returning_to_station"}:
            self.notification_attention = False

    def publish_notification_update(self) -> None:
        """Make notification metadata recorder-visible without waiting for a poll."""
        data = dict(self.coordinator.data or {})
        data["notification_sequence"] = self.notification_sequence
        data["notification_attention"] = self.notification_attention
        data["notification_monitor_available"] = self.notification_monitor_available
        if self.latest_notification:
            data["latest_notification"] = dict(self.latest_notification)
        self.coordinator.async_set_updated_data(data)
