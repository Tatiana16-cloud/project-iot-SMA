import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

import cherrypy
import requests
import pandas as pd
import numpy as np
from dateutil import tz, parser as dateparser

from common.catalog_client import CatalogClient


# -------------- Time utilities ----------------

def now_rome() -> datetime:
    return datetime.now(tz.gettz("Europe/Rome"))


# -------------- Catalog / Users ----------------

def extract_times(room_obj: Dict[str, Any]) -> Dict[str, str]:
    times = room_obj.get("times", {}) or {}
    timesleep = times.get("timesleep")
    timeawake = times.get("timeawake")
    if not timesleep or not timeawake:
        raise cherrypy.HTTPError(400, "timesleep/timeawake missing in catalog room object")
    return {"timesleep": str(timesleep), "timeawake": str(timeawake)}


def extract_thingspeak(room_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Return channel_id and read/write keys from a room's thingspeak_info."""
    tsi = room_obj.get("thingspeak_info", {}) or {}
    channel = str(tsi.get("channel_id") or "").strip()
    keys: List[str] = []
    for k in (tsi.get("read_api_key"), tsi.get("write_api_key")):
        if k:
            keys.append(str(k).strip())
    return {"channel": channel, "keys": keys}


# ============================== Sleep window ==============================

def window_for_date(timesleep: str, timeawake: str, ref_date_rome: datetime) -> Tuple[datetime, datetime]:
    """Return [start, end) window in Europe/Rome, handling midnight crossing."""
    tz_rome = tz.gettz("Europe/Rome")
    today = ref_date_rome.astimezone(tz_rome).date()
    yesterday = today - timedelta(days=1)

    ts_h, ts_m = map(int, timesleep.split(":"))
    ta_h, ta_m = map(int, timeawake.split(":"))

    ts_today = datetime(today.year, today.month, today.day, ts_h, ts_m, tzinfo=tz_rome)
    ta_today = datetime(today.year, today.month, today.day, ta_h, ta_m, tzinfo=tz_rome)

    if ts_today < ta_today:
        start_dt = ts_today
        end_dt = ta_today
    else:
        start_dt = datetime(yesterday.year, yesterday.month, yesterday.day, ts_h, ts_m, tzinfo=tz_rome)
        end_dt = ta_today

    return start_dt, end_dt


# ================================ ThingSpeak ==================================

def _normalize_ts_df(feeds: list) -> pd.DataFrame:
    """Convert TS feeds to DataFrame and normalize created_at to Europe/Rome."""
    if not feeds:
        return pd.DataFrame(columns=["created_at"])
    df = pd.DataFrame(feeds)
    if "created_at" in df.columns:
        # TS devuelve UTC con 'Z'; si viniera naive, igual utc=True funciona
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        df["created_at"] = df["created_at"].dt.tz_convert("Europe/Rome")
    return df


def fetch_ts_robusto(base_url: str, channel_id: str, keys: List[str],
                     start_local: datetime, end_local: datetime) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Try several combos to fetch feeds; returns (normalized df, debug_info)."""
    def _do_req(params: dict) -> dict:
        url = f"{base_url}/channels/{channel_id}/feeds.json"
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json() if r.content else {}

    debug = {"attempts": []}

    # 1) A: pasar ventana local con timezone
    candidate_keys = keys + [None] if keys else [None]
    for key in candidate_keys:
        params = {
            "start": start_local.strftime("%Y-%m-%d %H:%M:%S"),
            "end":   end_local.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Europe/Rome",
        }
        if key:
            params["api_key"] = key
        try:
            obj = _do_req(params)
            feeds = obj.get("feeds", []) if isinstance(obj, dict) else []
            debug["attempts"].append({
                "mode": "local_tz",
                "key_tail": (key[-4:] if key else None),
                "feeds": len(feeds)
            })
            if feeds:
                return _normalize_ts_df(feeds), debug
        except requests.RequestException as e:
            debug["attempts"].append({
                "mode": "local_tz",
                "key_tail": (key[-4:] if key else None),
                "error": str(e)
            })

    # 2) B: convertir a UTC y NO enviar timezone
    start_utc = start_local.astimezone(timezone.utc)
    end_utc   = end_local.astimezone(timezone.utc)
    for key in candidate_keys:
        params = {
            "start": start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "end":   end_utc.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if key:
            params["api_key"] = key
        try:
            obj = _do_req(params)
            feeds = obj.get("feeds", []) if isinstance(obj, dict) else []
            debug["attempts"].append({
                "mode": "utc_no_tzparam",
                "key_tail": (key[-4:] if key else None),
                "feeds": len(feeds)
            })
            if feeds:
                return _normalize_ts_df(feeds), debug
        except requests.RequestException as e:
            debug["attempts"].append({
                "mode": "utc_no_tzparam",
                "key_tail": (key[-4:] if key else None),
                "error": str(e)
            })

    # Nada funcionó
    return pd.DataFrame(columns=["created_at"]), debug


# ============================== Metrics/statistics ==============================

def pick_fields(df: pd.DataFrame, bpm_field: str, temp_field: str,
                hum_field: str, led_field: str) -> pd.DataFrame:
    cols = ["created_at", bpm_field, temp_field, hum_field, led_field]
    keep = [c for c in cols if c in df.columns]
    if not keep:
        return pd.DataFrame(columns=["created_at"])
    df2 = df[keep].copy()
    for c in [bpm_field, temp_field, hum_field, led_field]:
        if c in df2.columns:
            df2[c] = pd.to_numeric(df2[c], errors="coerce")
    return df2.dropna(subset=["created_at"])


def basic_stats(series: pd.Series) -> Dict[str, Optional[float]]:
    if series.dropna().empty:
        return {"mean": None, "min": None, "max": None}
    return {
        "mean": float(np.nanmean(series)),
        "min": float(np.nanmin(series)),
        "max": float(np.nanmax(series)),
    }


def count_led_activations(led_series: pd.Series) -> int:
    """Bridge sends an accumulated counter; activations = max value in window."""
    if led_series.dropna().empty:
        return 0
    return int(led_series.max())


def infer_sleep_stages_from_bpm(df: pd.DataFrame, bpm_field: str) -> Dict[str, Any]:
    if bpm_field not in df.columns or df[bpm_field].dropna().empty:
        return {"deep_h": 0.0, "light_h": 0.0, "rem_h": 0.0}

    d = df[["created_at", bpm_field]].dropna().sort_values("created_at").copy()
    if d.empty:
        return {"deep_h": 0.0, "light_h": 0.0, "rem_h": 0.0}

    # Rolling median as baseline (~15 samples)
    d["baseline"] = d[bpm_field].rolling(window=15, min_periods=1).median()

    def stage_label(row):
        bpm = row[bpm_field]
        base = row["baseline"]
        if pd.isna(bpm) or pd.isna(base):
            return "light"
        if bpm <= base - 10:
            return "deep"
        elif bpm >= base + 5:
            return "rem"
        else:
            return "light"

    d["stage"] = d.apply(stage_label, axis=1)
    d["dt_sec"] = d["created_at"].shift(-1) - d["created_at"]
    d.loc[d.index[-1], "dt_sec"] = pd.Timedelta(seconds=30)  # asume 30s si no hay siguiente
    d["dt_sec"] = d["dt_sec"].dt.total_seconds().clip(lower=0)

    deep_h = d.loc[d["stage"] == "deep", "dt_sec"].sum() / 3600.0
    light_h = d.loc[d["stage"] == "light", "dt_sec"].sum() / 3600.0
    rem_h = d.loc[d["stage"] == "rem", "dt_sec"].sum() / 3600.0

    return {"deep_h": round(deep_h, 2), "light_h": round(light_h, 2), "rem_h": round(rem_h, 2)}


def sleep_quality(temp_stats: Dict[str, Optional[float]],
                  hum_stats: Dict[str, Optional[float]],
                  bpm_stats: Dict[str, Optional[float]],
                  alarm_count: int,
                  duration_hours: float) -> Dict[str, Any]:
    # Temperatura ideal ~19°C
    t_mean = temp_stats.get("mean")
    if t_mean is None:
        score_temp = 50.0
    else:
        dist = min(abs(t_mean - 19.0), 6.0)
        score_temp = 100.0 * (1.0 - dist / 6.0)

    # Humedad ideal 40–60 %RH
    h_mean = hum_stats.get("mean")
    if h_mean is None:
        score_hum = 50.0
    else:
        if 40 <= h_mean <= 60:
            score_hum = 100.0
        else:
            dist = min(abs(h_mean - 50), 30.0)
            score_hum = max(0.0, 100.0 * (1.0 - dist / 30.0))

    # Variabilidad BPM (span)
    b_min, b_max = bpm_stats.get("min"), bpm_stats.get("max")
    if b_min is None or b_max is None:
        score_bpm = 50.0
    else:
        span = min(max(0.0, b_max - b_min), 40.0)
        score_bpm = 100.0 * (1.0 - span / 40.0)

    # Penalize alerts by rate: alerts per hour. Bursts in short windows hurt more.
    dh = max(duration_hours, 1e-3)
    rate_per_hour = alarm_count / dh
    penalty = min(rate_per_hour * 2, 40.0)
    score = max(0.0, min(100.0, 0.35 * score_temp + 0.25 * score_hum + 0.40 * score_bpm - penalty))

    if score >= 85:
        label = "your sleep was almost perfect"
    elif score >= 70:
        label = "you slept well"
    elif score >= 50:
        label = "you slept okay"
    else:
        label = "you slept poorly"

    return {
        "score": round(score, 1),
        "label": label,
        "components": {
            "temp": round(score_temp, 1),
            "hum": round(score_hum, 1),
            "bpm": round(score_bpm, 1),
            "penalty": round(penalty, 1)
        }
    }


def build_user_tips(temp_stats: Dict[str, Optional[float]],
                    hum_stats: Dict[str, Optional[float]],
                    bpm_stats: Dict[str, Optional[float]],
                    alarm_count: int) -> List[str]:
    tips: List[str] = []

    t_mean = temp_stats.get("mean")
    if t_mean is not None:
        if t_mean > 24:
            tips.append("Your room looked a bit warm. A cooler room may help you sleep more comfortably.")
        elif t_mean < 17:
            tips.append("Your room looked a bit cold. A slightly warmer room could make sleep more comfortable.")
        else:
            tips.append("Your room temperature stayed in a comfortable range for sleep.")

    h_mean = hum_stats.get("mean")
    if h_mean is not None:
        if h_mean > 65:
            tips.append("The air looked quite humid. Better ventilation may help the room feel fresher at night.")
        elif h_mean < 35:
            tips.append("The air looked a bit dry. Adding some humidity could help the room feel nicer while sleeping.")
        else:
            tips.append("Humidity stayed in a balanced range, which is good for overnight comfort.")

    b_mean = bpm_stats.get("mean")
    b_max = bpm_stats.get("max")
    if b_mean is not None:
        if b_mean > 85:
            tips.append("Your average heart rate was on the higher side. A calmer bedtime routine might help you wind down.")
        elif b_mean < 50:
            tips.append("Your heart rate stayed quite low overall. If that is normal for you, it may simply reflect deep rest.")
        else:
            tips.append("Your average heart rate stayed in a stable range through the night.")
    elif b_max is not None:
        tips.append("Heart-rate data was limited, but the report still captured part of your night.")

    if alarm_count > 3:
        tips.append("There were several alarm activations. Reducing sleep interruptions could help you wake up feeling better.")
    elif alarm_count > 0:
        tips.append("There were a few alarm activations. A steadier night routine may help reduce interruptions.")
    else:
        tips.append("No alarm activations were recorded during this sleep window.")

    return tips[:4]


# =============================== CherryPy Service ==============================

class ReportsGenerator:
    exposed = True  # para MethodDispatcher

    def __init__(self, settings: Dict[str, Any]):
        self.settings = settings
        self.catalog_url = settings.get("catalogURL", "")
        self.reports_url = settings.get("reportsURL", "")
        self.ts_base = settings.get("thingspeakURL", "")
        
        svc = settings.get("serviceInfo", {})
        self.service_id = svc.get("serviceID", "ReportsGenerator")
        self.rest_endpoint = svc.get("REST_endpoint", "")
        self.mqtt_sub = svc.get("MQTT_sub", [])
        self.mqtt_pub = svc.get("MQTT_pub", [])

        # Catalog client
        self.catalog = CatalogClient(self.catalog_url, ttl=5)
        
        # Upsert service
        try:
            self.catalog.upsert_service({
                "serviceID": self.service_id,
                "REST_endpoint": self.rest_endpoint,
                "MQTT_sub": self.mqtt_sub,
                "MQTT_pub": self.mqtt_pub,
            })
        except Exception as e:
            print(f"[ReportsGenerator] WARN: cannot upsert service: {e}")

        # TS field names from settings
        fields = settings.get("fields", {})
        self.f_bpm = fields.get("TS_BPM_FIELD", "field3")
        self.f_temp = fields.get("TS_TEMP_FIELD", "field1")
        self.f_hum  = fields.get("TS_HUM_FIELD",  "field2")
        self.f_alarm = fields.get("TS_ALARM_FIELD", "field8") # Renamed from LED

        # Padding for TS query (clipped later)
        self.pad_min = int(os.environ.get("TS_PAD_MIN", "5"))

    @cherrypy.tools.json_out()
    def GET(self, user_id: Optional[str] = None, room_id: Optional[str] = None, date: Optional[str] = None):
        # Heartbeat to catalog
        try:
            self.catalog.heartbeat_service(self.service_id)
        except Exception:
            pass

        if not user_id or not room_id:
            raise cherrypy.HTTPError(400, "user_id and room_id are required query params")

        # Reference date (today Rome or provided)
        ref_date = now_rome().date() if not date else dateparser.parse(date).date()
        today_dt = datetime(ref_date.year, ref_date.month, ref_date.day, tzinfo=tz.gettz("Europe/Rome"))

        # 1) Fetch room from catalog; validate it belongs to user_id
        try:
            room_obj = self.catalog.get_room(room_id)
            if not room_obj:
                raise cherrypy.HTTPError(404, f"Room '{room_id}' not found in catalog")
            if room_obj.get("userID") != user_id:
                raise cherrypy.HTTPError(404, f"Room '{room_id}' does not belong to user '{user_id}'")
        except cherrypy.HTTPError:
            raise
        except Exception as e:
             raise cherrypy.HTTPError(502, f"Catalog error: {e}")

        room_name = room_obj.get("roomName") or room_id

        # 2) Sleep/wake -> local window
        t = extract_times(room_obj)
        start_dt, end_dt = window_for_date(t["timesleep"], t["timeawake"], today_dt)

        # 3) TS credentials from catalog (per room)
        ts_info = extract_thingspeak(room_obj)
        channel_id = ts_info.get("channel")
        keys: List[str] = ts_info.get("keys") or []

        if not channel_id:
             raise cherrypy.HTTPError(400, f"Room '{room_id}' has no ThingSpeak channel configured")

        # 4) Fetch ThingSpeak with padding and robust strategy
        start_q = start_dt - timedelta(minutes=self.pad_min)
        end_q   = end_dt + timedelta(minutes=self.pad_min)

        try:
            df, dbg = fetch_ts_robusto(self.ts_base, channel_id, keys, start_q, end_q)
        except requests.RequestException as e:
            raise cherrypy.HTTPError(502, f"ThingSpeak error: {e}")

        # 5) Field selection and final clip to exact window
        if df.empty:
            return {
                "status": 200,
                "user_id": user_id,
                "room_id": room_id,
                "room_name": room_name,
                "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
                "message": "No data in the specified window",
                "metrics": {},
                "debug": dbg
            }

        df = pick_fields(df, self.f_bpm, self.f_temp, self.f_hum, self.f_alarm)
        df = df[(df["created_at"] >= start_dt) & (df["created_at"] < end_dt)].copy()

        if df.empty:
            return {
                "status": 200,
                "user_id": user_id,
                "room_id": room_id,
                "room_name": room_name,
                "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
                "message": "No data after window clipping",
                "metrics": {},
                "debug": dbg
            }

        # 6) Metrics
        bpm_stats = basic_stats(df[self.f_bpm]) if self.f_bpm in df.columns else {"mean": None, "min": None, "max": None}
        temp_stats = basic_stats(df[self.f_temp]) if self.f_temp in df.columns else {"mean": None, "min": None, "max": None}
        hum_stats = basic_stats(df[self.f_hum]) if self.f_hum in df.columns else {"mean": None, "min": None, "max": None}
        alarm_count = count_led_activations(df[self.f_alarm]) if self.f_alarm in df.columns else 0

        bpm_data = df[self.f_bpm].dropna().tolist() if self.f_bpm in df.columns else []
        temp_data = df[self.f_temp].dropna().tolist() if self.f_temp in df.columns else []
        hum_data = df[self.f_hum].dropna().tolist() if self.f_hum in df.columns else []

        stages = infer_sleep_stages_from_bpm(df, self.f_bpm)
        duration_hours = max((end_dt - start_dt).total_seconds() / 3600.0, 1e-3)
        quality = sleep_quality(temp_stats, hum_stats, bpm_stats, alarm_count, duration_hours)
        tips = build_user_tips(temp_stats, hum_stats, bpm_stats, alarm_count)

        return {
            "status": 200,
            "user_id": user_id,
            "room_id": room_id,
            "room_name": room_name,
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "stats": {
                "bpm": {
                    "mean": bpm_stats.get("mean"),
                    "min": bpm_stats.get("min"),
                    "max": bpm_stats.get("max"),
                    "data": bpm_data
                },
                "temperature": {
                    "mean": temp_stats.get("mean"),
                    "min": temp_stats.get("min"),
                    "max": temp_stats.get("max"),
                    "data": temp_data
                },
                "humidity": {
                    "mean": hum_stats.get("mean"),
                    "min": hum_stats.get("min"),
                    "max": hum_stats.get("max"),
                    "data": hum_data
                },
                "alarm_activations": alarm_count
            },
            "stages_hours": stages,
            "sleep_quality": quality,
            "tips": tips
            }
# =================================== Arranque ===================================

if __name__ == "__main__":
    settings_file_path = os.path.join(os.path.dirname(__file__), 'settings.json')
    try:
        with open(settings_file_path, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Error reading settings: {e}")
        raise SystemExit(1)

    web_service = ReportsGenerator(settings)

    conf = {
        '/': {
            'request.dispatch': cherrypy.dispatch.MethodDispatcher(),
            'tools.sessions.on': True
        }
    }

    cherrypy.tree.mount(web_service, '/', conf)
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': 8093,
        'engine.autoreload.on': False
    })
    cherrypy.engine.start()
    cherrypy.engine.block()
