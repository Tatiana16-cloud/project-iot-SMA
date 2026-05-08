import os
import time
import json
import logging
import threading
from dataclasses import dataclass
from typing import Dict, Tuple, Any, List, Optional
from datetime import datetime

from paho.mqtt.client import Client as MqttClient, MQTTMessage
from common.catalog_client import CatalogClient

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# --------------- Logging ---------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - timeshift - %(levelname)s - %(message)s",
)
log = logging.getLogger("timeshift")

# --------------- Settings ---------------
@dataclass
class TSSettings:
    catalog_url: str
    broker_ip: str
    broker_port: int
    service_id: str
    mqtt_pub: Dict[str, str]
    mqtt_sub: Dict[str, str]

    loop_interval_sec: int = 10
    wake_alarm_seconds: int = 30
    light_threshold_fallback: int = 2048
    timezone: str = "Europe/Rome"

    @classmethod
    def load(cls, path: str = "settings.json") -> "TSSettings":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        si = data["serviceInfo"]
        base = data["catalogURL"].rstrip("/")
        if base.endswith("/catalog"):
            base = base[: -len("/catalog")]
        return cls(
            catalog_url=base,
            broker_ip=data["brokerIP"],
            broker_port=int(data["brokerPort"]),
            service_id=si.get("serviceID", "TimeShift"),
            mqtt_pub=dict(si.get("MQTT_pub", {})),
            mqtt_sub=dict(si.get("MQTT_sub", {})),
            loop_interval_sec=int(data.get("loop_interval_sec", 10)),
            wake_alarm_seconds=int(data.get("wake_alarm_seconds", 30)),
            light_threshold_fallback=int(data.get("light_threshold_fallback", 2048)),
            timezone=data.get("timezone", "Europe/Rome"),
        )

# --------------- Helpers ---------------
def parse_hhmm(s: str) -> Optional[int]:
    if not s or not isinstance(s, str): return None
    s = s.strip()
    try:
        hh, mm = s.split(":")
        h = int(hh); m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h*60 + m
    except Exception:
        return None
    return None

def in_sleep_window(now_min: int, sleep_min: int, wake_min: int) -> bool:
    if sleep_min is None or wake_min is None:
        return False
    if sleep_min < wake_min:
        return sleep_min <= now_min < wake_min
    else:
        return now_min >= sleep_min or now_min < wake_min

def senml_led_payload(on: bool) -> str:
    return json.dumps([{
        "bn": "stateLed",
        "bt": 0,
        "e": [{"n":"LedL","u":"bool","vb": bool(on)}]
    }])

