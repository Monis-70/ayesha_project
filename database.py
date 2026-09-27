"""All SQL lives here. Every query uses ? parameters (no string building = no SQL injection)."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import alerts
import config

DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------- time helpers ----------

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db_time(dt):
    """Aware datetime -> 'YYYY-MM-DD HH:MM:SS' in UTC (the format stored in SQLite)."""
    return dt.astimezone(timezone.utc).strftime(DB_TIME_FORMAT)


def to_api_time(db_time):
    """'YYYY-MM-DD HH:MM:SS' -> 'YYYY-MM-DDTHH:MM:SSZ' so browsers read it as UTC."""
    return db_time.replace(" ", "T") + "Z"


def dt_to_api_time(dt):
    return to_api_time(to_db_time(dt))


# ---------- connection ----------

@contextmanager
def get_connection():
    """Open a short-lived connection; commit on success, roll back on error, always close."""
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    """Create tables and index (if missing) and add the three sensors."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with open(config.SCHEMA_PATH, encoding="utf-8") as f:
        schema = f.read()
    with get_connection() as conn:
        conn.executescript(schema)
        conn.executemany(
            "INSERT OR IGNORE INTO sensors (sensor_id, sensor_name, sensor_type, unit) "
            "VALUES (?, ?, ?, ?)",
            [(sid, s["sensor_name"], s["sensor_type"], s["unit"])
             for sid, s in config.SENSORS.items()],
        )


# ---------- INSERT ----------

def save_reading(sensor_id, value, timestamp):
    """INSERT one reading, plus an alert if it just crossed a threshold, in one transaction.

    Returns (reading_dict, alert_dict_or_None).
    """
    ts = to_db_time(timestamp)
    with get_connection() as conn:
        # Take the write lock first so two writers cannot both see the same previous value.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM readings WHERE sensor_id = ? AND timestamp <= ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (sensor_id, ts),
        ).fetchone()
        previous = row["value"] if row else None

        cur = conn.execute(
            "INSERT INTO readings (sensor_id, value, timestamp) VALUES (?, ?, ?)",
            (sensor_id, value, ts),
        )
        reading = {"id": cur.lastrowid, "sensor_id": sensor_id, "value": value,
                   "timestamp": to_api_time(ts)}

        alert = alerts.crossed(sensor_id, previous, value)
        if alert:
            cur = conn.execute(
                "INSERT INTO alerts (sensor_id, alert_type, message, value, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (sensor_id, alert["alert_type"], alert["message"], value, ts),
            )
            alert = {"id": cur.lastrowid, "sensor_id": sensor_id, **alert,
                     "value": value, "timestamp": to_api_time(ts)}
    return reading, alert


def bulk_insert(readings, alert_rows):
    """INSERT many rows at once (used for the 24 h history).

    readings:   [(sensor_id, value, db_timestamp), ...]
    alert_rows: [(sensor_id, alert_type, message, value, db_timestamp), ...]
    """
    with get_connection() as conn:
        conn.executemany(
            "INSERT INTO readings (sensor_id, value, timestamp) VALUES (?, ?, ?)", readings)
        conn.executemany(
            "INSERT INTO alerts (sensor_id, alert_type, message, value, timestamp) "
            "VALUES (?, ?, ?, ?, ?)", alert_rows)


# ---------- SELECT ----------

def is_empty():
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def get_sensors_with_latest():
    """Every sensor JOINed with its most recent reading (None if it has no readings)."""
    sql = """
        SELECT s.sensor_id, s.sensor_name, s.sensor_type, s.unit, r.value, r.timestamp
        FROM sensors s
        LEFT JOIN readings r ON r.id = (
            SELECT id FROM readings
            WHERE sensor_id = s.sensor_id
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        )
        ORDER BY s.rowid
    """
    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
    result = []
    for r in rows:
        latest = None
        if r["timestamp"] is not None:
            latest = {"value": r["value"], "timestamp": to_api_time(r["timestamp"])}
        result.append({"sensor_id": r["sensor_id"], "sensor_name": r["sensor_name"],
                       "sensor_type": r["sensor_type"], "unit": r["unit"], "latest": latest})
    return result


