---
name: plplus-pool-spa
description: Operate Andy's PLPLUS pool/spa safely through aqualogic_mqtt. OpenClaw owns calendar timing, preheat calculations, weather checks, and approvals; PLPLUS only executes approved sessions.
---

# PLPLUS Pool/Spa Operations

This bridge-repository copy is a deployment companion. The canonical OpenClaw
skill is `hal9000-v3/skills/plplus/SKILL.md`; keep this file aligned with its
host boundary, endpoints, and safety invariants.

## Ownership boundary

- OpenClaw owns calendar polling, event modifications, preheat planning,
  weather checks, and approval policy.
- Do not ask `aqualogic_mqtt` to poll or interpret a calendar.
- After an approved scheduled/manual spa decision, call the PLPLUS session API.
- Hubitat remains a HomeKit control surface; do not use its pool macros for
  OpenClaw automation.
- Hubitat's momentary Pool, Spa, and Spa Spillover children publish explicit
  semantic mode targets through the existing MQTT topics. They are manual
  controls, not scheduler inputs.

## PLPLUS endpoint

From HAL, use `http://10.40.1.61:8089`. `127.0.0.1` is valid only from a
shell running on the hardware-service host.

Start an approved session:

```bash
curl -fsS -X POST http://10.40.1.61:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"openclaw-<event-id>"}'
```

This requests Spa mode, Filter on, heater relay on, and Auto Heat. The API does
not run a software timer; PLPLUS Spa CountDn provides the hardware failsafe.

Stop when the event ends, is cancelled, or Andy says they are done:

```bash
curl -fsS -X DELETE http://10.40.1.61:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' -d '{}'
```

Read status:

```bash
curl -fsS http://10.40.1.61:8089/api/openclaw/spa
curl -fsS http://10.40.1.61:8089/api/equipment
curl -fsS http://10.40.1.61:8089/api/automation
```

Wait and verify the returned desired state and equipment state. Calendar spa
sessions have priority over manual override, cleanout, and the normal schedule.

## Calendar behavior

- Continue using OpenClaw's existing iCloud calendar configuration.
- Only exact event names `Spa` and `spa` qualify.
- Apply existing weather/approval and preheat rules before activation.
- Scheduled events use `/api/openclaw/spa/prepare` for a five-minute Speed 1 +
  Spillover cleanout immediately before the unchanged preheat time. Manual Spa
  sessions skip this phase.
- Event edits or cancellation must update/stop the PLPLUS session.
- Normally update the calendar only with planned preheat/ready times and the
  first confirmed at-temperature time. One exception update is allowed for a
  fault or heating at least 20% slower than expected.

## Manual controls

- Priority is calendar session, manual override, cleanout, then schedule.
- Timed manual overrides are released together at 03:00 America/New_York;
  Pool Heat and calendar sessions are not part of that reset.
- Pool/Spa targets use `GET /api/heater-targets`, read-only
  `POST /api/heater-targets/refresh`, and explicit
  `POST /api/control/temperature` with `body` and `target_f`.
- Target refresh sends only Menu/Right. It never changes a setpoint. Plus/Minus
  are reserved for an explicit confirmed set request.
- HAL scheduling separately uses `SPA_TARGET_TEMP_F` (default 102°F); do not
  assume a hardware Spa target change updates planned-session timing.
- Aux1 is the spa blower. Aux2 is the heater relay.

## Safety

- Hardware Service mode inhibits writes.
- Never bypass the session API with raw Pool/Spa key sequences.
- When stopping, verify Pool mode and that Auto Heat matches the saved
  `pool_heat_enabled` preference after valve settling.
