# ESP32 Smart Light & Curtain Actuator (Light_Servocurtain)

Firmware for the **ActuatorControl** device. Controls a curtain (servo) and a light (LED), and monitors ambient light with a photoresistor/potentiometer. Connects to the Catalog to discover its `userID` and `roomID` by matching its own device UUID.

## Features
- **Dynamic identity resolution**: at boot, fetches `GET /rooms` and finds the room that contains `DEVICE_ID` in `connected_devices`. Extracts `userID` and `roomID` from that room.
- **Catalog heartbeat**: PATCH `/devices/{DEVICE_ID}` on boot to update topics and timestamp.
- **Curtain control**: servo motor (0° = closed, 90° = open) driven by MQTT command or TimeShift automation.
- **LED control**: on/off driven by MQTT command or TimeShift automation.
- **Light telemetry**: publishes ambient light level (raw ADC 0–4095) via MQTT.
- **Sampling control**: enable/disable telemetry via MQTT command.
- **Wokwi compatible**: simulates photoresistor with a potentiometer.

## Configuration

Edit only these lines in `Light_Servocurtain.ino`:
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
5. Connect to MQTT broker and subscribe to command topics.

## MQTT topics

| Type | Topic | Description |
|------|-------|-------------|
| Pub | `SC/{User}/{Room}/Light` | Ambient light level (SenML, raw ADC) |
| Pub | `SC/{User}/{Room}/LedL` | LED state (SenML boolean) |
| Pub | `SC/{User}/{Room}/servoCurtain` | Curtain state (SenML boolean) |
| Pub | `SC/{User}/{Room}/down` | Device status (JSON) |
| Sub | `SC/{User}/{Room}/servoCurtain/set` | Open (90°) / Close (0°) curtain |
| Sub | `SC/{User}/{Room}/LedL/set` | Toggle LED on/off |
| Sub | `SC/{User}/{Room}/sampling` | Enable/disable light telemetry |

State and command topics are intentionally separated so the actuator can publish its current state without re-consuming it as a command.

## Light threshold (curtain automation)
The TimeShift service reads `threshold_parameters.light_threshold` from the room and publishes servo/LED commands at wakeup based on the ambient light level. The ADC range is 0–4095 (photoresistor: 0 = dark, 4095 = bright). The threshold is set in the Telegram bot when configuring the room:
- Low brightness → `light_threshold = 1200`
- Medium brightness → `light_threshold = 2400`
- High brightness → `light_threshold = 3000`

## Hardware (Wokwi)
- **Potentiometer (photoresistor)**: Pin 34 (analog input)
- **LED**: Pin 4
- **Servo**: Pin 15

## Dependencies
- `WiFi.h`, `HTTPClient.h`, `WiFiClientSecure.h` (ESP32 Core)
- `PubSubClient`
- `ArduinoJson`
- `ESP32Servo`

## Wokwi setup
1. Expose the catalog: `cloudflared tunnel --url http://localhost:9080`
2. Paste the URL into `CATALOG_BASE_URL`.
3. Paste the minted `dev-<hex>` from the Telegram bot into `DEVICE_ID`.
4. Build and start the simulation.
