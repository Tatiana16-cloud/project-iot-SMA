# ESP32 PPG Sensor & Alarm (PPG_buzzer)

Firmware for the **HealthMonitorPPG** device. Monitors heart rate (BPM) via a PPG sensor (ADC) and drives a buzzer + LED for wakeup and HR alarms. Connects to the Catalog to discover its `userID` and `roomID` by matching its own device UUID.

## Features
- **Dynamic identity resolution**: at boot, fetches `GET /rooms` and finds the room that contains `DEVICE_ID` in `connected_devices`. Extracts `userID` and `roomID` from that room.
- **Catalog heartbeat**: PATCH `/devices/{DEVICE_ID}` on boot to update topics and timestamp.
- **HR monitoring**: reads ADC, detects peaks, calculates BPM, publishes SenML via MQTT.
- **Alert handling**: subscribes to HR alert topic; triggers buzzer + LED when `status=ALERT`.
- **Wakeup alarm**: drives buzzer + LED for a configurable number of seconds on `wakeup` command.
- **Sampling control**: enable/disable BPM publishing via MQTT command.
- **Wokwi compatible**: simulates PPG with a potentiometer.

## Configuration

Edit only these lines in `PPG_buzzer.ino`:
```cpp
// Paste the dev-<hex> UUID shown by the Telegram bot after registering the room
#define DEVICE_ID          "dev-xxxxxxxxxx"
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
| Pub | `SC/{User}/{Room}/hr` | BPM telemetry (SenML) |
| Pub | `SC/{User}/{Room}/down` | Device status (JSON) |
| Sub | `SC/alerts/{User}/{Room}/hr` | HR alert → buzzer + LED |
| Sub | `SC/{User}/{Room}/wakeup` | Wakeup alarm for N seconds |
| Sub | `SC/{User}/{Room}/sampling` | Enable/disable BPM publishing |

## Hardware (Wokwi)
- **PPG / ADC**: Pin 34 (potentiometer simulating PPG signal)
- **LED**: Pin 4
- **Buzzer**: Pin 5

## Dependencies
- `WiFi.h`, `HTTPClient.h`, `WiFiClientSecure.h` (ESP32 Core)
- `PubSubClient`
- `ArduinoJson`

## Wokwi notes
> Start this simulation **before** the others — Wokwi allows only one simulation with network access at a time.

1. Expose the catalog: `cloudflared tunnel --url http://localhost:9080`
2. Paste the URL into `CATALOG_BASE_URL`.
3. Paste the minted `dev-<hex>` from the Telegram bot into `DEVICE_ID`.
4. Build and start the simulation.
