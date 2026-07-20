"""Event-driven, persisted schedule runtime for Sui the Hooverbot."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .callback_auth import CALLBACK_SIGNATURE_HEADER, CALLBACK_TIMESTAMP_HEADER
from .bridge import (
    BridgeError,
    BridgeUnavailable,
    FamilyBridgeClient,
)
from .const import (
    CONF_CLEANUP_DELAY_SECONDS,
    CONF_COUNTER_ENTITY_ID,
    CONF_LITTER_ZONE,
    CONF_LITTER_ZONE_APPROVED,
    CONF_MAP_CAMERA_ENTITY_ID,
    CONF_MAX_LATENESS_SECONDS,
    CONF_REACTION_GRACE_SECONDS,
    CONF_VACUUM_ENTITY_ID,
    CONF_WEBHOOK_ID,
    DEFAULT_RETRY_SECONDS,
    DREAME_CLEANING_MODE,
    DREAME_CLEAN_ZONE_SERVICE,
    DREAME_DOMAIN,
    DREAME_REQUEST_MAP_SERVICE,
    DREAME_STANDARD_SUCTION_LEVEL,
    DOMAIN,
    EVENT_JOB_UPDATED,
    EVENT_SKIP,
    MAX_PENDING_JOBS,
    MAX_WEBHOOK_BYTES,
    SKIP_REACTIONS,
)
from .coordinator import SuiCoordinator
from .model import (
    ACTIVE_STATUSES,
    SKIPPABLE_STATUSES,
    bridge_allows_dispatch,
    bridge_reports_skipped,
    bridge_response_matches,
    family_message,
    is_overnight_event,
    iso_utc,
    new_job,
    next_morning_dispatch,
    normalise_reaction,
    parse_counter,
    parse_utc,
    public_job,
    valid_identifier,
)
from .store import SuiScheduleStore
from .validation import normalise_litter_zone


class SuiRuntime:
    """Own every scheduling decision; the bridge only transports messages."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: SuiCoordinator,
        bridge: FamilyBridgeClient,
        schedule_store: SuiScheduleStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.bridge = bridge
        self.store = schedule_store
        self._lock = asyncio.Lock()
        self._unsub_counter: CALLBACK_TYPE | None = None
        self._unsub_vacuum: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_skip_event: CALLBACK_TYPE | None = None
        self._webhook_registered = False

    @property
    def counter_entity_id(self) -> str:
        return str(self.entry.data[CONF_COUNTER_ENTITY_ID])

    @property
    def vacuum_entity_id(self) -> str:
        return str(self.entry.data[CONF_VACUUM_ENTITY_ID])

    @property
    def map_camera_entity_id(self) -> str:
        return str(self.entry.data[CONF_MAP_CAMERA_ENTITY_ID])

    @property
    def litter_zone_approved(self) -> bool:
        return self.entry.data.get(CONF_LITTER_ZONE_APPROVED) is True

    @property
    def litter_zone(self) -> list[int] | None:
        if not self.litter_zone_approved:
            return None
        try:
            return normalise_litter_zone(self.entry.data.get(CONF_LITTER_ZONE))
        except ValueError:
            return None

    @property
    def webhook_id(self) -> str:
        return str(self.entry.data[CONF_WEBHOOK_ID])

    @property
    def cleanup_delay(self) -> float:
        return float(self.entry.data[CONF_CLEANUP_DELAY_SECONDS])

    @property
    def reaction_grace(self) -> float:
        return float(self.entry.data[CONF_REACTION_GRACE_SECONDS])

    @property
    def max_lateness(self) -> float:
        return float(self.entry.data[CONF_MAX_LATENESS_SECONDS])

    @staticmethod
    def _now() -> float:
        return dt_util.utcnow().timestamp()

    async def async_setup(self) -> None:
        """Restore durable state, then subscribe to one narrow counter signal."""
        await self.store.async_load()
        try:
            async with self._lock:
                changed = self._recover_interrupted_jobs_locked()
                changed |= await self._establish_baseline_locked()
                # Register before the first persistence await. A state change
                # after the baseline sample will queue behind this lock, then
                # compare against that stored baseline rather than being lost.
                self._unsub_counter = async_track_state_change_event(
                    self.hass, [self.counter_entity_id], self._handle_counter_event
                )
                self._unsub_vacuum = async_track_state_change_event(
                    self.hass, [self.vacuum_entity_id], self._handle_vacuum_event
                )
                self._unsub_skip_event = self.hass.bus.async_listen(
                    EVENT_SKIP, self._handle_skip_event
                )
                if changed:
                    await self.store.async_save()
                self._rearm_locked()
                self.coordinator.publish()

            webhook.async_register(
                self.hass,
                DOMAIN,
                "Sui the Hooverbot skip callback",
                self.webhook_id,
                self.async_handle_webhook,
                local_only=False,
                allowed_methods={METH_POST},
            )
            self._webhook_registered = True
        except Exception:
            self._unsubscribe_local_callbacks()
            if self._webhook_registered:
                webhook.async_unregister(self.hass, self.webhook_id)
                self._webhook_registered = False
            raise

    async def async_unload(self) -> None:
        """Remove callbacks; no delayed timer can survive an entry unload."""
        self._unsubscribe_local_callbacks()
        if self._webhook_registered:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._webhook_registered = False

    def _unsubscribe_local_callbacks(self) -> None:
        """Tear down listeners/timer after partial setup or normal unload."""
        for callback_fn in (
            self._unsub_counter,
            self._unsub_vacuum,
            self._unsub_timer,
            self._unsub_skip_event,
        ):
            if callback_fn is not None:
                callback_fn()
        self._unsub_counter = None
        self._unsub_vacuum = None
        self._unsub_timer = None
        self._unsub_skip_event = None

    def _recover_interrupted_jobs_locked(self) -> bool:
        changed = False
        for job in self._jobs:
            status = str(job.get("status") or "")
            job_changed = False
            if status == "notification_sending":
                job.update(status="notification_uncertain", last_error="service_interrupted")
                job_changed = True
            elif status == "dispatching":
                if job.get("zone_start_attempted_at"):
                    job.update(status="outcome_unknown", last_error="service_interrupted")
                else:
                    job.update(status="retrying", next_attempt_at=iso_utc(self._now()))
                job_changed = True
            if job_changed:
                job["updated_at"] = iso_utc(self._now())
                changed = True
        return changed

    async def _establish_baseline_locked(self) -> bool:
        """Never retrospectively clean an event that predates integration setup."""
        if self.store.data.get("last_counter") is not None:
            return False
        state = self.hass.states.get(self.counter_entity_id)
        if state is None:
            return False
        try:
            count = parse_counter(state.state)
        except ValueError:
            return False
        self.store.data["last_counter"] = format(count, "f")
        self.store.data["last_counter_event_key"] = state.last_changed.isoformat()
        return True

    @property
    def _jobs(self) -> list[dict[str, Any]]:
        return self.store.data["jobs"]

    def _job_by_event_key(self, event_key: str) -> dict[str, Any] | None:
        return next((job for job in self._jobs if job.get("event_key") == event_key), None)

    def _job_by_id(self, job_id: str) -> dict[str, Any] | None:
        return next((job for job in self._jobs if job.get("job_id") == job_id), None)

    def _active_jobs(self) -> list[dict[str, Any]]:
        return [job for job in self._jobs if str(job.get("status")) in ACTIVE_STATUSES]

    def _active_overnight_job(self, dispatch_at: float) -> dict[str, Any] | None:
        """Find the already-notified job for this exact morning run."""
        return next(
            (
                job
                for job in self._active_jobs()
                if job.get("schedule_kind") == "overnight"
                and abs(parse_utc(job["dispatch_after"]) - dispatch_at) < 1
            ),
            None,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return an entity-safe view: no bridge IDs, callbacks or credentials."""
        active = sorted(self._active_jobs(), key=lambda job: str(job.get("dispatch_after") or ""))
        last = max(self._jobs, key=lambda job: str(job.get("updated_at") or ""), default=None)
        if active:
            first_status = str(active[0].get("status"))
            state = "starting" if first_status == "dispatching" else (
                "reaction_grace" if first_status == "reaction_grace" else "countdown"
            )
        elif last is None:
            state = "idle"
        else:
            state = {
                "dispatched": "cleaning_requested",
                "skipped": "skipped",
                "notification_uncertain": "notification_failed",
                "outcome_unknown": "attention",
                "transport_unavailable": "attention",
                "missed": "missed",
            }.get(str(last.get("status")), "idle")
        vacuum_state = self.hass.states.get(self.vacuum_entity_id)
        vacuum_needs_attention = bool(
            vacuum_state is None
            or vacuum_state.state.lower() in {"unavailable", "unknown"}
            or vacuum_state.attributes.get("needs_attention")
        )
        return {
            "state": state,
            "counter": self.store.data.get("last_counter"),
            "pending_jobs": len(active),
            "next_job": public_job(active[0]) if active else None,
            "last_job": public_job(last) if last else None,
            "needs_attention": bool(
                vacuum_needs_attention
                or (
                    last
                    and str(last.get("status"))
                    in {"notification_uncertain", "outcome_unknown", "transport_unavailable"}
                )
            ),
            "control_backend": DREAME_DOMAIN,
            "map_camera_entity_id": self.map_camera_entity_id,
            "litter_zone_approved": self.litter_zone is not None,
        }

    async def _commit_locked(self, job: dict[str, Any] | None = None) -> None:
        await self.store.async_save()
        self.coordinator.publish()
        if job is not None:
            self.hass.bus.async_fire(
                EVENT_JOB_UPDATED,
                {"entry_id": self.entry.entry_id, "job": public_job(job)},
            )

    def _callback_url(self) -> str:
        """Use HA's supported configured external URL, never an inferred host."""
        try:
            base_url = get_url(
                self.hass,
                allow_internal=False,
                prefer_external=True,
                require_ssl=True,
                allow_cloud=False,
            ).rstrip("/")
        except NoURLAvailableError as exc:
            raise BridgeUnavailable("callback_url_unavailable") from exc
        if not base_url.startswith("https://"):
            raise BridgeUnavailable("callback_url_unavailable")
        return f"{base_url}/api/webhook/{self.webhook_id}"

    @callback
    def _handle_counter_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if isinstance(new_state, State):
            self.hass.async_create_task(self.async_handle_counter_state(new_state))

    @callback
    def _handle_vacuum_event(self, _event: Event) -> None:
        """Mirror the existing Xiaomi safety signal into Sui's status now."""
        self.coordinator.publish()

    async def async_handle_counter_state(self, state: State) -> None:
        """Turn only a genuine increasing MiniNook count into one Sui job."""
        try:
            count = parse_counter(state.state)
            source_event_key = valid_identifier(state.last_changed.isoformat())
        except ValueError:
            return
        job: dict[str, Any] | None = None
        async with self._lock:
            old_raw = self.store.data.get("last_counter")
            old_event_key = self.store.data.get("last_counter_event_key")
            if source_event_key == old_event_key:
                return
            self.store.data["last_counter"] = format(count, "f")
            self.store.data["last_counter_event_key"] = source_event_key
            try:
                old_count = Decimal(str(old_raw)) if old_raw is not None else None
            except InvalidOperation:
                old_count = None
            if old_count is None or count <= old_count:
                await self._commit_locked()
                return
            detected_at = state.last_changed.timestamp()
            overnight = is_overnight_event(detected_at)
            morning_dispatch = next_morning_dispatch(detected_at) if overnight else None
            if morning_dispatch is not None:
                existing = self._active_overnight_job(morning_dispatch)
                if existing is not None:
                    existing.update(
                        source_count=format(count, "f"),
                        last_detected_at=iso_utc(detected_at),
                        coalesced_events=int(existing.get("coalesced_events") or 1) + 1,
                        updated_at=iso_utc(self._now()),
                    )
                    await self._commit_locked(existing)
                    return
            if len(self._active_jobs()) >= MAX_PENDING_JOBS:
                # Preserve the observed baseline but never enqueue an unbounded
                # physical-control backlog.
                await self._commit_locked()
                return
            job = new_job(
                entry_id=self.entry.entry_id,
                source_event_key=source_event_key,
                source_count=count,
                detected_at=detected_at,
                cleanup_delay_seconds=self.cleanup_delay,
                reaction_grace_seconds=self.reaction_grace,
                now=self._now(),
                dispatch_at=morning_dispatch,
                schedule_kind="overnight" if overnight else "daytime",
            )
            self._jobs.append(job)
            # Persist first: a restart cannot lose the event and then silently
            # issue a second notification/cleanup.
            await self._commit_locked(job)

            try:
                await self.bridge.async_send_message(
                    event_key=str(job["event_key"]),
                    text=family_message(
                        parse_utc(job["dispatch_after"]), overnight=overnight
                    ),
                    deadline_at=str(job["cleanup_at"]),
                    callback_url=self._callback_url(),
                )
            except BridgeError:
                # A missing acknowledgement means the family may not have a
                # usable opt-out message. Never retry or move the robot.
                job.update(
                    status="notification_uncertain",
                    last_error="bridge_notification_unavailable",
                    updated_at=iso_utc(self._now()),
                )
            else:
                job.update(status="pending", updated_at=iso_utc(self._now()))
            await self._commit_locked(job)
            self._rearm_locked()

    @callback
    def _handle_skip_event(self, event: Event) -> None:
        data = event.data
        if str(data.get("entry_id") or "") != self.entry.entry_id:
            return
        self.hass.async_create_task(
            self.async_mark_skipped_local(
                job_id=data.get("job_id"),
                reaction_event_id=data.get("reaction_event_id"),
                reaction=data.get("reaction"),
            )
        )

    async def async_handle_webhook(
        self, _hass: HomeAssistant, _webhook_id: str, request: web.Request
    ) -> web.Response:
        """Accept only the bridge's opaque, exact-reaction callback contract."""
        if request.content_length is not None and request.content_length > MAX_WEBHOOK_BYTES:
            return web.Response(status=413)
        try:
            raw_body = await request.content.read(MAX_WEBHOOK_BYTES + 1)
        except (ValueError, asyncio.LimitOverrunError):
            return web.Response(status=400)
        if len(raw_body) > MAX_WEBHOOK_BYTES:
            return web.Response(status=413)
        if not self.bridge.callback_is_authenticated(
            timestamp=request.headers.get(CALLBACK_TIMESTAMP_HEADER),
            signature=request.headers.get(CALLBACK_SIGNATURE_HEADER),
            raw_body=raw_body,
            now=self._now(),
        ):
            return web.Response(status=401)
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.Response(status=400)
        if not isinstance(payload, Mapping):
            return web.Response(status=400)
        if str(payload.get("consumer") or "") != DOMAIN:
            return web.Response(status=400)
        try:
            await self.async_mark_skipped_from_bridge(
                event_key=payload.get("event_key"),
                reaction_event_id=payload.get("reaction_event_id"),
                reaction=payload.get("reaction"),
            )
        except ValueError:
            return web.Response(status=400)
        # Do not reveal whether a particular job/message ID exists.
        return web.json_response({"accepted": True}, status=202)

    async def async_mark_skipped_from_bridge(
        self, *, event_key: Any, reaction_event_id: Any, reaction: Any
    ) -> str:
        """Use only the bridge's exact, opaque event key after its verification."""
        event_key = valid_identifier(event_key)
        async with self._lock:
            return await self._async_mark_skipped_locked(
                self._job_by_event_key(event_key), reaction_event_id, reaction
            )

    async def async_mark_skipped_local(
        self, *, job_id: Any, reaction_event_id: Any, reaction: Any
    ) -> str:
        """Trusted local HA path: use the safe public job ID, never bridge IDs."""
        job_id = valid_identifier(job_id)
        async with self._lock:
            return await self._async_mark_skipped_locked(
                self._job_by_id(job_id), reaction_event_id, reaction
            )

    async def _async_mark_skipped_locked(
        self, job: dict[str, Any] | None, reaction_event_id: Any, reaction: Any
    ) -> str:
        """Atomically accept one allowed reaction before Sui claims dispatch."""
        event_id = valid_identifier(reaction_event_id)
        reaction = normalise_reaction(reaction)
        if event_id in self.store.data["processed_events"]:
            return "duplicate"
        now = self._now()
        if reaction not in SKIP_REACTIONS:
            outcome = "wrong_reaction"
        elif job is None:
            outcome = "not_found"
        elif str(job.get("status")) not in SKIPPABLE_STATUSES:
            outcome = "late"
        elif now >= parse_utc(job["dispatch_after"]):
            outcome = "late"
        else:
            job.update(
                status="skipped",
                skipped_at=iso_utc(now),
                updated_at=iso_utc(now),
                last_error=None,
            )
            outcome = "skipped"
        self.store.data["processed_events"].append(event_id)
        await self._commit_locked(job if outcome == "skipped" else None)
        self._rearm_locked()
        return outcome

    def _next_wakeup_locked(self) -> float | None:
        now = self._now()
        candidates: list[float] = []
        for job in self._jobs:
            status = str(job.get("status"))
            if status == "pending":
                cleanup = parse_utc(job["cleanup_at"])
                dispatch = parse_utc(job["dispatch_after"])
                candidates.append(cleanup if now < cleanup else dispatch)
            elif status == "reaction_grace":
                candidates.append(parse_utc(job["dispatch_after"]))
            elif status == "retrying":
                candidates.append(parse_utc(job.get("next_attempt_at") or job["dispatch_after"]))
        return min(candidates) if candidates else None

    def _rearm_locked(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        wake_at = self._next_wakeup_locked()
        if wake_at is None:
            return
        point = datetime.fromtimestamp(max(wake_at, self._now()), timezone.utc)
        self._unsub_timer = async_track_point_in_time(self.hass, self._handle_timer, point)

    @callback
    def _handle_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.hass.async_create_task(self.async_process_schedule())

    async def async_process_schedule(self) -> None:
        """Advance grace/countdown state and dispatch one due job at most."""
        async with self._lock:
            now = self._now()
            changed = False
            for job in self._jobs:
                if (
                    job.get("status") == "pending"
                    and parse_utc(job["cleanup_at"]) <= now < parse_utc(job["dispatch_after"])
                ):
                    job.update(status="reaction_grace", updated_at=iso_utc(now))
                    changed = True

            due_jobs = sorted(
                (
                    job
                    for job in self._jobs
                    if str(job.get("status")) in {"pending", "reaction_grace", "retrying"}
                    and parse_utc(job["dispatch_after"]) <= now
                    and (
                        not job.get("next_attempt_at")
                        or parse_utc(job["next_attempt_at"]) <= now
                    )
                ),
                key=lambda job: str(job.get("dispatch_after")),
            )
            job = due_jobs[0] if due_jobs else None
            if job is not None:
                if now - parse_utc(job["dispatch_after"]) > self.max_lateness:
                    job.update(status="missed", last_error="dispatch_too_late", updated_at=iso_utc(now))
                    changed = True
                    job = None
                else:
                    job.update(status="dispatching", dispatch_claimed_at=iso_utc(now), updated_at=iso_utc(now))
                    changed = True
            if changed:
                await self._commit_locked(job)
            if job is not None:
                await self._dispatch_locked(job)
            self._rearm_locked()

    async def _retry_or_miss_locked(self, job: dict[str, Any], error_code: str) -> None:
        now = self._now()
        if now - parse_utc(job["dispatch_after"]) > self.max_lateness:
            job.update(status="missed", last_error="dispatch_too_late", updated_at=iso_utc(now))
        else:
            job.update(
                status="retrying",
                last_error=error_code,
                next_attempt_at=iso_utc(now + DEFAULT_RETRY_SECONDS),
                updated_at=iso_utc(now),
            )
        await self._commit_locked(job)

    def _vacuum_is_ready(self) -> bool:
        state = self.hass.states.get(self.vacuum_entity_id)
        if state is None or state.state.lower() not in {"idle", "docked"}:
            return False
        attributes = state.attributes
        if attributes.get("needs_attention") or attributes.get("has_error"):
            return False
        if attributes.get("faults"):
            return False
        error = str(attributes.get("error") or "No error").strip().lower()
        if error not in {"no error", "none"}:
            return False
        cleaning_mode = str(attributes.get("cleaning_mode") or "").strip().lower()
        return cleaning_mode == DREAME_CLEANING_MODE

    def _fresh_map_generation(self) -> str | None:
        state = self.hass.states.get(self.map_camera_entity_id)
        if state is None or state.state.lower() in {"unknown", "unavailable"}:
            return None
        calibration = state.attributes.get("calibration_points")
        if not isinstance(calibration, list) or len(calibration) < 3:
            return None
        generation = f"{state.state}:{state.last_updated.isoformat()}"
        return generation if len(generation) <= 512 else None

    async def _dispatch_locked(self, job: dict[str, Any]) -> None:
        """Prepare the map, final-check the bridge, then make one zone request."""
        zone = self.litter_zone
        if zone is None:
            job.update(
                status="transport_unavailable",
                last_error="litter_zone_not_approved",
                updated_at=iso_utc(self._now()),
            )
            await self._commit_locked(job)
            return
        if not self.hass.services.has_service(DREAME_DOMAIN, DREAME_REQUEST_MAP_SERVICE):
            await self._retry_or_miss_locked(job, "direct_map_service_unavailable")
            return
        if not self.hass.services.has_service(DREAME_DOMAIN, DREAME_CLEAN_ZONE_SERVICE):
            await self._retry_or_miss_locked(job, "direct_zone_service_unavailable")
            return
        if not self._vacuum_is_ready():
            await self._retry_or_miss_locked(job, "vacuum_not_ready")
            return
        try:
            await self.hass.services.async_call(
                DREAME_DOMAIN,
                DREAME_REQUEST_MAP_SERVICE,
                {"entity_id": self.vacuum_entity_id},
                blocking=True,
            )
        except Exception:
            await self._retry_or_miss_locked(job, "map_refresh_unavailable")
            return
        generation = self._fresh_map_generation()
        if generation is None:
            await self._retry_or_miss_locked(job, "map_generation_unavailable")
            return

        # This is deliberately immediately before the only physical service
        # call. A lost callback cannot overrule a bridge-confirmed skip.
        try:
            bridge_state = await self.bridge.async_get_message(str(job["event_key"]))
        except BridgeError:
            job.update(
                status="transport_unavailable",
                last_error="bridge_final_check_unavailable",
                updated_at=iso_utc(self._now()),
            )
            await self._commit_locked(job)
            return
        if not bridge_response_matches(bridge_state, str(job["event_key"])):
            job.update(
                status="transport_unavailable",
                last_error="bridge_final_check_mismatch",
                updated_at=iso_utc(self._now()),
            )
            await self._commit_locked(job)
            return
        if bridge_reports_skipped(bridge_state):
            job.update(status="skipped", skipped_at=iso_utc(self._now()), updated_at=iso_utc(self._now()))
            await self._commit_locked(job)
            return
        if not bridge_allows_dispatch(bridge_state):
            job.update(
                status="transport_unavailable",
                last_error="bridge_final_check_ambiguous",
                updated_at=iso_utc(self._now()),
            )
            await self._commit_locked(job)
            return
        # The map refresh and bridge final check both await. Re-check the
        # observed Xiaomi safety state immediately before the one physical
        # request, so a manual start/error cannot be raced into by Sui.
        if not self._vacuum_is_ready():
            await self._retry_or_miss_locked(job, "vacuum_not_ready_before_start")
            return

        # Persist before calling start_zone. From this point any failure is
        # outcome-unknown and will never be retried automatically.
        job.update(zone_start_attempted_at=iso_utc(self._now()), updated_at=iso_utc(self._now()))
        await self._commit_locked(job)
        try:
            await self.hass.services.async_call(
                DREAME_DOMAIN,
                DREAME_CLEAN_ZONE_SERVICE,
                {
                    "entity_id": self.vacuum_entity_id,
                    "zone": zone,
                    "repeats": 1,
                    "suction_level": DREAME_STANDARD_SUCTION_LEVEL,
                },
                blocking=True,
            )
        except Exception:
            job.update(
                status="outcome_unknown",
                last_error="zone_start_outcome_unknown",
                updated_at=iso_utc(self._now()),
            )
        else:
            job.update(status="dispatched", dispatched_at=iso_utc(self._now()), updated_at=iso_utc(self._now()))
        await self._commit_locked(job)
