"""Flask web server: dashboard page + JSON API.

Locally (python app.py): SQLite database + background simulator thread.
On Vercel: the same routes answered by data_provider.py (generated data, nothing written to disk).
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, render_template, request

import alerts
import config
import data_provider
import database
import simulator

# Static files live in public/ because Vercel serves public/** from its CDN; Flask serves them locally.
app = Flask(__name__, static_folder="public/static", static_url_path="/static")
live_simulator = None   # set by main() in SQLite mode; stays None in tests and on Vercel


def demo_mode():
    return config.DATA_MODE == "demo"


def store():
    """Data source for this request: the database module locally, a DemoData view on Vercel."""
    if not demo_mode():
        return database
    if "demo_data" not in g:
        g.demo_data = data_provider.DemoData.from_cookie(request.cookies.get(data_provider.COOKIE_NAME))
    return g.demo_data


@app.after_request
def save_demo_readings(response):
    demo = g.get("demo_data")
    if demo is not None and demo.changed:
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        demo.write_cookie(response, secure=secure)
    return response


class ApiError(Exception):
    """Invalid request -> HTTP 400 with {"error": message}."""


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify(error=str(err)), 400


# ---------- input validation ----------

def require_sensor_id(value):
    if not isinstance(value, str) or value not in config.SENSORS:
        raise ApiError("sensor_id must be one of: " + ", ".join(config.SENSORS))
    return value


def parse_time(name):
    """Read an ISO 8601 query parameter as a UTC datetime (no offset = UTC). None if absent."""
    raw = request.args.get(name, "")
    if raw == "":
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ApiError(f"{name} must be an ISO 8601 date/time, e.g. 2026-09-27T10:00:00Z")
    if dt.year < 1970:
        raise ApiError(f"{name} must not be before 1970")
    return dt


def time_range():
    """start/end from the query string. Defaults: end = now, start = end - 1 hour."""
    end = parse_time("end") or database.utc_now()
    start = parse_time("start") or end - timedelta(hours=config.DEFAULT_RANGE_HOURS)
    if start > end:
        raise ApiError("start must not be later than end")
    return start, end


def parse_int(name, default, low, high):
    raw = request.args.get(name)
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9) or not low <= int(raw) <= high:
        raise ApiError(f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def parse_positive_hours(name):
    raw = request.args.get(name)
    if raw is None:
        raise ApiError(f"{name} is required")
    try:
        hours = float(raw)
    except ValueError:
        raise ApiError(f"{name} must be a number")
    # The comparison also rejects nan and inf.
    if not 0 < hours <= config.MAX_DELETE_HOURS:
        raise ApiError(f"{name} must be greater than 0 and at most {config.MAX_DELETE_HOURS}")
    return hours


# ---------- routes ----------

@app.get("/")
def index():
    return render_template("index.html", demo=demo_mode())


@app.get("/api/sensors")
def api_sensors():
    sensors = store().get_sensors_with_latest()
    for s in sensors:
        s["in_alert"] = s["latest"] is not None and alerts.is_breached(
            s["sensor_id"], s["latest"]["value"])
        rule = config.ALERT_RULES[s["sensor_id"]]
        s["alert_rule"] = {key: rule[key] for key in ("condition", "threshold", "alert_type", "label")}
    return jsonify(sensors)


@app.get("/api/summary")
def api_summary():
    """Dashboard summary and system status, calculated from the stored (or generated) data."""
    now = database.utc_now()
    local_midnight = simulator.to_local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        db = store().get_summary(alerts_since=local_midnight,
                                 active_since=now - timedelta(seconds=config.ACTIVE_SENSOR_SECONDS))
    except sqlite3.Error as err:
        return jsonify(error=f"database error: {err}", database={"connected": False}), 503
    if demo_mode():
        source = {"engine": "Generated demo data", "version": "(serverless)", "file": None}
        sim = {"type": "on-demand", "running": True, "last_tick": db["last_reading"]}
    else:
        source = {"engine": "SQLite", "version": sqlite3.sqlite_version, "file": os.path.basename(config.DB_PATH)}
        thread = live_simulator
        sim = {"type": "background-thread", "running": thread is not None and thread.is_running(),
               "last_tick": database.dt_to_api_time(thread.last_tick) if thread and thread.last_tick else None}
    return jsonify(
        mode=config.DATA_MODE,
        sensors_total=db["sensors"],
        sensors_active=db["active_sensors"],
        alerts_today=db["alerts_since"],
        readings_stored=db["readings"],
        database={**source, "connected": True, "tables": db["tables"],
                  "sensors": db["sensors"], "readings": db["readings"], "alerts": db["alerts"],
                  "first_reading": db["first_reading"], "last_reading": db["last_reading"]},
        simulator={**sim, "interval_seconds": config.TICK_SECONDS},
        history={"seed_days": config.BACKFILL_HOURS / 24,
                 "seed_step_minutes": config.BACKFILL_STEP_SECONDS / 60},
        server_time=database.dt_to_api_time(now),
    )


@app.get("/api/readings")
def api_readings():
    sensor_id = require_sensor_id(request.args.get("sensor_id"))
    start, end = time_range()
    limit = parse_int("limit", config.READINGS_DEFAULT_LIMIT, 1, config.READINGS_MAX_LIMIT)
    return jsonify(store().get_readings(sensor_id, start, end, limit))


@app.get("/api/stats")
def api_stats():
    sensor_id = require_sensor_id(request.args.get("sensor_id"))
    start, end = time_range()
    stats = store().get_stats(sensor_id, start, end)
    return jsonify(sensor_id=sensor_id, start=database.dt_to_api_time(start),
                   end=database.dt_to_api_time(end), **stats)


@app.get("/api/stats/hourly")
def api_stats_hourly():
    sensor_id = require_sensor_id(request.args.get("sensor_id"))
    start, end = time_range()
    return jsonify(store().get_hourly_stats(sensor_id, start, end))


@app.get("/api/alerts")
def api_alerts():
    limit = parse_int("limit", config.ALERTS_DEFAULT_LIMIT, 1, config.ALERTS_MAX_LIMIT)
    return jsonify(store().get_alerts(limit))


@app.post("/api/readings")
def api_add_reading():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError('body must be a JSON object like {"sensor_id": "TEMP-01", "value": 45}')
    sensor_id = require_sensor_id(data.get("sensor_id"))
    sensor = config.SENSORS[sensor_id]
    value = data.get("value")
    # bool is a subclass of int in Python, so reject it explicitly; the range check rejects nan/inf.
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not sensor["low"] <= value <= sensor["high"]):
        raise ApiError(f"value must be a number from {sensor['low']} to {sensor['high']}")
    reading, alert = store().save_reading(sensor_id, float(value), database.utc_now())
    return jsonify(reading=reading, alert=alert), 201


@app.delete("/api/readings")
def api_delete_readings():
    hours = parse_positive_hours("older_than_hours")
    deleted, cutoff = store().delete_older_than(hours)
    return jsonify(deleted=deleted, cutoff=database.dt_to_api_time(cutoff))


def prepare_database():
    """Create the schema; seed the demo history ONLY if the readings table is empty.

    Returns the number of readings seeded (0 when data already exists).
    """
    database.init_db()
    if database.is_empty():
        return simulator.backfill()
    return 0


def main():
    """Local development server. Vercel imports `app` directly and never calls this."""
    global live_simulator
    if demo_mode():
        print("Demo data mode (as on Vercel): generated data, no SQLite, no background simulator.")
    else:
        seeded = prepare_database()
        if seeded:
            print(f"First run: generated {seeded} historical readings "
                  f"({config.BACKFILL_HOURS // 24} days, one every {config.BACKFILL_STEP_SECONDS // 60} min per sensor).")
        live_simulator = simulator.Simulator()
        live_simulator.start()
    print(f"Dashboard: http://{config.HOST}:{config.PORT}  (Ctrl+C to stop)")
    # No debug reloader: it would start a second process and a second simulator.
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
