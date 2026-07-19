"""Atomic Home Assistant Store wrapper for small Sui schedule records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MAX_PROCESSED_EVENTS, MAX_TERMINAL_JOBS, STORAGE_VERSION


def empty_state() -> dict[str, Any]:
    return {
        "last_counter": None,
        "last_counter_event_key": None,
        "jobs": [],
        "processed_events": [],
    }


class SuiScheduleStore:
    """Keep only JSON data; critical transitions are saved before side effects."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self.data = empty_state()

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if not isinstance(raw, Mapping):
            self.data = empty_state()
            return
        jobs = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
        events = raw.get("processed_events") if isinstance(raw.get("processed_events"), list) else []
        self.data = {
            "last_counter": raw.get("last_counter"),
            "last_counter_event_key": raw.get("last_counter_event_key"),
            "jobs": [dict(item) for item in jobs if isinstance(item, Mapping)],
            "processed_events": [str(item) for item in events if isinstance(item, str)][
                -MAX_PROCESSED_EVENTS:
            ],
        }
        self._prune()

    async def async_save(self) -> None:
        self._prune()
        await self._store.async_save(self.data)

    def _prune(self) -> None:
        jobs = self.data["jobs"]
        terminal = [
            item
            for item in jobs
            if str(item.get("status"))
            in {
                "dispatched",
                "skipped",
                "missed",
                "notification_uncertain",
                "outcome_unknown",
                "transport_unavailable",
            }
        ]
        active = [item for item in jobs if item not in terminal]
        terminal.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        self.data["jobs"] = active + terminal[:MAX_TERMINAL_JOBS]
        self.data["processed_events"] = self.data["processed_events"][-MAX_PROCESSED_EVENTS:]
