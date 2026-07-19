"""Polling coordinator for the deterministic Android vacuum gateway."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import GatewayClient, GatewayError
from .const import (
    CLEANING_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_ANDROID_NOTIFICATION,
    EVENT_NEEDS_ATTENTION,
    IDLE_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class XiaomiAndroidVacuumCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll only observed phone state, without opening Xiaomi Home from idle."""

    def __init__(
        self, hass: HomeAssistant, client: GatewayClient, config_entry_id: str
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=IDLE_UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.config_entry_id = config_entry_id
        self._attention_active = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status, retaining a healthy integration when the phone is busy."""
        try:
            state = await self.client.async_get_state()
        except GatewayError as err:
            # A temporarily busy phone must not prevent HA from starting.  The
            # entity becomes unavailable until the next passive poll succeeds.
            _LOGGER.debug("Android vacuum state poll failed: %s", err)
            state = {
                "activity": "unknown",
                "bridge_available": False,
                "last_error": str(err),
                "needs_attention": False,
            }
        else:
            state = dict(state)
            state["bridge_available"] = True
            state.pop("last_error", None)

        activity = state.get("activity")
        poll_activity = activity if activity != "unknown" else state.get("last_known_activity")
        self.update_interval = (
            CLEANING_UPDATE_INTERVAL
            if poll_activity in {"cleaning", "paused", "returning"}
            else IDLE_UPDATE_INTERVAL
        )
        self._async_emit_attention_event(state)
        return state

    def _async_emit_attention_event(self, state: dict[str, Any]) -> None:
        """Record a single HA event when the phone observes a stuck/error state."""
        needs_attention = bool(state.get("needs_attention"))
        notification_id = f"{DOMAIN}_{self.config_entry_id}_needs_attention"
        if needs_attention and not self._attention_active:
            reason = str(state.get("attention_reason") or "Xiaomi Home reported a problem")
            self.hass.bus.async_fire(
                EVENT_NEEDS_ATTENTION,
                {
                    "config_entry_id": self.config_entry_id,
                    "vacuum_id": state.get("vacuum_id"),
                    "activity": state.get("activity"),
                    "last_seen": state.get("last_seen"),
                    "workflow_version": state.get("workflow_version"),
                    "attention_reason": reason,
                    "attention_since": state.get("attention_since"),
                },
            )
            persistent_notification.async_create(
                self.hass,
                (
                    "The Xiaomi Android bridge observed a vacuum problem "
                    f"({reason}). Open Xiaomi Home to inspect it before starting another zone."
                ),
                title="Xiaomi Robot Vacuum needs attention",
                notification_id=notification_id,
            )
        elif not needs_attention and self._attention_active:
            persistent_notification.async_dismiss(self.hass, notification_id)
        self._attention_active = needs_attention

    def async_emit_android_notification(self, event: dict[str, Any]) -> None:
        """Publish one filtered Xiaomi notification as an HA event."""
        payload = {
            key: event.get(key)
            for key in (
                "source",
                "package",
                "vacuum_id",
                "title",
                "text",
                "category",
                "event_at",
                "fingerprint",
            )
            if event.get(key) is not None
        }
        payload["config_entry_id"] = self.config_entry_id
        self.hass.bus.async_fire(EVENT_ANDROID_NOTIFICATION, payload)
        if event.get("category") == "needs_attention":
            title = str(event.get("title") or "Xiaomi Robot Vacuum needs attention")
            persistent_notification.async_create(
                self.hass,
                f"Xiaomi Home reported: {title}",
                title="Sui the Hooverbot needs attention",
                notification_id=f"{DOMAIN}_{self.config_entry_id}_android_notification",
            )
        elif event.get("category") in {"cleanup_completed", "returning_to_station"}:
            persistent_notification.async_dismiss(
                self.hass,
                f"{DOMAIN}_{self.config_entry_id}_android_notification",
            )
