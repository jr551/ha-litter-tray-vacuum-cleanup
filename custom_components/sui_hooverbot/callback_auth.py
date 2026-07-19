"""Small, dependency-free verifier for family bridge webhook callbacks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


CALLBACK_TIMESTAMP_HEADER = "X-Family-Reaction-Timestamp"
CALLBACK_SIGNATURE_HEADER = "X-Family-Reaction-Signature"
CALLBACK_SIGNATURE_PREFIX = b"family-reaction-callback-v1"
CALLBACK_MAX_AGE_SECONDS = 300
CALLBACK_MAX_FUTURE_SECONDS = 300


def callback_signature(token: str, timestamp: str, raw_body: bytes) -> str:
    """Return the domain-separated HMAC for this exact raw callback body."""
    signed = CALLBACK_SIGNATURE_PREFIX + b"." + timestamp.encode("ascii") + b"." + raw_body
    return hmac.new(token.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def callback_authentication_is_valid(
    *,
    token: str,
    timestamp: Any,
    signature: Any,
    raw_body: bytes,
    now: float,
) -> bool:
    """Reject malformed, stale, future, or modified callbacks before JSON parsing."""
    timestamp_text = str(timestamp or "")
    if not timestamp_text.isascii() or not timestamp_text.isdigit() or len(timestamp_text) > 16:
        return False
    try:
        timestamp_value = int(timestamp_text)
    except ValueError:
        return False
    if timestamp_value < now - CALLBACK_MAX_AGE_SECONDS:
        return False
    if timestamp_value > now + CALLBACK_MAX_FUTURE_SECONDS:
        return False
    supplied = str(signature or "")
    expected = f"sha256={callback_signature(token, timestamp_text, raw_body)}"
    return hmac.compare_digest(supplied, expected)
