"""
══════════════════════════════════════════
  ShadowClean Bot v3.0
  Telegram OSINT + Stalk + Cleaner
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
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError
)

load_dotenv()

# ══════════════════════════════════════
# CONFIG
# ══════════════════════════════════════
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

# ══════════════════════════════════════
# DATABASE
# ══════════════════════════════════════
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

# ══════════════════════════════════════
# STATE
# ══════════════════════════════════════
user_states: Dict[int, Dict] = {}

def sset(uid, state, **kw):
    user_states[uid] = {"s": state, **kw}

def sget(uid):
    d = user_states.get(uid, {})
    return d.get("s"), d

def sdel(uid):
    user_states.pop(uid, None)

# ══════════════════════════════════════
# TELEGRAM API
# ══════════════════════════════════════
async def tg(method, **kw):
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BOT_API}/{method}", json=kw)
            return r.json()
    except:
        return {"ok": False}

async def send(cid, text, markup=None, reply_markup_type="inline"):
    p = {"chat_id": cid, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return await tg("sendMessage", **p)

async def edit(cid, mid, text, markup=None):
    p = {"chat_id": cid, "message_id": mid, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    try:
        return await tg("editMessageText", **p)
    except:
        return await send(cid, text, markup)

async def answer(cbid, text=""):
    return await tg("answerCallbackQuery", callback_query_id=cbid, text=text)

# ══════════════════════════════════════
# KEYBOARDS (Reply Keyboard)
# ══════════════════════════════════════
def kb_main(la, is_admin=False):
    if la == "en":
        rows = [
            ["🔍 OSINT Search", "👁 Stalk"],
            ["🧹 Cleanup", "👤 Profile"],
            ["📱 Login", "❓ Help"],
        ]
        if is_admin:
            rows.append(["👑 Admin Panel"])
    else:
        rows = [
            ["🔍 جستجو OSINT", "👁 استاک"],
            ["🧹 پاکسازی", "👤 پروفایل"],
            ["📱 ورود", "❓ راهنما"],
        ]
        if is_admin:
            rows.append(["👑 پنل مدیریت"])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

def kb_back(la):
    txt = "🔙 Back" if la == "en" else "🔙 بازگشت"
    return {"keyboard": [[txt]], "resize_keyboard": True}

def kb_admin_panel(la):
    if la == "en":
        return {"keyboard": [
            ["💎 Add Credits", "🔧 Set Credits"],
            ["🔎 Lookup User", "🚫 Ban User"],
            ["✅ Unban User", "📢 Broadcast"],
            ["🔙 Back"],
        ], "resize_keyboard": True}
    return {"keyboard": [
        ["💎 افزودن اعتبار", "🔧 تنظیم اعتبار"],
        ["🔎 جستجوی کاربر", "🚫 بن کردن"],
        ["✅ آنبن کردن", "📢 پیام همگانی"],
        ["🔙 بازگشت"],
    ], "resize_keyboard": True}

# Inline keyboards for group/channel selection
def kb_groups_inline(groups, page=0, per_page=8):
    """Build inline keyboard for group list with pagination."""
    start = page * per_page
    end = start + per_page
    chunk = groups[start:end]
    rows = []
    for g in chunk:
        title = g["title"][:30]
        count = g.get("count", 0)
        rows.append([{"text": f"📂 {title} ({count})", "callback_data": f"grp_{g['id']}"}])
    # Pagination
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"gpage_{page-1}"})
    if end < len(groups):
        nav.append({"text": "➡️", "callback_data": f"gpage_{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "🔙", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def kb_confirm_inline(la):
    y = "✅ Yes, Delete!" if la == "en" else "✅ بله، حذف کن!"
    n = "❌ Cancel" if la == "en" else "❌ انصراف"
    return {"inline_keyboard": [
        [{"text": y, "callback_data": "cf_y"}, {"text": n, "callback_data": "cf_n"}]
    ]}

def kb_ethical_inline(la):
    y = "✅ Agree" if la == "en" else "✅ موافقم"
    n = "❌ No" if la == "en" else "❌ مخالفم"
    return {"inline_keyboard": [
        [{"text": y, "callback_data": "eth_y"}, {"text": n, "callback_data": "eth_n"}]
    ]}

def kb_clean_inline(la):
    s = "📊 Scan" if la == "en" else "📊 اسکن"
    d = "🗑️ Delete All" if la == "en" else "🗑️ حذف همه"
    return {"inline_keyboard": [
        [{"text": s, "callback_data": "cl_dry"}, {"text": d, "callback_data": "cl_real"}]
    ]}

# ══════════════════════════════════════
# TEXTS
# ══════════════════════════════════════
T = {
  "fa": {
    "welcome": (
        "🌑 <b>ShadowClean Bot</b>\n\n"
        "🔍 جستجوی OSINT کاربران\n"
        "👁 استاک فعالیت در گروه‌ها\n"
        "🧹 پاکسازی ردپای دیجیتال\n\n"
        "⚠️ <i>فقط استفاده شخصی و قانونی</i>\n\n"
        "💎 اعتبار: <b>{cr}</b> | استفاده‌شده: <b>{used}</b>"
    ),
    "help": (
        "❓ <b>راهنما</b>\n\n"
        "🔍 <b>OSINT</b> - اطلاعات عمومی کاربر\n"
        "👁 <b>استاک</b> - فعالیت در گروه‌های مشترک\n"
        "🧹 <b>پاکسازی</b> - حذف پیام‌های شما از گروه‌ها\n"
        "📱 <b>ورود</b> - لاگین برای امکانات پیشرفته\n\n"
        "💎 هر کاربر {cr} درخواست رایگان دارد"
    ),
    "no_credit": "❌ <b>اعتبار تمام شده!</b>\n\nبرای دریافت اعتبار با پشتیبانی تماس بگیرید.",
    "osint_ask": "🔍 <b>جستجوی OSINT</b>\n\n@username یا آیدی عددی هدف رو بفرستید:",
    "stalk_ask": "👁 <b>استاک</b>\n\n@username یا آیدی عددی هدف رو بفرستید:\n\n⚠️ نیاز به لاگین (📱 ورود)",
    "clean_info": "🧹 <b>پاکسازی</b>\n\n⚠️ فقط پیام‌های خودتان\n⚠️ برگشت‌ناپذیر\n\nاول 📱 ورود بزنید",
    "phone_ask": "📱 شماره با کد کشور:\n<code>+989121234567</code>\n\n🔐 رمزنگاری AES-256\n⏰ حذف ۲۴ ساعته",
    "code_ask": "📨 کد تأیید رو بفرستید:",
    "2fa_ask": "🔐 رمز دوم (2FA):",
    "login_ok": "✅ ورود موفق!",
    "login_fail": "❌ خطا: {e}",
    "logout_ok": "✅ خارج شدید.",
    "not_logged": "❌ ابتدا 📱 ورود بزنید",
    "profile": (
        "👤 <b>پروفایل</b>\n\n"
        "🆔 <code>{uid}</code>\n"
        "👤 {name}\n"
        "💎 اعتبار: <b>{cr}</b>\n"
        "📊 استفاده: {used}\n"
        "🔐 لاگین: {login}\n"
        "📅 عضویت: {date}"
    ),
    "ethical": "⚠️ <b>هشدار</b>\n\n• فقط داده خودتان\n• جاسوسی غیرقانونیه\n• مسئولیت با شماست\n\nموافقید؟",
    "processing": "⏳ صبر کنید...",
    "error": "❌ خطا: {e}",
    "banned": "🚫 حساب شما مسدود شده.\nبا پشتیبانی تماس بگیرید.",
    "osint_res": (
        "🔍 <b>نتیجه OSINT</b>\n\n"
        "👤 نام: {name}\n"
        "🆔 آیدی: <code>{uid}</code>\n"
        "📛 یوزرنیم: {uname}\n"
        "📸 عکس: {photo}\n"
        "ℹ️ بیو: {bio}\n"
        "⏰ آخرین: {seen}"
    ),
    "stalk_panel": (
        "👁 <b>استاک - {name}</b>\n\n"
        "📂 گروه‌ها: <b>{gr_count}</b>\n"
        "📢 کانال‌ها: <b>{ch_count}</b>\n"
        "💬 کل پیام‌ها: <b>{total_msgs}</b>\n\n"
        "گروه/کانال مورد نظر رو انتخاب کنید:"
    ),
    "stalk_group_msgs": "👁 <b>پیام‌های {name} در {group}</b>\n\n",
    "no_msgs": "💬 پیامی یافت نشد.",
    "dry_res": "📊 <b>اسکن</b>\n\n📂 گروه: {gr}\n💬 پیام: {ms}\n📸 مدیا: {md}\n📝 متن: {tx}",
    "del_done": "✅ <b>تمام!</b>\n\n🗑️ {done} حذف\n📂 {gr} گروه\n⏱️ {time}\n❌ {err} خطا",
    "confirm": "⚠️ مطمئنید؟ برگشت‌ناپذیره!",
    "admin_panel": (
        "👑 <b>پنل مدیریت</b>\n\n"
        "👥 کاربران: <b>{total}</b>\n"
        "🚫 بن: <b>{banned}</b>\n"
        "🔐 لاگین: <b>{logged}</b>"
    ),
    "a_credit_ask": "💎 بفرستید:\n<code>آیدی تعداد</code>\nمثال: <code>123456 10</code>",
    "a_credit_ok": "✅ <b>{n}</b> اعتبار به <code>{uid}</code> اضافه شد.\nفعلی: <b>{total}</b>",
    "a_credit_fail": "❌ فرمت اشتباه! <code>آیدی تعداد</code>",
    "a_setcr_ask": "🔧 بفرستید:\n<code>آیدی تعداد</code>",
    "a_setcr_ok": "✅ اعتبار <code>{uid}</code> = <b>{n}</b>",
    "a_ban_ask": "🚫 آیدی عددی:",
    "a_ban_ok": "✅ <code>{uid}</code> بن شد.",
    "a_unban_ask": "✅ آیدی عددی:",
    "a_unban_ok": "✅ <code>{uid}</code> آنبن شد.",
    "a_notfound": "❌ کاربر یافت نشد!",
    "a_lookup_ask": "🔎 آیدی عددی:",
    "a_user_info": "📊 <b>کاربر</b>\n\n🆔 <code>{uid}</code>\n👤 {name}\n📛 @{uname}\n💎 {cr}\n📊 {used}\n🚫 {ban}\n📅 {date}",
    "a_bcast_ask": "📢 متن پیام:",
    "a_bcast_ok": "✅ به {n} کاربر ارسال شد.",
  },
  "en": {
    "welcome": "🌑 <b>ShadowClean Bot</b>\n\n🔍 OSINT\n👁 Stalk\n🧹 Cleanup\n\n💎 Credits: <b>{cr}</b> | Used: <b>{used}</b>",
    "help": "❓ <b>Help</b>\n\n🔍 OSINT - Public info\n👁 Stalk - Group activity\n🧹 Cleanup - Delete msgs\n📱 Login - Advanced\n\n💎 {cr} free credits",
    "no_credit": "❌ <b>No credits!</b>\n\nContact support.",
    "osint_ask": "🔍 Send @username or numeric ID:",
    "stalk_ask": "👁 <b>Stalk</b>\n\nSend @username or ID:\n⚠️ Login required (📱)",
    "clean_info": "🧹 YOUR msgs only, irreversible.\n📱 Login first",
    "phone_ask": "📱 Phone with code:\n<code>+989121234567</code>",
    "code_ask": "📨 Enter code:",
    "2fa_ask": "🔐 2FA password:",
    "login_ok": "✅ Login OK!",
    "login_fail": "❌ Error: {e}",
    "logout_ok": "✅ Logged out.",
    "not_logged": "❌ 📱 Login first",
    "profile": "👤 <b>Profile</b>\n\n🆔 <code>{uid}</code>\n👤 {name}\n💎 {cr}\n📊 {used}\n🔐 {login}\n📅 {date}",
    "ethical": "⚠️ YOUR data only. Spying = illegal.\n\nAgree?",
    "processing": "⏳ Processing...",
    "error": "❌ Error: {e}",
    "banned": "🚫 Banned. Contact support.",
    "osint_res": "🔍 <b>OSINT</b>\n\n👤 {name}\n🆔 <code>{uid}</code>\n📛 {uname}\n📸 {photo}\nℹ️ {bio}\n⏰ {seen}",
    "stalk_panel": "👁 <b>Stalk - {name}</b>\n\n📂 Groups: <b>{gr_count}</b>\n📢 Channels: <b>{ch_count}</b>\n💬 Messages: <b>{total_msgs}</b>\n\nSelect:",
    "stalk_group_msgs": "👁 <b>{name} in {group}</b>\n\n",
    "no_msgs": "💬 No messages found.",
    "dry_res": "📊 Groups: {gr} | Msgs: {ms} | Media: {md} | Text: {tx}",
    "del_done": "✅ Deleted: {done} | Groups: {gr} | Time: {time} | Errors: {err}",
    "confirm": "⚠️ Sure? Irreversible!",
    "admin_panel": "👑 <b>Admin</b>\n\n👥 {total}\n🚫 {banned}\n🔐 {logged}",
    "a_credit_ask": "💎 <code>ID amount</code>",
    "a_credit_ok": "✅ +{n} to {uid}. Total: {total}",
    "a_credit_fail": "❌ Format: <code>ID amount</code>",
    "a_setcr_ask": "🔧 <code>ID amount</code>",
    "a_setcr_ok": "✅ {uid} credits = {n}",
    "a_ban_ask": "🚫 User ID:",
    "a_ban_ok": "✅ {uid} banned.",
    "a_unban_ask": "✅ User ID:",
    "a_unban_ok": "✅ {uid} unbanned.",
    "a_notfound": "❌ Not found!",
    "a_lookup_ask": "🔎 User ID:",
    "a_user_info": "📊 {uid} | {name} | @{uname} | 💎{cr} | 📊{used} | {ban} | {date}",
    "a_bcast_ask": "📢 Message text:",
    "a_bcast_ok": "✅ Sent to {n} users.",
  }
}

def tx(la, key, **kw):
    txt = T.get(la, T["fa"]).get(key, T["fa"].get(key, key))
    try: return txt.format(**kw) if kw else txt
    except: return txt

# ══════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════
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
    """Admin has unlimited credits."""
    if u.is_admin or u.id in ADMIN_IDS:
        return True
    return u.credits > 0

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

# ══════════════════════════════════════
# TELETHON
# ══════════════════════════════════════
clients: Dict[int, TelegramClient] = {}

async def tclient(uid, ss):
    if uid in clients and clients[uid].is_connected(): return clients[uid]
    c = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await c.connect(); clients[uid] = c; return c

async def tnew():
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect(); return c

# ══════════════════════════════════════
# OSINT
# ══════════════════════════════════════
async def osint_light(target):
    r = await tg("getChat", chat_id=target)
    if r.get("ok"):
        c = r["result"]
        pr = await tg("getUserProfilePhotos", user_id=c.get("id",0), limit=1)
        pc = pr.get("result",{}).get("total_count",0) if pr.get("ok") else 0
        return {"uid":c.get("id"), "name":f'{c.get("first_name","")} {c.get("last_name","")}'.strip(),
                "uname":c.get("username",""), "bio":c.get("bio","—"), "photo":"✅" if pc else "❌"}
    return None

async def osint_full(client, target):
    try:
        ent = await client.get_entity(target)
        full = await client(GetFullUserRequest(ent))
        seen = "?"
        if hasattr(ent,'status') and ent.status:
            if hasattr(ent.status,'was_online'): seen = str(ent.status.was_online)
            else: seen = type(ent.status).__name__.replace("UserStatus","")
        commons = []
        try:
            cr = await client(functions.messages.GetCommonChatsRequest(user_id=ent,max_id=0,limit=100))
            commons = [{"id":c.id, "title":getattr(c,'title','?')} for c in cr.chats]
        except: pass
        return {"uid":ent.id,
                "name":f'{getattr(ent,"first_name","") or ""} {getattr(ent,"last_name","") or ""}'.strip(),
                "uname":getattr(ent,'username',''), "bio":getattr(full.full_user,'about','') or '—',
                "photo":"✅" if ent.photo else "❌", "seen":seen, "commons":commons}
    except: return None

# ══════════════════════════════════════
# STALK ENGINE (with group panel)
# ══════════════════════════════════════
async def stalk_collect(client, target_id):
    """Collect all groups/channels where target has messages."""
    result = {"groups": [], "channels": [], "total_msgs": 0}
    try:
        dlg = await client.get_dialogs(limit=300)
        for d in dlg:
            if not hasattr(d.entity, 'id'): continue
            is_mega = hasattr(d.entity, 'megagroup') and d.entity.megagroup
            is_channel = hasattr(d.entity, 'broadcast') and d.entity.broadcast
            is_group = d.is_group

            if not (is_mega or is_channel or is_group): continue

            cnt = 0
            try:
                async for _ in client.iter_messages(d.entity, from_user=target_id, limit=100):
                    cnt += 1
            except: continue

            if cnt > 0:
                info = {"id": d.entity.id, "title": getattr(d.entity,'title','?'), "count": cnt}
                if is_channel and not is_mega:
                    result["channels"].append(info)
                else:
                    result["groups"].append(info)
                result["total_msgs"] += cnt
    except: pass
    return result

async def stalk_group_messages(client, target_id, group_id, limit=20):
    """Get messages from target in specific group with links."""
    messages = []
    try:
        entity = await client.get_entity(group_id)
        chat_username = getattr(entity, 'username', None)

        async for msg in client.iter_messages(entity, from_user=target_id, limit=limit):
            text_preview = ""
            if msg.text:
                text_preview = msg.text[:150].replace("<","&lt;").replace(">","&gt;")
            elif msg.media:
                text_preview = "📎 [Media]"
            else:
                text_preview = "..."

            # Build message link
            if chat_username:
                link = f"https://t.me/{chat_username}/{msg.id}"
            else:
                chat_id_str = str(entity.id)
                if hasattr(entity, 'id'):
                    link = f"https://t.me/c/{chat_id_str}/{msg.id}"
                else:
                    link = ""

            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"

            messages.append({
                "text": text_preview,
                "date": date_str,
                "link": link,
                "id": msg.id,
            })
    except Exception as e:
        print(f"stalk_group_messages error: {e}")
    return messages

# ══════════════════════════════════════
# CLEANUP ENGINE
# ══════════════════════════════════════
async def do_dry(client, cid, la):
    res = {"gr":[],"ms":0,"md":0,"tx":0}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [d for d in dlg if d.is_group and hasattr(d.entity,'megagroup') and d.entity.megagroup]
        pm = await send(cid, tx(la,"processing")); pmid = pm.get("result",{}).get("message_id")
        for i,d in enumerate(sgs):
            gc=gm=gt=0
            try:
                async for m in client.iter_messages(d.entity, from_user=me.id):
                    gc+=1
                    if m.media: gm+=1
                    else: gt+=1
                if gc: res["gr"].append({"t":d.entity.title,"c":gc}); res["ms"]+=gc; res["md"]+=gm; res["tx"]+=gt
            except FloodWaitError as e: await asyncio.sleep(e.seconds+1)
            except: continue
            if pmid and (i+1)%3==0:
                await edit(cid,pmid,f"📊 {int((i+1)/len(sgs)*100)}%...")
    except: pass
    return res

async def do_real_delete(client, cid, la):
    res = {"done":0,"err":0,"gr":0,"det":[]}
    try:
        me = await client.get_me()
        dlg = await client.get_dialogs(limit=500)
        sgs = [d for d in dlg if d.is_group and hasattr(d.entity,'megagroup') and d.entity.megagroup]
        pm = await send(cid, tx(la,"processing")); pmid = pm.get("result",{}).get("message_id")
        start = time.time()
        for i,d in enumerate(sgs):
            ids=[]
            try:
                async for m in client.iter_messages(d.entity, from_user=me.id): ids.append(m.id)
            except FloodWaitError as e: await asyncio.sleep(e.seconds+1)
            except: continue
            if not ids: continue
            gd=ge=0
            for j in range(0,len(ids),50):
                batch=ids[j:j+50]
                try:
                    await client.delete_messages(d.entity, batch, revoke=True)
                    gd+=len(batch); await asyncio.sleep(1)
                except FloodWaitError as e:
                    await asyncio.sleep(int(e.seconds*1.5))
                    try: await client.delete_messages(d.entity,batch,revoke=True); gd+=len(batch)
                    except: ge+=len(batch)
                except: ge+=len(batch)
            res["done"]+=gd; res["err"]+=ge
            if gd: res["gr"]+=1; res["det"].append(f"{d.entity.title}: {gd}")
            if pmid:
                try: await edit(cid,pmid,f"🧹 {int((i+1)/len(sgs)*100)}% | {res['done']} deleted")
                except: pass
    except: pass
    return res

# ══════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════
async def bg_osint(uid, cid, target, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if ss:
            client = await tclient(uid, ss)
            r = await osint_full(client, target)
        else:
            r = await osint_light(target)
        if r:
            txt = tx(la,"osint_res", name=r.get("name","?"), uid=r.get("uid","?"),
                uname=f'@{r["uname"]}' if r.get("uname") else "—",
                photo=r.get("photo","?"), bio=r.get("bio","—"), seen=r.get("seen","—"))
            if r.get("commons"):
                txt += "\n\n📂 گروه‌های مشترک:\n" + "\n".join(f"  • {c['title']}" for c in r["commons"][:10])
            await send(cid, txt)
        else:
            await send(cid, tx(la,"error",e="Not found"))

async def bg_stalk(uid, cid, target, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)
        try:
            ent = await client.get_entity(target)
            target_id = ent.id
            target_name = f'{getattr(ent,"first_name","") or ""} {getattr(ent,"last_name","") or ""}'.strip() or "?"
        except:
            await send(cid, tx(la,"error",e="Target not found")); return

        pm = await send(cid, tx(la,"processing"))
        pmid = pm.get("result",{}).get("message_id")

        result = await stalk_collect(client, target_id)

        # Save stalk data for this user
        all_items = []
        for g in result["groups"]:
            all_items.append(g)
        for c in result["channels"]:
            all_items.append(c)

        sset(uid, "stalk_panel", target_id=target_id, target_name=target_name,
             items=all_items, groups=result["groups"], channels=result["channels"])

        txt = tx(la, "stalk_panel",
                 name=target_name,
                 gr_count=len(result["groups"]),
                 ch_count=len(result["channels"]),
                 total_msgs=result["total_msgs"])

        kb = kb_groups_inline(all_items, page=0)

        if pmid:
            await edit(cid, pmid, txt, kb)
        else:
            await send(cid, txt, kb)

async def bg_stalk_group(uid, cid, group_id, la):
    """Show messages from target in specific group."""
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: return
        client = await tclient(uid, ss)
        _, sd = sget(uid)
        target_id = sd.get("target_id")
        target_name = sd.get("target_name", "?")
        if not target_id: return

        try:
            entity = await client.get_entity(group_id)
            group_title = getattr(entity, 'title', '?')
        except:
            group_title = "?"

        messages = await stalk_group_messages(client, target_id, group_id, limit=30)

        if not messages:
            await send(cid, tx(la, "no_msgs"))
            return

        header = tx(la, "stalk_group_msgs", name=target_name, group=group_title)
        chunks = [messages[i:i+5] for i in range(0, len(messages), 5)]

        for chunk in chunks:
            txt = header if chunk == chunks[0] else ""
            for m in chunk:
                link_text = f'(<a href="{m["link"]}">link</a>)' if m["link"] else ""
                txt += f'📅 {m["date"]} {link_text}\n💬 {m["text"]}\n{"─"*30}\n'
            await send(cid, txt)
            await asyncio.sleep(0.3)

async def bg_dry(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)
        r = await do_dry(client, cid, la)
        txt = tx(la,"dry_res", gr=len(r["gr"]), ms=r["ms"], md=r["md"], tx=r["tx"])
        if r["gr"]: txt += "\n\n" + "\n".join(f"• {g['t']}: {g['c']}" for g in r["gr"][:20])
        await send(cid, txt, kb_confirm_inline(la))

async def bg_real(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss: await send(cid, tx(la,"not_logged")); return
        client = await tclient(uid, ss)
        start = time.time()
        r = await do_real_delete(client, cid, la)
        el = time.time()-start; ts = f"{int(el//60)}m {int(el%60)}s"
        txt = tx(la,"del_done", done=r["done"], gr=r["gr"], time=ts, err=r["err"])
        if r["det"]: txt += "\n\n" + "\n".join(f"• {d}" for d in r["det"][:20])
        await send(cid, txt)

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
                ia = uid in ADMIN_IDS
                await send(cid, tx(la,"login_ok"), kb_main(la, ia))
            except SessionPasswordNeededError:
                nss = client.session.save()
                so.enc_session = fernet.encrypt(nss.encode()).decode(); await db.commit()
                sset(uid, "2fa"); await send(cid, tx(la,"2fa_ask"))
            finally: await client.disconnect()
        except PhoneCodeInvalidError: await send(cid, tx(la,"login_fail",e="Wrong code"))
        except PhoneCodeExpiredError: sdel(uid); await send(cid, tx(la,"login_fail",e="Expired, try again"))
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

async def bg_broadcast(admin_uid, cid, text, la):
    async with DBS() as db:
        users = await get_all_users(db); n = 0
        for u in users:
            if u.id == admin_uid: continue
            try: await send(u.id, f"📢\n\n{text}"); n+=1; await asyncio.sleep(0.1)
            except: continue
        await send(cid, tx(la,"a_bcast_ok",n=n), kb_admin_panel(la))

# ══════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════
async def on_msg(db, msg, bg: BackgroundTasks):
    cid = msg.get("chat",{}).get("id")
    uid = msg.get("from",{}).get("id")
    fname = msg.get("from",{}).get("first_name","")
    uname = msg.get("from",{}).get("username","")
    text = (msg.get("text") or "").strip()
    if not cid or not uid or msg.get("chat",{}).get("type") != "private": return

    u = await get_user(db, uid, uname, fname)
    la = u.lang
    ia = u.is_admin or uid in ADMIN_IDS

    if u.is_banned: await send(cid, tx(la,"banned")); return

    st, sd = sget(uid)

    # Login states
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
            tuid,n = int(parts[0]),int(parts[1])
            total = await add_credits(db, tuid, n)
            if total is not None: await send(cid, tx(la,"a_credit_ok",uid=tuid,n=n,total=total), kb_admin_panel(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        else: await send(cid, tx(la,"a_credit_fail"), kb_admin_panel(la))
        return

    if st == "a_setcr" and ia:
        sdel(uid); parts = text.split()
        if len(parts)==2 and parts[0].isdigit() and parts[1].isdigit():
            tuid,n = int(parts[0]),int(parts[1])
            r = await set_credits(db, tuid, n)
            if r is not None: await send(cid, tx(la,"a_setcr_ok",uid=tuid,n=n), kb_admin_panel(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        else: await send(cid, tx(la,"a_credit_fail"), kb_admin_panel(la))
        return

    if st == "a_ban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await ban_user(db, int(text))
            await send(cid, tx(la,"a_ban_ok",uid=text) if ok else tx(la,"a_notfound"), kb_admin_panel(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        return

    if st == "a_unban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await unban_user(db, int(text))
            await send(cid, tx(la,"a_unban_ok",uid=text) if ok else tx(la,"a_notfound"), kb_admin_panel(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        return

    if st == "a_lookup" and ia:
        sdel(uid)
        if text.isdigit():
            tu = await lookup_user(db, int(text))
            if tu: await send(cid, tx(la,"a_user_info",uid=tu.id,name=tu.first_name or "?",
                uname=tu.username or "—",cr=tu.credits,used=tu.total_used,
                ban="🚫" if tu.is_banned else "✅",
                date=tu.joined.strftime("%Y-%m-%d") if tu.joined else "?"), kb_admin_panel(la))
            else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        else: await send(cid, tx(la,"a_notfound"), kb_admin_panel(la))
        return

    if st == "a_bcast" and ia:
        sdel(uid); bg.add_task(bg_broadcast, uid, cid, text, la); return

    # ── Keyboard button texts ──
    if text in ["🔍 جستجو OSINT", "🔍 OSINT Search"]:
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sset(uid, "osint"); await send(cid, tx(la,"osint_ask"), kb_back(la)); return

    if text in ["👁 استاک", "👁 Stalk"]:
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sess = await get_auth_session(db, uid)
        if not sess: await send(cid, tx(la,"not_logged"), kb_main(la, ia)); return
        sset(uid, "stalk"); await send(cid, tx(la,"stalk_ask"), kb_back(la)); return

    if text in ["🧹 پاکسازی", "🧹 Cleanup"]:
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        sess = await get_auth_session(db, uid)
        if not sess: await send(cid, tx(la,"clean_info"), kb_main(la, ia)); return
        await send(cid, tx(la,"ethical"), kb_ethical_inline(la)); return

    if text in ["📱 ورود", "📱 Login"]:
        sset(uid, "phone"); await send(cid, tx(la,"phone_ask"), kb_back(la)); return

    if text in ["👤 پروفایل", "👤 Profile"]:
        sess = await get_auth_session(db, uid)
        await send(cid, tx(la,"profile",uid=uid,name=fname or uname or "?",
            cr="♾️" if ia else u.credits, used=u.total_used,
            login="✅" if sess else "❌",
            date=u.joined.strftime("%Y-%m-%d") if u.joined else "?"), kb_main(la, ia)); return

    if text in ["❓ راهنما", "❓ Help"]:
        await send(cid, tx(la,"help",cr=DEFAULT_CREDITS), kb_main(la, ia)); return

    if text in ["👑 پنل مدیریت", "👑 Admin Panel"] and ia:
        total, banned, logged = await get_stats(db)
        await send(cid, tx(la,"admin_panel",total=total,banned=banned,logged=logged), kb_admin_panel(la)); return

    if text in ["🔙 بازگشت", "🔙 Back"]:
        sdel(uid)
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return

    # Admin panel buttons
    if ia:
        if text in ["💎 افزودن اعتبار", "💎 Add Credits"]:
            sset(uid, "a_credit"); await send(cid, tx(la,"a_credit_ask"), kb_back(la)); return
        if text in ["🔧 تنظیم اعتبار", "🔧 Set Credits"]:
            sset(uid, "a_setcr"); await send(cid, tx(la,"a_setcr_ask"), kb_back(la)); return
        if text in ["🔎 جستجوی کاربر", "🔎 Lookup User"]:
            sset(uid, "a_lookup"); await send(cid, tx(la,"a_lookup_ask"), kb_back(la)); return
        if text in ["🚫 بن کردن", "🚫 Ban User"]:
            sset(uid, "a_ban"); await send(cid, tx(la,"a_ban_ask"), kb_back(la)); return
        if text in ["✅ آنبن کردن", "✅ Unban User"]:
            sset(uid, "a_unban"); await send(cid, tx(la,"a_unban_ask"), kb_back(la)); return
        if text in ["📢 پیام همگانی", "📢 Broadcast"]:
            sset(uid, "a_bcast"); await send(cid, tx(la,"a_bcast_ask"), kb_back(la)); return

    # Commands
    if text.startswith("/start"):
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return
    if text.startswith("/help"):
        await send(cid, tx(la,"help",cr=DEFAULT_CREDITS), kb_main(la, ia)); return
    if text.startswith("/login"):
        sset(uid, "phone"); await send(cid, tx(la,"phone_ask"), kb_back(la)); return
    if text.startswith("/logout"):
        bg.add_task(bg_logout, uid, cid, la); return
    if text.startswith("/lang"):
        u.lang = "en" if u.lang=="fa" else "fa"; await db.commit()
        await send(cid, tx(u.lang,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(u.lang, ia)); return

    # Default
    await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia))

# ══════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════
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
    la = u.lang
    ia = u.is_admin or uid in ADMIN_IDS

    if u.is_banned: return

    # Stalk group selection
    if data.startswith("grp_"):
        group_id = int(data.replace("grp_",""))
        bg.add_task(bg_stalk_group, uid, cid, group_id, la)
        return

    # Stalk pagination
    if data.startswith("gpage_"):
        page = int(data.replace("gpage_",""))
        _, sd = sget(uid)
        items = sd.get("items", [])
        target_name = sd.get("target_name", "?")
        if items:
            txt = tx(la, "stalk_panel", name=target_name,
                     gr_count=len(sd.get("groups",[])),
                     ch_count=len(sd.get("channels",[])),
                     total_msgs=sum(g.get("count",0) for g in items))
            await edit(cid, mid, txt, kb_groups_inline(items, page))
        return

    if data == "back_main":
        sdel(uid)
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia))
        return

    # Ethical
    if data == "eth_y":
        await edit(cid, mid, "🧹", kb_clean_inline(la)); return
    if data == "eth_n":
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return

    # Cleanup
    if data == "cl_dry":
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid); bg.add_task(bg_dry, uid, cid, la); return
    if data == "cl_real":
        await edit(cid, mid, tx(la,"confirm"), kb_confirm_inline(la)); return
    if data == "cf_y":
        if not await has_credit(u): await send(cid, tx(la,"no_credit")); return
        await use_credit(db, uid); bg.add_task(bg_real, uid, cid, la); return
    if data == "cf_n":
        await send(cid, tx(la,"welcome",cr="♾️" if ia else u.credits,used=u.total_used), kb_main(la, ia)); return

# ══════════════════════════════════════
# FASTAPI
# ══════════════════════════════════════
@asynccontextmanager
async def lifespan(a):
    print("🚀 ShadowClean Bot v3.0 Starting...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"✅ DB ready | Admins: {ADMIN_IDS} | Credits: {DEFAULT_CREDITS} | Port: {PORT}")
    yield
    for c in clients.values():
        try: await c.disconnect()
        except: pass
    await engine.dispose()
    print("🛑 Stopped")

app = FastAPI(title="ShadowClean v3", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status":"ok","v":"3.0"}

@app.get("/")
async def root():
    return {"status":"running"}

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
