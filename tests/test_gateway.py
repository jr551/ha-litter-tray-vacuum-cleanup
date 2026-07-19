"""Pure safety tests for the deterministic Xiaomi workflow gateway."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock


MODULE = Path(__file__).parents[1] / "gateway" / "android_vacuum_gateway.py"
SPEC = importlib.util.spec_from_file_location("android_vacuum_gateway", MODULE)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class GatewaySafetyTests(unittest.TestCase):
    def test_android_notification_parser_keeps_only_x20_events(self) -> None:
        dump = """
NotificationRecord(0x1: pkg=com.example.chat user=UserHandle{0} id=1 tag=null)
    when=1784485315000
    android.title=String (Private message)
    android.text=String (must never leave the phone)
NotificationRecord(0x2: pkg=com.xiaomi.smarthome user=UserHandle{0} id=516 tag=null)
    when=1784485315369
    android.title=String (Cleanup completed)
    android.text=String (【Xiaomi Robot Vacuum X20+-Home】)
NotificationRecord(0x3: pkg=com.xiaomi.smarthome user=UserHandle{0} id=7 tag=null)
    when=1784485316000
    android.title=String (Door opened)
    android.text=String (Front door sensor)
"""
        events = gateway.parse_xiaomi_vacuum_notifications(dump)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "cleanup_completed")
        self.assertEqual(events[0]["source"], "android_notification")
        self.assertNotIn("Private message", str(events))

    def test_android_attention_notification_is_classified(self) -> None:
        dump = """
NotificationRecord(0x2: pkg=com.xiaomi.smarthome user=UserHandle{0} id=516 tag=null)
    when=1784485315369
    android.title=String (Wheels are suspended)
    android.bigText=String (【Xiaomi Robot Vacuum X20+-Home】)
