import unittest
from datetime import timedelta, timezone

import database
from app import app
from tests import TempDatabaseTestCase


class ApiTestCase(TempDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()

    def post_reading(self, body):
        return self.client.post("/api/readings", json=body)


class HappyPathTests(ApiTestCase):
    def test_dashboard_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"IoT Time-Series Monitor", res.data)

    def test_manual_insert_alert_flow(self):
        first = self.post_reading({"sensor_id": "TEMP-01", "value": 45})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json["alert"]["alert_type"], "HIGH_TEMPERATURE")

        second = self.post_reading({"sensor_id": "TEMP-01", "value": 46})
        self.assertIsNone(second.json["alert"])

        alerts = self.client.get("/api/alerts?limit=20").json
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["message"], "High Temperature Alert: 45.00 °C")

        sensors = {s["sensor_id"]: s for s in self.client.get("/api/sensors").json}
        self.assertTrue(sensors["TEMP-01"]["in_alert"])
        self.assertEqual(sensors["TEMP-01"]["latest"]["value"], 46)
        self.assertFalse(sensors["HUM-01"]["in_alert"])
        self.assertIsNone(sensors["HUM-01"]["latest"])

    def test_readings_stats_and_hourly_default_to_last_hour(self):
        for v in (20, 30, 40):
            self.post_reading({"sensor_id": "TEMP-01", "value": v})

        readings = self.client.get("/api/readings?sensor_id=TEMP-01").json
        self.assertEqual([r["value"] for r in readings], [20, 30, 40])
        self.assertEqual(set(readings[0]), {"id", "sensor_id", "sensor_name", "sensor_type",
                                            "value", "unit", "timestamp"})

        stats = self.client.get("/api/stats?sensor_id=TEMP-01").json
        self.assertEqual((stats["min"], stats["max"], stats["avg"], stats["count"]), (20, 40, 30, 3))

        hourly = self.client.get("/api/stats/hourly?sensor_id=TEMP-01").json
        self.assertEqual(sum(h["count"] for h in hourly), 3)

    def test_time_range_accepts_z_and_offsets(self):
        database.save_reading("HUM-01", 50, database.utc_now() - timedelta(hours=3))
        start = (database.utc_now() - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
        # Same instant written as UTC with Z, and as India time (+05:30).
        for start_text in (start + "Z", start + ".000Z"):
            res = self.client.get("/api/readings", query_string={"sensor_id": "HUM-01", "start": start_text})
            self.assertEqual(len(res.json), 1, start_text)
        ist = (database.utc_now() - timedelta(hours=4)).astimezone(
            timezone(timedelta(hours=5, minutes=30))).isoformat()
        res = self.client.get("/api/readings", query_string={"sensor_id": "HUM-01", "start": ist})
        self.assertEqual(len(res.json), 1)

    def test_delete_old_readings(self):
        now = database.utc_now()
        database.save_reading("PRES-01", 1000, now - timedelta(hours=30))
        database.save_reading("PRES-01", 1001, now)
        res = self.client.delete("/api/readings?older_than_hours=24")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["deleted"], 1)
        self.assertTrue(res.json["cutoff"].endswith("Z"))

    def test_sensors_include_alert_rule(self):
        sensors = {s["sensor_id"]: s for s in self.client.get("/api/sensors").json}
        self.assertEqual(sensors["TEMP-01"]["alert_rule"],
                         {"condition": ">", "threshold": 40.0, "alert_type": "HIGH_TEMPERATURE",
                          "label": "High Temperature Alert"})
        self.assertEqual(sensors["PRES-01"]["alert_rule"]["condition"], "<")

    def test_alerts_include_unit(self):
        self.post_reading({"sensor_id": "HUM-01", "value": 20})
        self.assertEqual(self.client.get("/api/alerts").json[0]["unit"], "%")

    def test_summary_is_calculated_from_the_database(self):
        empty = self.client.get("/api/summary").json
        self.assertEqual((empty["sensors_total"], empty["sensors_active"], empty["readings_stored"]), (3, 0, 0))
        self.assertIsNone(empty["database"]["first_reading"])

        now = database.utc_now()
        database.save_reading("TEMP-01", 45, now - timedelta(days=3))   # old alert: not "today"
        self.post_reading({"sensor_id": "TEMP-01", "value": 30})
        self.post_reading({"sensor_id": "TEMP-01", "value": 45})        # alert today
        self.post_reading({"sensor_id": "HUM-01", "value": 50})

        res = self.client.get("/api/summary")
        self.assertEqual(res.status_code, 200)
        s = res.json
        self.assertEqual(s["sensors_total"], 3)
        self.assertEqual(s["sensors_active"], 2)
        self.assertEqual(s["readings_stored"], 4)
        self.assertEqual(s["alerts_today"], 1)
        db = s["database"]
        self.assertEqual((db["engine"], db["connected"], db["tables"], db["sensors"], db["readings"], db["alerts"]),
                         ("SQLite", True, 3, 3, 4, 2))
        self.assertTrue(db["first_reading"].endswith("Z") and db["last_reading"].endswith("Z"))
        self.assertFalse(s["simulator"]["running"])   # no simulator thread in tests
        self.assertEqual(s["simulator"]["interval_seconds"], 5)
        self.assertEqual(s["history"], {"seed_days": 7.0, "seed_step_minutes": 5.0})


