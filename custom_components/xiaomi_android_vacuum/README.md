# Xiaomi Android Vacuum Bridge

This integration represents a Xiaomi Robot Vacuum X20+ controlled through a
dedicated Android phone.  It talks only to the local `android-vacuum-gateway`;
Home Assistant never receives Android MCP credentials or a free-form computer
control tool.

The vacuum entity is deliberately read-only.  The only v1 mutation is the
`xiaomi_android_vacuum.start_zone` service for explicit rectangular cleanups.
The gateway is responsible for UI assertions, phone locking, idempotency,
named routines and map-generation expiry.

## Configuration

Use the integration UI to enter the local gateway URL and bearer token, or add
a YAML block (usually with the token in `secrets.yaml`):

```yaml
xiaomi_android_vacuum:
  - url: http://android-vacuum-gateway.local:8091
    token: !secret xiaomi_android_vacuum_gateway_token
```

`base_url` is also accepted; `host` and optional `port` are supported for a
shorter local declaration.  YAML is imported into an ordinary config entry, so
the same deterministic entity IDs and services are used either way.

## Safe zone cleanups

Named routines are the normal path. Refresh the map first: even an allowlisted
routine is tied to a fresh preview so a moved/panned Xiaomi map cannot be used.

```yaml
action: xiaomi_android_vacuum.start_zone
target:
  entity_id: vacuum.xiaomi_robot_vacuum_x20
data:
  zone_name: litter_box
  map_generation: fresh-generation-from-refresh_map
```

For an ad-hoc area, call `refresh_map` first and send the returned/current
`map_generation` with one or two normalized rectangles.  The gateway rejects
stale map previews, malformed coordinates and any UI state that fails its
known Xiaomi workflow assertions.

## Status and alerts

The main vacuum and workflow sensor are polled every 45 seconds when cleaning
and every five minutes otherwise. While a recently observed cleanup is active,
the gateway may reopen Xiaomi Home only from the Android launcher to continue
safe monitoring; it leaves any other foreground app untouched. The integration
emits these Home Assistant events for recorder/automation use:

- `xiaomi_android_vacuum_needs_attention` when Xiaomi Home observes a stuck or
  error state.
- `xiaomi_android_vacuum_job_failed` when an explicit command is safely refused
  or cannot reach the Android bridge.
- `xiaomi_android_vacuum_android_notification` within about three seconds of a
  new Xiaomi Home notification for this exact X20+ vacuum.

The gateway reads Android's notification service every two seconds, but keeps
only `com.xiaomi.smarthome` records whose text names `Xiaomi Robot Vacuum X20+`.
It never returns or stores notifications from other applications or unrelated
Xiaomi devices. Home Assistant records the latest classified event in the
**Latest Android notification** sensor, including categories such as
`cleanup_completed` and `needs_attention`. A new notification also requests an
immediate normal status refresh, rather than waiting for the five-minute idle
poll.

It also creates a deduplicated Home Assistant persistent notification for a
needs-attention transition. Screenshot bytes are retained only in the map image
entity; they are never copied into state attributes or the recorder.
