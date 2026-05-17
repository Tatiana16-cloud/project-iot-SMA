
import json
import time
import uuid
import copy
from datetime import datetime
import threading
import logging
import cherrypy
import os

# -------- Logging --------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("catalog")

# -------- Config --------
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.json")
TEMPLATES_PATH = os.getenv(
    "CATALOG_TEMPLATES_PATH",
    os.path.join(os.path.dirname(CATALOG_PATH) or ".", "device_templates.json"),
)
DEVICE_POOL_PATH = os.getenv(
    "CATALOG_DEVICE_POOL_PATH",
    os.path.join(os.path.dirname(CATALOG_PATH) or ".", "device_pool.json"),
)
WRITE_TOKEN = os.getenv("CATALOG_WRITE_TOKEN")  # optional
READ_ONLY = os.getenv("CATALOG_READ_ONLY", "false").lower() == "true"


def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def uuid_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CatalogService:
    def __init__(self, json_path: str, templates_path: str, device_pool_path: str):
        self.json_path = json_path
        self.templates_path = templates_path
        self.device_pool_path = device_pool_path
        self._lock = threading.RLock()
        self._catalog = None  # lazy load
        self._last_load = 0.0
        self._templates = None
        self._templates_mtime = 0.0
        self._device_pool = None  # lazy load (mutable, persisted)
        self._cache_ttl = float(os.getenv("CATALOG_CACHE_TTL", "2.0"))  # seconds

    # ------------- Storage helpers -------------
    def _load_from_disk(self) -> dict:
        if not os.path.exists(self.json_path):
            raise FileNotFoundError(f"No existe {self.json_path}")
        with open(self.json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_to_disk(self, payload: dict):
        ensure_parent(self.json_path)
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _get_catalog(self) -> dict:
        with self._lock:
            expired = (time.time() - self._last_load) > self._cache_ttl
            if self._catalog is None or expired:
                self._catalog = self._load_from_disk()
                self._last_load = time.time()
            return self._catalog

    def _replace_catalog(self, payload: dict):
        with self._lock:
            payload["lastUpdate"] = now_str()
            self._validate_minimal(payload)
            self._save_to_disk(payload)
            self._catalog = payload
            self._last_load = time.time()

    def _load_templates(self) -> dict:
        with self._lock:
            if not os.path.exists(self.templates_path):
                raise cherrypy.HTTPError(500, f"No existe {self.templates_path}")
            mtime = os.path.getmtime(self.templates_path)
            if self._templates is None or mtime > self._templates_mtime:
                with open(self.templates_path, "r", encoding="utf-8") as f:
                    self._templates = json.load(f)
                self._templates_mtime = mtime
            return self._templates

    # ----- Device pool: pre-seeded device UUIDs, mutable & persisted -----
    def _load_device_pool(self) -> dict:
        with self._lock:
            if self._device_pool is None:
                if not os.path.exists(self.device_pool_path):
                    raise cherrypy.HTTPError(500, f"No existe {self.device_pool_path}")
                with open(self.device_pool_path, "r", encoding="utf-8") as f:
                    self._device_pool = json.load(f)
            return self._device_pool

    def _save_device_pool_to_disk(self, payload: dict):
        ensure_parent(self.device_pool_path)
        with open(self.device_pool_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _replace_device_pool(self, payload: dict):
        with self._lock:
            self._save_device_pool_to_disk(payload)
            self._device_pool = payload

    # ------------- Minimal schema validation -------------
    def _validate_minimal(self, data: dict):
        required = ["catalog_url", "projectOwners", "project_name",
                    "broker", "servicesList", "devicesList", "roomsList", "usersList"]
        for k in required:
            if k not in data:
                raise cherrypy.HTTPError(400, f"Falta campo obligatorio: {k}")
        if not isinstance(data["servicesList"], list): raise cherrypy.HTTPError(400, "servicesList debe ser lista")
        if not isinstance(data["devicesList"], list):  raise cherrypy.HTTPError(400, "devicesList debe ser lista")
        if not isinstance(data["roomsList"], list):    raise cherrypy.HTTPError(400, "roomsList debe ser lista")
        if not isinstance(data["usersList"], list):    raise cherrypy.HTTPError(400, "usersList debe ser lista")
        pool = data.setdefault("thingspeak_pool", {"slots": []})
        if not isinstance(pool, dict) or not isinstance(pool.get("slots"), list):
            raise cherrypy.HTTPError(400, "thingspeak_pool debe ser objeto con 'slots' lista")

    # ------------- Utilities -------------
    @staticmethod
    def _json_response(obj, status=200):
        cherrypy.response.headers["Content-Type"] = "application/json; charset=utf-8"
        cherrypy.response.status = status
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _require_token():
        if WRITE_TOKEN:
            token = cherrypy.request.headers.get("X-Write-Token")
            if token != WRITE_TOKEN:
                raise cherrypy.HTTPError(401, "Token inválido")

    @staticmethod
    def _read_body() -> dict:
        raw_bytes = cherrypy.request.body.fp.read() if cherrypy.request.body else b""
        raw = raw_bytes.decode("utf-8") if raw_bytes else ""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            raise cherrypy.HTTPError(400, f"JSON inválido: {e}")

    @staticmethod
    def _find_index(seq, key, value):
        for i, item in enumerate(seq):
            if item.get(key) == value:
                return i
        return -1

    # ------------- HTTP endpoints -------------
    @cherrypy.expose
    def health(self):
        return self._json_response({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})

    @cherrypy.expose
    def index(self):
        return self._json_response({"see": "/catalog"})

    @cherrypy.expose
    def catalog(self, **kwargs):
        method = cherrypy.request.method.upper()
        if method == "GET":
            try:
                return self._json_response(self._get_catalog())
            except Exception as e:
                logger.exception("Error leyendo catálogo")
                raise cherrypy.HTTPError(500, str(e))
        elif method in ("PUT", "POST"):
            if READ_ONLY:
                raise cherrypy.HTTPError(403, "Read-only")
            self._require_token()
            try:
                payload = self._read_body()
                self._replace_catalog(payload)
                return self._json_response({"status": "updated", "lastUpdate": self._catalog["lastUpdate"]})
            except cherrypy.HTTPError:
                raise
            except Exception as e:
                logger.exception("Error actualizando catálogo")
                raise cherrypy.HTTPError(400, f"Payload inválido: {e}")
        else:
            raise cherrypy.HTTPError(405)

    @cherrypy.expose
    def device_templates(self, **kwargs):
        if cherrypy.request.method.upper() != "GET":
            raise cherrypy.HTTPError(405)
        return self._json_response(self._load_templates())

    @cherrypy.expose
    def device_pool(self, **kwargs):
        if cherrypy.request.method.upper() != "GET":
            raise cherrypy.HTTPError(405)
        return self._json_response(self._load_device_pool())

    # ----- Services: generic CRUD (known IDs, user-supplied allowed) -----
    @cherrypy.expose
    def services(self, serviceID=None, **kwargs):
        return self._resource_handler("servicesList", "serviceID", serviceID, allow_user_supplied_id=True)

    # ----- Devices: read + PATCH; individual create/delete forbidden -----
    @cherrypy.expose
    def devices(self, deviceID=None, **kwargs):
        method = cherrypy.request.method.upper()

        if method == "GET":
            data = self._get_catalog()
            collection = data.get("devicesList", [])
            if deviceID is None:
                return self._json_response(collection)
            idx = self._find_index(collection, "deviceID", deviceID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"deviceID '{deviceID}' no encontrado")
            return self._json_response(collection[idx])

        if method in ("POST", "DELETE"):
            raise cherrypy.HTTPError(405, "Los dispositivos solo se crean/eliminan vía /rooms")

        if READ_ONLY: raise cherrypy.HTTPError(403, "Read-only")
        self._require_token()

        if method in ("PATCH", "PUT"):
            if deviceID is None:
                raise cherrypy.HTTPError(400, "Especifica deviceID en la URL")
            working = copy.deepcopy(self._get_catalog())
            collection = working.setdefault("devicesList", [])
            idx = self._find_index(collection, "deviceID", deviceID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"deviceID '{deviceID}' no encontrado")
            payload = self._read_body()
            payload.pop("deviceID", None)
            if method == "PATCH":
                collection[idx].update(payload)
            else:
                payload["deviceID"] = deviceID
                collection[idx] = payload
            collection[idx]["timestamp"] = now_str()
            self._replace_catalog(working)
            return self._json_response(collection[idx])

        raise cherrypy.HTTPError(405)

    # ----- Users: server-minted IDs; cascade on delete -----
    @cherrypy.expose
    def users(self, userID=None, **kwargs):
        method = cherrypy.request.method.upper()

        if method == "GET":
            data = self._get_catalog()
            collection = data.get("usersList", [])
            if userID is None:
                return self._json_response(collection)
            idx = self._find_index(collection, "userID", userID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"userID '{userID}' no encontrado")
            return self._json_response(collection[idx])

        if READ_ONLY: raise cherrypy.HTTPError(403, "Read-only")
        self._require_token()

        if method == "POST":
            payload = self._read_body()
            if "userID" in payload:
                raise cherrypy.HTTPError(400, "No proporciones userID; el servidor lo genera")
            working = copy.deepcopy(self._get_catalog())
            collection = working.setdefault("usersList", [])
            payload["userID"] = self._mint_unique_id(working, "usersList", "userID", "usr")
            payload.setdefault("role", "User")
            payload["timestamp"] = now_str()
            collection.append(payload)
            self._replace_catalog(working)
            return self._json_response(payload, status=201)

        if method in ("PATCH", "PUT"):
            if userID is None:
                raise cherrypy.HTTPError(400, "Especifica userID en la URL")
            working = copy.deepcopy(self._get_catalog())
            collection = working.setdefault("usersList", [])
            idx = self._find_index(collection, "userID", userID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"userID '{userID}' no encontrado")
            payload = self._read_body()
            payload.pop("userID", None)
            if method == "PATCH":
                collection[idx].update(payload)
            else:
                payload["userID"] = userID
                collection[idx] = payload
            collection[idx]["timestamp"] = now_str()
            self._replace_catalog(working)
            return self._json_response(collection[idx])

        if method == "DELETE":
            if userID is None:
                raise cherrypy.HTTPError(400, "Especifica userID en la URL")
            working = copy.deepcopy(self._get_catalog())
            working_pool = copy.deepcopy(self._load_device_pool())
            result = self._delete_user_cascade(working, working_pool, userID)
            self._replace_device_pool(working_pool)
            self._replace_catalog(working)
            return self._json_response(result)

        raise cherrypy.HTTPError(405)

    # ----- Rooms: cascade on POST (3 devices + pool slot) and DELETE -----
    @cherrypy.expose
    def rooms(self, roomID=None, **kwargs):
        method = cherrypy.request.method.upper()

        if method == "GET":
            data = self._get_catalog()
            collection = data.get("roomsList", [])
            if roomID is None:
                return self._json_response(collection)
            idx = self._find_index(collection, "roomID", roomID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"roomID '{roomID}' no encontrado")
            return self._json_response(collection[idx])

        if READ_ONLY: raise cherrypy.HTTPError(403, "Read-only")
        self._require_token()

        if method == "POST":
            payload = self._read_body()
            if "roomID" in payload:
                raise cherrypy.HTTPError(400, "No proporciones roomID; el servidor lo genera")
            user_id = payload.get("userID")
            if not user_id:
                raise cherrypy.HTTPError(400, "Falta userID en payload")
            working = copy.deepcopy(self._get_catalog())
            working_pool = copy.deepcopy(self._load_device_pool())
            if self._find_index(working.get("usersList", []), "userID", user_id) < 0:
                raise cherrypy.HTTPError(400, f"userID '{user_id}' no existe")
            room = self._create_room_cascade(working, working_pool, user_id, payload)
            self._replace_device_pool(working_pool)
            self._replace_catalog(working)
            return self._json_response(room, status=201)

        if method in ("PATCH", "PUT"):
            if roomID is None:
                raise cherrypy.HTTPError(400, "Especifica roomID en la URL")
            working = copy.deepcopy(self._get_catalog())
            collection = working.setdefault("roomsList", [])
            idx = self._find_index(collection, "roomID", roomID)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"roomID '{roomID}' no encontrado")
            payload = self._read_body()
            for forbidden in ("roomID", "userID", "thingspeak_info", "connected_devices"):
                payload.pop(forbidden, None)
            if method == "PATCH":
                collection[idx].update(payload)
            else:
                merged = copy.deepcopy(collection[idx])
                merged.update(payload)
                collection[idx] = merged
            collection[idx]["timestamp"] = now_str()
            self._replace_catalog(working)
            return self._json_response(collection[idx])

        if method == "DELETE":
            if roomID is None:
                raise cherrypy.HTTPError(400, "Especifica roomID en la URL")
            working = copy.deepcopy(self._get_catalog())
            working_pool = copy.deepcopy(self._load_device_pool())
            result = self._delete_room_cascade(working, working_pool, roomID)
            self._replace_device_pool(working_pool)
            self._replace_catalog(working)
            return self._json_response(result)

        raise cherrypy.HTTPError(405)

    # ----- ThingSpeak pool -----
    @cherrypy.expose
    def thingspeak_pool(self, **kwargs):
        method = cherrypy.request.method.upper()

        if method == "GET":
            data = self._get_catalog()
            return self._json_response(data.get("thingspeak_pool", {"slots": []}))

        if READ_ONLY: raise cherrypy.HTTPError(403, "Read-only")
        self._require_token()

        if method == "POST":
            payload = self._read_body()
            for required in ("channel_id", "write_api_key", "read_api_key"):
                if required not in payload:
                    raise cherrypy.HTTPError(400, f"Falta {required}")
            working = copy.deepcopy(self._get_catalog())
            pool = working.setdefault("thingspeak_pool", {"slots": []})
            slots = pool.setdefault("slots", [])
            if self._find_index(slots, "channel_id", str(payload["channel_id"])) >= 0:
                raise cherrypy.HTTPError(409, f"channel_id '{payload['channel_id']}' ya existe en pool")
            slot = {
                "channel_id": str(payload["channel_id"]),
                "write_api_key": payload["write_api_key"],
                "read_api_key": payload["read_api_key"],
                "status": "available",
                "assigned_to_room": None,
                "updated_at": now_str(),
            }
            slots.append(slot)
            self._replace_catalog(working)
            return self._json_response(slot, status=201)

        raise cherrypy.HTTPError(405)

    # ------------- Cascade helpers (mutate the dict passed in) -------------
    def _mint_unique_id(self, data: dict, list_key: str, id_key: str, prefix: str) -> str:
        existing = {x.get(id_key) for x in data.get(list_key, []) if isinstance(x, dict)}
        for _ in range(10):
            candidate = uuid_id(prefix)
            if candidate not in existing:
                return candidate
        raise cherrypy.HTTPError(500, "No se pudo generar ID único")

    def _allocate_pool_slot(self, data: dict, room_id: str) -> dict:
        slots = data.setdefault("thingspeak_pool", {"slots": []}).setdefault("slots", [])
        for slot in slots:
            if slot.get("status") == "available":
                slot["status"] = "assigned"
                slot["assigned_to_room"] = room_id
                slot["updated_at"] = now_str()
                return slot
        raise cherrypy.HTTPError(409, "No hay canales de ThingSpeak disponibles en el pool")

    def _release_pool_slot(self, data: dict, room_id: str) -> None:
        slots = data.setdefault("thingspeak_pool", {"slots": []}).setdefault("slots", [])
        for slot in slots:
            if slot.get("assigned_to_room") == room_id:
                slot["status"] = "available"
                slot["assigned_to_room"] = None
                slot["updated_at"] = now_str()

    def _allocate_device_from_pool(self, pool: dict, role: str, room_id: str) -> str:
        """Take the first available deviceID for the given role and mark it as assigned."""
        pools = pool.setdefault("pools", {})
        role_pool = pools.get(role)
        if role_pool is None:
            raise cherrypy.HTTPError(500, f"Rol '{role}' no existe en device_pool.json")
        for entry in role_pool:
            if entry.get("status") == "available":
                entry["status"] = "assigned"
                entry["assigned_to_room"] = room_id
                entry["updated_at"] = now_str()
                return entry["deviceID"]
        raise cherrypy.HTTPError(409, f"Device pool exhausted for role '{role}'")

    def _release_devices_to_pool(self, pool: dict, device_ids) -> None:
        """Mark every device whose ID is in `device_ids` back to 'available'."""
        ids = set(device_ids)
        if not ids:
            return
        pools = pool.setdefault("pools", {})
        for role_entries in pools.values():
            for entry in role_entries:
                if entry.get("deviceID") in ids:
                    entry["status"] = "available"
                    entry["assigned_to_room"] = None
                    entry["updated_at"] = now_str()

    def _instantiate_room_devices(self, data: dict, pool: dict, user_id: str, room_id: str) -> list:
        templates = (self._load_templates() or {}).get("templates") or []
        if not templates:
            raise cherrypy.HTTPError(500, "device_templates.json sin plantillas")
        devices_list = data.setdefault("devicesList", [])
        refs = []
        for tmpl in templates:
            dev = copy.deepcopy(tmpl)
            role = dev.pop("role", dev.get("deviceName"))
            # Take a deviceID from the pre-seeded device pool instead of minting random
            dev_id = self._allocate_device_from_pool(pool, role, room_id)
            dev["deviceID"] = dev_id
            for sd in dev.get("servicesDetails", []) or []:
                for key in ("topic_pub", "topic_sub"):
                    topics = sd.get(key) or []
                    sd[key] = [
                        t.replace("{User}", user_id).replace("{Room}", room_id)
                        for t in topics
                    ]
            dev["timestamp"] = now_str()
            devices_list.append(dev)
            refs.append({"deviceID": dev_id, "role": role})
        return refs

    def _create_room_cascade(self, data: dict, pool: dict, user_id: str, payload: dict) -> dict:
        rooms_list = data.setdefault("roomsList", [])
        room_id = self._mint_unique_id(data, "roomsList", "roomID", "rm")
        slot = self._allocate_pool_slot(data, room_id)
        devices_refs = self._instantiate_room_devices(data, pool, user_id, room_id)
        room = {
            "roomID": room_id,
            "userID": user_id,
            "roomName": payload.get("roomName") or f"Room {len(rooms_list) + 1}",
            "times": payload.get("times") or {"timeawake": None, "timesleep": None},
            "threshold_parameters": payload.get("threshold_parameters") or {},
            "thingspeak_info": {
                "channel_id": slot["channel_id"],
                "write_api_key": slot["write_api_key"],
                "read_api_key": slot["read_api_key"],
            },
            "connected_devices": devices_refs,
            "timestamp": now_str(),
        }
        rooms_list.append(room)
        return room

    def _delete_room_cascade(self, data: dict, pool: dict, room_id: str) -> dict:
        rooms = data.setdefault("roomsList", [])
        idx = self._find_index(rooms, "roomID", room_id)
        if idx < 0:
            raise cherrypy.HTTPError(404, f"roomID '{room_id}' no encontrado")
        room = rooms[idx]
        dev_ids = [d.get("deviceID") for d in room.get("connected_devices", []) if d.get("deviceID")]
        data["devicesList"] = [
            d for d in data.get("devicesList", []) if d.get("deviceID") not in set(dev_ids)
        ]
        self._release_pool_slot(data, room_id)
        # Release the device UUIDs back to the device pool
        self._release_devices_to_pool(pool, dev_ids)
        rooms.pop(idx)
        return {"deleted_room": room_id, "deleted_devices": dev_ids}

    def _delete_user_cascade(self, data: dict, pool: dict, user_id: str) -> dict:
        users = data.setdefault("usersList", [])
        idx = self._find_index(users, "userID", user_id)
        if idx < 0:
            raise cherrypy.HTTPError(404, f"userID '{user_id}' no encontrado")
        user_rooms = [r.get("roomID") for r in data.get("roomsList", []) if r.get("userID") == user_id]
        all_deleted_devices = []
        for rid in user_rooms:
            res = self._delete_room_cascade(data, pool, rid)
            all_deleted_devices.extend(res.get("deleted_devices", []))
        users.pop(idx)
        return {
            "deleted_user": user_id,
            "deleted_rooms": user_rooms,
            "deleted_devices": all_deleted_devices,
        }

    # ------------- Generic CRUD (used only for /services) -------------
    def _resource_handler(self, list_key: str, id_key: str, resource_id, allow_user_supplied_id: bool = False):
        method = cherrypy.request.method.upper()
        data = self._get_catalog()
        try:
            collection = data[list_key]
        except KeyError:
            raise cherrypy.HTTPError(500, f"Catálogo corrupto: falta {list_key}")

        if method == "GET":
            if resource_id is None:
                return self._json_response(collection)
            idx = self._find_index(collection, id_key, resource_id)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"{id_key} '{resource_id}' no encontrado")
            return self._json_response(collection[idx])

        if READ_ONLY:
            raise cherrypy.HTTPError(403, "Read-only")
        self._require_token()

        payload = self._read_body()
        working = copy.deepcopy(data)
        w_collection = working[list_key]

        if method == "POST":
            if not allow_user_supplied_id:
                raise cherrypy.HTTPError(405)
            if id_key not in payload:
                raise cherrypy.HTTPError(400, f"Falta {id_key} en payload")
            if self._find_index(w_collection, id_key, payload[id_key]) >= 0:
                raise cherrypy.HTTPError(409, f"{id_key} ya existe")
            payload.setdefault("timestamp", now_str())
            w_collection.append(payload)
            self._replace_catalog(working)
            return self._json_response(payload, status=201)

        if method in ("PUT", "PATCH"):
            if resource_id is None:
                raise cherrypy.HTTPError(400, f"Especifica {id_key} en la URL")
            idx = self._find_index(w_collection, id_key, resource_id)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"{id_key} '{resource_id}' no encontrado")
            if method == "PATCH":
                w_collection[idx].update(payload)
            else:
                payload.setdefault(id_key, resource_id)
                w_collection[idx] = payload
            w_collection[idx]["timestamp"] = now_str()
            self._replace_catalog(working)
            return self._json_response(w_collection[idx])

        if method == "DELETE":
            if resource_id is None:
                raise cherrypy.HTTPError(400, f"Especifica {id_key} en la URL")
            idx = self._find_index(w_collection, id_key, resource_id)
            if idx < 0:
                raise cherrypy.HTTPError(404, f"{id_key} '{resource_id}' no encontrado")
            removed = w_collection.pop(idx)
            self._replace_catalog(working)
            return self._json_response({"deleted": removed.get(id_key)})

        raise cherrypy.HTTPError(405)


# --------- Server bootstrap ---------
def run():
    svc = CatalogService(CATALOG_PATH, TEMPLATES_PATH, DEVICE_POOL_PATH)
    conf = {
        "/": {
            "tools.response_headers.on": True,
            "tools.response_headers.headers": [
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type, X-Write-Token"),
                ("Content-Type", "application/json; charset=utf-8"),
            ],
        }
    }

    class CORS(object):
        @cherrypy.tools.register("before_handler")
        def cors():
            if cherrypy.request.method == "OPTIONS":
                cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
                cherrypy.response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                cherrypy.response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Write-Token"
                cherrypy.response.status = 204
                return True

    cherrypy.tools.cors = CORS.cors
    cherrypy.config.update({"server.socket_host": "0.0.0.0",
                            "server.socket_port": int(os.getenv("PORT", "9080")),
                            "log.screen": True})
    cherrypy.quickstart(svc, config=conf)


if __name__ == "__main__":
    run()
