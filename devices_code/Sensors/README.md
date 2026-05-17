# ESP32 DHT Sensor + Servo Fan (Sensors)

Firmware for the **EnvMonitorDHT** device. Measures temperature and humidity (DHT22) and controls a servo fan. Connects to the Catalog to discover its `userID` and `roomID` by matching its own device UUID.

## Features
- **Dynamic identity resolution**: at boot, fetches `GET /rooms` and finds the room that contains `DEVICE_ID` in `connected_devices`. Extracts `userID` and `roomID` from that room.
- **Catalog heartbeat**: PATCH `/devices/{DEVICE_ID}` on boot to update topics and timestamp.
- **Telemetry**: publishes temperature + humidity in SenML format via MQTT.
- **Servo fan**: subscribes to servo command topic; activates fan on DHT alert or manual command.
- **Alert handling**: wildcard subscription for env alerts triggers servo automatically.
- **Sampling control**: enable/disable telemetry via MQTT command.
- **Wokwi compatible**: simulates DHT22 with Wokwi's DHT22 component.

## Configuration

`DEVICE_ID` is pre-configured with a UUID taken from `catalog/device_pool.json` — no manual editing is needed after registering a room. The remaining line to set is the tunnel URL exposing the catalog:
```cpp
#define DEVICE_ID          "dev-xxxxxxxxxx"   // value from catalog/device_pool.json
#define CATALOG_BASE_URL   "https://your-tunnel.trycloudflare.com"
```

All other settings (Wi-Fi, MQTT broker, pins, flags) are in the same block at the top of the file.

## Boot flow
1. Connect to Wi-Fi.
2. `GET /rooms` → scan `connected_devices` for `DEVICE_ID` → set `userId`, `roomId`.
3. Build MQTT topics using resolved IDs.
4. `PATCH /devices/{DEVICE_ID}` → register topics in catalog.
5. Connect to MQTT broker and subscribe to command/alert topics.

## MQTT topics

| Type | Topic | Description |
|------|-------|-------------|
| Pub | `SC/{User}/{Room}/dht` | Temp & humidity telemetry (SenML) |
| Pub | `SC/{User}/{Room}/ServoDHT` | Servo fan state (SenML) |
| Pub | `SC/{User}/{Room}/down` | Device status (JSON) |
| Sub | `SC/alerts/{User}/{Room}/dht` | Direct env alert → servo ON |
| Sub | `SC/alerts/+/+/dht` | Wildcard env alert (broadcast) |
| Sub | `SC/{User}/{Room}/sampling` | Enable/disable telemetry |

## Hardware (Wokwi)
- **DHT22**: Pin 13
- **Servo fan**: Pin 15

## Dependencies
- `WiFi.h`, `HTTPClient.h`, `WiFiClientSecure.h` (ESP32 Core)
- `PubSubClient`
- `ArduinoJson`
- `DHT sensor library for ESPx`
- `ESP32Servo`

## Wokwi setup
1. Expose the catalog: `cloudflared tunnel --url http://localhost:9080`
2. Paste the URL into `CATALOG_BASE_URL`.
3. Make sure `DEVICE_ID` matches one of the UUIDs in `catalog/device_pool.json`. It does not need to be changed per registration — the catalog allocates and releases it automatically.
4. Build and start the simulation.
