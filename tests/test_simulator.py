import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import alerts
import app
import config
import database
import simulator
from tests import TempDatabaseTestCase

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
STEPS = config.BACKFILL_HOURS * 3600 // config.BACKFILL_STEP_SECONDS


def local_time_at(hour):
    return datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0)


class GenerateValueTests(unittest.TestCase):
    def test_values_stay_inside_sensor_range_even_with_many_spikes(self):
        rng = random.Random(1)
        when = datetime.now(timezone.utc)
        with mock.patch.object(config, "SPIKE_PROBABILITY", 0.5):
            for sid, sensor in config.SENSORS.items():
                drift = 0.0
                for _ in range(10000):
                    value, drift = simulator.generate_value(sid, when, drift, rng)
                    self.assertGreaterEqual(value, sensor["low"], sid)
                    self.assertLessEqual(value, sensor["high"], sid)

    def test_drift_stays_bounded(self):
        rng = random.Random(2)
        when = datetime.now(timezone.utc)
        for sid, sensor in config.SENSORS.items():
            drift = 0.0
            for _ in range(10000):
                _, drift = simulator.generate_value(sid, when, drift, rng)
                self.assertLessEqual(abs(drift), config.DRIFT_LIMIT_FACTOR * sensor["noise"], sid)

    def test_values_are_rounded_to_two_decimals(self):
        value, _ = simulator.generate_value("TEMP-01", datetime.now(timezone.utc), 0.0, random.Random(4))
        self.assertEqual(value, round(value, 2))


class SignalModelTests(unittest.TestCase):
    def test_temperature_is_warmer_in_the_afternoon_than_at_night(self):
        afternoon = simulator.temperature_signal(local_time_at(15))
        night = simulator.temperature_signal(local_time_at(3))
        self.assertGreater(afternoon - night, config.TEMPERATURE_MODEL["daily_amplitude"])

    def test_humidity_moves_opposite_to_temperature(self):
        self.assertLess(simulator.humidity_signal(local_time_at(15)),
                        simulator.humidity_signal(local_time_at(3)))

    def test_pressure_changes_slowly(self):
        start = datetime(2026, 9, 20, tzinfo=timezone.utc)
        values = [simulator.pressure_signal(start + timedelta(minutes=5 * i)) for i in range(2016)]
        steps = [abs(b - a) for a, b in zip(values, values[1:])]
        self.assertLess(max(steps), 0.3)
        self.assertGreater(max(values) - min(values), 5)   # but it does change over the week

    def test_days_differ_but_each_day_is_repeatable(self):
        days = [datetime(2026, 9, d, 15, tzinfo=timezone.utc) for d in range(20, 27)]
        temps = [simulator.temperature_signal(d) for d in days]
        self.assertGreater(max(temps) - min(temps), 0.5)
        self.assertEqual(temps, [simulator.temperature_signal(d) for d in days])

    def test_signal_is_continuous_across_midnight(self):
        midnight = local_time_at(0)
        before = simulator.temperature_signal(midnight - timedelta(minutes=5))
        after = simulator.temperature_signal(midnight + timedelta(minutes=5))
        self.assertLess(abs(after - before), 0.5)


