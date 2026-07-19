# Xiaomi Android Vacuum Bridge

This is a local, deterministic Home Assistant integration for the Xiaomi Robot
Vacuum X20+ when its only practical control surface is Xiaomi Home on a
dedicated Android phone.

It is intentionally not a free-form computer-use agent. The Android gateway
contains one allowlisted workflow (`xiaomi-x20-zone-v1`), named zones, fixed
screen assertions, map freshness checks, a phone mutex, and at-most-once job
handling. Normal Home Assistant operation does not consume an LLM/API call.

## What it provides

- `vacuum.xiaomi_robot_vacuum_x20` with observed Xiaomi state only.
- Connectivity, needs-attention, workflow, last-job, latest-Android-notification,
  and map-image entities.
- `xiaomi_android_vacuum.refresh_map` to capture a current map preview.
- `xiaomi_android_vacuum.start_zone` for one allowlisted named zone or one/two
  previewed XY rectangles.
- A local Lovelace map widget with drag-to-draw, dry-run preview, and a
  two-click start confirmation.
- A Home Assistant persistent notification and event when Xiaomi itself reports
  a stuck/error condition.
- A privacy-filtered Android notification source that interrupts the normal
  five-minute idle poll, records X20+ events in Home Assistant, and never
  exposes notifications from other apps or Xiaomi devices.

The current named routine is `litter_box`. Its coordinates are stored on the
gateway, not trusted from a dashboard request.

## Architecture

```text
Home Assistant custom integration
       │  bearer token + HA container IP allowlist
       ▼
android-vacuum-gateway (phone container, port 8091)
       │  named Android MCP workflow; no prompts
       ▼
Android Remote Control MCP → Xiaomi Home → X20+
```

The gateway accepts only `state`, filtered `notifications`, `map`, and
`zone_clean` requests. A zone job
requires a recently captured map generation, a bounded idempotency key, the
approved X20 map anchors, and a clean-ready Xiaomi screen. It persists audit
state without storing screenshots or bearer tokens.

Passive status reads never disturb an unrelated foreground app. While a
recently observed cleanup is active, the gateway may reopen Xiaomi Home only
from the Android launcher to continue polling; it never does so when another
app is foregrounded.

Copy `gateway/config.example.json` outside the repository, generate a strong
bearer token, and set `ANDROID_SERIAL` for `gateway/android-mcp-forward` to the
dedicated phone's ADB serial. Do not commit the resulting configuration,
tokens, runtime state, screenshots, or audit logs. The supplied systemd unit
expects an unprivileged `android-vacuum` service account.

## Deployment layout

```text
custom_components/xiaomi_android_vacuum/  HACS-ready backend integration
custom_components/sui_hooverbot/           staged direct-install cat-litter scheduler
gateway/                                  systemd Android bridge service
card/xiaomi-android-vacuum-map.js         local dashboard resource
hermes/                                   neutral family-message/reaction transport bridge
```

The backend can be installed as a HACS custom integration after this repository
is published to a GitHub repository. HACS requires a repository it can fetch;
it cannot install the private local working tree directly. The frontend card is
deliberately a standalone local resource for now, because HACS frontend plugins
are packaged as a separate repository.

## Home Assistant configuration

```yaml
xiaomi_android_vacuum:
  - base_url: http://android-vacuum-gateway.local:8091
    token: !secret xiaomi_android_vacuum_gateway_token
```

Add the map card resource through **Settings → Dashboards → Resources**:

```yaml
url: /local/xiaomi-android-vacuum-map.js
type: module
```

Then use this minimal card configuration:

```yaml
type: custom:xiaomi-android-vacuum-map
entity: vacuum.xiaomi_robot_vacuum_x20
map_image_entity: image.xiaomi_robot_vacuum_x20_map
```

Use **Refresh map** before either previewing or starting a zone. The card learns
the allowlisted named zones from the integration, so they are not duplicated in
dashboard YAML.

## Verification

Run the dependency-free gateway tests with:

```sh
python3 -m unittest discover -s tests -v
```

The supplied dry-run path verifies the full Android/Xiaomi screen workflow
without tapping the zone tool or starting the robot.

## Sui the Hooverbot

[Sui the Hooverbot](custom_components/sui_hooverbot/README.md) is a native
Home Assistant custom integration staged for direct installation beside the
Xiaomi component. It persists cat-litter jobs in Home Assistant, offers the
family a reaction-based opt-out through an opaque bridge, and otherwise invokes
only the fixed `litter_box` routine after ten minutes plus a short reaction
grace period. It does not use an LLM. To publish it through HACS later, place
only its component in a dedicated repository.
