"""Run with: .venv/bin/python -m unittest tests.test_costs"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from app.costs import Costs, build_forecast, calibrate, climate_normals, degree_days, gas_per_valve_hour, season_for, DEFAULTS
from app.history import History
from app.local import TZ, LocalStore

TODAY = date(2026, 11, 20)


def winters(temp=8.0, years=range(2016, 2026)):
    out = {}
    for y in years:
        d = date(y, 10, 15)
        while d <= date(y + 1, 4, 15):
            out[d] = temp + (y - 2020) * 0.2  # winters differ a little
            d += timedelta(days=1)
    return out


NORMALS = climate_normals(winters())


def forecast(usage=None, minutes=None, weather=None, gas=0.1, measured=False, price=1.0, budget=None):
    day = TODAY - timedelta(days=1)
    usage = {day: {1: 8.0, 2: 2.0}} if usage is None else usage
    minutes = {day: 1440} if minutes is None else minutes
    return build_forecast(TODAY, usage, minutes, weather or {}, NORMALS, gas, measured, price, budget, {1: "Living room", 2: "Office"})


class SeasonTest(unittest.TestCase):
    def test_season_runs_15_october_to_15_april(self):
        self.assertEqual(season_for(date(2026, 11, 5)), (date(2026, 10, 15), date(2027, 4, 15)))
        self.assertEqual(season_for(date(2027, 4, 15)), (date(2026, 10, 15), date(2027, 4, 15)))

    def test_between_seasons_means_the_next_one(self):
        self.assertEqual(season_for(date(2027, 4, 16))[0], date(2027, 10, 15))
        self.assertEqual(season_for(date(2027, 10, 14))[0], date(2027, 10, 15))

    def test_degree_days_follow_the_eurostat_rule(self):
        self.assertEqual(degree_days(8), 10)
        self.assertEqual(degree_days(15), 3)
        self.assertEqual(degree_days(15.1), 0)


class NormalsTest(unittest.TestCase):
    def test_range_brackets_the_average_winter(self):
        _, mild, cold = NORMALS
        self.assertLess(mild, 1)
        self.assertGreater(cold, 1)

    def test_incomplete_seasons_are_not_enough(self):
        self.assertIsNone(climate_normals({date(2025, 11, 1): 8.0}))


class ForecastTest(unittest.TestCase):
    def test_nothing_to_scale_from_yet(self):
        self.assertIsNone(forecast(usage={}, minutes={}))

    def test_a_house_that_never_called_for_heat_has_no_forecast(self):
        self.assertIsNone(forecast(usage={TODAY: {1: 0.0}}, minutes={TODAY: 600}))

    def test_total_is_recorded_plus_scaled_cold(self):
        f = forecast(weather={TODAY - timedelta(days=1): 8.0})  # 10 valve-hours on a 10 degree-day day
        self.assertAlmostEqual(f["model"]["valve_hours_per_degree_day"], 1.0)
        cold = f["model"]["season_degree_days"]
        self.assertAlmostEqual(f["forecast"]["eur"], cold * 0.1, delta=0.05)

    def test_recording_gaps_are_estimated_not_counted_as_recorded(self):
        f = forecast(weather={TODAY - timedelta(days=1): 8.0})
        self.assertAlmostEqual(f["spent"]["recorded_eur"], 1.0)
        self.assertGreater(f["spent"]["estimated_eur"], 0)  # 15 Oct until recording began
        self.assertEqual(f["today_eur"], 0)

    def test_a_part_day_scales_by_the_share_recorded(self):
        whole = forecast(weather={TODAY - timedelta(days=1): 8.0})
        half = forecast(usage={TODAY - timedelta(days=1): {1: 5.0}}, minutes={TODAY - timedelta(days=1): 720}, weather={TODAY - timedelta(days=1): 8.0})
        self.assertAlmostEqual(whole["model"]["valve_hours_per_degree_day"], half["model"]["valve_hours_per_degree_day"])

    def test_whole_days_set_the_rate_once_there_is_one(self):
        whole, part = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
        f = forecast(usage={whole: {1: 10.0}, part: {1: 9.0}}, minutes={whole: 1440, part: 300}, weather={whole: 8.0, part: 8.0})
        self.assertAlmostEqual(f["model"]["valve_hours_per_degree_day"], 1.0)

    def test_range_contains_the_forecast_and_tightens_with_meter_data(self):
        rough = forecast()
        settled = forecast(minutes={TODAY - timedelta(days=1): 1440 * 8})
        measured = forecast(minutes={TODAY - timedelta(days=1): 1440 * 8}, measured=True)
        for f in (rough, settled, measured):
            self.assertLess(f["forecast"]["low"], f["forecast"]["eur"])
            self.assertGreater(f["forecast"]["high"], f["forecast"]["eur"])
        spread = lambda f: (f["forecast"]["high"] - f["forecast"]["low"]) / f["forecast"]["eur"]
        self.assertGreater(spread(rough), spread(settled))
        self.assertGreater(spread(settled), spread(measured))
        self.assertEqual([f["confidence"]["level"] for f in (rough, settled, measured)], ["rough", "estimate", "measured"])

    def test_cumulative_series_never_falls_and_spans_the_season(self):
        f = forecast()
        self.assertEqual((f["series"][0]["d"], f["series"][-1]["d"]), ("2026-10-15", "2027-04-15"))
        eur = [p["eur"] for p in f["series"]]
        self.assertEqual(eur, sorted(eur))

    def test_rooms_and_months_add_up_to_the_forecast(self):
        f = forecast()
        self.assertAlmostEqual(sum(r["season_eur"] for r in f["rooms"]), f["forecast"]["eur"], delta=0.05)
        self.assertAlmostEqual(sum(m["eur"] for m in f["months"]), f["forecast"]["eur"], delta=0.05)
        self.assertEqual(f["rooms"][0]["name"], "Living room")
        self.assertEqual(f["rooms"][0]["share"], 0.8)

    def test_budget_line_follows_the_cold_and_ends_at_the_budget(self):
        f = forecast(budget=900)
        aim = [p["aim"] for p in f["series"]]
        self.assertEqual(aim[-1], 900)
        self.assertEqual(aim, sorted(aim))
        self.assertAlmostEqual(f["budget"]["forecast_gap_eur"], f["forecast"]["eur"] - 900, delta=0.01)


class CalibrationTest(unittest.TestCase):
    USAGE = [(1000 + i, 1, 1.0) for i in range(40)]

    def test_assumed_gas(self):
        self.assertAlmostEqual(gas_per_valve_hour(DEFAULTS), 1 / (10.7 * 0.9))

    def test_measures_gas_after_removing_hot_water_and_cooking(self):
        readings = [(1000, 100.0), (1000 + 10 * 86400, 114.0)]
        self.assertAlmostEqual(calibrate(readings, self.USAGE, 0.4), (14 - 4) / 40)

    def test_needs_a_week_of_readings_and_enough_heating(self):
        self.assertIsNone(calibrate([(1000, 100.0)], self.USAGE, 0.4))
        self.assertIsNone(calibrate([(1000, 100.0), (1000 + 86400, 104.0)], self.USAGE, 0.4))
        self.assertIsNone(calibrate([(1000, 100.0), (1000 + 10 * 86400, 114.0)], self.USAGE[:5], 0.4))
        self.assertIsNone(calibrate([(1000, 100.0), (1000 + 10 * 86400, 101.0)], self.USAGE, 0.4))  # less than hot water alone


class ReportTest(unittest.TestCase):
    NOW = int(datetime(2026, 11, 20, 12, tzinfo=TZ).timestamp())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalStore(Path(self.tmp.name) / "tado.db")
        self.rid = self.store.add_room("Living room")
        self.history = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.history.db.close)
        self.addCleanup(self.store.db.close)
        self.costs = Costs(self.history, self.store)

    def fill(self):
        self.costs.save_climate([(d.isoformat(), t) for d, t in winters().items()])
        self.costs.save_weather([(self.NOW - 3600 * i, 8.0) for i in range(48)])
        for i in range(60):
            ts = self.NOW - 3600 - 60 * i
            self.history.record(ts - ts % 60, [("VA1", self.rid, 19.0, 20.0, 50, 1), ("RU1", self.rid, 19.0, 20.0, 50, 1)])

    def test_waits_for_past_winters(self):
        report = self.costs.report(self.NOW)
        self.assertFalse(report["ready"])
        self.assertIn("past winters", report["reason"])

    def test_wall_thermostat_is_not_a_radiator(self):
        self.fill()
        report = self.costs.report(self.NOW)
        self.assertTrue(report["ready"])
        self.assertEqual(report["rooms"][0]["valve_hours"], 1.0)

    def test_settings_change_the_price_and_survive(self):
        self.fill()
        before = self.costs.report(self.NOW)["forecast"]["eur"]
        self.costs.update_settings({"price": DEFAULTS["price"] * 2})
        after = self.costs.report(self.NOW)
        self.assertAlmostEqual(after["forecast"]["eur"], before * 2, delta=0.05)
        self.assertEqual(self.store.get_setting("costs")["price"], DEFAULTS["price"] * 2)

    def test_too_little_heating_between_readings_keeps_the_assumption(self):
        self.fill()
        start = self.NOW - 3600 * 2
        self.costs._usage = None
        self.costs.add_reading(100.0, start - 8 * 86400)
        self.costs.add_reading(100.0 + 8 * 0.4 + 5, self.NOW)
        report = self.costs.report(self.NOW)
        self.assertFalse(report["confidence"]["measured"])  # only 1 valve-hour between them

    def test_enough_heating_between_readings_replaces_the_assumption(self):
        self.fill()
        for i in range(25):
            ts = self.NOW - 86400 - 3600 * i
            self.history.record(ts - ts % 60, [("VA1", self.rid, 19.0, 20.0, 50, 1)])
        # each of those is one minute: heating time is what's recorded, so 60 minutes per valve-hour
        self.history.db.executemany(
            "INSERT OR REPLACE INTO samples VALUES (?, 'VA1', ?, 19.0, 20.0, 50, 1)",
            [(self.NOW - 2 * 86400 - 60 * i - self.NOW % 60, self.rid) for i in range(1500)],
        )
        self.history.db.commit()
        self.costs.add_reading(100.0, self.NOW - 8 * 86400)
        self.costs.add_reading(100.0 + 8 * 0.4 + 5, self.NOW)
        report = self.costs.report(self.NOW)
        self.assertTrue(report["confidence"]["measured"])
        self.assertEqual(report["confidence"]["level"], "measured")


if __name__ == "__main__":
    unittest.main()