class SeedHistoryTests(TempDatabaseTestCase):
    def seed(self, rng_seed=5):
        return simulator.backfill(now=NOW, rng=random.Random(rng_seed))

    def test_fresh_database_gets_seven_days_of_five_minute_readings(self):
        self.assertEqual(self.seed(), STEPS * len(config.SENSORS))
        self.assertEqual(STEPS, 2016)
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT sensor_id, COUNT(*) AS n, MIN(timestamp) AS first, MAX(timestamp) AS last "
                "FROM readings GROUP BY sensor_id").fetchall()
        self.assertEqual({r["sensor_id"] for r in rows}, set(config.SENSORS))
        for r in rows:
            self.assertEqual(r["n"], STEPS)
            self.assertEqual(r["first"], "2026-09-20 12:05:00")
            self.assertEqual(r["last"], "2026-09-27 12:00:00")

    def test_values_stay_inside_valid_ranges(self):
        self.seed()
        with database.get_connection() as conn:
            for sid, sensor in config.SENSORS.items():
                lo, hi = conn.execute("SELECT MIN(value), MAX(value) FROM readings WHERE sensor_id = ?",
                                      (sid,)).fetchone()
                self.assertGreaterEqual(lo, sensor["low"], sid)
                self.assertLessEqual(hi, sensor["high"], sid)

    def test_most_values_are_in_the_typical_range(self):
        self.seed()
        typical = {"TEMP-01": (20, 38), "HUM-01": (35, 80), "PRES-01": (995, 1025)}
        with database.get_connection() as conn:
            for sid, (lo, hi) in typical.items():
                values = [v for (v,) in conn.execute("SELECT value FROM readings WHERE sensor_id = ?", (sid,))]
                inside = sum(lo <= v <= hi for v in values) / len(values)
                self.assertGreater(inside, 0.85, sid)

    def test_anomalies_exist_but_most_readings_are_normal(self):
        self.seed()
        with database.get_connection() as conn:
            for sid in config.SENSORS:
                values = [v for (v,) in conn.execute("SELECT value FROM readings WHERE sensor_id = ?", (sid,))]
                breached = sum(alerts.is_breached(sid, v) for v in values)
                self.assertGreater(breached, 0, sid)
                self.assertLess(breached / len(values), 0.08, sid)

    def test_every_alert_type_is_generated_in_a_demo_friendly_amount(self):
        self.seed()
        with database.get_connection() as conn:
            counts = dict(conn.execute("SELECT alert_type, COUNT(*) FROM alerts GROUP BY alert_type").fetchall())
        self.assertGreaterEqual(counts.get("HIGH_TEMPERATURE", 0), 3)
        self.assertGreaterEqual(counts.get("LOW_HUMIDITY", 0), 3)
        self.assertGreaterEqual(counts.get("LOW_PRESSURE", 0), 2)
        self.assertTrue(10 <= sum(counts.values()) <= 30, counts)

    def test_history_alerts_only_on_crossing(self):
        self.seed(rng_seed=6)
        with database.get_connection() as conn:
            alert_rows = conn.execute("SELECT sensor_id, value, timestamp FROM alerts").fetchall()
            self.assertGreater(len(alert_rows), 0)
            for a in alert_rows:
                self.assertTrue(alerts.is_breached(a["sensor_id"], a["value"]))
                prev = conn.execute(
                    "SELECT value FROM readings WHERE sensor_id = ? AND timestamp < ? "
                    "ORDER BY timestamp DESC LIMIT 1", (a["sensor_id"], a["timestamp"])).fetchone()
                if prev is not None:
                    self.assertFalse(alerts.is_breached(a["sensor_id"], prev["value"]))

    def test_every_configured_event_lies_inside_the_seeded_period(self):
        start = NOW - timedelta(hours=config.BACKFILL_HOURS)
        for event in simulator.history_events(NOW):
            self.assertGreater(event["start"], start, event["name"])
            self.assertLess(event["end"], NOW, event["name"])

    def test_each_designed_event_raises_exactly_one_alert(self):
        self.seed()
        with database.get_connection() as conn:
            for event in simulator.history_events(NOW):
                count = conn.execute(
                    "SELECT COUNT(*) FROM alerts WHERE sensor_id = ? AND timestamp BETWEEN ? AND ?",
                    (event["sensor_id"], database.to_db_time(event["start"]),
                     database.to_db_time(event["end"]))).fetchone()[0]
                self.assertEqual(count, 1, event["name"])
            total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(total, len(config.HISTORY_EVENTS))

    def test_history_has_no_random_glitches(self):
        with mock.patch.object(config, "SPIKE_PROBABILITY", 1.0):
            self.seed()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
                             len(config.HISTORY_EVENTS))


class StartupTests(TempDatabaseTestCase):
    def test_repeated_startup_does_not_seed_twice(self):
        self.assertEqual(app.prepare_database(), STEPS * len(config.SENSORS))
        self.assertEqual(app.prepare_database(), 0)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                             STEPS * len(config.SENSORS))

    def test_live_simulator_continues_smoothly_after_seeding(self):
        with mock.patch.object(config, "SPIKE_PROBABILITY", 0.0):
            app.prepare_database()
            before = {s["sensor_id"]: s["latest"]["value"] for s in database.get_sensors_with_latest()}
            simulator.Simulator(rng=random.Random(7)).tick()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                             STEPS * len(config.SENSORS) + len(config.SENSORS))
        after = {s["sensor_id"]: s["latest"]["value"] for s in database.get_sensors_with_latest()}
        allowed = {"TEMP-01": 2.0, "HUM-01": 5.0, "PRES-01": 1.0}
        for sid, jump in allowed.items():
            self.assertLess(abs(after[sid] - before[sid]), jump, sid)


class SimulatorTickTests(TempDatabaseTestCase):
    def test_tick_stores_one_reading_per_sensor(self):
        sim = simulator.Simulator(rng=random.Random(7))
        sim.tick()
        sim.tick()
        self.assertIsNotNone(sim.last_tick)
        self.assertFalse(sim.is_running())
        now = database.utc_now()
        for sid in config.SENSORS:
            rows = database.get_readings(sid, now - timedelta(minutes=1), now, 100)
            self.assertEqual(len(rows), 2, sid)


if __name__ == "__main__":
    unittest.main()
