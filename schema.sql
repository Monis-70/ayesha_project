-- Time-series database schema (SQLite).
-- Timestamps are UTC text 'YYYY-MM-DD HH:MM:SS' so text order = time order.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sensors (
    sensor_id   TEXT PRIMARY KEY,
    sensor_name TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    unit        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
    value     REAL NOT NULL,
    timestamp TEXT NOT NULL
);

-- Core time-series index: find one sensor's readings in a time range, already sorted.
CREATE INDEX IF NOT EXISTS idx_readings_sensor_time ON readings(sensor_id, timestamp);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id  TEXT NOT NULL REFERENCES sensors(sensor_id),
    alert_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    value      REAL NOT NULL,
    timestamp  TEXT NOT NULL
);
