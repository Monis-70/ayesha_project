import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import database
from tests import TempDatabaseTestCase

T0 = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


class SchemaTests(TempDatabaseTestCase):
    def test_init_db_can_run_twice_without_duplicate_sensors(self):
        database.init_db()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], 3)

    def test_foreign_key_rejects_unknown_sensor(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            database.save_reading("XX-99", 1.0, T0)

    def test_range_query_uses_time_series_index(self):
        with database.get_connection() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM readings "
                "WHERE sensor_id = ? AND timestamp BETWEEN ? AND ?", ("TEMP-01", "a", "b")).fetchall()
        self.assertIn("idx_readings_sensor_time", " ".join(row["detail"] for row in plan))


class SaveReadingTests(TempDatabaseTestCase):
    def test_alert_only_when_crossing_into_breach(self):
        results = [database.save_reading("TEMP-01", v, at(i * 5))
                   for i, v in enumerate([30, 45, 46, 30, 45])]
        created = [alert is not None for _, alert in results]
        self.assertEqual(created, [False, True, False, False, True])
        self.assertEqual(len(database.get_alerts(10)), 2)

    def test_returns_reading_with_api_timestamp(self):
        reading, alert = database.save_reading("HUM-01", 50.0, T0)
        self.assertIsNone(alert)
        self.assertEqual(reading["timestamp"], "2026-09-27T10:00:00Z")
        self.assertEqual(reading["value"], 50.0)


class QueryTests(TempDatabaseTestCase):
    def setUp(self):
        super().setUp()
        for i in range(10):
            database.save_reading("TEMP-01", 20 + i, at(i))
        database.save_reading("HUM-01", 50, at(5))

    def test_between_includes_both_ends_and_filters_by_sensor(self):
        rows = database.get_readings("TEMP-01", at(2), at(4), 100)
        self.assertEqual([r["value"] for r in rows], [22, 23, 24])
        self.assertTrue(all(r["sensor_id"] == "TEMP-01" for r in rows))

    def test_rows_are_joined_with_sensor_details(self):
        row = database.get_readings("TEMP-01", at(0), at(0), 1)[0]
        self.assertEqual((row["sensor_name"], row["sensor_type"], row["unit"]),
                         ("Temperature", "temperature", "°C"))

    def test_limit_keeps_newest_rows_sorted_oldest_first(self):
        rows = database.get_readings("TEMP-01", at(0), at(9), 3)
        self.assertEqual([r["value"] for r in rows], [27, 28, 29])
        self.assertEqual([r["timestamp"] for r in rows],
                         ["2026-09-27T10:00:07Z", "2026-09-27T10:00:08Z", "2026-09-27T10:00:09Z"])

    def test_stats(self):
        stats = database.get_stats("TEMP-01", at(0), at(9))
        self.assertEqual(stats, {"min": 20, "max": 29, "avg": 24.5, "count": 10})

    def test_stats_for_empty_range(self):
        stats = database.get_stats("PRES-01", at(0), at(9))
        self.assertEqual(stats, {"min": None, "max": None, "avg": None, "count": 0})

    def test_latest_reading_per_sensor(self):
        sensors = {s["sensor_id"]: s for s in database.get_sensors_with_latest()}
        self.assertEqual(list(sensors), ["TEMP-01", "HUM-01", "PRES-01"])
        self.assertEqual(sensors["TEMP-01"]["latest"],
                         {"value": 29, "timestamp": "2026-09-27T10:00:09Z"})
        self.assertIsNone(sensors["PRES-01"]["latest"])


class HourlyStatsTests(TempDatabaseTestCase):
    def test_group_by_hour(self):
        database.save_reading("PRES-01", 1000, T0.replace(minute=5))
        database.save_reading("PRES-01", 1010, T0.replace(minute=55))
        database.save_reading("PRES-01", 1020, T0.replace(hour=11, minute=10))
        rows = database.get_hourly_stats("PRES-01", T0, T0 + timedelta(hours=2))
        self.assertEqual(rows, [
            {"hour": "2026-09-27T10:00:00Z", "min": 1000, "max": 1010, "avg": 1005, "count": 2},
            {"hour": "2026-09-27T11:00:00Z", "min": 1020, "max": 1020, "avg": 1020, "count": 1},
        ])


class AlertsQueryTests(TempDatabaseTestCase):
    def test_newest_first_with_sensor_name(self):
        database.save_reading("TEMP-01", 45, at(0))
        database.save_reading("HUM-01", 20, at(10))
        rows = database.get_alerts(10)
        self.assertEqual([r["alert_type"] for r in rows], ["LOW_HUMIDITY", "HIGH_TEMPERATURE"])
        self.assertEqual(rows[0]["sensor_name"], "Humidity")
        self.assertEqual(len(database.get_alerts(1)), 1)


class DeleteTests(TempDatabaseTestCase):
    def test_delete_older_than_keeps_recent_rows(self):
        now = database.utc_now()
        database.save_reading("TEMP-01", 25, now - timedelta(hours=48))
        database.save_reading("TEMP-01", 26, now - timedelta(hours=30))
        database.save_reading("TEMP-01", 27, now - timedelta(hours=1))

        deleted, cutoff = database.delete_older_than(24)

        self.assertEqual(deleted, 2)
        self.assertLessEqual(abs((now - timedelta(hours=24) - cutoff).total_seconds()), 5)
        rows = database.get_readings("TEMP-01", now - timedelta(days=3), now, 100)
        self.assertEqual([r["value"] for r in rows], [27])


if __name__ == "__main__":
    unittest.main()
