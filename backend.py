"""
══════════════════════════════════════════════════════════
  ShadowClean Bot v2.0
  Telegram OSINT + Footprint Cleaner
  Deploy: Render.com

  ⚠️ PERSONAL USE ONLY
══════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import asyncio
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from contextlib import asynccontextmanager

import httpx
import uvicorn
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime,
    ForeignKey, select, delete, and_, update
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import DeclarativeBase, relationship
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)

load_dotenv()


# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB_URL = os.getenv("DATABASE_URL", "")
FERNET_KEY = os.getenv("FERNET_KEY", Fernet.generate_key().decode())
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
PORT = int(os.getenv("PORT", "8000"))
DEFAULT_CREDITS = 1
ADMIN_USERNAME = "@masho_mammado"

if not all([BOT_TOKEN, API_ID, API_HASH, DB_URL]):
    print("❌ Set: BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, DATABASE_URL")
    sys.exit(1)

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
fernet = Fernet(
    FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY
)


# ══════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════

class Base(DeclarativeBase):
    pass


class UserDB(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    lang = Column(String(5), default="fa")
    credits = Column(Integer, default=DEFAULT_CREDITS)
    is_banned = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    total_used = Column(Integer, default=0)
    joined = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    sessions = relationship(
        "SessionDB", back_populates="user", cascade="all, delete-orphan"
    )


class SessionDB(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    phone = Column(String(50))
    enc_session = Column(Text)
    phone_hash = Column(String(255))
    authorized = Column(Boolean, default=False)
    expires = Column(DateTime(timezone=True))
    user = relationship("UserDB", back_populates="sessions")


engine = create_async_engine(
    DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True
)
DBS = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


# ══════════════════════════════════════════
# USER STATE (in memory)
# ══════════════════════════════════════════

user_states: Dict[int, Dict] = {}


def sset(uid: int, state: str, **kw):
    user_states[uid] = {"s": state, **kw}


def sget(uid: int):
    d = user_states.get(uid, {})
    return d.get("s"), d


def sdel(uid: int):
    user_states.pop(uid, None)


# ══════════════════════════════════════════
# TELEGRAM BOT API
# ══════════════════════════════════════════

async def tg(method: str, **kw) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BOT_API}/{method}", json=kw)
            return r.json()
    except Exception:
        return {"ok": False}


async def send(cid: int, text: str, markup=None) -> dict:
    p = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    if markup:
        p["reply_markup"] = markup
    return await tg("sendMessage", **p)


async def edit(cid: int, mid: int, text: str, markup=None) -> dict:
    p = {
        "chat_id": cid,
        "message_id": mid,
        "text": text,
        "parse_mode": "HTML",
    }
    if markup:
        p["reply_markup"] = markup
    try:
        return await tg("editMessageText", **p)
    except Exception:
        return await send(cid, text, markup)


async def answer(cbid: str, text: str = "") -> dict:
    return await tg(
        "answerCallbackQuery", callback_query_id=cbid, text=text
    )


# ══════════════════════════════════════════
# TEXTS (FA + EN)
# ══════════════════════════════════════════

T = {
    "fa": {
        "welcome": (
            "🌑 <b>ShadowClean Bot</b>\n\n"
            "🔍 جستجوی OSINT کاربران\n"
            "👁️ استالک فعالیت در گروه‌ها\n"
            "🧹 پاکسازی ردپای دیجیتال\n\n"
            "⚠️ <i>فقط استفاده شخصی و قانونی</i>\n\n"
            "💎 اعتبار شما: <b>{cr}</b> درخواست\n"
            "📊 کل استفاده: <b>{used}</b>"
        ),
        "help": (
            "❓ <b>راهنمای کامل</b>\n\n"
            "🔍 <b>OSINT</b> - جستجوی اطلاعات عمومی\n"
            "👁️ <b>استالک</b> - بررسی فعالیت در گروه‌ها\n"
            "🧹 <b>پاکسازی</b> - حذف پیام‌های شما\n"
            "📱 <b>ورود</b> - لاگین برای قابلیت‌های پیشرفته\n\n"
            "💎 هر کاربر {cr} درخواست رایگان\n"
            f"⚠️ سوءاستفاده = بن دائم\n"
            f"📞 ادمین: {ADMIN_USERNAME}"
        ),
        "no_credit": (
            "❌ <b>اعتبار شما تمام شده!</b>\n\n"
            f"برای خرید اعتبار با ادمین تماس بگیرید:\n{ADMIN_USERNAME}"
        ),
        "osint_ask": (
            "🔍 <b>جستجوی OSINT</b>\n\n"
            "یکی از موارد زیر رو بفرستید:\n"
            "• @username\n"
            "• آیدی عددی\n\n"
            "⚠️ فقط اطلاعات عمومی نمایش داده میشه"
        ),
        "stalk_ask": (
            "👁️ <b>استالک پیشرفته</b>\n\n"
            "یوزرنیم یا آیدی هدف رو بفرستید:\n\n"
            "⚠️ نیاز به لاگین دارید\n"
            "اگه لاگین نکردید دکمه 📱 ورود بزنید"
        ),
        "clean_info": (
            "🧹 <b>پاکسازی ردپای دیجیتال</b>\n\n"
            "• فقط پیام‌های <b>خودتان</b> حذف میشه\n"
            "• از <b>همه</b> سوپرگروه‌ها\n"
            "• ⚠️ <b>برگشت‌ناپذیر</b>!\n\n"
            "ابتدا باید لاگین کنید (دکمه 📱 ورود)"
        ),
        "phone_ask": (
            "📱 <b>ورود امن به تلگرام</b>\n\n"
            "شماره تلفن با کد کشور بفرستید:\n"
            "<code>+989121234567</code>\n\n"
            "🔐 سشن با AES-256 رمزنگاری میشه\n"
            "⏰ بعد ۲۴ ساعت خودکار حذف میشه\n"
            "🚪 هر وقت خواستید /logout بزنید"
        ),
        "code_ask": (
            "📨 <b>کد تأیید ارسال شد!</b>\n\n"
            "کد ۵ رقمی رو اینجا بفرستید:"
        ),
        "2fa_ask": "🔐 رمز دوم (2FA) رو وارد کنید:",
        "login_ok": "✅ <b>ورود موفق!</b>\nحالا از امکانات پیشرفته استفاده کنید.",
        "login_fail": "❌ <b>خطا در ورود:</b>\n{e}",
        "logout_ok": "✅ خارج شدید. سشن حذف شد.",
        "not_logged": (
            "❌ <b>ابتدا وارد شوید</b>\n\n"
            "از دکمه 📱 ورود استفاده کنید"
        ),
        "profile": (
            "👤 <b>پروفایل شما</b>\n\n"
            "🆔 آیدی: <code>{uid}</code>\n"
            "👤 نام: {name}\n"
            "💎 اعتبار باقیمانده: <b>{cr}</b>\n"
            "📊 کل استفاده: {used}\n"
            "🔐 وضعیت لاگین: {login}\n"
            "📅 تاریخ عضویت: {date}"
        ),
        "ethical": (
            "⚠️ <b>هشدار قانونی و اخلاقی</b>\n\n"
            "• این ابزار فقط برای مدیریت داده‌های <b>شخصی</b> شماست\n"
            "• جاسوسی از دیگران <b>غیرقانونی</b> است\n"
            "• تمام مسئولیت استفاده با <b>شماست</b>\n"
            "• با ادامه، شرایط استفاده رو می‌پذیرید\n\n"
            "آیا موافقید؟"
        ),
        "processing": "⏳ <b>در حال پردازش...</b>\nلطفاً صبر کنید",
        "osint_res": (
            "🔍 <b>نتیجه جستجوی OSINT</b>\n\n"
            "👤 نام: {name}\n"
            "🆔 آیدی: <code>{uid}</code>\n"
            "📛 یوزرنیم: {uname}\n"
            "📸 عکس پروفایل: {photo}\n"
            "ℹ️ بیو: {bio}\n"
            "⏰ آخرین حضور: {seen}"
        ),
        "dry_res": (
            "📊 <b>نتیجه اسکن (بدون حذف)</b>\n\n"
            "📂 گروه‌ها: {gr}\n"
            "💬 کل پیام‌ها: {ms}\n"
            "📸 مدیا: {md}\n"
            "📝 متن: {tx}\n\n"
            "برای حذف واقعی دکمه زیر رو بزنید:"
        ),
        "del_prog": "🧹 حذف... {pct}%\n✅ {done} حذف شده\n📂 {group}",
        "del_done": (
            "✅ <b>پاکسازی کامل شد!</b>\n\n"
            "🗑️ حذف شده: <b>{done}</b>\n"
            "📂 گروه‌ها: {gr}\n"
            "⏱️ زمان: {time}\n"
            "❌ خطاها: {err}"
        ),
        "confirm": (
            "⚠️ <b>آیا مطمئنید؟</b>\n\n"
            "تمام پیام‌های شما از همه گروه‌ها حذف میشه!\n"
            "این عمل <b>برگشت‌ناپذیر</b> است!"
        ),
        "banned": f"🚫 <b>حساب شما مسدود شده</b>\n\nادمین: {ADMIN_USERNAME}",
        "error": "❌ <b>خطا:</b> {e}",
        "stalk_res": (
            "👁️ <b>نتیجه استالک</b>\n\n"
            "💬 کل پیام‌ها: <b>{ms}</b>\n"
            "📂 گروه‌ها: <b>{gr}</b>"
        ),
        # Admin texts
        "admin_panel": (
            "👑 <b>پنل مدیریت</b>\n\n"
            "👥 کل کاربران: <b>{total}</b>\n"
            "🚫 بن‌شده: <b>{banned}</b>\n"
            "🔐 لاگین‌شده: <b>{logged}</b>\n"
            "💎 مجموع اعتبار داده‌شده: <b>{credits}</b>\n\n"
            "از دکمه‌های زیر استفاده کنید:"
        ),
        "a_credit_ask": (
            "💎 <b>افزودن اعتبار</b>\n\n"
            "به این فرمت بفرستید:\n"
            "<code>آیدی_عددی تعداد</code>\n\n"
            "مثال:\n"
            "<code>123456789 10</code>"
        ),
        "a_credit_ok": (
            "✅ <b>اعتبار اضافه شد!</b>\n\n"
            "👤 کاربر: <code>{uid}</code>\n"
            "➕ اضافه شده: <b>{n}</b>\n"
            "💎 اعتبار فعلی: <b>{total}</b>"
        ),
        "a_credit_fail": (
            "❌ <b>فرمت اشتباه!</b>\n\n"
            "صحیح: <code>آیدی تعداد</code>\n"
            "مثال: <code>123456789 5</code>"
        ),
        "a_ban_ask": "🚫 <b>بن کردن کاربر</b>\n\nآیدی عددی رو بفرستید:",
        "a_ban_ok": "✅ کاربر <code>{uid}</code> بن شد.",
        "a_unban_ask": "✅ <b>آنبن کردن</b>\n\nآیدی عددی رو بفرستید:",
        "a_unban_ok": "✅ کاربر <code>{uid}</code> آنبن شد.",
        "a_notfound": "❌ کاربر پیدا نشد!",
        "a_user_info": (
            "📊 <b>اطلاعات کاربر</b>\n\n"
            "🆔 آیدی: <code>{uid}</code>\n"
            "👤 نام: {name}\n"
            "📛 یوزرنیم: @{uname}\n"
            "💎 اعتبار: <b>{cr}</b>\n"
            "📊 استفاده: {used}\n"
            "🚫 بن: {ban}\n"
            "📅 عضویت: {date}"
        ),
        "a_lookup_ask": "🔎 <b>جستجوی کاربر</b>\n\nآیدی عددی بفرستید:",
        "a_broadcast_ask": (
            "📢 <b>پیام همگانی</b>\n\n"
            "متن پیام رو بفرستید.\n"
            "به همه کاربران ارسال میشه."
        ),
        "a_broadcast_ok": "✅ پیام به <b>{n}</b> کاربر ارسال شد.",
        "a_broadcast_fail": "❌ ارسال ناموفق به بعضی کاربران.",
        "a_setcredit_ask": (
            "🔧 <b>تنظیم اعتبار (جایگزین)</b>\n\n"
            "فرمت: <code>آیدی تعداد</code>\n"
            "اعتبار فعلی کاربر با این عدد جایگزین میشه"
        ),
        "a_setcredit_ok": (
            "✅ اعتبار کاربر <code>{uid}</code> به <b>{n}</b> تغییر کرد."
        ),
    },
    "en": {
        "welcome": (
            "🌑 <b>ShadowClean Bot</b>\n\n"
            "🔍 OSINT User Search\n"
            "👁️ Stalk Activity\n"
            "🧹 Footprint Cleanup\n\n"
            "⚠️ <i>Personal & legal use only</i>\n\n"
            "💎 Credits: <b>{cr}</b>\n"
            "📊 Total used: <b>{used}</b>"
        ),
        "help": (
            "❓ <b>Help</b>\n\n"
            "🔍 <b>OSINT</b> - Public info search\n"
            "👁️ <b>Stalk</b> - Group activity\n"
            "🧹 <b>Cleanup</b> - Delete your messages\n"
            "📱 <b>Login</b> - Advanced features\n\n"
            "💎 {cr} free credits\n"
            f"⚠️ Abuse = permanent ban\n"
            f"📞 Admin: {ADMIN_USERNAME}"
        ),
        "no_credit": (
            "❌ <b>No credits left!</b>\n\n"
            f"Contact admin: {ADMIN_USERNAME}"
        ),
        "osint_ask": (
            "🔍 <b>OSINT Search</b>\n\n"
            "Send:\n• @username\n• Numeric ID\n\n"
            "⚠️ Public info only"
        ),
        "stalk_ask": (
            "👁️ <b>Advanced Stalk</b>\n\n"
            "Send target username or ID:\n\n"
            "⚠️ Login required (📱 Login button)"
        ),
        "clean_info": (
            "🧹 <b>Footprint Cleanup</b>\n\n"
            "• Only <b>YOUR</b> messages\n"
            "• From <b>all</b> supergroups\n"
            "• ⚠️ <b>Irreversible</b>!\n\n"
            "Login first (📱 Login button)"
        ),
        "phone_ask": (
            "📱 <b>Secure Login</b>\n\n"
            "Send phone with country code:\n"
            "<code>+989121234567</code>\n\n"
            "🔐 AES-256 encrypted\n"
            "⏰ Auto-delete 24h\n"
            "🚪 /logout anytime"
        ),
        "code_ask": "📨 <b>Code sent!</b>\n\nEnter the 5-digit code:",
        "2fa_ask": "🔐 Enter your 2FA password:",
        "login_ok": "✅ <b>Login successful!</b>",
        "login_fail": "❌ <b>Login error:</b>\n{e}",
        "logout_ok": "✅ Logged out. Session deleted.",
        "not_logged": "❌ <b>Login first</b>\n\nUse 📱 Login button",
        "profile": (
            "👤 <b>Your Profile</b>\n\n"
            "🆔 ID: <code>{uid}</code>\n"
            "👤 Name: {name}\n"
            "💎 Credits: <b>{cr}</b>\n"
            "📊 Used: {used}\n"
            "🔐 Login: {login}\n"
            "📅 Joined: {date}"
        ),
        "ethical": (
            "⚠️ <b>Legal & Ethical Warning</b>\n\n"
            "• YOUR personal data only\n"
            "• Spying is <b>ILLEGAL</b>\n"
            "• <b>You</b> are responsible\n\n"
            "Do you agree?"
        ),
        "processing": "⏳ <b>Processing...</b>\nPlease wait",
        "osint_res": (
            "🔍 <b>OSINT Result</b>\n\n"
            "👤 Name: {name}\n"
            "🆔 ID: <code>{uid}</code>\n"
            "📛 Username: {uname}\n"
            "📸 Photo: {photo}\n"
            "ℹ️ Bio: {bio}\n"
            "⏰ Last seen: {seen}"
        ),
        "dry_res": (
            "📊 <b>Scan Result (Dry Run)</b>\n\n"
            "📂 Groups: {gr}\n"
            "💬 Messages: {ms}\n"
            "📸 Media: {md}\n"
            "📝 Text: {tx}"
        ),
        "del_prog": "🧹 {pct}% | {done} deleted | {group}",
        "del_done": (
            "✅ <b>Cleanup Complete!</b>\n\n"
            "🗑️ Deleted: <b>{done}</b>\n"
            "📂 Groups: {gr}\n"
            "⏱️ Time: {time}\n"
            "❌ Errors: {err}"
        ),
        "confirm": (
            "⚠️ <b>Are you sure?</b>\n\n"
            "All YOUR messages from ALL groups will be deleted!\n"
            "<b>Irreversible!</b>"
        ),
        "banned": f"🚫 <b>You are banned</b>\n\nAdmin: {ADMIN_USERNAME}",
        "error": "❌ <b>Error:</b> {e}",
        "stalk_res": (
            "👁️ <b>Stalk Result</b>\n\n"
            "💬 Messages: <b>{ms}</b>\n"
            "📂 Groups: <b>{gr}</b>"
        ),
        "admin_panel": (
            "👑 <b>Admin Panel</b>\n\n"
            "👥 Total users: <b>{total}</b>\n"
            "🚫 Banned: <b>{banned}</b>\n"
            "🔐 Logged in: <b>{logged}</b>\n"
            "💎 Total credits given: <b>{credits}</b>"
        ),
        "a_credit_ask": (
            "💎 <b>Add Credits</b>\n\n"
            "Format: <code>user_id amount</code>\n"
            "Example: <code>123456789 10</code>"
        ),
        "a_credit_ok": (
            "✅ <b>Credits added!</b>\n\n"
            "👤 User: <code>{uid}</code>\n"
            "➕ Added: <b>{n}</b>\n"
            "💎 Current: <b>{total}</b>"
        ),
        "a_credit_fail": (
            "❌ <b>Wrong format!</b>\n\n"
            "Correct: <code>ID amount</code>"
        ),
        "a_ban_ask": "🚫 <b>Ban User</b>\n\nSend numeric user ID:",
        "a_ban_ok": "✅ User <code>{uid}</code> banned.",
        "a_unban_ask": "✅ <b>Unban User</b>\n\nSend numeric ID:",
        "a_unban_ok": "✅ User <code>{uid}</code> unbanned.",
        "a_notfound": "❌ User not found!",
        "a_user_info": (
            "📊 <b>User Info</b>\n\n"
            "🆔 ID: <code>{uid}</code>\n"
            "👤 Name: {name}\n"
            "📛 @{uname}\n"
            "💎 Credits: <b>{cr}</b>\n"
            "📊 Used: {used}\n"
            "🚫 Ban: {ban}\n"
            "📅 Joined: {date}"
        ),
        "a_lookup_ask": "🔎 <b>Lookup User</b>\n\nSend numeric ID:",
        "a_broadcast_ask": "📢 <b>Broadcast</b>\n\nSend your message:",
        "a_broadcast_ok": "✅ Sent to <b>{n}</b> users.",
        "a_broadcast_fail": "❌ Failed for some users.",
        "a_setcredit_ask": (
            "🔧 <b>Set Credits (Replace)</b>\n\n"
            "Format: <code>ID amount</code>\n"
            "Replaces current credits"
        ),
        "a_setcredit_ok": "✅ User <code>{uid}</code> credits set to <b>{n}</b>.",
    },
}


def tx(la: str, key: str, **kw) -> str:
    txt = T.get(la, T["fa"]).get(key, T["fa"].get(key, key))
    try:
        return txt.format(**kw) if kw else txt
    except Exception:
        return txt


# ══════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════

def kb_main(la: str, is_admin: bool = False) -> dict:
    if la == "en":
        rows = [
            [
                {"text": "🔍 OSINT Search", "callback_data": "osint"},
                {"text": "👁️ Stalk", "callback_data": "stalk"},
            ],
            [
                {"text": "🧹 Cleanup", "callback_data": "clean"},
                {"text": "📱 Login", "callback_data": "do_login"},
            ],
            [
                {"text": "👤 Profile", "callback_data": "prof"},
                {"text": "❓ Help", "callback_data": "help"},
            ],
            [
                {"text": "🌐 فارسی", "callback_data": "lang"},
                {"text": "🚪 Logout", "callback_data": "do_logout"},
            ],
        ]
        if is_admin:
            rows.append(
                [{"text": "👑 Admin Panel", "callback_data": "admin"}]
            )
    else:
        rows = [
            [
                {"text": "🔍 جستجو OSINT", "callback_data": "osint"},
                {"text": "👁️ استالک", "callback_data": "stalk"},
            ],
            [
                {"text": "🧹 پاکسازی ردپا", "callback_data": "clean"},
                {"text": "📱 ورود", "callback_data": "do_login"},
            ],
            [
                {"text": "👤 پروفایل", "callback_data": "prof"},
                {"text": "❓ راهنما", "callback_data": "help"},
            ],
            [
                {"text": "🌐 English", "callback_data": "lang"},
                {"text": "🚪 خروج", "callback_data": "do_logout"},
            ],
        ]
        if is_admin:
            rows.append(
                [{"text": "👑 پنل مدیریت", "callback_data": "admin"}]
            )
    return {"inline_keyboard": rows}


def kb_back(la: str) -> dict:
    txt = "🔙 Back" if la == "en" else "🔙 بازگشت"
    return {"inline_keyboard": [[{"text": txt, "callback_data": "main"}]]}


def kb_eth(la: str) -> dict:
    y = "✅ I Agree" if la == "en" else "✅ موافقم"
    n = "❌ Disagree" if la == "en" else "❌ مخالفم"
    return {
        "inline_keyboard": [
            [
                {"text": y, "callback_data": "eth_y"},
                {"text": n, "callback_data": "eth_n"},
            ]
        ]
    }


def kb_clean(la: str) -> dict:
    s = "📊 Scan First" if la == "en" else "📊 اسکن اول"
    d = "🗑️ Delete All" if la == "en" else "🗑️ حذف همه"
    b = "🔙 Back" if la == "en" else "🔙 بازگشت"
    return {
        "inline_keyboard": [
            [
                {"text": s, "callback_data": "cl_dry"},
                {"text": d, "callback_data": "cl_real"},
            ],
            [{"text": b, "callback_data": "main"}],
        ]
    }


def kb_confirm(la: str) -> dict:
    y = "✅ Yes, Delete!" if la == "en" else "✅ بله، حذف کن!"
    n = "❌ Cancel" if la == "en" else "❌ انصراف"
    return {
        "inline_keyboard": [
            [
                {"text": y, "callback_data": "cf_y"},
                {"text": n, "callback_data": "cf_n"},
            ]
        ]
    }


def kb_admin(la: str) -> dict:
    if la == "en":
        return {
            "inline_keyboard": [
                [
                    {"text": "💎 Add Credits", "callback_data": "a_credit"},
                    {"text": "🔧 Set Credits", "callback_data": "a_setcr"},
                ],
                [
                    {"text": "🔎 Lookup User", "callback_data": "a_lookup"},
                    {"text": "📊 Stats", "callback_data": "a_stats"},
                ],
                [
                    {"text": "🚫 Ban", "callback_data": "a_ban"},
                    {"text": "✅ Unban", "callback_data": "a_unban"},
                ],
                [
                    {"text": "📢 Broadcast", "callback_data": "a_bcast"},
                ],
                [{"text": "🔙 Back", "callback_data": "main"}],
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "💎 افزودن اعتبار", "callback_data": "a_credit"},
                {"text": "🔧 تنظیم اعتبار", "callback_data": "a_setcr"},
            ],
            [
                {"text": "🔎 جستجوی کاربر", "callback_data": "a_lookup"},
                {"text": "📊 آمار", "callback_data": "a_stats"},
            ],
            [
                {"text": "🚫 بن کردن", "callback_data": "a_ban"},
                {"text": "✅ آنبن", "callback_data": "a_unban"},
            ],
            [
                {"text": "📢 پیام همگانی", "callback_data": "a_bcast"},
            ],
            [{"text": "🔙 بازگشت", "callback_data": "main"}],
        ]
    }


# ══════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════

async def get_user(db: AsyncSession, uid: int, uname="", fname=""):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        u = UserDB(
            id=uid,
            username=uname,
            first_name=fname,
            credits=DEFAULT_CREDITS,
            is_admin=uid in ADMIN_IDS,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
    else:
        ch = False
        if uname and u.username != uname:
            u.username = uname
            ch = True
        if fname and u.first_name != fname:
            u.first_name = fname
            ch = True
        if uid in ADMIN_IDS and not u.is_admin:
            u.is_admin = True
            ch = True
        if ch:
            await db.commit()
    return u


async def use_credit(db: AsyncSession, uid: int) -> bool:
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u or u.credits <= 0:
        return False
    u.credits -= 1
    u.total_used += 1
    await db.commit()
    return True


async def add_credits(db: AsyncSession, uid: int, n: int):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        return None
    u.credits += n
    await db.commit()
    return u.credits


async def set_credits(db: AsyncSession, uid: int, n: int):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        return None
    u.credits = n
    await db.commit()
    return u.credits


async def ban_user(db: AsyncSession, uid: int) -> bool:
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        return False
    u.is_banned = True
    await db.commit()
    return True


async def unban_user(db: AsyncSession, uid: int) -> bool:
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        return False
    u.is_banned = False
    await db.commit()
    return True


async def lookup_user(db: AsyncSession, uid: int):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    return r.scalar_one_or_none()


async def get_all_users(db: AsyncSession):
    r = await db.execute(select(UserDB))
    return r.scalars().all()


async def get_stats(db: AsyncSession):
    users = await get_all_users(db)
    total = len(users)
    banned = sum(1 for u in users if u.is_banned)
    total_credits = sum(u.credits for u in users)
    r2 = await db.execute(
        select(SessionDB).where(SessionDB.authorized == True)
    )
    logged = len(r2.scalars().all())
    return total, banned, logged, total_credits


async def get_auth_session(db: AsyncSession, uid: int):
    r = await db.execute(
        select(SessionDB).where(
            and_(
                SessionDB.user_id == uid,
                SessionDB.authorized == True,
                SessionDB.expires > datetime.now(timezone.utc),
            )
        )
    )
    return r.scalar_one_or_none()


async def get_any_sess(db: AsyncSession, uid: int):
    r = await db.execute(
        select(SessionDB).where(SessionDB.user_id == uid)
    )
    return r.scalar_one_or_none()


async def save_sess(db: AsyncSession, uid: int, phone, ss, ph):
    await db.execute(
        delete(SessionDB).where(SessionDB.user_id == uid)
    )
    s = SessionDB(
        user_id=uid,
        phone=phone,
        enc_session=fernet.encrypt(ss.encode()).decode(),
        phone_hash=ph,
        expires=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(s)
    await db.commit()


async def auth_sess(db: AsyncSession, uid: int, ss: str):
    r = await db.execute(
        select(SessionDB).where(SessionDB.user_id == uid)
    )
    s = r.scalar_one_or_none()
    if s:
        s.enc_session = fernet.encrypt(ss.encode()).decode()
        s.authorized = True
        await db.commit()


async def del_sess(db: AsyncSession, uid: int):
    await db.execute(
        delete(SessionDB).where(SessionDB.user_id == uid)
    )
    await db.commit()


async def dec_sess(db: AsyncSession, uid: int):
    s = await get_auth_session(db, uid)
    if s and s.enc_session:
        return fernet.decrypt(s.enc_session.encode()).decode()
    return None


# ══════════════════════════════════════════
# TELETHON
# ══════════════════════════════════════════

clients: Dict[int, TelegramClient] = {}


async def tclient(uid: int, ss: str) -> TelegramClient:
    if uid in clients and clients[uid].is_connected():
        return clients[uid]
    c = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await c.connect()
    clients[uid] = c
    return c


async def tnew() -> TelegramClient:
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect()
    return c


# ══════════════════════════════════════════
# OSINT ENGINE
# ══════════════════════════════════════════

async def osint_light(target) -> Optional[dict]:
    r = await tg("getChat", chat_id=target)
    if r.get("ok"):
        c = r["result"]
        pr = await tg(
            "getUserProfilePhotos", user_id=c.get("id", 0), limit=1
        )
        pc = (
            pr.get("result", {}).get("total_count", 0)
            if pr.get("ok")
            else 0
        )
        return {
            "uid": c.get("id"),
            "name": f'{c.get("first_name", "")} {c.get("last_name", "")}'.strip(),
            "uname": c.get("username", ""),
            "bio": c.get("bio", "—"),
            "photo": "✅" if pc else "❌",
        }
    return None


async def osint_full(client: TelegramClient, target) -> Optional[dict]:
    try:
        ent = await client.get_entity(target)
        full = await client(GetFullUserRequest(ent))
        seen = "?"
        if hasattr(ent, "status") and ent.status:
            if hasattr(ent.status, "was_online"):
                seen = str(ent.status.was_online)
            else:
                seen = type(ent.status).__name__.replace("UserStatus", "")
        commons = []
        try:
            cr = await client(
                functions.messages.GetCommonChatsRequest(
                    user_id=ent, max_id=0, limit=50
                )
            )
            commons = [getattr(c, "title", "?") for c in cr.chats]
        except Exception:
            pass
        return {
            "uid": ent.id,
            "name": f'{getattr(ent, "first_name", "") or ""} {getattr(ent, "last_name", "") or ""}'.strip(),
            "uname": getattr(ent, "username", ""),
            "bio": getattr(full.full_user, "about", "") or "—",
            "photo": "✅" if ent.photo else "❌",
            "seen": seen,
            "commons": commons,
        }
    except Exception:
        return None


# ══════════════════════════════════════════
# STALK ENGINE
# ══════════════════════════════════════════

async def do_stalk(client, tid, cid, la):
    res = {"ms": 0, "groups": []}
    try:
        dlg = await client.get_dialogs(limit=200)
        pubs = [
            d
            for d in dlg
            if d.is_group
            and hasattr(d.entity, "megagroup")
            and d.entity.megagroup
        ]
        pm = await send(cid, tx(la, "processing"))
        pmid = (
            pm.get("result", {}).get("message_id")
            if pm.get("ok")
            else None
        )
        for i, d in enumerate(pubs):
            cnt = 0
            try:
                async for _ in client.iter_messages(
                    d.entity, from_user=tid, limit=100
                ):
                    cnt += 1
                if cnt:
                    res["groups"].append(
                        {"t": d.entity.title, "c": cnt}
                    )
                    res["ms"] += cnt
                if pmid and (i + 1) % 5 == 0:
                    pct = int((i + 1) / len(pubs) * 100)
                    await edit(cid, pmid, f"👁️ Scanning... {pct}%")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                continue
    except Exception:
        pass
    return res


# ══════════════════════════════════════════
# CLEANUP ENGINE
# ══════════════════════════════════════════

async def do_dry(client, cid, la):
    res = {"gr": [], "ms": 0, "md": 0, "tx": 0}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [
            d
            for d in dlg
            if d.is_group
            and hasattr(d.entity, "megagroup")
            and d.entity.megagroup
        ]
        pm = await send(cid, tx(la, "processing"))
        pmid = (
            pm.get("result", {}).get("message_id")
            if pm.get("ok")
            else None
        )
        for i, d in enumerate(sgs):
            gc = gm = gt = 0
            try:
                async for m in client.iter_messages(
                    d.entity, from_user=me.id
                ):
                    gc += 1
                    if m.media:
                        gm += 1
                    else:
                        gt += 1
                if gc:
                    res["gr"].append(
                        {"t": d.entity.title, "c": gc}
                    )
                    res["ms"] += gc
                    res["md"] += gm
                    res["tx"] += gt
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                continue
            if pmid and (i + 1) % 3 == 0:
                pct = int((i + 1) / len(sgs) * 100)
                await edit(
                    cid,
                    pmid,
                    f"📊 {pct}% | {d.entity.title}",
                )
    except Exception:
        pass
    return res


async def do_real_delete(client, cid, la):
    res = {"done": 0, "err": 0, "gr": 0, "det": []}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [
            d
            for d in dlg
            if d.is_group
            and hasattr(d.entity, "megagroup")
            and d.entity.megagroup
        ]
        pm = await send(cid, tx(la, "processing"))
        pmid = (
            pm.get("result", {}).get("message_id")
            if pm.get("ok")
            else None
        )
        start = time.time()
        for i, d in enumerate(sgs):
            ids = []
            try:
                async for m in client.iter_messages(
                    d.entity, from_user=me.id
                ):
                    ids.append(m.id)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                continue
            if not ids:
                continue
            gd = ge = 0
            for j in range(0, len(ids), 50):
                batch = ids[j : j + 50]
                try:
                    await client.delete_messages(
                        d.entity, batch, revoke=True
                    )
                    gd += len(batch)
                    await asyncio.sleep(1)
                except FloodWaitError as e:
                    await asyncio.sleep(int(e.seconds * 1.5))
                    try:
                        await client.delete_messages(
                            d.entity, batch, revoke=True
                        )
                        gd += len(batch)
                    except Exception:
                        ge += len(batch)
                except Exception:
                    ge += len(batch)
            res["done"] += gd
            res["err"] += ge
            if gd:
                res["gr"] += 1
                res["det"].append(f"{d.entity.title}: {gd}")
            if pmid:
                try:
                    pct = int((i + 1) / len(sgs) * 100)
                    await edit(
                        cid,
                        pmid,
                        tx(
                            la,
                            "del_prog",
                            pct=pct,
                            done=res["done"],
                            group=d.entity.title,
                        ),
                    )
                except Exception:
                    pass
    except Exception:
        pass
    return res


# ══════════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════════

async def bg_osint(uid, cid, target, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if ss:
            client = await tclient(uid, ss)
            r = await osint_full(client, target)
        else:
            r = await osint_light(target)
        if r:
            txt = tx(
                la,
                "osint_res",
                name=r.get("name", "?"),
                uid=r.get("uid", "?"),
                uname=f'@{r["uname"]}' if r.get("uname") else "—",
                photo=r.get("photo", "?"),
                bio=r.get("bio", "—"),
                seen=r.get("seen", "—"),
            )
            if r.get("commons"):
                txt += "\n\n📂 گروه‌های مشترک:\n" + "\n".join(
                    f"  • {c}" for c in r["commons"][:10]
                )
            await send(cid, txt, kb_back(la))
        else:
            await send(
                cid, tx(la, "error", e="User not found"), kb_back(la)
            )


async def bg_stalk(uid, cid, target, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "not_logged"))
            return
        client = await tclient(uid, ss)
        try:
            ent = await client.get_entity(target)
            tid = ent.id
        except Exception:
            await send(cid, tx(la, "error", e="Target not found"))
            return
        r = await do_stalk(client, tid, cid, la)
        txt = tx(la, "stalk_res", ms=r["ms"], gr=len(r["groups"]))
        if r["groups"]:
            txt += "\n\n" + "\n".join(
                f"  • {g['t']}: {g['c']}" for g in r["groups"][:15]
            )
        await send(cid, txt, kb_back(la))


async def bg_dry(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "not_logged"))
            return
        client = await tclient(uid, ss)
        r = await do_dry(client, cid, la)
        txt = tx(
            la,
            "dry_res",
            gr=len(r["gr"]),
            ms=r["ms"],
            md=r["md"],
            tx=r["tx"],
        )
        if r["gr"]:
            txt += "\n\n" + "\n".join(
                f"  • {g['t']}: {g['c']}" for g in r["gr"][:20]
            )
        await send(cid, txt, kb_confirm(la))


async def bg_real(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "not_logged"))
            return
        client = await tclient(uid, ss)
        start = time.time()
        r = await do_real_delete(client, cid, la)
        el = time.time() - start
        ts = f"{int(el // 60)}m {int(el % 60)}s"
        txt = tx(
            la,
            "del_done",
            done=r["done"],
            gr=r["gr"],
            time=ts,
            err=r["err"],
        )
        if r["det"]:
            txt += "\n\n" + "\n".join(
                f"  • {d}" for d in r["det"][:20]
            )
        await send(cid, txt, kb_back(la))


async def bg_login(uid, cid, phone, la):
    async with DBS() as db:
        try:
            client = await tnew()
            result = await client.send_code_request(phone)
            ss = client.session.save()
            await save_sess(db, uid, phone, ss, result.phone_code_hash)
            sset(uid, "code", phone=phone, ph=result.phone_code_hash)
            await send(cid, tx(la, "code_ask"))
            await client.disconnect()
        except Exception as e:
            await send(cid, tx(la, "login_fail", e=str(e)[:200]))


async def bg_code(uid, cid, code, la):
    async with DBS() as db:
        try:
            so = await get_any_sess(db, uid)
            if not so or not so.enc_session:
                await send(cid, tx(la, "login_fail", e="No session"))
                return
            ss = fernet.decrypt(so.enc_session.encode()).decode()
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            _, sd = sget(uid)
            try:
                await client.sign_in(
                    phone=sd.get("phone", so.phone),
                    code=code,
                    phone_code_hash=sd.get("ph", so.phone_hash),
                )
                nss = client.session.save()
                await auth_sess(db, uid, nss)
                sdel(uid)
                ia = uid in ADMIN_IDS
                await send(
                    cid, tx(la, "login_ok"), kb_main(la, ia)
                )
            except SessionPasswordNeededError:
                nss = client.session.save()
                so.enc_session = fernet.encrypt(
                    nss.encode()
                ).decode()
                await db.commit()
                sset(uid, "2fa")
                await send(cid, tx(la, "2fa_ask"))
            finally:
                await client.disconnect()
        except PhoneCodeInvalidError:
            await send(cid, tx(la, "login_fail", e="Wrong code"))
        except PhoneCodeExpiredError:
            sdel(uid)
            await send(
                cid, tx(la, "login_fail", e="Code expired. Try /login")
            )
        except Exception as e:
            await send(cid, tx(la, "login_fail", e=str(e)[:200]))


async def bg_2fa(uid, cid, pwd, la):
    async with DBS() as db:
        try:
            so = await get_any_sess(db, uid)
            if not so or not so.enc_session:
                await send(cid, tx(la, "login_fail", e="No session"))
                return
            ss = fernet.decrypt(so.enc_session.encode()).decode()
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            try:
                await client.sign_in(password=pwd)
                nss = client.session.save()
                await auth_sess(db, uid, nss)
                sdel(uid)
                ia = uid in ADMIN_IDS
                await send(
                    cid, tx(la, "login_ok"), kb_main(la, ia)
                )
            finally:
                await client.disconnect()
        except PasswordHashInvalidError:
            await send(
                cid, tx(la, "login_fail", e="Wrong 2FA password")
            )
        except Exception as e:
            await send(cid, tx(la, "login_fail", e=str(e)[:200]))


async def bg_logout(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if ss:
            try:
                c = TelegramClient(
                    StringSession(ss), API_ID, API_HASH
                )
                await c.connect()
                await c.log_out()
                await c.disconnect()
            except Exception:
                pass
        await del_sess(db, uid)
        clients.pop(uid, None)
        sdel(uid)
        ia = uid in ADMIN_IDS
        await send(cid, tx(la, "logout_ok"), kb_main(la, ia))


async def bg_broadcast(admin_uid, admin_cid, text, la):
    async with DBS() as db:
        users = await get_all_users(db)
        n = 0
        for u in users:
            if u.id == admin_uid:
                continue
            try:
                await send(
                    u.id,
                    f"📢 <b>{'پیام از مدیریت' if la == 'fa' else 'Admin Message'}:</b>\n\n{text}",
                )
                n += 1
                await asyncio.sleep(0.1)
            except Exception:
                continue
        await send(
            admin_cid, tx(la, "a_broadcast_ok", n=n), kb_admin(la)
        )


# ══════════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════════

async def on_msg(db: AsyncSession, msg: dict, bg: BackgroundTasks):
    cid = msg.get("chat", {}).get("id")
    uid = msg.get("from", {}).get("id")
    fname = msg.get("from", {}).get("first_name", "")
    uname = msg.get("from", {}).get("username", "")
    text = (msg.get("text") or "").strip()

    if not cid or not uid:
        return
    if msg.get("chat", {}).get("type") != "private":
        return

    u = await get_user(db, uid, uname, fname)
    la = u.lang
    ia = u.is_admin or uid in ADMIN_IDS

    if u.is_banned:
        await send(cid, tx(la, "banned"))
        return

    # ── Check states ──
    st, sd = sget(uid)

    if st == "code":
        bg.add_task(bg_code, uid, cid, text, la)
        return
    if st == "2fa":
        bg.add_task(bg_2fa, uid, cid, text, la)
        return
    if st == "phone":
        ph = text if text.startswith("+") else "+" + text
        bg.add_task(bg_login, uid, cid, ph, la)
        return

    if st == "osint":
        sdel(uid)
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        await use_credit(db, uid)
        bg.add_task(bg_osint, uid, cid, text, la)
        return

    if st == "stalk":
        sdel(uid)
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        await use_credit(db, uid)
        bg.add_task(bg_stalk, uid, cid, text, la)
        return

    # ── Admin states ──
    if st == "a_credit" and ia:
        sdel(uid)
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            tuid, n = int(parts[0]), int(parts[1])
            total = await add_credits(db, tuid, n)
            if total is not None:
                await send(
                    cid,
                    tx(la, "a_credit_ok", uid=tuid, n=n, total=total),
                    kb_admin(la),
                )
            else:
                await send(
                    cid, tx(la, "a_notfound"), kb_admin(la)
                )
        else:
            await send(
                cid, tx(la, "a_credit_fail"), kb_admin(la)
            )
        return

    if st == "a_setcr" and ia:
        sdel(uid)
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            tuid, n = int(parts[0]), int(parts[1])
            total = await set_credits(db, tuid, n)
            if total is not None:
                await send(
                    cid,
                    tx(la, "a_setcredit_ok", uid=tuid, n=n),
                    kb_admin(la),
                )
            else:
                await send(
                    cid, tx(la, "a_notfound"), kb_admin(la)
                )
        else:
            await send(
                cid, tx(la, "a_credit_fail"), kb_admin(la)
            )
        return

    if st == "a_ban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await ban_user(db, int(text))
            if ok:
                await send(
                    cid,
                    tx(la, "a_ban_ok", uid=text),
                    kb_admin(la),
                )
            else:
                await send(
                    cid, tx(la, "a_notfound"), kb_admin(la)
                )
        else:
            await send(cid, tx(la, "a_notfound"), kb_admin(la))
        return

    if st == "a_unban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await unban_user(db, int(text))
            if ok:
                await send(
                    cid,
                    tx(la, "a_unban_ok", uid=text),
                    kb_admin(la),
                )
            else:
                await send(
                    cid, tx(la, "a_notfound"), kb_admin(la)
                )
        else:
            await send(cid, tx(la, "a_notfound"), kb_admin(la))
        return

    if st == "a_lookup" and ia:
        sdel(uid)
        if text.isdigit():
            tu = await lookup_user(db, int(text))
            if tu:
                await send(
                    cid,
                    tx(
                        la,
                        "a_user_info",
                        uid=tu.id,
                        name=tu.first_name or "?",
                        uname=tu.username or "—",
                        cr=tu.credits,
                        used=tu.total_used,
                        ban="🚫 Yes" if tu.is_banned else "✅ No",
                        date=(
                            tu.joined.strftime("%Y-%m-%d")
                            if tu.joined
                            else "?"
                        ),
                    ),
                    kb_admin(la),
                )
            else:
                await send(
                    cid, tx(la, "a_notfound"), kb_admin(la)
                )
        else:
            await send(cid, tx(la, "a_notfound"), kb_admin(la))
        return

    if st == "a_bcast" and ia:
        sdel(uid)
        bg.add_task(bg_broadcast, uid, cid, text, la)
        return

    # ── Commands ──
    cmd = text.split()[0].lower() if text else ""

    if cmd in ["/start", "start"]:
        await send(
            cid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )

    elif cmd in ["/help"]:
        await send(
            cid,
            tx(la, "help", cr=DEFAULT_CREDITS),
            kb_back(la),
        )

    elif cmd in ["/osint"]:
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        sset(uid, "osint")
        await send(cid, tx(la, "osint_ask"), kb_back(la))

    elif cmd in ["/stalk"]:
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        s = await get_auth_session(db, uid)
        if not s:
            await send(cid, tx(la, "not_logged"))
            return
        sset(uid, "stalk")
        await send(cid, tx(la, "stalk_ask"), kb_back(la))

    elif cmd in ["/cleanup"]:
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        s = await get_auth_session(db, uid)
        if not s:
            await send(cid, tx(la, "clean_info"), kb_back(la))
            return
        await send(cid, tx(la, "ethical"), kb_eth(la))

    elif cmd in ["/login"]:
        sset(uid, "phone")
        await send(cid, tx(la, "phone_ask"))

    elif cmd in ["/logout"]:
        bg.add_task(bg_logout, uid, cid, la)

    elif cmd in ["/profile"]:
        s = await get_auth_session(db, uid)
        await send(
            cid,
            tx(
                la,
                "profile",
                uid=uid,
                name=fname or uname or "?",
                cr=u.credits,
                used=u.total_used,
                login="✅" if s else "❌",
                date=(
                    u.joined.strftime("%Y-%m-%d")
                    if u.joined
                    else "?"
                ),
            ),
            kb_back(la),
        )

    elif cmd in ["/lang"]:
        u.lang = "en" if u.lang == "fa" else "fa"
        await db.commit()
        await send(
            cid,
            tx(u.lang, "welcome", cr=u.credits, used=u.total_used),
            kb_main(u.lang, ia),
        )

    elif cmd in ["/admin"] and ia:
        total, banned, logged, credits = await get_stats(db)
        await send(
            cid,
            tx(
                la,
                "admin_panel",
                total=total,
                banned=banned,
                logged=logged,
                credits=credits,
            ),
            kb_admin(la),
        )

    else:
        await send(
            cid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )


# ══════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════

async def on_cb(db: AsyncSession, cb: dict, bg: BackgroundTasks):
    cbid = cb.get("id", "")
    uid = cb.get("from", {}).get("id")
    fname = cb.get("from", {}).get("first_name", "")
    uname = cb.get("from", {}).get("username", "")
    cid = cb.get("message", {}).get("chat", {}).get("id")
    mid = cb.get("message", {}).get("message_id")
    data = cb.get("data", "")

    if not uid or not cid:
        return

    await answer(cbid)
    u = await get_user(db, uid, uname, fname)
    la = u.lang
    ia = u.is_admin or uid in ADMIN_IDS

    if u.is_banned:
        await send(cid, tx(la, "banned"))
        return

    # ── Menu ──
    if data == "main":
        sdel(uid)
        await edit(
            cid,
            mid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )

    elif data == "osint":
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        sset(uid, "osint")
        await edit(cid, mid, tx(la, "osint_ask"), kb_back(la))

    elif data == "stalk":
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        s = await get_auth_session(db, uid)
        if not s:
            await edit(
                cid, mid, tx(la, "not_logged"), kb_back(la)
            )
            return
        sset(uid, "stalk")
        await edit(cid, mid, tx(la, "stalk_ask"), kb_back(la))

    elif data == "clean":
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        s = await get_auth_session(db, uid)
        if not s:
            await edit(
                cid, mid, tx(la, "clean_info"), kb_back(la)
            )
            return
        await edit(cid, mid, tx(la, "ethical"), kb_eth(la))

    elif data == "do_login":
        sset(uid, "phone")
        await edit(cid, mid, tx(la, "phone_ask"), kb_back(la))

    elif data == "do_logout":
        bg.add_task(bg_logout, uid, cid, la)

    elif data == "prof":
        s = await get_auth_session(db, uid)
        await edit(
            cid,
            mid,
            tx(
                la,
                "profile",
                uid=uid,
                name=fname or uname or "?",
                cr=u.credits,
                used=u.total_used,
                login="✅" if s else "❌",
                date=(
                    u.joined.strftime("%Y-%m-%d")
                    if u.joined
                    else "?"
                ),
            ),
            kb_back(la),
        )

    elif data == "help":
        await edit(
            cid,
            mid,
            tx(la, "help", cr=DEFAULT_CREDITS),
            kb_back(la),
        )

    elif data == "lang":
        u.lang = "en" if u.lang == "fa" else "fa"
        await db.commit()
        la = u.lang
        await edit(
            cid,
            mid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )

    elif data == "eth_y":
        await edit(cid, mid, "🧹", kb_clean(la))

    elif data == "eth_n":
        await edit(
            cid,
            mid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )

    elif data == "cl_dry":
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        await use_credit(db, uid)
        bg.add_task(bg_dry, uid, cid, la)

    elif data == "cl_real":
        await edit(cid, mid, tx(la, "confirm"), kb_confirm(la))

    elif data == "cf_y":
        if u.credits <= 0:
            await send(cid, tx(la, "no_credit"))
            return
        await use_credit(db, uid)
        bg.add_task(bg_real, uid, cid, la)

    elif data == "cf_n":
        await edit(
            cid,
            mid,
            tx(la, "welcome", cr=u.credits, used=u.total_used),
            kb_main(la, ia),
        )

    # ── Admin ──
    elif data == "admin" and ia:
        total, banned, logged, credits = await get_stats(db)
        await edit(
            cid,
            mid,
            tx(
                la,
                "admin_panel",
                total=total,
                banned=banned,
                logged=logged,
                credits=credits,
            ),
            kb_admin(la),
        )

    elif data == "a_credit" and ia:
        sset(uid, "a_credit")
        await edit(
            cid, mid, tx(la, "a_credit_ask"), kb_back(la)
        )

    elif data == "a_setcr" and ia:
        sset(uid, "a_setcr")
        await edit(
            cid, mid, tx(la, "a_setcredit_ask"), kb_back(la)
        )

    elif data == "a_ban" and ia:
        sset(uid, "a_ban")
        await edit(cid, mid, tx(la, "a_ban_ask"), kb_back(la))

    elif data == "a_unban" and ia:
        sset(uid, "a_unban")
        await edit(
            cid, mid, tx(la, "a_unban_ask"), kb_back(la)
        )

    elif data == "a_lookup" and ia:
        sset(uid, "a_lookup")
        await edit(
            cid, mid, tx(la, "a_lookup_ask"), kb_back(la)
        )

    elif data == "a_bcast" and ia:
        sset(uid, "a_bcast")
        await edit(
            cid, mid, tx(la, "a_broadcast_ask"), kb_back(la)
        )

    elif data == "a_stats" and ia:
        total, banned, logged, credits = await get_stats(db)
        await edit(
            cid,
            mid,
            tx(
                la,
                "admin_panel",
                total=total,
                banned=banned,
                logged=logged,
                credits=credits,
            ),
            kb_admin(la),
        )


# ══════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════

@asynccontextmanager
async def lifespan(a):
    print("🚀 Starting ShadowClean Bot v2.0...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Database ready!")
    print(f"✅ Admin IDs: {ADMIN_IDS}")
    print(f"✅ Default credits: {DEFAULT_CREDITS}")
    print(f"✅ Port: {PORT}")
    print("✅ Bot is running!")
    yield
    for c in clients.values():
        try:
            await c.disconnect()
        except Exception:
            pass
    await engine.dispose()
    print("🛑 Bot stopped")


app = FastAPI(title="ShadowClean Bot v2.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bot": "ShadowClean v2.0",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def root():
    return {"status": "running", "bot": "ShadowClean Bot v2.0"}


@app.post("/webhook")
async def webhook(request: dict, bg: BackgroundTasks):
    async with DBS() as db:
        try:
            if "message" in request:
                await on_msg(db, request["message"], bg)
            elif "callback_query" in request:
                await on_cb(db, request["callback_query"], bg)
        except Exception as e:
            print(f"❌ {e}\n{traceback.format_exc()}")
    return {"ok": True}


# ══════════════════════════════════════════
# RUN
# ══════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