"""
        events = gateway.parse_xiaomi_vacuum_notifications(dump)
        self.assertEqual(events[0]["category"], "needs_attention")

    def test_focused_package_accepts_multiline_android_dump(self) -> None:
        dump = "window: 4\npkg:com.xiaomi.smarthome\nfocused:true\n"
        self.assertEqual(gateway.focused_package(dump), "com.xiaomi.smarthome")

    def test_litter_box_round_trips_to_known_screen_area(self) -> None:
        source = {"x1": 195, "y1": 1365, "x2": 545, "y2": 1685}
        normalized = gateway.pixels_to_normalized(source)
        rebuilt = gateway.normalized_to_pixels(normalized)
        for coordinate in source:
            self.assertLessEqual(abs(source[coordinate] - rebuilt[coordinate]), 1)

    def test_rejects_invalid_or_tiny_custom_rectangle(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            gateway.normalize_rectangle({"x1": 10, "y1": 10, "x2": 9, "y2": 50})
        with self.assertRaises(gateway.GatewayError):
            gateway.normalize_rectangle({"x1": 10, "y1": 10, "x2": 11, "y2": 11})

    def test_detects_an_attention_state_before_any_tap(self) -> None:
        self.assertEqual(
            gateway.parse_activity(
                "hierarchy:\nnode TextView Robot is stuck. Pause cleanup - 100,500,800,600 on"
            ),
            "error",
        )

        offline = "hierarchy:\nnode TextView Device is offline - - 143,1759,937,1821 on"
        self.assertEqual(gateway.attention_reason(offline), "device_is_offline")
        self.assertEqual(gateway.parse_activity(offline), "error")

    def test_ignores_system_notification_errors_outside_app_hierarchy(self) -> None:
        dump = (
            "pkg:com.xiaomi.smarthome focused:true\n"
            "Android System notification: Sign-in error\n"
            "hierarchy:\n"
            "node TextView Charging completed - - 198,209,893,260 on,ena\n"
        )
        self.assertEqual(gateway.parse_activity(dump), "docked")
        self.assertIsNone(gateway.attention_reason(dump))

    def test_cleaning_area_label_is_not_itself_a_cleaning_state(self) -> None:
        dump = (
            "hierarchy:\n"
            "node TextView Cleaning area - - 223,405,392,438 on,ena\n"
            "node TextView Start cleanup - - 734,1768,1032,1826 on,ena\n"
            "node TextView Charging completed - - 198,209,893,260 on,ena\n"
        )
        self.assertEqual(gateway.parse_activity(dump), "docked")

    def test_detail_screen_is_not_confused_with_device_card(self) -> None:
        card = (
            "hierarchy:\n"
            "node\tTextView\tXiaomi Robot Vacuum X20+\t-\t-\t50,500,900,600\ton,ena\n"
        )
        detail = card + "node\tTextView\tStart cleanup\t-\t-\t734,1768,1032,1826\ton,ena\n"
        hidden = card + "node\tTextView\tStart cleanup\t-\t-\t734,301,1032,227\toff,ena\n"
        self.assertFalse(gateway.is_x20_detail_screen(card))
        self.assertFalse(gateway.is_x20_detail_screen(hidden))
        self.assertTrue(gateway.is_x20_detail_screen(detail))
        title_node = {
            "bounds": {"left": 258, "top": 149, "right": 834, "bottom": 209},
            "visible": True,
            "enabled": True,
        }
        card_node = {
            "bounds": {"left": 50, "top": 400, "right": 1030, "bottom": 650},
            "visible": True,
            "enabled": True,
        }
        self.assertFalse(gateway.is_x20_device_card_node(title_node))
        self.assertTrue(gateway.is_x20_device_card_node(card_node))

    @patch.object(gateway.time, "sleep")
    def test_nested_xiaomi_page_is_bounded_back_to_detail(self, _sleep: MagicMock) -> None:
        nested = (
            "screen:1080x2400\n"
            "pkg:com.xiaomi.smarthome focused:true\n"
            "hierarchy:\nnode TextView Scheduled cleanup - - 334,148,758,213 on,ena\n"
        )
        detail = (
            "screen:1080x2400\n"
            "pkg:com.xiaomi.smarthome focused:true\n"
            "hierarchy:\n"
            "node\tTextView\tXiaomi Robot Vacuum X20+\t-\t-\t233,144,858,209\ton,ena\n"
            "node\tTextView\tStart cleanup\t-\t-\t734,1768,1032,1826\ton,ena\n"
        )
        workflow = gateway.XiaomiVacuumWorkflow.__new__(gateway.XiaomiVacuumWorkflow)
        workflow._mcp = MagicMock()
        workflow._mcp.screen.side_effect = [{"text": nested}, {"text": detail}]
        workflow._mcp.find_nodes.side_effect = [
            [],
            [
                {
                    "bounds": {"left": 55, "top": 125, "right": 165, "bottom": 235},
                    "visible": True,
                    "enabled": True,
                }
            ],
        ]
        self.assertEqual(workflow._require_xiaomi_screen(allow_open=True), {"text": detail})
        workflow._mcp.tool.assert_called_once_with("android_tap", {"x": 110, "y": 180})

    @patch.object(gateway.time, "sleep")
    def test_station_status_popup_is_dismissed_without_station_action(
        self, _sleep: MagicMock
    ) -> None:
        popup = (
            "screen:1080x2400\n"
            "pkg:com.xiaomi.smarthome focused:true\n"
            "hierarchy:\n"
            "node\tTextView\tStation\t-\t-\t469,1625,612,1684\ton,ena\n"
            "node\tTextView\tNo tasks yet\t-\t-\t432,1687,648,1737\ton,ena\n"
            "node\tTextView\tHide pop-up window\t-\t-\t336,2104,743,2163\ton,ena\n"
        )
        detail = (
            "screen:1080x2400\n"
            "pkg:com.xiaomi.smarthome focused:true\n"
            "hierarchy:\n"
            "node\tTextView\tXiaomi Robot Vacuum X20+\t-\t-\t233,144,858,209\ton,ena\n"
            "node\tTextView\tStart cleanup\t-\t-\t734,1768,1032,1826\ton,ena\n"
        )
        workflow = gateway.XiaomiVacuumWorkflow.__new__(gateway.XiaomiVacuumWorkflow)
        workflow._mcp = MagicMock()
        workflow._mcp.screen.side_effect = [{"text": popup}, {"text": detail}]
        workflow._mcp.find_nodes.return_value = [
            {
                "bounds": {"left": 74, "top": 2072, "right": 1006, "bottom": 2196},
                "visible": True,
                "enabled": True,
            }
        ]
        self.assertEqual(workflow._require_xiaomi_screen(allow_open=True), {"text": detail})
        workflow._mcp.tool.assert_called_once_with("android_tap", {"x": 540, "y": 2134})

    def test_canonical_map_fingerprint_rejects_materially_moved_anchor(self) -> None:
        canonical = (
            "screen:1080x2400\n"
            "hierarchy:\n"
            "node ViewGroup - - - 0,301,1080,2270 on,clk\n"
            "node TextView Xiaomi Robot Vacuum X20+ - - 233,144,858,209 on\n"
            "node TextView Room12 - - 0,301,144,350 off\n"
            "node ViewGroup - - - 943,560,998,693 on,clk\n"
            "node TextView Start cleanup - - 734,1768,1032,1826 on\n"
        )
        self.assertTrue(gateway.canonical_map_fingerprint(canonical))
        self.assertRaises(
            gateway.WorkflowAssertion,
            gateway.canonical_map_fingerprint,
            canonical.replace("0,301,144,350", "6,301,150,350"),
        )

    def test_canonical_map_fingerprint_allows_tiny_rn_rounding(self) -> None:
        canonical = (
            "screen:1080x2400\n"
            "hierarchy:\n"
            "node TextView Xiaomi Robot Vacuum X20+ - - 233,144,858,209 on\n"
            "node TextView Room12 - - 0,301,144,350 off\n"
            "node ViewGroup - - - 943,560,998,693 on,clk\n"
            "node TextView Start cleanup - - 734,1770,1032,1824 on\n"
            "node ViewGroup - - - 0,301,1080,2270 on,clk\n"
        )
        self.assertTrue(gateway.canonical_map_fingerprint(canonical))

    def test_mcp_client_decodes_streamable_http_sse_result(self) -> None:
        raw = b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n'
        self.assertEqual(
            gateway.AndroidMcpClient._decode_response(raw, "text/event-stream"),
            {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},
        )

    def test_mcp_client_accepts_null_notification_acknowledgement(self) -> None:
        self.assertIsNone(gateway.AndroidMcpClient._decode_response(b"null", "application/json"))

    def test_raw_screenshot_requires_exact_phone_geometry(self) -> None:
        header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        valid = header + struct.pack(">II", 1080, 2400) + b"pixels"
        self.assertEqual(gateway.validate_png_screenshot(valid), valid)
        with self.assertRaises(gateway.WorkflowAssertion):
            gateway.validate_png_screenshot(
                header + struct.pack(">II", 720, 1600) + b"pixels"
            )

    def test_find_nodes_uses_only_structured_json_line(self) -> None:
        client = gateway.AndroidMcpClient("http://127.0.0.1:18080/mcp")
        wrapped = [
            {
                "type": "text",
                "text": "untrusted wrapper text\n"
                '{"nodes":[{"node_id":"node_1","text":"Xiaomi Home","visible":true}]}',
            }
        ]
        with patch.object(client, "tool", return_value=wrapped):
            self.assertEqual(
                client.find_nodes(by="text", value="Xiaomi Home"),
                [{"node_id": "node_1", "text": "Xiaomi Home", "visible": True}],
            )

    def test_zone_jobs_require_bounded_idempotency_key(self) -> None:
        self.assertEqual(
            gateway.XiaomiVacuumWorkflow._require_idempotency_key("a" * 16),
            "a" * 16,
        )
        with self.assertRaises(gateway.GatewayError):
            gateway.XiaomiVacuumWorkflow._require_idempotency_key("short")

    def test_idempotency_refuses_a_different_request_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = gateway.StateStore(root / "state.json", root / "audit.jsonl")
            job = {
                "job_id": "job-1",
                "status": "accepted",
                "request_fingerprint": "a" * 64,
            }
            store.remember_job(job, "zone-request-key-0001")
            self.assertEqual(
                store.idempotent_job("zone-request-key-0001", "a" * 64)["job_id"],
                "job-1",
            )
            with self.assertRaises(gateway.GatewayError):
                store.idempotent_job("zone-request-key-0001", "b" * 64)

    def test_view_only_map_generation_cannot_authorize_motion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = gateway.StateStore(root / "state.json", root / "audit.jsonl")
            store.set(
                "map",
                {
                    "generation": "view-only-generation",
                    "expires_at": 9_999_999_999,
                    "layout_fingerprint": None,
                    "zone_ready": False,
                },
            )
            workflow = gateway.XiaomiVacuumWorkflow.__new__(gateway.XiaomiVacuumWorkflow)
            workflow._store = store
            with self.assertRaises(gateway.WorkflowAssertion):
                workflow._assert_fresh_map("view-only-generation")


if __name__ == "__main__":
    unittest.main()
