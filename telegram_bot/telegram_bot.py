#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Set, List

import requests
from paho.mqtt.client import Client as MqttClient, MQTTMessage
from common.catalog_client import CatalogClient

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - telegrambot - %(levelname)s - %(message)s",
)
log = logging.getLogger("telegrambot")

# ---------------- Settings ----------------
@dataclass
class BotSettings:
    catalog_url: str
    broker_ip: str
    broker_port: int
    service_id: str
    telegram_token: str
    mqtt_subs: List[str]
    nodered_url: str

    @classmethod
    def load(cls, path: str = "settings.json") -> "BotSettings":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        si = data["serviceInfo"]
        base = data["catalogURL"].rstrip("/")
        return cls(
            catalog_url=base,
            broker_ip=data["brokerIP"],
            broker_port=int(data["brokerPort"]),
            service_id=si["serviceID"],
            telegram_token=si["telegram_token"],
            mqtt_subs=list(si.get("MQTT_sub", [])),
            nodered_url=data.get("nodered_url", "http://localhost:1880/ui/"),
        )

# ---------------- Conversation states ----------------
(
    ASK_PHONE, ASK_PASSWORD,
    MAIN_MENU, ROOMS_LIST, ROOM_MENU,
    CFG_MENU,
    CFG_TIME_AWAKE, CFG_TIME_SLEEP,
    CFG_HR_LOW, CFG_HR_HIGH,
    CFG_TEMP_LOW, CFG_TEMP_HIGH, CFG_HUM_LOW, CFG_HUM_HIGH,
    CFG_POT_LEVEL,
    CONFIRM_DELETE_ROOM, CONFIRM_DELETE_ACCOUNT,
    # Account registration
    REG_CONFIRM, REG_USERNAME, REG_PASSWORD, REG_PASSWORD_CONFIRM,
    # Per-room wizard (used both at registration and from "Add new room")
    RW_NAME,
    RW_TIMEAWAKE, RW_TIMESLEEP,
    RW_HR_LOW, RW_HR_HIGH,
    RW_TEMP_LOW, RW_TEMP_HIGH,
    RW_HUM_LOW, RW_HUM_HIGH,
    RW_POT_LEVEL,
    RW_ADD_ANOTHER,
) = range(32)

# ---------------- Light sensitivity mapping ----------------
# The ADC range is fixed 0..4095; the user only picks the brightness threshold
# above which curtains should open (and LED turns off at wakeup).
LIGHT_LEVEL_TO_THRESHOLD = {
    "low":    1200,
    "medium": 2400,
    "high":   3000,
}
LIGHT_LEVEL_LABELS = {
    "low":    "🌙 Low",
    "medium": "🌤️ Medium",
    "high":   "☀️ High",
}
THRESHOLD_TO_LIGHT_LEVEL = {v: k for k, v in LIGHT_LEVEL_TO_THRESHOLD.items()}

def light_level_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(LIGHT_LEVEL_LABELS["low"],    callback_data=f"{prefix}:low")],
        [InlineKeyboardButton(LIGHT_LEVEL_LABELS["medium"], callback_data=f"{prefix}:medium")],
        [InlineKeyboardButton(LIGHT_LEVEL_LABELS["high"],   callback_data=f"{prefix}:high")],
    ])

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 My rooms",        callback_data="main:rooms")],
        [InlineKeyboardButton("➕ Add new room",    callback_data="main:add_room")],
        [InlineKeyboardButton("📊 Show dashboard",  callback_data="main:dash")],
        [InlineKeyboardButton("🗑 Delete account",  callback_data="main:del_acc")],
        [InlineKeyboardButton("🚪 Exit",            callback_data="main:exit")],
    ])

def room_menu_kb(room_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 View devices",    callback_data=f"room:devices:{room_id}")],
        [InlineKeyboardButton("📊 View data",       callback_data=f"room:data:{room_id}")],
        [InlineKeyboardButton("⚙️ Configure room",  callback_data=f"room:cfg:{room_id}")],
        [InlineKeyboardButton("🗑 Delete this room", callback_data=f"room:del:{room_id}")],
        [InlineKeyboardButton("⬅️ Back",             callback_data="room:back")],
    ])

def room_devices_kb(room_id: str, connected_devices: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for dev in connected_devices:
        role = str(dev.get("role") or dev.get("deviceID") or "Device")
        rows.append([InlineKeyboardButton(role, callback_data=f"room:devinfo:{room_id}:{role}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"room:show:{room_id}")])
    return InlineKeyboardMarkup(rows)

DEVICE_ROLE_SUMMARY = {
    "HealthMonitorPPG": "This device watches your heart rhythm during sleep and also handles the wake-up alarm.",
    "EnvMonitorDHT": "This device watches the room environment, like temperature and humidity, to help keep your sleep space comfortable.",
    "ActuatorControl": "This device manages the light and the curtain to help the room adapt to your sleep routine.",
}

def cfg_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ Wake/Sleep time",        callback_data="cfg:times")],
        [InlineKeyboardButton("❤️ Heart-rate min-max",     callback_data="cfg:hr")],
        [InlineKeyboardButton("🌡️ Temp/Humidity min-max", callback_data="cfg:thr")],
        [InlineKeyboardButton("💡 Light threshold",        callback_data="cfg:pot")],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="cfg:back"),
            InlineKeyboardButton("🚪 Exit",  callback_data="cfg:exit"),
        ],
    ])

def yes_no_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton("❌ No",  callback_data=f"{prefix}:no"),
    ]])

# ---------------- Utilities ----------------
PHONE_RE = re.compile(r"^\+?\d{7,15}$")
TIME_RE  = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
NUM_RE   = re.compile(r"^-?\d+(\.\d+)?$")

def ok_num(s: str) -> bool:
    return bool(NUM_RE.match((s or "").strip()))

def ok_time(s: str) -> bool:
    return bool(TIME_RE.match((s or "").strip()))

