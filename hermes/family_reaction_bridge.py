#!/usr/bin/env python3
"""Neutral family-message and WhatsApp-reaction bridge for Home Assistant.

The bridge owns no appliance, schedule or Home Assistant token.  Home
Assistant registers an opaque event key, fixed message text, deadline and an
unguessable HA webhook URL.  The bridge sends the family message, stores the
returned WhatsApp message ID, and forwards only an exact approved reaction on
that message to the registered webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib import parse, request


MAX_BODY = 12 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
CONSUMER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ALLOWED_REACTIONS = frozenset({"⏭", "❌", "🛑"})
CALLBACK_TIMESTAMP_HEADER = "X-Family-Reaction-Timestamp"
CALLBACK_SIGNATURE_HEADER = "X-Family-Reaction-Signature"
CALLBACK_SIGNATURE_PREFIX = b"family-reaction-callback-v1"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def now() -> float:
    return time.time()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_deadline(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("deadline_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deadline_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("deadline_at must include an offset")
    return parsed.astimezone(timezone.utc).timestamp()


def normalize_reaction(value: Any) -> str:
    return str(value or "").replace("\ufe0f", "").strip()


def callback_signature(token: str, timestamp: str, raw_body: bytes) -> str:
    """Sign the exact callback bytes with a domain-separated bridge HMAC."""
    signed = CALLBACK_SIGNATURE_PREFIX + b"." + timestamp.encode("ascii") + b"." + raw_body
    return hmac.new(token.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def actor_ref(value: Any) -> str:
    raw = str(value or "").strip()
    return "family-" + hashlib.sha256(raw.encode()).hexdigest()[:12] if raw else "family-unknown"


def reaction_time(value: Any) -> float:
    """Use bridge-provided epoch/ISO time when valid, otherwise receipt time."""
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                candidate = parsed.astimezone(timezone.utc).timestamp() if parsed.tzinfo else 0
            except ValueError:
                return now()
        else:
            return now()
    return candidate if 0 < candidate < now() + 300 else now()


@dataclass(frozen=True)
class Config:
    bind: str
    port: int
    allowed_peers: frozenset[str]
    token: str
    family_chat_id: str
    callback_origin: str
    source_log: Path
    db: Path
    send_url: str
    legacy_inboxes: tuple[Path, ...]
    poll_seconds: float

    @classmethod
    def from_env(cls) -> "Config":
        try:
            port = int(env("FAMILY_REACTION_BRIDGE_PORT", "38181"))
            poll_seconds = float(env("FAMILY_REACTION_BRIDGE_POLL_SECONDS", "2"))
        except ValueError as exc:
            raise RuntimeError("bridge port and poll interval must be numeric") from exc
        if not 1024 <= port <= 65535 or not 0.5 <= poll_seconds <= 60:
            raise RuntimeError("bridge port or poll interval is out of range")
        peers = frozenset(item for item in (part.strip() for part in env("FAMILY_REACTION_BRIDGE_ALLOWED_PEERS", "127.0.0.1").split(",")) if item)
        token = env("FAMILY_REACTION_BRIDGE_TOKEN")
        if not peers or "*" in peers or len(token) < 24:
            raise RuntimeError("explicit peers and a dedicated bridge token are required")
        origin = env("FAMILY_REACTION_BRIDGE_CALLBACK_ORIGIN")
        parsed_origin = parse.urlparse(origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.path not in {"", "/"}:
            raise RuntimeError("FAMILY_REACTION_BRIDGE_CALLBACK_ORIGIN must be an HTTPS origin")
        send_url = env("FAMILY_REACTION_BRIDGE_SEND_URL", "http://127.0.0.1:3000/send")
        parsed_send = parse.urlparse(send_url)
        if parsed_send.scheme != "http" or parsed_send.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("FAMILY_REACTION_BRIDGE_SEND_URL must be a loopback WhatsApp bridge URL")
        inboxes = tuple(Path(item) for item in env("FAMILY_REACTION_BRIDGE_LEGACY_INBOXES", "").split(",") if item.strip())
        source = Path(env("FAMILY_REACTION_BRIDGE_SOURCE_LOG", "/var/lib/family-reaction-bridge/whatsapp-reactions.jsonl"))
        if source in inboxes:
            raise RuntimeError("a legacy inbox cannot be the raw source log")
        family_chat = env("FAMILY_REACTION_BRIDGE_FAMILY_CHAT_ID")
        if not family_chat:
            raise RuntimeError("FAMILY_REACTION_BRIDGE_FAMILY_CHAT_ID is required")
        return cls(
            bind=env("FAMILY_REACTION_BRIDGE_BIND", "127.0.0.1"), port=port,
            allowed_peers=peers, token=token, family_chat_id=family_chat,
            callback_origin=origin.rstrip("/"), source_log=source,
            db=Path(env("FAMILY_REACTION_BRIDGE_DB", "/var/lib/family-reaction-bridge/family-reaction-bridge.sqlite3")),
            send_url=send_url, legacy_inboxes=inboxes, poll_seconds=poll_seconds,
        )

    def callback_is_allowed(self, url: str) -> bool:
        candidate = parse.urlparse(url)
        origin = parse.urlparse(self.callback_origin)
        return (
            candidate.scheme == origin.scheme and candidate.netloc == origin.netloc
            and not candidate.username and not candidate.password and not candidate.query and not candidate.fragment
            and candidate.path.startswith("/api/webhook/") and len(candidate.path.removeprefix("/api/webhook/")) >= 24
        )


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
              event_key TEXT PRIMARY KEY, consumer TEXT NOT NULL, text_hash TEXT NOT NULL,
              deadline_at REAL NOT NULL, callback_url TEXT NOT NULL, message_id TEXT UNIQUE,
              status TEXT NOT NULL, created_at REAL NOT NULL, reacted_at REAL,
              reaction_event_id TEXT UNIQUE, reaction TEXT, actor_ref TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS raw_events (
              event_id TEXT PRIMARY KEY, seen_at REAL NOT NULL, outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS callback_outbox (
              reaction_event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, callback_url TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, delivered_at REAL, last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS callback_outbox_pending ON callback_outbox(next_attempt_at)
              WHERE delivered_at IS NULL;
            """
        )
        self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def get(self, event_key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM messages WHERE event_key = ?", (event_key,)).fetchone()
        return dict(row) if row else None

    def create_sending(self, *, event_key: str, consumer: str, text_hash: str, deadline: float, callback_url: str) -> tuple[dict[str, Any], bool]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM messages WHERE event_key = ?", (event_key,)).fetchone()
            if row:
                current = dict(row)
                if (current["consumer"], current["text_hash"], float(current["deadline_at"]), current["callback_url"]) != (consumer, text_hash, deadline, callback_url):
                    raise ValueError("event_key is already registered with different content")
                return current, False
            conn.execute(
                "INSERT INTO messages(event_key,consumer,text_hash,deadline_at,callback_url,status,created_at) "
                "VALUES(?,?,?,?,?,'sending',?)", (event_key, consumer, text_hash, deadline, callback_url, now())
            )
            return dict(conn.execute("SELECT * FROM messages WHERE event_key = ?", (event_key,)).fetchone()), True

    def mark_sent(self, event_key: str, message_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            changed = conn.execute("UPDATE messages SET message_id=?, status='pending', last_error=NULL WHERE event_key=? AND status='sending'", (message_id, event_key)).rowcount
            if changed != 1:
                raise RuntimeError("could not record WhatsApp message ID")
            return dict(conn.execute("SELECT * FROM messages WHERE event_key = ?", (event_key,)).fetchone())

    def mark_uncertain(self, event_key: str, detail: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE messages SET status='send_uncertain', last_error=? WHERE event_key=? AND status='sending'", (detail[:160], event_key))

    def raw_seen(self, event_id: str) -> bool:
        with self.lock:
            return bool(self.conn.execute("SELECT 1 FROM raw_events WHERE event_id=?", (event_id,)).fetchone())

    def record_raw(self, event_id: str, outcome: str) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO raw_events(event_id,seen_at,outcome) VALUES(?,?,?)", (event_id, now(), outcome))

    def react(self, event: dict[str, Any]) -> str:
        event_id = str(event.get("eventId") or "")
        message_id = str(event.get("targetMessageId") or "")
        reaction = normalize_reaction(event.get("reaction"))
        event_time = reaction_time(event.get("timestamp"))
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if not row:
                return "unregistered"
            if reaction not in ALLOWED_REACTIONS:
                return "wrong_reaction"
            if event_time > float(row["deadline_at"]):
                return "late"
            if row["status"] != "pending":
                return f"already_{row['status']}"
            changed = conn.execute(
                "UPDATE messages SET status='reaction_received', reacted_at=?, reaction_event_id=?, reaction=?, actor_ref=? "
                "WHERE message_id=? AND status='pending'", (now(), event_id, reaction, actor_ref(event.get("reactorId")), message_id)
            ).rowcount
            if changed != 1:
                return "already_changed"
            payload = {
                "event_key": row["event_key"], "consumer": row["consumer"], "reaction_event_id": event_id,
                "reaction": reaction, "actor": actor_ref(event.get("reactorId")), "deadline_at": iso(float(row["deadline_at"])),
            }
            conn.execute(
                "INSERT INTO callback_outbox(reaction_event_id,event_key,callback_url,payload_json,created_at,next_attempt_at) VALUES(?,?,?,?,?,?)",
                (event_id, row["event_key"], row["callback_url"], json.dumps(payload, separators=(",", ":")), now(), now()),
            )
            return "reaction_routed"

    def pending_callbacks(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM callback_outbox WHERE delivered_at IS NULL AND next_attempt_at <= ? ORDER BY created_at LIMIT 20", (now(),)).fetchall()
        return [dict(row) for row in rows]

    def callback_result(self, reaction_event_id: str, error_name: str | None) -> None:
        with self.transaction() as conn:
            if error_name is None:
                conn.execute("UPDATE callback_outbox SET delivered_at=?, last_error=NULL WHERE reaction_event_id=?", (now(), reaction_event_id))
            else:
                row = conn.execute("SELECT attempts FROM callback_outbox WHERE reaction_event_id=?", (reaction_event_id,)).fetchone()
                attempts = int(row["attempts"]) + 1 if row else 1
                conn.execute("UPDATE callback_outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE reaction_event_id=?", (attempts, now() + min(60, 2 ** min(attempts, 6)), error_name[:120], reaction_event_id))


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = Path(f"{path}.lock")

    @contextmanager
    def lock(self, timeout: float = 5) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.lock_path.mkdir(mode=0o700)
                break
            except FileExistsError:
                try:
                    if now() - self.lock_path.stat().st_mtime > 300:
                        self.lock_path.rmdir(); continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {self.lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try: self.lock_path.rmdir()
            except FileNotFoundError: pass

    def snapshot(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0: return
        with self.lock():
            if not self.path.exists() or self.path.stat().st_size == 0: return
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            pending = self.path.with_name(f"{self.path.name}.pending.{stamp}.{os.getpid()}.jsonl")
            os.replace(self.path, pending); os.chmod(pending, 0o600)

    def pending(self) -> list[Path]:
        return sorted(self.path.parent.glob(f"{self.path.name}.pending.*.jsonl"))

    def append(self, event: dict[str, Any]) -> None:
        payload = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                written = 0
                while written < len(payload): written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config, self.store = config, Store(config.db)
        self.source = JsonlLog(config.source_log)
        self.legacy = tuple(JsonlLog(path) for path in config.legacy_inboxes)
        self.process_lock = threading.Lock()
        self.stop = threading.Event()

    def send(self, text: str) -> str:
        req = request.Request(self.config.send_url, data=json.dumps({"chatId": self.config.family_chat_id, "message": text}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=15) as response: payload = json.loads(response.read().decode())
        message_id = str(payload.get("messageId") or "").strip()
        if not message_id: raise RuntimeError("WhatsApp bridge did not return a message ID")
        return message_id

    def register(self, event_key: str, consumer: str, text: str, deadline: float, callback_url: str) -> tuple[dict[str, Any], bool]:
        digest = hashlib.sha256(text.encode()).hexdigest()
        row, created = self.store.create_sending(event_key=event_key, consumer=consumer, text_hash=digest, deadline=deadline, callback_url=callback_url)
        if not created: return row, False
        try: message_id = self.send(text)
        except Exception as exc:
            self.store.mark_uncertain(event_key, type(exc).__name__); raise RuntimeError("message delivery is uncertain and will not be retried automatically") from exc
        return self.store.mark_sent(event_key, message_id), True

    def process_raw(self) -> bool:
        """Synchronously drain newly arrived reactions before a status read.

        The same lock covers the periodic worker and HTTP status requests, so
        a pending batch cannot be fanned out twice before its event ID is
        recorded.  A failure is observable by the status endpoint and must
        block a physical action rather than returning a stale ``pending``.
        """
        with self.process_lock:
            try:
                self.source.snapshot()
                batches = self.source.pending()
            except Exception as exc:
                print(
                    f"reaction intake preparation failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                return False

            for batch in batches:
                try:
                    lines = batch.read_text(errors="replace").splitlines()
                except Exception as exc:
                    print(
                        f"reaction intake read failed: {type(exc).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return False

                for line in lines:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        event_id = str(event.get("eventId") or "")
                        if not ID_PATTERN.fullmatch(event_id):
                            continue
                        if self.store.raw_seen(event_id):
                            continue
                        if str(event.get("chatId") or "") != self.config.family_chat_id:
                            self.store.record_raw(event_id, "wrong_chat")
                            continue
                        # Legacy inboxes preserve existing family-alert behaviour.
                        # Consumers retain their own exact target-ID validation.
                        for inbox in self.legacy:
                            inbox.append(event)
                        outcome = self.store.react(event)
                        self.store.record_raw(event_id, outcome)
                    except Exception as exc:
                        print(
                            f"reaction intake processing failed: {type(exc).__name__}",
                            file=sys.stderr,
                            flush=True,
                        )
                        return False
                try:
                    batch.unlink(missing_ok=True)
                except Exception as exc:
                    print(
                        f"reaction intake cleanup failed: {type(exc).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return False
        return True

    def deliver_callbacks(self) -> None:
        for item in self.store.pending_callbacks():
            try:
                raw_body = str(item["payload_json"]).encode("utf-8")
                timestamp = str(int(now()))
                signature = callback_signature(self.config.token, timestamp, raw_body)
                req = request.Request(
                    str(item["callback_url"]),
                    data=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        CALLBACK_TIMESTAMP_HEADER: timestamp,
                        CALLBACK_SIGNATURE_HEADER: f"sha256={signature}",
                    },
                    method="POST",
                )
                with request.urlopen(req, timeout=15) as response:
                    if not 200 <= response.status < 300: raise RuntimeError("callback HTTP status")
            except Exception as exc:
                self.store.callback_result(str(item["reaction_event_id"]), type(exc).__name__)
            else:
                self.store.callback_result(str(item["reaction_event_id"]), None)

    def loop(self) -> None:
        while not self.stop.is_set():
            self.process_raw(); self.deliver_callbacks(); self.stop.wait(self.config.poll_seconds)


class Api(BaseHTTPRequestHandler):
    server: "BridgeHttpServer"
    def log_message(self, *_args: Any) -> None: return
    def json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode(); self.send_response(status)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw)
    def auth(self) -> bool:
        cfg = self.server.bridge.config
        if not self.peer_allowed(): self.json(HTTPStatus.FORBIDDEN, {"error":"peer_not_allowed"}); return False
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {cfg.token}"):
            self.json(HTTPStatus.UNAUTHORIZED, {"error":"unauthorized"}); return False
        return True
    def peer_allowed(self) -> bool:
        return self.client_address[0] in self.server.bridge.config.allowed_peers
    def body(self) -> dict[str, Any]:
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc: raise ValueError("invalid content length") from exc
        if not 0 < length <= MAX_BODY: raise ValueError("request body is required and small")
        value = json.loads(self.rfile.read(length).decode())
        if not isinstance(value, dict): raise ValueError("request body must be an object")
        return value
    def do_GET(self) -> None:
        if self.path == "/health":
            if not self.peer_allowed(): self.json(HTTPStatus.FORBIDDEN, {"error":"peer_not_allowed"}); return
            self.json(HTTPStatus.OK, {"ok":True,"service":"family-reaction-bridge"}); return
        if not self.auth(): return
        match = re.fullmatch(r"/v1/messages/([^/]+)", self.path)
        if not match: self.json(HTTPStatus.NOT_FOUND, {"error":"not_found"}); return
        if not self.server.bridge.process_raw():
            self.json(HTTPStatus.SERVICE_UNAVAILABLE, {"error":"reaction_state_unavailable"})
            return
        row = self.server.bridge.store.get(parse.unquote(match.group(1)))
        if not row: self.json(HTTPStatus.NOT_FOUND, {"error":"not_found"}); return
        self.json(HTTPStatus.OK, {"event_key":row["event_key"],"consumer":row["consumer"],"status":row["status"],"deadline_at":iso(float(row["deadline_at"]))})
    def do_POST(self) -> None:
        if not self.auth(): return
        if self.path != "/v1/messages": self.json(HTTPStatus.NOT_FOUND, {"error":"not_found"}); return
        try:
            value = self.body()
            if set(value) != {"event_key","consumer","text","deadline_at","callback_url"}: raise ValueError("only event_key, consumer, text, deadline_at and callback_url are accepted")
            event_key, consumer, text, callback = str(value["event_key"]), str(value["consumer"]), str(value["text"]), str(value["callback_url"])
            if not ID_PATTERN.fullmatch(event_key) or not CONSUMER_PATTERN.fullmatch(consumer): raise ValueError("invalid event_key or consumer")
            if not 1 <= len(text) <= 1200 or "\x00" in text: raise ValueError("invalid text")
            deadline = parse_deadline(value["deadline_at"])
            if not 20 <= deadline-now() <= 7200: raise ValueError("deadline must be 20 seconds to two hours in the future")
            if not self.server.bridge.config.callback_is_allowed(callback): raise ValueError("callback_url is not an allowed Home Assistant webhook")
            row, created = self.server.bridge.register(event_key, consumer, text, deadline, callback)
            self.json(HTTPStatus.CREATED if created else HTTPStatus.OK, {"event_key":event_key,"status":row["status"],"deadline_at":iso(float(row["deadline_at"]))})
        except ValueError as exc: self.json(HTTPStatus.BAD_REQUEST, {"error":str(exc)})
        except RuntimeError as exc: self.json(HTTPStatus.BAD_GATEWAY, {"error":str(exc)})
        except Exception: self.json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error":"internal_error"})


class BridgeHttpServer(ThreadingHTTPServer):
    allow_reuse_address, daemon_threads = True, True
    def __init__(self, address: tuple[str,int], bridge: Bridge) -> None:
        self.bridge = bridge; super().__init__(address, Api)


def main() -> int:
    bridge = Bridge(Config.from_env()); server = BridgeHttpServer((bridge.config.bind, bridge.config.port), bridge)
    def stop(*_args: Any) -> None:
        bridge.stop.set(); threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    worker = threading.Thread(target=bridge.loop, daemon=True); worker.start()
    try: server.serve_forever(poll_interval=0.5)
    finally: bridge.stop.set(); server.server_close(); worker.join(timeout=5)
    return 0


if __name__ == "__main__": raise SystemExit(main())
