# Family reaction bridge

This is a staged, neutral Hermes service. It does not contain appliance control,
cleanup timing, or Home Assistant API-token logic.

## Contract

Home Assistant calls `POST /v1/messages` with a Bearer token known only to HA
and this bridge:

```json
{
  "event_key": "opaque-stable-event-key",
  "consumer": "sui_hooverbot",
  "text": "Sui the Hooverbot will vacuum in about 10 minutes. React ⏭️, ❌ or 🛑 to skip this cleanup.",
  "deadline_at": "2026-07-19T15:10:30Z",
  "callback_url": "https://home-assistant.example.com/api/webhook/<unguessable-webhook-id>"
}
```

The bridge sends exactly that text to its configured family chat and persists
the returned WhatsApp message ID. A repeated identical `event_key` is
idempotent; a changed request is rejected. Only a reaction on that exact
message, in the configured chat, using `⏭️`, `❌`, or `🛑`, is accepted. The
bridge then POSTs a small JSON callback to the supplied Home Assistant webhook:

```json
{
  "event_key": "opaque-stable-event-key",
  "consumer": "sui_hooverbot",
  "reaction_event_id": "whatsapp-reaction-id",
  "reaction": "⏭",
  "actor": "family-<one-way-reference>",
  "deadline_at": "2026-07-19T15:10:30Z"
}
```

The webhook URL is restricted to the configured HTTPS Home Assistant origin and
`/api/webhook/` path. It is treated as a secret: the router database is mode
0600 and it is never logged. Hermes stores no Home Assistant long-lived token.

Every callback also includes `X-Family-Reaction-Timestamp` and
`X-Family-Reaction-Signature`. The signature is
`sha256=<HMAC-SHA256>` of the exact bytes
`family-reaction-callback-v1.<timestamp>.<raw JSON body>`, keyed with the
existing dedicated bridge bearer token. The receiving Home Assistant component
checks the signature and a short replay window before it parses the JSON. The
bearer-token value itself is never placed in the callback, database, logs, or
documentation.

`GET /v1/messages/{event_key}` uses the same bridge token and returns only
`event_key`, `consumer`, `status`, and `deadline_at`. HACS must read it just
before physically starting a job and fail closed unless the state is still
`pending`; callback delivery is deliberately not the only safety signal. The
bridge processes newly received reactions before each such status response. If
that intake fails, it returns HTTP 503 so HACS fails closed instead of acting
on a stale `pending` value.

After an exact approved reaction is accepted, the status becomes
`reaction_received`. This is a final bridge decision for that message: the
Home Assistant component must not start the associated physical action.

## Legacy family-alert migration

The optional `FAMILY_REACTION_BRIDGE_LEGACY_INBOXES` creates an isolated
`family_alerts.jsonl` fan-out. During migration, point an existing family-alert
consumer's reaction-log setting at that inbox. It retains its own exact target
message and emoji validation. The bridge contains no dependency on that
consumer’s code or database.

Migration sequence:

1. Stage the bridge script, systemd unit, and a mode-0600 environment file.
2. Change the legacy consumer to read the isolated inbox, but do not restart it
   yet.
3. Start the bridge as the sole reader of the raw WhatsApp reaction JSONL.
4. Verify a harmless test reaction appears in the legacy inbox and is ignored
   unless it matches the consumer’s own registration.

Rollback is safe after the raw router has drained its pending batches: stop the
bridge, restore the legacy consumer's raw-log setting, then start that consumer.
Never delete a raw or pending JSONL batch during migration; replay is safe
because consumers deduplicate reaction event IDs.
