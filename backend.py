"""
══════════════════════════════════════════
  ShadowClean Bot v4.0
  ⚠️ PERSONAL USE ONLY
══════════════════════════════════════════
"""

import os, sys, json, time, asyncio, traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from contextlib import asynccontextmanager

import httpx
import uvicorn
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime,
    ForeignKey, select, delete, and_
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    Channel, Chat, User as TUser,
    PeerChannel, PeerChat, PeerUser
)
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError
)

load_dotenv()

# ══════════════════════════════
# CONFIG
# ══════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB_URL = os.getenv("DATABASE_URL", "")
FERNET_KEY = os.getenv("FERNET_KEY", Fernet.generate_key().decode())
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PORT = int(os.getenv("PORT", "8000"))
DEFAULT_CREDITS = 3

if not all([BOT_TOKEN, API_ID, API_HASH, DB_URL]):
    print("❌ Set: BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, DATABASE_URL")
    sys.exit(1)

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

# ══════════════════════════════
# DATABASE
# ══════════════════════════════
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
    joined = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sessions = relationship("SessionDB", back_populates="user", cascade="all, delete-orphan")

class SessionDB(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    phone = Column(String(50))
    enc_session = Column(Text)
    phone_hash = Column(String(255))
    authorized = Column(Boolean, default=False)
    expires = Column(DateTime(timezone=True))
    user = relationship("UserDB", back_populates="sessions")

engine = create_async_engine(DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)
DBS = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ══════════════════════════════
# STATE
# ══════════════════════════════
user_states: Dict[int, Dict] = {}

def sset(uid, state, **kw):
    user_states[uid] = {"s": state, **kw}

def sget(uid):
    d = user_states.get(uid, {})
    return d.get("s"), d

def sdel(uid):
    user_states.pop(uid, None)

# ══════════════════════════════
# TELEGRAM API
# ══════════════════════════════
async def tg(method, **kw):
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BOT_API}/{method}", json=kw)
            return r.json()
    except:
        return {"ok": False}

