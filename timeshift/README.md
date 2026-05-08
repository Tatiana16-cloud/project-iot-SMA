# TimeShift Service

Microservice that automates sleep routine transitions (night/day) per user/room. It reads sleep times and thresholds from the Catalog **per room**, listens to light measurements, and publishes MQTT commands/events for bedtime/wakeup, sampling, curtain (servo), and LED.

## What it does
- Reads per-**room** sleep times (`times.timesleep/timeawake`) from Catalog on demand.
- Listens to light sensor topics and caches the last light value per `(user, room)` pair.
- Decides transition to NIGHT/DAY based on current time vs. room sleep window.
- On transitions:
  - **Night (bedtime)**: publish `bedtime`, enable sampling, close curtain (servo 0), LED OFF.
  - **Day (wakeup)**: publish `wakeup`, decide LED ON/OFF from light reading vs. room `light_threshold`, open curtain (servo 90), disable sampling.
- On first observation of a `(user, room)` pair, seeds the baseline phase without triggering any action (avoids spurious alarms when a new room is added).
- Registers its MQTT pub/sub in the Catalog `serviceList` at startup.

## Light threshold logic (LED at wakeup)
- Reads `threshold_parameters.light_threshold` from the room (single scalar, ADC range 0–4095).
- If the last cached `raw` light value < `light_threshold` → LED ON at wakeup; else LED OFF.
- If no cached light for the pair, defaults to LED ON.
- Fallback value if the room has no `light_threshold`: configurable via `settings.json` (`light_threshold_fallback`).

## Settings (`timeshift/settings.json`)
- `catalogURL`: Catalog base URL.
- `brokerIP`, `brokerPort`: MQTT broker.
- `timezone`: e.g., `Europe/Rome`.
- `light_threshold_fallback`: default light threshold if room has none (e.g., `2048`).
- `serviceInfo.serviceID`: e.g., `TimeShift`.
- `serviceInfo.MQTT_sub.Light`: topic template `SC/{User}/{Room}/Light`.
- `serviceInfo.MQTT_pub`: `sampling`, `bedtime`, `wakeup`, `down`, `servoV`, `LedL` topic templates. The actuator commands should point to `/set` topics.

## MQTT flow
- Subscribe:
  - `SC/<User>/<Room>/Light` (SenML with `raw` field)
  - `SC/<User>/<Room>/initTimeshift` (seeds pairs and current phase)
- Publish on transitions:
  - `bedtime` — `{"ts": epoch}`
  - `wakeup` — `{"seconds": wake_alarm_seconds}`
  - `sampling` — `{"enable": bool}` (retained)
  - `servoCurtain/set` — `"0"` or `"90"` (retained command)
  - `LedL/set` — SenML boolean (retained command)

`TimeShift` is an automation producer: it publishes commands, while the actuator publishes the resulting state on `servoCurtain` and `LedL` without the `/set` suffix.

## Pair discovery (user/room)
- Pairs are discovered from incoming Light and initTimeshift topics.
- On first observation of a new `(user, room)` pair, the current phase is seeded without firing any action. Subsequent ticks detect phase changes and trigger transitions.

## Catalog interactions (via `common/catalog_client.py`)
- `room_times(room_id)` → reads `times.timesleep/timeawake` from the room.
- `room_thresholds(room_id)` → reads `threshold_parameters` (for `light_threshold`).
- `upsert_service(...)` → registers the service at startup.

## Running
- Docker: service `timeshift` in `docker-compose.yml`.
- Local dev: `pip install -r timeshift/requirements.txt` and `python timeshift.py`.
