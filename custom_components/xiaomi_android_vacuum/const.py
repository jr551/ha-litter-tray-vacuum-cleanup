"""Constants for the Xiaomi Android Vacuum integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "xiaomi_android_vacuum"
PLATFORMS: Final = ["binary_sensor", "image", "sensor", "vacuum"]

CONF_BASE_URL: Final = "base_url"
CONF_TOKEN: Final = "token"
CONF_URL: Final = "url"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"

DEFAULT_BASE_URL: Final = "http://android-vacuum-gateway.local:8091"
VACUUM_ID: Final = "xiaomi-robot-vacuum-x20-plus"
API_PREFIX: Final = "/v1"

SERVICE_START_ZONE: Final = "start_zone"
SERVICE_REFRESH_MAP: Final = "refresh_map"

ATTR_ZONE_NAME: Final = "zone_name"
ATTR_RECTANGLES: Final = "rectangles"
ATTR_MAP_GENERATION: Final = "map_generation"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_IDEMPOTENCY_KEY: Final = "idempotency_key"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

EVENT_NEEDS_ATTENTION: Final = f"{DOMAIN}_needs_attention"
EVENT_JOB_FAILED: Final = f"{DOMAIN}_job_failed"
EVENT_ANDROID_NOTIFICATION: Final = f"{DOMAIN}_android_notification"

NOTIFICATION_POLL_INTERVAL: Final = 3

CLEANING_UPDATE_INTERVAL: Final = timedelta(seconds=45)
IDLE_UPDATE_INTERVAL: Final = timedelta(minutes=5)
REQUEST_TIMEOUT: Final = 20

MANUFACTURER: Final = "Xiaomi"
MODEL: Final = "Robot Vacuum X20+"
WORKFLOW_NAME: Final = "Xiaomi Home Android workflow"
