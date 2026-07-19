"""Narrow client for the opaque family notification/reaction bridge."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientSession

from .callback_auth import callback_authentication_is_valid
from .const import BRIDGE_TIMEOUT
from .model import bridge_message_payload
from .validation import normalize_bridge_url


class BridgeError(Exception):
    """A safe bridge failure with no raw body or credential in its text."""


class BridgeUnavailable(BridgeError):
    """The bridge could not be reached or returned malformed JSON."""


class BridgeRejected(BridgeError):
    """The bridge rejected a narrow Sui request or status was ambiguous."""


class FamilyBridgeClient:
    """Only send Sui prompts and inspect their final reaction state."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self.base_url = normalize_bridge_url(base_url)
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    async def _response_json(self, response: ClientResponse) -> Mapping[str, Any]:
        try:
            payload = await response.json(content_type=None)
        except (ClientError, json.JSONDecodeError) as exc:
            if response.status >= 400:
                raise BridgeRejected(f"bridge_http_{response.status}") from exc
            raise BridgeUnavailable("bridge_invalid_json") from exc
        if not isinstance(payload, Mapping):
            raise BridgeUnavailable("bridge_invalid_json")
        if response.status >= 400:
            raise BridgeRejected(f"bridge_http_{response.status}")
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            async with asyncio.timeout(BRIDGE_TIMEOUT.total_seconds()):
                async with self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers,
                    json=dict(payload) if payload is not None else None,
                    allow_redirects=False,
                ) as response:
                    return await self._response_json(response)
        except TimeoutError as exc:
            raise BridgeUnavailable("bridge_timeout") from exc
        except ClientError as exc:
            raise BridgeUnavailable("bridge_unavailable") from exc

    async def async_send_message(
        self,
        *,
        event_key: str,
        text: str,
        deadline_at: str,
        callback_url: str,
    ) -> None:
        """Ask the bridge to own the chat/message-ID correlation."""
        response = await self._request(
            "POST",
            "/v1/messages",
            payload=bridge_message_payload(
                event_key=event_key,
                text=text,
                deadline_at=deadline_at,
                callback_url=callback_url,
            ),
        )
        if str(response.get("status") or "").strip().lower() != "pending":
            raise BridgeRejected("bridge_not_pending")

    async def async_get_message(self, event_key: str) -> Mapping[str, Any]:
        """Return the bridge's final authoritative state for one exact prompt."""
        return await self._request("GET", f"/v1/messages/{quote(event_key, safe='')}")

    def callback_is_authenticated(
        self, *, timestamp: Any, signature: Any, raw_body: bytes, now: float
    ) -> bool:
        """Authenticate raw bridge callbacks without exposing the shared key."""
        return callback_authentication_is_valid(
            token=self._token,
            timestamp=timestamp,
            signature=signature,
            raw_body=raw_body,
            now=now,
        )
