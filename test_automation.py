import json
import os
import tempfile
import unittest
from datetime import datetime, time, timezone

from aqualogic_mqtt.automation import (
    AutomationEngine,
    ManualOverride,
    PumpWindow,
    ScheduleResolver,
    format_utc,
    parse_utc,
)


UTC = timezone.utc


class ScheduleResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = ScheduleResolver()

    def resolve(self, iso, **kwargs):
        return self.resolver.resolve(parse_utc(iso), **kwargs)

    def test_normal_schedule_and_cleanout_boundaries_use_new_york_time(self):
        self.assertEqual(self.resolve("2026-06-27T11:59:59Z").pump_preset, "speed4")
        self.assertEqual(self.resolve("2026-06-27T12:00:00Z").pump_preset, "speed1")
        self.assertEqual(self.resolve("2026-06-27T12:59:59Z").source, "schedule")
        self.assertEqual(self.resolve("2026-06-27T13:00:00Z").source, "cleanout")
        self.assertEqual(self.resolve("2026-06-27T14:00:00Z").pump_preset, "speed2")
        self.assertEqual(self.resolve("2026-06-27T14:30:00Z").source, "schedule")

    def test_calendar_beats_manual_and_manual_beats_cleanout(self):
        now = "2026-06-27T13:30:00Z"
        manual = ManualOverride(parse_utc("2026-06-27T15:00:00Z"), mode="pool", pump_preset="speed3")
        calendar = self.resolve(
            now,
            openclaw_spa_session={"session_id": "openclaw-spa-1"},
            manual_override=manual,
        )
        self.assertEqual(calendar.source, "calendar")
        self.assertEqual(calendar.mode, "spa")
        self.assertTrue(calendar.suppress_filter_speed)
        self.assertIsNone(calendar.pump_preset)
        self.assertEqual(calendar.switches, {"auto_heat": True, "heater_relay": True})

        manual_only = self.resolve(now, manual_override=manual)
        self.assertEqual(manual_only.source, "manual")
        self.assertEqual(manual_only.mode, "pool")
        self.assertEqual(manual_only.pump_preset, "speed3")

    def test_missing_schedule_speed_falls_back_to_t4(self):
        resolver = ScheduleResolver(
            pump_schedule=(PumpWindow(time(8), time(9), "speed1"),),
        )
        state = resolver.resolve(parse_utc("2026-06-27T16:00:00Z"))
        self.assertEqual(state.pump_preset, "speed4")

    def test_pool_heat_preference_applies_during_schedule_cleanout_and_manual(self):
        scheduled = self.resolve("2026-06-27T16:00:00Z", pool_heat_enabled=True)
        self.assertEqual(scheduled.switches, {"auto_heat": True})

        cleanout = self.resolve("2026-06-27T13:30:00Z", pool_heat_enabled=True)
        self.assertEqual(cleanout.source, "cleanout")
        self.assertEqual(cleanout.mode, "spillover")
        self.assertEqual(cleanout.switches, {"auto_heat": True})

        manual = ManualOverride(parse_utc("2026-06-28T00:00:00Z"), lights=True)
        manual_state = self.resolve(
            "2026-06-27T16:00:00Z",
            manual_override=manual,
            pool_heat_enabled=True,
        )
        self.assertEqual(manual_state.switches, {"lights": True, "auto_heat": True})

    def test_spring_dst_uses_wall_clock_schedule(self):
        # 06:59Z is 01:59 EST; 07:00Z jumps to 03:00 EDT.
        before = self.resolve("2026-03-08T06:59:00Z")
        after = self.resolve("2026-03-08T07:00:00Z")
        self.assertEqual(before.pump_preset, "speed4")
        self.assertEqual(after.pump_preset, "speed4")
        self.assertEqual(self.resolve("2026-03-08T12:00:00Z").pump_preset, "speed1")

    def test_fall_dst_repeated_hour_is_consistent(self):
        # Both UTC instants are local 01:30, on opposite sides of the fold.
        first = self.resolve("2026-11-01T05:30:00Z")
        second = self.resolve("2026-11-01T06:30:00Z")
        self.assertEqual(first.pump_preset, "speed4")
        self.assertEqual(second.pump_preset, "speed4")

    def test_utc_round_trip_and_naive_rejection(self):
        self.assertEqual(format_utc(parse_utc("2026-06-27T09:00:00-04:00")), "2026-06-27T13:00:00Z")
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            parse_utc(datetime(2026, 6, 27, 13, 0))


