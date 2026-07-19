"""Pure safety tests for the native Home Assistant Sui integration."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest


_COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "sui_hooverbot"
_TEST_PACKAGE = "_sui_hooverbot_pure_test"
_package = ModuleType(_TEST_PACKAGE)
_package.__path__ = [str(_COMPONENT_DIR)]
sys.modules[_TEST_PACKAGE] = _package


def _load_pure_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{_TEST_PACKAGE}.{name}", _COMPONENT_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_pure_module("const")
model = _load_pure_module("model")
validation = _load_pure_module("validation")
callback_auth = _load_pure_module("callback_auth")


class SuiModelTests(unittest.TestCase):
    def test_counter_validation_rejects_unknown_and_negative_values(self) -> None:
        for value in ("unknown", "unavailable", "", "-1", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                model.parse_counter(value)
        self.assertEqual(model.parse_counter("3.0"), Decimal("3.0"))

    def test_reactions_are_narrow_and_presentation_normalised(self) -> None:
        self.assertEqual(model.normalise_reaction("⏭️"), "⏭")
        self.assertIn(model.normalise_reaction("🛑"), model.ALLOWED_SKIP_REACTIONS)
        self.assertNotIn(model.normalise_reaction("👍"), model.ALLOWED_SKIP_REACTIONS)

    def test_job_has_fixed_event_identity_and_safe_public_view(self) -> None:
        job = model.new_job(
            entry_id="entry-1",
            source_event_key="2026-07-19T10:00:00Z",
            source_count=Decimal("4"),
            detected_at=1_700_000_000,
            cleanup_delay_seconds=600,
            reaction_grace_seconds=30,
            now=1_700_000_000,
        )
        self.assertEqual(job["status"], "notification_sending")
        self.assertTrue(job["event_key"].startswith("sui:entry-1:"))
        self.assertTrue(job["idempotency_key"].startswith("sui-hooverbot-"))
        self.assertEqual(model.parse_utc(job["cleanup_at"]), 1_700_000_600)
        self.assertEqual(model.parse_utc(job["dispatch_after"]), 1_700_000_630)
        public = model.public_job(job)
        self.assertNotIn("event_key", public)
        self.assertNotIn("idempotency_key", public)
        self.assertEqual(public["status"], "notification_sending")

    def test_message_calls_sui_by_name_and_offers_only_skip_reactions(self) -> None:
        message = model.family_message(1_700_000_600)
        self.assertIn("Sui the Hooverbot", message)
        self.assertIn("React", message)
        self.assertIn("⏭️", message)

    def test_overnight_window_coalesces_to_dst_safe_six_am(self) -> None:
        london = model.LOCAL_TIME_ZONE
        late = datetime(2026, 7, 19, 22, 0, tzinfo=london).timestamp()
        early = datetime(2026, 7, 20, 5, 59, tzinfo=london).timestamp()
        boundary = datetime(2026, 7, 20, 6, 0, tzinfo=london).timestamp()
        expected = datetime(2026, 7, 20, 6, 0, tzinfo=london).timestamp()
        self.assertTrue(model.is_overnight_event(late))
        self.assertTrue(model.is_overnight_event(early))
        self.assertFalse(model.is_overnight_event(boundary))
        self.assertEqual(model.next_morning_dispatch(late), expected)
        self.assertEqual(model.next_morning_dispatch(early), expected)

        dst_night = datetime(2026, 10, 24, 23, 0, tzinfo=london).timestamp()
        dst_expected = datetime(2026, 10, 25, 6, 0, tzinfo=london).timestamp()
        self.assertEqual(model.next_morning_dispatch(dst_night), dst_expected)

    def test_overnight_job_dispatches_at_six_after_safety_grace(self) -> None:
        london = model.LOCAL_TIME_ZONE
        detected = datetime(2026, 7, 19, 23, 30, tzinfo=london).timestamp()
        dispatch = model.next_morning_dispatch(detected)
        job = model.new_job(
            entry_id="entry-1",
            source_event_key="night-event",
            source_count=Decimal("5"),
            detected_at=detected,
            cleanup_delay_seconds=600,
            reaction_grace_seconds=30,
            now=detected,
            dispatch_at=dispatch,
            schedule_kind="overnight",
        )
        self.assertEqual(model.parse_utc(job["dispatch_after"]), dispatch)
        self.assertEqual(model.parse_utc(job["cleanup_at"]), dispatch - 30)
        self.assertEqual(job["coalesced_events"], 1)
        self.assertIn("single overnight cleanup", model.family_message(dispatch, overnight=True))

    def test_bridge_payload_is_exact_and_final_status_is_fail_closed(self) -> None:
        payload = model.bridge_message_payload(
            event_key="sui:entry:job",
            text="Sui test",
            deadline_at="2026-07-19T12:10:30Z",
            callback_url="https://ha.example/api/webhook/opaque",
        )
        self.assertEqual(
            payload,
            {
                "event_key": "sui:entry:job",
                "consumer": "sui_hooverbot",
                "text": "Sui test",
                "deadline_at": "2026-07-19T12:10:30Z",
                "callback_url": "https://ha.example/api/webhook/opaque",
            },
        )
        self.assertTrue(model.bridge_allows_dispatch({"status": "pending"}))
        self.assertFalse(model.bridge_allows_dispatch({"status": "reaction_received"}))
        self.assertFalse(model.bridge_allows_dispatch({"status": "anything_else"}))
        self.assertTrue(model.bridge_reports_skipped({"status": "reaction_received"}))
        self.assertFalse(model.bridge_reports_skipped({"status": "skipped"}))
        self.assertTrue(
            model.bridge_response_matches(
                {"event_key": "sui:entry:job", "consumer": "sui_hooverbot"},
                "sui:entry:job",
            )
        )
        self.assertFalse(
            model.bridge_response_matches(
                {"event_key": "other-job", "consumer": "sui_hooverbot"},
                "sui:entry:job",
            )
        )

    def test_config_input_keeps_transport_credentials_separate(self) -> None:
        data = validation.normalise_config_input(
            {
                "counter_entity_id": "sensor.mininook_excretion_times_day",
                "vacuum_entity_id": "vacuum.xiaomi_robot_vacuum_x20",
                "bridge_url": "https://family-bridge.example/",
                "bridge_token": "test-token",
                "cleanup_delay_seconds": "600",
                "reaction_grace_seconds": 30,
                "max_lateness_seconds": 120,
            }
        )
        self.assertEqual(data["bridge_url"], "https://family-bridge.example")
        self.assertEqual(data["bridge_token"], "test-token")
        self.assertEqual(data["cleanup_delay_seconds"], 600)
        self.assertEqual(
            validation.schedule_identity(data),
            "sensor.mininook_excretion_times_day:vacuum.xiaomi_robot_vacuum_x20",
        )

        for bad_url in (
            "family-bridge.example",
            "https://token@family-bridge.example",
            "https://family-bridge.example/v1",
            "https://family-bridge.example?token=secret",
        ):
            with self.assertRaises(ValueError):
                validation.normalise_config_input(
                    {
                        **data,
                        "bridge_url": bad_url,
                    }
                )
        with self.assertRaises(ValueError):
            validation.normalise_config_input(
                {
                    **data,
                    "bridge_token": "",
                }
            )
        same_schedule_different_bridge = validation.normalise_config_input(
            {
                **data,
                "bridge_url": "https://replacement-family-bridge.example",
            }
        )
        self.assertEqual(
            validation.schedule_identity(same_schedule_different_bridge),
            validation.schedule_identity(data),
        )
        with self.assertRaises(ValueError):
            validation.normalise_config_input(
                {
                    **data,
                    "cleanup_delay_seconds": "600.0",
                }
            )

    def test_callback_authentication_binds_timestamp_and_raw_body(self) -> None:
        token = "bridge-token-for-tests"
        timestamp = "1700000000"
        raw_body = b'{"event_key":"sui:entry:job","reaction":"\xe2\x8f\xad"}'
        signature = "sha256=" + callback_auth.callback_signature(token, timestamp, raw_body)
        self.assertTrue(
            callback_auth.callback_authentication_is_valid(
                token=token,
                timestamp=timestamp,
                signature=signature,
                raw_body=raw_body,
                now=1_700_000_100,
            )
        )
        self.assertFalse(
            callback_auth.callback_authentication_is_valid(
                token=token,
                timestamp=timestamp,
                signature=signature,
                raw_body=raw_body + b" ",
                now=1_700_000_100,
            )
        )
        self.assertFalse(
            callback_auth.callback_authentication_is_valid(
                token=token,
                timestamp=timestamp,
                signature=signature,
                raw_body=raw_body,
                now=1_700_000_301,
            )
        )


if __name__ == "__main__":
    unittest.main()