# ---------------- TelegramBot Service ----------------
class TelegramBotService:
    def __init__(self, settings: BotSettings):
        self.S = settings
        self.cat = CatalogClient(url=self.S.catalog_url)
        self.session_by_chat: Dict[int, str] = {}
        self.chats_by_user: Dict[str, Set[int]] = {}
        self.tmp: Dict[int, Dict[str, Any]] = {}
        self.application = None  # type: ignore
        self.cat.upsert_service({
            "serviceID": self.S.service_id,
            "REST_endpoint": "",
            "MQTT_sub": self.S.mqtt_subs,
            "MQTT_pub": [],
        })
        self.mqtt_pub = MqttClient(client_id=f"telegram-pub-{self.S.service_id}", clean_session=True)
        self.mqtt_pub.connect(self.S.broker_ip, self.S.broker_port, keepalive=30)
        self.mqtt_pub.loop_start()

    # ---------------- Session helpers ----------------
    def _open_session(self, chat_id: int, user_id: str):
        self.session_by_chat[chat_id] = user_id
        self.chats_by_user.setdefault(user_id, set()).add(chat_id)

    def _close_session(self, chat_id: int):
        uid = self.session_by_chat.pop(chat_id, None)
        if uid:
            self.chats_by_user.get(uid, set()).discard(chat_id)

    def _drop_user_sessions(self, user_id: str):
        for cid in list(self.chats_by_user.get(user_id, set())):
            self.session_by_chat.pop(cid, None)
        self.chats_by_user.pop(user_id, None)

    def _reg_state(self, chat_id: int) -> Dict[str, Any]:
        return self.tmp.setdefault(chat_id, {}).setdefault("reg", {})

    def _room_state(self, chat_id: int) -> Dict[str, Any]:
        return self.tmp.setdefault(chat_id, {}).setdefault("rw", {})

    @staticmethod
    def _hash_password(salt: str, password: str) -> str:
        return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

    # ---------------- /start & login ----------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.tmp.pop(chat_id, None)
        await update.message.reply_text(
            "👋 Hi! I'm your sleep monitoring assistant.\n\n"
            "Please verify your identity by sending your *phone number* "
            "(international format, e.g. `+573001112233`).",
            parse_mode="Markdown",
        )
        return ASK_PHONE

    async def ask_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        phone = (update.message.text or "").strip()
        if not PHONE_RE.match(phone):
            await update.message.reply_text("❌ Invalid phone format. Try `+573001112233`.")
            return ASK_PHONE

        try:
            user = self.cat.find_user_by_phone(phone)
        except Exception:
            log.exception("Catalog error on find_user_by_phone")
            await update.message.reply_text("⚠️ Catalog lookup error. Try again later.")
            return ASK_PHONE

        chat_id = update.effective_chat.id

        if not user:
            self.tmp[chat_id] = {"reg": {"phone": phone}}
            await update.message.reply_text(
                "❌ Phone not found.\n\nWould you like to register in the system?",
                reply_markup=yes_no_kb("reg_confirm"),
            )
            return REG_CONFIRM

        user_id = user.get("userID")
        uname = user.get("user_information", {}).get("userName", user_id)
        self.tmp[chat_id] = {"user_id": user_id, "user_obj": user}
        await update.message.reply_text(
            f"📞 Found *{uname}* (`{user_id}`).\n\n"
            "Please send your *password* to continue.\n"
            "_(Your message will be deleted automatically for security.)_",
            parse_mode="Markdown",
        )
        return ASK_PASSWORD

    async def ask_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        pending = self.tmp.get(chat_id) or {}
        user_id = pending.get("user_id")
        if not user_id:
            await update.message.reply_text("⚠️ No pending user. /start again.")
            return ASK_PHONE

        pwd = (update.message.text or "").strip()
        if not pwd:
            await update.message.reply_text("❌ Password cannot be empty.")
            return ASK_PASSWORD

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

        try:
            user = self.cat.get_user(user_id) or pending.get("user_obj") or {}
        except Exception:
            log.exception("get_user for password")
            await update.message.reply_text("⚠️ Catalog error.")
            return ASK_PASSWORD

        auth = user.get("auth") or {}
        salt = auth.get("password_salt")
        stored_hash = auth.get("password_hash")
        if not salt or not stored_hash:
            await update.message.reply_text("⚠️ No password configured. Contact admin.")
            return ASK_PASSWORD
        if self._hash_password(salt, pwd) != stored_hash:
            await update.message.reply_text("❌ Incorrect password. Try again.")
            return ASK_PASSWORD

        self._open_session(chat_id, user_id)
        uname = user.get("user_information", {}).get("userName", user_id)
        await update.message.reply_text(
            f"✅ Logged in as *{uname}* (`{user_id}`). Choose an option:",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return MAIN_MENU

    # ---------------- Main menu ----------------
    async def main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return MAIN_MENU
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]
        chat_id = query.message.chat.id
        user_id = self.session_by_chat.get(chat_id)

        if choice == "exit":
            self._close_session(chat_id)
            await query.edit_message_text("👋 Bye. Use /start anytime.")
            return ConversationHandler.END

        if not user_id:
            await query.edit_message_text("⚠️ Session not verified. Use /start.")
            return ASK_PHONE

        if choice == "rooms":
            return await self._render_rooms_list(chat_id, query, context)

        if choice == "add_room":
            self.tmp.setdefault(chat_id, {})["rw"] = {"mode": "add"}
            await query.edit_message_text(
                "🏠 Let's add a new room.\n\nSend a *name* for the room (e.g. `Bedroom`).",
                parse_mode="Markdown",
            )
            return RW_NAME

        if choice == "dash":
            rooms = self.cat.rooms_for_user(user_id)
            if not rooms:
                await query.edit_message_text("⚠️ You have no rooms yet.")
            else:
                lines = ["📊 *Your dashboards:*"]
                for r in rooms:
                    rid = r.get("roomID")
                    rname = r.get("roomName", rid)
                    ch = (r.get("thingspeak_info") or {}).get("channel_id")
                    if ch:
                        lines.append(f"• *{rname}* → https://thingspeak.com/channels/{ch}")
                    else:
                        lines.append(f"• *{rname}* → (no channel)")
                await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                              disable_web_page_preview=True)
            await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU

        if choice == "del_acc":
            await query.edit_message_text(
                "⚠️ Are you sure you want to *delete your account*?\n"
                "This removes every room, device and ThingSpeak slot.",
                parse_mode="Markdown",
                reply_markup=yes_no_kb("confirm_del_acc"),
            )
            return CONFIRM_DELETE_ACCOUNT

        await query.edit_message_text("Main menu:", reply_markup=main_menu_kb())
        return MAIN_MENU

    async def _render_rooms_list(self, chat_id: int, query, context: ContextTypes.DEFAULT_TYPE):
        user_id = self.session_by_chat.get(chat_id)
        try:
            rooms = self.cat.rooms_for_user(user_id) if user_id else []
        except Exception:
            log.exception("rooms_for_user")
            rooms = []
        if not rooms:
            await query.edit_message_text(
                "You have no rooms yet. Use ➕ *Add new room* from the main menu.",
                parse_mode="Markdown",
            )
            await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU
        buttons = [
            [InlineKeyboardButton(r.get("roomName") or r.get("roomID"),
                                  callback_data=f"rooms:pick:{r.get('roomID')}")]
            for r in rooms
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="rooms:back")])
        await query.edit_message_text("🏠 *Your rooms*", parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return ROOMS_LIST

    async def rooms_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = (query.data or "").split(":")
        chat_id = query.message.chat.id
        if len(data) >= 2 and data[1] == "back":
            await query.edit_message_text("Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU
        if len(data) == 3 and data[1] == "pick":
            room_id = data[2]
            self.tmp.setdefault(chat_id, {})["selected_room_id"] = room_id
            r = self.cat.get_room(room_id) or {}
            await query.edit_message_text(
                self._room_text(r, room_id), parse_mode="Markdown",
                reply_markup=room_menu_kb(room_id),
            )
            return ROOM_MENU
        return ROOMS_LIST

    async def room_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = (query.data or "").split(":")
        chat_id = query.message.chat.id
        if len(data) >= 2 and data[1] == "back":
            return await self._render_rooms_list(chat_id, query, context)
        if len(data) == 3 and data[1] == "show":
            room_id = data[2]
            self.tmp.setdefault(chat_id, {})["selected_room_id"] = room_id
            r = self.cat.get_room(room_id) or {}
            await query.edit_message_text(
                self._room_text(r, room_id),
                parse_mode="Markdown",
                reply_markup=room_menu_kb(room_id),
            )
            return ROOM_MENU
        if len(data) == 3 and data[1] == "devices":
            room_id = data[2]
            self.tmp.setdefault(chat_id, {})["selected_room_id"] = room_id
            r = self.cat.get_room(room_id) or {}
            devices = self._connected_devices(r)
            await query.edit_message_text(
                self._room_devices_text(r, room_id),
                parse_mode="Markdown",
                reply_markup=room_devices_kb(room_id, devices),
            )
            return ROOM_MENU
        if len(data) >= 4 and data[1] == "devinfo":
            room_id = data[2]
            role = ":".join(data[3:])
            summary = self._device_role_summary(role)
            await query.edit_message_text(
                f"📱 *{role}*\n\n{summary}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back to devices", callback_data=f"room:devices:{room_id}")
                ]]),
            )
            return ROOM_MENU
        if len(data) == 3 and data[1] == "data":
            room_id = data[2]
            r = self.cat.get_room(room_id) or {}
            await query.edit_message_text(
                self._room_data_text(r, room_id),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data=f"room:show:{room_id}")
                ]]),
            )
            return ROOM_MENU
        if len(data) == 3 and data[1] == "cfg":
            self.tmp.setdefault(chat_id, {})["selected_room_id"] = data[2]
            await query.edit_message_text("⚙️ *Configuration*", parse_mode="Markdown",
                                          reply_markup=cfg_menu_kb())
            return CFG_MENU
        if len(data) == 3 and data[1] == "del":
            self.tmp.setdefault(chat_id, {})["pending_del_room"] = data[2]
            r = self.cat.get_room(data[2]) or {}
            name = r.get("roomName", data[2])
            await query.edit_message_text(
                f"⚠️ Delete room *{name}*?\nThis removes its 3 devices and releases the ThingSpeak slot.",
                parse_mode="Markdown",
                reply_markup=yes_no_kb("confirm_del_room"),
            )
            return CONFIRM_DELETE_ROOM
        return ROOM_MENU

    # ---------------- Delete confirmations ----------------
    async def confirm_delete_room(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]
        chat_id = query.message.chat.id
        room_id = (self.tmp.get(chat_id) or {}).pop("pending_del_room", None)
        if choice != "yes":
            await query.edit_message_text("🚫 Cancelled.")
        elif not room_id:
            await query.edit_message_text("⚠️ Nothing to delete.")
        else:
            try:
                self.cat.delete_room(room_id)
                await query.edit_message_text(f"🗑 Room `{room_id}` deleted.", parse_mode="Markdown")
            except Exception:
                log.exception("delete_room")
                await query.edit_message_text("⚠️ Error deleting room.")
        await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
        return MAIN_MENU

    async def confirm_delete_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]
        chat_id = query.message.chat.id
        user_id = self.session_by_chat.get(chat_id)
        if choice != "yes":
            await query.edit_message_text("🚫 Cancelled.")
            await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU
        if not user_id:
            await query.edit_message_text("⚠️ No session.")
            return ConversationHandler.END
        try:
            self.cat.delete_user(user_id)
            self._drop_user_sessions(user_id)
            self.tmp.pop(chat_id, None)
            await query.edit_message_text("👋 Account deleted. Goodbye.")
            return ConversationHandler.END
        except Exception:
            log.exception("delete_user")
            await query.edit_message_text("⚠️ Error deleting account.")
            await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU

    # ---------------- Config menu (scope = selected_room_id) ----------------
    async def cfg_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]

        if choice == "times":
            await query.edit_message_text("⏰ Send *wake-up time* in HH:MM (24h).", parse_mode="Markdown")
            return CFG_TIME_AWAKE
        if choice == "hr":
            await query.edit_message_text("❤️ Send *minimum heart rate (bpm)*:", parse_mode="Markdown")
            return CFG_HR_LOW
        if choice == "thr":
            await query.edit_message_text("🌡️ Send *minimum temperature (°C)*:", parse_mode="Markdown")
            return CFG_TEMP_LOW
        if choice == "pot":
            await query.edit_message_text(
                "💡 *Light sensitivity*\n\nHow bright should it be for the curtains to open?",
                parse_mode="Markdown",
                reply_markup=light_level_kb("cfg_pot"),
            )
            return CFG_POT_LEVEL
        if choice == "back":
            await query.edit_message_text("Main menu:", reply_markup=main_menu_kb())
            return MAIN_MENU
        if choice == "exit":
            chat_id = query.message.chat.id
            self._close_session(chat_id)
            await query.edit_message_text("👋 Bye. Use /start anytime.")
            return ConversationHandler.END
        await query.edit_message_text("⚙️ *Configuration*", parse_mode="Markdown", reply_markup=cfg_menu_kb())
        return CFG_MENU

    def _selected_room(self, chat_id: int) -> Optional[str]:
        return (self.tmp.get(chat_id) or {}).get("selected_room_id")

    @staticmethod
    def _room_title(room_obj: Dict[str, Any], room_id: str) -> str:
        return room_obj.get("roomName") or room_id

    @staticmethod
    def _connected_devices(room_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        return list(room_obj.get("connected_devices") or [])

    def _room_text(self, room_obj: Dict[str, Any], room_id: str) -> str:
        name = self._room_title(room_obj, room_id)
        return f"🏠 *{name}* (`{room_id}`)"

    def _room_devices_text(self, room_obj: Dict[str, Any], room_id: str) -> str:
        name = self._room_title(room_obj, room_id)
        devices = self._connected_devices(room_obj)
        lines = [f"📱 *Devices in {name}* (`{room_id}`)", ""]
        if not devices:
            lines.append("No devices are registered for this room yet.")
        else:
            for dev in devices:
                role = dev.get("role") or "Device"
                device_id = dev.get("deviceID") or "-"
                lines.append(f"`{role}`: `{device_id}`")
            lines.append("")
            lines.append("Tap a device name to see what it does.")
        return "\n".join(lines)

    @staticmethod
    def _device_role_summary(role: str) -> str:
        return DEVICE_ROLE_SUMMARY.get(role, "This device helps your room follow your sleep routine.")

    def _room_data_text(self, room_obj: Dict[str, Any], room_id: str) -> str:
        name = self._room_title(room_obj, room_id)
        times = room_obj.get("times") or {}
        thr = room_obj.get("threshold_parameters") or {}
        lt = thr.get("light_threshold")
        level = THRESHOLD_TO_LIGHT_LEVEL.get(lt)
        light_label = LIGHT_LEVEL_LABELS.get(level, str(lt)) if lt is not None else "N/A"

        def fmt(v):
            if v is None:
                return "N/A"
            return str(int(v)) if isinstance(v, float) and v == int(v) else str(v)

        lines = [
            f"📊 *Room data — {name}*",
            "",
            f"⏰ Wake-up: `{times.get('timeawake', 'N/A')}`",
            f"🌙 Sleep: `{times.get('timesleep', 'N/A')}`",
            "",
            f"❤️ Heart rate: `{fmt(thr.get('hr_low'))}` – `{fmt(thr.get('hr_high'))}` bpm",
            f"🌡️ Temperature: `{fmt(thr.get('temp_low'))}` – `{fmt(thr.get('temp_high'))}` °C",
            f"💧 Humidity: `{fmt(thr.get('hum_low'))}` – `{fmt(thr.get('hum_high'))}` %",
            f"💡 Light sensitivity: {light_label}",
        ]
        return "\n".join(lines)

    async def set_time_awake(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_time(s):
            await update.message.reply_text("❌ Invalid time. Example: `06:30`")
            return CFG_TIME_AWAKE
        self.tmp.setdefault(chat_id, {})["timeawake"] = s
        await update.message.reply_text("Now send *sleep time* (HH:MM).", parse_mode="Markdown")
        return CFG_TIME_SLEEP

    async def set_time_sleep(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_time(s):
            await update.message.reply_text("❌ Invalid time. Example: `22:45`")
            return CFG_TIME_SLEEP

        user_id = self.session_by_chat.get(chat_id)
        room_id = self._selected_room(chat_id)
        if not user_id or not room_id:
            await update.message.reply_text("⚠️ Session or room missing. /start again.")
            return ASK_PHONE

        vals = self.tmp.setdefault(chat_id, {})
        vals["timesleep"] = s

        try:
            self.cat.patch_room(room_id, {"times": {
                "timeawake": vals["timeawake"], "timesleep": vals["timesleep"]
            }})
            topic = f"SC/{user_id}/{room_id}/initTimeshift"
            payload = {"timeawake": vals["timeawake"], "timesleep": vals["timesleep"]}
            try:
                self.mqtt_pub.publish(topic, json.dumps(payload), qos=1, retain=False)
                log.info("MQTT PUB initTimeshift %s -> %s", topic, payload)
            except Exception:
                log.exception("MQTT publish initTimeshift failed")
            await update.message.reply_text("✅ Room times updated.", reply_markup=cfg_menu_kb())
        except Exception:
            log.exception("patch_room times")
            await update.message.reply_text("⚠️ Error saving to Catalog.")
        return CFG_MENU

    async def set_hr_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 50")
            return CFG_HR_LOW
        self.tmp.setdefault(chat_id, {})["hr_low"] = float(s)
        await update.message.reply_text("Now send *maximum heart rate (bpm)*:", parse_mode="Markdown")
        return CFG_HR_HIGH

    async def set_hr_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 100")
            return CFG_HR_HIGH

        vals = self.tmp.setdefault(chat_id, {})
        hi = float(s)
        if hi <= vals.get("hr_low", -1e9):
            await update.message.reply_text("❌ Max HR must be greater than min.")
            return CFG_HR_HIGH
        vals["hr_high"] = hi

        room_id = self._selected_room(chat_id)
        if not room_id:
            await update.message.reply_text("⚠️ No room selected.")
            return MAIN_MENU

        try:
            current = self.cat.room_thresholds(room_id) or {}
            current.update({"hr_low": vals["hr_low"], "hr_high": vals["hr_high"]})
            self.cat.patch_room(room_id, {"threshold_parameters": current})
            await update.message.reply_text("✅ Heart-rate thresholds updated.", reply_markup=cfg_menu_kb())
        except Exception:
            log.exception("patch_room hr thresholds")
            await update.message.reply_text("⚠️ Error saving to Catalog.")
        return CFG_MENU

    async def set_temp_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 18.0")
            return CFG_TEMP_LOW
        self.tmp.setdefault(chat_id, {})["temp_low"] = float(s)
        await update.message.reply_text("Now send *maximum temperature (°C)*:")
        return CFG_TEMP_HIGH

    async def set_temp_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 25.0")
            return CFG_TEMP_HIGH
        self.tmp.setdefault(chat_id, {})["temp_high"] = float(s)
        await update.message.reply_text("Now send *minimum humidity (%)*:")
        return CFG_HUM_LOW

    async def set_hum_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 35")
            return CFG_HUM_LOW
        self.tmp.setdefault(chat_id, {})["hum_low"] = float(s)
        await update.message.reply_text("Finally, send *maximum humidity (%)*:")
        return CFG_HUM_HIGH

    async def set_hum_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number. Example: 60")
            return CFG_HUM_HIGH

        vals = self.tmp.setdefault(chat_id, {})
        vals["hum_high"] = float(s)

        room_id = self._selected_room(chat_id)
        if not room_id:
            await update.message.reply_text("⚠️ No room selected.")
            return MAIN_MENU

        try:
            current = self.cat.room_thresholds(room_id) or {}
            current.update({
                "temp_low":  vals["temp_low"],
                "temp_high": vals["temp_high"],
                "hum_low":   vals["hum_low"],
                "hum_high":  vals["hum_high"],
            })
            self.cat.patch_room(room_id, {"threshold_parameters": current})
            await update.message.reply_text("✅ Room thresholds updated.", reply_markup=cfg_menu_kb())
        except Exception:
            log.exception("patch_room thresholds")
            await update.message.reply_text("⚠️ Error saving to Catalog.")
        return CFG_MENU

    async def cfg_pot_level_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        level = (query.data or "").split(":", 1)[-1]
        if level not in LIGHT_LEVEL_TO_THRESHOLD:
            return CFG_POT_LEVEL

        chat_id = query.message.chat.id
        room_id = self._selected_room(chat_id)
        if not room_id:
            await query.edit_message_text("⚠️ No room selected.")
            return MAIN_MENU

        light_threshold = LIGHT_LEVEL_TO_THRESHOLD[level]
        label = LIGHT_LEVEL_LABELS[level]
        try:
            thr = self.cat.room_thresholds(room_id) or {}
            thr.pop("pot_min", None)
            thr.pop("pot_max", None)
            thr["light_threshold"] = light_threshold
            self.cat.patch_room(room_id, {"threshold_parameters": thr})
            await query.edit_message_text(
                f"✅ Light sensitivity updated to *{label}* (threshold: {light_threshold}).",
                parse_mode="Markdown",
            )
        except Exception:
            log.exception("patch_room pot level")
            await query.edit_message_text("⚠️ Error saving to Catalog.")

        await context.bot.send_message(
            chat_id=chat_id, text="⚙️ *Configuration*",
            parse_mode="Markdown", reply_markup=cfg_menu_kb(),
        )
        return CFG_MENU

    # ===================== REGISTRATION =====================
    async def reg_confirm_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]
        chat_id = query.message.chat.id
        if choice == "no":
            await query.edit_message_text("👍 Ok, cancelled. /start anytime.")
            self.tmp.pop(chat_id, None)
            return ConversationHandler.END
        await query.edit_message_text(
            "🌙 *Welcome to Sleep Monitoring!*\n\n"
            "Our platform monitors your sleep parameters — heart rate, "
            "room temperature, humidity and ambient light — to help you "
            "get a better night's rest.\n\n"
            "We automate your room environment at bedtime and wake-up, "
            "generate personalised sleep reports, and send you real-time "
            "alerts when something needs attention.\n\n"
            "Let's get you set up!\n\n"
            "Send your desired *userName* (2-32 letters/digits/underscore).",
            parse_mode="Markdown",
        )
        return REG_USERNAME

    @staticmethod
    def _username_exists(users: list, name: str) -> bool:
        name_l = name.strip().lower()
        for u in users:
            info = u.get("user_information", {}) or {}
            if str(info.get("userName", "")).strip().lower() == name_l:
                return True
        return False

    async def reg_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        name = (update.message.text or "").strip()
        if not re.match(r"^[A-Za-z0-9_]{2,32}$", name):
            await update.message.reply_text("❌ Invalid userName. 2-32 letters/digits/underscore.")
            return REG_USERNAME
        try:
            users = self.cat.get_users()
        except Exception:
            log.exception("get_users on reg_username")
            await update.message.reply_text("⚠️ Catalog error.")
            return REG_USERNAME
        if self._username_exists(users, name):
            await update.message.reply_text("❌ That userName already exists.")
            return REG_USERNAME

        self._reg_state(chat_id)["userName"] = name
        await update.message.reply_text(
            "🔐 Now send a *password* for your account.\n"
            "_(Your message will be deleted automatically for security.)_",
            parse_mode="Markdown",
        )
        return REG_PASSWORD

    async def reg_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        pwd = (update.message.text or "").strip()
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass
        if len(pwd) < 4:
            await context.bot.send_message(chat_id=chat_id, text="❌ Password too short (min 4).")
            return REG_PASSWORD
        self._reg_state(chat_id)["_pwd1"] = pwd
        await context.bot.send_message(chat_id=chat_id,
            text="🔐 *Confirm* the password by sending it again.\n"
            "_(Your message will be deleted automatically.)_", parse_mode="Markdown")
        return REG_PASSWORD_CONFIRM

    async def reg_password_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        pwd2 = (update.message.text or "").strip()
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass
        reg = self._reg_state(chat_id)
        pwd1 = reg.pop("_pwd1", None)
        if not pwd1 or pwd1 != pwd2:
            await context.bot.send_message(chat_id=chat_id, text="❌ Passwords don't match. Send again.")
            return REG_PASSWORD
        salt = secrets.token_hex(8)
        reg["password_salt"] = salt
        reg["password_hash"] = self._hash_password(salt, pwd1)

        # Create the user now so rooms can reference userID.
        user_payload = {
            "role": "User",
            "user_information": {
                "userName": reg["userName"],
                "phone":    reg["phone"],
            },
            "dashboard_info": {
                "dashboard_username": reg["userName"],
                "dashboard_password": None,
            },
            "auth": {
                "password_salt": reg["password_salt"],
                "password_hash": reg["password_hash"],
            },
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        try:
            created = self.cat.create_user(user_payload)
            new_user_id = created.get("userID")
            if not new_user_id:
                raise RuntimeError("catalog did not return userID")
        except Exception:
            log.exception("create_user failed")
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Error creating account. /start again.")
            self.tmp.pop(chat_id, None)
            return ConversationHandler.END

        reg["user_id"] = new_user_id
        self._open_session(chat_id, new_user_id)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"✅ Account created (`{new_user_id}`).\n\n"
                  "Now let's register your first *room*.\nSend a *name* for the room (e.g. `Bedroom`)."),
            parse_mode="Markdown",
        )
        self.tmp[chat_id]["rw"] = {"mode": "reg"}
        return RW_NAME

    # ===================== ROOM WIZARD =====================
    async def rw_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        name = (update.message.text or "").strip()
        if not name or len(name) > 40:
            await update.message.reply_text("❌ Invalid name. 1-40 chars.")
            return RW_NAME
        self._room_state(chat_id)["roomName"] = name
        await update.message.reply_text(
            "⏰ Send *wake-up time* in HH:MM (24h). Example: `07:00`", parse_mode="Markdown")
        return RW_TIMEAWAKE

    async def rw_timeawake(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_time(s):
            await update.message.reply_text("❌ Invalid time. Example: `07:00`")
            return RW_TIMEAWAKE
        self._room_state(chat_id)["timeawake"] = s
        await update.message.reply_text("🌙 Send *sleep time* (HH:MM).", parse_mode="Markdown")
        return RW_TIMESLEEP

    async def rw_timesleep(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_time(s):
            await update.message.reply_text("❌ Invalid time.")
            return RW_TIMESLEEP
        self._room_state(chat_id)["timesleep"] = s
        await update.message.reply_text("❤️ Send *minimum heart rate* (bpm). Example: `45`")
        return RW_HR_LOW

    async def rw_hr_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_HR_LOW
        self._room_state(chat_id)["hr_low"] = float(s)
        await update.message.reply_text("❤️ Send *maximum heart rate* (bpm). Example: `110`")
        return RW_HR_HIGH

    async def rw_hr_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_HR_HIGH
        rw = self._room_state(chat_id)
        hi = float(s)
        if hi <= rw.get("hr_low", -1):
            await update.message.reply_text("❌ Max HR must be greater than min.")
            return RW_HR_HIGH
        rw["hr_high"] = hi
        await update.message.reply_text("🌡️ Send *minimum temperature* (°C). Example: `18`")
        return RW_TEMP_LOW

    async def rw_temp_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_TEMP_LOW
        self._room_state(chat_id)["temp_low"] = float(s)
        await update.message.reply_text("🌡️ Send *maximum temperature* (°C). Example: `26`")
        return RW_TEMP_HIGH

    async def rw_temp_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_TEMP_HIGH
        rw = self._room_state(chat_id)
        hi = float(s)
        if hi <= rw.get("temp_low", -1e9):
            await update.message.reply_text("❌ Max temp must be greater than min.")
            return RW_TEMP_HIGH
        rw["temp_high"] = hi
        await update.message.reply_text("💧 Send *minimum humidity* (%). Example: `35`")
        return RW_HUM_LOW

    async def rw_hum_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_HUM_LOW
        self._room_state(chat_id)["hum_low"] = float(s)
        await update.message.reply_text("💧 Send *maximum humidity* (%). Example: `65`")
        return RW_HUM_HIGH

    async def rw_hum_high(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = (update.message.text or "").strip()
        if not ok_num(s):
            await update.message.reply_text("❌ Invalid number.")
            return RW_HUM_HIGH
        rw = self._room_state(chat_id)
        hi = float(s)
        if hi <= rw.get("hum_low", -1e9):
            await update.message.reply_text("❌ Max humidity must be greater than min.")
            return RW_HUM_HIGH
        rw["hum_high"] = hi
        await update.message.reply_text(
            "💡 *Light sensitivity*\n\nHow bright should it be for the curtains to open?",
            parse_mode="Markdown",
            reply_markup=light_level_kb("rw_pot"),
        )
        return RW_POT_LEVEL

    async def rw_pot_level_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        level = (query.data or "").split(":", 1)[-1]
        if level not in LIGHT_LEVEL_TO_THRESHOLD:
            return RW_POT_LEVEL
        chat_id = query.message.chat.id
        rw = self._room_state(chat_id)
        rw["light_threshold"] = LIGHT_LEVEL_TO_THRESHOLD[level]

        # Commit the room to catalog now.
        user_id = self.session_by_chat.get(chat_id)
        if not user_id:
            await query.edit_message_text("⚠️ No session. /start.")
            return ConversationHandler.END

        payload = {
            "userID":   user_id,
            "roomName": rw["roomName"],
            "times": {"timeawake": rw["timeawake"], "timesleep": rw["timesleep"]},
            "threshold_parameters": {
                "hr_low":   rw["hr_low"],   "hr_high":   rw["hr_high"],
                "temp_low": rw["temp_low"], "temp_high": rw["temp_high"],
                "hum_low":  rw["hum_low"],  "hum_high":  rw["hum_high"],
                "light_threshold": rw["light_threshold"],
            },
        }
        try:
            created = self.cat.create_room(payload)
            new_room_id = created.get("roomID")
            ch = (created.get("thingspeak_info") or {}).get("channel_id")
            await query.edit_message_text(
                f"✅ Room *{rw['roomName']}* created (`{new_room_id}`).\n"
                f"ThingSpeak channel: `{ch or '—'}`",
                parse_mode="Markdown",
            )
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text if e.response is not None else ""
            log.exception("create_room HTTP %s: %s", code, body)
            if code == 409:
                await query.edit_message_text(
                    "⚠️ No ThingSpeak channels available. Ask the admin to add more.")
            else:
                await query.edit_message_text("⚠️ Error creating room.")
            return await self._after_room_wizard(chat_id, context, committed=False)
        except Exception:
            log.exception("create_room")
            await query.edit_message_text("⚠️ Error creating room.")
            return await self._after_room_wizard(chat_id, context, committed=False)

        return await self._after_room_wizard(chat_id, context, committed=True)

    async def _after_room_wizard(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, committed: bool):
        rw = self._room_state(chat_id)
        mode = rw.get("mode", "reg")
        self.tmp.get(chat_id, {}).pop("rw", None)
        if mode == "reg" and not committed:
            # Registration requires at least 1 room; ask for another attempt.
            await context.bot.send_message(
                chat_id=chat_id,
                text="Let's retry. Send a *name* for the room.", parse_mode="Markdown",
            )
            self.tmp[chat_id]["rw"] = {"mode": "reg"}
            return RW_NAME
        if mode == "reg":
            await context.bot.send_message(
                chat_id=chat_id,
                text="Do you want to register *another room*?",
                parse_mode="Markdown",
                reply_markup=yes_no_kb("rw_add"),
            )
            return RW_ADD_ANOTHER
        # mode == "add": single room, go to main menu
        await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
        return MAIN_MENU

    async def rw_add_another_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = (query.data or "").split(":", 1)[-1]
        chat_id = query.message.chat.id
        if choice == "yes":
            self.tmp.setdefault(chat_id, {})["rw"] = {"mode": "reg"}
            await query.edit_message_text("🏠 Send a *name* for the next room.", parse_mode="Markdown")
            return RW_NAME
        user_id = self.session_by_chat.get(chat_id)
        summary = self._registration_summary(user_id)
        await query.edit_message_text(summary, parse_mode="Markdown")
        await context.bot.send_message(chat_id=chat_id, text="Main menu:", reply_markup=main_menu_kb())
        return MAIN_MENU

    def _registration_summary(self, user_id: str) -> str:
        lines = ["🎉 *Registration complete!*", ""]
        try:
            user = self.cat.get_user(user_id) or {}
            uname = (user.get("user_information") or {}).get("userName", user_id)
            lines.append(f"👤 User: *{uname}* (`{user_id}`)")
            lines.append("")

            def fmt(v):
                if v is None:
                    return "?"
                return str(int(v)) if isinstance(v, float) and v == int(v) else str(v)

            rooms = self.cat.rooms_for_user(user_id)
            for r in rooms:
                name = r.get("roomName", r.get("roomID"))
                rid = r.get("roomID")
                times = r.get("times") or {}
                thr = r.get("threshold_parameters") or {}
                lt = thr.get("light_threshold")
                level = THRESHOLD_TO_LIGHT_LEVEL.get(lt)
                light_label = LIGHT_LEVEL_LABELS.get(level, str(lt)) if lt is not None else "N/A"
                devs = r.get("connected_devices") or []
                dev_ids = ", ".join(f"`{d.get('deviceID')}`" for d in devs)
                lines.append(f"🏠 *{name}* (`{rid}`)")
                lines.append(f"   ⏰ {times.get('timeawake', '?')} – {times.get('timesleep', '?')}")
                lines.append(f"   ❤️ HR: {fmt(thr.get('hr_low'))} – {fmt(thr.get('hr_high'))} bpm")
                lines.append(f"   🌡️ Temp: {fmt(thr.get('temp_low'))} – {fmt(thr.get('temp_high'))} °C")
                lines.append(f"   💧 Hum: {fmt(thr.get('hum_low'))} – {fmt(thr.get('hum_high'))} %")
                lines.append(f"   💡 Light: {light_label}")
                if dev_ids:
                    lines.append(f"   📱 Devices: {dev_ids}")
                lines.append("")
        except Exception:
            log.exception("registration summary")
            lines.append("_(Could not load room details.)_")
        return "\n".join(lines)

    # ---------------- Misc ----------------
    async def handle_alarm_off_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = (query.data or "").split(":")
        if len(data) != 3 or data[0] != "alarm_off":
            return
        user_id, room_id = data[1], data[2]
        topic = f"SC/{user_id}/{room_id}/alarm_off"
        payload = json.dumps({"action": "stop"})
        try:
            self.mqtt_pub.publish(topic, payload, qos=1, retain=False)
            log.info("MQTT PUB alarm_off %s", topic)
            await query.edit_message_text(
                "✅ Alarm turned off!\n\n"
                f"📊 View your sleep report at:\n{self.S.nodered_url}\n"
                "Log in with your *userID*, *phone number*, and *password*.",
                parse_mode="Markdown",
            )
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Main menu:",
                reply_markup=main_menu_kb(),
            )
        except Exception:
            log.exception("alarm_off publish")
            await query.edit_message_text("⚠️ Failed to turn off alarm.")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👋 Bye. Use /start anytime.")
        return ConversationHandler.END

# ---------------- MQTT Alerts Listener ----------------
class AlertsMQTT:
    RESEND_SECONDS = 120

    def __init__(self, svc: TelegramBotService):
        self.svc = svc
        self.host = svc.S.broker_ip
        self.port = svc.S.broker_port
        self.subs = svc.S.mqtt_subs or []
        self.client = MqttClient(client_id="telegram-bot-alerts", clean_session=True)
        self.thread: Optional[threading.Thread] = None
        self.state: Dict[tuple, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        # lightweight cache of room_id -> roomName
        self._room_name_cache: Dict[str, str] = {}

    def _normalized_subs(self) -> List[str]:
        out = set(self.subs)
        out.add("SC/alerts/+/+/#")
        out.add("SC/+/+/bedtime")
        out.add("SC/+/+/wakeup")
        return list(out)

    def _room_label(self, room_id: str) -> str:
        if not room_id:
            return ""
        cached = self._room_name_cache.get(room_id)
        if cached is not None:
            return cached
        try:
            r = self.svc.cat.get_room(room_id) or {}
            name = r.get("roomName") or room_id
        except Exception:
            name = room_id
        self._room_name_cache[room_id] = name
        return name

    # ---- MQTT callbacks ----
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%s", self.host, self.port)
            for t in self._normalized_subs():
                try:
                    client.subscribe(t, qos=1)
                    log.info("MQTT SUB %s", t)
                except Exception:
                    log.exception("subscribe failed: %s", t)
        else:
            log.error("MQTT connection failed rc=%s", rc)

    def on_message(self, client, userdata, msg: MQTTMessage):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8", errors="ignore")
            parts = topic.split("/")
            if len(parts) < 3 or parts[0] != "SC":
                return

            if len(parts) == 4 and parts[2] and parts[3] in ("bedtime", "wakeup"):
                user_id, room_id, leaf = parts[1], parts[2], parts[3]
                chats = self.svc.chats_by_user.get(user_id, set())
                if not chats:
                    return
                if leaf == "wakeup":
                    for chat_id in list(chats):
                        self._send_wakeup_message_sync(chat_id, user_id, room_id)
                else:
                    text = self._format_sleep_text(leaf, user_id, room_id, self._room_label(room_id))
                    for chat_id in list(chats):
                        self._send_to_chat_sync(chat_id, text)
                return

            if len(parts) >= 5 and parts[1] == "alerts":
                user_id, room_id, leaf = parts[2], parts[3], parts[4]
                chats = self.svc.chats_by_user.get(user_id, set())
                if not chats:
                    return
                status = self._extract_status(leaf, payload)
                if status is None:
                    return
                key = (user_id, room_id, leaf)
                now = time.time()
                with self._lock:
                    st = self.state.get(key, {"last_status": None, "last_sent": 0.0})
                    last_status = st.get("last_status")
                    last_sent = float(st.get("last_sent") or 0.0)
                    should_send = False
                    if status == "ALERT":
                        if last_status != "ALERT":
                            should_send = True
                        elif (now - last_sent) >= self.RESEND_SECONDS:
                            should_send = True
                    st["last_status"] = status
                    if should_send:
                        st["last_sent"] = now
                    self.state[key] = st
                if should_send:
                    text = self._format_alert_text(leaf, payload, topic, user_id, room_id,
                                                   self._room_label(room_id))
                    if not text:
                        return
                    for chat_id in list(chats):
                        self._send_to_chat_sync(chat_id, text)
        except Exception:
            log.exception("on_message error")

    # ---- Helpers ----
    @staticmethod
    def _extract_status(leaf: str, payload: str) -> Optional[str]:
        try:
            obj = json.loads(payload)
        except Exception:
            obj = None
        if leaf == "hr":
            if isinstance(obj, dict):
                status = obj.get("status") or (obj.get("event") or {}).get("status")
                if isinstance(status, str):
                    su = status.strip().upper()
                    if su in ("ALERT", "OK"):
                        return su
            if '"status":"ALERT"' in payload: return "ALERT"
            if '"status":"OK"' in payload:    return "OK"
            return None
        if leaf == "dht":
            if isinstance(obj, dict):
                evs = obj.get("events", [])
                saw_alert = saw_ok = False
                for e in evs:
                    s = e.get("status")
                    if isinstance(s, str):
                        su = s.strip().upper()
                        if su == "ALERT": saw_alert = True
                        elif su == "OK":  saw_ok = True
                if saw_alert: return "ALERT"
                if saw_ok and not saw_alert: return "OK"
            if '"status":"ALERT"' in payload: return "ALERT"
            if '"status":"OK"' in payload:    return "OK"
            return None
        if isinstance(obj, dict):
            s = obj.get("status") or (obj.get("event") or {}).get("status")
            if isinstance(s, str):
                su = s.strip().upper()
                if su in ("ALERT", "OK"):
                    return su
        if '"status":"ALERT"' in payload: return "ALERT"
        if '"status":"OK"' in payload:    return "OK"
        return None

    @staticmethod
    def _format_alert_text(leaf: str, payload: str, topic: str,
                           user: str, room: str, room_name: str) -> str:
        try:
            obj = json.loads(payload)
        except Exception:
            obj = None
        who = f"{user} / {room_name} ({room})"

        if leaf == "hr":
            if isinstance(obj, dict):
                var = obj.get("variable", "bpm")
                val = obj.get("value")
                status = obj.get("status")
                bounds = obj.get("bounds", [])
                msg = obj.get("message", "")
                return (f"❤️ Heart Rate Alert [{who}]\n"
                        f"Status: {status}\n{var.upper()}: {val}\n"
                        f"Range: {bounds}\n{msg}")
            return f"❤️ Heart Rate alert [{who}]:\n{payload}"

        if leaf == "dht":
            if isinstance(obj, dict):
                events = obj.get("events", [])
                lines = []
                for e in events:
                    lines.append(f"- {e.get('variable')}: {e.get('value')}  |  "
                                 f"Status: {e.get('status')}  |  Range: {e.get('bounds', [])}")
                head = f"🌡️ Environment Alert [{who}]"
                return head + "\n" + "\n".join(lines) if lines else head
            return f"🌡️ Environment alert [{who}]:\n{payload}"

        if isinstance(obj, dict):
            status = obj.get("status") or (obj.get("event") or {}).get("status")
            return f"🚨 Alert [{who}] ({leaf}) — Status: {status}\n{json.dumps(obj, ensure_ascii=False)}"
        return f"🚨 Alert [{who}] ({leaf})\n{payload}"

    @staticmethod
    def _format_sleep_text(leaf: str, user: str, room: str, room_name: str) -> str:
        tag = f"[{room_name}]" if room_name and room_name != room else ""
        if leaf == "bedtime":
            return (f"😴 {tag} It's time to sleep.\n"
                    "Please get ready. From now on, sleep monitoring is active.\n"
                    "Have a good night! 🌙")
        return (f"⏰ {tag} Time to wake up!\n"
                "Monitoring deactivated. Check your dashboard for analysis.\n"
                "Have a great day! ☀️")

    def _send_to_chat_sync(self, chat_id: int, text: str):
        try:
            url = f"https://api.telegram.org/bot{self.svc.S.telegram_token}/sendMessage"
            r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=8)
            if r.status_code != 200:
                log.error("sendMessage failed %s: %s", r.status_code, r.text)
        except Exception:
            log.exception("sendMessage error")

    def _send_wakeup_message_sync(self, chat_id: int, user_id: str, room_id: str):
        room_name = self._room_label(room_id)
        text = (
            f"⏰ *Time to wake up!* [{room_name}]\n\n"
            "Monitoring has been deactivated.\n\n"
            "🔕 Press the button below to turn off the alarm.\n\n"
            f"📊 View your sleep report at:\n{self.svc.S.nodered_url}\n"
            "_Log in with your userID, phone number, and password._"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "🔕 Turn off alarm", "callback_data": f"alarm_off:{user_id}:{room_id}"}
            ]]
        }
        try:
            url = f"https://api.telegram.org/bot{self.svc.S.telegram_token}/sendMessage"
            r = requests.post(url, json={
                "chat_id": chat_id, "text": text,
                "parse_mode": "Markdown", "reply_markup": reply_markup,
            }, timeout=8)
            if r.status_code != 200:
                log.error("sendMessage (wakeup) failed %s: %s", r.status_code, r.text)
        except Exception:
            log.exception("sendMessage (wakeup) error")

    def start(self):
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect(self.host, self.port, keepalive=30)
        self.thread = threading.Thread(target=self.client.loop_forever, daemon=True)
        self.thread.start()
        log.info("MQTT loop thread started.")