class FakeEquipment:
    def __init__(self):
        self.state = {
            "mode": "pool",
            "service_mode": False,
            "filter_on": True,
            "busy": False,
            "auto_heat": False,
            "auto_heat_confirmed": True,
            "heater_relay": False,
            "lights": False,
            "blower": False,
        }
        self.calls = []

    def status(self):
        return dict(self.state)

    def request_mode(self, mode):
        self.calls.append(("mode", mode))
        self.state["mode"] = mode

    def set_switch(self, name, target):
        self.calls.append((name, target))
        self.state[name] = target


class FakeVsp:
    def __init__(self):
        self.state = {
            "enabled": True,
            "service_mode": False,
            "filter_on": True,
            "busy": False,
            "phase": "idle",
            "target_name": None,
            "requested_speed_pct": 0,
            "verified": False,
            "lease_remaining_sec": None,
            "rollback_pending": False,
            "rollback_target_pct": None,
            "hardware_priming": False,
        }
        self.calls = []

    def status(self):
        return dict(self.state)

    def request_preset(self, target, **kwargs):
        self.calls.append(("speed", target, kwargs.get("source")))
        self.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": target,
            "lease_remaining_sec": kwargs.get("lease_seconds"),
            "verified": True,
        })

    def adopt_observed_preset(self, target, **kwargs):
        self.calls.append(("adopt", target, kwargs.get("source")))
        expected = {"speed1": 70, "speed2": 95, "speed3": 55, "speed4": 40}[target]
        self.state.update({
            "busy": False,
            "phase": "observed",
            "target_name": target,
            "verified": self.state.get("requested_speed_pct") == expected,
        })

    def clear_target(self):
        self.calls.append(("clear",))
        self.state.update({
            "busy": False,
            "phase": "complete",
            "target_name": None,
            "verified": False,
        })

    def recover_pending(self):
        self.calls.append(("recover",))
        self.state.update({"busy": True, "phase": "recovering"})


class FakeClockSync:
    def is_busy(self):
        return False

    def due(self, _now=None):
        return False

    def status(self):
        return {"phase": "checked", "due": False}


class FakeHeaterTargets:
    def __init__(self, busy=False):
        self.busy = busy
        self.refresh_calls = 0
        self.observed_since_startup = {"pool": False, "spa": False}

    def is_busy(self):
        return self.busy

    def status(self):
        return {
            "busy": self.busy,
            "phase": "reading" if self.busy else "complete",
            "observed_since_startup": dict(self.observed_since_startup),
        }

    def request_refresh(self):
        self.refresh_calls += 1
        self.busy = True
        return self.status()


