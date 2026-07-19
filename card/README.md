# Xiaomi Android Vacuum Map card

`xiaomi-android-vacuum-map.js` is a dependency-free Lovelace custom card for
the companion `xiaomi_android_vacuum` integration. It overlays a current phone
map screenshot, accepts one or two drag-drawn rectangles, and sends only the
integration's bounded `start_zone` service calls. It does not speak to Android,
Xiaomi Home, or the gateway directly.

The card does not automatically run a cleaning job. `Preview` sends a dry-run
request; `Start` changes to `Confirm start` and must be clicked again within
eight seconds.

## Install as a local dashboard resource

Copy the source file to Home Assistant's `www` directory, for example:

```text
/config/www/xiaomi-android-vacuum-map.js
```

Then add this dashboard resource through **Settings → Dashboards → Resources**:

```yaml
url: /local/xiaomi-android-vacuum-map.js
type: module
```

Do not edit Home Assistant's `.storage` files to add the resource. Reload the
browser after adding or updating it.

## Card configuration

The entity names below are examples; use the names created by the installed
integration. All rectangle coordinates are normalized screenshot coordinates
from `0` to `10000`, not physical room measurements.

```yaml
type: custom:xiaomi-android-vacuum-map
title: Xiaomi Robot Vacuum X20+
entity: vacuum.xiaomi_robot_vacuum_x20
map_image_entity: image.xiaomi_robot_vacuum_x20_map
safe_area:
  x1: 0
  y1: 1254
  x2: 10000
  y2: 7067
```

Named server-side workflows such as `litter_box` are discovered automatically
from the vacuum entity's `known_zones` attribute. Selecting one sends its `id`
as `zone_name`; the visible rectangle is only a helpful preview. Every named or
drawn zone includes the current `map_generation`, which the gateway uses to
reject stale map actions.

## Expected Home Assistant services

The card uses these default services. Both can be overridden with the
`service` and `refresh_map_service` card options.

```yaml
# Preview or start a named or drawn zone
service: xiaomi_android_vacuum.start_zone
data:
  entity_id: vacuum.xiaomi_robot_vacuum_x20
  zone_name: litter_box # or rectangles + map_generation
  map_generation: fresh-generation-from-the-card
  dry_run: false

# Request a new Android/Xiaomi Home screenshot when the phone is free
service: xiaomi_android_vacuum.refresh_map
data:
  entity_id: vacuum.xiaomi_robot_vacuum_x20
```

For a drawn zone, the card sends the equivalent of:

```yaml
service: xiaomi_android_vacuum.start_zone
data:
  entity_id: vacuum.xiaomi_robot_vacuum_x20
  rectangles:
    - x1: 1806
      y1: 5688
      x2: 5046
      y2: 7021
  map_generation: 8e91f4b4-…
  dry_run: false
```

The widget disables requests while Home Assistant reports the phone busy, the
vacuum needs attention, the entity is unavailable, or a custom zone has no
fresh map generation. The gateway remains the authority: it independently
checks phone ownership, app identity, map freshness, zone bounds, and the
actual Xiaomi Home screen before it touches the vacuum.