def get_readings(sensor_id, start, end, limit):
    """Readings of one sensor between start and end (inclusive), oldest first.

    The inner query keeps the NEWEST `limit` rows; the outer query re-sorts them oldest first.
    """
    sql = """
        SELECT * FROM (
            SELECT r.id, r.sensor_id, s.sensor_name, s.sensor_type, r.value, s.unit, r.timestamp
            FROM readings r
            JOIN sensors s ON s.sensor_id = r.sensor_id
            WHERE r.sensor_id = ? AND r.timestamp BETWEEN ? AND ?
            ORDER BY r.timestamp DESC, r.id DESC
            LIMIT ?
        )
        ORDER BY timestamp ASC, id ASC
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (sensor_id, to_db_time(start), to_db_time(end), limit)).fetchall()
    return [{**dict(r), "timestamp": to_api_time(r["timestamp"])} for r in rows]


def get_stats(sensor_id, start, end):
    """MIN / MAX / AVG / COUNT of one sensor over a time range."""
    sql = """
        SELECT MIN(value) AS min, MAX(value) AS max, AVG(value) AS avg, COUNT(*) AS count
        FROM readings
        WHERE sensor_id = ? AND timestamp BETWEEN ? AND ?
    """
    with get_connection() as conn:
        r = conn.execute(sql, (sensor_id, to_db_time(start), to_db_time(end))).fetchone()
    return {"min": r["min"], "max": r["max"],
            "avg": round(r["avg"], 2) if r["avg"] is not None else None,
            "count": r["count"]}


def get_hourly_stats(sensor_id, start, end):
    """GROUP BY hour (UTC): one row per hour with min / max / avg / count."""
    sql = """
        SELECT strftime('%Y-%m-%d %H:00:00', timestamp) AS hour,
               MIN(value) AS min, MAX(value) AS max, AVG(value) AS avg, COUNT(*) AS count
        FROM readings
        WHERE sensor_id = ? AND timestamp BETWEEN ? AND ?
        GROUP BY hour
        ORDER BY hour
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (sensor_id, to_db_time(start), to_db_time(end))).fetchall()
    return [{"hour": to_api_time(r["hour"]), "min": r["min"], "max": r["max"],
             "avg": round(r["avg"], 2), "count": r["count"]} for r in rows]


def get_alerts(limit):
    """Most recent alerts first, JOINed with the sensor name."""
    sql = """
        SELECT a.id, a.sensor_id, s.sensor_name, s.unit, a.alert_type, a.message, a.value, a.timestamp
        FROM alerts a
        JOIN sensors s ON s.sensor_id = a.sensor_id
        ORDER BY a.timestamp DESC, a.id DESC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [{**dict(r), "timestamp": to_api_time(r["timestamp"])} for r in rows]


def get_summary(alerts_since, active_since):
    """Counts for the dashboard summary, all calculated from the three existing tables."""
    with get_connection() as conn:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite%'"
        ).fetchone()[0]
        sensors = conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0]
        readings = conn.execute(
            "SELECT COUNT(*) AS n, MIN(timestamp) AS first, MAX(timestamp) AS last FROM readings"
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(DISTINCT sensor_id) FROM readings WHERE timestamp >= ?",
            (to_db_time(active_since),)).fetchone()[0]
        alerts_total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        alerts_since_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE timestamp >= ?", (to_db_time(alerts_since),)).fetchone()[0]
    return {
        "tables": tables, "sensors": sensors, "readings": readings["n"],
        "first_reading": to_api_time(readings["first"]) if readings["first"] else None,
        "last_reading": to_api_time(readings["last"]) if readings["last"] else None,
        "active_sensors": active, "alerts": alerts_total, "alerts_since": alerts_since_count,
    }


# ---------- DELETE ----------

def delete_older_than(hours):
    """DELETE readings older than `hours`. Returns (rows_deleted, cutoff_datetime)."""
    cutoff = utc_now() - timedelta(hours=hours)
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM readings WHERE timestamp < ?", (to_db_time(cutoff),))
    return cur.rowcount, cutoff
