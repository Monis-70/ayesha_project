"""All project settings in one place: sensors, simulation, alerts, limits."""
import os
from datetime import timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "iot.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

HOST = "127.0.0.1"
PORT = 5000


def detect_data_mode(environ):
    """"sqlite" locally (SQLite + background simulator); "demo" on Vercel (generated data).

    Vercel sets VERCEL=1 in its functions (and the AWS Lambda host sets AWS_LAMBDA_FUNCTION_NAME).
    IOT_DATA_MODE overrides the choice (e.g. to preview the Vercel behaviour locally with
    IOT_DATA_MODE=demo).
    """
    on_vercel = environ.get("VERCEL") or environ.get("AWS_LAMBDA_FUNCTION_NAME")
    mode = environ.get("IOT_DATA_MODE") or ("demo" if on_vercel else "sqlite")
    if mode not in ("sqlite", "demo"):
        raise ValueError(f"IOT_DATA_MODE must be 'sqlite' or 'demo', not {mode!r}")
    return mode


DATA_MODE = detect_data_mode(os.environ)

# Time zone of the simulated day/night cycle. None = this computer's zone. Vercel servers run in
# UTC, so set DEMO_UTC_OFFSET_HOURS there (e.g. 5.5 for India) to match the audience's clock.
LOCAL_TZ = (timezone(timedelta(hours=float(os.environ["DEMO_UTC_OFFSET_HOURS"])))
            if os.environ.get("DEMO_UTC_OFFSET_HOURS") else None)

# Simulation timing
TICK_SECONDS = 5              # one live reading per sensor every 5 s
BACKFILL_HOURS = 7 * 24       # demo history generated on first run: 7 days
BACKFILL_STEP_SECONDS = 300   # history resolution: one reading every 5 minutes (2,016 per sensor)
ACTIVE_SENSOR_SECONDS = 30    # a sensor counts as "active" if it reported within this time
DEMO_LIVE_WINDOW_HOURS = 1    # Vercel demo: the last hour has a reading every 5 s (older: every 5 min)

# Simulation behaviour
SPIKE_PROBABILITY = 0.001     # chance per reading of a one-reading glitch spike (rare on purpose)
DRIFT_DECAY = 0.95            # drift is pulled back toward 0 by 5 % each step
DRIFT_LIMIT_FACTOR = 20       # drift is kept within +/- (factor x noise)

# Identity, valid range, noise step and glitch-spike size of each sensor.
SENSORS = {
    "TEMP-01": {
        "sensor_name": "Temperature", "sensor_type": "temperature", "unit": "°C",
        "noise": 0.15, "low": 10.0, "high": 50.0, "spike": 15.0,
    },
    "HUM-01": {
        "sensor_name": "Humidity", "sensor_type": "humidity", "unit": "%",
        "noise": 0.4, "low": 10.0, "high": 95.0, "spike": -30.0,
    },
    "PRES-01": {
        "sensor_name": "Pressure", "sensor_type": "pressure", "unit": "hPa",
        "noise": 0.05, "low": 980.0, "high": 1040.0, "spike": 0.0,   # pressure changes only gradually
    },
}

# Signal models (see simulator.py). day_variation = (min, max) random offset per calendar day.
TEMPERATURE_MODEL = {"base": 28.0, "daily_amplitude": 6.0, "peak_hour": 15,
                     "day_variation": (-2.0, 3.0)}
# Humidity moves opposite to temperature: -1.7 % for every degree above the temperature base.
HUMIDITY_MODEL = {"base": 55.0, "change_per_degree": -1.7, "day_variation": (-4.0, 4.0)}
# Slow weather waves: (amplitude hPa, period in days, phase in radians).
PRESSURE_MODEL = {"base": 1011.0, "weather_waves": [(6.0, 3.7, 1.0), (3.0, 1.6, 2.0)],
                  "daily_amplitude": 0.6, "peak_hour": 10}

# Anomaly periods written into the demo history. The value moves smoothly toward `target`
# and back over `hours`. Day events use local time; "hours_ago" events keep the 24 h chart interesting.
# The Vercel demo replays this 7-day schedule every week; there "hours_ago" events use demo_start_hour.
HISTORY_EVENTS = [
    {"name": "Low pressure system", "sensor_id": "PRES-01", "days_ago": 6, "start_hour": 16, "hours": 10, "target": 986.0},
    {"name": "Server fan stopped", "sensor_id": "TEMP-01", "days_ago": 6, "start_hour": 10, "hours": 0.4, "target": 42.0},
    {"name": "Heatwave afternoon", "sensor_id": "TEMP-01", "days_ago": 5, "start_hour": 12.5, "hours": 3.5, "target": 42.5},
    {"name": "Dry air during heatwave", "sensor_id": "HUM-01", "days_ago": 5, "start_hour": 13, "hours": 3, "target": 24.0},
    {"name": "Storm front", "sensor_id": "PRES-01", "days_ago": 4, "start_hour": 1, "hours": 14, "target": 984.0},
    {"name": "Dehumidifier burst", "sensor_id": "HUM-01", "days_ago": 4, "start_hour": 19, "hours": 0.4, "target": 26.0},
    {"name": "Hot afternoon", "sensor_id": "TEMP-01", "days_ago": 3, "start_hour": 13.5, "hours": 2, "target": 41.5},
    {"name": "Dry spell", "sensor_id": "HUM-01", "days_ago": 2, "start_hour": 10, "hours": 4, "target": 25.0},
    {"name": "Cooling unit fault", "sensor_id": "TEMP-01", "days_ago": 2, "start_hour": 22, "hours": 0.5, "target": 44.0},
    {"name": "Hot afternoon", "sensor_id": "TEMP-01", "days_ago": 1, "start_hour": 12.5, "hours": 1.5, "target": 43.0},
    {"name": "Dry afternoon", "sensor_id": "HUM-01", "days_ago": 1, "start_hour": 13, "hours": 1.5, "target": 27.0},
    {"name": "Pressure dip", "sensor_id": "PRES-01", "hours_ago": 16, "demo_start_hour": 4.5, "hours": 6, "target": 987.0},
    {"name": "Temperature spike", "sensor_id": "TEMP-01", "hours_ago": 9, "demo_start_hour": 11.5, "hours": 0.35, "target": 44.0},
    {"name": "Humidity drop", "sensor_id": "HUM-01", "hours_ago": 6, "demo_start_hour": 14.5, "hours": 2, "target": 26.0},
]

ALERT_RULES = {
    "TEMP-01": {"condition": ">", "threshold": 40.0,
                "alert_type": "HIGH_TEMPERATURE", "label": "High Temperature Alert"},
    "HUM-01": {"condition": "<", "threshold": 30.0,
               "alert_type": "LOW_HUMIDITY", "label": "Low Humidity Alert"},
    "PRES-01": {"condition": "<", "threshold": 990.0,
                "alert_type": "LOW_PRESSURE", "label": "Low Pressure Alert"},
}

# API limits
DEFAULT_RANGE_HOURS = 1
READINGS_DEFAULT_LIMIT = 1000
READINGS_MAX_LIMIT = 20000
ALERTS_DEFAULT_LIMIT = 20
ALERTS_MAX_LIMIT = 100
MAX_DELETE_HOURS = 24 * 365 * 10