class AutomationEngineTest(unittest.TestCase):
    def make_engine(self, now, **kwargs):
        equipment = FakeEquipment()
        vsp = FakeVsp()
        engine = AutomationEngine(
            equipment,
            vsp,
            enabled=kwargs.pop("enabled", True),
            state_file=kwargs.pop("state_file", None),
            now=lambda: parse_utc(now[0]),
            **kwargs,
        )
        return engine, equipment, vsp

    def test_disabled_engine_never_writes_hardware(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"], enabled=False)
        self.assertFalse(engine.tick())
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [])
        self.assertEqual(engine.status()["phase"], "disabled")

    def test_status_includes_clock_sync_state(self):
        now = ["2026-06-27T12:00:00Z"]
        engine, _equipment, _vsp = self.make_engine(now, clock_sync=FakeClockSync())
        self.assertEqual(engine.status()["clock_sync"], {"phase": "checked", "due": False})

    def test_heater_target_menu_work_pauses_automation(self):
        targets = FakeHeaterTargets(busy=True)
        engine, equipment, vsp = self.make_engine(
            ["2026-06-27T12:00:00Z"], heater_targets=targets
        )
        self.assertTrue(engine.hardware_busy())
        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "heater_target")
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [])

    def test_service_mode_is_global_inhibit(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        equipment.state["service_mode"] = True
        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "service_inhibit")
        self.assertEqual(vsp.calls, [])

    def test_normal_schedule_requests_speed_and_renews_near_expiry(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("speed", "speed1", "schedule")])
        vsp.state["lease_remaining_sec"] = 30
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls[-1], ("speed", "speed1", "schedule"))

    def test_matching_observed_schedule_speed_is_adopted_without_menu_control(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state["requested_speed_pct"] = 70

        self.assertFalse(engine.tick())
        self.assertEqual(vsp.calls, [("adopt", "speed1", "schedule")])
        self.assertEqual(vsp.state["phase"], "observed")
        self.assertEqual(vsp.state["target_name"], "speed1")
        self.assertTrue(vsp.state["verified"])
        self.assertEqual(engine.status()["phase"], "observed_speed")

    def test_matching_startup_speed_triggers_one_read_only_target_scan(self):
        targets = FakeHeaterTargets()
        engine, _equipment, vsp = self.make_engine(
            ["2026-06-27T12:00:00Z"], heater_targets=targets
        )
        vsp.state["requested_speed_pct"] = 70

        self.assertFalse(engine.tick())
        self.assertTrue(engine.tick())
        self.assertEqual(targets.refresh_calls, 1)
        self.assertEqual(engine.status()["phase"], "scanning_startup_heater_targets")

        targets.busy = False
        targets.observed_since_startup = {"pool": True, "spa": True}
        self.assertFalse(engine.tick())
        self.assertEqual(targets.refresh_calls, 1)

    def test_unconfirmed_startup_auto_heat_assumption_never_sends_a_command(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        equipment.state.update({"auto_heat": True, "auto_heat_confirmed": False})
        vsp.state["requested_speed_pct"] = 70

        self.assertFalse(engine.tick())
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [("adopt", "speed1", "schedule")])

    def test_unconfirmed_non_calendar_auto_heat_request_is_skipped(self):
        engine, equipment, _vsp = self.make_engine(["2026-06-27T16:00:00Z"])
        engine.set_pool_heat(True)
        engine.set_manual(mode="spa")
        equipment.state.update({
            "mode": "spa",
            "auto_heat": False,
            "auto_heat_confirmed": False,
        })

        self.assertFalse(engine.tick())
        self.assertEqual(equipment.calls, [])
        self.assertEqual(engine.status()["phase"], "converged")

    def test_calendar_spa_auto_heat_request_bypasses_unconfirmed_guard(self):
        engine, equipment, _vsp = self.make_engine(["2026-06-27T16:00:00Z"])
        engine.activate_openclaw_spa(session_id="calendar-heat")
        equipment.state.update({
            "mode": "spa",
            "auto_heat": False,
            "auto_heat_confirmed": False,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("auto_heat", True)])
        self.assertEqual(engine.status()["phase"], "setting_auto_heat")

    def test_auto_heat_retry_waits_for_new_authoritative_observation(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        equipment.state.update({
            "auto_heat": True,
            "auto_heat_confirmed": True,
            "switch_retry_block": {"control": "auto_heat", "target": False},
        })
        vsp.state["requested_speed_pct"] = 70

        self.assertFalse(engine.tick())
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [("adopt", "speed1", "schedule")])

    def test_matching_persisted_lease_is_adopted_without_recovery(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state.update({
            "requested_speed_pct": 70,
            "rollback_pending": True,
            "rollback_target_pct": 70,
        })

        self.assertFalse(engine.tick())
        self.assertEqual(vsp.calls, [("adopt", "speed1", "schedule")])
        self.assertEqual(engine.status()["phase"], "observed_speed")

    def test_incompatible_persisted_lease_is_recovered_before_speed_change(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state.update({
            "requested_speed_pct": 70,
            "rollback_pending": True,
            "rollback_target_pct": 55,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("recover",)])
        self.assertEqual(engine.status()["phase"], "recovering_speed")

    def test_startup_waits_for_initial_speed_observation_before_using_menu(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state["requested_speed_pct"] = None

        self.assertFalse(engine.tick())
        self.assertEqual(vsp.calls, [])
        self.assertEqual(engine.status()["phase"], "waiting_for_speed_observation")

        vsp.state["requested_speed_pct"] = 70
        self.assertFalse(engine.tick())
        self.assertEqual(vsp.calls, [("adopt", "speed1", "schedule")])

    def test_adopted_schedule_speed_uses_driver_if_observed_request_drifts(self):
        engine, _equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state["requested_speed_pct"] = 70
        self.assertFalse(engine.tick())

        vsp.state.update({"requested_speed_pct": 55, "verified": False})
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls[-1], ("speed", "speed1", "schedule"))

    def test_hardware_priming_pauses_reconcile_until_controller_releases(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        vsp.state["hardware_priming"] = True

        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "waiting_for_hardware_prime")
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [])

        vsp.state["hardware_priming"] = False
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("speed", "speed1", "schedule")])

    def test_unknown_mode_waits_without_requesting_hardware_transition(self):
        engine, equipment, vsp = self.make_engine(["2026-06-27T12:00:00Z"])
        equipment.state["mode"] = "unknown"

        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "waiting_for_mode_observation")
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [])

        equipment.state["mode"] = "pool"
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("speed", "speed1", "schedule")])

    def test_calendar_releases_speed_before_entering_spa(self):
        now = ["2026-06-27T16:00:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.activate_openclaw_spa(session_id="openclaw-event-1")
        vsp.state.update({"busy": True, "phase": "holding", "target_name": "speed1"})
        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("clear",)])
        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("mode", "spa")])
        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls[-1], ("auto_heat", True))
        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls[-1], ("heater_relay", True))
        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "converged")

    def test_spillover_mode_keeps_active_pump_speed_lease(self):
        now = ["2026-06-27T13:30:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.set_manual(mode="spillover")
        vsp.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": "speed1",
            "lease_remaining_sec": 80,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("mode", "spillover")])
        self.assertEqual(vsp.calls, [])

    def test_spillover_to_pool_changes_only_mode_and_keeps_pump_speed(self):
        now = ["2026-06-27T13:30:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        equipment.state["mode"] = "spillover"
        engine.set_manual(mode="pool")
        vsp.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": "speed1",
            "lease_remaining_sec": 80,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("mode", "pool")])
        self.assertEqual(vsp.calls, [])

    def test_cleanout_spillover_keeps_scheduled_speed_lease(self):
        now = ["2026-06-27T13:30:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        vsp.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": "speed1",
            "lease_remaining_sec": 80,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(engine.status()["desired"]["source"], "cleanout")
        self.assertEqual(equipment.calls, [("mode", "spillover")])
        self.assertEqual(vsp.calls, [])

    def test_spillover_waits_for_vsp_menu_without_cancelling_speed(self):
        now = ["2026-06-27T13:30:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.set_manual(mode="spillover")
        vsp.state.update({
            "busy": True,
            "phase": "navigating",
            "target_name": "speed1",
            "lease_remaining_sec": 80,
        })

        self.assertFalse(engine.tick())
        self.assertEqual(engine.status()["phase"], "waiting_for_speed_before_mode")
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [])

    def test_spillover_renews_near_expiry_without_changing_speed(self):
        now = ["2026-06-27T13:30:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.set_manual(mode="spillover")
        vsp.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": "speed1",
            "lease_remaining_sec": 20,
        })

        self.assertTrue(engine.tick())
        self.assertEqual(engine.status()["phase"], "holding_speed_for_mode")
        self.assertEqual(equipment.calls, [])
        self.assertEqual(vsp.calls, [("speed", "speed1", "manual")])

    def test_manual_override_is_utc_persisted_and_survives_restart(self):
        now = ["2026-06-27T13:30:00Z"]
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            engine.set_manual(mode="pool", pump_preset="speed3")
            restarted, _equipment2, _vsp2 = self.make_engine(now, state_file=state_file)
            status = restarted.status()
            self.assertEqual(status["desired"]["source"], "manual")
            self.assertEqual(status["manual_override"]["expires_utc"], "2026-06-28T01:30:00Z")
            self.assertEqual(status["desired"]["pump_preset"], "speed3")

    def test_all_manual_overrides_release_once_at_three_am_new_york(self):
        now = ["2026-06-27T06:59:00Z"]  # 02:59 EDT
        engine, _equipment, _vsp = self.make_engine(now)
        engine.set_manual(mode="pool", lights=True, blower=True, filter_on=False)
        engine.set_pool_heat(True)

        engine.tick()
        self.assertIsNotNone(engine.status()["manual_override"])

        now[0] = "2026-06-27T07:00:00Z"  # 03:00 EDT
        engine.tick()
        status = engine.status()
        self.assertIsNone(status["manual_override"])
        self.assertTrue(status["pool_heat_enabled"])
        self.assertEqual(status["last_manual_release_local_date"], "2026-06-27")
        self.assertEqual(status["manual_release_time_local"], "03:00:00")

        # A new override after the daily checkpoint survives until tomorrow.
        now[0] = "2026-06-27T07:30:00Z"
        engine.set_manual(blower=True)
        engine.tick()
        self.assertTrue(engine.status()["manual_override"]["blower"])

    def test_fall_dst_transition_releases_at_three_am_standard_time(self):
        now = ["2026-11-01T04:59:00Z"]  # 00:59 EDT, before fallback
        engine, _equipment, _vsp = self.make_engine(now)
        engine.set_manual(lights=True)

        now[0] = "2026-11-01T06:30:00Z"  # repeated 01:30, now EST
        engine.tick()
        self.assertTrue(engine.status()["manual_override"]["lights"])

        now[0] = "2026-11-01T08:00:00Z"  # 03:00 EST
        engine.tick()
        self.assertIsNone(engine.status()["manual_override"])

    def test_spring_dst_jump_releases_when_clock_reaches_three_am(self):
        now = ["2026-03-08T06:59:00Z"]  # 01:59 EST
        engine, _equipment, _vsp = self.make_engine(now)
        engine.set_manual(blower=True)

        now[0] = "2026-03-08T07:00:00Z"  # clocks jump to 03:00 EDT
        engine.tick()
        self.assertIsNone(engine.status()["manual_override"])
        self.assertEqual(
            engine.status()["last_manual_release_local_date"], "2026-03-08"
        )

    def test_restart_after_missed_one_am_catches_up(self):
        now = ["2026-06-27T12:00:00Z"]  # 08:00 EDT
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 3,
                    "manual_override": {
                        "expires_utc": "2026-06-27T20:00:00Z",
                        "lights": True,
                    },
                    "pool_heat_enabled": False,
                    "openclaw_spa_session": None,
                    "last_manual_release_local_date": "2026-06-26",
                }, handle)
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            self.assertIsNotNone(engine.status()["manual_override"])
            engine.tick()
            self.assertIsNone(engine.status()["manual_override"])
            with open(state_file, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["last_manual_release_local_date"], "2026-06-27")

    def test_legacy_state_migration_does_not_clear_at_deploy_time(self):
        now = ["2026-06-27T20:00:00Z"]  # 16:00 EDT
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 2,
                    "manual_override": {
                        "expires_utc": "2026-06-28T01:00:00Z",
                        "lights": True,
                    },
                    "pool_heat_enabled": False,
                    "openclaw_spa_session": None,
                }, handle)
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            engine.tick()
            self.assertTrue(engine.status()["manual_override"]["lights"])
            with open(state_file, encoding="utf-8") as handle:
                migrated = json.load(handle)
            self.assertEqual(migrated["version"], 3)
            self.assertEqual(migrated["last_manual_release_local_date"], "2026-06-27")

    def test_pool_heat_is_persistent_and_does_not_create_or_extend_manual_override(self):
        now = ["2026-06-27T16:00:00Z"]
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            engine, equipment, _vsp = self.make_engine(now, state_file=state_file)
            status = engine.set_pool_heat(True)
            self.assertTrue(status["pool_heat_enabled"])
            self.assertIsNone(status["manual_override"])
            self.assertEqual(status["desired"]["switches"], {"auto_heat": True})
            self.assertTrue(engine.tick())
            self.assertEqual(equipment.calls, [("auto_heat", True)])

            now[0] = "2026-07-04T16:00:00Z"
            restarted, equipment2, _vsp2 = self.make_engine(now, state_file=state_file)
            self.assertTrue(restarted.status()["pool_heat_enabled"])
            self.assertEqual(restarted.status()["desired"]["switches"], {"auto_heat": True})

            restarted.set_pool_heat(False)
            equipment2.state["auto_heat"] = True
            self.assertTrue(restarted.tick())
            self.assertEqual(equipment2.calls, [("auto_heat", False)])

            restarted_again, _equipment3, _vsp3 = self.make_engine(now, state_file=state_file)
            self.assertFalse(restarted_again.status()["pool_heat_enabled"])

    def test_legacy_auto_heat_override_migrates_to_pool_heat_preference(self):
        now = ["2026-06-27T13:30:00Z"]
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1,
                    "manual_override": {
                        "expires_utc": "2026-06-28T01:30:00Z",
                        "auto_heat": True,
                    },
                    "openclaw_spa_session": None,
                }, handle)
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            status = engine.status()
            self.assertTrue(status["pool_heat_enabled"])
            self.assertIsNone(status["manual_override"])

    def test_manual_filter_off_suppresses_speed_until_override_clears(self):
        now = ["2026-06-27T16:00:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.set_manual(filter_on=False)
        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("filter", False)])
        self.assertEqual(vsp.calls, [])
        self.assertTrue(engine.status()["desired"]["suppress_filter_speed"])

    def test_openclaw_spa_session_has_no_software_timer_and_is_top_priority(self):
        now = ["2026-06-27T16:00:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        status = engine.activate_openclaw_spa(
            session_id="openclaw-event-1",
        )
        desired = status["desired"]
        self.assertEqual(desired["source"], "calendar")
        self.assertEqual(desired["mode"], "spa")
        self.assertEqual(desired["switches"], {"auto_heat": True, "heater_relay": True})
        self.assertEqual(status["openclaw_spa_session"]["session_id"], "openclaw-event-1")
        self.assertEqual(status["openclaw_spa_session"]["phase"], "spa")
        self.assertEqual(status["openclaw_spa_session"]["started_utc"], "2026-06-27T16:00:00Z")
        stopped = engine.stop_openclaw_spa("openclaw-event-1")
        self.assertEqual(stopped["desired"]["source"], "schedule")
        self.assertEqual(stopped["desired"]["switches"], {"auto_heat": False})

    def test_scheduled_spa_has_exact_calendar_prep_then_preheat(self):
        now = ["2026-06-27T15:50:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        armed = engine.activate_openclaw_spa(
            session_id="calendar-event-1",
            phase="scheduled",
            prep_start_utc="2026-06-27T15:55:00Z",
            preheat_start_utc="2026-06-27T16:00:00Z",
        )
        self.assertEqual(armed["desired"]["source"], "schedule")
        self.assertEqual(armed["openclaw_spa_session"]["phase"], "scheduled")

        now[0] = "2026-06-27T15:55:00Z"
        prep = engine.status()["desired"]
        self.assertEqual(prep["source"], "calendar")
        self.assertEqual(prep["mode"], "spillover")
        self.assertEqual(prep["pump_preset"], "speed1")
        self.assertEqual(prep["session_phase"], "prep")
        self.assertEqual(prep["switches"], {})

        now[0] = "2026-06-27T15:59:59Z"
        self.assertEqual(engine.status()["desired"]["mode"], "spillover")
        now[0] = "2026-06-27T16:00:00Z"
        preheat = engine.status()["desired"]
        self.assertEqual(preheat["source"], "calendar")
        self.assertEqual(preheat["mode"], "spa")
        self.assertIsNone(preheat["pump_preset"])
        self.assertEqual(preheat["session_phase"], "spa")
        self.assertEqual(preheat["switches"], {"auto_heat": True, "heater_relay": True})

    def test_calendar_prep_establishes_speed1_before_spillover(self):
        now = ["2026-06-27T15:55:00Z"]
        engine, equipment, vsp = self.make_engine(now)
        engine.activate_openclaw_spa(
            session_id="calendar-speed-first",
            phase="scheduled",
            prep_start_utc="2026-06-27T15:55:00Z",
            preheat_start_utc="2026-06-27T16:00:00Z",
        )

        self.assertTrue(engine.tick())
        self.assertEqual(vsp.calls, [("speed", "speed1", "calendar")])
        self.assertEqual(equipment.calls, [])

        vsp.state.update({
            "busy": True,
            "phase": "holding",
            "target_name": "speed1",
            "lease_remaining_sec": 80,
        })
        self.assertTrue(engine.tick())
        self.assertEqual(equipment.calls, [("mode", "spillover")])

    def test_calendar_prep_overrides_manual_only_during_its_window(self):
        now = ["2026-06-27T15:50:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        engine.set_manual(mode="pool", pump_preset="speed3")
        engine.activate_openclaw_spa(
            session_id="calendar-event-2",
            phase="scheduled",
            prep_start_utc="2026-06-27T15:55:00Z",
            preheat_start_utc="2026-06-27T16:00:00Z",
        )
        self.assertEqual(engine.status()["desired"]["source"], "manual")
        self.assertEqual(engine.status()["desired"]["pump_preset"], "speed3")
        now[0] = "2026-06-27T15:55:00Z"
        self.assertEqual(engine.status()["desired"]["source"], "calendar")
        self.assertEqual(engine.status()["desired"]["pump_preset"], "speed1")

    def test_manual_spa_session_bypasses_calendar_prep(self):
        now = ["2026-06-27T15:55:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        status = engine.activate_openclaw_spa(session_id="manual-session")
        self.assertEqual(status["openclaw_spa_session"]["phase"], "spa")
        self.assertEqual(status["desired"]["mode"], "spa")
        self.assertIsNone(status["desired"]["pump_preset"])

    def test_scheduled_prep_requires_ordered_utc_timestamps(self):
        now = ["2026-06-27T15:50:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        with self.assertRaisesRegex(ValueError, "requires prep_start_utc"):
            engine.activate_openclaw_spa(session_id="event", phase="scheduled")
        with self.assertRaisesRegex(ValueError, "must precede"):
            engine.activate_openclaw_spa(
                session_id="event",
                phase="scheduled",
                prep_start_utc="2026-06-27T16:00:00Z",
                preheat_start_utc="2026-06-27T16:00:00Z",
            )

    def test_openclaw_spa_restores_current_pool_heat_preference(self):
        now = ["2026-06-27T16:00:00Z"]
        engine, _equipment, _vsp = self.make_engine(now)
        engine.set_pool_heat(True)
        active = engine.activate_openclaw_spa(session_id="event-heat-on")
        self.assertEqual(active["desired"]["switches"], {"auto_heat": True, "heater_relay": True})
        stopped = engine.stop_openclaw_spa("event-heat-on")
        self.assertEqual(stopped["desired"]["switches"], {"auto_heat": True})

        engine.activate_openclaw_spa(session_id="event-heat-off")
        during = engine.set_pool_heat(False)
        self.assertEqual(during["desired"]["switches"], {"auto_heat": True, "heater_relay": True})
        stopped = engine.stop_openclaw_spa("event-heat-off")
        self.assertEqual(stopped["desired"]["switches"], {"auto_heat": False})

    def test_openclaw_start_is_rejected_while_automation_is_disabled(self):
        now = ["2026-06-27T16:00:00Z"]
        engine, _equipment, _vsp = self.make_engine(now, enabled=False)
        with self.assertRaisesRegex(RuntimeError, "automation is disabled"):
            engine.activate_openclaw_spa(session_id="event-1")

    def test_openclaw_spa_latch_survives_restart_without_expiry(self):
        now = ["2026-06-27T16:00:00Z"]
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            engine.activate_openclaw_spa(session_id="event-1")
            now[0] = "2026-06-30T16:00:00Z"
            restarted, _equipment2, _vsp2 = self.make_engine(now, state_file=state_file)
            self.assertEqual(restarted.status()["desired"]["source"], "calendar")
            self.assertEqual(
                restarted.status()["openclaw_spa_session"]["session_id"],
                "openclaw-event-1",
            )

    def test_scheduled_prep_survives_restart_with_utc_boundaries(self):
        now = ["2026-06-27T15:50:00Z"]
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "automation.json")
            engine, _equipment, _vsp = self.make_engine(now, state_file=state_file)
            engine.activate_openclaw_spa(
                session_id="calendar-restart",
                phase="scheduled",
                prep_start_utc="2026-06-27T15:55:00-00:00",
                preheat_start_utc="2026-06-27T12:00:00-04:00",
            )
            now[0] = "2026-06-27T15:56:00Z"
            restarted, _equipment2, _vsp2 = self.make_engine(now, state_file=state_file)
            session = restarted.status()["openclaw_spa_session"]
            self.assertEqual(session["prep_start_utc"], "2026-06-27T15:55:00Z")
            self.assertEqual(session["preheat_start_utc"], "2026-06-27T16:00:00Z")
            self.assertEqual(restarted.status()["desired"]["mode"], "spillover")


if __name__ == "__main__":
    unittest.main()
