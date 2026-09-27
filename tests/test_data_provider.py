"""Tests for the Vercel demo mode: generated data, cookie-based demo buttons, no SQLite, no thread."""
import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import alerts
import app as app_module
import config
import data_provider
import database
import simulator
from tests import TempDatabaseTestCase

NOW = datetime(2026, 9, 27, 12, 0, 3, tzinfo=timezone.utc)
ENDPOINTS = ["/api/sensors", "/api/summary", "/api/alerts",
             "/api/readings?sensor_id=TEMP-01", "/api/stats?sensor_id=TEMP-01",
             "/api/stats/hourly?sensor_id=TEMP-01"]


def no_sqlite(*args, **kwargs):
    raise AssertionError("demo mode must not open the SQLite database")


class DetectModeTests(unittest.TestCase):
    def test_local_is_sqlite_and_vercel_is_demo(self):
        self.assertEqual(config.detect_data_mode({}), "sqlite")
        self.assertEqual(config.detect_data_mode({"VERCEL": "1"}), "demo")
        self.assertEqual(config.detect_data_mode({"AWS_LAMBDA_FUNCTION_NAME": "fn"}), "demo")

    def test_override(self):
        self.assertEqual(config.detect_data_mode({"IOT_DATA_MODE": "demo"}), "demo")
        self.assertEqual(config.detect_data_mode({"VERCEL": "1", "IOT_DATA_MODE": "sqlite"}), "sqlite")
        with self.assertRaisesRegex(ValueError, "IOT_DATA_MODE must be"):
            config.detect_data_mode({"IOT_DATA_MODE": "mysql"})

    def test_tests_run_in_sqlite_mode(self):
        self.assertEqual(config.DATA_MODE, "sqlite")


class GeneratedDataTests(unittest.TestCase):
    def setUp(self):
        self.data = data_provider.DemoData(now=NOW)

    def rows(self, sensor_id, delta, limit=20000):
        return self.data.get_readings(sensor_id, NOW - delta, NOW, limit)

    def test_same_timestamp_gives_same_value(self):
        a = self.rows("TEMP-01", timedelta(days=7))
        b = data_provider.DemoData(now=NOW).get_readings("TEMP-01", NOW - timedelta(days=7), NOW, 20000)
        self.assertEqual(a, b)
        self.assertEqual(data_provider.value_at("HUM-01", 1790000000), data_provider.value_at("HUM-01", 1790000000))

    def test_every_preset_range_has_enough_points(self):
        # NOW is 12:00:03, so 15 min holds 11:45:05 ... 12:00:00 = 180 readings 5 s apart.
        self.assertEqual(len(self.rows("TEMP-01", timedelta(minutes=15))), 180)
        self.assertEqual(len(self.rows("TEMP-01", timedelta(hours=1))), 720)
        self.assertGreater(len(self.rows("TEMP-01", timedelta(hours=6))), 750)
        self.assertGreater(len(self.rows("TEMP-01", timedelta(hours=24))), 950)
        week = self.rows("TEMP-01", timedelta(days=7))
        self.assertGreater(len(week), 2700)
        first = datetime.fromisoformat(week[0]["timestamp"])
        self.assertLess(NOW - first, timedelta(days=7))
        self.assertGreater(NOW - first, timedelta(days=6, hours=23))

    def test_rows_are_sorted_unique_and_limit_keeps_newest(self):
        week = self.rows("PRES-01", timedelta(days=7))
        stamps = [r["timestamp"] for r in week]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len({r["id"] for r in week}), len(week))
        self.assertEqual(self.rows("PRES-01", timedelta(days=7), limit=5), week[-5:])

    def test_values_are_realistic(self):
        typical = {"TEMP-01": (20, 38), "HUM-01": (35, 80), "PRES-01": (995, 1025)}
        for sid, (lo, hi) in typical.items():
            values = [r["value"] for r in self.rows(sid, timedelta(days=7))]
            sensor = config.SENSORS[sid]
            self.assertTrue(all(sensor["low"] <= v <= sensor["high"] for v in values), sid)
            self.assertGreater(sum(lo <= v <= hi for v in values) / len(values), 0.85, sid)
            breached = sum(alerts.is_breached(sid, v) for v in values) / len(values)
            self.assertTrue(0 < breached < 0.08, (sid, breached))

    def test_no_jump_where_live_readings_start(self):
        rows = self.rows("TEMP-01", timedelta(hours=2))
        values = [r["value"] for r in rows]
        self.assertLess(max(abs(b - a) for a, b in zip(values, values[1:])), 1.5)

    def test_historical_alerts_match_the_designed_events(self):
        found = self.data.get_alerts(100)
        counts = {t: sum(a["alert_type"] == t for a in found)
                  for t in ("HIGH_TEMPERATURE", "LOW_HUMIDITY", "LOW_PRESSURE")}
        self.assertEqual(counts, {"HIGH_TEMPERATURE": 6, "LOW_HUMIDITY": 5, "LOW_PRESSURE": 3})
        self.assertEqual([a["timestamp"] for a in found], sorted((a["timestamp"] for a in found), reverse=True))
        for a in found:
            self.assertTrue(alerts.is_breached(a["sensor_id"], a["value"]))
            self.assertEqual(set(a), {"id", "sensor_id", "sensor_name", "unit", "alert_type", "message",
                                      "value", "timestamp"})

    def test_alerts_do_not_move_as_time_passes(self):
        later = data_provider.DemoData(now=NOW + timedelta(seconds=25)).get_alerts(100)
        self.assertEqual(later, self.data.get_alerts(100))

    def test_stats_and_hourly_agree_with_readings(self):
        start, end = NOW - timedelta(hours=24), NOW
        values = [r["value"] for r in self.data.get_readings("HUM-01", start, end, 20000)]
        stats = self.data.get_stats("HUM-01", start, end)
        self.assertEqual((stats["min"], stats["max"], stats["count"]), (min(values), max(values), len(values)))
        self.assertAlmostEqual(stats["avg"], sum(values) / len(values), places=2)
        hourly = self.data.get_hourly_stats("HUM-01", start, end)
        self.assertEqual(sum(h["count"] for h in hourly), len(values))
        self.assertTrue(all(h["hour"].endswith(":00:00Z") for h in hourly))
        empty = self.data.get_stats("HUM-01", NOW - timedelta(days=30), NOW - timedelta(days=20))
        self.assertEqual(empty, {"min": None, "max": None, "avg": None, "count": 0})

    def test_every_event_repeats_inside_each_week(self):
        this_week = simulator.to_local(NOW).toordinal() // 7
        for week in (this_week - 1, this_week):
            events = data_provider.events_for_week(week)
            self.assertEqual(len(events), len(config.HISTORY_EVENTS))
            start = data_provider._local_midnight(week * 7)
            for e in events:
                self.assertTrue(start <= e["start"] < e["end"] <= start + timedelta(days=7), e["name"])

    def test_day_night_zone_is_configurable(self):
        with mock.patch.object(config, "LOCAL_TZ", timezone(timedelta(hours=5, minutes=30))):
            data_provider.events_for_week.cache_clear()
            try:
                week = simulator.to_local(NOW).toordinal() // 7
                spike = next(e for e in data_provider.events_for_week(week) if e["name"] == "Temperature spike")
                self.assertEqual((spike["start"].hour, spike["start"].minute), (11, 30))
                self.assertEqual(spike["start"].utcoffset(), timedelta(hours=5, minutes=30))
            finally:
                data_provider.events_for_week.cache_clear()