async def send(cid, text, markup=None):
    p = {"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup: p["reply_markup"] = markup
    return await tg("sendMessage", **p)

async def edit(cid, mid, text, markup=None):
    p = {"chat_id": cid, "message_id": mid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup: p["reply_markup"] = markup
    try: return await tg("editMessageText", **p)
    except: return await send(cid, text, markup)

async def answer(cbid, text=""):
    return await tg("answerCallbackQuery", callback_query_id=cbid, text=text)

# ══════════════════════════════
# KEYBOARDS
# ══════════════════════════════
def kb_main(la, is_admin=False):
    if la == "en":
        rows = [
            ["🔍 OSINT", "👁 Stalk"],
            ["🧹 My Footprint", "👤 Profile"],
            ["📱 Login", "❓ Help"],
        ]
        if is_admin: rows.append(["👑 Admin"])
    else:
        rows = [
            ["🔍 جستجو", "👁 استاک"],
            ["🧹 ردپای من", "👤 پروفایل"],
            ["📱 ورود", "❓ راهنما"],
        ]
        if is_admin: rows.append(["👑 مدیریت"])
    return {"keyboard": rows, "resize_keyboard": True}

def kb_back(la):
    return {"keyboard": [["🔙 بازگشت" if la=="fa" else "🔙 Back"]], "resize_keyboard": True}

def kb_admin_menu(la):
    if la == "en":
        return {"keyboard": [
            ["💎 Add Credits", "🔧 Set Credits"],
            ["🔎 Lookup", "🚫 Ban"],
            ["✅ Unban", "📢 Broadcast"],
            ["🔙 Back"],
        ], "resize_keyboard": True}
    return {"keyboard": [
        ["💎 اعتبار", "🔧 تنظیم اعتبار"],
        ["🔎 جستجو کاربر", "🚫 بن"],
        ["✅ آنبن", "📢 پیام همگانی"],
        ["🔙 بازگشت"],
    ], "resize_keyboard": True}

def kb_groups_list(groups, page=0, per_page=8, prefix="grp"):
    start = page * per_page
    chunk = groups[start:start+per_page]
    rows = []
    for g in chunk:
        title = g["title"][:28]
        cnt = g.get("count", 0)
        rows.append([{"text": f"📂 {title} ({cnt})", "callback_data": f"{prefix}_{g['id']}"}])
    nav = []
    if page > 0: nav.append({"text": "⬅️", "callback_data": f"{prefix}p_{page-1}"})
    if start+per_page < len(groups): nav.append({"text": "➡️", "callback_data": f"{prefix}p_{page+1}"})
    if nav: rows.append(nav)
    rows.append([{"text": "🔙", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def kb_footprint_actions(la):
    if la == "en":
        return {"inline_keyboard": [
            [{"text": "📊 Scan (no delete)", "callback_data": "fp_scan"}],
            [{"text": "🗑️ DELETE ALL MY MESSAGES", "callback_data": "fp_delete"}],
            [{"text": "🔙 Back", "callback_data": "back_main"}],
        ]}
    return {"inline_keyboard": [
        [{"text": "📊 اسکن (بدون حذف)", "callback_data": "fp_scan"}],
        [{"text": "🗑️ حذف همه پیام‌های من", "callback_data": "fp_delete"}],
        [{"text": "🔙 بازگشت", "callback_data": "back_main"}],
    ]}

def kb_confirm(la):
    if la == "en":
        return {"inline_keyboard": [[
            {"text": "✅ Yes DELETE", "callback_data": "fp_confirm_yes"},
            {"text": "❌ Cancel", "callback_data": "back_main"},
        ]]}
    return {"inline_keyboard": [[
        {"text": "✅ بله حذف کن", "callback_data": "fp_confirm_yes"},
        {"text": "❌ انصراف", "callback_data": "back_main"},
    ]]}

# ══════════════════════════════
# TEXTS
# ══════════════════════════════
T = {
  "fa": {
    "welcome": "🌑 <b>ShadowClean Bot</b>\n\n🔍 جستجو - اطلاعات عمومی\n👁 استاک - پیام‌های کاربر در گروه‌ها\n🧹 ردپای من - مدیریت پیام‌هام\n\n💎 اعتبار: <b>{cr}</b>",
    "help": "❓ <b>راهنما</b>\n\n🔍 جستجو - OSINT عمومی\n👁 استاک - پیام‌های هدف در گروه‌های مشترک\n🧹 ردپای من - اسکن و حذف پیام‌های خودم\n📱 ورود - لاگین برای امکانات پیشرفته\n\n💎 {cr} درخواست رایگان",
    "no_credit": "❌ اعتبار تمام! با پشتیبانی تماس بگیرید.",
    "osint_ask": "🔍 @username یا آیدی عددی بفرستید:",
    "stalk_ask": "👁 <b>استاک</b>\n\n@username یا آیدی عددی هدف:\n\n⚠️ نیاز به لاگین (📱 ورود)",
    "phone_ask": "📱 شماره با کد کشور:\n<code>+989121234567</code>\n\n🔐 AES-256 | ⏰ حذف ۲۴ ساعته",
    "code_ask": "📨 کد تأیید:",
    "2fa_ask": "🔐 رمز دوم:",
    "login_ok": "✅ ورود موفق!",
    "login_fail": "❌ خطا: {e}",
    "logout_ok": "✅ خارج شدید.",
    "not_logged": "❌ ابتدا 📱 ورود بزنید",
    "profile": "👤 <b>پروفایل</b>\n\n🆔 <code>{uid}</code>\n👤 {name}\n💎 اعتبار: <b>{cr}</b>\n📊 استفاده: {used}\n🔐 {login}\n📅 {date}",
    "processing": "⏳ صبر کنید...",
    "error": "❌ خطا: {e}",
    "banned": "🚫 مسدود شدید. با پشتیبانی تماس بگیرید.",
    "osint_res": "🔍 <b>نتیجه</b>\n\n👤 {name}\n🆔 <code>{uid}</code>\n📛 {uname}\n📸 {photo}\nℹ️ {bio}\n⏰ {seen}",
    "stalk_panel": "👁 <b>استاک: {name}</b>\n\n📂 گروه‌ها: <b>{gr}</b>\n📢 کانال‌ها: <b>{ch}</b>\n💬 کل پیام‌ها: <b>{msgs}</b>\n\nانتخاب کنید:",
    "stalk_msgs_header": "👁 <b>{name} در {group}</b>\n\n",
    "no_msgs": "💬 پیامی یافت نشد.",
    "not_found": "❌ کاربر یافت نشد. مطمئن شوید یوزرنیم یا آیدی درسته.",
    "footprint_info": "🧹 <b>ردپای دیجیتال من</b>\n\n📂 گروه‌هایی که پیام دارم: <b>{gr}</b>\n💬 کل پیام‌ها: <b>{msgs}</b>\n📸 مدیا: <b>{md}</b>\n📝 متن: <b>{tx}</b>\n\nچه کاری انجام بدم؟",
    "footprint_scanning": "📊 اسکن گروه‌ها... {pct}%\n📂 {name}",
    "footprint_scan_done": "📊 <b>نتیجه اسکن</b>\n\n📂 گروه‌ها: <b>{gr}</b>\n💬 پیام‌ها: <b>{msgs}</b>\n📸 مدیا: <b>{md}</b>\n📝 متن: <b>{tx}</b>",
    "footprint_confirm": "⚠️ <b>هشدار!</b>\n\n🗑️ همه پیام‌های شما از <b>{gr}</b> گروه حذف میشه!\n💬 تعداد: <b>{msgs}</b> پیام\n\n<b>این عمل برگشت‌ناپذیره!</b>\n\nمطمئنید؟",
    "footprint_deleting": "🗑️ حذف... {pct}%\n✅ {done} حذف شده\n📂 {name}",
    "footprint_done": "✅ <b>پاکسازی کامل!</b>\n\n🗑️ حذف شده: <b>{done}</b>\n📂 گروه‌ها: <b>{gr}</b>\n⏱️ {time}\n❌ خطا: {err}",
    "need_login_footprint": "🧹 <b>ردپای من</b>\n\nبرای دیدن و حذف پیام‌هاتون باید اول لاگین کنید.\n\n📱 دکمه ورود رو بزنید.",
    # Admin
    "admin_panel": "👑 <b>مدیریت</b>\n\n👥 {total} | 🚫 {banned} | 🔐 {logged}",
    "a_credit_ask": "💎 <code>آیدی تعداد</code>\nمثال: <code>123456 10</code>",
    "a_credit_ok": "✅ +{n} به {uid} (فعلی: {total})",
    "a_credit_fail": "❌ فرمت: <code>آیدی تعداد</code>",
    "a_setcr_ask": "🔧 <code>آیدی تعداد</code>",
    "a_setcr_ok": "✅ {uid} = {n}",
    "a_ban_ask": "🚫 آیدی:", "a_ban_ok": "✅ {uid} بن شد.",
    "a_unban_ask": "✅ آیدی:", "a_unban_ok": "✅ {uid} آنبن شد.",
    "a_notfound": "❌ یافت نشد!",
    "a_lookup_ask": "🔎 آیدی:",
    "a_user_info": "📊 <code>{uid}</code> | {name} | @{uname} | 💎{cr} | 📊{used} | {ban} | {date}",
    "a_bcast_ask": "📢 متن:", "a_bcast_ok": "✅ ارسال به {n} نفر.",
  },
  "en": {
    "welcome": "🌑 <b>ShadowClean</b>\n\n🔍 Search - Public info\n👁 Stalk - User msgs in groups\n🧹 My Footprint - Manage my msgs\n\n💎 Credits: <b>{cr}</b>",
    "help": "❓ 🔍Search 👁Stalk 🧹Footprint 📱Login\n💎 {cr} free credits",
    "no_credit": "❌ No credits! Contact support.",
    "osint_ask": "🔍 Send @username or ID:",
    "stalk_ask": "👁 Send target @username or ID:\n⚠️ Login required",
    "phone_ask": "📱 Phone: <code>+989121234567</code>",
    "code_ask": "📨 Code:", "2fa_ask": "🔐 2FA:",
    "login_ok": "✅ OK!", "login_fail": "❌ {e}",
    "logout_ok": "✅ Out.", "not_logged": "❌ Login first",
    "profile": "👤 {uid} | {name} | 💎{cr} | 📊{used} | {login} | {date}",
    "processing": "⏳...", "error": "❌ {e}",
    "banned": "🚫 Banned.",
    "osint_res": "🔍 {name} | <code>{uid}</code> | {uname} | {photo} | {bio} | {seen}",
    "stalk_panel": "👁 <b>{name}</b>\n📂{gr} 📢{ch} 💬{msgs}\nSelect:",
    "stalk_msgs_header": "👁 <b>{name} in {group}</b>\n\n",
    "no_msgs": "💬 No messages.", "not_found": "❌ Not found. Check username/ID.",
    "footprint_info": "🧹 <b>My Footprint</b>\n\n📂 Groups: {gr}\n💬 Messages: {msgs}\n📸 Media: {md}\n📝 Text: {tx}",
    "footprint_scanning": "📊 Scanning... {pct}%\n📂 {name}",
    "footprint_scan_done": "📊 Groups:{gr} Msgs:{msgs} Media:{md} Text:{tx}",
    "footprint_confirm": "⚠️ Delete {msgs} messages from {gr} groups?\nIRREVERSIBLE!",
    "footprint_deleting": "🗑️ {pct}% | {done} deleted | {name}",
    "footprint_done": "✅ Deleted:{done} Groups:{gr} Time:{time} Errors:{err}",
    "need_login_footprint": "🧹 Login first to see/delete your messages.",
    "admin_panel": "👑 {total} | 🚫{banned} | 🔐{logged}",
    "a_credit_ask": "💎 <code>ID amount</code>", "a_credit_ok": "✅ +{n} {uid} ({total})",
    "a_credit_fail": "❌ <code>ID amount</code>",
    "a_setcr_ask": "🔧 <code>ID amount</code>", "a_setcr_ok": "✅ {uid}={n}",
    "a_ban_ask": "🚫 ID:", "a_ban_ok": "✅ {uid} banned.",
    "a_unban_ask": "✅ ID:", "a_unban_ok": "✅ {uid} unbanned.",
    "a_notfound": "❌ Not found!",
    "a_lookup_ask": "🔎 ID:",
    "a_user_info": "📊 {uid}|{name}|@{uname}|💎{cr}|📊{used}|{ban}|{date}",
    "a_bcast_ask": "📢 Text:", "a_bcast_ok": "✅ Sent to {n}.",
  }
}

def tx(la, key, **kw):
    txt = T.get(la, T["fa"]).get(key, T["fa"].get(key, key))
    try: return txt.format(**kw) if kw else txt
    except: return txt

# ══════════════════════════════
# DB HELPERS
# ══════════════════════════════
async def get_user(db, uid, uname="", fname=""):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u:
        u = UserDB(id=uid, username=uname, first_name=fname,
                    credits=DEFAULT_CREDITS, is_admin=uid in ADMIN_IDS)
        db.add(u); await db.commit(); await db.refresh(u)
    else:
        ch = False
        if uname and u.username != uname: u.username = uname; ch = True
        if fname and u.first_name != fname: u.first_name = fname; ch = True
        if uid in ADMIN_IDS and not u.is_admin: u.is_admin = True; ch = True
        if ch: await db.commit()
    return u

async def has_credit(u):
    return True if (u.is_admin or u.id in ADMIN_IDS) else u.credits > 0

async def use_credit(db, uid):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u: return False
    if u.is_admin or u.id in ADMIN_IDS:
        u.total_used += 1; await db.commit(); return True
    if u.credits <= 0: return False
    u.credits -= 1; u.total_used += 1; await db.commit(); return True

async def add_credits(db, uid, n):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u: return None
    u.credits += n; await db.commit(); return u.credits

async def set_credits(db, uid, n):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u: return None
    u.credits = n; await db.commit(); return u.credits

async def ban_user(db, uid):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u: return False
    u.is_banned = True; await db.commit(); return True

async def unban_user(db, uid):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    u = r.scalar_one_or_none()
    if not u: return False
    u.is_banned = False; await db.commit(); return True

async def lookup_user(db, uid):
    r = await db.execute(select(UserDB).where(UserDB.id == uid))
    return r.scalar_one_or_none()

async def get_all_users(db):
    r = await db.execute(select(UserDB)); return r.scalars().all()

async def get_stats(db):
    users = await get_all_users(db)
    total = len(users); banned = sum(1 for u in users if u.is_banned)
    r2 = await db.execute(select(SessionDB).where(SessionDB.authorized == True))
    logged = len(r2.scalars().all())
    return total, banned, logged

async def get_auth_session(db, uid):
    r = await db.execute(select(SessionDB).where(and_(
        SessionDB.user_id == uid, SessionDB.authorized == True,
        SessionDB.expires > datetime.now(timezone.utc))))
    return r.scalar_one_or_none()

async def get_any_sess(db, uid):
    r = await db.execute(select(SessionDB).where(SessionDB.user_id == uid))
    return r.scalar_one_or_none()

async def save_sess(db, uid, phone, ss, ph):
    await db.execute(delete(SessionDB).where(SessionDB.user_id == uid))
    s = SessionDB(user_id=uid, phone=phone,
                   enc_session=fernet.encrypt(ss.encode()).decode(),
                   phone_hash=ph, expires=datetime.now(timezone.utc)+timedelta(hours=24))
    db.add(s); await db.commit()

async def auth_sess(db, uid, ss):
    r = await db.execute(select(SessionDB).where(SessionDB.user_id == uid))
    s = r.scalar_one_or_none()
    if s: s.enc_session = fernet.encrypt(ss.encode()).decode(); s.authorized = True; await db.commit()

async def del_sess(db, uid):
    await db.execute(delete(SessionDB).where(SessionDB.user_id == uid)); await db.commit()

async def dec_sess(db, uid):
    s = await get_auth_session(db, uid)
    if s and s.enc_session: return fernet.decrypt(s.enc_session.encode()).decode()
    return None

# ══════════════════════════════
# TELETHON
# ══════════════════════════════
clients: Dict[int, TelegramClient] = {}

async def tclient(uid, ss):
    if uid in clients and clients[uid].is_connected(): return clients[uid]
    c = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await c.connect(); clients[uid] = c; return c

async def tnew():
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect(); return c

# ══════════════════════════════
# RESOLVE TARGET (fix not found)
# ══════════════════════════════
async def resolve_target(client, target_str):
    """Try multiple ways to find the user."""
    target_str = target_str.strip()

    # Remove @ if present
    if target_str.startswith("@"):
        target_str = target_str[1:]

    # Try as username
    try:
        entity = await client.get_entity(target_str)
        return entity
    except:
        pass

    # Try as numeric ID
    try:
        uid = int(target_str)
        entity = await client.get_entity(PeerUser(uid))
        return entity
    except:
        pass

    # Try with @
    try:
        entity = await client.get_entity(f"@{target_str}")
        return entity
    except:
        pass

    # Try get_input_entity
    try:
        uid = int(target_str)
        # Search in dialogs
        async for dialog in client.iter_dialogs(limit=500):
            if hasattr(dialog.entity, 'id') and dialog.entity.id == uid:
                return dialog.entity
    except:
        pass

    return None

# ══════════════════════════════
# BUILD MESSAGE LINK
# ══════════════════════════════
def make_link(entity, msg_id):
    """Build clickable link to message."""
    uname = getattr(entity, 'username', None)
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    else:
        eid = getattr(entity, 'id', 0)
        return f"https://t.me/c/{eid}/{msg_id}"

# ══════════════════════════════
# OSINT (light search via bot API)
# ══════════════════════════════
async def osint_light(target):
    # Try username
    t = target.strip()
    if not t.startswith("@") and not t.isdigit():
        t = "@" + t
    r = await tg("getChat", chat_id=t)
    if r.get("ok"):
        c = r["result"]
        pr = await tg("getUserProfilePhotos", user_id=c.get("id",0), limit=1)
        pc = pr.get("result",{}).get("total_count",0) if pr.get("ok") else 0
        return {"uid":c.get("id"), "name":f'{c.get("first_name","")} {c.get("last_name","")}'.strip(),
                "uname":c.get("username",""), "bio":c.get("bio","—"), "photo":"✅" if pc else "❌"}
    return None

async def osint_full(client, target_str):
    entity = await resolve_target(client, target_str)
    if not entity: return None
    try:
        full = await client(GetFullUserRequest(entity))
        seen = "?"
        if hasattr(entity,'status') and entity.status:
            if hasattr(entity.status,'was_online'): seen = str(entity.status.was_online)
            else: seen = type(entity.status).__name__.replace("UserStatus","")
        commons = []
        try:
            cr = await client(functions.messages.GetCommonChatsRequest(user_id=entity,max_id=0,limit=100))
            commons = [{"id":c.id, "title":getattr(c,'title','?')} for c in cr.chats]
        except: pass
        return {"uid":entity.id,
                "name":f'{getattr(entity,"first_name","") or ""} {getattr(entity,"last_name","") or ""}'.strip(),
                "uname":getattr(entity,'username',''),
                "bio":getattr(full.full_user,'about','') or '—',
                "photo":"✅" if entity.photo else "❌", "seen":seen, "commons":commons}
    except:
        return None

# ══════════════════════════════
# STALK ENGINE (search others)
# ══════════════════════════════
async def stalk_collect(client, target_entity, cid, la):
    """Find target's messages in all shared groups/channels."""
    target_id = target_entity.id
    result = {"groups": [], "channels": [], "total": 0}

    try:
        dlg = await client.get_dialogs(limit=500)
        chats = []
        for d in dlg:
            ent = d.entity
            if isinstance(ent, Channel):
                chats.append(d)
            elif isinstance(ent, Chat):
                chats.append(d)

        pm = await send(cid, tx(la, "processing"))
        pmid = pm.get("result",{}).get("message_id")

        for i, d in enumerate(chats):
            cnt = 0
            try:
                async for msg in client.iter_messages(d.entity, from_user=target_id, limit=200):
                    cnt += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                continue
            except:
                continue

            if cnt > 0:
                info = {"id": d.entity.id, "title": getattr(d.entity,'title','?'), "count": cnt}
                is_broadcast = getattr(d.entity, 'broadcast', False)
                is_mega = getattr(d.entity, 'megagroup', False)

                if is_broadcast and not is_mega:
                    result["channels"].append(info)
                else:
                    result["groups"].append(info)
                result["total"] += cnt

            if pmid and (i+1) % 10 == 0:
                pct = int((i+1)/len(chats)*100)
                try: await edit(cid, pmid, f"👁 {pct}% | {len(result['groups'])+len(result['channels'])} found...")
                except: pass

        if pmid:
            try: await edit(cid, pmid, "✅")
            except: pass

    except Exception as e:
        print(f"stalk_collect error: {e}")
    return result

async def get_group_messages(client, target_id, group_id, limit=30):
    """Get messages from target in a specific group with links."""
    messages = []
    try:
        entity = await client.get_entity(PeerChannel(group_id))
    except:
        try:
            entity = await client.get_entity(group_id)
        except:
            return messages

    try:
        async for msg in client.iter_messages(entity, from_user=target_id, limit=limit):
            text_preview = ""
            if msg.text:
                text_preview = msg.text[:200].replace("<","&lt;").replace(">","&gt;")
            elif msg.media:
                text_preview = "📎 [Media/File]"
            else:
                text_preview = "..."

            link = make_link(entity, msg.id)
            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"

            messages.append({
                "text": text_preview,
                "date": date_str,
                "link": link,
            })
    except Exception as e:
        print(f"get_group_messages error: {e}")
    return messages

# ══════════════════════════════
# FOOTPRINT ENGINE (my own msgs)
# ══════════════════════════════
async def footprint_scan(client, cid, la):
    """Scan my own messages in all groups."""
    res = {"groups": [], "total": 0, "media": 0, "text": 0}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [d for d in dlg if isinstance(d.entity, Channel) and getattr(d.entity, 'megagroup', False)]

        pm = await send(cid, tx(la, "processing"))
        pmid = pm.get("result",{}).get("message_id")

        for i, d in enumerate(sgs):
            gc = gm = gt = 0
            try:
                async for m in client.iter_messages(d.entity, from_user=me.id):
                    gc += 1
                    if m.media: gm += 1
                    else: gt += 1
                if gc:
                    res["groups"].append({"id": d.entity.id, "title": d.entity.title, "count": gc, "media": gm, "text": gt})
                    res["total"] += gc; res["media"] += gm; res["text"] += gt
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except: continue

            if pmid and (i+1) % 3 == 0:
                pct = int((i+1)/len(sgs)*100)
                try: await edit(cid, pmid, tx(la, "footprint_scanning", pct=pct, name=d.entity.title))
                except: pass
    except: pass
    return res

async def footprint_delete(client, cid, la):
    """Delete all my messages from all supergroups."""
    res = {"done": 0, "err": 0, "gr": 0, "det": []}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [d for d in dlg if isinstance(d.entity, Channel) and getattr(d.entity, 'megagroup', False)]

        pm = await send(cid, tx(la, "processing"))
        pmid = pm.get("result",{}).get("message_id")
        start = time.time()

        for i, d in enumerate(sgs):
            ids = []
            try:
                async for m in client.iter_messages(d.entity, from_user=me.id):
                    ids.append(m.id)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except: continue

            if not ids: continue
            gd = ge = 0
            for j in range(0, len(ids), 50):
                batch = ids[j:j+50]
                try:
                    await client.delete_messages(d.entity, batch, revoke=True)
                    gd += len(batch); await asyncio.sleep(1)
                except FloodWaitError as e:
                    await asyncio.sleep(int(e.seconds * 1.5))
                    try:
                        await client.delete_messages(d.entity, batch, revoke=True)
                        gd += len(batch)
                    except: ge += len(batch)
                except: ge += len(batch)

            res["done"] += gd; res["err"] += ge
            if gd: res["gr"] += 1; res["det"].append(f"{d.entity.title}: {gd}")

            if pmid:
                pct = int((i+1)/len(sgs)*100)
                try: await edit(cid, pmid, tx(la, "footprint_deleting", pct=pct, done=res["done"], name=d.entity.title))
                except: pass
    except: pass
    return res

# ══════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════
async def bg_osint(uid, cid, target, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        r = None
        if ss:
            client = await tclient(uid, ss)
            r = await osint_full(client, target)
        if not r:
            r = await osint_light(target)
        if r:
            txt = tx(la,"osint_res", name=r.get("name","?"), uid=r.get("uid","?"),
                uname=f'@{r["uname"]}' if r.get("uname") else "—",
                photo=r.get("photo","?"), bio=r.get("bio","—"), seen=r.get("seen","—"))
            if r.get("commons"):
                txt += "\n\n📂 مشترک:\n" + "\n".join(f"  • {c['title']}" for c in r["commons"][:10])
            await send(cid, txt)
        else:
            await send(cid, tx(la, "not_found"))

async def bg_stalk(uid, cid, target_str, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)

        target_entity = await resolve_target(client, target_str)
        if not target_entity:
            await send(cid, tx(la, "not_found"))
            return

        target_id = target_entity.id
        target_name = f'{getattr(target_entity,"first_name","") or ""} {getattr(target_entity,"last_name","") or ""}'.strip() or str(target_id)

        result = await stalk_collect(client, target_entity, cid, la)

        all_items = result["groups"] + result["channels"]

        if not all_items:
            await send(cid, tx(la, "no_msgs"))
            return

        sset(uid, "stalk_view", target_id=target_id, target_name=target_name,
             items=all_items, groups=result["groups"], channels=result["channels"])

        txt = tx(la, "stalk_panel", name=target_name,
                 gr=len(result["groups"]), ch=len(result["channels"]),
                 msgs=result["total"])

        await send(cid, txt, kb_groups_list(all_items, 0, 8, "sg"))

async def bg_stalk_group_msgs(uid, cid, group_id, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: return
        client = await tclient(uid, ss)
        _, sd = sget(uid)
        target_id = sd.get("target_id")
        target_name = sd.get("target_name", "?")
        if not target_id: return

        try:
            entity = await client.get_entity(PeerChannel(group_id))
        except:
            try: entity = await client.get_entity(group_id)
            except: await send(cid, tx(la,"error",e="Can't access group")); return

        group_title = getattr(entity, 'title', '?')
        messages = await get_group_messages(client, target_id, group_id, limit=30)

        if not messages:
            await send(cid, tx(la, "no_msgs")); return

        # Send in chunks of 5
        for ci, chunk_start in enumerate(range(0, len(messages), 5)):
            chunk = messages[chunk_start:chunk_start+5]
            txt = ""
            if ci == 0:
                txt = tx(la, "stalk_msgs_header", name=target_name, group=group_title)

            for m in chunk:
                link_html = f'(<a href="{m["link"]}">link</a>)' if m["link"] else ""
                txt += f'📅 <code>{m["date"]}</code> {link_html}\n💬 {m["text"]}\n{"─"*25}\n'

            await send(cid, txt)
            await asyncio.sleep(0.3)

async def bg_footprint_scan(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)
        r = await footprint_scan(client, cid, la)

        sset(uid, "fp_scanned", scan_result=r)

        txt = tx(la, "footprint_scan_done", gr=len(r["groups"]),
                 msgs=r["total"], md=r["media"], tx=r["text"])

        if r["groups"]:
            txt += "\n\n"
            for g in r["groups"][:20]:
                txt += f"• {g['title']}: {g['count']} ({g.get('media',0)}📸 {g.get('text',0)}📝)\n"

        await send(cid, txt, kb_footprint_actions(la))

async def bg_footprint_delete(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)
        start = time.time()
        r = await footprint_delete(client, cid, la)
        el = time.time() - start
        ts = f"{int(el//60)}m {int(el%60)}s"
        txt = tx(la, "footprint_done", done=r["done"], gr=r["gr"], time=ts, err=r["err"])
        if r["det"]:
            txt += "\n\n" + "\n".join(f"• {d}" for d in r["det"][:20])
        await send(cid, txt)

async def bg_footprint_info(uid, cid, la):
    """Quick scan and show footprint panel."""
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "need_login_footprint"))
            return
        client = await tclient(uid, ss)
        r = await footprint_scan(client, cid, la)

        sset(uid, "fp_scanned", scan_result=r)

        txt = tx(la, "footprint_info", gr=len(r["groups"]),
                 msgs=r["total"], md=r["media"], tx=r["text"])

        await send(cid, txt, kb_footprint_actions(la))

async def bg_login(uid, cid, phone, la):
    async with DBS() as db:
        try:
            client = await tnew()
            result = await client.send_code_request(phone)
            ss = client.session.save()
            await save_sess(db, uid, phone, ss, result.phone_code_hash)
            sset(uid, "code", phone=phone, ph=result.phone_code_hash)
            await send(cid, tx(la,"code_ask"))
            await client.disconnect()
        except Exception as e:
            await send(cid, tx(la,"login_fail",e=str(e)[:200]))

async def bg_code(uid, cid, code, la):
    async with DBS() as db:
        try:
            so = await get_any_sess(db, uid)
            if not so or not so.enc_session: await send(cid, tx(la,"login_fail",e="No session")); return
            ss = fernet.decrypt(so.enc_session.encode()).decode()
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            _, sd = sget(uid)
            try:
                await client.sign_in(phone=sd.get("phone",so.phone), code=code,
                                      phone_code_hash=sd.get("ph",so.phone_hash))
                nss = client.session.save()
                await auth_sess(db, uid, nss); sdel(uid)
                await send(cid, tx(la,"login_ok"), kb_main(la, uid in ADMIN_IDS))
            except SessionPasswordNeededError:
                nss = client.session.save()
                so.enc_session = fernet.encrypt(nss.encode()).decode(); await db.commit()
                sset(uid, "2fa"); await send(cid, tx(la,"2fa_ask"))
            finally: await client.disconnect()
        except PhoneCodeInvalidError: await send(cid, tx(la,"login_fail",e="Wrong code"))
        except PhoneCodeExpiredError: sdel(uid); await send(cid, tx(la,"login_fail",e="Expired"))
        except Exception as e: await send(cid, tx(la,"login_fail",e=str(e)[:200]))

async def bg_2fa(uid, cid, pwd, la):
    async with DBS() as db:
        try:
            so = await get_any_sess(db, uid)
            if not so or not so.enc_session: return
            ss = fernet.decrypt(so.enc_session.encode()).decode()
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            try:
                await client.sign_in(password=pwd)
                nss = client.session.save()
                await auth_sess(db, uid, nss); sdel(uid)
                await send(cid, tx(la,"login_ok"), kb_main(la, uid in ADMIN_IDS))
            finally: await client.disconnect()
        except PasswordHashInvalidError: await send(cid, tx(la,"login_fail",e="Wrong 2FA"))
        except Exception as e: await send(cid, tx(la,"login_fail",e=str(e)[:200]))

async def bg_logout(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if ss:
            try:
                c = TelegramClient(StringSession(ss), API_ID, API_HASH)
                await c.connect(); await c.log_out(); await c.disconnect()
            except: pass
        await del_sess(db, uid); clients.pop(uid, None); sdel(uid)
        await send(cid, tx(la,"logout_ok"), kb_main(la, uid in ADMIN_IDS))

async def bg_broadcast(auid, cid, text, la):
    async with DBS() as db:
        users = await get_all_users(db); n = 0
        for u in users:
            if u.id == auid: continue
            try: await send(u.id, f"📢\n\n{text}"); n+=1; await asyncio.sleep(0.1)
            except: continue
        await send(cid, tx(la,"a_bcast_ok",n=n), kb_admin_menu(la))

# ══════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════
async def on_msg(db, msg, bg: BackgroundTasks):
    cid = msg.get("chat",{}).get("id")
    uid = msg.get("from",{}).get("id")
    fname = msg.get("from",{}).get("first_name","")
    uname = msg.get("from",{}).get("username","")
    text = (msg.get("text") or "").strip()
    if not cid or not uid or msg.get("chat",{}).get("type") != "private": return

    u = await get_user(db, uid, uname, fname)
    la = u.lang; ia = u.is_admin or uid in ADMIN_IDS
    if u.is_banned: await send(cid, tx(la,"banned")); return

    st, sd = sget(uid)

    # Login flow
    if st == "code": bg.add_task(bg_code, uid, cid, text, la); return
    if st == "2fa": bg.add_task(bg_2fa, uid, cid, text, la); return
    if st == "phone":
        ph = text if text.startswith("+") else "+"+text
        bg.add_task(bg_login, uid, cid, ph, la); return

    # Search states
    if st == "osint":
        sdel(uid)
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_osint, uid, cid, text, la); return

    if st == "stalk":
        sdel(uid)
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sess = await get_auth_session(db, uid)
        if not sess: await send(cid, tx(la,"not_logged")); return
        await use_credit(db, uid)
        bg.add_task(bg_stalk, uid, cid, text, la); return

    # Admin states
    if st == "a_credit" and ia:
        sdel(uid); parts = text.split()
        if len(parts)==2 and parts[0].isdigit() and parts[1].isdigit():
            total = await add_credits(db, int(parts[0]), int(parts[1]))
            if total is not None: await send(cid, tx(la,"a_credit_ok",uid=parts[0],n=parts[1],total=total), kb_admin_menu(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la,"a_credit_fail"), kb_admin_menu(la))
        return
    if st == "a_setcr" and ia:
        sdel(uid); parts = text.split()
        if len(parts)==2 and parts[0].isdigit() and parts[1].isdigit():
            r = await set_credits(db, int(parts[0]), int(parts[1]))
            if r is not None: await send(cid, tx(la,"a_setcr_ok",uid=parts[0],n=parts[1]), kb_admin_menu(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la,"a_credit_fail"), kb_admin_menu(la))
        return
    if st == "a_ban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await ban_user(db, int(text))
            await send(cid, tx(la,"a_ban_ok",uid=text) if ok else tx(la,"a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        return
    if st == "a_unban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await unban_user(db, int(text))
            await send(cid, tx(la,"a_unban_ok",uid=text) if ok else tx(la,"a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        return
    if st == "a_lookup" and ia:
        sdel(uid)
        if text.isdigit():
            tu = await lookup_user(db, int(text))
            if tu: await send(cid, tx(la,"a_user_info",uid=tu.id,name=tu.first_name or "?",
                uname=tu.username or "—",cr=tu.credits,used=tu.total_used,
                ban="🚫" if tu.is_banned else "✅",
                date=tu.joined.strftime("%Y-%m-%d") if tu.joined else "?"), kb_admin_menu(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_menu(la))
        return
    if st == "a_bcast" and ia:
        sdel(uid); bg.add_task(bg_broadcast, uid, cid, text, la); return

    # ── Reply Keyboard Buttons ──
    if text in ["🔍 جستجو", "🔍 OSINT"]:
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sset(uid, "osint"); await send(cid, tx(la,"osint_ask"), kb_back(la)); return

    if text in ["👁 استاک", "👁 Stalk"]:
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sess = await get_auth_session(db, uid)
        if not sess: await send(cid, tx(la,"not_logged"), kb_main(la, ia)); return
        sset(uid, "stalk"); await send(cid, tx(la,"stalk_ask"), kb_back(la)); return

    if text in ["🧹 ردپای من", "🧹 My Footprint"]:
        sess = await get_auth_session(db, uid)
        if not sess:
            await send(cid, tx(la,"need_login_footprint"), kb_main(la, ia)); return
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_footprint_info, uid, cid, la); return

    if text in ["👤 پروفایل", "👤 Profile"]:
        sess = await get_auth_session(db, uid)
        await send(cid, tx(la,"profile",uid=uid,name=fname or uname or "?",
            cr="♾️" if ia else u.credits, used=u.total_used,
            login="✅" if sess else "❌",
            date=u.joined.strftime("%Y-%m-%d") if u.joined else "?"), kb_main(la, ia)); return

    if text in ["📱 ورود", "📱 Login"]:
        sset(uid, "phone"); await send(cid, tx(la,"phone_ask"), kb_back(la)); return

    if text in ["❓ راهنما", "❓ Help"]:
        await send(cid, tx(la,"help",cr=DEFAULT_CREDITS), kb_main(la, ia)); return

    if text in ["👑 مدیریت", "👑 Admin"] and ia:
        total, banned, logged = await get_stats(db)
        await send(cid, tx(la,"admin_panel",total=total,banned=banned,logged=logged), kb_admin_menu(la)); return

    if text in ["🔙 بازگشت", "🔙 Back"]:
        sdel(uid)
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return

    # Admin buttons
    if ia:
        if text in ["💎 اعتبار", "💎 Add Credits"]:
            sset(uid, "a_credit"); await send(cid, tx(la,"a_credit_ask"), kb_back(la)); return
        if text in ["🔧 تنظیم اعتبار", "🔧 Set Credits"]:
            sset(uid, "a_setcr"); await send(cid, tx(la,"a_setcr_ask"), kb_back(la)); return
        if text in ["🔎 جستجو کاربر", "🔎 Lookup"]:
            sset(uid, "a_lookup"); await send(cid, tx(la,"a_lookup_ask"), kb_back(la)); return
        if text in ["🚫 بن", "🚫 Ban"]:
            sset(uid, "a_ban"); await send(cid, tx(la,"a_ban_ask"), kb_back(la)); return
        if text in ["✅ آنبن", "✅ Unban"]:
            sset(uid, "a_unban"); await send(cid, tx(la,"a_unban_ask"), kb_back(la)); return
        if text in ["📢 پیام همگانی", "📢 Broadcast"]:
            sset(uid, "a_bcast"); await send(cid, tx(la,"a_bcast_ask"), kb_back(la)); return

    # Commands
    if text.startswith("/start"):
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return
    if text.startswith("/login"):
        sset(uid, "phone"); await send(cid, tx(la,"phone_ask"), kb_back(la)); return
    if text.startswith("/logout"):
        bg.add_task(bg_logout, uid, cid, la); return
    if text.startswith("/lang"):
        u.lang = "en" if u.lang=="fa" else "fa"; await db.commit()
        await send(cid, tx(u.lang,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(u.lang, ia)); return

    # Default
    await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia))

# ══════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════
async def on_cb(db, cb, bg: BackgroundTasks):
    cbid = cb.get("id","")
    uid = cb.get("from",{}).get("id")
    fname = cb.get("from",{}).get("first_name","")
    uname = cb.get("from",{}).get("username","")
    cid = cb.get("message",{}).get("chat",{}).get("id")
    mid = cb.get("message",{}).get("message_id")
    data = cb.get("data","")
    if not uid or not cid: return
    await answer(cbid)

    u = await get_user(db, uid, uname, fname)
    la = u.lang; ia = u.is_admin or uid in ADMIN_IDS
    if u.is_banned: return

    # ── Stalk group selection ──
    if data.startswith("sg_"):
        group_id = int(data[3:])
        bg.add_task(bg_stalk_group_msgs, uid, cid, group_id, la)
        return

    # ── Stalk pagination ──
    if data.startswith("sgp_"):
        page = int(data[4:])
        _, sd = sget(uid)
        items = sd.get("items", [])
        target_name = sd.get("target_name", "?")
        if items:
            txt = tx(la, "stalk_panel", name=target_name,
                     gr=len(sd.get("groups",[])), ch=len(sd.get("channels",[])),
                     msgs=sum(g.get("count",0) for g in items))
            await edit(cid, mid, txt, kb_groups_list(items, page, 8, "sg"))
        return

    # ── Footprint actions ──
    if data == "fp_scan":
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_footprint_scan, uid, cid, la)
        return

    if data == "fp_delete":
        _, sd = sget(uid)
        sr = sd.get("scan_result", {})
        txt = tx(la, "footprint_confirm",
                 gr=len(sr.get("groups",[])), msgs=sr.get("total", "?"))
        await edit(cid, mid, txt, kb_confirm(la))
        return

    if data == "fp_confirm_yes":
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_footprint_delete, uid, cid, la)
        return

    # ── Ethical (kept for cleanup flow) ──
    if data == "eth_y":
        await edit(cid, mid, "🧹", kb_footprint_actions(la)); return
    if data == "eth_n" or data == "back_main":
        sdel(uid)
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia))
        return

# ══════════════════════════════
# FASTAPI
# ══════════════════════════════
@asynccontextmanager
async def lifespan(a):
    print("🚀 ShadowClean Bot v4.0")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"✅ DB | Admins: {ADMIN_IDS} | Credits: {DEFAULT_CREDITS} | Port: {PORT}")
    yield
    for c in clients.values():
        try: await c.disconnect()
        except: pass
    await engine.dispose()
    print("🛑 Off")

app = FastAPI(title="ShadowClean v4", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

@app.post("/webhook")
async def webhook(request: dict, bg: BackgroundTasks):
    async with DBS() as db:
        try:
            if "message" in request: await on_msg(db, request["message"], bg)
            elif "callback_query" in request: await on_cb(db, request["callback_query"], bg)
        except Exception as e:
            print(f"❌ {e}\n{traceback.format_exc()}")
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
