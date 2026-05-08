# Bridge ThingSpeak

Service that consumes MQTT events from `SC/<User>/<Room>/...`, processes sensor data and alerts, and publishes to ThingSpeak using per-**room** credentials stored in the Catalog.

## Purpose
- Send sensor readings to ThingSpeak (averaged per time window).
- Send actuator values and alert counts in the same periodic publish.
- Respect ThingSpeak rate-limit (1 update every ≥15 s per channel) by consolidating a single payload per `minPeriodSec`.

## Configuration (`settings.json`)
- `catalogURL`: Catalog URL (used to fetch ThingSpeak API keys/channel per room).
- `ThingspeakWriteURL`: ThingSpeak write endpoint.
- `brokerIP` / `brokerPort`: MQTT broker.
- `minPeriodSec`: sending window; keep ≥15 s.
- `service.serviceID`, `service.MQTT_sub`: MQTT subscriptions (use `{User}/{Room}` placeholders).
- `fields`: logical-name → `fieldN` mapping. Example: `"alerts": "field8"`, `"temp": "field1"`.

## Data flow
1. **MQTT subscribe**: dynamic topics `SC/<User>/<Room>/Light|dht|hr|servoCurtain|LedL|wakeup|...`, plus `SC/alerts/<User>/<Room>/...` and `initTimeshift`.
2. **Processing**:
   - SenML sensors: accumulate sum/count per field (`temp`, `hum`, `bpm`, `light`) for averaging.
   - Actuators (`servoCurtain`, `LedL`, `ServoDHT`): keep last reported state value.
   - Alerts: count events with `status="ALERT"` per user/room.
   - `initTimeshift`: reset alert counter for that user/room.
3. **Periodic send** (every `minPeriodSec` or wakeup trigger):
   - Averages for sensor fields; last known value for actuators and alert count.
   - Single payload published to ThingSpeak. `entry_id=0` → rate-limit rejection (logged as WARNING).

## ThingSpeak credentials
- Credentials (`write_api_key`, `channel_id`) are read from **room** data:
  `GET /rooms/{roomID}` → `thingspeak_info`.
- Each room has its own ThingSpeak channel allocated from the pool at room creation.

## Alert behavior
- Only counts alerts whose payload has `status="ALERT"` in `events`.
- Sent as the `alerts` field in the periodic ThingSpeak payload.
- `wakeup` schedules the next send after the remaining time to satisfy `minPeriodSec`.

## Useful logs
- DEBUG: SenML parsing/skips.
- INFO: publishes (`TS periodic ... entry_id=...`) and alert counts.
- WARNING: ThingSpeak rate-limit rejection or missing room credentials.

## Limitations
- Keep `minPeriodSec` ≥ 15 s.
- Ensure each room has valid ThingSpeak credentials in the Catalog pool.

## Running
- Docker: service `bridge_thingspeak` in `docker-compose.yml`.
- Local dev: `pip install -r bridge_thingspeak/requirements.txt` and `python bridge.py`.