class CookieTests(unittest.TestCase):
    def encode(self, items):
        return base64.urlsafe_b64encode(json.dumps(items).encode()).decode()

    def test_malformed_or_tampered_cookies_are_ignored(self):
        t = int(NOW.timestamp()) - 60
        bad = ["", "not base64!", self.encode({"x": 1}), self.encode([1, 2]),
               self.encode([["XX-99", 45, t, None]]),
               self.encode([["TEMP-01", 999, t, None]]),                  # out of sensor range
               self.encode([["TEMP-01", True, t, None]]),
               self.encode([["TEMP-01", 30, t, "HIGH_TEMPERATURE"]]),     # not actually breached
               self.encode([["TEMP-01", 45, t, "LOW_PRESSURE"]]),         # wrong alert type
               self.encode([["TEMP-01", 45, t - 30 * 86400, None]])]      # outside the 7-day window
        for raw in bad:
            with self.subTest(raw=raw[:30]):
                self.assertEqual(data_provider.DemoData.from_cookie(raw, now=NOW).manual, [])

    def test_valid_cookie_round_trip(self):
        t = int(NOW.timestamp()) - 60
        data = data_provider.DemoData.from_cookie(self.encode([["TEMP-01", 45, t, "HIGH_TEMPERATURE"]]), now=NOW)
        self.assertEqual(data.manual, [{"s": "TEMP-01", "v": 45.0, "t": t, "a": "HIGH_TEMPERATURE"}])
        self.assertEqual(data.get_alerts(1)[0]["value"], 45.0)


