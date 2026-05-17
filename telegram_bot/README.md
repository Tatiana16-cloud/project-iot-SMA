# Telegram Bot Service

User-facing bot for registration, per-room configuration, dashboard access, and real-time alerts. The system is room-centric: each room has its own sleep schedule, thresholds, and ThingSpeak channel.

## What it does
- **Registration**: wizard that collects account info and creates at least one room (name, sleep times, all thresholds, brightness level). The catalog allocates 3 device UUIDs from the device pool; the bot displays them to the user as a reference, but no manual editing of the ESP32 firmware is required.
- **Room management**: list rooms, add new rooms, configure or delete individual rooms.
- **Device visibility per room**: users can open a room, see its connected devices and tap each one to read a short friendly explanation.
- **Per-room configuration**: wake/sleep times, temperature/humidity min-max, heart-rate min-max, acceptable brightness level (light threshold for curtain automation).
- **Dashboard link**: ThingSpeak channel URL from the room's `thingspeak_info`.
- **Alerts**: forwards HR and env alerts from MQTT to the user's Telegram chat.
- **Sleep events**: forwards `bedtime` and `wakeup` events from TimeShift.

## Menus

### Main menu
```
🏠 My rooms | ➕ Add new room | 📊 Show dashboard | 🗑 Delete account | 🚪 Exit
```

### Room submenu (after selecting a room)
```
📱 View devices | ⚙️ Configure room | 🗑 Delete this room | ⬅️ Back
```

### Config menu (scoped to selected room)
```
⏰ Wake/Sleep time
🌡 Temp/Humidity min-max
❤️ Heart-rate min-max
💡 Acceptable brightness
⬅️ Back
```

## Registration wizard flow
1. Phone number → password → account name.
2. Room wizard (loops until user says "no more rooms"):
   - Room name → wake time (HH:MM) → sleep time (HH:MM)
   - HR low/high → temp low/high → hum low/high
   - Brightness level (Low / Medium / High → mapped to `light_threshold` scalar)
3. Catalog `POST /rooms` allocates 3 device UUIDs from `device_pool.json`; the bot displays them for reference.

## Brightness / light threshold
The user selects an ambient brightness level at which curtains should open:
- **Low** → `light_threshold = 1200`
- **Medium** → `light_threshold = 2400`
- **High** → `light_threshold = 3000`

This single value (ADC 0–4095 scale) is stored as `threshold_parameters.light_threshold` in the room.

## Authentication & password hashing
- Hash: `sha256(password_salt + password)` compared to `auth.password_hash` in Catalog.
- On failure: prompts again. Password message is deleted (best effort) from the chat.

## Settings (`settings.json`)
- `catalogURL`: Catalog base URL.
- `brokerIP`, `brokerPort`: MQTT broker.
- `serviceInfo.telegram_token`: BotFather token.
- `serviceInfo.serviceID`: logical name (e.g., `TelegramBot`).

## MQTT integration
- Subscribe:
  - `SC/alerts/+/+/#` (HR and env alerts from Alarm)
  - `SC/+/+/bedtime`, `SC/+/+/wakeup` (sleep events from TimeShift)
- Publish:
  - `SC/{User}/{Room}/initTimeshift` after setting wake/sleep times.
- Alert forwarding: only forwards `status="ALERT"` events; re-sends every 120 s while still in ALERT (no spam on OK).

## Catalog interactions (via `common/catalog_client.py`)
- `find_user_by_phone(phone)` — login lookup.
- `get_user(userID)` — load account data.
- `rooms_for_user(userID)` — list rooms for the main menu.
- `get_room(roomID)` — load room data (thresholds, times, ThingSpeak channel).
- `patch_room(roomID, patch)` — update room config.
- `delete_room(roomID)` — cascade delete from bot (confirms first).
- `delete_user(userID)` — cascade delete account (confirms first; ends conversation).

## Runtime
- Entry: `telegram_bot.py`
- Starts Telegram polling + background `AlertsMQTT` thread.
- Dependencies: `python-telegram-bot`, `requests`, `paho-mqtt`.
