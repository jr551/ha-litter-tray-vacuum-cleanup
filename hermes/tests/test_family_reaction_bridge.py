from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from family_reaction_bridge import (  # noqa: E402
    CALLBACK_SIGNATURE_HEADER,
    CALLBACK_TIMESTAMP_HEADER,
    Bridge,
    BridgeHttpServer,
    Config,
    callback_signature,
)


CHAT = "family@example.invalid"
CALLBACK = "https://ha.example.invalid/api/webhook/abcdefghijklmnopqrstuvwxyz123456"


class CallbackRecorder(BaseHTTPRequestHandler):
    """Minimal local receiver used to verify exact bridge callback bytes."""

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.callback_body = self.rfile.read(length)  # type: ignore[attr-defined]
        self.server.callback_headers = dict(self.headers.items())  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.ACCEPTED)
        self.end_headers()


def config(directory: Path) -> Config:
    return Config(
        bind="127.0.0.1", port=38181, allowed_peers=frozenset({"127.0.0.1"}), token="x" * 32,
        family_chat_id=CHAT, callback_origin="https://ha.example.invalid",
        source_log=directory / "raw.jsonl", db=directory / "bridge.sqlite3",
        send_url="http://127.0.0.1:3000/send", legacy_inboxes=(directory / "family_alerts.jsonl",), poll_seconds=2,
    )


