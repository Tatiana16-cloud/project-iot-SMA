# IoT Sleep Monitoring Stack

Room-centric IoT system for sleep quality monitoring. Each **room** owns its own thresholds, sleep schedule, ThingSpeak channel, and three fixed devices (PPG/HR sensor, DHT sensor, light+curtain actuator).

## Prerequisites
1. Install Docker & Docker Compose
2. Install Python 3 (local dev only)
3. Install Node.js (local dev only)

## Quick start
```bash
docker compose up -d --build
```
Open the dashboard: `http://localhost:1880/ui/`

## Architecture overview

| Component | Role |
|-----------|------|
| `catalog` | REST catalog (CherryPy, port 9080) — rooms, users, devices, ThingSpeak pool |
| `alarm` | MQTT listener → per-room threshold checks → alert publisher |
| `timeshift` | Sleep-phase automator (bedtime/wakeup per room) |
| `bridge_thingspeak` | MQTT → ThingSpeak bridge (per-room channel) |
| `reportGenerator` | REST sleep-report generator (port 8093) |
| `telegram_bot` | User-facing bot: registration, room config, alerts |
| `NodeRed` | Web dashboard UI (port 1880) |
| `devices_code/` | ESP32 firmware (PPG_buzzer, Sensors, Light_Servocurtain) |

## Data model

- **User** — account credentials and dashboard link. Thresholds and schedule live per-room.
- **Room** — owns `timeawake`, `timesleep`, `threshold_parameters` (hr_low/high, temp_low/high, hum_low/high, `light_threshold`), `thingspeak_info`, and `connected_devices` (3 fixed devices per room).
- **Device** — UUID `dev-<hex>`, linked to one room. Templates live in `catalog/device_templates.json`.
- **ThingSpeak pool** — pre-seeded slots in `catalog/catalog.json`; automatically allocated on room creation and freed on room deletion.

## User registration flow

1. User sends `/start` in the Telegram bot.
2. Bot runs the registration wizard: account info → at least one room (name, sleep times, thresholds, brightness level).
3. Catalog mints UUIDs for user, room, and 3 devices; assigns a ThingSpeak pool slot.
4. User pastes the minted `dev-<hex>` IDs into each ESP32 firmware and flashes the devices.

## Device setup (ESP32)

Each firmware file has a single line to edit after registering a room in the bot:
```cpp
#define DEVICE_ID  "dev-xxxxxxxxxx"   // paste the UUID from the catalog
```
The firmware discovers its `userID` and `roomID` automatically at boot by querying `GET /rooms`.

## Cloudflare Tunnel (required for Wokwi)

Wokwi simulations run in the cloud and cannot reach `localhost`. Expose the catalog:
```bash
cloudflared tunnel --url http://localhost:9080
```
Paste the resulting URL into `CATALOG_BASE_URL` in each `.ino` file.
