"""Run with: .venv/bin/python -m unittest tests.test_rules"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import local
from app.history import History, snapshot
from app.homekit import MODE_HEAT, MODE_OFF
from app.local import LocalStore, enforce

ROME = ZoneInfo("Europe/Rome")
THU = datetime(2026, 11, 5, tzinfo=ROME)  # a Thursday


def at(hour, minute=0, day=0):
    return THU + timedelta(days=day, hours=hour, minutes=minute)


class Frozen(datetime):
    current = THU

    @classmethod
    def now(cls, tz=None):
        return cls.current


class FakeHomeKit:
    def __init__(self, serials, fail=()):
        self.zones = {i: {"serial": s} for i, s in enumerate(serials, 1)}
        self.state = {i: {"target_hc": MODE_HEAT, "target_temp": 20.0} for i in self.zones}
        self.fail = fail

    async def set_mode(self, aid, mode):
        if self.zones[aid]["serial"] in self.fail:
            raise RuntimeError("valve offline")
        self.state[aid]["target_hc"] = mode

    async def set_temperature(self, aid, temp):
        if self.zones[aid]["serial"] in self.fail:
            raise RuntimeError("valve offline")
        self.state[aid] |= {"target_hc": MODE_HEAT, "target_temp": temp}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalStore(Path(self.tmp.name) / "tado.db")
        self.store.set_quiet(room := self.store.add_room("Bedroom"), "22:00", "08:00")
        self.room = room
        self.store.set_points(room, [{"days": "1234567", "time": "08:30", "celsius": 17}, {"days": "1234567", "time": "17:00", "celsius": 20}])
        self.store.move_device("V1", room)
        patcher = patch.object(local, "datetime", Frozen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, now):
        Frozen.current = now
        return self.store.plan(now)[self.room]

    def test_schedule_runs_during_the_day(self):
        s = self.state(at(18))
        self.assertEqual((s["mode"], s["want"]), ("schedule", 20))

    def test_night_off_overrides_the_schedule_across_midnight(self):
        for now in (at(22), at(23, 30), at(0, 15, day=1), at(7, 59, day=1)):
            s = self.state(now)
            self.assertEqual((s["mode"], s["want"]), ("night", None), now)

    def test_schedule_returns_at_the_end_of_night_off(self):
        # the 17:00 -> 20° point is still current at 08:00, until 08:30
        s = self.state(at(8, day=1))
        self.assertEqual((s["mode"], s["want"]), ("schedule", 20))

    def test_next_change_is_the_night_off_start(self):
        s = self.state(at(21))
        self.assertEqual(s["until"], at(22).isoformat())
        self.assertEqual(s["next"], "today 22:00 → off")

    def test_a_schedule_point_inside_night_off_does_not_fire(self):
        self.store.set_points(self.room, [{"days": "1234567", "time": "22:30", "celsius": 20}])
        self.assertEqual(self.state(at(22, 45))["want"], None)

    def test_room_without_night_off_keeps_its_schedule(self):
        self.store.set_quiet(self.room, None, None)
        self.assertEqual(self.state(at(23))["want"], 20)

    def test_manual_hold_ends_at_night_off(self):
        Frozen.current = at(21, 50)
        self.store.hold("V1")
        self.assertEqual(self.state(at(21, 55))["mode"], "manual")
        self.assertEqual(self.state(at(22))["mode"], "night")

    def test_manual_hold_at_night_lasts_an_hour(self):
        Frozen.current = at(23)
        self.store.hold("V1")
        self.assertEqual(self.state(at(23, 59))["mode"], "manual")
        self.assertEqual(self.state(at(0, day=1))["mode"], "night")

    def test_manual_hold_at_night_stops_at_the_end_of_night_off(self):
        Frozen.current = at(7, 30)
        self.store.hold("V1")
        self.assertEqual(self.state(at(8))["mode"], "schedule")

    def test_away_overrides_schedule_and_night_off(self):
        Frozen.current = at(18)
        self.store.set_away(True, None, 12)
        for now in (at(18), at(23), at(9, day=1)):
            s = self.state(now)
            self.assertEqual((s["mode"], s["want"]), ("away", 12), now)

    def test_away_can_mean_off(self):
        Frozen.current = at(18)
        self.store.set_away(True, None, None)
        s = self.state(at(18))
        self.assertEqual((s["mode"], s["want"], s["enforce"]), ("away", None, True))

    def test_away_ends_by_itself_and_clears_holds(self):
        Frozen.current = at(9)
        self.store.hold("V1")
        self.store.set_away(True, at(18, day=2), 12)
        self.assertEqual(self.state(at(10))["mode"], "away")
        self.assertEqual(self.state(at(18, day=2))["mode"], "schedule")
        self.assertFalse(self.store.away(at(18, day=2))["active"])

    def test_room_with_no_schedule_only_follows_night_off(self):
        rid = self.store.add_room("Empty")
        self.assertEqual(self.store.plan(at(12))[rid]["enforce"], False)
        self.assertEqual(self.store.plan(at(23))[rid]["mode"], "night")

    def test_enforce_turns_off_unassigned_devices_at_night(self):
        hk = FakeHomeKit(["V1", "LOOSE"])
        Frozen.current = at(23)
        asyncio.run(enforce(self.store, hk))
        self.assertEqual([s["target_hc"] for s in hk.state.values()], [MODE_OFF, MODE_OFF])

    def test_enforce_continues_past_a_failing_valve(self):
        other = self.store.add_room("Other")
        self.store.move_device("V2", other)
        hk = FakeHomeKit(["V1", "V2"], fail=("V1",))
        Frozen.current = at(23)
        asyncio.run(enforce(self.store, hk))
        self.assertEqual(hk.state[2]["target_hc"], MODE_OFF)

    def test_dial_shifts_the_schedule_without_changing_it(self):
        self.store.set_dial(-1.5)
        self.assertEqual(self.state(at(18))["want"], 18.5)
        self.assertEqual(self.state(at(21))["next"], "today 22:00 → off")
        self.assertEqual(self.store.points(self.room)[1]["celsius"], 20)
        self.assertEqual(self.store.rooms()[0]["schedule"][1]["celsius"], 20)

    def test_dial_shows_in_the_next_change(self):
        self.store.set_dial(1)
        self.assertEqual(self.state(at(16, day=0))["next"], "today 17:00 → 21°")

    def test_dial_leaves_night_off_and_away_alone(self):
        self.store.set_dial(2)
        self.assertEqual(self.state(at(23))["want"], None)
        Frozen.current = at(18)
        self.store.set_away(True, None, 12)
        self.assertEqual(self.state(at(18))["want"], 12)

    def test_dial_stays_within_what_a_valve_accepts(self):
        self.store.set_points(self.room, [{"days": "1234567", "time": "00:00", "celsius": 24}, {"days": "1234567", "time": "12:00", "celsius": None}])
        self.store.set_dial(3)
        self.assertEqual(self.state(at(9))["want"], 25)
        self.assertEqual((self.state(at(13))["mode"], self.state(at(13))["want"]), ("schedule", None))

    def test_dial_moves_valves_on_the_next_check(self):
        hk = FakeHomeKit(["V1"])
        self.store.set_dial(-1)
        Frozen.current = at(18)
        asyncio.run(enforce(self.store, hk))
        self.assertEqual(hk.state[1]["target_temp"], 19)

    def test_dial_shifts_on_demand_but_not_its_setting(self):
        self.store.set_on_demand(self.room, {"celsius": 21, "hours": 3})
        self.store.set_dial(-2)
        room = self.store.rooms()[0]
        self.assertEqual((room["on_demand"]["celsius"], room["on_demand"]["effective"]), (21, 19))
        self.assertEqual(self.store.dialled(21), 19)

    def test_migration_adds_night_off_and_clears_old_holds(self):
        path = Path(self.tmp.name) / "old.db"
        import sqlite3

        db = sqlite3.connect(path)
        db.execute("CREATE TABLE rooms (id INTEGER PRIMARY KEY, name TEXT NOT NULL, last_applied TEXT, hold_until TEXT)")
        db.execute("INSERT INTO rooms (name, hold_until) VALUES ('Living room', '2030-01-01T00:00:00+01:00'), ('Bedroom', NULL)")
        db.commit()
        db.close()
        rooms = {r["name"]: r for r in LocalStore(path).rooms()}
        self.assertEqual(rooms["Living room"]["quiet"], {"start": "22:00", "end": "08:00"})
        self.assertEqual(rooms["Bedroom"]["quiet"], {"start": "22:00", "end": "08:00"})
        self.assertFalse(rooms["Living room"]["held"])


class HistoryTest(unittest.TestCase):
    NOW = int(THU.timestamp()) + 12 * 3600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.h.db.close)

    def fill(self, minutes, rows):
        for i in range(minutes):
            self.h.record(self.NOW - 60 * (minutes - i), rows)
        self.h.rollup(self.NOW)

    def test_room_heats_when_any_device_does_not_double_counted(self):
        self.fill(120, [("A", 1, 20.0, 21.0, 50.0, 1), ("B", 1, 22.0, 21.0, 60.0, 1), ("C", 1, 21.0, 21.0, None, 0)])
        room = self.h.query("24h", [{"id": 1, "name": "R"}], self.NOW)["rooms"][0]
        self.assertEqual(room["stats"]["heat_hours"], 2)
        self.assertEqual(room["stats"]["avg"], 21)
        self.assertEqual((room["stats"]["min"], room["stats"]["max"]), (20, 22))
        self.assertEqual(room["stats"]["humidity"], 55)

    def test_long_ranges_match_raw_ranges(self):
        self.fill(300, [("A", 1, 20.0, 21.0, 50.0, 1)])
        rooms = [{"id": 1, "name": "R"}]
        raw = self.h.query("7d", rooms, self.NOW)["rooms"][0]["stats"]
        rolled = self.h.query("30d", rooms, self.NOW)["rooms"][0]["stats"]
        self.assertEqual(raw, rolled)

    def test_off_room_has_no_target_line(self):
        self.fill(60, [("A", 1, 18.0, None, 50.0, 0)])
        point = self.h.query("24h", [{"id": 1, "name": "R"}], self.NOW)["rooms"][0]["points"][0]
        self.assertIsNone(point[2])

    def test_empty_database_is_fine(self):
        data = self.h.query("24h", [{"id": 1, "name": "R"}], self.NOW)
        self.assertIsNone(data["collecting_since"])
        self.assertIsNone(data["rooms"][0]["stats"])

    def test_raw_samples_expire_but_hourly_stays(self):
        self.h.record(self.NOW - 400 * 86400, [("A", 1, 20.0, 21.0, 50.0, 1)])
        self.h.rollup(self.NOW)
        self.assertEqual(self.h.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
        self.assertEqual(self.h.db.execute("SELECT COUNT(*) FROM hourly").fetchone()[0], 1)

    def test_rollup_is_idempotent_for_the_running_hour(self):
        self.fill(30, [("A", 1, 20.0, 21.0, 50.0, 1)])
        self.h.rollup(self.NOW)
        self.assertEqual(self.h.db.execute("SELECT SUM(minutes) FROM hourly").fetchone()[0], 30)

    def test_snapshot_skips_devices_without_a_reading(self):
        store = LocalStore(Path(self.tmp.name) / "tado.db")
        rid = store.add_room("R")
        store.move_device("A", rid)
        hk = FakeHomeKit(["A", "B", "C"])
        hk.zones = {i: z | {"serial": z["serial"]} for i, z in hk.zones.items()}
        hk.state[1] |= {"current_temp": 19.5, "humidity": 50, "current_hc": MODE_HEAT}
        hk.state[3] |= {"current_temp": 18.0, "target_hc": MODE_OFF}
        rows = snapshot(store, hk)
        self.assertEqual(rows, [("A", rid, 19.5, 20.0, 50, 1), ("C", 0, 18.0, None, None, 0)])


if __name__ == "__main__":
    unittest.main()
