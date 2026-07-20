"""Pure config-flow validation rules, independently testable without HA."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .const import (
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_CLEANUP_DELAY_SECONDS,
    CONF_COUNTER_ENTITY_ID,
    CONF_LITTER_ZONE,
    CONF_LITTER_ZONE_APPROVED,
    CONF_MAP_CAMERA_ENTITY_ID,
    CONF_MAX_LATENESS_SECONDS,
    CONF_REACTION_GRACE_SECONDS,
    CONF_VACUUM_ENTITY_ID,
)


def normalise_litter_zone(value: Any, *, allow_empty: bool = False) -> list[int]:
    """Validate one bounded Dreame rectangle in native millimetre coordinates."""
    if isinstance(value, str):
        raw_values: list[Any] = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    elif value is None:
        raw_values = []
    else:
        raise ValueError("Litter zone must contain four coordinates")
    if not raw_values and allow_empty:
        return []
    if len(raw_values) != 4 or any(isinstance(item, bool) for item in raw_values):
        raise ValueError("Litter zone must contain four integer coordinates")
    try:
        zone = [int(item) for item in raw_values]
    except (TypeError, ValueError) as exc:
        raise ValueError("Litter zone must contain four integer coordinates") from exc
    if any(str(item).strip() != str(number) for item, number in zip(raw_values, zone)):
        raise ValueError("Litter zone must contain four integer coordinates")
    x0, y0, x1, y1 = zone
    if any(abs(number) > 50_000 for number in zone):
        raise ValueError("Litter zone coordinates are outside the safe map bounds")
    width = x1 - x0
    height = y1 - y0
    if width < 200 or height < 200 or width > 10_000 or height > 10_000:
        raise ValueError("Litter zone edges must be between 200 and 10000 millimetres")
    if width * height > 50_000_000:
        raise ValueError("Litter zone is too large")
    return zone


def litter_zone_text(value: Any) -> str:
    """Render stored coordinates for the text-based Home Assistant form."""
    zone = normalise_litter_zone(value, allow_empty=True)
    return ",".join(str(number) for number in zone)


def normalize_bridge_url(value: str) -> str:
    """Allow only an HTTP(S) endpoint root; credentials stay in the token field."""
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Bridge URL must start with http:// or https://")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Bridge URL has an invalid port") from exc
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Bridge URL must not embed credentials, query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("Bridge URL must not include a path")
    return url


def _entity_id(value: Any, expected_domain: str) -> str:
    entity_id = str(value or "").strip()
    if not entity_id.startswith(f"{expected_domain}.") or " " in entity_id:
        raise ValueError(f"Expected a {expected_domain} entity ID")
    return entity_id


def _positive_seconds(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("Expected whole seconds")
    try:
        raw = str(value).strip()
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected whole seconds") from exc
    if raw != str(seconds):
        raise ValueError("Expected whole seconds")
    if not minimum <= seconds <= maximum:
        raise ValueError(f"Expected a value between {minimum} and {maximum}")
    return seconds


def normalise_config_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return safe config-entry data, excluding the generated webhook ID."""
    token = str(raw.get(CONF_BRIDGE_TOKEN) or "").strip()
    if not token:
        raise ValueError("Bridge token is required")
    approved = raw.get(CONF_LITTER_ZONE_APPROVED, False)
    if not isinstance(approved, bool):
        raise ValueError("Litter zone approval must be an explicit checkbox")
    zone = normalise_litter_zone(raw.get(CONF_LITTER_ZONE), allow_empty=not approved)
    return {
        CONF_COUNTER_ENTITY_ID: _entity_id(raw.get(CONF_COUNTER_ENTITY_ID), "sensor"),
        CONF_VACUUM_ENTITY_ID: _entity_id(raw.get(CONF_VACUUM_ENTITY_ID), "vacuum"),
        CONF_MAP_CAMERA_ENTITY_ID: _entity_id(
            raw.get(CONF_MAP_CAMERA_ENTITY_ID), "camera"
        ),
        CONF_LITTER_ZONE: zone,
        CONF_LITTER_ZONE_APPROVED: approved,
        CONF_BRIDGE_URL: normalize_bridge_url(str(raw.get(CONF_BRIDGE_URL) or "")),
        CONF_BRIDGE_TOKEN: token,
        CONF_CLEANUP_DELAY_SECONDS: _positive_seconds(
            raw.get(CONF_CLEANUP_DELAY_SECONDS), 60, 3600
        ),
        CONF_REACTION_GRACE_SECONDS: _positive_seconds(
            raw.get(CONF_REACTION_GRACE_SECONDS), 0, 120
        ),
        CONF_MAX_LATENESS_SECONDS: _positive_seconds(
            raw.get(CONF_MAX_LATENESS_SECONDS), 15, 3600
        ),
    }


def schedule_identity(data: Mapping[str, Any]) -> str:
    """One physical counter/vacuum pair may have only one Sui scheduler."""
    return f"{data[CONF_COUNTER_ENTITY_ID]}:{data[CONF_VACUUM_ENTITY_ID]}"
