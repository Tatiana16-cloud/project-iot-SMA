# Node-RED UI

Node-RED exposes the user-facing web UI at `http://localhost:1880/ui/`. The data directory is `NodeRed/data` (mounted in Docker at `/data`), which holds flows, credentials, and dashboard configuration.

## Purpose
- Login UI backed by the Catalog (userID, phone, password).
- Room picker — selects which room's data to display after login.
- Dashboard — fetches and displays the sleep report for the selected room.

## Architecture & data flow

### 1. Login
1. User enters userID, phone, and password.
2. `POST http://catalog:9080/login` with body `{userID, phone, password}`.
3. The Catalog performs the credential check server-side and returns `{ok: bool, userID?, reason?}` — Node-RED never sees other users' hashes.
4. On success: sets `flow.current_user_id`; switches to room-picker view.
5. On failure: shows error and clears inputs.

### 2. Room picker (post-login)
1. `GET http://catalog:9080/rooms?userID=<current_user_id>` fetches **only** the logged-in user's rooms — no other users' data is transferred to Node-RED.
2. A defensive client-side filter ensures only matching rooms are shown.
3. Renders one entry per room in the dropdown (label = `roomName`).
4. On selection: sets `flow.current_room_id`.
5. A "Change room" button clears `flow.current_room_id` and re-fetches the picker with the same `userID` filter.

### 3. Dashboard
1. `load-dashboard` function node constructs the report URL:
   ```
   http://reports_generator:8093/?user_id={current_user_id}&room_id={current_room_id}&date=YYYY-MM-DD
   ```
2. Both `user_id` and `room_id` must be set; if either is missing, the request is not fired.
3. Charts display BPM, temperature, humidity trends and sleep quality from the report JSON.

## Password hashing
- Performed by the Catalog at the `POST /login` endpoint.
- Node-RED no longer hashes or sees credentials beyond what the user typed — it only forwards them to the Catalog and reads the boolean result.

## Integration with reportGenerator
- Endpoint: `GET /?user_id=<id>&room_id=<id>&date=YYYY-MM-DD`
- Service: `reports_generator:8093`
- Response used for chart data: raw bpm/temp/hum series, `sleep_quality`, `stages_hours`.

## Files of interest
- `NodeRed/data/flows.json` — full flow (login, room picker, dashboard wiring).
- `NodeRed/data/settings.js` — Node-RED settings; data dir mounted at `/data`.

## Running
- Docker Compose: `docker compose up -d --build`, then open `http://localhost:1880/ui/`.
- Persistence: `NodeRed/data` is bind-mounted, so flows/credentials survive container restarts.
