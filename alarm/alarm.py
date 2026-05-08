import json
import time
from typing import Dict, Any, List, Tuple
from common.MyMQTT import MQTTClient
from common.senml import parse_senml
from common.catalog_client import CatalogClient


class AlarmSettings:
    def __init__(self, path: str = "settings.json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.catalog_url: str = data["catalogURL"]
        self.broker_ip: str = data["brokerIP"]
        self.broker_port: int = int(data["brokerPort"])
        svc = data["serviceInfo"]
        self.service_id: str = svc["serviceID"]
        self.subscriptions: List[str] = list(svc["MQTT_sub"])
        self.pub_alert_env: str = svc["MQTT_pub_alert_env"]
        self.pub_alert_hr:  str = svc["MQTT_pub_alert_hr"]



class AlarmControl:
    """Microservice that evaluates HR, temperature, and humidity."""

    RE_SC = r"^SC/([^/]+)/([^/]+)/"

    def __init__(self, settings: AlarmSettings):
        self.S = settings
        self.catalog = CatalogClient(url=self.S.catalog_url, ttl=5)
        self.mqtt = MQTTClient(
            cid=f"svc-{self.S.service_id}",
            host=self.S.broker_ip,
            port=self.S.broker_port,
        )
        # Best-effort: update service entry in Catalog (MQTT_sub/MQTT_pub/timestamp)
        self.catalog.upsert_service({
            "serviceID": self.S.service_id,
            "REST_endpoint": "",
            "MQTT_sub": self.S.subscriptions,
            "MQTT_pub": [self.S.pub_alert_hr, self.S.pub_alert_env],
        })

    # ---------- Helpers ----------
    @staticmethod
    def _fmt_topic(template: str, user: str, room: str) -> str:
        """Supports placeholders {User} and {Room}."""
        return (template
                .replace("{User}", user)
                .replace("{Room}", room))

    def _room_thresholds(self, room_id: str) -> Dict[str, float]:
        """Fetch thresholds from catalog for the room via GET /rooms/{room_id}."""
        try:
            return self.catalog.room_thresholds(room_id)
        except Exception as e:
            print(f"[alarm] WARN: cannot load thresholds from catalog (room={room_id}): {e}")
            return {}

    # ---------- Publishing ----------
    def _publish_alert_env(self, user: str, room: str, src_topic: str, payload: Dict[str, Any]):
        msg = {
            "service": self.S.service_id,
            "source_topic": src_topic,
            "type": "env",
            **payload,
            "ts": int(time.time())
        }
        print(f"[alarm] PUBLISH ENV -> {msg}")
        topic = self._fmt_topic(self.S.pub_alert_env, user, room)
        self.mqtt.pub(topic, json.dumps(msg), qos=1, retain=False)
        print(f"[alarm] ALERT ENV -> {topic}: {payload}")

    def _publish_alert_hr(self, user: str, room: str, src_topic: str, payload: Dict[str, Any]):
        msg = {
            "service": self.S.service_id,
            "source_topic": src_topic,
            "type": "hr",
            **payload,
            "ts": int(time.time())
        }
        topic = self._fmt_topic(self.S.pub_alert_hr, user, room)
        self.mqtt.pub(topic, json.dumps(msg), qos=1, retain=False)
        print(f"[alarm] ALERT HR  -> {topic}: {payload}")


        # ---------- MQTT Callback ----------
    def _on_msg(self, topic: str, payload: str):
        t = topic.lstrip("/")           # tolerates "/SC" or "SC"
        parts = t.split("/")
        if len(parts) < 4 or parts[0] != "SC":
            return
        user, room, leaf = parts[1], parts[2], parts[3]  # leaf: hr | dht

        # Per-room thresholds from catalog
        thr = self._room_thresholds(room)
        print(f"[alarm] thresholds: {thr} for room {room}")

        # Robust SenML parsing
        try:
            measures = parse_senml(payload)
        except Exception as e:
            print(f"[alarm] bad SenML: {e}")
            return

        vals: Dict[str, float] = {}
        for name, unit, val, ts in measures:
            name = name.replace("//", "/")
            base = name.split("/")[-1]
            if base in ("bpm", "temp", "hum"):
                try:
                    vals[base] = float(val)
                except Exception:
                    pass

        if leaf == "hr":
            # ----- HR branch -----
            v = vals.get("bpm")
            if v is None:
                return

            low = thr.get("hr_low")
            high = thr.get("hr_high")
            if low is None or high is None:
                print(f"[alarm] missing hr thresholds for room={room}")
                return
            in_range = (low <= v <= high)

            self._publish_alert_hr(user, room, t, {
                "variable": "bpm",
                "value": v,
                "bounds": [low, high],
                "status": "OK" if in_range else "ALERT",
                "message": (
                    "bpm within range"
                    if in_range
                    else f"bpm out of range ({low}-{high})"
                ),
            })
            return


        if leaf == "dht":
            # ----- TEMP+HUM: always publish a single message with both -----
            temp = vals.get("temp")
            hum  = vals.get("hum")

            # If neither arrived, skip
            if temp is None and hum is None:
                return

            def pack(var: str, val):
                key_low = f"{var}_low"
                key_high = f"{var}_high"
                low = thr.get(key_low)
                high = thr.get(key_high)
                if low is None or high is None:
                    return {
                        "variable": var,
                        "value": val,
                        "bounds": None,
                        "status": "NODATA",
                        "message": "thresholds missing"
                    }
                if val is None:
                    return {
                        "variable": var,
                        "value": None,
                        "bounds": [low, high],
                        "status": "NODATA",
                        "message": "no value"
                    }
                in_range = (low <= val <= high)
                return {
                    "variable": var,
                    "value": val,
                    "bounds": [low, high],
                    "status": "OK" if in_range else "ALERT",
                    "message": ("within range"
                                if in_range
                                else f"out of range ({low}-{high})")
                }

            payload = {
                "events": [
                    pack("temp", temp),
                    pack("hum",  hum)
                ]
            }
            self._publish_alert_env(user, room, t, payload)
            return



    # ---------- Run loop ----------
    def run(self):
        for t in self.S.subscriptions:
            self.mqtt.sub(t, self._on_msg)
        print(f"[alarm] broker={self.S.broker_ip}:{self.S.broker_port} subs={self.S.subscriptions}")
        while True:
            time.sleep(1)


if __name__ == "__main__":
    st = AlarmSettings("settings.json")
    svc = AlarmControl(st)
    svc.run()