# --------------- TimeShift core ---------------
class TimeShiftService:
    def __init__(self, settings: TSSettings):
        self.S = settings
        self.cat = CatalogClient(self.S.catalog_url)

        # Keyed by (user_id, room_id) using the raw IDs the catalog mints.
        self.last_light: Dict[Tuple[str, str], int] = {}
        self.last_phase: Dict[Tuple[str, str], str] = {}
        self.known_pairs: set[Tuple[str, str]] = set()

        self.light_min = 0
        self.light_max = 4095

        if ZoneInfo is not None:
            try:
                self.tz = ZoneInfo(self.S.timezone)
            except Exception:
                log.warning("Invalid timezone '%s', fallback to UTC", self.S.timezone)
                self.tz = ZoneInfo("UTC")
        else:
            self.tz = None

        self.mqtt = MqttClient(client_id="timeshift", clean_session=True)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._upsert_service()
        self._seed_pairs_from_catalog()

    # ---------- Catalog ----------
    def _seed_pairs_from_catalog(self):
        """Populate known_pairs from roomsList at startup so we act on rooms
        even before any traffic is observed."""
        try:
            for r in self.cat.get_rooms():
                uid = r.get("userID"); rid = r.get("roomID")
                if uid and rid:
                    self.known_pairs.add((uid, rid))
            log.info("Seeded known_pairs from catalog: %d", len(self.known_pairs))
        except Exception:
            log.exception("seed_pairs_from_catalog failed")

    def _room_times(self, room_id: str) -> Tuple[Optional[int], Optional[int]]:
        try:
            times = self.cat.room_times(room_id) or {}
            ts = parse_hhmm(times.get("timesleep"))
            ta = parse_hhmm(times.get("timeawake"))
            return ts, ta
        except Exception:
            log.exception("Error reading times for room %s", room_id)
            return None, None

    # ---------- MQTT ----------
    def connect_mqtt(self):
        self.mqtt.connect(self.S.broker_ip, self.S.broker_port, keepalive=30)
        self._thread = threading.Thread(target=self.mqtt.loop_forever, daemon=True)
        self._thread.start()
        log.info("MQTT loop thread started.")

    def on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect rc=%s", rc); return
        try:
            topics = list(self.S.mqtt_sub.values()) if self.S.mqtt_sub else []
            if not topics:
                topics = ["SC/+/+/Light"]
            for t in topics:
                sub = self._normalize_sub(t)
                client.subscribe(sub, qos=1)
                log.info("SUB %s (from %s)", sub, t)
        except Exception:
            log.exception("subscribe topics failed")

    def on_message(self, client, userdata, msg: MQTTMessage):
        try:
            topic = msg.topic
            parts = topic.split("/")
            if len(parts) == 4 and parts[0] == "SC" and parts[3] == "Light":
                user, room = parts[1], parts[2]
                self.known_pairs.add((user, room))
                raw = self._parse_light_senml(msg.payload.decode("utf-8","ignore"))
                if raw is not None:
                    self.last_light[(user, room)] = raw
                    log.info("[light] cached raw=%s for %s/%s", raw, user, room)
            elif len(parts) == 4 and parts[0] == "SC" and parts[3] == "initTimeshift":
                user, room = parts[1], parts[2]
                self.known_pairs.add((user, room))
                log.info("[initTimeshift] registered pair user=%s room=%s", user, room)
                # Seed last_phase so we don't fire on the first tick
                phase, ts, ta = self.desired_phase(user, room)
                if phase is not None:
                    self.last_phase[(user, room)] = phase
        except Exception:
            log.exception("on_message error")

    @staticmethod
    def _parse_light_senml(payload: str) -> Optional[int]:
        try:
            arr = json.loads(payload)
            if isinstance(arr, list) and arr:
                rec = arr[0]
                e = rec.get("e", [])
                if isinstance(e, list):
                    for ent in e:
                        if ent.get("n") == "raw":
                            v = ent.get("v")
                            if isinstance(v, (int, float)):
                                return int(v)
        except Exception:
            return None
        return None

    # ---------- Publish helper ----------
    def _pub(self, topic: str, payload, *, qos: int = 1, retain: bool = False):
        try:
            res = self.mqtt.publish(topic, payload=payload, qos=qos, retain=retain)
            res.wait_for_publish()
            log.info("PUB %s (qos=%d retain=%s) -> %s", topic, qos, retain,
                     payload if isinstance(payload, str) else "<bytes>")
        except Exception:
            log.exception("Publish failed: %s", topic)

    @staticmethod
    def _fmt_topic(template: str, user: str, room: str) -> str:
        return (template
                .replace("{User}", user)
                .replace("{Room}", room))

    @staticmethod
    def _normalize_sub(template: str) -> str:
        t = (template or "").replace("{User}", "+").replace("{Room}", "+")
        while "//" in t:
            t = t.replace("//", "/")
        return t

    def pub_sampling(self, user: str, room: str, enable: bool):
        tpl = self.S.mqtt_pub.get("sampling", "SC/{User}/{Room}/sampling")
        topic = self._fmt_topic(tpl, user, room)
        self._pub(topic, json.dumps({"enable": bool(enable)}), qos=1, retain=True)

    def pub_bedtime(self, user: str, room: str):
        tpl = self.S.mqtt_pub.get("bedtime", "SC/{User}/{Room}/bedtime")
        topic = self._fmt_topic(tpl, user, room)
        self._pub(topic, json.dumps({"ts": int(time.time())}), qos=1, retain=False)

    def pub_wakeup(self, user: str, room: str):
        tpl = self.S.mqtt_pub.get("wakeup", "SC/{User}/{Room}/wakeup")
        topic = self._fmt_topic(tpl, user, room)
        self._pub(topic, json.dumps({"seconds": int(self.S.wake_alarm_seconds)}), qos=1, retain=False)

    def pub_led_senml(self, user: str, room: str, on: bool):
        tpl = self.S.mqtt_pub.get("LedL", "SC/{User}/{Room}/LedL/set")
        topic = self._fmt_topic(tpl, user, room)
        self._pub(topic, senml_led_payload(on), qos=1, retain=True)

    def pub_servo(self, user: str, room: str, deg: int):
        tpl = self.S.mqtt_pub.get("servoV", "SC/{User}/{Room}/servoCurtain/set")
        topic = self._fmt_topic(tpl, user, room)
        self._pub(topic, str(int(deg)), qos=1, retain=True)

    # ---------- Core logic ----------
    def desired_phase(self, user: str, room: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        ts, ta = self._room_times(room)
        if ts is None or ta is None:
            return None, ts, ta
        now = datetime.now(self.tz) if self.tz is not None else datetime.now()
        now_min = now.hour * 60 + now.minute
        night = in_sleep_window(now_min, ts, ta)
        return ("night" if night else "day"), ts, ta

    def light_needs_led(self, user: str, room: str) -> bool:
        thr = self.cat.room_thresholds(room) or {}
        threshold = thr.get("light_threshold", self.S.light_threshold_fallback)
        log.info("[thr] user=%s room=%s light_threshold=%s", user, room, threshold)

        raw = self.last_light.get((user, room))
        if raw is None:
            log.info("No light cached for %s/%s -> LED ON by default", user, room)
            return True
        need = raw < threshold
        log.info("[decision] light %s/%s raw=%s thr=%s -> LED %s",
                 user, room, raw, threshold, "ON" if need else "OFF")
        return need

    def do_bedtime(self, user: str, room: str):
        self.pub_bedtime(user, room)
        self.pub_sampling(user, room, True)
        self.pub_servo(user, room, 0)
        self.pub_led_senml(user, room, False)

    def do_wakeup(self, user: str, room: str):
        self.pub_wakeup(user, room)
        led_on = self.light_needs_led(user, room)
        self.pub_led_senml(user, room, led_on)
        self.pub_servo(user, room, 90)
        self.pub_sampling(user, room, False)

    def _upsert_service(self):
        mqtt_sub_list = list(self.S.mqtt_sub.values()) if self.S.mqtt_sub else []
        mqtt_pub_list = list(self.S.mqtt_pub.values()) if self.S.mqtt_pub else []
        try:
            self.cat.upsert_service({
                "serviceID": self.S.service_id,
                "REST_endpoint": "",
                "MQTT_sub": mqtt_sub_list,
                "MQTT_pub": mqtt_pub_list,
            })
        except Exception:
            log.exception("Catalog upsert service failed")

    def run(self):
        self.connect_mqtt()
        log.info("TimeShift running every %ss (TZ=%s)", self.S.loop_interval_sec, self.S.timezone)

        last_reseed = 0.0
        while not self._stop.is_set():
            try:
                # Re-seed rooms from catalog periodically (cheap, 5s TTL cache)
                now = time.time()
                if now - last_reseed > 30:
                    self._seed_pairs_from_catalog()
                    last_reseed = now

                for (user, room) in list(self.known_pairs):
                    phase, ts, ta = self.desired_phase(user, room)
                    if phase is None:
                        continue
                    key = (user, room)
                    if key not in self.last_phase:
                        # First observation of this pair: seed baseline without firing.
                        # Otherwise every newly-created room triggers a wakeup/bedtime
                        # on the next tick regardless of the actual clock.
                        self.last_phase[key] = phase
                        log.info("[%s/%s] Seeded phase=%s (no action on first observation)",
                                 user, room, phase)
                        continue
                    if self.last_phase[key] != phase:
                        self.last_phase[key] = phase
                        if phase == "night":
                            log.info("[%s/%s] Transition -> NIGHT", user, room)
                            self.do_bedtime(user, room)
                        else:
                            log.info("[%s/%s] Transition -> DAY", user, room)
                            self.do_wakeup(user, room)

                self._stop.wait(self.S.loop_interval_sec)
            except Exception:
                log.exception("loop error")
                self._stop.wait(self.S.loop_interval_sec)

    def stop(self):
        self._stop.set()
        try:
            self.mqtt.disconnect()
        except Exception:
            pass

# --------------- Bootstrap ---------------
def main():
    S = TSSettings.load("settings.json")
    svc = TimeShiftService(S)
    try:
        svc.run()
    except KeyboardInterrupt:
        svc.stop()

if __name__ == "__main__":
    main()