class FamilyReactionBridgeTests(unittest.TestCase):
    def _register(self, bridge: Bridge, event_key: str = "sui-visit-000000001", deadline: float | None = None) -> None:
        bridge.store.create_sending(
            event_key=event_key, consumer="sui", text_hash="a" * 64,
            deadline=deadline if deadline is not None else 9_999_999_999, callback_url=CALLBACK,
        )
        bridge.store.mark_sent(event_key, "whatsapp-message-00000001")

    def test_exact_pre_deadline_iso_reaction_is_fanned_out_and_routed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            # The raw event was generated before the deadline but the router
            # receives it just after; ISO time must preserve the valid skip.
            deadline = datetime.now(timezone.utc).timestamp() - 1
            self._register(bridge, deadline=deadline)
            event = {
                "eventId": "reaction-event-000001", "chatId": CHAT,
                "targetMessageId": "whatsapp-message-00000001", "reactorId": "person@example.invalid",
                "reaction": "⏭️", "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
            }
            bridge.config.source_log.write_text(json.dumps(event) + "\n")
            bridge.process_raw()
            self.assertEqual(bridge.store.get("sui-visit-000000001")["status"], "reaction_received")
            self.assertEqual(len(bridge.store.pending_callbacks()), 1)
            inbox = bridge.config.legacy_inboxes[0]
            self.assertEqual(json.loads(inbox.read_text()), event)
            self.assertEqual(bridge.source.pending(), [])

    def test_wrong_target_has_no_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            bridge.config.source_log.write_text(json.dumps({
                "eventId": "reaction-event-000002", "chatId": CHAT,
                "targetMessageId": "other-message-00000001", "reactorId": "person@example.invalid",
                "reaction": "❌", "timestamp": 1_700_000_000,
            }) + "\n")
            bridge.process_raw()
            self.assertEqual(bridge.store.get("sui-visit-000000001")["status"], "pending")
            self.assertEqual(bridge.store.pending_callbacks(), [])

    def test_wrong_chat_and_wrong_emoji_are_never_routed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            events = [
                {"eventId": "reaction-event-000003", "chatId": "wrong-chat", "targetMessageId": "whatsapp-message-00000001", "reactorId": "p", "reaction": "⏭️"},
                {"eventId": "reaction-event-000004", "chatId": CHAT, "targetMessageId": "whatsapp-message-00000001", "reactorId": "p", "reaction": "👍"},
            ]
            bridge.config.source_log.write_text("".join(json.dumps(event) + "\n" for event in events))
            bridge.process_raw()
            self.assertEqual(bridge.store.get("sui-visit-000000001")["status"], "pending")
            self.assertEqual(bridge.store.pending_callbacks(), [])

    def test_duplicate_raw_event_creates_one_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            event = {"eventId": "reaction-event-000005", "chatId": CHAT, "targetMessageId": "whatsapp-message-00000001", "reactorId": "p", "reaction": "❌"}
            bridge.config.source_log.write_text((json.dumps(event) + "\n") * 2)
            bridge.process_raw()
            self.assertEqual(len(bridge.store.pending_callbacks()), 1)
            self.assertEqual(bridge.config.legacy_inboxes[0].read_text().count("reaction-event-000005"), 1)

    def test_callback_failure_is_kept_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            bridge.config.source_log.write_text(json.dumps({"eventId": "reaction-event-000006", "chatId": CHAT, "targetMessageId": "whatsapp-message-00000001", "reactorId": "p", "reaction": "🛑"}) + "\n")
            bridge.process_raw()
            bridge.store.callback_result("reaction-event-000006", "URLError")
            with bridge.store.lock:
                row = bridge.store.conn.execute("SELECT attempts, delivered_at, last_error FROM callback_outbox WHERE reaction_event_id=?", ("reaction-event-000006",)).fetchone()
            self.assertEqual((row["attempts"], row["delivered_at"], row["last_error"]), (1, None, "URLError"))

    def test_callback_signature_is_bound_to_domain_timestamp_and_raw_body(self) -> None:
        token = "x" * 32
        timestamp = "1700000000"
        raw_body = b'{"event_key":"sui-visit-000000001"}'
        signature = callback_signature(token, timestamp, raw_body)
        self.assertEqual(len(signature), 64)
        self.assertNotEqual(signature, callback_signature(token, timestamp, raw_body + b" "))
        self.assertNotEqual(signature, callback_signature(token, "1700000001", raw_body))
        self.assertEqual(CALLBACK_TIMESTAMP_HEADER, "X-Family-Reaction-Timestamp")
        self.assertEqual(CALLBACK_SIGNATURE_HEADER, "X-Family-Reaction-Signature")

    def test_callback_delivery_sends_a_signature_for_the_exact_body(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            receiver = ThreadingHTTPServer(("127.0.0.1", 0), CallbackRecorder)
            receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            receiver_thread.start()
            try:
                callback_url = f"http://127.0.0.1:{receiver.server_port}/callback"
                bridge.store.create_sending(
                    event_key="sui-visit-000000007",
                    consumer="sui",
                    text_hash="a" * 64,
                    deadline=9_999_999_999,
                    callback_url=callback_url,
                )
                bridge.store.mark_sent("sui-visit-000000007", "whatsapp-message-00000007")
                bridge.config.source_log.write_text(
                    json.dumps(
                        {
                            "eventId": "reaction-event-000007",
                            "chatId": CHAT,
                            "targetMessageId": "whatsapp-message-00000007",
                            "reactorId": "person@example.invalid",
                            "reaction": "⏭️",
                        }
                    )
                    + "\n"
                )
                bridge.process_raw()
                bridge.deliver_callbacks()
            finally:
                receiver.shutdown()
                receiver.server_close()
                receiver_thread.join(timeout=2)

            callback_body = receiver.callback_body  # type: ignore[attr-defined]
            callback_headers = receiver.callback_headers  # type: ignore[attr-defined]
            timestamp = callback_headers[CALLBACK_TIMESTAMP_HEADER]
            self.assertEqual(
                callback_headers[CALLBACK_SIGNATURE_HEADER],
                f"sha256={callback_signature(bridge.config.token, timestamp, callback_body)}",
            )
            self.assertEqual(len(bridge.store.pending_callbacks()), 0)

    def test_callback_url_must_be_same_origin_webhook(self) -> None:
        cfg = config(Path(tempfile.gettempdir()))
        self.assertTrue(cfg.callback_is_allowed(CALLBACK))
        self.assertFalse(cfg.callback_is_allowed("https://elsewhere.invalid/api/webhook/abcdefghijklmnopqrstuvwxyz123456"))
        self.assertFalse(cfg.callback_is_allowed("https://ha.example.invalid/api/events/not-a-webhook"))

    def test_authenticated_get_returns_pre_start_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            server = BridgeHttpServer(("127.0.0.1", 0), bridge)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/messages/sui-visit-000000001"
                response = request.urlopen(request.Request(url, headers={"Authorization": "Bearer " + "x" * 32}), timeout=3)
                payload = json.loads(response.read())
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertEqual(payload["event_key"], "sui-visit-000000001")
            self.assertEqual(payload["status"], "pending")

    def test_authenticated_get_drains_a_pre_deadline_reaction_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            bridge.config.source_log.write_text(
                json.dumps(
                    {
                        "eventId": "reaction-event-000008",
                        "chatId": CHAT,
                        "targetMessageId": "whatsapp-message-00000001",
                        "reactorId": "person@example.invalid",
                        "reaction": "🛑",
                    }
                )
                + "\n"
            )
            server = BridgeHttpServer(("127.0.0.1", 0), bridge)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/messages/sui-visit-000000001"
                response = request.urlopen(
                    request.Request(url, headers={"Authorization": "Bearer " + "x" * 32}),
                    timeout=3,
                )
                payload = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(payload["status"], "reaction_received")

    def test_get_fails_closed_when_reaction_intake_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge = Bridge(config(Path(raw)))
            self._register(bridge)
            bridge.config.source_log.write_text("this is not json\n")
            server = BridgeHttpServer(("127.0.0.1", 0), bridge)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/messages/sui-visit-000000001"
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(
                        request.Request(
                            url,
                            headers={"Authorization": "Bearer " + "x" * 32},
                        ),
                        timeout=3,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(raised.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
