"""Run with: .venv/bin/python -m unittest tests.test_air"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.air import absolute_humidity, airing_minutes, detect_windows, heating_without_effect, rate_hours
from app.alerts import Alerts, check_rooms
from app.local import LocalStore

T0 = int(datetime(2026, 11, 5, 8, tzinfo=ZoneInfo("Europe/Rome")).timestamp())


def series(temps, heating=0, start=T0):
    return [(start + i * 60, t, heating) for i, t in enumerate(temps)]


class WindowTest(unittest.TestCase):
    def test_a_fast_fall_is_a_window_and_it_ends_when_the_room_recovers(self):
        pts = series([20.0] * 20 + [19.8, 19.5, 19.2, 19.0, 18.9] + [18.9, 19.0, 19.2] + [19.3] * 5)
        e = detect_windows(pts)
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["drop"], 1.1)
        self.assertIsNotNone(e[0]["end"])

    def test_still_falling_means_still_open(self):
        e = detect_windows(series([20.0] * 20 + [19.7, 19.4, 19.2, 19.0]))
        self.assertIsNone(e[-1]["end"])

    def test_a_room_that_stopped_reporting_is_not_still_open(self):
        pts = series([20.0] * 20 + [19.7, 19.4, 19.2, 19.0])
        self.assertIsNone(detect_windows(pts, pts[-1][0] + 300)[-1]["end"])
        self.assertEqual(detect_windows(pts, pts[-1][0] + 3600)[-1]["end"], pts[-1][0])

    def test_normal_overnight_cooling_is_not_a_window(self):
        temps = [20 - 0.1 * (i // 30) for i in range(600)]  # 0.2° an hour
        self.assertEqual(detect_windows(series(temps)), [])

    def test_a_cooling_valve_after_heating_is_ignored(self):
        heat = series([20.0] * 10, heating=1)
        cool = series([19.9, 19.6, 19.3, 19.0], start=T0 + 600)
        self.assertEqual(detect_windows(heat + cool), [])

    def test_a_fall_while_heating_counts_and_says_so(self):
        e = detect_windows(series([20.0] * 20 + [19.6, 19.2, 18.9], heating=1))
        self.assertTrue(e[0]["heating"])

    def test_opened_and_reopened_is_one_event(self):
        temps = [20.0] * 16 + [19.5, 19.0] + [19.3] * 6 + [18.8, 18.5] + [18.8] * 6
        self.assertEqual(len(detect_windows(series(temps))), 1)


class NoEffectTest(unittest.TestCase):
    def test_two_flat_hours_of_heating_is_a_problem(self):
        self.assertTrue(heating_without_effect(series([19.0] * 130, heating=1)))

    def test_warming_is_fine(self):
        self.assertFalse(heating_without_effect(series([19 + i * 0.01 for i in range(130)], heating=1)))

    def test_short_or_stopped_heating_is_fine(self):
        self.assertFalse(heating_without_effect(series([19.0] * 60, heating=1)))
        self.assertFalse(heating_without_effect(series([19.0] * 130, heating=1) + series([19.0], start=T0 + 7900)))


class RatingTest(unittest.TestCase):
    def hours(self, over=None):
        over = over or {}
        out = []
        for i in range(14):  # 08:00 to 21:00
            h = {"hour": T0 + i * 3600, "temp": 8.0 + min(i, 13 - i), "humidity": 70.0, "rain": 0.0, "aqi": 30.0}
            out.append(h | over.get(i, {}))
        return out

    def test_physics(self):
        self.assertAlmostEqual(absolute_humidity(20, 50), 8.6, delta=0.1)
        self.assertEqual([airing_minutes(t) for t in (2, 8, 15)], [5, 10, 15])

    def test_best_block_is_the_warmest_two_hours(self):
        rated, best = rate_hours(self.hours(), 10.0, T0)
        self.assertEqual((best["start"], best["end"]), (T0 + 6 * 3600, T0 + 8 * 3600))
        self.assertEqual([r["rating"] for r in rated].count("best"), 2)

    def test_rain_and_smog_are_avoided_even_when_warm(self):
        rated, best = rate_hours(self.hours({6: {"rain": 1.0}, 7: {"aqi": 75.0}}), 10.0, T0)
        self.assertEqual((rated[6]["reason"], rated[7]["reason"]), ("rain", "poor air"))
        self.assertNotIn(best["start"], (T0 + 6 * 3600, T0 + 7 * 3600))

    def test_no_point_when_outside_is_not_drier(self):
        rated, best = rate_hours(self.hours(), 3.0, T0)
        self.assertTrue(all(r["reason"] == "not drier outside" for r in rated))
        self.assertIsNone(best)

    def test_past_hours_and_the_night_are_left_out(self):
        rated, _ = rate_hours(self.hours() + [{"hour": T0 + 15 * 3600, "temp": 8.0, "humidity": 70.0, "rain": 0.0, "aqi": 30.0}], 10.0, T0 + 3 * 3600 + 5)
        self.assertEqual(rated[0]["hour"], T0 + 3 * 3600)
        self.assertEqual(rated[-1]["hour"], T0 + 13 * 3600)

    def test_missing_air_quality_is_not_a_reason_to_avoid(self):
        rated, best = rate_hours(self.hours({i: {"aqi": None} for i in range(14)}), 10.0, T0)
        self.assertIsNotNone(best)
        self.assertIsNone(best["aqi"])


class RoomChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = LocalStore(Path(self.tmp.name) / "t.db")
        self.addCleanup(store.db.close)
        self.alerts = Alerts(store)

    def test_open_window_waits_longer_before_notifying_and_clears(self):
        pts = series([20.0] * 20 + [19.7, 19.4, 19.1])
        check_rooms(self.alerts, {1: pts}, {1: "Living room"})
        a = self.alerts.active["window:1"]
        self.assertEqual((a["kind"], a["room_id"], a["notify_after"]), ("window", 1, 1200))
        self.assertEqual(self.alerts.due(a["since"] + 600), [])
        self.assertEqual(len(self.alerts.due(a["since"] + 1200)), 1)
        check_rooms(self.alerts, {1: series([20.0] * 30)}, {1: "Living room"})
        self.assertEqual(self.alerts.list(), [])

    def test_room_not_warming_is_reported(self):
        check_rooms(self.alerts, {1: series([19.0] * 130, heating=1)}, {1: "Office"})
        self.assertEqual(self.alerts.list()[0]["title"], "Office: not warming up")


if __name__ == "__main__":
    unittest.main()
