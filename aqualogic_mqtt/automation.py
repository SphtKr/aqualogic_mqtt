"""Priority and time resolution for host-owned PL-PLUS automation.

All persisted timestamps are UTC. Recurring pool schedules are interpreted in
America/New_York so the intended wall-clock behavior survives DST changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from threading import Lock
from typing import Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .vsp import PRESET_SPEEDS


LOCAL_TIMEZONE = ZoneInfo("America/New_York")
UTC = timezone.utc
DAILY_MANUAL_RELEASE_TIME = time(3, 0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return parse_utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PumpWindow:
    start: time
    end: time
    preset: str

    def contains(self, local_time: time) -> bool:
        current = (local_time.hour, local_time.minute, local_time.second)
        start = (self.start.hour, self.start.minute, self.start.second)
        end = (self.end.hour, self.end.minute, self.end.second)
        if start < end:
            return start <= current < end
        return current >= start or current < end


DEFAULT_PUMP_SCHEDULE = (
    PumpWindow(time(0, 0), time(8, 0), "speed4"),
    PumpWindow(time(8, 0), time(10, 0), "speed1"),
    PumpWindow(time(10, 0), time(11, 0), "speed2"),
    PumpWindow(time(11, 0), time(0, 0), "speed3"),
)


@dataclass(frozen=True)
class ManualOverride:
    expires_utc: datetime
    mode: Optional[str] = None
    pump_preset: Optional[str] = None
    auto_heat: Optional[bool] = None
    heater_relay: Optional[bool] = None
    lights: Optional[bool] = None
    blower: Optional[bool] = None
    filter_on: Optional[bool] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_utc", parse_utc(self.expires_utc))
        if self.mode is not None and self.mode not in ("pool", "spa", "spillover"):
            raise ValueError("manual mode must be pool, spa, or spillover")
        if self.pump_preset is not None and self.pump_preset not in {
            "speed1", "speed2", "speed3", "speed4"
        }:
            raise ValueError("manual pump preset must be speed1, speed2, speed3, or speed4")
        if self.filter_on is not None and not isinstance(self.filter_on, bool):
            raise ValueError("manual filter_on must be a boolean or null")

    def active_at(self, now_utc: datetime) -> bool:
        return parse_utc(now_utc) < self.expires_utc


@dataclass(frozen=True)
class DesiredState:
    source: str
    mode: str
    pump_preset: Optional[str]
    filter_on: bool = True
    suppress_filter_speed: bool = False
    session_id: Optional[str] = None
    session_phase: Optional[str] = None
    switches: Mapping[str, Optional[bool]] = field(default_factory=dict)


class ScheduleResolver:
    """Resolve: calendar spa > manual override > cleanout > pump schedule."""

    def __init__(
        self,
        *,
        timezone_: ZoneInfo = LOCAL_TIMEZONE,
        pump_schedule: Sequence[PumpWindow] = DEFAULT_PUMP_SCHEDULE,
        cleanout_start: time = time(9, 0),
        cleanout_end: time = time(10, 30),
        fallback_preset: str = "speed4",
    ):
        self.timezone = timezone_
        self.pump_schedule = tuple(pump_schedule)
        self.cleanout_start = cleanout_start
        self.cleanout_end = cleanout_end
        self.fallback_preset = fallback_preset

    def scheduled_preset(self, local_time: time) -> str:
        for window in self.pump_schedule:
            if window.contains(local_time):
                return window.preset
        return self.fallback_preset

    def _cleanout_active(self, local_time: time) -> bool:
        current = (local_time.hour, local_time.minute, local_time.second)
        start = (self.cleanout_start.hour, self.cleanout_start.minute, self.cleanout_start.second)
        end = (self.cleanout_end.hour, self.cleanout_end.minute, self.cleanout_end.second)
        return start <= current < end

    @staticmethod
    def _switches(source: object) -> dict[str, Optional[bool]]:
        return {
            name: getattr(source, name)
            for name in ("auto_heat", "heater_relay", "lights", "blower")
            if getattr(source, name) is not None
        }

    def resolve(
        self,
        now_utc: datetime,
        *,
        manual_override: Optional[ManualOverride] = None,
        openclaw_spa_session: Optional[Mapping[str, object]] = None,
        pool_heat_enabled: bool = False,
    ) -> DesiredState:
        now = parse_utc(now_utc)
        local = now.astimezone(self.timezone)
        scheduled = self.scheduled_preset(local.timetz().replace(tzinfo=None))

        if openclaw_spa_session is not None:
            session_phase = str(openclaw_spa_session.get("phase") or "spa")
            if session_phase == "scheduled":
                prep_start = parse_utc(openclaw_spa_session.get("prep_start_utc"))
                preheat_start = parse_utc(openclaw_spa_session.get("preheat_start_utc"))
                if now < prep_start:
                    # The one-shot calendar plan is armed but must not affect
                    # lower-priority equipment state before its exact start.
                    openclaw_spa_session = None
                elif now < preheat_start:
                    return DesiredState(
                        source="calendar",
                        mode="spillover",
                        pump_preset="speed1",
                        filter_on=True,
                        session_id=str(openclaw_spa_session.get("session_id") or "openclaw"),
                        session_phase="prep",
                        switches={},
                    )

        if openclaw_spa_session is not None:
            return DesiredState(
                source="calendar",
                mode="spa",
                pump_preset=None,
                filter_on=True,
                suppress_filter_speed=True,
                session_id=str(openclaw_spa_session.get("session_id") or "openclaw"),
                session_phase="spa",
                switches={"auto_heat": True, "heater_relay": True},
            )

        cleanout = self._cleanout_active(local.timetz().replace(tzinfo=None))
        base_mode = "spillover" if cleanout else "pool"
        base_source = "cleanout" if cleanout else "schedule"
        if manual_override is not None and manual_override.active_at(now):
            mode = manual_override.mode or base_mode
            pump = manual_override.pump_preset or scheduled
            filter_on = True if manual_override.filter_on is None else manual_override.filter_on
            suppress = mode == "spa" or not filter_on
            switches = self._switches(manual_override)
            # Pool heat is a durable user preference rather than a timed
            # manual override. Calendar Spa sessions still take precedence.
            switches["auto_heat"] = pool_heat_enabled
            return DesiredState(
                source="manual",
                mode=mode,
                pump_preset=None if suppress else pump,
                filter_on=filter_on,
                suppress_filter_speed=suppress,
                switches=switches,
            )

        return DesiredState(
            source=base_source,
            mode=base_mode,
            pump_preset=scheduled,
            filter_on=True,
            switches={"auto_heat": pool_heat_enabled},
        )


def desired_state_dict(state: DesiredState) -> dict:
    return asdict(state)


def _is_calendar_spa_auto_heat_request(desired: DesiredState) -> bool:
    return desired.source == "calendar" and desired.session_phase == "spa"


class AutomationEngine:
    """Persist desired inputs and reconcile one safe hardware action per tick."""

    def __init__(
        self,
        equipment: object,
        vsp: object,
        *,
        enabled: bool = False,
        enable_file: Optional[str] = None,
        state_file: Optional[str] = ".automation-state.json",
        resolver: Optional[ScheduleResolver] = None,
        now: Callable[[], datetime] = utc_now,
        speed_lease_seconds: float = 90.0,
        manual_duration_seconds: float = 12 * 60 * 60,
        clock_sync: Optional[object] = None,
        heater_targets: Optional[object] = None,
    ):
        self._equipment = equipment
        self._vsp = vsp
        self._enabled = bool(enabled)
        self._enable_file = str(enable_file) if enable_file else None
        self._state_file = str(state_file) if state_file else None
        self._resolver = resolver or ScheduleResolver()
        self._now = now
        self._speed_lease_seconds = float(speed_lease_seconds)
        self._manual_duration_seconds = float(manual_duration_seconds)
        self._clock_sync = clock_sync
        self._heater_targets = heater_targets
        self._lock = Lock()
        self._tick_lock = Lock()
        self._manual_override: Optional[ManualOverride] = None
        self._openclaw_spa_session: Optional[dict] = None
        self._pool_heat_enabled = False
        self._phase = "disabled"
        self._last_error: Optional[str] = None
        self._last_tick_utc: Optional[datetime] = None
        self._desired: Optional[DesiredState] = None
        self._startup_target_scan_attempted = False
        self._last_manual_release_local_date = self._initial_manual_release_date()
        self._load()

    def _initial_manual_release_date(self) -> date:
        """Migration-safe baseline that never clears an override at upgrade time."""
        local_now = parse_utc(self._now()).astimezone(self._resolver.timezone)
        if local_now.timetz().replace(tzinfo=None) >= DAILY_MANUAL_RELEASE_TIME:
            return local_now.date()
        return local_now.date() - timedelta(days=1)

    def is_enabled(self) -> bool:
        return self._enabled or bool(self._enable_file and os.path.isfile(self._enable_file))

    def hardware_busy(self) -> bool:
        return bool(
            (self._clock_sync is not None and self._clock_sync.is_busy())
            or (self._heater_targets is not None and self._heater_targets.is_busy())
        )

    def _scan_targets_after_startup_speed_confirmation(self) -> bool:
        """Read targets once when startup adopts an already-matching speed."""
        if self._heater_targets is None or self._startup_target_scan_attempted:
            return False
        target_status = self._heater_targets.status()
        observed = target_status.get("observed_since_startup") or {}
        if all(observed.get(body) is True for body in ("pool", "spa")):
            self._startup_target_scan_attempted = True
            return False
        self._startup_target_scan_attempted = True
        self._heater_targets.request_refresh()
        with self._lock:
            self._phase = "scanning_startup_heater_targets"
            self._last_error = None
        return True

    @staticmethod
    def _manual_dict(manual: ManualOverride) -> dict:
        return {
            "expires_utc": format_utc(manual.expires_utc),
            **{
                name: getattr(manual, name)
                for name in ("mode", "pump_preset", "auto_heat", "heater_relay", "lights", "blower", "filter_on")
                if getattr(manual, name) is not None
            },
        }

    @staticmethod
    def _manual_from_dict(value: Mapping[str, object]) -> ManualOverride:
        kwargs = {
            name: value.get(name)
            for name in ("mode", "pump_preset", "auto_heat", "heater_relay", "lights", "blower", "filter_on")
        }
        for name in ("auto_heat", "heater_relay", "lights", "blower", "filter_on"):
            if kwargs[name] is not None and not isinstance(kwargs[name], bool):
                raise ValueError(f"manual {name} must be a boolean or null")
        return ManualOverride(expires_utc=parse_utc(value.get("expires_utc")), **kwargs)

    def _load(self) -> None:
        if not self._state_file or not os.path.isfile(self._state_file):
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            manual_data = dict(payload.get("manual_override") or {})
            saved_pool_heat = payload.get("pool_heat_enabled")
            if saved_pool_heat is None:
                # Migrate a legacy timed Auto Heat override to the durable
                # pool-heating preference on first load.
                saved_pool_heat = manual_data.get("auto_heat", False)
            if not isinstance(saved_pool_heat, bool):
                raise ValueError("pool_heat_enabled must be a boolean")
            manual_data.pop("auto_heat", None)
            manual_fields = ("mode", "pump_preset", "heater_relay", "lights", "blower", "filter_on")
            manual = (
                self._manual_from_dict(manual_data)
                if manual_data and any(manual_data.get(name) is not None for name in manual_fields)
                else None
            )
            openclaw_spa = payload.get("openclaw_spa_session")
            saved_release_date = payload.get("last_manual_release_local_date")
            if saved_release_date is not None:
                parsed_release_date = date.fromisoformat(str(saved_release_date))
            else:
                # Existing state files predate the daily-release feature. Mark
                # today's checkpoint complete rather than unexpectedly
                # clearing a live override during deployment.
                parsed_release_date = self._initial_manual_release_date()
            if openclaw_spa:
                raw_openclaw_spa = dict(openclaw_spa)
                phase = str(openclaw_spa.get("phase") or "spa")
                if phase not in ("spa", "scheduled"):
                    raise ValueError(f"invalid OpenClaw spa session phase: {phase}")
                openclaw_spa = {
                    "session_id": str(openclaw_spa.get("session_id") or "openclaw"),
                    "started_utc": format_utc(parse_utc(openclaw_spa.get("started_utc"))),
                    "phase": phase,
                }
                if phase == "scheduled":
                    prep_start = parse_utc(payload["openclaw_spa_session"].get("prep_start_utc"))
                    preheat_start = parse_utc(payload["openclaw_spa_session"].get("preheat_start_utc"))
                    if prep_start >= preheat_start:
                        raise ValueError("OpenClaw prep start must precede preheat start")
                    openclaw_spa.update({
                        "prep_start_utc": format_utc(prep_start),
                        "preheat_start_utc": format_utc(preheat_start),
                    })
                elif raw_openclaw_spa.get("spa_started_utc"):
                    openclaw_spa["spa_started_utc"] = format_utc(
                        parse_utc(raw_openclaw_spa.get("spa_started_utc"))
                    )
            with self._lock:
                self._manual_override = manual
                self._openclaw_spa_session = openclaw_spa
                self._pool_heat_enabled = saved_pool_heat
                self._last_manual_release_local_date = parsed_release_date
                if saved_release_date is None:
                    self._save_locked()
        except Exception as exc:
            self._last_error = f"automation state load failed: {exc}"

    def _save_locked(self) -> None:
        if not self._state_file:
            return
        payload = {
            "version": 3,
            "updated_at_utc": format_utc(self._now()),
            "pool_heat_enabled": self._pool_heat_enabled,
            "manual_override": (
                self._manual_dict(self._manual_override) if self._manual_override is not None else None
            ),
            "openclaw_spa_session": self._openclaw_spa_session,
            "last_manual_release_local_date": self._last_manual_release_local_date.isoformat(),
        }
        parent = os.path.dirname(os.path.abspath(self._state_file))
        os.makedirs(parent, exist_ok=True)
        temp_path = f"{self._state_file}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._state_file)

    def activate_openclaw_spa(
        self,
        *,
        session_id: Optional[str] = None,
        phase: str = "spa",
        prep_start_utc: Optional[str] = None,
        preheat_start_utc: Optional[str] = None,
    ) -> dict:
        if not self.is_enabled():
            raise RuntimeError("host automation is disabled; refusing to queue an OpenClaw spa session")
        now = parse_utc(self._now())
        requested_id = str(session_id or format_utc(now))
        identifier = requested_id if requested_id.startswith("openclaw") else f"openclaw-{requested_id}"
        session_phase = str(phase or "spa").strip().lower()
        if session_phase not in ("spa", "scheduled"):
            raise ValueError("OpenClaw spa phase must be spa or scheduled")

        prep_start = None
        preheat_start = None
        if session_phase == "scheduled":
            if prep_start_utc is None or preheat_start_utc is None:
                raise ValueError("scheduled Spa preparation requires prep_start_utc and preheat_start_utc")
            prep_start = parse_utc(prep_start_utc)
            preheat_start = parse_utc(preheat_start_utc)
            if prep_start >= preheat_start:
                raise ValueError("prep_start_utc must precede preheat_start_utc")

        with self._lock:
            existing = self._openclaw_spa_session or {}
            started_utc = (
                existing.get("started_utc")
                if existing.get("session_id") == identifier
                else format_utc(now)
            )
            session = {
                "session_id": identifier,
                "started_utc": started_utc,
                "phase": session_phase,
            }
            if session_phase == "scheduled":
                session.update({
                    "prep_start_utc": format_utc(prep_start),
                    "preheat_start_utc": format_utc(preheat_start),
                })
            else:
                session["spa_started_utc"] = format_utc(now)
            self._openclaw_spa_session = session
            self._save_locked()
        return self.status()

    def stop_openclaw_spa(self, session_id: Optional[str] = None) -> dict:
        identifier = str(session_id) if session_id else None
        if identifier is not None and not identifier.startswith("openclaw"):
            identifier = f"openclaw-{identifier}"
        with self._lock:
            if identifier is None or (
                self._openclaw_spa_session is not None
                and self._openclaw_spa_session.get("session_id") == identifier
            ):
                self._openclaw_spa_session = None
            self._save_locked()
        return self.status()

    def set_manual(self, **updates: object) -> dict:
        marker = object()
        pool_heat = updates.pop("auto_heat", marker)
        pool_heat_supplied = pool_heat is not marker
        if pool_heat_supplied and not isinstance(pool_heat, bool):
            raise ValueError("auto_heat must be a boolean")
        allowed = {"mode", "pump_preset", "heater_relay", "lights", "blower", "filter_on"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported manual fields: {', '.join(sorted(unknown))}")
        now = parse_utc(self._now())
        with self._lock:
            if pool_heat_supplied:
                self._pool_heat_enabled = pool_heat
            current = self._manual_override
            if updates:
                fields = {}
                if current is not None and current.active_at(now):
                    fields = {
                        name: getattr(current, name)
                        for name in allowed
                        if getattr(current, name) is not None
                    }
                fields.update(updates)
                self._manual_override = ManualOverride(
                    expires_utc=now + timedelta(seconds=self._manual_duration_seconds),
                    **fields,
                )
            self._save_locked()
        return self.status()

    def set_pool_heat(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("pool heat target must be a boolean")
        with self._lock:
            self._pool_heat_enabled = enabled
            self._save_locked()
        return self.status()

    def clear_manual(self, field: Optional[str] = None) -> dict:
        allowed = {"mode", "pump_preset", "auto_heat", "heater_relay", "lights", "blower", "filter_on"}
        if field is not None and field not in allowed:
            raise ValueError(f"unsupported manual field: {field}")
        with self._lock:
            if field == "auto_heat":
                self._pool_heat_enabled = False
            current = self._manual_override
            if field is None or current is None:
                self._manual_override = None
            else:
                fields = {
                    name: getattr(current, name)
                    for name in allowed
                    if name != field and getattr(current, name) is not None
                }
                self._manual_override = (
                    ManualOverride(expires_utc=current.expires_utc, **fields) if fields else None
                )
            self._save_locked()
        return self.status()

    def _inputs(self) -> tuple[Optional[ManualOverride], Optional[dict], bool]:
        with self._lock:
            return (
                self._manual_override,
                dict(self._openclaw_spa_session) if self._openclaw_spa_session else None,
                self._pool_heat_enabled,
            )

    def _release_manual_for_daily_checkpoint(self, now: datetime) -> bool:
        local_now = parse_utc(now).astimezone(self._resolver.timezone)
        if local_now.timetz().replace(tzinfo=None) < DAILY_MANUAL_RELEASE_TIME:
            return False
        with self._lock:
            if self._last_manual_release_local_date >= local_now.date():
                return False
            released = self._manual_override is not None
            self._manual_override = None
            self._last_manual_release_local_date = local_now.date()
            self._save_locked()
            return released

    def status(self) -> dict:
        now = parse_utc(self._now())
        manual, openclaw_spa, pool_heat_enabled = self._inputs()
        desired = self._resolver.resolve(
            now,
            manual_override=manual,
            openclaw_spa_session=openclaw_spa,
            pool_heat_enabled=pool_heat_enabled,
        )
        with self._lock:
            phase = self._phase
            last_error = self._last_error
            last_tick = self._last_tick_utc
            last_manual_release_local_date = self._last_manual_release_local_date
        result = {
            "available": True,
            "enabled": self.is_enabled(),
            "enable_file": self._enable_file,
            "state_file": self._state_file,
            "timezone": str(self._resolver.timezone),
            "priority": ["calendar", "manual", "cleanout", "schedule"],
            "phase": phase,
            "last_error": last_error,
            "last_tick_utc": format_utc(last_tick) if last_tick is not None else None,
            "now_utc": format_utc(now),
            "now_local": now.astimezone(self._resolver.timezone).isoformat(),
            "desired": desired_state_dict(desired),
            "manual_override": self._manual_dict(manual) if manual is not None else None,
            "openclaw_spa_session": openclaw_spa,
            "pool_heat_enabled": pool_heat_enabled,
            "manual_release_time_local": DAILY_MANUAL_RELEASE_TIME.isoformat(),
            "last_manual_release_local_date": last_manual_release_local_date.isoformat(),
        }
        if self._clock_sync is not None:
            result["clock_sync"] = self._clock_sync.status()
        if self._heater_targets is not None:
            result["heater_targets"] = self._heater_targets.status()
        return result

    def tick(self) -> bool:
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            now = parse_utc(self._now())
            self._release_manual_for_daily_checkpoint(now)
            manual, openclaw_spa, pool_heat_enabled = self._inputs()
            desired = self._resolver.resolve(
                now,
                manual_override=manual,
                openclaw_spa_session=openclaw_spa,
                pool_heat_enabled=pool_heat_enabled,
            )
            with self._lock:
                self._last_tick_utc = now
                self._desired = desired

            if not self.is_enabled():
                with self._lock:
                    self._phase = "disabled"
                return False

            equipment = self._equipment.status()
            vsp = self._vsp.status()
            if equipment.get("service_mode") or vsp.get("service_mode"):
                with self._lock:
                    self._phase = "service_inhibit"
                    self._last_error = None
                return False

            if vsp.get("hardware_priming"):
                with self._lock:
                    self._phase = "waiting_for_hardware_prime"
                    self._last_error = None
                return False

            if self._heater_targets is not None and self._heater_targets.is_busy():
                with self._lock:
                    self._phase = "heater_target"
                return False

            if self._clock_sync is not None and self._clock_sync.is_busy():
                with self._lock:
                    self._phase = "clock_sync"
                return False

            # Maintenance is lower priority than calendar/manual/cleanout. A
            # due weekly check briefly releases the schedule speed lease so it
            # has exclusive use of the display menu.
            if (
                self._clock_sync is not None
                and desired.source == "schedule"
                and self._clock_sync.due(now)
            ):
                if vsp.get("busy"):
                    self._vsp.clear_target()
                    with self._lock:
                        self._phase = "releasing_speed_for_clock"
                    return True
                if equipment.get("busy"):
                    with self._lock:
                        self._phase = "waiting_for_clock"
                    return False
                started = self._clock_sync.check_or_start()
                if started:
                    with self._lock:
                        self._phase = "clock_sync"
                    return True

            current_mode = equipment.get("mode")
            if current_mode not in ("pool", "spa", "spillover"):
                with self._lock:
                    self._phase = "waiting_for_mode_observation"
                    self._last_error = None
                return False

            # A scheduled calendar preparation must establish Speed 1 before
            # moving the valves to Spillover. This avoids spending the valve
            # transition at a prior schedule/manual speed. Manual Spa sessions
            # never carry the prep phase and bypass this path entirely.
            if desired.session_phase == "prep" and current_mode != "spillover":
                if equipment.get("busy"):
                    with self._lock:
                        self._phase = "waiting_for_mode"
                    return False
                if vsp.get("busy"):
                    if vsp.get("phase") == "holding" and vsp.get("target_name") == "speed1":
                        pass
                    elif vsp.get("phase") == "holding":
                        self._vsp.clear_target()
                        with self._lock:
                            self._phase = "releasing_speed_for_prep"
                        return True
                    else:
                        with self._lock:
                            self._phase = "waiting_for_prep_speed"
                        return False
                elif (
                    vsp.get("phase") == "observed"
                    and vsp.get("target_name") == "speed1"
                    and vsp.get("verified")
                ):
                    pass
                elif vsp.get("rollback_pending"):
                    if (
                        vsp.get("requested_speed_pct") == PRESET_SPEEDS["speed1"]
                        and vsp.get("rollback_target_pct") == PRESET_SPEEDS["speed1"]
                    ):
                        self._vsp.adopt_observed_preset("speed1", source="calendar")
                        with self._lock:
                            self._phase = "observed_prep_speed"
                        return True
                    self._vsp.recover_pending()
                    with self._lock:
                        self._phase = "recovering_prep_speed"
                    return True
                else:
                    self._vsp.request_preset(
                        "speed1",
                        source="calendar",
                        lease_seconds=self._speed_lease_seconds,
                    )
                    with self._lock:
                        self._phase = "setting_prep_speed"
                    return True

            # Spa mode selects a different hardware pump preset, so release a
            # leased Filter Speed edit before entering/leaving Spa. Pool and
            # Spillover share the Filter preset: transitions in either direction
            # must retain the active speed and change only the valve mode.
            mode_change = current_mode != desired.mode
            speed_preserving_mode_change = (
                mode_change
                and current_mode in ("pool", "spillover")
                and desired.mode in ("pool", "spillover")
            )
            if desired.suppress_filter_speed or mode_change:
                if vsp.get("busy"):
                    if speed_preserving_mode_change and vsp.get("phase") == "holding":
                        # Do not cancel/roll back the current speed. Renew a
                        # matching lease first if it could expire while valves
                        # are moving; renewal changes no hardware speed.
                        remaining = vsp.get("lease_remaining_sec") or 0
                        held_target = vsp.get("target_name")
                        if remaining < 45 and held_target is not None:
                            self._vsp.request_preset(
                                held_target,
                                source=desired.source,
                                lease_seconds=self._speed_lease_seconds,
                            )
                            with self._lock:
                                self._phase = "holding_speed_for_mode"
                            return True
                    elif speed_preserving_mode_change:
                        with self._lock:
                            self._phase = "waiting_for_speed_before_mode"
                        return False
                    else:
                        self._vsp.clear_target()
                        with self._lock:
                            self._phase = "releasing_speed_for_mode"
                        return True
                elif vsp.get("phase") == "observed" and not speed_preserving_mode_change:
                    self._vsp.clear_target()
                    with self._lock:
                        self._phase = "releasing_speed_for_mode"
                    return True
                elif vsp.get("rollback_pending") and not speed_preserving_mode_change:
                    self._vsp.recover_pending()
                    with self._lock:
                        self._phase = "recovering_speed_for_mode"
                    return True
                if equipment.get("busy"):
                    with self._lock:
                        self._phase = "waiting_for_mode"
                    return False
                if current_mode != desired.mode:
                    self._equipment.request_mode(desired.mode)
                    with self._lock:
                        self._phase = "setting_mode"
                    return True

            if equipment.get("busy"):
                with self._lock:
                    self._phase = "waiting_for_mode"
                return False

            for name, target in desired.switches.items():
                if (
                    name == "auto_heat"
                    and not equipment.get("auto_heat_confirmed", True)
                    and not _is_calendar_spa_auto_heat_request(desired)
                ):
                    # pyAqualogic assumes Auto Heat is on until it has parsed
                    # a Heater1 display page. Never turn that startup default
                    # into a real HEATER_1 keypress for non-calendar sources.
                    continue
                retry_block = equipment.get("switch_retry_block") or {}
                if (
                    name == "auto_heat"
                    and retry_block.get("control") == name
                    and retry_block.get("target") == target
                ):
                    # Do not repeat a toggle-style heater command until a new
                    # authoritative Heater1 page has been observed.
                    continue
                if equipment.get(name) != target:
                    self._equipment.set_switch(name, target)
                    with self._lock:
                        self._phase = f"setting_{name}"
                    return True

            if equipment.get("filter_on") != desired.filter_on:
                self._equipment.set_switch("filter", desired.filter_on)
                with self._lock:
                    self._phase = "setting_filter"
                return True

            if desired.suppress_filter_speed or desired.pump_preset is None:
                with self._lock:
                    self._phase = "converged"
                    self._last_error = None
                return False

            target = desired.pump_preset
            current_target = vsp.get("target_name")
            if vsp.get("busy") and current_target != target:
                self._vsp.clear_target()
                with self._lock:
                    self._phase = "changing_speed"
                return True
            if vsp.get("busy") and current_target == target:
                if vsp.get("phase") == "holding" and (vsp.get("lease_remaining_sec") or 0) < 45:
                    self._vsp.request_preset(
                        target,
                        source=desired.source,
                        lease_seconds=self._speed_lease_seconds,
                    )
                    with self._lock:
                        self._phase = "holding_speed"
                    return True
                with self._lock:
                    self._phase = "holding_speed"
                return False
            if vsp.get("requested_speed_pct") is None:
                with self._lock:
                    self._phase = "waiting_for_speed_observation"
                    self._last_error = None
                return False

            expected_pct = PRESET_SPEEDS[target]
            observed_matches = vsp.get("requested_speed_pct") == expected_pct
            rollback_compatible = (
                not vsp.get("rollback_pending")
                or vsp.get("rollback_target_pct") == expected_pct
            )
            if (
                vsp.get("phase") == "observed"
                and current_target == target
                and vsp.get("verified")
                and observed_matches
            ):
                if self._scan_targets_after_startup_speed_confirmation():
                    return True
                with self._lock:
                    self._phase = "observed_speed"
                    self._last_error = None
                return False
            if observed_matches and rollback_compatible:
                self._vsp.adopt_observed_preset(target, source=desired.source)
                with self._lock:
                    self._phase = "observed_speed"
                    self._last_error = None
                return False

            if vsp.get("rollback_pending"):
                self._vsp.recover_pending()
                with self._lock:
                    self._phase = "recovering_speed"
                return True

            self._vsp.request_preset(
                target,
                source=desired.source,
                lease_seconds=self._speed_lease_seconds,
            )
            with self._lock:
                self._phase = "setting_speed"
            return True
        except Exception as exc:
            with self._lock:
                self._phase = "error"
                self._last_error = str(exc)
            return False
        finally:
            self._tick_lock.release()
