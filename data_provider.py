"""Serverless demo data provider (used on Vercel instead of SQLite + the background simulator).

Uses the same sensor model as simulator.py, but every value is calculated from its timestamp,
so nothing has to be stored or kept running between requests:

- history: one reading every 5 minutes for the last 7 days;
- "live": one reading every 5 seconds for the last hour, so each poll shows a new reading;
- anomaly events: config.HISTORY_EVENTS replayed as a fixed weekly schedule;
- demo-button readings: kept in a small cookie in the visitor's browser, never on the server's disk.

The same timestamp always produces the same value.
"""
import base64
import binascii
import functools
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import alerts
import config
import database
import simulator

HISTORY_STEP = config.BACKFILL_STEP_SECONDS            # 300 s
LIVE_STEP = config.TICK_SECONDS                        # 5 s
WINDOW_SECONDS = config.BACKFILL_HOURS * 3600          # 7 days
LIVE_WINDOW_SECONDS = config.DEMO_LIVE_WINDOW_HOURS * 3600
ID_EPOCH = 1767225600                                  # 2026-01-01T00:00:00Z, keeps ids short
SENSOR_IDS = list(config.SENSORS)
COOKIE_NAME = "iot_demo_readings"
MAX_MANUAL_READINGS = 20


# ---------- deterministic values ----------

def _local_midnight(day_number):
    d = date.fromordinal(day_number)
    if config.LOCAL_TZ is not None:
        return datetime(d.year, d.month, d.day, tzinfo=config.LOCAL_TZ)
    return datetime(d.year, d.month, d.day).astimezone()


@functools.lru_cache(maxsize=16)
def events_for_week(week):
    """config.HISTORY_EVENTS placed in 7-day cycle number `week` (each cycle starts on a Sunday).

    "days_ago" N events fall on cycle day 7 - N; the "hours_ago" events fall on day 0.
    """
    events = []
    for e in config.HISTORY_EVENTS:
        if "days_ago" in e:
            day, hour = (7 - e["days_ago"]) % 7, e["start_hour"]
        else:
            day, hour = 0, e["demo_start_hour"]
        start = _local_midnight(week * 7 + day) + timedelta(hours=hour)
        events.append({**e, "start": start, "end": start + timedelta(hours=e["hours"])})
    return events


def _drift(sensor_index, epoch, noise):
    # A few slow sine waves with unrelated periods look like a random walk but need no stored state.
    p = sensor_index * 1.7
    tau = 2 * math.pi
    return noise * (1.2 * math.sin(tau * epoch / 2400 + p) + 0.8 * math.sin(tau * epoch / 5700 + 2 * p)
                    + 0.5 * math.sin(tau * epoch / 13100 + 3 * p) + 0.3 * math.sin(tau * epoch / 170 + p))


@functools.lru_cache(maxsize=60000)
def value_at(sensor_id, epoch):
    """Demo reading of one sensor at a whole-second Unix time: signal + event + drift + noise."""
    sensor = config.SENSORS[sensor_id]
    index = SENSOR_IDS.index(sensor_id)
    when = datetime.fromtimestamp(epoch, timezone.utc)
    value = simulator.SIGNALS[sensor_id](when)
    week = simulator.to_local(when).toordinal() // 7
    for event in events_for_week(week - 1) + events_for_week(week):
        if event["sensor_id"] == sensor_id:
            value += simulator.event_weight(event, when) * (event["target"] - value)
    noise = sensor["noise"]
    value += _drift(index, epoch, noise) + random.Random(epoch * 8 + index).uniform(-noise, noise) / 3
    return round(max(sensor["low"], min(sensor["high"], value)), 2)


def reading_id(sensor_id, epoch, manual=False):
    """Unique id from time + sensor (manual readings get odd ids, so they sort after generated ones)."""
    return (epoch - ID_EPOCH) * 8 + SENSOR_IDS.index(sensor_id) * 2 + int(manual)


def _api_time(epoch):
    return database.dt_to_api_time(datetime.fromtimestamp(epoch, timezone.utc))


