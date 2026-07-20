# Sui the Hooverbot

Sui is a Home Assistant custom integration for one safe, repeatable cat-litter
cleanup workflow. It reacts to a genuine MiniNook litter counter increase,
notifies the family, honours a narrow reaction-based skip window, and then
uses the direct Dreame/Xiaomi cloud integration to sweep one explicitly
approved native map rectangle.

The retired Android/Xiaomi Home gesture gateway is intentionally absent. The
generic Android MCP forwarder remains in `gateway/android-mcp-forward` for
unrelated phone-control projects, and the neutral family reaction bridge
remains in `hermes/` because Sui still uses it for notifications and opt-outs.

## Safety model

- A first counter observation establishes a baseline; it cannot trigger an old
  cat visit.
- Daytime events schedule one cleanup after ten minutes plus the configured
  final reaction grace.
- Events from 22:00 inclusive to 06:00 exclusive coalesce into one 06:00 run.
- Sui persists the job before sending a family notification.
- Only ⏭️, ❌, or 🛑 on the exact message can skip the exact job.
- Immediately before motion, Sui refreshes the direct map, checks the bridge
  again, requires a docked/idle error-free vacuum in Sweeping mode, and
  revalidates the approved rectangle.
- A missing/unapproved rectangle, unavailable map/service, ambiguous bridge
  result, vacuum fault, or changed cleaning mode fails closed.
- Once the direct zone call is attempted, an ambiguous result is never retried
  automatically.

The currently proven litter cleanup rectangle is configured in Home Assistant,
not committed to this public repository. Home Assistant stores it only after
an administrator explicitly marks that exact rectangle as visually approved.

## Home Assistant installation

The HACS-compatible component lives at:

```text
custom_components/sui_hooverbot/
```

Install that directory under `/config/custom_components/sui_hooverbot/`,
restart Home Assistant, and add **Sui the Hooverbot** from
**Settings → Devices & services**. Configure:

- the MiniNook counter entity;
- the direct `dreame_vacuum` vacuum entity;
- the direct Dreame current-map camera;
- the native `x0,y0,x1,y1` litter rectangle;
- explicit visual approval of that rectangle;
- the private family bridge URL/token and timing values.

The integration exposes:

- `sensor.sui_the_hooverbot_status`;
- `binary_sensor.sui_the_hooverbot_needs_attention`;
- `sui_hooverbot.skip` for one exact pending job;
- `sui_hooverbot.configure_litter_zone` for an authenticated administrator to
  persist or revoke the fixed rectangle.

See [the component documentation](custom_components/sui_hooverbot/README.md)
for the complete bridge contract and failure semantics.

## Preserved reusable pieces

`gateway/android-mcp-forward` is the generic persistent ADB/MCP recovery loop.
It is intentionally independent of Sui and contains no vacuum coordinates or
Xiaomi cleanup workflow.

`hermes/family_reaction_bridge.py` is a neutral, authenticated notification and
reaction transport. It knows the consumer/job correlation contract but does
not schedule or control physical devices.

## Verification

```sh
python3 -m unittest discover -s tests -v
ruff check custom_components/sui_hooverbot tests/test_sui_hooverbot_model.py
python3 -m py_compile custom_components/sui_hooverbot/*.py
```

The integration uses no LLM or paid API call during normal Home Assistant
operation.
