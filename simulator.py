"""Simulated IoT sensors: realistic telemetry, a 7-day demo history, and a live background thread.

Every reading = smooth "true" signal for that moment (+ anomaly event) + slow drift + small noise.
The signal depends only on the clock, so the seeded history and the live readings join up smoothly.
"""
import functools
import logging
import math
import random
import threading
from datetime import timedelta

import alerts
import config
import database

log = logging.getLogger(__name__)


# ---------- smooth signals (no noise) ----------

def to_local(when):
    """`when` in the day/night time zone (config.LOCAL_TZ; None = this computer's zone)."""
    return when.astimezone(config.LOCAL_TZ)


def _local_hours(when):
    local = to_local(when)
    return local, local.hour + local.minute / 60 + local.second / 3600


@functools.lru_cache(maxsize=None)
def _value_for_day(key, day_number, low, high):
    # Seeded by the date, so a given day always gets the same value (history and live agree).
    return random.Random(f"{key}-{day_number}").uniform(low, high)


def day_variation(key, when, low, high):
    """Day-to-day variation: one random offset per calendar day, blended smoothly between days."""
    local, hours = _local_hours(when)
    days = local.toordinal() + hours / 24 - 0.5          # a whole number at local noon
    day = math.floor(days)
    today = _value_for_day(key, day, low, high)
    tomorrow = _value_for_day(key, day + 1, low, high)
    blend = (1 - math.cos(math.pi * (days - day))) / 2   # cosine blend: no jump at midnight
    return today + (tomorrow - today) * blend


def daily_wave(when, peak_hour):
    """Sine wave from -1 to +1 over 24 h, highest at peak_hour (local time)."""
    _, hours = _local_hours(when)
    return math.sin(2 * math.pi * (hours - peak_hour + 6) / 24)


def temperature_signal(when):
    m = config.TEMPERATURE_MODEL
    return (m["base"] + m["daily_amplitude"] * daily_wave(when, m["peak_hour"])
            + day_variation("TEMP", when, *m["day_variation"]))


def humidity_signal(when):
    m = config.HUMIDITY_MODEL
    warmer_by = temperature_signal(when) - config.TEMPERATURE_MODEL["base"]
    return m["base"] + m["change_per_degree"] * warmer_by + day_variation("HUM", when, *m["day_variation"])


def pressure_signal(when):
    m = config.PRESSURE_MODEL
    days = when.timestamp() / 86400
    value = m["base"] + m["daily_amplitude"] * daily_wave(when, m["peak_hour"])
    for amplitude, period_days, phase in m["weather_waves"]:
        value += amplitude * math.sin(2 * math.pi * days / period_days + phase)
    return value


SIGNALS = {"TEMP-01": temperature_signal, "HUM-01": humidity_signal, "PRES-01": pressure_signal}


# ---------- anomaly events (demo history only) ----------

def history_events(now):
    """config.HISTORY_EVENTS converted to absolute start/end times relative to `now`."""
    midnight = to_local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    events = []
    for e in config.HISTORY_EVENTS:
        if "days_ago" in e:
            start = midnight - timedelta(days=e["days_ago"]) + timedelta(hours=e["start_hour"])
        else:
            start = now - timedelta(hours=e["hours_ago"])
        events.append({**e, "start": start, "end": start + timedelta(hours=e["hours"])})
    return events


def event_weight(event, when):
    """0 outside the event, rising smoothly to 1 in the middle of it (a sin² hill)."""
    if not event["start"] <= when <= event["end"]:
        return 0.0
    fraction = (when - event["start"]) / (event["end"] - event["start"])
    return math.sin(math.pi * fraction) ** 2


# ---------- one reading ----------

def generate_value(sensor_id, when, drift, rng, events=(), spikes=True):
    """Return (value, new_drift) for one sensor at time `when`.

    value = signal (pulled toward any active event's target) + slow drift + small noise
            (+ rare glitch spike if `spikes`), clamped to the sensor range.
    """
    sensor = config.SENSORS[sensor_id]
    noise = sensor["noise"]

    # Mean-reverting random walk: small random step, pulled back toward 0, bounded.
    drift = drift * config.DRIFT_DECAY + rng.uniform(-noise, noise)
    limit = config.DRIFT_LIMIT_FACTOR * noise
    drift = max(-limit, min(limit, drift))

    value = SIGNALS[sensor_id](when)
    for event in events:
        if event["sensor_id"] == sensor_id:
            value += event_weight(event, when) * (event["target"] - value)
    value += drift + rng.uniform(-noise, noise) / 3

    if spikes and rng.random() < config.SPIKE_PROBABILITY:
        value += sensor["spike"]

    value = max(sensor["low"], min(sensor["high"], value))
    return round(value, 2), drift


def backfill(now=None, rng=None):
    """Seed the demo history: BACKFILL_HOURS of readings, one every BACKFILL_STEP_SECONDS,
    including the designed anomaly events (no random glitches, so the history is predictable).
    Alerts use the same crossing check as live readings.

    Returns the number of readings inserted.
    """
    rng = rng or random.Random()
    now = now or database.utc_now()
    step = config.BACKFILL_STEP_SECONDS
    end = now - timedelta(seconds=now.timestamp() % step)   # align to the 5-minute grid
    events = history_events(now)
    drift = {sid: 0.0 for sid in config.SENSORS}
    previous = {sid: None for sid in config.SENSORS}
    readings, alert_rows = [], []

    steps = config.BACKFILL_HOURS * 3600 // step
    for i in range(steps - 1, -1, -1):
        when = end - timedelta(seconds=i * step)
        ts = database.to_db_time(when)
        for sid in config.SENSORS:
            value, drift[sid] = generate_value(sid, when, drift[sid], rng, events, spikes=False)
            readings.append((sid, value, ts))
            alert = alerts.crossed(sid, previous[sid], value)
            if alert:
                alert_rows.append((sid, alert["alert_type"], alert["message"], value, ts))
            previous[sid] = value

    database.bulk_insert(readings, alert_rows)
    return len(readings)


class Simulator:
    """Background thread that stores one reading per sensor every `interval` seconds."""

    def __init__(self, interval=config.TICK_SECONDS, rng=None):
        self.interval = interval
        self.rng = rng or random.Random()
        self.drift = {sid: 0.0 for sid in config.SENSORS}
        self.last_tick = None
        self._stop = threading.Event()
        self._thread = None

    def tick(self):
        now = database.utc_now()
        for sid in config.SENSORS:
            value, self.drift[sid] = generate_value(sid, now, self.drift[sid], self.rng)
            database.save_reading(sid, value, now)
        self.last_tick = now

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # One failed write must not stop the simulation.
                log.exception("Simulator tick failed")
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="sensor-simulator", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)
