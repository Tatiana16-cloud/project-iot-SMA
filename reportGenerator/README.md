# ReportsGenerator

REST service (CherryPy, port 8093) that generates a sleep report for a given user + room + date using ThingSpeak data and metadata from the Catalog.

## What it does
- Loads per-**room** sleep window (`times.timesleep/timeawake`) and ThingSpeak credentials (`thingspeak_info`) from the Catalog.
- Fetches ThingSpeak feeds for that window, normalizes timestamps to Europe/Rome.
- Computes stats for BPM, temperature, humidity, alarm activations, and derives sleep stages + a sleep quality score.
- Generates short user-friendly tips based on the available temperature, humidity, BPM, and alarm data.
- Sends a heartbeat/upsert to the Catalog at startup and on each request.

## Request format

```
GET /?user_id=<userID>&room_id=<roomID>&date=YYYY-MM-DD
```
- `user_id` and `room_id` are both required.
- `date` is optional (defaults to today in Europe/Rome).
- Returns 404 if `room.userID != user_id`.

## Configuration (`reportGenerator/settings.json`)
- `catalogURL`: Catalog base URL.
- `reportsURL`: Service URL (used for Catalog registration).
- `thingspeakURL`: Base URL for ThingSpeak reads.
- `serviceInfo`: `serviceID`, `REST_endpoint`, `MQTT_sub`/`MQTT_pub` (registered only).
- `fields`: ThingSpeak field mapping:
  - `TS_BPM_FIELD` (default `field3`)
  - `TS_TEMP_FIELD` (default `field1`)
  - `TS_HUM_FIELD` (default `field2`)
  - `TS_ALARM_FIELD` (default `field8`)
- Padding for TS queries: env `TS_PAD_MIN` (default 5 minutes). Data is clipped to the exact sleep window.

## Request flow
1. Heartbeat + upsert to Catalog (best effort).
2. Determine reference date (today in Europe/Rome unless `date` provided).
3. Fetch **room** from Catalog (`GET /rooms/{room_id}`), validate `room.userID == user_id`, extract `times.timesleep/timeawake`.
4. Get ThingSpeak credentials from `room.thingspeak_info` (`channel_id`, `write_api_key`, `read_api_key`).
5. Fetch ThingSpeak feeds with padding; clip to exact sleep window.
6. Compute metrics:
   - `basic_stats`: mean/min/max for bpm, temp, hum.
   - `count_led_activations`: max value of alarm counter in the window.
   - Raw arrays for bpm/temp/hum (for plotting).
   - `infer_sleep_stages_from_bpm`: heuristic stages (deep/light/rem).
   - `sleep_quality`: weighted score (0–100).
7. Return JSON: `{status, user_id, room_id, room_name, window, stats, stages_hours, sleep_quality, tips}`.

## Sleep quality score
- Temp ideal ~19 °C: linear decay to 0 at ±6 °C.
- Hum ideal 40–60 %RH: 100 inside range, linear decay to ±30.
- BPM variability: span = max–min, capped at 40; score = `100*(1–span/40)`.
- Alerts penalty: `rate_per_hour = alarm_count / duration_hours`; penalty = `min(rate_per_hour * 2, 40)`.
- Final: `clip(0.35*temp + 0.25*hum + 0.40*bpm – penalty, 0, 100)`.

## Dependencies
- `reportGenerator/reporting_service.py` (CherryPy service)
- `common/catalog_client.py`
- Libraries: pandas, numpy, dateutil, requests.

## Running
- Docker: built via `docker-compose.yml`, port 8093.
- Local dev: `pip install -r reportGenerator/requirements.txt` and `python reporting_service.py`.
