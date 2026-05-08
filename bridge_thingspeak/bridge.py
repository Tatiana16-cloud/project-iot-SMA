import os
import re
import json
import time
import logging
import requests
from typing import Dict, List, Tuple, Any

from common.MyMQTT import MQTTClient
from common.senml import parse_senml
from common.catalog_client import CatalogClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - bridge_ts - %(levelname)s - %(message)s",
)
log = logging.getLogger("bridge_ts")


class BridgeSettings:
    """
    Carga settings desde JSON, sin defaults “hardcodeados”.
    Si falta algo requerido => ValueError.
    """
    REQUIRED_ROOT = [
        "catalogURL",
        "ThingspeakWriteURL",
        "brokerIP",
        "brokerPort",
        "minPeriodSec",
        "serviceInfo",
        "fields",
    ]
    REQUIRED_SERVICE = ["serviceID", "MQTT_sub"]  # MQTT_pub opcional

    def __init__(self, path: str = "settings.json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Validación estricta
        for k in self.REQUIRED_ROOT:
            if k not in data:
                raise ValueError(f"[settings] Falta clave requerida: {k}")

        svc = data["serviceInfo"]
        for k in self.REQUIRED_SERVICE:
            if k not in svc:
                raise ValueError(f"[settings] Falta serviceInfo.{k}")

        if not isinstance(data["fields"], dict) or not data["fields"]:
            raise ValueError("[settings] 'fields' debe ser un objeto no vacío")

        # Asignación 1:1 desde archivo (sin defaults)
        self.catalog_url: str  = data["catalogURL"]
        self.ts_write_url: str = data["ThingspeakWriteURL"]
        self.broker_ip: str    = data["brokerIP"]
        self.broker_port: int  = int(data["brokerPort"])
        self.min_period: int   = int(data["minPeriodSec"])
        self.wakeup_delay: int = int(data.get("wakeupDelaySec", 5))

        self.service_id: str      = svc["serviceID"]
        self.rest_endpoint: str   = svc.get("REST_endpoint", "")
        self.mqtt_subs: List[str] = list(svc["MQTT_sub"])
        self.mqtt_pub:  List[str] = list(svc.get("MQTT_pub", []))

        self.fields_map: Dict[str, str] = dict(data["fields"])

    @staticmethod
    def normalize_topics(topics: List[str]) -> List[str]:
        """
        No inventa suscripciones nuevas; solo limpia espacios y barras duplicadas.
        """
        out = []
        for t in topics:
            t = (t or "").strip()
            if not t:
                continue
            while "//" in t:
                t = t.replace("//", "/")
            out.append(t)
        return out


class ThingspeakBridge:
    """Bridge MQTT → ThingSpeak, broker+subs estrictamente desde settings.json.
       El catálogo solo se usa para obtener API keys por usuario/habitación.
    """

    RE_SC = re.compile(r"^SC/([^/]+)/([^/]+)/")  # SC/<User>/<Room>/...

    def __init__(self, settings: BridgeSettings,
                 catalog: CatalogClient | None = None,
                 mqtt_cls=MQTTClient):
        self.S = settings
        self.catalog = catalog or CatalogClient(url=self.S.catalog_url, ttl=5)
        self.mqtt_cls = mqtt_cls
        self.debug = os.getenv("BRIDGE_DEBUG", "true").lower() == "true"
        if self.debug:
            log.setLevel(logging.DEBUG)

        # Estado por (user,room)
        self.states: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Mapa (user,room) -> (write_api_key, channel_id | None)
        self.user_api: Dict[Tuple[str, str], Tuple[str, str | None]] = {}
        # Conteo de eventos enviados por ventana (reset con initTimeshift)
        self.window_counts: Dict[Tuple[str, str], int] = {}
        # Conteo de alertas por ventana (reinicia con initTimeshift)
        self.alert_counts: Dict[Tuple[str, str], int] = {}

        # Broker y subs estrictamente desde settings.json
        self.broker_host = self.S.broker_ip
        self.broker_port = self.S.broker_port
        self.subscriptions = [self._normalize_sub(t) for t in BridgeSettings.normalize_topics(self.S.mqtt_subs)]

        # Cliente MQTT
        self.mqtt = self.mqtt_cls(cid="svc-bridge-ts",
                                  host=self.broker_host,
                                  port=self.broker_port)

        # Registrar servicio en catálogo (best-effort)
        try:
            self.catalog.upsert_service({
                "serviceID": self.S.service_id,
                "REST_endpoint": self.S.rest_endpoint,
                "MQTT_sub": self.subscriptions,
                "MQTT_pub": self.S.mqtt_pub,
            })
            log.info("service upserted")
        except Exception as e:
            log.warning("cannot upsert service: %s", e)

    # ---------- logging ----------
    def _debug(self, msg: str):
        if self.debug:
            log.debug(msg)

    @staticmethod
    def _normalize_sub(template: str) -> str:
        """Reemplaza placeholders {User}/{Room} por comodines +."""
        t = (template or "").replace("{User}", "+").replace("{Room}", "+")
        while "//" in t:
            t = t.replace("//", "/")
        return t

    # ---------- bootstrap ----------
    def _refresh_user_api_map(self):
        try:
            raw = self.catalog.rooms_map_api_keys()
            # Attach channel_id when we know it
            new_map: Dict[Tuple[str, str], Tuple[str, str | None]] = {}
            for r in self.catalog.get().get("roomsList", []):
                uid = r.get("userID"); rid = r.get("roomID")
                ts = r.get("thingspeak_info") or {}
                wk = ts.get("write_api_key"); ch = ts.get("channel_id")
                if uid and rid and wk:
                    new_map[(uid, rid)] = (wk, ch)
            # Fall back to map from helper if roomsList iteration missed anything
            for k, wk in raw.items():
                new_map.setdefault(k, (wk, None))
            self.user_api = new_map
        except Exception as e:
            print("[bridge] WARN: cannot load rooms from catalog:", e)

    # ---------- state ----------
    def _ensure_state(self, user: str, room: str) -> Dict[str, Any]:
        key = (user, room)
        if key not in self.states:
            self.states[key] = {
                "last": 0.0,
                "wakeup_due": 0.0,
                "vals": {
                    "temp": None, "hum": None, "bpm": None, "light": None,
                    "servoFan": None, "servoCurtain": None, "LedL": None,
                    "alerts": None
                },
                "acc": {
                    "temp": {"sum": 0.0, "count": 0},
                    "hum": {"sum": 0.0, "count": 0},
                    "bpm": {"sum": 0.0, "count": 0},
                    "light": {"sum": 0.0, "count": 0},
                },
            }
        return self.states[key]

    @staticmethod
    def _to_bool(v):
        if isinstance(v, bool):   return v
        if isinstance(v, (int, float)): return bool(int(v))
        if isinstance(v, str):    return v.strip().lower() in ("true", "1", "on")
        return None

    # ---------- ThingSpeak POST ----------
    def _post_thingspeak(self, write_api_key: str, values: Dict[str, Any]):
        params = {"api_key": write_api_key}
        for name, field in self.S.fields_map.items():
            val = values.get(name)
            if val is None:
                continue
            if name in ("servoFan", "servoCurtain", "LedL"):
                params[field] = 1 if self._to_bool(val) else 0
            else:
                params[field] = val

        if len(params) == 1:
            # solo api_key ⇒ no hay nada que enviar
            log.info("skip: no fields to send")
            return None

        log.info("POST TS -> %s", params)
        r = requests.post(self.S.ts_write_url, params=params, timeout=5)
        return r

    # ---------- API key lookup ----------
    def _get_ts_creds(self, user: str, room: str) -> Tuple[str, str | None] | None:
        key = (user, room)
        if key in self.user_api:
            val = self.user_api[key]
            if isinstance(val, tuple) and len(val) == 2:
                return val
            # migrate old cache (solo key)
            return (val, None) if isinstance(val, str) else None
        try:
            r = self.catalog.get_room(room)
            if not r or r.get("userID") != user:
                return None
            ts = (r.get("thingspeak_info") or {})
            wk = ts.get("write_api_key")
            channel = ts.get("channel_id")
            if wk:
                self.user_api[key] = (wk, channel)
                return self.user_api[key]
        except Exception as e:
            log.warning("cannot load room %s from catalog: %s", room, e)
        return None

    @staticmethod
    def _field_num(field_name: str) -> str | None:
        # expects "fieldN"
        if field_name.startswith("field"):
            return field_name[5:]
        return None

    def _update_chart_results(self, channel_id: str | None, write_key: str, results: int):
        if not channel_id:
            return
        # best-effort: update all mapped fields charts
        for fname in self.S.fields_map.values():
            num = self._field_num(fname)
            if not num:
                continue
            url = f"{self.S.ts_write_url.rsplit('/',1)[0].replace('/update','')}/channels/{channel_id}/charts/{num}.json"
            try:
                requests.post(url, params={"api_key": write_key, "results": results}, timeout=5)
            except Exception as e:
                self._debug(f"chart update failed: {e}")

    # ---------- MQTT helpers ----------
    def _handle_init_timeshift(self, user: str, room: str):
        key = (user, room)
        self.alert_counts[key] = 0
        self.window_counts[key] = 0
        st = self._ensure_state(user, room)
        st["vals"]["alerts"] = 0
        st["wakeup_due"] = 0.0
        log.info(f"[bridge] initTimeshift {user}/{room}: alerts count reset. Next periodic send will include alerts=0.")

    def _parse_senml_safe(self, topic: str, payload: str) -> List[Any]:
        try:
            measures = parse_senml(payload)
            if not isinstance(measures, list):
                self._debug(f"non-SenML message skipped topic={topic} payload={payload[:200]}")
                return []
            return measures
        except Exception as e:
            self._debug(f"parse skipped (not SenML) topic={topic} payload={payload[:200]} err={e}")
            return []

    def _handle_alert_json(self, user: str, room: str, topic: str, payload: str):
        try:
            data = json.loads(payload)
        except Exception as e:
            self._debug(f"alert payload not JSON-parsable topic={topic} err={e}")
            return
            
        # Validar si contiene al menos un evento con status="ALERT"
        events = data.get("events", [])
        if isinstance(events, list):
            has_alert = any(evt.get("status") == "ALERT" for evt in events if isinstance(evt, dict))
            if not has_alert:
                self._debug(f"[alert] Skip: no ALERT status in msg {topic}")
                return
        else:
            # si no hay events lista, ignorar o procesar diferente
            self._debug(f"[alert] Skip: malformed events in msg {topic}")
            return

        key = (user, room)
        self.alert_counts[key] = self.alert_counts.get(key, 0) + 1
        count = self.alert_counts[key]
        log.info(f"[alert] Count incremented for {user}/{room}: {count} (topic={topic})")
        
        # Guardar en estado para el próximo envío periódico
        st = self._ensure_state(user, room)
        st["vals"]["alerts"] = count

    def _process_measures(self, measures, st):
        for name, unit, val, ts in measures:
            if not name:
                continue
            base = name.split('/')[-1]
            if base == "raw":
                base = "light"
            
            # Si es un campo conocido, actualizar
            if base in st["vals"]:
                if base in ("servoFan", "servoCurtain", "LedL"):
                    st["vals"][base] = self._to_bool(val)
                else:
                    if isinstance(val, (int, float)):
                        st["vals"][base] = float(val)
                        if base in st["acc"]:
                            st["acc"][base]["sum"] += float(val)
                            st["acc"][base]["count"] += 1
            
            # Si es un campo dinámico o de control directo, asegurar que esté en vals
            if base in ("servoCurtain", "LedL", "ServoDHT"):
                 # Force update if not already handled
                 st["vals"][base] = self._to_bool(val) if base in ("servoCurtain", "LedL") else val

    def _send_periodic(self, user: str, room: str, st, api_key: str, channel_id: str | None, now: float):
        # Allow forced send on wakeup_due, otherwise respect min_period
        forced = st.get("wakeup_due", 0.0) and now >= st.get("wakeup_due", 0.0)
        if not forced and (now - st["last"] < self.S.min_period):
            # self._debug(f"[bridge] rate-limit {user}/{room}, skip periodic")
            return
            
        payload_values: Dict[str, Any] = {}
        for name in self.S.fields_map.keys():
            # Casos promediables
            if name in st["acc"]:
                cnt = st["acc"][name]["count"]
                if cnt > 0:
                    payload_values[name] = round(st["acc"][name]["sum"] / cnt, 2)
                elif st["vals"].get(name) is not None:
                    # Fallback a ultimo valor si no hubo lecturas nuevas pero hay estado previo?
                    # O preferible enviar solo si hubo lecturas?
                    # Requerimiento: "todo se envie siempre". 
                    # Si "acc" count es 0, significa que no hubo lecturas de sensor en este periodo.
                    # Si enviamos valor viejo, repetimos dato. Si no enviamos, es null.
                    # Asumamos que enviamos ultimo valor conocido si existe, o null.
                    payload_values[name] = st["vals"][name]
            else:
                # Casos no promediables (actuadores, alertas) -> enviar ultimo valor conocido
                if st["vals"].get(name) is not None:
                    payload_values[name] = st["vals"][name]

        if not payload_values:
            self._debug(f"[periodic] nothing to send for {user}/{room}")
            st["last"] = now  # reset timer anyway to avoid busy loop checks?
            st["wakeup_due"] = 0.0
            return

        self._debug(f"sending periodic payload {payload_values} for {user}/{room}")
        try:
            r = self._post_thingspeak(api_key, payload_values)
            if r is not None:
                log.info("TS periodic %s (entry_id=%s) (%s/%s) -> %s", r.status_code, r.text, user, room, payload_values)
                if str(r.text).strip() == "0":
                    log.warning("[periodic] TS rejected (entry_id=0), likely rate-limit. payload=%s", payload_values)
                else:
                    self.window_counts[(user, room)] = self.window_counts.get((user, room), 0) + 1
                    # self._update_chart_results(channel_id, api_key, self.window_counts[(user, room)])
            
            st["last"] = now
            st["wakeup_due"] = 0.0
            # Reset accumulators
            for name in st["acc"]:
                st["acc"][name]["sum"] = 0.0
                st["acc"][name]["count"] = 0
        except Exception as e:
            log.error("TS periodic error: %s", e)

    def _check_wakeup_due(self):
        """Background tick to send pending wakeup-forced payloads without waiting for new MQTT messages."""
        now = time.time()
        for (user, room), st in list(self.states.items()):
            due = st.get("wakeup_due", 0.0) or 0.0
            if due > 0 and now >= due:
                creds = self._get_ts_creds(user, room)
                if not creds:
                    log.warning("No API key for %s/%s (wakeup_due), skip.", user, room)
                    st["last"] = now
                    st["wakeup_due"] = 0.0
                    continue
                api_key, channel_id = creds
                self._send_periodic(user, room, st, api_key, channel_id, now)

    def _on_msg(self, topic: str, payload: str):
        try:
            t = topic.lstrip("/")
            
            user = None
            room = None

            # 1. Parse topic to extract User/Room
            if t.startswith("SC/alerts/"):
                # Format: SC/alerts/<User>/<Room>/...
                parts = t.split('/')
                if len(parts) >= 4:
                    user = parts[2]
                    room = parts[3]
            else:
                # Format: SC/<User>/<Room>/...
                m = self.RE_SC.match(t)
                if m:
                    user, room = m.group(1), m.group(2)

            if not user or not room:
                self._debug(f"topic not matched or invalid format {topic}")
                return

            st = self._ensure_state(user, room)

            if topic.endswith("/initTimeshift"):
                self._handle_init_timeshift(user, room)
                return

            if topic.endswith("/wakeup"):
                now_ts = time.time()
                elapsed = now_ts - st["last"]
                remaining = max(0.0, self.S.min_period - elapsed)
                st["wakeup_due"] = now_ts + remaining
                log.info("[wakeup] Scheduled forced send for %s/%s after %.2fs (remaining to minPeriod)", user, room, remaining)
                # still try to parse payload if any, but if not SenML it's fine
                # we continue to parse below to capture any vals if provided

            self._debug(f"MQTT msg {topic} payload={payload[:200]}")

            measures = self._parse_senml_safe(topic, payload)
            if not measures:
                if topic.startswith("SC/alerts/"):
                    self._handle_alert_json(user, room, topic, payload)
                return

            self._process_measures(measures, st)

            creds = self._get_ts_creds(user, room)
            if not creds:
                log.warning("No API key for %s/%s, skip.", user, room)
                st["last"] = time.time()
                return
            api_key, channel_id = creds

            now = time.time()
            self._send_periodic(user, room, st, api_key, channel_id, now)
        except Exception as e:
            log.error("on_message fatal: %s (topic=%s payload=%s)", e, topic, payload[:200])

    # ---------- run ----------
    def run(self):
        # Cargar mapa inicial de API keys
        self._refresh_user_api_map()

        # Suscripciones (estrictas desde settings)
        for t in self.subscriptions:
            self.mqtt.sub(t, self._on_msg)

        print(f"[bridge] broker={self.broker_host}:{self.broker_port} subs={self.subscriptions}")
        while True:
            # Tick pending wakeup_due to avoid waiting for new MQTT messages
            self._check_wakeup_due()
            time.sleep(1)


if __name__ == "__main__":
    # Ruta al settings.json (opcional por env: SETTINGS_PATH)
    settings = BridgeSettings()
    bridge = ThingspeakBridge(settings)
    bridge.run()



