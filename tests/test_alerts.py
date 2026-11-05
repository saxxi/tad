"""Run with: .venv/bin/python -m unittest tests.test_alerts"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import local
from app.alerts import NOTIFY_AFTER, Alerts, check_bridge
from app.local import LocalStore, enforce
from tests.test_rules import FakeHomeKit, Frozen

THU = datetime(2026, 11, 5, tzinfo=ZoneInfo("Europe/Rome"))


def at(hour, minute=0, day=0):
    return THU + timedelta(days=day, hours=hour, minutes=minute)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalStore(Path(self.tmp.name) / "tado.db")
        self.addCleanup(self.store.db.close)
        self.room = self.store.add_room("Office")
        self.store.set_points(self.room, [{"days": "1234567", "time": "00:00", "celsius": 17}])
        self.store.move_device("V1", self.room)
        patcher = patch.object(local, "datetime", Frozen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, now):
        Frozen.current = now
        return self.store.plan(now)[self.room]


class OnDemandTest(Base):
    def boost(self, now, hours=3, extend=False):
        Frozen.current = now
        self.store.boost(self.room, hours, extend)

    def test_holds_for_the_set_time_then_returns_to_the_schedule(self):
        self.boost(at(10))
        self.assertEqual(self.state(at(12, 59))["mode"], "manual")
        s = self.state(at(13))
        self.assertEqual((s["mode"], s["want"]), ("schedule", 17))

    def test_ignores_schedule_changes_while_on(self):
        self.store.set_points(self.room, [{"days": "1234567", "time": "11:00", "celsius": 15}])
        self.boost(at(10))
        self.assertEqual(self.state(at(12))["mode"], "manual")

    def test_night_off_ends_it(self):
        self.boost(at(21))
        self.assertEqual(self.state(at(21, 59))["mode"], "manual")
        self.assertEqual(self.state(at(22))["mode"], "night")

    def test_during_night_off_it_lasts_an_hour_at_most(self):
        self.boost(at(23))
        self.assertEqual(self.state(at(23, 59))["mode"], "manual")
        self.assertEqual(self.state(at(0, day=1))["mode"], "night")

    def test_extend_adds_to_the_time_left(self):
        self.boost(at(10))
        self.boost(at(11), hours=1, extend=True)
        self.assertEqual(self.state(at(13, 59))["mode"], "manual")
        self.assertEqual(self.state(at(14))["mode"], "schedule")

    def test_extend_cannot_pass_night_off(self):
        self.boost(at(19))
        self.boost(at(20), hours=5, extend=True)
        self.assertEqual(self.state(at(22))["mode"], "night")

    def test_config_is_kept_per_room(self):
        self.store.set_on_demand(self.room, {"celsius": 21, "hours": 3})
        self.assertEqual({r["id"]: r["on_demand"] for r in self.store.rooms()}, {self.room: {"celsius": 21, "hours": 3, "effective": 21}})
        self.store.set_on_demand(self.room, None)
        self.assertIsNone(self.store.rooms()[0]["on_demand"])


class AlertsTest(Base):
    def setUp(self):
        super().setUp()
        self.alerts = Alerts(self.store)
        self.sent = []

    async def send(self, title, message, priority):
        self.sent.append((title, priority))

    def tick(self, hk):
        Frozen.current = at(23)
        asyncio.run(enforce(self.store, hk, self.alerts))

    def notify(self, now):
        asyncio.run(self.alerts.notify(self.send, now))

    def test_failing_valve_shows_at_once_and_clears_when_it_answers(self):
        hk = FakeHomeKit(["V1"], fail=("V1",))
        self.tick(hk)
        self.assertEqual(self.alerts.list()[0]["title"], "Office: valve not responding")
        self.assertIn("should be off", self.alerts.list()[0]["message"])
        hk.fail = ()
        self.tick(hk)
        self.assertEqual(self.alerts.list(), [])

    def test_held_room_is_not_reported(self):
        hk = FakeHomeKit(["V1"], fail=("V1",))
        self.tick(hk)
        Frozen.current = at(23)
        self.store.boost(self.room, 1)
        self.tick(hk)
        self.assertEqual(self.alerts.list(), [])

    def test_a_brief_miss_sends_nothing(self):
        self.alerts.set_phone(True)
        self.alerts.fail("k", "t", "m", now=1000)
        self.notify(1000 + NOTIFY_AFTER - 1)
        self.alerts.clear("k")
        self.notify(1000 + NOTIFY_AFTER + 1)
        self.assertEqual(self.sent, [])

    def test_lasting_failure_notifies_once_then_says_when_fixed(self):
        self.alerts.set_phone(True)
        self.alerts.fail("k", "Valve", "m", now=1000)
        self.notify(1000 + NOTIFY_AFTER)
        self.alerts.fail("k", "Valve", "m", now=1400)  # still failing
        self.notify(1000 + NOTIFY_AFTER + 60)
        self.assertEqual(self.sent, [("Valve", "high")])
        self.assertEqual(self.alerts.active["k"]["since"], 1000)
        self.alerts.clear("k")
        self.notify(2000)
        self.assertEqual(self.sent[-1], ("Valve: fixed", "default"))

    def test_failed_send_is_retried(self):
        self.alerts.set_phone(True)
        self.alerts.fail("k", "Valve", "m", now=1000)

        async def broken(*_):
            raise RuntimeError("no network")

        with self.assertRaises(RuntimeError):
            asyncio.run(self.alerts.notify(broken, 2000))
        self.notify(2030)
        self.assertEqual(self.sent, [("Valve", "high")])

    def test_phone_off_sends_nothing(self):
        self.alerts.fail("k", "Valve", "m", now=1000)
        self.notify(5000)
        self.assertEqual(self.sent, [])

    def test_topic_is_random_and_survives_turning_off(self):
        topic = self.alerts.set_phone(True)["topic"]
        self.assertGreater(len(topic), 20)
        self.assertEqual(self.alerts.set_phone(False)["topic"], topic)
        self.assertEqual(self.alerts.set_phone(True)["topic"], topic)

    def test_bridge_alert_follows_the_connection(self):
        class Hk:
            connected = False
            def is_paired(self):
                return True

        hk = Hk()
        check_bridge(self.alerts, hk)
        self.assertEqual(self.alerts.list()[0]["title"], "Bridge not responding")
        hk.connected = True
        check_bridge(self.alerts, hk)
        self.assertEqual(self.alerts.list(), [])


if __name__ == "__main__":
    unittest.main()
