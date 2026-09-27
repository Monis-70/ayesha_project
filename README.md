# IoT Time-Series Monitoring System

*College mini-project: Time-Series Database for IoT Monitoring*

This project simulates three IoT sensors: temperature, humidity and pressure. Their readings are stored as time-series data in a local **SQLite** database. A **Flask** REST API serves the data to a single-page dashboard, which shows live values, history charts (Chart.js), statistics and alerts.

No hardware, no database server and no internet connection are needed (after `pip install`).

The same code runs in two modes:
- **locally** with SQLite (the full database project);
- **on Vercel** as a serverless demo (see [Deploying to Vercel](#deploying-to-vercel)).

## Two architectures: local and Vercel

> **The local version uses SQLite for persistent storage. The Vercel deployment uses deterministic serverless-generated demo data because Vercel functions should not depend on a persistent local filesystem or long-running background process.**

Nothing is written to SQLite on Vercel. There is no `data/iot.db` there and no background thread.

### Local architecture (`python app.py`)

```
IoT Sensors (simulated)
   ↓  simulator.py – background thread, one reading per sensor every 5 s
Time-Series Data
   ↓  database.py – INSERT into SQLite (data/iot.db: sensors, readings, alerts)
Storage: SQLite
   ↓  app.py – Flask REST API runs parameterised SELECT / aggregates / DELETE
Analytics: MIN, MAX, AVG, COUNT, GROUP BY hour
   ↓  JSON
Dashboard (HTML/CSS/JS + Chart.js, polls every 5 s)
   ↓
Alerts (alerts.py – raised when a reading crosses a threshold)
```

### Vercel demo architecture (serverless)

```
IoT Sensors (simulated)
   ↓  data_provider.py – each value is calculated from its timestamp
      (same signal model and anomaly events as simulator.py)
Time-Series Data (generated on request: 7 days every 5 min + the last hour every 5 s)
   ↓
Storage: none needed; the same timestamp always gives the same value
   ↓  app.py – the same Flask routes and the same JSON responses
Analytics: MIN, MAX, AVG, COUNT and hourly groups computed in Python
   ↓  JSON
Dashboard (identical page; static files served by Vercel's CDN from public/)
   ↓
Alerts (same alerts.py crossing rule applied to the generated readings)
```

**Live updates without a thread:** every 5 s the browser polls `GET /api/readings`. The server calculates the reading for the current 5-second timestamp, so each poll shows a new value.

**Demo buttons on Vercel:** the reading still goes through `POST /api/readings` and the same validation and crossing check. Instead of a database row, it is kept in a small **httpOnly cookie in the visitor's browser**, at most 20 readings. Only that browser sees it: the red card, the ACTIVE alert and the red chart point. Other visitors do not.

**How the mode is chosen** (`config.detect_data_mode`):

| Situation | Mode |
|---|---|
| `python app.py` on your laptop | `sqlite` (SQLite + background simulator) |
| Running on Vercel (`VERCEL=1` is set automatically) | `demo` (generated data) |
| `IOT_DATA_MODE=demo` or `IOT_DATA_MODE=sqlite` | overrides the automatic choice |

To preview the Vercel behaviour on your laptop:
```powershell
$env:IOT_DATA_MODE = "demo"; python app.py
```
Run `Remove-Item Env:IOT_DATA_MODE` afterwards to go back to SQLite.

## Requirements

- A Windows laptop.
- Python 3.12 or newer. Python 3.12 added the built-in `python -m sqlite3` shell used in the demo.
- Flask. It is the only dependency; see `requirements.txt`.

## Setup and start

Run these commands in a terminal opened in this project folder:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**. Stop the app with `Ctrl+C`.

## Demo dataset (created automatically)

On the first start, when the `readings` table is empty, the app seeds a realistic 7-day history. After that, the live simulator adds one reading per sensor every 5 seconds.

| Item | Value |
|---|---|
| Sensors | 3 (TEMP-01, HUM-01, PRES-01) |
| Historical period | 7 days |
| Historical sampling | one reading every 5 minutes |
| Historical readings | 6,048 (2,016 per sensor) |
| Live sampling | one reading per sensor every 5 seconds |
| Historical alerts | 14 (6 high temperature, 5 low humidity, 3 low pressure) |

- **Startup is safe to repeat.** If the database already contains readings, **nothing is seeded again**. Restarting the app simply continues with live readings.
- **Numbers after a reset:** exactly 14 alerts and 6,048 historical readings. Live readings and alerts are added on top.
- **Where the settings live:** all dataset parameters are in [config.py](config.py) (`BACKFILL_*`, `*_MODEL`, `HISTORY_EVENTS`).

### How the data is made realistic

Every reading is built the same way:

```
reading = smooth signal for that moment
        + anomaly event (history only)
        + slow random drift + small noise
```

The signal depends only on the clock. That is why the 7-day history and the live readings join up smoothly.

| Sensor | Behaviour |
|---|---|
| **Temperature** | Daily cycle: warmest around 15:00, coolest around 03:00, mostly 21–37 °C. Each day is slightly warmer or cooler than the one before (a random offset per calendar day, blended smoothly). |
| **Humidity** | Moves opposite to temperature, falling 1.7 % for every degree warmer. It has its own day-to-day variation and is mostly 38–68 %. |
| **Pressure** | Slow weather waves (3.7-day and 1.6-day periods) plus a tiny daily tide. It is mostly 1000–1020 hPa, with no sudden jumps. |

**Designed anomaly events** (in `HISTORY_EVENTS`): in each one, the value moves smoothly to a peak and back. Each event produces exactly one alert, because alerts are raised only when a sensor crosses from normal to abnormal.

| When | Event | Sensor | Peak |
|---|---|---|---|
| 6 days ago, 10:00 (24 min) | Server fan stopped | TEMP-01 | 42 °C |
| 6 days ago, 16:00 (10 h) | Low pressure system | PRES-01 | 986 hPa |
| 5 days ago, 12:30 (3.5 h) | Heatwave afternoon | TEMP-01 | 42.5 °C |
| 5 days ago, 13:00 (3 h) | Dry air during heatwave | HUM-01 | 24 % |
| 4 days ago, 01:00 (14 h) | Storm front | PRES-01 | 984 hPa |
| 4 days ago, 19:00 (24 min) | Dehumidifier burst | HUM-01 | 26 % |
| 3 days ago, 13:30 (2 h) | Hot afternoon | TEMP-01 | 41.5 °C |
| 2 days ago, 10:00 (4 h) | Dry spell | HUM-01 | 25 % |
| 2 days ago, 22:00 (30 min) | Cooling unit fault | TEMP-01 | 44 °C |
| 1 day ago, 12:30 (1.5 h) | Hot afternoon | TEMP-01 | 43 °C |
| 1 day ago, 13:00 (1.5 h) | Dry afternoon | HUM-01 | 27 % |
| 16 h before start (6 h) | Pressure dip | PRES-01 | 987 hPa |
| 9 h before start (21 min) | Temperature spike | TEMP-01 | 44 °C |
| 6 h before start (2 h) | Humidity drop | HUM-01 | 26 % |

The last three events are timed relative to when the app starts, so the **24-hour** chart always contains an alert event.

Readings stay normal the rest of the time: less than 6 % of historical readings are beyond a threshold. The live simulator adds a rare one-reading glitch spike: 0.1 % chance per reading, for temperature and humidity only.

## Reset for a fresh demo

1. Stop the application (`Ctrl+C` in its terminal).
2. Delete the database file, including the SQLite WAL files if present:
   ```powershell
   Remove-Item data\iot.db*
   ```
3. Start the application again:
   ```powershell
   python app.py
   ```
4. It prints `First run: generated 6048 historical readings (7 days, one every 5 min per sensor).` and the dashboard is immediately populated.

The app never deletes the database by itself.

## Run the tests

```powershell
python -m unittest -v
```

The tests cover:
- alert rules and crossing logic;
- the signal models;
- the 7-day seed: counts, time span, ranges, anomalies, one alert per event, no duplicate seeding on restart;
- the live simulator after seeding;
- all SQL queries;
- every API endpoint, including validation that returns 400;
- the Vercel demo mode (`tests/test_data_provider.py`):
  - mode detection and deterministic values;
  - enough points for every range preset;
  - realistic ranges and the 14 historical alerts;
  - statistics and hourly groups;
  - demo buttons through the API, and tampered cookies;
  - identical JSON shapes in both modes;
  - a guard that fails if demo mode opens SQLite.

Each test uses its own temporary database, so the tests never touch `data/iot.db`.

## Deploying to Vercel

No database, paid service or environment variable is required. Vercel's zero-configuration Flask support detects `app.py`, which exposes `app = Flask(__name__)`, and `requirements.txt` (Flask). No `vercel.json` is needed.

- **Static files:** files in `public/` (the CSS, JS and the local copy of Chart.js) are served by Vercel's CDN at the same `/static/...` URLs the page uses.
- **`app.run()`:** only called under `if __name__ == "__main__":`, so Vercel never starts the development server.
- **`.vercelignore`:** keeps `.venv`, `data/` (the local SQLite file) and `tests/` out of the upload.
- **`.gitignore`:** keeps `.venv` and the database file out of GitHub.

### Option A – GitHub + Vercel dashboard

1. **Push the project to GitHub.** Create an empty repository on github.com, then run:
   ```powershell
   git init
   git add .
   git commit -m "IoT time-series monitoring system"
   git branch -M main
   git remote add origin https://github.com/<your-user>/<your-repo>.git
   git push -u origin main
   ```
2. **Open Vercel:** sign in at https://vercel.com (you can sign in with GitHub).
3. **Import the repository:** choose **Add New… → Project**, then **Import** next to your repository.
4. **Confirm Python/Flask detection:** the Framework Preset should show **Flask**. Leave the Root Directory as `./` and the build settings empty.
5. **Deploy:** click **Deploy** and wait for the build to finish.
6. **Open the deployed URL**, for example `https://<project>.vercel.app`.
7. **Test the dashboard:**
   - The status strip shows **DEMO DATA (no DB)** and **ON-DEMAND**.
   - The cards update every 5 s.
   - Try 15 min, 1 h, 6 h, 24 h, 7 days and a custom range, plus Pause/Resume.
   - Click the three demo buttons: each card turns red and an ACTIVE alert appears.
8. **Test the API endpoints** (replace the URL with yours):
   ```powershell
   $u = "https://<project>.vercel.app"
   Invoke-RestMethod "$u/api/summary"
   Invoke-RestMethod "$u/api/sensors"
   Invoke-RestMethod "$u/api/readings?sensor_id=TEMP-01&limit=5"
   Invoke-RestMethod "$u/api/stats?sensor_id=HUM-01"
   Invoke-RestMethod "$u/api/stats/hourly?sensor_id=PRES-01"
   Invoke-RestMethod "$u/api/alerts?limit=5"
   Invoke-RestMethod -Method Post -Uri "$u/api/readings" -ContentType 'application/json' -Body '{"sensor_id":"TEMP-01","value":45}'
   ```
   `/api/summary` must report `"mode": "demo"`.

Every later `git push` to `main` redeploys automatically.

### Option B – Vercel CLI

```powershell
npm install -g vercel
vercel login
vercel          # first run: link/create the project and make a preview deployment
vercel --prod   # production deployment
```

To test through the Vercel local runtime first, run `vercel dev` (after `python -m pip install -r requirements.txt`). Note that `vercel dev` itself may not set `VERCEL=1`. Run `$env:IOT_DATA_MODE = "demo"; vercel dev` to be sure you get the demo mode.

### Environment variables

None are required.

| Variable | Needed? | Purpose |
|---|---|---|
| `VERCEL` | set by Vercel automatically | Switches the app to demo mode |
| `IOT_DATA_MODE` | optional | `demo` or `sqlite`, to force a mode. Setting `demo` in Vercel → Project → Settings → Environment Variables is a safe extra. |
| `DEMO_UTC_OFFSET_HOURS` | optional, **recommended on Vercel** | Vercel servers run in UTC, so the simulated afternoon peak would happen at 15:00 UTC. Set this to your audience's offset (for example `5.5` for India) so day and night match their clock. Locally the laptop's time zone is used. |

There are no secrets, API keys or database URLs.

### Limitations of the Vercel demo version

- **No persistent storage.**
  - Demo-button readings live in the visitor's browser cookie (the latest 20). They disappear when cookies are cleared.
  - `DELETE /api/readings` only removes that browser's demo readings; the generated history cannot be deleted.
- **The data is generated, not recorded.** The 14 anomaly events repeat as a fixed weekly schedule, and the dashboard's "last 7 days" always shows one full cycle.
- **Live data resolution:** the last hour has a reading every 5 s; older data has one every 5 minutes. A random "glitch spike" like the local simulator's never occurs; alerts come from the designed events and the demo buttons.
- **SQLite-specific parts are local-only:** the database panel shows "not used", and the viva steps with `python -m sqlite3` and `EXPLAIN QUERY PLAN` must be shown on the laptop.
- **Cold starts:** the first request after a period of no traffic can take about a second.

## Dashboard features

- **Header:** project title, online indicator, live clock, and a pause/resume refresh button.
- **System status strip:** system ONLINE, database CONNECTED, simulator RUNNING, last reading age, historical data period (7 days, 5-minute samples), live update interval, and last dashboard refresh. All of these come from `GET /api/summary`, which checks the real database and the simulator thread.
- **Summary tiles:** total sensors, active sensors, alerts today and readings stored. All are counted from the existing tables.
- **Sensor cards:** name, ID, current value and unit, NORMAL/ALERT status, last update time, alert threshold and last alert time. A card turns red while its latest reading is beyond the threshold. Clicking a card charts that sensor.
- **Main chart:**
  - Sensor selector and range buttons: 15 min, 1 h, 6 h, 24 h (the default) and 7 days, plus a custom From/To range.
  - A smooth line with a dashed red **threshold line** and a shaded alert zone.
  - Red points mark readings beyond the threshold.
  - The time axis is aligned to local time, and the tooltip shows the exact value.
- **Statistics:** minimum, maximum, average and count for the selected range.
- **Recent alerts:** alert type, sensor, value, threshold, time ("2 minutes ago") and ACTIVE/RESOLVED status.
- **Demo mode:** three buttons that send a real reading through `POST /api/readings`.
- **Database panel:** SQLite version, file, number of tables and sensors, number of time-series records and alerts, and the oldest and newest reading.
- **Tables:** hourly statistics (`GROUP BY`) and the 20 newest raw readings in the selected range.
- **Architecture diagram:** Sensors → Simulator → SQLite → Flask API → Dashboard.
- **Loading, empty and error states.** The layout also adapts to narrow screens.

## College demonstration (5–7 minutes)

Before the viva, reset the demo dataset (see above) and keep a second PowerShell window open in the project folder.

**STEP 1: Start the application.**
```powershell
python app.py
```
Point out the message saying 6,048 historical readings were generated.

**STEP 2: Open the dashboard** at http://127.0.0.1:5000.

**STEP 3: Explain the idea.**
> "These are simulated IoT sensors. A background thread in `simulator.py` behaves like three devices and sends a reading every 5 seconds. Readings are stored as time-series rows in SQLite."

**STEP 4: Show the three sensor cards:** temperature, humidity and pressure. Point out the value, unit, NORMAL status, "Updated … seconds ago" and the alert threshold. Show the status strip too: ONLINE, CONNECTED, RUNNING.

**STEP 5: Explain the time-series data** using the 24-hour chart (the default view).
> "Each row is (sensor_id, value, timestamp). The chart shows the daily cycle: cooler at night and warmer in the afternoon. The dashed red line is the alert threshold."

Then click **7 days** to show a week of history with its designed events: heatwave, dry spells and storm fronts.

**STEP 6: Change the sensor and time range.**
1. Select Humidity. It moves opposite to temperature.
2. Select Pressure. It changes slowly, with storm dips below 990 hPa.
3. Try 15 min, 1 h and 6 h, and a custom From/To range.

**STEP 7: Show the statistics:** minimum, maximum, average and count. These come from one SQL aggregate query:
```sql
SELECT MIN(value), MAX(value), AVG(value), COUNT(*)
FROM readings WHERE sensor_id = ? AND timestamp BETWEEN ? AND ?;
```
Also show the hourly statistics table, which uses `GROUP BY` hour.

**STEP 8: Trigger a demo temperature alert.** In Demo mode, click **Trigger High Temperature**. The same request from PowerShell is:
```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/readings -ContentType 'application/json' -Body '{"sensor_id":"TEMP-01","value":45}'
```

**STEP 9: Show the alert on the dashboard.**
- The temperature card turns red (ALERT).
- A HIGH TEMPERATURE alert marked ACTIVE appears at the top of Recent alerts.
- A red point appears on the chart, and "Alerts today" goes up.

The next simulated reading (within 5 s) is normal again, so the card returns to NORMAL and the alert becomes RESOLVED. Press **Pause refresh** first if you want to keep the red state on screen.

Explain the crossing rule: pressing the button again while TEMP-01 is still beyond 40 °C creates **no new alert**. The message under the buttons says so.

**STEP 10: Show the alert stored in SQLite.** In the second PowerShell window:
```powershell
python -m sqlite3 data\iot.db
```
```sql
SELECT id, sensor_id, alert_type, value, timestamp FROM alerts ORDER BY id DESC LIMIT 3;
```

**STEP 11: Show the SQL used to retrieve historical data.** It is `get_readings()` in [database.py](database.py). You can run a simplified version:
```sql
SELECT r.timestamp, s.sensor_name, r.value, s.unit
  FROM readings r JOIN sensors s ON s.sensor_id = r.sensor_id
  WHERE r.sensor_id = 'TEMP-01'
    AND r.timestamp BETWEEN datetime('now', '-1 hour') AND datetime('now')
  ORDER BY r.timestamp DESC LIMIT 5;
```
Point out the `WHERE`, `BETWEEN`, `JOIN` and `ORDER BY`. The app always uses `?` parameters, which prevents SQL injection.

**STEP 12: Show the index with EXPLAIN QUERY PLAN.**
```sql
EXPLAIN QUERY PLAN SELECT * FROM readings
  WHERE sensor_id = 'TEMP-01'
    AND timestamp BETWEEN datetime('now', '-1 hour') AND datetime('now');
.quit
```
The output shows `SEARCH readings USING INDEX idx_readings_sensor_time (sensor_id=? AND timestamp>? AND timestamp<?)`. SQLite jumps straight to one sensor's time range instead of scanning every row.

**STEP 13: Explain why SQLite was chosen.**
- It is built into Python (`sqlite3`), with no server to install or configure.
- The whole database is one file (`data/iot.db`), easy to reset, copy and inspect.
- It supports full SQL (JOIN, GROUP BY, indexes, transactions), which is enough to demonstrate every database concept here.
- It is fast enough for thousands of readings per sensor on a laptop. WAL mode lets the dashboard read while the simulator writes.

**STEP 14: Explain how this could scale to a real IoT system.**
- **Devices:** real sensors send readings over MQTT or HTTP to an ingestion service instead of the simulator thread.
- **Database:** a dedicated time-series database such as TimescaleDB or InfluxDB would add compression, automatic retention policies and continuous aggregates (pre-computed hourly and daily summaries).
- **Scale:** readings could be partitioned by time or device, written in batches, and old raw data downsampled.
- **Operations:** authentication, alert notifications (email/SMS) and a production web server behind HTTPS.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Flask routes, input validation, startup (`prepare_database()`), chooses SQLite or demo data |
| `config.py` | Sensors, alert thresholds, signal models, demo events, limits, mode detection |
| `database.py` | Every SQL query (INSERT, SELECT, aggregates, GROUP BY, JOIN, DELETE) – local mode |
| `data_provider.py` | Vercel demo mode: the same queries answered from generated data (no disk writes) |
| `simulator.py` | Signal models, 7-day seed, live background thread (local mode) |
| `alerts.py` | Threshold rules and the "alert only when crossing" check |
| `schema.sql` | Tables and the time-series index |
| `templates/index.html` | Dashboard page |
| `public/static/js/dashboard.js` | Fetches the API every 5 s; draws cards, chart, tables and demo mode |
| `public/static/js/chart.umd.min.js` | Chart.js 4.5.0, a local copy so the demo works offline |
| `public/static/css/style.css` | Dashboard styling |
| `tests/` | `unittest` tests |
| `.gitignore`, `.vercelignore` | Keep `.venv`, the SQLite file and caches out of GitHub and out of Vercel uploads |

Static files are in `public/` because Vercel serves `public/**` from its CDN. Locally Flask serves the same folder at `/static/...`.

## Database schema

| Table | Columns |
|---|---|
| `sensors` | `sensor_id` (PK), `sensor_name`, `sensor_type`, `unit` |
| `readings` | `id` (PK), `sensor_id` (FK), `value`, `timestamp` |
| `alerts` | `id` (PK), `sensor_id` (FK), `alert_type`, `message`, `value`, `timestamp` |

- **Index:** `idx_readings_sensor_time ON readings(sensor_id, timestamp)`.
- **Timestamps:** stored as UTC text in the form `YYYY-MM-DD HH:MM:SS`, so sorting the text gives time order. The API returns them as `YYYY-MM-DDTHH:MM:SSZ`, and the browser shows local time.

## API

| Method | URL | Description |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/sensors` | Sensors with their latest reading, alert state and alert rule |
| GET | `/api/readings?sensor_id=&start=&end=&limit=` | History, oldest first. Returns the newest `limit` rows (default 1000, maximum 20000). |
| GET | `/api/stats?sensor_id=&start=&end=` | `min`, `max`, `avg`, `count` |
| GET | `/api/stats/hourly?sensor_id=&start=&end=` | The same values per hour (`GROUP BY`) |
| GET | `/api/alerts?limit=20` | Recent alerts, newest first (limit 1–100) |
| GET | `/api/summary` | Dashboard summary and system status, counted from the existing tables |
| POST | `/api/readings` | Insert a reading, body `{"sensor_id": "TEMP-01", "value": 45}` |
| DELETE | `/api/readings?older_than_hours=24` | Delete old readings and return the number deleted |

- `start` and `end` are ISO 8601 (for example `2026-09-27T10:00:00Z`). A time without an offset is treated as UTC. If they are missing, the range is the last 1 hour.
- An invalid request returns **HTTP 400** with `{"error": "..."}`.

**Deleting old records** (optional extra for the viva):
```powershell
Invoke-RestMethod -Method Delete -Uri 'http://127.0.0.1:5000/api/readings?older_than_hours=72'
```

## Database concepts demonstrated

| Concept | Where |
|---|---|
| INSERT | `database.save_reading`, `database.bulk_insert` (the 7-day seed, one transaction) |
| SELECT / WHERE / BETWEEN / ORDER BY | `database.get_readings` |
| MIN / MAX / AVG / COUNT | `database.get_stats`, `database.get_summary` |
| GROUP BY | `database.get_hourly_stats` (`strftime` hour groups) |
| JOIN | Readings and alerts joined with `sensors` |
| INDEX | `idx_readings_sensor_time`, checked with `EXPLAIN QUERY PLAN` |
| DELETE | `database.delete_older_than` (removing old data) |
| Primary and foreign keys | `schema.sql` plus `PRAGMA foreign_keys = ON` |
| Transactions | A reading and its alert are saved together (`BEGIN IMMEDIATE`) |
| Parameterised queries | Every query in `database.py` |

## Known limitations (by design)

- Hourly groups are UTC hours. In time zones with a half-hour offset, such as India (UTC+5:30), they show as `07:30 PM–08:30 PM`.
- A sensor card is red only while that sensor's **latest** reading is beyond its threshold. The alert itself stays in the alert list.
- The chart loads at most 20,000 points per request. That covers the 7-day seed plus more than a day of live readings. If the app runs for several days, the 7-day chart shows the newest 20,000 points.
- The app is single-user and localhost-only, with no login. This is a demo, not a production system.
- For the Vercel version's limitations, see [Limitations of the Vercel demo version](#limitations-of-the-vercel-demo-version).