def _ceil_to(value, step):
    return -(-value // step) * step


# ---------- per-request view of the data ----------

class DemoData:
    """Same query methods as database.py, answered from generated data (read-only on the server)."""

    def __init__(self, manual=(), now=None):
        self.now = int((now or database.utc_now()).timestamp())
        self.latest = self.now - self.now % LIVE_STEP
        self.first = (self.latest - WINDOW_SECONDS) // HISTORY_STEP * HISTORY_STEP + HISTORY_STEP
        self.live_start = self.latest - LIVE_WINDOW_SECONDS
        # Demo-button readings: {"s": sensor_id, "v": value, "t": epoch, "a": alert_type or None}
        self.manual = [m for m in manual if self.first <= m["t"] <= self.now]
        self.changed = False

    # ----- cookie storage of demo-button readings -----

    @classmethod
    def from_cookie(cls, raw, now=None):
        """Rebuild from the browser cookie. Anything malformed or out of range is ignored."""
        items = []
        try:
            decoded = json.loads(base64.urlsafe_b64decode((raw or "").encode()) or b"[]")
        except (ValueError, binascii.Error):
            decoded = []
        for item in decoded if isinstance(decoded, list) else []:
            if not (isinstance(item, list) and len(item) == 4):
                continue
            sid, value, t, alert_type = item
            sensor = config.SENSORS.get(sid) if isinstance(sid, str) else None
            if (sensor is None or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not sensor["low"] <= value <= sensor["high"]
                    or isinstance(t, bool) or not isinstance(t, int)):
                continue
            if alert_type is not None and (alert_type != config.ALERT_RULES[sid]["alert_type"]
                                           or not alerts.is_breached(sid, value)):
                continue
            items.append({"s": sid, "v": float(value), "t": t, "a": alert_type})
        return cls(sorted(items, key=lambda m: m["t"])[-MAX_MANUAL_READINGS:], now)

    def write_cookie(self, response, secure):
        payload = json.dumps([[m["s"], m["v"], m["t"], m["a"]] for m in self.manual], separators=(",", ":"))
        response.set_cookie(COOKIE_NAME, base64.urlsafe_b64encode(payload.encode()).decode(),
                            max_age=WINDOW_SECONDS, path="/", httponly=True, samesite="Lax", secure=secure)

    # ----- helpers -----

    def _epochs(self, lo, hi):
        """Times (Unix seconds) of generated readings in [lo, hi], oldest first."""
        lo, hi = max(lo, self.first), min(hi, self.latest)
        if lo > hi:
            return []
        history = range(_ceil_to(lo, HISTORY_STEP), min(hi + 1, self.live_start), HISTORY_STEP)
        live = range(_ceil_to(max(lo, self.live_start), LIVE_STEP), hi + 1, LIVE_STEP)
        return [*history, *live]

    def _series(self, sensor_id, lo, hi, limit=None):
        """[(epoch, id, value)] of one sensor in [lo, hi], oldest first; newest `limit` rows if given."""
        rows = [(t, reading_id(sensor_id, t), None) for t in self._epochs(lo, hi)]
        rows += [(m["t"], reading_id(sensor_id, m["t"], manual=True), m["v"])
                 for m in self.manual if m["s"] == sensor_id and lo <= m["t"] <= hi]
        rows.sort(key=lambda r: (r[0], r[1]))
        if limit is not None:
            rows = rows[-limit:]
        return [(t, rid, value_at(sensor_id, t) if v is None else v) for t, rid, v in rows]

    def _range_series(self, sensor_id, start, end, limit=None):
        return self._series(sensor_id, math.ceil(start.timestamp()), math.floor(end.timestamp()), limit)

    def _alert_row(self, sensor_id, epoch, value, alert, manual):
        s = config.SENSORS[sensor_id]
        return {"id": reading_id(sensor_id, epoch, manual), "sensor_id": sensor_id,
                "sensor_name": s["sensor_name"], "unit": s["unit"], "alert_type": alert["alert_type"],
                "message": alert["message"], "value": value, "timestamp": _api_time(epoch), "_t": epoch}

    @functools.cached_property
    def _all_alerts(self):
        """Newest first. Generated alerts use the fixed 5-minute grid, so they never move."""
        found = []
        for sid in SENSOR_IDS:
            previous = value_at(sid, self.first - HISTORY_STEP)
            for t in range(self.first, self.latest + 1, HISTORY_STEP):
                value = value_at(sid, t)
                alert = alerts.crossed(sid, previous, value)
                if alert:
                    found.append(self._alert_row(sid, t, value, alert, manual=False))
                previous = value
        for m in self.manual:
            if m["a"]:
                found.append(self._alert_row(m["s"], m["t"], m["v"], alerts.crossed(m["s"], None, m["v"]), True))
        found.sort(key=lambda a: (a["_t"], a["id"]), reverse=True)
        return found

    # ----- the same queries as database.py -----

    def get_sensors_with_latest(self):
        result = []
        for sid, s in config.SENSORS.items():
            t, _, value = self._series(sid, self.first, self.now, limit=1)[0]
            result.append({"sensor_id": sid, "sensor_name": s["sensor_name"], "sensor_type": s["sensor_type"],
                           "unit": s["unit"], "latest": {"value": value, "timestamp": _api_time(t)}})
        return result

    def get_readings(self, sensor_id, start, end, limit):
        s = config.SENSORS[sensor_id]
        return [{"id": rid, "sensor_id": sensor_id, "sensor_name": s["sensor_name"],
                 "sensor_type": s["sensor_type"], "value": value, "unit": s["unit"], "timestamp": _api_time(t)}
                for t, rid, value in self._range_series(sensor_id, start, end, limit)]

    def get_stats(self, sensor_id, start, end):
        values = [v for _, _, v in self._range_series(sensor_id, start, end)]
        if not values:
            return {"min": None, "max": None, "avg": None, "count": 0}
        return {"min": min(values), "max": max(values), "avg": round(sum(values) / len(values), 2),
                "count": len(values)}

    def get_hourly_stats(self, sensor_id, start, end):
        hours = {}
        for t, _, value in self._range_series(sensor_id, start, end):
            hours.setdefault(t // 3600, []).append(value)
        return [{"hour": _api_time(h * 3600), "min": min(v), "max": max(v),
                 "avg": round(sum(v) / len(v), 2), "count": len(v)} for h, v in sorted(hours.items())]

    def get_alerts(self, limit):
        return [{k: v for k, v in a.items() if k != "_t"} for a in self._all_alerts[:limit]]

    def get_summary(self, alerts_since, active_since):
        since = alerts_since.timestamp()
        last = max([self.latest] + [m["t"] for m in self.manual])
        return {
            "tables": None, "sensors": len(SENSOR_IDS),
            "readings": len(self._epochs(self.first, self.latest)) * len(SENSOR_IDS) + len(self.manual),
            "first_reading": _api_time(self.first), "last_reading": _api_time(last),
            "active_sensors": len(SENSOR_IDS) if self.latest >= active_since.timestamp() else 0,
            "alerts": len(self._all_alerts),
            "alerts_since": sum(1 for a in self._all_alerts if a["_t"] >= since),
        }

    # ----- demo-button writes (kept in the browser cookie only) -----

    def save_reading(self, sensor_id, value, timestamp):
        t = int(timestamp.timestamp())
        # Keep one manual reading per sensor per second so ids stay unique.
        t = max([t] + [m["t"] + 1 for m in self.manual if m["s"] == sensor_id])
        previous = self._series(sensor_id, self.first, t, limit=1)
        alert = alerts.crossed(sensor_id, previous[0][2] if previous else None, value)
        self.manual = (self.manual + [{"s": sensor_id, "v": value, "t": t,
                                       "a": alert["alert_type"] if alert else None}])[-MAX_MANUAL_READINGS:]
        self.changed = True
        self.__dict__.pop("_all_alerts", None)
        reading = {"id": reading_id(sensor_id, t, manual=True), "sensor_id": sensor_id, "value": value,
                   "timestamp": _api_time(t)}
        if alert:
            alert = {k: v for k, v in self._alert_row(sensor_id, t, value, alert, True).items()
                     if k in ("id", "sensor_id", "alert_type", "message", "value", "timestamp")}
        return reading, alert

    def delete_older_than(self, hours):
        """Generated history is read-only; only this browser's demo readings can be deleted."""
        cutoff = datetime.fromtimestamp(self.now, timezone.utc) - timedelta(hours=hours)
        kept = [m for m in self.manual if m["t"] >= cutoff.timestamp()]
        deleted = len(self.manual) - len(kept)
        if deleted:
            self.manual, self.changed = kept, True
            self.__dict__.pop("_all_alerts", None)
        return deleted, cutoff