class ValidationTests(ApiTestCase):
    def assert_400(self, res):
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("error", res.json)

    def test_bad_query_parameters(self):
        urls = [
            "/api/readings",
            "/api/readings?sensor_id=XX-99",
            "/api/readings?sensor_id=TEMP-01'%20OR%201=1--",
            "/api/readings?sensor_id=TEMP-01&start=yesterday",
            "/api/readings?sensor_id=TEMP-01&start=2026-13-40T00:00:00",
            "/api/readings?sensor_id=TEMP-01&start=1900-01-01T00:00:00Z",
            "/api/readings?sensor_id=TEMP-01&start=2026-09-27T12:00:00Z&end=2026-09-27T11:00:00Z",
            "/api/readings?sensor_id=TEMP-01&limit=0",
            "/api/readings?sensor_id=TEMP-01&limit=abc",
            "/api/readings?sensor_id=TEMP-01&limit=-5",
            "/api/readings?sensor_id=TEMP-01&limit=20001",
            "/api/readings?sensor_id=TEMP-01&limit=" + "9" * 5000,
            "/api/stats",
            "/api/stats?sensor_id=temp-01",
            "/api/stats/hourly?sensor_id=",
            "/api/alerts?limit=0",
            "/api/alerts?limit=101",
            "/api/alerts?limit=1.5",
        ]
        for url in urls:
            with self.subTest(url=url[:80]):
                self.assert_400(self.client.get(url))

    def test_bad_delete_parameters(self):
        for query in ("", "?older_than_hours=", "?older_than_hours=abc", "?older_than_hours=0",
                      "?older_than_hours=-1", "?older_than_hours=nan", "?older_than_hours=inf",
                      "?older_than_hours=1e12"):
            with self.subTest(query=query):
                self.assert_400(self.client.delete("/api/readings" + query))

    def test_bad_post_bodies(self):
        bodies = [
            None,
            [],
            {"value": 45},
            {"sensor_id": "XX-99", "value": 45},
            {"sensor_id": ["TEMP-01"], "value": 45},
            {"sensor_id": "TEMP-01"},
            {"sensor_id": "TEMP-01", "value": "45"},
            {"sensor_id": "TEMP-01", "value": True},
            {"sensor_id": "TEMP-01", "value": 51},
            {"sensor_id": "TEMP-01", "value": 9.99},
            {"sensor_id": "HUM-01", "value": 96},
            {"sensor_id": "PRES-01", "value": 900},
            {"sensor_id": "TEMP-01", "value": 10 ** 400},
        ]
        for body in bodies:
            with self.subTest(body=str(body)[:60]):
                self.assert_400(self.post_reading(body))

    def test_non_json_and_nan_bodies(self):
        for data, ctype in (("not json", "application/json"),
                            ('{"sensor_id": "TEMP-01", "value": 45}', "text/plain"),
                            ('{"sensor_id": "TEMP-01", "value": NaN}', "application/json"),
                            ('{"sensor_id": "TEMP-01", "value": Infinity}', "application/json")):
            with self.subTest(data=data, ctype=ctype):
                self.assert_400(self.client.post("/api/readings", data=data, content_type=ctype))

    def test_rejected_requests_store_nothing(self):
        self.post_reading({"sensor_id": "TEMP-01", "value": 99})
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
