"""Client for the deliberately narrow Android vacuum gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import API_PREFIX, REQUEST_TIMEOUT, VACUUM_ID


class GatewayError(Exception):
    """Base exception for a gateway request failure."""


class GatewayConnectionError(GatewayError):
    """The gateway could not be reached."""


class GatewayAuthError(GatewayError):
    """The configured bearer token was rejected."""


class GatewayBusyError(GatewayError):
    """The dedicated phone is in use or another workflow owns it."""


class GatewayWorkflowError(GatewayError):
    """The gateway safely refused an Android workflow."""


def normalize_base_url(value: str) -> str:
    """Validate and normalize a gateway base URL without retaining a path."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Gateway URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("Gateway URL must not embed credentials")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Gateway URL must not include a path, query, or fragment")
    return value


class GatewayClient:
    """Authenticated async client for one deterministic vacuum gateway."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    async def _response_json(self, response: ClientResponse) -> dict[str, Any]:
        """Decode a JSON response and convert HTTP errors to useful exceptions."""
        try:
            payload = await response.json(content_type=None)
        except (json.JSONDecodeError, ClientError) as err:
            payload = {"detail": "Gateway returned a non-JSON response"}
            if response.status < 400:
                raise GatewayError(payload["detail"]) from err

        if not isinstance(payload, dict):
            raise GatewayError("Gateway returned an invalid JSON object")
        if response.status < 400:
            return payload

        detail = str(payload.get("detail") or payload.get("error") or response.reason)
        if response.status == 401:
            raise GatewayAuthError("Gateway rejected the configured token")
        if response.status == 409:
            raise GatewayBusyError(detail)
        if response.status == 412:
            raise GatewayWorkflowError(detail)
        raise GatewayError(f"Gateway HTTP {response.status}: {detail}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Make one bounded request without logging credentials or map contents."""
        headers = dict(self._headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=dict(payload) if payload is not None else None,
                ) as response:
                    return await self._response_json(response)
        except TimeoutError as err:
            raise GatewayConnectionError("Timed out contacting the Android vacuum gateway") from err
        except ClientError as err:
            raise GatewayConnectionError("Cannot reach the Android vacuum gateway") from err

    async def async_get_state(self) -> dict[str, Any]:
        """Return the gateway's observed state; this never foregrounds Xiaomi Home."""
        return await self._request(
            "GET", f"{API_PREFIX}/vacuums/{VACUUM_ID}/state"
        )

    async def async_get_map(self) -> dict[str, Any]:
        """Fetch an explicitly requested map preview and its generation token."""
        return await self._request(
            "GET", f"{API_PREFIX}/vacuums/{VACUUM_ID}/map"
        )

    async def async_get_notifications(self) -> dict[str, Any]:
        """Fetch only privacy-filtered Xiaomi vacuum notification metadata."""
        return await self._request(
            "GET", f"{API_PREFIX}/vacuums/{VACUUM_ID}/notifications"
        )

    async def async_start_zone(
        self,
        *,
        zone_name: str | None,
        rectangles: list[dict[str, int]] | None,
        map_generation: str | None,
        dry_run: bool,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Start a pre-named or explicitly previewed rectangular zone job."""
        body: dict[str, Any] = {
            "action": "zone_clean",
            "dry_run": dry_run,
        }
        if zone_name:
            body["zone_name"] = zone_name
        if rectangles is not None:
            body["rectangles"] = rectangles
        if map_generation:
            body["map_generation"] = map_generation
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return await self._request(
            "POST",
            f"{API_PREFIX}/vacuums/{VACUUM_ID}/jobs",
            payload=body,
            idempotency_key=idempotency_key,
        )
