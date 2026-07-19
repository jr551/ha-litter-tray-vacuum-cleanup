"""Pure, dependency-free rules for Sui the Hooverbot's job state machine."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ALLOWED_SKIP_REACTIONS = frozenset({"⏭", "❌", "🛑"})
SKIPPABLE_STATUSES = frozenset({"pending", "reaction_grace", "retrying"})
ACTIVE_STATUSES = frozenset({"pending", "reaction_grace", "retrying", "dispatching"})
MAX_IDENTIFIER_LENGTH = 512
LOCAL_TIME_ZONE = ZoneInfo("Europe/London")
NIGHT_START = time(22, 0)
MORNING_CLEANUP = time(6, 0)


def normalise_reaction(value: Any) -> str:
    """Compare WhatsApp presentation variants without broadening allowed actions."""
    return str(value or "").replace("\ufe0f", "").strip()


def valid_identifier(value: Any) -> str:
    """Accept bounded opaque bridge IDs only; never use them as paths or SQL."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_IDENTIFIER_LENGTH or any(ord(char) < 32 for char in text):
        raise ValueError("identifier must be a short, non-empty text value")
    return text


def parse_counter(value: Any) -> Decimal:
    """Require a finite non-negative MiniNook counter before scheduling."""
    raw = str(value or "").strip()
    if raw.lower() in {"", "unknown", "unavailable", "none", "null"}:
        raise ValueError("counter is unavailable")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("counter is not numeric") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("counter is invalid")
    return number


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def is_overnight_event(detected_at: float) -> bool:
    """Return whether an event belongs to the 22:00-06:00 quiet window."""
    local_time = datetime.fromtimestamp(detected_at, LOCAL_TIME_ZONE).time()
    return local_time >= NIGHT_START or local_time < MORNING_CLEANUP


def next_morning_dispatch(detected_at: float) -> float:
    """Map a night event to the one DST-safe 06:00 Europe/London run."""
    local = datetime.fromtimestamp(detected_at, LOCAL_TIME_ZONE)
    if not is_overnight_event(detected_at):
        raise ValueError("event is outside the overnight window")
    target_date = local.date() + (timedelta(days=1) if local.time() >= NIGHT_START else timedelta())
    return datetime.combine(target_date, MORNING_CLEANUP, LOCAL_TIME_ZONE).timestamp()


def family_message(dispatch_at: float, *, overnight: bool = False) -> str:
    """The only notification text Sui asks the opaque bridge to deliver."""
    local_time = datetime.fromtimestamp(dispatch_at, LOCAL_TIME_ZONE).strftime("%H:%M")
    timing = (
        "This is the single overnight cleanup; any more litter-tray visits "
        "before morning will be included in the same run."
        if overnight
        else "This follows the 10-minute opt-out window and short safety grace."
    )
    return (
        "💩 The cat used the litter tray. Sui the Hooverbot will hoover the "
        f"litter-box zone at {local_time}. {timing} Want to skip this cleanup? "
        "React ⏭️, ❌ or 🛑 "
        "to this message before then."
    )


def new_job(
    *,
    entry_id: str,
    source_event_key: str,
    source_count: Decimal,
    detected_at: float,
    cleanup_delay_seconds: float,
    reaction_grace_seconds: float,
    now: float,
    dispatch_at: float | None = None,
    schedule_kind: str = "daytime",
) -> dict[str, Any]:
    """Create one JSON-serializable job before sending any notification."""
    if not math.isfinite(detected_at) or not math.isfinite(now):
        raise ValueError("timestamps must be finite")
    job_id = str(uuid.uuid4())
    if dispatch_at is None:
        cleanup_at = detected_at + cleanup_delay_seconds
        dispatch_after = cleanup_at + reaction_grace_seconds
    else:
        if not math.isfinite(dispatch_at) or dispatch_at <= detected_at:
            raise ValueError("dispatch timestamp must follow detection")
        dispatch_after = dispatch_at
        cleanup_at = dispatch_after - reaction_grace_seconds
    return {
        "job_id": job_id,
        "event_key": f"sui:{entry_id}:{job_id}",
        "source_event_key": valid_identifier(source_event_key),
        "source_count": format(source_count, "f"),
        "detected_at": iso_utc(detected_at),
        "cleanup_at": iso_utc(cleanup_at),
        "dispatch_after": iso_utc(dispatch_after),
        "schedule_kind": schedule_kind,
        "coalesced_events": 1,
        "status": "notification_sending",
        "idempotency_key": f"sui-hooverbot-{uuid.uuid4().hex}",
        "created_at": iso_utc(now),
        "updated_at": iso_utc(now),
    }


def public_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Return recorder-safe fields; bridge IDs/tokens never become attributes."""
    fields = (
        "job_id",
        "source_count",
        "detected_at",
        "cleanup_at",
        "dispatch_after",
        "schedule_kind",
        "coalesced_events",
        "status",
        "created_at",
        "updated_at",
        "last_error",
        "skipped_at",
        "dispatched_at",
    )
    return {field: job[field] for field in fields if field in job and job[field] is not None}


def bridge_message_payload(
    *, event_key: str, text: str, deadline_at: str, callback_url: str
) -> dict[str, str]:
    """The exact narrow request understood by the opaque family bridge."""
    return {
        "event_key": valid_identifier(event_key),
        "consumer": "sui_hooverbot",
        "text": str(text),
        "deadline_at": str(deadline_at),
        "callback_url": str(callback_url),
    }


def bridge_status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status") or "").strip().lower()


def bridge_reports_skipped(payload: Mapping[str, Any]) -> bool:
    """Recognise the bridge's final accepted-reaction state only.

    The bridge intentionally does not expose raw WhatsApp reaction records to
    Home Assistant.  ``reaction_received`` is its durable, final decision for
    this exact message and therefore has the same effect as a local skip.
    """
    return bridge_status(payload) == "reaction_received"


def bridge_response_matches(payload: Mapping[str, Any], event_key: str) -> bool:
    """Require the final bridge response to correlate to this exact Sui job."""
    return payload.get("event_key") == event_key and payload.get("consumer") == "sui_hooverbot"


def bridge_allows_dispatch(payload: Mapping[str, Any]) -> bool:
    """The bridge contract grants motion only while the exact state is pending."""
    return bridge_status(payload) == "pending"
