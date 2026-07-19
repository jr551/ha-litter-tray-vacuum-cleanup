"""Constants for the native Sui the Hooverbot integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final


DOMAIN: Final = "sui_hooverbot"
PLATFORMS: Final = ["binary_sensor", "sensor"]

CONF_BRIDGE_URL: Final = "bridge_url"
CONF_BRIDGE_TOKEN: Final = "bridge_token"
CONF_COUNTER_ENTITY_ID: Final = "counter_entity_id"
CONF_VACUUM_ENTITY_ID: Final = "vacuum_entity_id"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_CLEANUP_DELAY_SECONDS: Final = "cleanup_delay_seconds"
CONF_REACTION_GRACE_SECONDS: Final = "reaction_grace_seconds"
CONF_MAX_LATENESS_SECONDS: Final = "max_lateness_seconds"

DEFAULT_COUNTER_ENTITY_ID: Final = "sensor.mininook_excretion_times_day"
DEFAULT_VACUUM_ENTITY_ID: Final = "vacuum.xiaomi_robot_vacuum_x20"
DEFAULT_CLEANUP_DELAY_SECONDS: Final = 600
DEFAULT_REACTION_GRACE_SECONDS: Final = 30
DEFAULT_MAX_LATENESS_SECONDS: Final = 120
DEFAULT_RETRY_SECONDS: Final = 15
MAX_PENDING_JOBS: Final = 32
MAX_TERMINAL_JOBS: Final = 100
MAX_PROCESSED_EVENTS: Final = 256
MAX_WEBHOOK_BYTES: Final = 16 * 1024
BRIDGE_TIMEOUT: Final = timedelta(seconds=15)
STORAGE_VERSION: Final = 1

EVENT_NOTIFICATION_REQUESTED: Final = f"{DOMAIN}_notification_requested"
EVENT_JOB_UPDATED: Final = f"{DOMAIN}_job_updated"
EVENT_SKIP: Final = f"{DOMAIN}_skip"

SERVICE_SKIP: Final = "skip"

XIAOMI_DOMAIN: Final = "xiaomi_android_vacuum"
XIAOMI_REFRESH_MAP_SERVICE: Final = "refresh_map"
XIAOMI_START_ZONE_SERVICE: Final = "start_zone"
FIXED_ZONE_NAME: Final = "litter_box"

SKIP_REACTIONS: Final = frozenset({"⏭", "❌", "🛑"})
