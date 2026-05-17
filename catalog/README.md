# Catalog Service

Lightweight CherryPy REST API that stores the system catalog: users, rooms, devices, services, the ThingSpeak channel pool, and the device UUID pool. Data is persisted in `catalog.json` and `device_pool.json` (Docker volumes); device templates are read from `device_templates.json`.

## Run (Docker)
Service `catalog` in `docker-compose.yml`, port `9080`.
```
./catalog/catalog.json:/data/catalog.json:rw
./catalog/device_pool.json:/data/device_pool.json:rw
```
Relevant env vars:
- `CATALOG_PATH` (default: `catalog.json`)
- `CATALOG_TEMPLATES_PATH` (default: `device_templates.json` next to the catalog file)
- `CATALOG_DEVICE_POOL_PATH` (default: `device_pool.json` next to the catalog file)
- `CATALOG_WRITE_TOKEN` — if set, all write operations require `X-Write-Token: <token>` header.
- `CATALOG_READ_ONLY` — `true` disables all writes.
- `CATALOG_CACHE_TTL` — internal cache TTL in seconds (default 2.0).

## Catalog structure

Required root fields:
```json
{
  "catalog_url", "projectOwners", "project_name", "broker",
  "servicesList", "thingspeak_pool", "usersList", "roomsList", "devicesList"
}
```

`thingspeak_pool.slots` is a list of pre-seeded ThingSpeak channels:
```json
{ "channel_id": "...", "write_api_key": "...", "read_api_key": "...",
  "status": "available", "assigned_to_room": null }
```

The device UUID pool (`device_pool.json`) is a separate file with one list per device role. Each entry tracks status and the room it is currently assigned to:
```json
{
  "pools": {
    "HealthMonitorPPG": [ { "deviceID": "...", "status": "available", "assigned_to_room": null, "updated_at": null }, ... ],
    "EnvMonitorDHT":    [ ... ],
    "ActuatorControl":  [ ... ]
  }
}
```

## Endpoints (base: `http://catalog:9080`)

### Health
- `GET /health` → `{"status": "ok"}`

### Full catalog
- `GET /catalog` → full JSON.
- `PUT|POST /catalog` → replace entire catalog (token required if configured).

### CRUD collections: `/services`, `/devices`, `/users`
All follow the same pattern:
- `GET /<col>` → full list.
- `GET /<col>/{id}` → single item.
- `POST /<col>` → create (ID required in body). 409 if duplicate.
- `PUT /<col>/{id}` → replace.
- `PATCH /<col>/{id}` → partial update (deep merge).
- `DELETE /<col>/{id}` → delete.

> `POST /devices` returns **405** — devices are only created via `POST /rooms`, which takes UUIDs from the device pool.

### Rooms — `/rooms`
- `GET /rooms` → full list.
- `GET /rooms/{roomID}` → single room.
- `POST /rooms` — **atomic room creation**:
  - Body: `{userID, roomName, times, threshold_parameters}` (do NOT include IDs — the server assigns them).
  - Server mints `roomID` (`rm-<hex>`), allocates a ThingSpeak pool slot, takes one available device UUID per role from `device_pool.json`, writes `roomsList` and `devicesList`, and updates both pools.
  - Returns the full room object including `connected_devices` with the assigned device IDs.
  - If any step fails → full rollback.
  - Returns 409 if the ThingSpeak pool or any role of the device pool has no available entries.
- `PATCH /rooms/{roomID}` → partial update (`roomName`, `times`, `threshold_parameters`).
- `DELETE /rooms/{roomID}` — **cascade delete**: removes the 3 linked devices, releases the ThingSpeak slot, releases the 3 device UUIDs back to the device pool, deletes the room.

### Users — `/users`
- Standard CRUD as above.
- `DELETE /users/{userID}` — **cascade delete**: for each room owned by the user, runs the room cascade (delete devices, release ThingSpeak slot and device UUIDs), then deletes the user.

### Device templates
- `GET /device_templates` → returns the contents of `device_templates.json` (3 templates: HealthMonitorPPG, EnvMonitorDHT, ActuatorControl).

### Device pool
- `GET /device_pool` → full pool status (every UUID with its `status`, `assigned_to_room`, and `updated_at`).

### ThingSpeak pool
- `GET /thingspeak_pool` → full pool status (all slots with their `status` and `assigned_to_room`).

## Device templates (`device_templates.json`)

Static file with 3 device templates. Tokens `{User}` and `{Room}` are substituted with real IDs when a room is created. Topics follow the scheme `SC/{User}/{Room}/...`.

## Room data model

```json
{
  "roomID": "rm-<hex>",
  "userID": "usr-<hex>",
  "roomName": "My Room",
  "times": { "timeawake": "07:00", "timesleep": "23:00" },
  "threshold_parameters": {
    "hr_low": 45, "hr_high": 100,
    "temp_low": 16, "temp_high": 26,
    "hum_low": 30, "hum_high": 70,
    "light_threshold": 2400
  },
  "thingspeak_info": { "channel_id": "...", "write_api_key": "...", "read_api_key": "..." },
  "connected_devices": [
    { "deviceID": "...", "role": "HealthMonitorPPG" },
    { "deviceID": "...", "role": "EnvMonitorDHT" },
    { "deviceID": "...", "role": "ActuatorControl" }
  ]
}
```

## Common error codes
| Code | Meaning |
|------|---------|
| 400 | Invalid payload or missing fields |
| 401 | Wrong or missing write token |
| 403 | Read-only mode |
| 404 | Resource not found |
| 405 | Method not allowed (e.g. POST /devices) |
| 409 | Duplicate on create, or pool exhausted (ThingSpeak slots / device UUIDs) |

## Local dev (without Docker)
```bash
pip install cherrypy
python catalog.py   # uses CATALOG_PATH env or catalog.json in cwd
```
