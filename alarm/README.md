# Alarm Service

Microservice that listens to MQTT sensor data (HR, temperature, humidity), applies per-**room** thresholds from the Catalog, and publishes alerts on MQTT for downstream consumers (e.g., Telegram bot).

## What it does
- Subscribes to sensor topics:
  - `SC/+/+/hr`
  - `SC/+/+/dht`
- Parses SenML payloads and extracts:
  - HR (`bpm`) from `/hr`
  - Temp/Humidity (`temp`, `hum`) from `/dht`
- Fetches thresholds **per room** from Catalog (`GET /rooms/{roomID}` → `threshold_parameters`):
  - HR: `hr_low`, `hr_high`
  - Temp: `temp_low`, `temp_high`
  - Humidity: `hum_low`, `hum_high`
- Publishes alerts:
  - HR alerts: `SC/alerts/{User}/{Room}/hr`
  - Env alerts (temp/hum): `SC/alerts/{User}/{Room}/dht`
- If thresholds for a room are missing, the alarm skips publishing for that room.

## Settings (`alarm/settings.json`)
- `catalogURL`: Catalog endpoint (e.g., `http://catalog:9080`)
- `brokerIP`, `brokerPort`: MQTT broker (default `broker.hivemq.com`, `1883`)
- `serviceInfo`:
  - `serviceID`: service name (e.g., `AlarmControl`)
  - `MQTT_sub`: sensor subscriptions (uses wildcards)
  - `MQTT_pub_alert_env`: env alerts topic template (supports `{User}`, `{Room}`)
  - `MQTT_pub_alert_hr`: HR alerts topic template (supports `{User}`, `{Room}`)

## MQTT flow
- Inbound:
  - `SC/<User>/<Room>/hr`  (SenML with bpm)
  - `SC/<User>/<Room>/dht` (SenML with temp/hum)
- Outbound:
  - `SC/alerts/<User>/<Room>/hr`
  - `SC/alerts/<User>/<Room>/dht`

## Catalog data required
- `GET /rooms/{roomID}` must return an object with `threshold_parameters` containing:
  - `hr_low`, `hr_high`
  - `temp_low`, `temp_high`
  - `hum_low`, `hum_high`

> Thresholds are fetched from the **room** (not the user). Each room can have independent alarm limits.

## Runtime
- Entry: `alarm.py`
- Uses shared utilities: `common/MyMQTT.py`, `common/senml.py`, `common/catalog_client.py`
- Dependencies: `paho-mqtt`, `requests`

## Running
- Docker: service `alarm` in `docker-compose.yml` (uses `alarm/Dockerfile`).
- Local dev: `pip install -r alarm/requirements.txt` and `python alarm.py`.