@mock.patch.object(config, "DATA_MODE", "demo")
@mock.patch.object(database, "get_connection", no_sqlite)
class DemoApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_all_endpoints_work_without_sqlite(self):
        for url in ENDPOINTS + ["/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_dashboard_page_says_demo_data_and_loads_local_chartjs(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-mode="demo"', html)
        self.assertIn("Serverless demo data", html)
        self.assertIn('src="/static/js/chart.umd.min.js"', html)
        self.assertNotIn("localhost", html)
        self.assertNotIn("127.0.0.1", html)

    def test_summary_is_honest_about_the_data_source(self):
        s = self.client.get("/api/summary").json
        self.assertEqual(s["mode"], "demo")
        self.assertEqual(s["database"]["engine"], "Generated demo data")
        self.assertIsNone(s["database"]["file"])
        self.assertEqual(s["simulator"]["type"], "on-demand")
        self.assertEqual((s["sensors_total"], s["sensors_active"]), (3, 3))
        self.assertGreater(s["readings_stored"], 8000)
        self.assertEqual(s["database"]["alerts"], 14)

    def test_each_demo_button_shows_an_alert(self):
        for sensor_id, value, alert_type in (("TEMP-01", 45, "HIGH_TEMPERATURE"),
                                             ("HUM-01", 22, "LOW_HUMIDITY"),
                                             ("PRES-01", 985, "LOW_PRESSURE")):
            with self.subTest(sensor_id=sensor_id):
                res = self.client.post("/api/readings", json={"sensor_id": sensor_id, "value": value})
                self.assertEqual(res.status_code, 201)
                self.assertEqual(res.json["alert"]["alert_type"], alert_type)
                self.assertIn(data_provider.COOKIE_NAME, res.headers.get("Set-Cookie", ""))

                sensors = {s["sensor_id"]: s for s in self.client.get("/api/sensors").json}
                self.assertTrue(sensors[sensor_id]["in_alert"])                          # red card
                self.assertEqual(sensors[sensor_id]["latest"]["value"], value)
                newest = self.client.get("/api/alerts?limit=1").json[0]                  # alert list
                self.assertEqual((newest["alert_type"], newest["value"]), (alert_type, value))
                points = self.client.get(f"/api/readings?sensor_id={sensor_id}&limit=3").json
                self.assertEqual(points[-1]["value"], value)                             # breach point
                self.assertEqual(points[-1]["id"], res.json["reading"]["id"])

        self.assertEqual(self.client.get("/api/summary").json["database"]["alerts"], 17)

    def test_second_press_while_breached_creates_no_new_alert(self):
        first = self.client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 45})
        second = self.client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 46})
        self.assertIsNotNone(first.json["alert"])
        self.assertIsNone(second.json["alert"])
        self.assertNotEqual(first.json["reading"]["id"], second.json["reading"]["id"])

    def test_demo_readings_belong_to_one_browser(self):
        self.client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 45})
        other_browser = app_module.app.test_client()
        self.assertEqual(other_browser.get("/api/summary").json["database"]["alerts"], 14)

    def test_delete_removes_only_demo_readings(self):
        self.client.post("/api/readings", json={"sensor_id": "HUM-01", "value": 20})
        res = self.client.delete("/api/readings?older_than_hours=0.0001")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.json), {"deleted", "cutoff"})
        self.assertEqual(self.client.get("/api/summary").json["database"]["alerts"], 15)  # still in window

    def test_validation_still_returns_400(self):
        self.assertEqual(self.client.get("/api/readings?sensor_id=XX-99").status_code, 400)
        self.assertEqual(self.client.get("/api/stats?sensor_id=TEMP-01&start=bad").status_code, 400)
        self.assertEqual(self.client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 99}).status_code, 400)
        self.assertEqual(self.client.delete("/api/readings?older_than_hours=-1").status_code, 400)

    def test_tampered_cookie_does_not_break_the_api(self):
        self.client.set_cookie(data_provider.COOKIE_NAME, "%%%garbage%%%")
        for url in ENDPOINTS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class SameResponseShapeTests(TempDatabaseTestCase):
    """Every endpoint returns the same JSON structure in both modes."""

    def keys(self, body):
        item = body[0] if isinstance(body, list) else body
        return {k: sorted(v) if isinstance(v, dict) else None for k, v in item.items()}

    def test_response_shapes_match(self):
        app_module.prepare_database()
        client = app_module.app.test_client()
        local = {url: client.get(url).json for url in ENDPOINTS}
        local_post = client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 45}).json
        with mock.patch.object(config, "DATA_MODE", "demo"), \
                mock.patch.object(database, "get_connection", no_sqlite):
            demo_client = app_module.app.test_client()
            demo = {url: demo_client.get(url).json for url in ENDPOINTS}
            demo_post = demo_client.post("/api/readings", json={"sensor_id": "TEMP-01", "value": 45}).json
        for url in ENDPOINTS:
            with self.subTest(url=url):
                local_keys, demo_keys = self.keys(local[url]), self.keys(demo[url])
                if url == "/api/summary":
                    self.assertLessEqual(set(local_keys["simulator"]), set(demo_keys["simulator"]))
                    local_keys.pop("simulator"), demo_keys.pop("simulator")
                self.assertEqual(local_keys, demo_keys)
        self.assertEqual(self.keys(local_post["reading"]), self.keys(demo_post["reading"]))
        self.assertEqual(self.keys(local_post["alert"]), self.keys(demo_post["alert"]))


if __name__ == "__main__":
    unittest.main()
