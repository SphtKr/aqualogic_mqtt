# AquaLogic MQTT + Web UI (Enhanced)

## System Architecture

```text
 ┌───────────────────────┐
 │   Hayward PL-PLUS     │
 │  (RS-485 Controller)  │
 └─────────┬─────────────┘
           │ RS-485 (via TCP/serial adapter)
           ▼
 ┌───────────────────────┐
 │  aqualogic_mqtt       │
 │  (Python client)      │
 │                       │
 │ - Parses RS-485 data  │
 │ - Publishes MQTT      │
 │ - Hosts Web UI (Flask)│
 └───────┬─────┬─────────┘
         │     │
   MQTT  │     │  HTTP (Web UI + API)
         ▼     ▼
 ┌─────────────┐     ┌─────────────────────┐
 │ Mosquitto   │     │ Web Browser (UI)    │
 │ (Broker)    │     │ - LCD Display       │
 │             │     │ - Menu/Arrow/+/–    │
 │             │     │ - Filter button     │
 └─────────────┘     └─────────────────────┘
         │
         ▼
 ┌─────────────────────┐
 │ Home Assistant /    │
 │ Other MQTT Clients  │
 └─────────────────────┘
```

---

## Overview
This fork extends the original [`swilson/aqualogic`](https://github.com/swilson/aqualogic) project with:

- Embedded **Flask web UI** (`webapp.py`) for live panel display and keypress control.
- **Five navigation buttons** (Menu, Left, Right, Plus, Minus) in the UI + new **Filter** button.
- Mobile-optimized web UI (`static/index.html`) — designed for iPhone 16 Pro and responsive devices.
- Real **RS-485 LCD display text** streamed into the web UI via `panelmanager.text_updated`.
- Cleaned up **fallback suppression** in `client.py` so only live panel content shows.
- MQTT integration (unchanged from original) for state publishing and command control.

---

## File Changes

### `aqualogic_mqtt/client.py`
- Hooks into `controls.register_with_panel(self._panel)`.
- Updated `_panel_changed`:
  - Suppresses synthetic fallback display (POOL/TA/Salt).
  - Only updates UI when real LCD lines are present.
- Improved debug logging for LCD attributes.
- Fixed `logger.debug` bug (`name 'k' is not defined`).

### `aqualogic_mqtt/panelmanager.py`
- `text_updated()` now:
  - Forwards actual RS-485 display frames directly to the web UI.
  - Collapses each frame into a **single line string**.
- Keeps last-seen LCD line so UI always reflects what the panel is cycling.

### `aqualogic_mqtt/controls.py`
- Added centralized **DisplayState** and API helpers (`update_display`, `get_display`).
- Implemented **keypress queue** (`enqueue_key`, `drain_keypresses`).
- Added `_KEY_MAP` with support for:
  - `menu`, `left`, `right`, `plus`, `minus`
  - **`filter`** → `Keys.FILTER`
- Backend now recognizes `/api/key/filter` requests.

### `aqualogic_mqtt/webapp.py`
- Flask app serves:
  - `/api/display` → JSON LCD state.
  - `/api/key/<key>` → queues + drains keypress immediately.
- Logs POSTs clearly so you can confirm in `journalctl`.
- Static files (web UI) served from `/`.

### `aqualogic_mqtt/static/index.html`
- Rebuilt to mimic the original AquaLogic web panel look.
- Layout:
  - LCD-style green display window.
  - Navigation pad (Menu + arrows + +/-) **left aligned**.
  - **Filter** button added on the right.
- Responsive/mobile tweaks:
  - `viewport-fit=cover` and safe-area padding (handles iPhone Dynamic Island).
  - `100svh` + JS fallback to handle iOS Safari viewport height bugs.
  - Buttons sized ≥44×44px (Apple HIG).
  - Collapses display lines into one string, collapses extra spaces.
  - On very narrow screens, stacks Filter button under nav pad.

---

## Running as a Service

Systemd unit (`/etc/systemd/system/aqualogic_mqtt.service`):

```ini
[Service]
ExecStart=/home/andy/aqualogic_mqtt/venv-pool/bin/python   -m aqualogic_mqtt.client   -t 10.40.1.78:26   -m 10.40.1.61:1883   -e t_a t_p t_s cl_p cl_s salt s_p p_p l f aux1 aux2 aux3 aux4 spill v3 v4 h1 hauto sc pool spa   --http-host 0.0.0.0 --http-port 8089 -vvv
WorkingDirectory=/home/andy/aqualogic_mqtt
Restart=always
```

Reload + restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart aqualogic_mqtt
```

Check logs:

```bash
journalctl -u aqualogic_mqtt -f
```

---

## MQTT Broker (Mosquitto)

`/etc/mosquitto/mosquitto.conf`:

```
listener 1883 0.0.0.0
allow_anonymous true
log_dest file /var/log/mosquitto/mosquitto.log
persistence true
persistence_location /var/lib/mosquitto/
```

Sample subscription:

```bash
mosquitto_sub -h 10.40.1.61 -p 1883 -v -t 'homeassistant/#' -t 'aqualogic/#'
```

---

## Troubleshooting

### Web UI
- **Blank display** → Check `/api/display` directly:
  ```bash
  curl http://10.40.1.61:8089/api/display
  ```
  If `lines` are empty, ensure `panelmanager.text_updated` is firing.

- **Buttons do nothing**:
  - Confirm POST works:
    ```bash
    curl -X POST http://10.40.1.61:8089/api/key/menu
    ```
    Should return `{"ok":true,"key":"menu"}`.
  - Tail logs:
    ```bash
    journalctl -u aqualogic_mqtt -f | grep "queued key"
    ```
    You should see `queued key menu` then `sent 1 key(s)`.

- **Filter button not working**:
  - Ensure `_KEY_MAP` includes `"filter": Keys.FILTER`.
  - Hardware may expect `Keys.FILTER_PUMP` instead. If so, update mapping.

### Service won’t start
- Run manually in venv for immediate feedback:
  ```bash
  source ~/aqualogic_mqtt/venv-pool/bin/activate
  python -m aqualogic_mqtt.client -t 10.40.1.78:26 -m 10.40.1.61:1883 --http-port 8089 -vvv
  ```

- Look for:
  - Serial connection errors (`tcp host:port` mismatch).
  - MQTT broker connection failures.

### Display truncated horizontally
- The JS `collapseLines` function strips double-spaces:
  ```js
  const text = parts.join(' ').replace(/\s{2,}/g, ' ');
  ```
  If content still looks cut off, tweak `.display { min-width: … }` in CSS.

---

## Quick Test Checklist
1. `curl /api/display` shows live controller text (not synthetic fallback).
2. Web UI display matches pool panel LCD.
3. Buttons (`Menu, Left, Right, Plus, Minus, Filter`) send keys — confirmed in logs.
4. MQTT broker publishes state to `homeassistant/device/aqualogic/state`.
5. Service restarts cleanly via systemd.

---

## No-Power-Cycle VSP Driver

The bridge includes a commissioned VSP driver that edits the currently active
Filter Speed preset through the PL-PLUS Settings menu. PL-PLUS then owns and
continuously broadcasts the resulting speed. The driver does **not** press
Filter and does not turn the pump on or off. At lease expiry it restores the
original preset percentage automatically.

The driver is disabled by default until it has passed live commissioning:

```bash
export AQUALOGIC_VSP_CONTROL=1
```

For temporary commissioning without changing the service definition, create
the local interlock file and delete it at any time to clear the active target:

```bash
touch /home/andy/aqualogic_mqtt/.vsp-control-enabled
rm -f /home/andy/aqualogic_mqtt/.vsp-control-enabled
```

With the service restarted, inspect status and request a short leased preset:

```bash
curl http://10.40.1.61:8089/api/vsp
curl -X POST http://10.40.1.61:8089/api/vsp/speed \
  -H 'Content-Type: application/json' \
  -d '{"preset":"speed1","lease_seconds":60}'
curl -X DELETE http://10.40.1.61:8089/api/vsp/speed
```

Safety behavior:

- Requests are rejected while the Filter LED is off or Service mode is on.
- On service startup, a scheduled percentage already reported by PL-PLUS is
  adopted in memory without opening the Settings menu. Continued observation
  still invokes the VSP driver if that request later drifts from schedule. If
  the initial speed has not arrived yet, automation waits rather than treating
  an unknown value as a mismatch. A persisted rollback journal whose target
  matches both the observed request and desired speed is treated as the durable
  record of that active lease and is also adopted without menu navigation.
  Journal recovery is explicitly started only when those values do not match.
- The host never starts or estimates a priming interval. When PL-PLUS reports
  `Prime`, `Priming`, or `Start Delay` on its live display/default-menu state,
  automation pauses all reconciliation and resumes after that hardware-owned
  indication clears.
- Commissioning leases default to 60 seconds and are capped at 15 minutes.
- The driver never falls back to the older Filter off/on speed-change helper.

Before enabling any scheduler, live commissioning must verify each transition
between 40%, 55%, 70%, and 95%, confirm that the Filter LED never turns off,
confirm that no start-delay/prime screen appears, and confirm the observed
pump speed remains stable for ten minutes.

## Host-Owned PL-PLUS Automation

The host scheduler and clock synchronization are disabled by default. They use
separate local interlock files so deployment and activation are distinct:

```bash
touch /home/andy/aqualogic_mqtt/.vsp-control-enabled
touch /home/andy/aqualogic_mqtt/.automation-control-enabled
```

Remove the automation interlock to stop new scheduled commands. Remove the VSP
interlock to cancel and restore an active temporary preset edit.

The recurring schedule is interpreted in `America/New_York`:

- Speed 4: 00:00–08:00 (40%)
- Speed 1: 08:00–10:00 (70%)
- Speed 2: 10:00–11:00 (95%)
- Speed 3: 11:00–00:00 (55%)
- Cleanout: 09:00–10:30 in Spillover mode
- Pool↔Spillover transitions preserve the active pump speed and change only
  valve mode. Pool→Spillover sends the two mode selections 500 ms apart without
  pausing in Spa, then allows 15 seconds to confirm only the final Spillover
  state.
- Any uncovered pump interval falls back to Speed 4 while Service mode is off.

Resolution order is calendar spa session, manual override, cleanout, then the
normal pump schedule. Hardware Service mode inhibits every automation write.
Calendar spa mode suppresses Filter Speed edits because PL-PLUS owns the
separate Spa Speed preset.

Filter on/off is part of the resolved desired state. Normal schedule,
cleanout, and calendar Spa sessions require Filter on. A manual Filter-off
override suppresses speed edits until it expires or is cleared. After every
normal mode transition, the host confirms the requested mode and allows 35
seconds for valve motion to settle before issuing another mode or Filter
command. Pool→Spillover is the deliberate exception: its two required
selections are sent 500 ms apart, then only final Spillover is confirmed and
settled.

Manual web/API changes become 12-hour persisted overrides whenever automation
is enabled, except Pool Heat. Pool Heat is a durable preference that remains
enabled until explicitly turned off and is restored after a Spa session.
At 03:00 America/New_York each day, the host releases the entire timed manual
override and resumes the next applicable priority source. This checkpoint is
persisted by local calendar date, runs once through the repeated fall-DST hour,
and catches up after a service outage. Using 03:00 avoids the repeated/missing
DST transition hours. It does not clear an active OpenClaw Spa
session or the durable Pool Heat preference.
Inspect the complete resolved state, local/UTC conversion, clock sync status,
and active priority source at `/api/automation`.
`/api/equipment` reports the PL-PLUS mode, equipment outputs, and the live
`heater_running` LED state used by HAL for heating confirmation and health.
Auto Heat is not reconciled until the PL-PLUS Heater1 display page confirms its
mode; the upstream library's unconfirmed startup default is never allowed to
generate a heater keypress for non-calendar sources (`3b254c6`). Calendar Spa
preheat/start requests are the explicit exception (`9ca394e`): they always
command Auto Heat on, even before `auto_heat_confirmed` is true. An accepted
Auto Heat command is issued once and held pending until the Heater1 page
confirms it, preventing repeated toggle keys.

The WebUI globally disables navigation and semantic controls only while
automation owns the PL-PLUS LCD menu, such as active VSP menu work, heater
target reads/writes, or clock synchronization. Direct mode and equipment
commands do not globally lock the interface. A scheduled VSP lease in the
stable `holding` phase also does not lock the controls.

The Pump On button reflects and controls the PL-PLUS Filter relay independently
of the selected speed preset. Pool and Spa target temperatures are cached under
Equipment. The cache is populated passively whenever a scheduled speed change
visits the two Heater1 pages and by a read-only scan after startup confirms an
already-matching pump speed. Clicking a Pool or Spa target first scans that
specific current hardware setting, then opens the +/− editor; only Confirm
performs a serialized Settings-menu write. OpenClaw can use the same API
directly:

```bash
curl http://127.0.0.1:8089/api/heater-targets
curl -X POST http://127.0.0.1:8089/api/heater-targets/scan \
  -H 'Content-Type: application/json' -d '{"body":"spa"}'
curl -X POST http://127.0.0.1:8089/api/control/temperature \
  -H 'Content-Type: application/json' -d '{"body":"spa","target_f":102}'
```

Numeric targets are restricted to the PL-PLUS range of 65–104°F. Hardware
Service mode and concurrent LCD-menu work inhibit reads and writes.
Target scans are strictly read-only: they use only Menu/Right navigation and
never send Plus or Minus. The full `POST /api/heater-targets/refresh` endpoint
remains available for compatibility, but there is no separate Refresh Targets
button. The driver changes a target only after an explicit confirmed WebUI edit
or `POST /api/control/temperature`. A displayed
`Manual Off` is a heater operating-state observation and must not be described
as proof that the stored numeric setpoint was erased; use `observed_at_utc` and
the raw display when diagnosing that case.
OpenClaw's scheduled-Spa target is separately configured by
`SPA_TARGET_TEMP_F` (default 102°F); changing the PL-PLUS hardware target does
not automatically change HAL's planning model.

When a mode, speed, Resume Schedule, or equipment button is clicked, the action
turns orange immediately and any superseded green selection clears. A pending
direct command disables only its selected button; speed/menu work retains the
global lock until completion. Mode, speed, and equipment selections then show
their confirmed state in green; Resume
Schedule is momentary and returns to neutral. Failed or timed-out commands
restore the confirmed hardware presentation instead of leaving a false active
state. Resume Schedule clears the complete timed manual override (mode, speed,
Filter, Lights, Blower, and Heater Relay fields), not only its pump-speed field;
durable Pool Heat and calendar Spa sessions are separate state and are retained.

Existing Home Assistant/Hubitat discovery IDs and command topics are unchanged.
The Hubitat parent driver also creates three momentary component buttons named
Pool, Spa, and Spa Spillover. Each publishes `ON` to the corresponding existing
mode switch topic, so it requests an explicit target rather than replaying the
raw Pool/Spa toggle. Existing Pool/Spa/Spillover state children remain in place
for compatibility. Saving or reinitializing the updated parent driver creates
the new button children.
While host automation is enabled, Filter, Lights, Aux1/Blower, Aux2/Heater
Relay, Auto Heat, Pool, Spa, and Spa Spillover commands on those topics remain
supported.
Auto Heat updates the durable Pool Heat preference; the others become persisted
manual overrides, preserving HomeKit control without bypassing priority or
Service-mode interlocks. Mode topics always use the semantic mode controller,
including when host automation is disabled, so Spillover retains its guarded
transition and confirmation behavior. Other disabled-automation commands retain
the legacy direct MQTT path.

Once weekly, normal-schedule operation compares the cached PL-PLUS clock with
the system clock. A difference of one minute or more triggers a guarded
Settings-menu update. The check and successful sync timestamps are persisted
in UTC in `.clock-sync-state.json`.

The PL-PLUS `Spa CountDn` setting is a Configuration-menu hardware safeguard,
not the Aux1 blower countdown. Set/audit it separately at 12:00; the host's
manual override lifetime is also 12 hours.

### OpenClaw spa-session control

OpenClaw remains responsible for calendar schedules, preheat calculations,
weather checks, and approvals. `aqualogic_mqtt` does not poll any calendar.
After approval, OpenClaw activates a top-priority spa session:

```bash
curl -X POST http://127.0.0.1:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"openclaw-example"}'
```

Activation requests Spa mode, Filter on, Aux2 heater relay on, and Auto Heat
on. Calendar Spa activation bypasses only the unconfirmed Auto Heat startup
guard added in `3b254c6`, as fixed by `9ca394e`; WebUI, MQTT, manual, and Pool
Heat requests still wait for Heater1 confirmation. The software session has no
timer; OpenClaw must stop it explicitly. The PL-PLUS Spa CountDn remains the
independent hardware failsafe.

Calendar events may first arm the same stable session ID with an exact
five-minute preparation window:

```bash
curl -X POST http://127.0.0.1:8089/api/openclaw/spa/prepare \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"openclaw-example","prep_start_utc":"2026-06-27T19:55:00Z","preheat_start_utc":"2026-06-27T20:00:00Z"}'
```

Before `prep_start_utc` the armed plan does not override normal automation.
From prep start until preheat start it has calendar priority and requests
Speed 1, Spillover, and Filter on without changing heater outputs. At the exact
preheat timestamp it resolves to the normal Spa/heat state, even if HAL's next
60-second tick has not yet run. A later start POST is idempotent. Manual
sessions use only the normal endpoint and enter Spa immediately without preparation.

```bash
curl -X DELETE http://127.0.0.1:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' -d '{}'
```

Stopping returns to the lower-priority manual/cleanout/schedule state and
restores the saved Pool Heat preference.

OpenClaw normally updates the calendar event twice: when planned preheat/ready
times are first calculated, and when the configured set temperature is first
confirmed. It may add one exception update for a heating fault or a measured
rate at least 20% slower than expected. Routine start, active, and completion
ticks do not rewrite the event.