# ---------------- Bootstrap ----------------
def build_app(bot: TelegramBotService):
    app = ApplicationBuilder().token(bot.S.telegram_token).build()
    bot.application = app

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", bot.start)],
        states={
            ASK_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.ask_phone)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.ask_password)],

            MAIN_MENU:   [CallbackQueryHandler(bot.main_menu,   pattern=r"^main:")],
            ROOMS_LIST:  [CallbackQueryHandler(bot.rooms_list,  pattern=r"^rooms:")],
            ROOM_MENU:   [CallbackQueryHandler(bot.room_menu,   pattern=r"^room:")],
            CFG_MENU:    [CallbackQueryHandler(bot.cfg_menu,    pattern=r"^cfg:")],

            CFG_TIME_AWAKE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_time_awake)],
            CFG_TIME_SLEEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_time_sleep)],
            CFG_HR_LOW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_hr_low)],
            CFG_HR_HIGH:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_hr_high)],
            CFG_TEMP_LOW:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_temp_low)],
            CFG_TEMP_HIGH:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_temp_high)],
            CFG_HUM_LOW:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_hum_low)],
            CFG_HUM_HIGH:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_hum_high)],
            CFG_POT_LEVEL:  [CallbackQueryHandler(bot.cfg_pot_level_callback, pattern=r"^cfg_pot:")],

            CONFIRM_DELETE_ROOM:    [CallbackQueryHandler(bot.confirm_delete_room,    pattern=r"^confirm_del_room:")],
            CONFIRM_DELETE_ACCOUNT: [CallbackQueryHandler(bot.confirm_delete_account, pattern=r"^confirm_del_acc:")],

            REG_CONFIRM:          [CallbackQueryHandler(bot.reg_confirm_callback, pattern=r"^reg_confirm:")],
            REG_USERNAME:         [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.reg_username)],
            REG_PASSWORD:         [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.reg_password)],
            REG_PASSWORD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.reg_password_confirm)],

            RW_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_name)],
            RW_TIMEAWAKE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_timeawake)],
            RW_TIMESLEEP:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_timesleep)],
            RW_HR_LOW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_hr_low)],
            RW_HR_HIGH:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_hr_high)],
            RW_TEMP_LOW:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_temp_low)],
            RW_TEMP_HIGH:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_temp_high)],
            RW_HUM_LOW:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_hum_low)],
            RW_HUM_HIGH:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.rw_hum_high)],
            RW_POT_LEVEL:  [CallbackQueryHandler(bot.rw_pot_level_callback, pattern=r"^rw_pot:")],
            RW_ADD_ANOTHER:[CallbackQueryHandler(bot.rw_add_another_callback, pattern=r"^rw_add:")],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(bot.handle_alarm_off_callback, pattern="^alarm_off:"))
    return app

if __name__ == "__main__":
    S = BotSettings.load("settings.json")
    service = TelegramBotService(S)
    application = build_app(service)

    alerts = AlertsMQTT(service)
    alerts.start()

    log.info("TelegramBot started. Listening for alerts, bedtime/wakeup and user commands.")
    application.run_polling(close_loop=False)
