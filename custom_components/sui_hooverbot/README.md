# Sui the Hooverbot

Sui is a native Home Assistant custom integration. This workspace stages it
for direct installation beside the Xiaomi component; a dedicated repository
containing only `custom_components/sui_hooverbot/` can later be published as a
HACS integration. It owns the litter-tray trigger, durable countdown, skip
decision, and fixed Xiaomi service calls. The family bridge is only an opaque
message/reaction transport; it never schedules or controls the vacuum.

## Behaviour

1. Sui listens for a real increase in `sensor.mininook_excretion_times_day`.
   Its first healthy observation establishes a baseline, so installing or
   restarting it can never clean for an old cat visit.
2. The integration saves a job through Home Assistant's atomic `Store` before
   it asks the bridge to notify the family.
3. The bridge receives one fixed Sui message. The family can react `⏭️`, `❌`,
   or `🛑` to skip that exact job.
4. At 10 minutes, Sui enters a 30-second configurable reaction grace period.
   Its family message states the actual 10-minute-30-second scheduled start.
   At that time it checks the bridge one final time immediately before the
   only physical zone-start call.
5. If still active, Sui calls only the existing
   `xiaomi_android_vacuum.refresh_map` and `xiaomi_android_vacuum.start_zone`
   services. The latter always uses `zone_name: litter_box`, a fresh map
   generation, and a persisted idempotency key.

The `sensor.sui_the_hooverbot_status` entity shows the schedule state; the
`binary_sensor.sui_the_hooverbot_needs_attention` entity reports an ambiguous
notification, bridge final-check failure, or ambiguous zone start.

## Bridge contract

The integration configures a bridge URL and bearer token in its UI config flow.
For every pending job it calls:

```http
POST /v1/messages
Authorization: Bearer <bridge token>
Content-Type: application/json

{
  "event_key": "sui:<entry-id>:<job-id>",
  "consumer": "sui_hooverbot",
  "text": "...fixed Sui message...",
  "deadline_at": "2026-07-19T12:10:00Z",
  "callback_url": "https://your-ha.example/api/webhook/<opaque-id>"
}
```

`deadline_at` is the hard 10-minute opt-out cutoff; Sui waits through its
short safety grace after that cutoff before its final status check. The bridge
responds with `{"status":"pending"}`. The bridge keeps any WhatsApp message ID and
raw reaction data private. Once it has verified an allowed reaction against
that exact outbound message, it POSTs the callback URL:

```json
{
  "event_key": "sui:<entry-id>:<job-id>",
  "consumer": "sui_hooverbot",
  "reaction_event_id": "unique-bridge-reaction-id",
  "reaction": "⏭️"
}
```

The webhook ID is randomly generated per configuration entry. Sui deduplicates
`reaction_event_id`, accepts only the three skip reactions, and gives no
job-existence details to the callback sender. A trusted local Home Assistant
automation may also fire the `sui_hooverbot_skip` event (with `entry_id`, the
safe `job_id`, `reaction_event_id`, and `reaction`) or call
`sui_hooverbot.skip` with the same fields.

The callback body is authenticated before Sui parses it. The bridge sends
`X-Family-Reaction-Timestamp` and `X-Family-Reaction-Signature` headers; the
signature is HMAC-SHA256 over
`family-reaction-callback-v1.<timestamp>.<raw JSON body>`, using the existing
bridge bearer token as its key. Sui accepts only a matching, fresh callback
(five-minute window), then still deduplicates `reaction_event_id`.

Immediately before it calls `start_zone`, Sui asks the bridge:

```http
GET /v1/messages/<event_key>
```

The bridge must return exactly `pending` to permit dispatch. Its final
`reaction_received` state cancels the job. An unavailable or any other final
bridge state never starts the vacuum. The response must also repeat the exact
job `event_key` and `consumer: sui_hooverbot`; a mismatched response never
starts the vacuum.

## Safe failures

- No bridge acknowledgement: `notification_uncertain`; no retry and no robot
  movement.
- No fresh Xiaomi map or an occupied vacuum: safe retry only before the
  physical request and only within the configured lateness window.
- Job too late: `missed`; never start later.
- Bridge unavailable/ambiguous at final check: `transport_unavailable`; never
  start.
- Once `start_zone` is attempted: any failure becomes `outcome_unknown`; it is
  never retried automatically.

## Direct staged installation

This multi-component workspace is not itself a HACS repository. This folder is
staged for direct installation beside the existing Xiaomi component at:

```text
/config/custom_components/sui_hooverbot/
```

After Home Assistant restarts, add **Sui the Hooverbot** through
**Settings → Devices & services → Add integration**. Its UI form defaults to
the established MiniNook counter and Xiaomi vacuum entity. For HACS, publish a
dedicated repository with this `custom_components/sui_hooverbot/` tree as its
only custom integration, then add that repository in HACS.

## Local verification

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile custom_components/sui_hooverbot/*.py
```
