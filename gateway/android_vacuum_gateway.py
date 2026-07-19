#!/usr/bin/env python3
"""Narrow, deterministic gateway from Home Assistant to Xiaomi Home UI workflows.

This service intentionally does not accept free-form prompts. It is the only
component that may call Android MCP for routine vacuum control.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request


LOGGER = logging.getLogger("android_vacuum_gateway")
API_VERSION = "v1"
MCP_PROTOCOL_VERSION = "2025-03-26"
VACUUM_ID = "xiaomi-robot-vacuum-x20-plus"
XIAOMI_PACKAGE = "com.xiaomi.smarthome"
SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 2400
MAP_TOP = 301
MAP_BOTTOM = 1696
LAUNCHER_PACKAGES = frozenset(
    {"com.mi.android.globalminusscreen", "com.miui.home", "com.microsoft.launcher"}
)
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{16,128}\Z")
MAX_AUDIT_BYTES = 1_000_000
ADB_PATH = "/usr/bin/adb"
XIAOMI_VACUUM_NOTIFICATION_MARKER = "Xiaomi Robot Vacuum X20+"
NOTIFICATION_POLL_SECONDS = 2.0


class GatewayError(RuntimeError):
    """Base error returned to API clients."""


class PhoneBusy(GatewayError):
    """The dedicated phone is foregrounded by an unrelated application."""


class WorkflowAssertion(GatewayError):
    """A known Android workflow no longer matches the app UI."""


@dataclass(frozen=True)
class GatewayConfig:
    bind: str
    port: int
    token: str
    allowed_ips: frozenset[str]
    mcp_url: str
    state_file: Path
    audit_file: Path
    zones: dict[str, dict[str, int]]

    @classmethod
    def load(cls, path: Path) -> "GatewayConfig":
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("Gateway configuration must be a JSON object")
        token = raw.get("token", "")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Gateway token is missing or too short")
        bind = raw.get("bind", "127.0.0.1")
        if not isinstance(bind, str):
            raise ValueError("Gateway bind address must be a string")
        try:
            bind_ip = ipaddress.ip_address(bind)
        except ValueError as exc:
            raise ValueError("Gateway bind address must be an IP address") from exc
        try:
            port = int(raw.get("port", 8091))
        except (TypeError, ValueError) as exc:
            raise ValueError("Gateway port must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Gateway port must be in range 1..65535")
        allowed_raw = raw.get("allowed_ips", [])
        if not isinstance(allowed_raw, list) or not all(isinstance(item, str) for item in allowed_raw):
            raise ValueError("allowed_ips must be a list of IP addresses")
        try:
            allowed_ips = frozenset(str(ipaddress.ip_address(item)) for item in allowed_raw)
        except ValueError as exc:
            raise ValueError("allowed_ips contains an invalid IP address") from exc
        if not bind_ip.is_loopback and not allowed_ips:
            raise ValueError("A LAN-bound gateway requires a non-empty allowed_ips list")
        zones = raw.get("zones", {})
        if not isinstance(zones, dict) or not zones:
            raise ValueError("At least one named zone is required")
        validated_zones: dict[str, dict[str, int]] = {}
        for name, rect in zones.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", name):
                raise ValueError("Named zones must use lowercase letters, digits, and underscores")
            if not isinstance(rect, dict):
                raise ValueError(f"Named zone {name} must be an object")
            try:
                parsed = {key: int(rect[key]) for key in ("x1", "y1", "x2", "y2")}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Named zone {name} has invalid coordinates") from exc
            if not (
                0 <= parsed["x1"] < parsed["x2"] <= SCREEN_WIDTH
                and MAP_TOP <= parsed["y1"] < parsed["y2"] <= MAP_BOTTOM
            ):
                raise ValueError(f"Named zone {name} lies outside the safe map area")
            validated_zones[name] = parsed
        mcp_url = raw.get("mcp_url", "http://127.0.0.1:18080/mcp")
        if not isinstance(mcp_url, str) or not mcp_url.startswith("http://127.0.0.1:"):
            raise ValueError("mcp_url must remain a local HTTP Android MCP endpoint")
        return cls(
            bind=bind,
            port=port,
            token=token,
            allowed_ips=allowed_ips,
            mcp_url=mcp_url,
            state_file=Path(raw.get("state_file", "/var/lib/android-vacuum-gateway/state.json")),
            audit_file=Path(raw.get("audit_file", "/var/lib/android-vacuum-gateway/audit.jsonl")),
            zones=validated_zones,
        )


class AndroidMcpClient:
    """Tiny Streamable HTTP MCP client using only the Python standard library."""

    def __init__(self, url: str) -> None:
        self._url = url

    @staticmethod
    def _decode_response(raw: bytes, content_type: str) -> dict[str, Any] | None:
        """Decode either of the response encodings permitted by Streamable HTTP MCP."""
        text = raw.decode(errors="replace")
        try:
            payload = json.loads(text)
            if payload is None:
                # Android Remote Control MCP acknowledges notifications with
                # the valid JSON body `null` rather than an empty 202.
                return None
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

        if "text/event-stream" in content_type.lower():
            messages: list[dict[str, Any]] = []
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    messages.append(payload)
            if messages:
                # Progress messages can precede the JSON-RPC result.  The
                # final event is the response to this short-lived request.
                return messages[-1]
        raise GatewayError("Android MCP returned an invalid JSON-RPC response")

    def _post(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any] | None, Any]:
        req = request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=25) as response:
                raw = response.read()
                if not raw:
                    if allow_empty:
                        return None, response.headers
                    raise GatewayError("Android MCP returned an unexpected empty response")
                decoded = self._decode_response(raw, response.headers.get("Content-Type", ""))
                if decoded is None and not allow_empty:
                    raise GatewayError("Android MCP returned an unexpected null response")
                return decoded, response.headers
        except error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise GatewayError(f"Android MCP HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise GatewayError(f"Android MCP unavailable: {exc.reason}") from exc

    def _new_session(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        response, response_headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "android-vacuum-gateway", "version": "0.1.0"},
                },
            },
            headers,
        )
        if not response:
            raise GatewayError("Android MCP initialize returned no response")
        if "error" in response:
            raise GatewayError(f"Android MCP initialize failed: {response['error']}")
        session_id = response_headers.get("mcp-session-id")
        if not session_id:
            raise GatewayError("Android MCP did not issue a session id")
        headers["mcp-session-id"] = session_id
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers,
            allow_empty=True,
        )
        return headers

    def _close_session(self, headers: dict[str, str]) -> None:
        """Release the short-lived MCP session without masking tool results."""
        req = request.Request(self._url, headers=headers, method="DELETE")
        try:
            with request.urlopen(req, timeout=5):
                pass
        except (error.HTTPError, error.URLError, TimeoutError):
            LOGGER.warning("Could not close Android MCP session", exc_info=True)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        headers = self._new_session()
        try:
            response, _ = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
                headers,
            )
            if not response:
                raise GatewayError(f"Android MCP {name} returned no response")
            if "error" in response:
                raise GatewayError(f"Android MCP {name} failed: {response['error']}")
            result = response.get("result", {})
            if result.get("isError"):
                text = " ".join(block.get("text", "") for block in result.get("content", []))
                raise GatewayError(f"Android MCP {name} failed: {text[:1000]}")
            return result.get("content", [])
        finally:
            self._close_session(headers)

    def find_nodes(self, *, by: str, value: str, exact_match: bool = True) -> list[dict[str, Any]]:
        """Return only the structured node payload from the MCP wrapper text."""
        content = self.tool(
            "android_find_nodes",
            {"by": by, "value": value, "exact_match": exact_match},
        )
        for block in content:
            if block.get("type") != "text":
                continue
            for line in reversed(block.get("text", "").splitlines()):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                nodes = payload.get("nodes") if isinstance(payload, dict) else None
                if isinstance(nodes, list):
                    return [node for node in nodes if isinstance(node, dict)]
        raise GatewayError("Android MCP node search returned no structured result")

    def screen(self, screenshot: bool = False) -> dict[str, Any]:
        content = self.tool("android_get_screen_state", {"include_screenshot": screenshot})
        text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
        image = next((block for block in content if block.get("type") == "image"), None)
        return {"text": text, "image": image}


class StateStore:
    """Small bounded durable state/audit store; no screenshots or secrets are stored."""

    def __init__(self, state_file: Path, audit_file: Path) -> None:
        self._state_file = state_file
        self._audit_file = audit_file
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return {"last_state": {}, "last_detail_state": {}, "jobs": {}, "idempotency": {}, "map": {}}
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Ignoring unreadable state file", exc_info=True)
            return {"last_state": {}, "last_detail_state": {}, "jobs": {}, "idempotency": {}, "map": {}}

    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self._state, sort_keys=True, indent=2) + "\n")
        os.replace(temp, self._state_file)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value
            self._save()

    def remember_job(self, job: dict[str, Any], idempotency_key: str | None = None) -> None:
        with self._lock:
            jobs = self._state.setdefault("jobs", {})
            jobs[job["job_id"]] = dict(job)
            self._state["jobs"] = dict(list(jobs.items())[-100:])
            if idempotency_key:
                keys = self._state.setdefault("idempotency", {})
                keys[idempotency_key] = job["job_id"]
                self._state["idempotency"] = dict(list(keys.items())[-100:])
            self._save()
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            if self._audit_file.exists() and self._audit_file.stat().st_size >= MAX_AUDIT_BYTES:
                archive = self._audit_file.with_name(f"{self._audit_file.name}.1")
                os.replace(self._audit_file, archive)
            with self._audit_file.open("a") as audit:
                audit.write(json.dumps(job, sort_keys=True) + "\n")

    def idempotent_job(self, key: str, request_fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            job_id = self._state.get("idempotency", {}).get(key)
            job = self._state.get("jobs", {}).get(job_id) if job_id else None
            if job is None:
                return None
            if not hmac.compare_digest(str(job.get("request_fingerprint", "")), request_fingerprint):
                raise GatewayError("Idempotency key was already used for a different request")
            return dict(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._state.get("jobs", {}).get(job_id)
            return dict(job) if job else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notification_extra(block: str, key: str) -> str | None:
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(key)}=(?:String \()?([^\n)]*)(?:\))?\s*$",
        block,
        re.MULTILINE,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return value[:500] if value and value.lower() != "null" else None


def classify_xiaomi_notification(title: str) -> str:
    """Map notification wording to a stable, automation-friendly category."""
    normalized = re.sub(r"\s+", " ", title.strip().lower())
    if "cleanup completed" in normalized or "cleaning completed" in normalized:
        return "cleanup_completed"
    if "return" in normalized and "station" in normalized:
        return "returning_to_station"
    if "pause" in normalized:
        return "paused"
    if "clean" in normalized and any(word in normalized for word in ("start", "began", "commenced")):
        return "cleaning_started"
    attention_terms = (
        "suspended",
        "stuck",
        "trapped",
        "blocked",
        "error",
        "fault",
        "low battery",
        "water tank",
        "dust compartment",
        "brush",
    )
    if any(term in normalized for term in attention_terms):
        return "needs_attention"
    return "status"


def parse_xiaomi_vacuum_notifications(dump: str) -> list[dict[str, Any]]:
    """Extract only X20+ Xiaomi Home notifications from an Android dump."""
    events: list[dict[str, Any]] = []
    for raw in dump.split("NotificationRecord(")[1:]:
        block = "NotificationRecord(" + raw
        header = block.splitlines()[0]
        if f"pkg={XIAOMI_PACKAGE}" not in header:
            continue
        title = _notification_extra(block, "android.title")
        text = _notification_extra(block, "android.bigText") or _notification_extra(
            block, "android.text"
        )
        if not title or not text or XIAOMI_VACUUM_NOTIFICATION_MARKER not in text:
            continue
        when_match = re.search(r"(?:^|\n)\s*when=(\d{10,16})\s*$", block, re.MULTILINE)
        if not when_match:
            continue
        event_millis = int(when_match.group(1))
        try:
            event_at = datetime.fromtimestamp(event_millis / 1000, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            continue
        id_match = re.search(r"\bid=([^ ]+)", header)
        tag_match = re.search(r"\btag=([^ ]+)", header)
        fingerprint = hashlib.sha256(
            "\0".join(
                (
                    str(event_millis),
                    id_match.group(1) if id_match else "",
                    tag_match.group(1) if tag_match else "",
                    title,
                    text,
                )
            ).encode()
        ).hexdigest()
        events.append(
            {
                "source": "android_notification",
                "package": XIAOMI_PACKAGE,
                "vacuum_id": VACUUM_ID,
                "title": title[:200],
                "text": text[:500],
                "category": classify_xiaomi_notification(title),
                "event_at": event_at,
                "event_millis": event_millis,
                "fingerprint": fingerprint,
            }
        )
    return sorted(events, key=lambda item: (item["event_millis"], item["fingerprint"]))


class XiaomiNotificationMonitor:
    """Poll Android notifications without exposing unrelated phone content."""

    def __init__(self, store: StateStore, poll_seconds: float = NOTIFICATION_POLL_SECONDS) -> None:
        self._store = store
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="xiaomi-vacuum-notifications",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "available": False,
            "sequence": int(store.get("notification_sequence", 0) or 0),
            "latest_event": store.get("latest_notification"),
        }

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def _read_dump(self) -> str:
        completed = subprocess.run(
            [ADB_PATH, "shell", "dumpsys", "notification", "--noredact"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return completed.stdout.decode("utf-8", errors="replace")

    def _poll_once(self) -> None:
        events = parse_xiaomi_vacuum_notifications(self._read_dump())
        seen = list(self._store.get("notification_seen", []))[-100:]
        seen_set = set(str(value) for value in seen)
        if not seen:
            seen = [str(event["fingerprint"]) for event in events][-100:]
            self._store.set("notification_seen", seen)
            with self._lock:
                self._snapshot["available"] = True
                self._snapshot["checked_at"] = now_iso()
            return

        unseen = [event for event in events if event["fingerprint"] not in seen_set]
        latest = unseen[-1] if unseen else None
        if latest:
            sequence = int(self._store.get("notification_sequence", 0) or 0) + 1
            public_event = {key: value for key, value in latest.items() if key != "event_millis"}
            self._store.set("notification_sequence", sequence)
            self._store.set("latest_notification", public_event)
            seen.extend(str(event["fingerprint"]) for event in unseen)
            self._store.set("notification_seen", seen[-100:])
        else:
            sequence = int(self._store.get("notification_sequence", 0) or 0)
            public_event = self._store.get("latest_notification")
        with self._lock:
            self._snapshot = {
                "available": True,
                "sequence": sequence,
                "latest_event": public_event,
                "checked_at": now_iso(),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except (OSError, subprocess.SubprocessError):
                with self._lock:
                    self._snapshot["available"] = False
                    self._snapshot["checked_at"] = now_iso()
            self._stop.wait(self._poll_seconds)


def validate_png_screenshot(image: bytes) -> bytes:
    """Accept only a bounded raw PNG with the dedicated phone's geometry."""
    if len(image) < 24 or len(image) > 20 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GatewayError("ADB screenshot was not a bounded PNG")
    width, height = struct.unpack(">II", image[16:24])
    if (width, height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
        raise WorkflowAssertion("ADB screenshot geometry does not match the approved phone")
    return image


def capture_adb_screenshot() -> bytes:
    """Capture a clean read-only screenshot without MCP's debug annotations."""
    try:
        result = subprocess.run(
            [ADB_PATH, "exec-out", "screencap", "-p"],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GatewayError("ADB screenshot capture failed") from exc
    if result.returncode != 0:
        raise GatewayError("ADB screenshot capture failed")
    return validate_png_screenshot(result.stdout)


def focused_package(screen_text: str) -> str | None:
    # Android Remote Control's compact UI dump has changed formatting between
    # versions: the package and focused flag may be on separate lines.  Keep
    # this deliberately narrow, but allow a small gap between the two fields.
    match = re.search(r"pkg:([^\s]+)[\s\S]{0,1000}?focused:true", screen_text)
    return match.group(1) if match else None


def app_hierarchy_text(screen_text: str) -> str:
    """Return only the accessibility tree below Android's `hierarchy:` marker.

    Status-bar notifications are intentionally excluded: a Google/Xiaomi
    account sign-in error must never be mistaken for a vacuum fault.
    """
    marker, separator, hierarchy = screen_text.partition("hierarchy:")
    return hierarchy if separator else ""


def device_ui_text(screen_text: str) -> str:
    """Return only node lines below the Android system status/notification bar."""
    visible: list[str] = []
    for line in app_hierarchy_text(screen_text).splitlines():
        bounds = re.search(r"(\d+),(\d+),(\d+),(\d+)\s", line)
        if bounds and int(bounds.group(2)) >= 125:
            visible.append(line)
    return "\n".join(visible)


def is_x20_detail_screen(screen_text: str) -> bool:
    """Distinguish the control screen from a same-named home device card."""
    hierarchy = device_ui_text(screen_text)
    if "Xiaomi Robot Vacuum X20+" not in hierarchy:
        return False
    # Xiaomi's nested settings sheet leaves an off-screen copy of the map and
    # bottom action in the accessibility tree. Only accept an action whose
    # node is actually flagged on-screen.
    return any(
        any(f"TextView\t{action}\t" in line and "\ton" in line for line in hierarchy.splitlines())
        for action in ("Start cleanup", "Pause cleanup", "Continue cleanup", "Back to station")
    )


def is_x20_device_card_node(node: dict[str, Any]) -> bool:
    """Reject the same-labelled plugin title at the top of nested pages."""
    bounds = node.get("bounds")
    if not isinstance(bounds, dict):
        return False
    try:
        top = int(bounds["top"])
        bottom = int(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return False
    return node.get("visible") and node.get("enabled") and 300 <= top < bottom <= SCREEN_HEIGHT


def is_station_status_popup(screen_text: str) -> bool:
    """Recognise only Xiaomi's non-command station status pop-up."""
    hierarchy = device_ui_text(screen_text)
    return all(label in hierarchy for label in ("Station", "No tasks yet", "Hide pop-up window"))


def attention_reason(screen_text: str) -> str | None:
    """Return a conservative, stable reason from Xiaomi's own UI only."""
    lowered = device_ui_text(screen_text).lower()
    for term in ("device is offline", "stuck", "blocked", "cannot continue", "error"):
        if term in lowered:
            return term.replace(" ", "_")
    return None


def parse_activity(screen_text: str) -> str:
    lowered = device_ui_text(screen_text).lower()
    if attention_reason(screen_text):
        return "error"
    # Do not search for a generic "cleaning" word: this UI permanently shows
    # the label "Cleaning area" even when the robot is docked.
    if "pause cleanup" in lowered:
        return "cleaning"
    if "paused" in lowered or "continue cleanup" in lowered:
        return "paused"
    if "returning" in lowered or "back to station" in lowered:
        return "returning"
    if "charging" in lowered or "docked" in lowered:
        return "docked"
    return "idle"


def canonical_map_fingerprint(screen_text: str) -> str:
    """Fail closed unless the known X20 map viewport is visibly canonical.

    Coordinates are only meaningful for the captured Xiaomi Home layout.  This
    guards against a map pan, zoom, or UI redesign between preview and start.
    """
    hierarchy = app_hierarchy_text(screen_text)
    if "screen:1080x2400" not in screen_text:
        raise WorkflowAssertion("Unexpected phone screen geometry")
    if "Xiaomi Robot Vacuum X20+" not in hierarchy:
        raise WorkflowAssertion("X20+ detail screen is not visible")

    expected = {
        "Room12": (0, 301, 144, 350),
        "Start cleanup": (734, 1768, 1032, 1826),
    }
    observed: dict[str, str] = {}
    for label, bounds in expected.items():
        match = re.search(
            rf"TextView\s+{re.escape(label)}\s+.*?(\d+,\d+,\d+,\d+)\s",
            hierarchy,
        )
        if not match:
            raise WorkflowAssertion(f"Map anchor {label!r} no longer matches the approved viewport")
        coordinates = tuple(int(value) for value in match.group(1).split(","))
        # Xiaomi's two RN activity variants can round the bottom action label
        # by two pixels. Permit only that tiny rendering variance; the exact
        # observed coordinates are still fingerprinted for the later start.
        if any(abs(actual - approved) > 4 for actual, approved in zip(coordinates, bounds)):
            raise WorkflowAssertion(f"Map anchor {label!r} no longer matches the approved viewport")
        observed[label] = match.group(1)
    if not re.search(r"ViewGroup\s+-\s+-\s+-\s+0,301,1080,2270\s", hierarchy):
        raise WorkflowAssertion("Approved X20 map canvas is not visible")
    if "943,560,998,693" not in hierarchy:
        raise WorkflowAssertion("Approved zone-tool position is not visible")
    return hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()


def normalize_rectangle(rect: dict[str, Any]) -> dict[str, int]:
    if not isinstance(rect, dict):
        raise GatewayError("Each rectangle must be a JSON object")
    try:
        values = {key: rect[key] for key in ("x1", "y1", "x2", "y2")}
    except KeyError as exc:
        raise GatewayError("Rectangle requires integer x1, y1, x2, y2 values") from exc
    if any(type(value) is not int for value in values.values()):
        raise GatewayError("Rectangle requires integer x1, y1, x2, y2 values")
    normalized = values
    if not (0 <= normalized["x1"] < normalized["x2"] <= 10000):
        raise GatewayError("Rectangle x coordinates must be ordered in range 0..10000")
    if not (0 <= normalized["y1"] < normalized["y2"] <= 10000):
        raise GatewayError("Rectangle y coordinates must be ordered in range 0..10000")
    if (normalized["x2"] - normalized["x1"]) * (normalized["y2"] - normalized["y1"]) < 40000:
        raise GatewayError("Rectangle is too small to be safe")
    return normalized


def pixels_to_normalized(rect: dict[str, int]) -> dict[str, int]:
    return {
        "x1": round(rect["x1"] * 10000 / SCREEN_WIDTH),
        "y1": round(rect["y1"] * 10000 / SCREEN_HEIGHT),
        "x2": round(rect["x2"] * 10000 / SCREEN_WIDTH),
        "y2": round(rect["y2"] * 10000 / SCREEN_HEIGHT),
    }


def normalized_to_pixels(rect: dict[str, int]) -> dict[str, int]:
    return {
        "x1": round(rect["x1"] * SCREEN_WIDTH / 10000),
        "y1": round(rect["y1"] * SCREEN_HEIGHT / 10000),
        "x2": round(rect["x2"] * SCREEN_WIDTH / 10000),
        "y2": round(rect["y2"] * SCREEN_HEIGHT / 10000),
    }


class XiaomiVacuumWorkflow:
    """Fixed X20+ workflows guarded by app and UI assertions."""

    def __init__(self, config: GatewayConfig, mcp: AndroidMcpClient, store: StateStore) -> None:
        self._config = config
        self._mcp = mcp
        self._store = store
        self._job_lock = threading.Lock()

    def _state_from_screen(self, screen: dict[str, Any]) -> dict[str, Any]:
        text = screen["text"]
        package = focused_package(text)
        hierarchy = app_hierarchy_text(text)
        detail_visible = package == XIAOMI_PACKAGE and "Xiaomi Robot Vacuum X20+" in device_ui_text(text)
        reason = attention_reason(text) if detail_visible else None
        state = {
            "vacuum_id": VACUUM_ID,
            "activity": parse_activity(text) if detail_visible else "unknown",
            "phone_busy": package not in ({XIAOMI_PACKAGE} | LAUNCHER_PACKAGES),
            "foreground_package": package,
            "workflow_version": "xiaomi-x20-zone-v1",
            "last_seen": now_iso(),
            "needs_attention": reason is not None,
        }
        if package == XIAOMI_PACKAGE and not detail_visible:
            state["status_reason"] = "xiaomi_detail_screen_not_visible"
        elif package is None:
            state["status_reason"] = "foreground_window_unknown"
        if reason:
            state["attention_reason"] = reason
            previous = self._store.get("last_detail_state", {})
            state["attention_since"] = (
                previous.get("attention_since")
                if previous.get("attention_reason") == reason
                else state["last_seen"]
            )
        area_match = re.search(r"\nnode_[^\n]+\s+TextView\s+(\d+(?:\.\d+)?)\s+.*?\n.*?TextView\s+m²", hierarchy)
        if area_match:
            state["cleaning_area_m2"] = float(area_match.group(1))
        return state

    def _recent_active_detail(self) -> dict[str, Any] | None:
        """Return a bounded last observed active state, if one exists."""
        previous = self._store.get("last_detail_state", {})
        if previous.get("activity") not in {"cleaning", "paused", "returning"}:
            return None
        try:
            seen = datetime.fromisoformat(str(previous.get("last_seen")))
        except (TypeError, ValueError):
            return None
        if (datetime.now(timezone.utc) - seen).total_seconds() > 4 * 60 * 60:
            return None
        return previous

    def _tap_found_node(self, node: dict[str, Any]) -> None:
        """Tap the verified centre of a freshly found visible node."""
        bounds = node.get("bounds")
        if not isinstance(bounds, dict):
            raise WorkflowAssertion("Android node has no usable bounds")
        try:
            left, top, right, bottom = (
                int(bounds[key]) for key in ("left", "top", "right", "bottom")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowAssertion("Android node bounds are invalid") from exc
        if not (0 <= left < right <= SCREEN_WIDTH and 0 <= top < bottom <= SCREEN_HEIGHT):
            raise WorkflowAssertion("Android node lies outside the expected screen")
        self._mcp.tool(
            "android_tap",
            {"x": (left + right) // 2, "y": (top + bottom) // 2},
        )

    def _should_resume_active_poll(self, package: str | None) -> bool:
        """Allow a low-priority status check only from the launcher.

        This is the one background workflow that may foreground Xiaomi Home:
        it is used only while a recently observed clean is active, never while
        another application owns the dedicated phone.
        """
        if package not in LAUNCHER_PACKAGES:
            return False
        return self._recent_active_detail() is not None

    def get_state(self) -> dict[str, Any]:
        # Every Android MCP call, including passive screen reads, must share a
        # single mutex.  Otherwise a periodic HA poll could consume a screen
        # midway through a zone workflow and make its assertions meaningless.
        if not self._job_lock.acquire(blocking=False):
            last = self._store.get("last_state", {})
            return {**last, "phone_busy": True, "workflow_busy": True}
        try:
            screen = self._mcp.screen()
            if self._should_resume_active_poll(focused_package(screen["text"])):
                try:
                    screen = self._require_xiaomi_screen(allow_open=True)
                except GatewayError:
                    # The next passive poll may retry only while the prior
                    # active observation remains fresh.  Do not manufacture a
                    # current vacuum state when the detail screen is absent.
                    state = self._state_from_screen(screen)
                    state["status_reason"] = "active_status_refresh_refused"
                else:
                    state = self._state_from_screen(screen)
                    state["status_poll_reopened_xiaomi"] = True
            else:
                state = self._state_from_screen(screen)
            if state.get("activity") == "unknown":
                previous = self._recent_active_detail()
                if previous:
                    state["last_known_activity"] = previous["activity"]
            self._store.set("last_state", state)
            if state.get("activity") != "unknown":
                self._store.set("last_detail_state", state)
            return state
        finally:
            self._job_lock.release()

    def _require_xiaomi_screen(self, allow_open: bool) -> dict[str, Any]:
        screen = self._mcp.screen()
        package = focused_package(screen["text"])
        if package == XIAOMI_PACKAGE and is_station_status_popup(screen["text"]):
            dismiss = [
                node
                for node in self._mcp.find_nodes(
                    by="text", value="Hide pop-up window", exact_match=True
                )
                if node.get("visible") and node.get("enabled")
            ]
            if len(dismiss) != 1:
                raise WorkflowAssertion("Could not identify one station status dismiss control")
            self._tap_found_node(dismiss[0])
            time.sleep(0.5)
            screen = self._mcp.screen()
            package = focused_package(screen["text"])
        if package != XIAOMI_PACKAGE:
            if not allow_open or package not in LAUNCHER_PACKAGES:
                raise PhoneBusy(f"Phone is currently in {package}; workflow will not foreground Xiaomi Home")
            # MIUI reports its Recent Apps activity as the launcher package.
            # Launching an app while Recents owns focus can update the card but
            # leave the Recents overlay in front. HOME is harmless on the real
            # launcher and reliably dismisses that overlay before launch.
            self._mcp.tool("android_press_key", {"key": "HOME"})
            time.sleep(0.3)
            self._mcp.tool("android_open_app", {"package_id": XIAOMI_PACKAGE})
            time.sleep(1.0)
            screen = self._mcp.screen()
            package = focused_package(screen["text"])
            if package in LAUNCHER_PACKAGES:
                icons = [
                    node
                    for node in self._mcp.find_nodes(
                        by="text", value="Xiaomi Home", exact_match=True
                    )
                    if node.get("visible") and node.get("enabled")
                ]
                if len(icons) != 1:
                    raise PhoneBusy("Could not identify one Xiaomi Home launcher icon")
                self._tap_found_node(icons[0])
                time.sleep(1.0)
                screen = self._mcp.screen()
        if focused_package(screen["text"]) != XIAOMI_PACKAGE:
            raise PhoneBusy("Xiaomi Home did not become the focused foreground app")
        # Xiaomi Home remembers nested plugin pages such as "Scheduled
        # cleanup" across launches and HA restarts. Walk back only inside the
        # already-focused Xiaomi app, or tap the exact device card if the home
        # screen is reached. Every step is re-observed and bounded.
        for attempt in range(5):
            if is_x20_detail_screen(screen["text"]):
                break
            devices = [
                node
                for node in self._mcp.find_nodes(
                    by="text", value="Xiaomi Robot Vacuum X20+", exact_match=True
                )
                if is_x20_device_card_node(node)
            ]
            if len(devices) == 1:
                self._tap_found_node(devices[0])
                time.sleep(3.0)
            elif len(devices) > 1:
                raise WorkflowAssertion("Could not identify one X20+ device card")
            elif attempt < 4:
                back_buttons = [
                    node
                    for node in self._mcp.find_nodes(
                        by="content_desc",
                        value="Back, Double tap to activate",
                        exact_match=True,
                    )
                    if node.get("visible") and node.get("enabled")
                ]
                if len(back_buttons) != 1:
                    raise WorkflowAssertion("Could not identify one Xiaomi subpage back button")
                self._tap_found_node(back_buttons[0])
                time.sleep(0.5)
            else:
                raise WorkflowAssertion("Could not return to the X20+ control screen")
            screen = self._mcp.screen()
        if (
            focused_package(screen["text"]) != XIAOMI_PACKAGE
            or not is_x20_detail_screen(screen["text"])
        ):
            raise WorkflowAssertion("Xiaomi Home is open but the X20+ control screen is not visible")
        if "screen:1080x2400" not in screen["text"]:
            raise WorkflowAssertion("Unexpected phone screen geometry")
        return screen

    def refresh_map(self) -> dict[str, Any]:
        if not self._job_lock.acquire(blocking=False):
            raise PhoneBusy("A vacuum command is in progress")
        try:
            preview = self._require_xiaomi_screen(allow_open=True)
            if reason := attention_reason(preview["text"]):
                raise WorkflowAssertion(f"Vacuum needs attention: {reason}")
            try:
                layout_fingerprint = canonical_map_fingerprint(preview["text"])
            except WorkflowAssertion as exc:
                # A current screenshot is still useful for the HA map/plotter,
                # but its coordinates must never become motion-authoritative.
                layout_fingerprint = None
                zone_status_reason = str(exc)
            else:
                zone_status_reason = None
            image = capture_adb_screenshot()
            image_base64 = base64.b64encode(image).decode()
            generation = str(uuid.uuid4())
            map_info = {
                "generation": generation,
                "created_at": now_iso(),
                "expires_at": time.time() + 600,
                "sha256": hashlib.sha256(image).hexdigest(),
                "layout_fingerprint": layout_fingerprint,
                "zone_ready": layout_fingerprint is not None,
                "zone_status_reason": zone_status_reason,
            }
            self._store.set("map", map_info)
            return {
                "vacuum_id": VACUUM_ID,
                "map_generation": generation,
                "screen_width": SCREEN_WIDTH,
                "screen_height": SCREEN_HEIGHT,
                "mime_type": "image/png",
                "image_base64": image_base64,
                "zone_ready": layout_fingerprint is not None,
                "zone_status_reason": zone_status_reason,
                "known_zones": {
                    name: pixels_to_normalized(rect) for name, rect in self._config.zones.items()
                },
            }
        finally:
            self._job_lock.release()

    def _assert_fresh_map(self, map_generation: str | None) -> None:
        if not map_generation:
            raise GatewayError("map_generation is required for every zone job")
        current = self._store.get("map", {})
        if current.get("generation") != map_generation or current.get("expires_at", 0) < time.time():
            raise WorkflowAssertion("Map preview has expired; refresh the map before starting")
        if not current.get("layout_fingerprint"):
            raise WorkflowAssertion(
                "Map preview is view-only; restore the approved viewport before starting"
            )

    def _assert_preview_still_matches(self, screen_text: str) -> None:
        current = self._store.get("map", {})
        expected = current.get("layout_fingerprint")
        observed = canonical_map_fingerprint(screen_text)
        if not expected or not hmac.compare_digest(str(expected), observed):
            raise WorkflowAssertion("Map viewport changed since the preview; refresh before starting")

    def _tap(self, x: int, y: int) -> None:
        self._mcp.tool("android_tap", {"x": x, "y": y})

    def _zone_mode(self) -> None:
        self._tap(970, 625)
        time.sleep(0.5)
        text = self._mcp.screen()["text"]
        if "Robot vacuum will clean up within the zoned area" not in app_hierarchy_text(text):
            raise WorkflowAssertion("Zone tool did not activate; refusing to draw")

    def _start_rectangle(self, rect: dict[str, int]) -> None:
        self._mcp.tool(
            "android_swipe",
            {"x1": rect["x1"], "y1": rect["y1"], "x2": rect["x2"], "y2": rect["y2"], "duration": 700},
        )
        time.sleep(0.5)

    @staticmethod
    def _require_idempotency_key(key: str | None) -> str:
        if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise GatewayError("A 16-128 character idempotency key is required for every zone job")
        return key

    def zone_clean(
        self,
        *,
        zone_name: str | None,
        rectangles: list[dict[str, Any]] | None,
        map_generation: str | None,
        dry_run: bool,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if bool(zone_name) == bool(rectangles):
            raise GatewayError("Specify exactly one of zone_name or rectangles")
        if not isinstance(dry_run, bool):
            raise GatewayError("dry_run must be a JSON boolean")
        key = self._require_idempotency_key(idempotency_key)
        self._assert_fresh_map(map_generation)

        if zone_name:
            if not isinstance(zone_name, str) or zone_name not in self._config.zones:
                raise GatewayError(f"Unknown named zone: {zone_name}")
            pixel_rectangles = [self._config.zones[zone_name]]
            normalized_rectangles = [pixels_to_normalized(pixel_rectangles[0])]
        else:
            if not isinstance(rectangles, list):
                raise GatewayError("rectangles must be a JSON array")
            normalized_rectangles = [normalize_rectangle(item) for item in rectangles]
            if not normalized_rectangles or len(normalized_rectangles) > 2:
                raise GatewayError("Provide one or two rectangles")
            pixel_rectangles = [normalized_to_pixels(item) for item in normalized_rectangles]
            for rect in pixel_rectangles:
                if not (
                    0 <= rect["x1"] < rect["x2"] <= SCREEN_WIDTH
                    and MAP_TOP <= rect["y1"] < rect["y2"] <= MAP_BOTTOM
                ):
                    raise GatewayError("Custom rectangle lies outside the safe map area")

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "action": "zone_clean",
                    "zone_name": zone_name,
                    "rectangles": normalized_rectangles,
                    "map_generation": map_generation,
                    "dry_run": dry_run,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        previous = self._store.idempotent_job(key, fingerprint)
        if previous:
            return previous
        if not self._job_lock.acquire(blocking=False):
            raise PhoneBusy("Another Android workflow is already using the phone")
        job = {
            "job_id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "action": "zone_clean",
            "status": "accepted",
            "zone_name": zone_name,
            "rectangles": normalized_rectangles,
            "map_generation": map_generation,
            "request_fingerprint": fingerprint,
            "workflow_version": "xiaomi-x20-zone-v1",
        }
        tap_started = False
        try:
            # Persist the accepted request before any Android interaction.  A
            # client retry with this key can then never issue a second cleanup.
            self._store.remember_job(job, key)
            screen = self._require_xiaomi_screen(allow_open=False)
            self._assert_preview_still_matches(screen["text"])
            state = self._state_from_screen(screen)
            if "Start cleanup" not in app_hierarchy_text(screen["text"]) or state["activity"] not in {"idle", "docked"}:
                raise WorkflowAssertion("Vacuum is not ready for a new cleanup job")
            if dry_run:
                job.update({"status": "dry_run", "completed_at": now_iso()})
                self._store.remember_job(job, key)
                return job

            job["status"] = "running"
            self._store.remember_job(job, key)
            tap_started = True
            self._zone_mode()
            for rect in pixel_rectangles:
                self._start_rectangle(rect)
            before_start = self._mcp.screen()["text"]
            if "Robot vacuum will clean up within the zoned area" not in app_hierarchy_text(before_start):
                raise WorkflowAssertion("Zone mode disappeared before start")
            self._tap(805, 1795)
            time.sleep(2.0)
            final_state = self._state_from_screen(self._mcp.screen())
            if final_state["activity"] != "cleaning":
                raise WorkflowAssertion(f"Vacuum did not accept the zone job (state {final_state['activity']})")
            job.update({"status": final_state["activity"], "completed_at": now_iso(), "state": final_state})
            self._store.set("last_state", final_state)
            self._store.set("last_detail_state", final_state)
            self._store.remember_job(job, key)
            return job
        except Exception as exc:
            job.update(
                {
                    "status": "outcome_unknown" if tap_started else "rejected",
                    "completed_at": now_iso(),
                    "error": str(exc),
                }
            )
            self._store.remember_job(job, key)
            raise
        finally:
            self._job_lock.release()


class ApiHandler(BaseHTTPRequestHandler):
    server: "GatewayServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        config = self.server.config
        if config.allowed_ips and self.client_address[0] not in config.allowed_ips:
            return False
        value = self.headers.get("Authorization", "")
        return value.startswith("Bearer ") and hmac.compare_digest(value[7:], config.token)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise GatewayError("Invalid Content-Length") from exc
        if not 0 <= length <= 100_000:
            raise GatewayError("Request body is too large")
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise GatewayError("Request body must be a JSON object")
        return body

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"ok": True, "version": "0.1.0"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            if self.path == f"/{API_VERSION}/vacuums/{VACUUM_ID}/state":
                self._json(HTTPStatus.OK, self.server.workflow.get_state())
            elif self.path == f"/{API_VERSION}/vacuums/{VACUUM_ID}/notifications":
                self._json(HTTPStatus.OK, self.server.notification_monitor.snapshot())
            elif self.path == f"/{API_VERSION}/vacuums/{VACUUM_ID}/map":
                self._json(HTTPStatus.OK, self.server.workflow.refresh_map())
            elif self.path.startswith(f"/{API_VERSION}/jobs/"):
                job = self.server.store.get_job(self.path.rsplit("/", 1)[-1])
                self._json(HTTPStatus.OK if job else HTTPStatus.NOT_FOUND, job or {"error": "not_found"})
            elif self.path == f"/{API_VERSION}/vacuums/{VACUUM_ID}/zones":
                self._json(HTTPStatus.OK, {"zones": self.server.config.zones})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except PhoneBusy as exc:
            self._json(HTTPStatus.CONFLICT, {"error": "phone_busy", "detail": str(exc)})
        except GatewayError as exc:
            self._json(HTTPStatus.PRECONDITION_FAILED, {"error": "workflow_assertion", "detail": str(exc)})
        except Exception:
            LOGGER.exception("Unhandled GET error")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            body = self._body()
            header_key = self.headers.get("Idempotency-Key")
            body_key = body.get("idempotency_key")
            if header_key and body_key and header_key != body_key:
                raise GatewayError("Idempotency-Key header and body value differ")
            key = header_key or body_key
            if self.path == f"/{API_VERSION}/vacuums/{VACUUM_ID}/jobs":
                if body.get("action") != "zone_clean":
                    raise GatewayError("Only action=zone_clean is supported")
                job = self.server.workflow.zone_clean(
                    zone_name=body.get("zone_name"),
                    rectangles=body.get("rectangles"),
                    map_generation=body.get("map_generation"),
                    dry_run=bool(body.get("dry_run", False)),
                    idempotency_key=key,
                )
                self._json(HTTPStatus.ACCEPTED, job)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except PhoneBusy as exc:
            self._json(HTTPStatus.CONFLICT, {"error": "phone_busy", "detail": str(exc)})
        except (GatewayError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.PRECONDITION_FAILED, {"error": "workflow_assertion", "detail": str(exc)})
        except Exception:
            LOGGER.exception("Unhandled POST error")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})


class GatewayServer(ThreadingHTTPServer):
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.store = StateStore(config.state_file, config.audit_file)
        self.workflow = XiaomiVacuumWorkflow(config, AndroidMcpClient(config.mcp_url), self.store)
        self.notification_monitor = XiaomiNotificationMonitor(self.store)
        super().__init__((config.bind, config.port), ApiHandler)
        self.notification_monitor.start()

    def server_close(self) -> None:
        self.notification_monitor.stop()
        super().server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/android-vacuum-gateway.json")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    config = GatewayConfig.load(Path(args.config))
    server = GatewayServer(config)
    LOGGER.info("Listening on %s:%s", config.bind, config.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
