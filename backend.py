"""
══════════════════════════════════════════
  ShadowClean Bot v5.0
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
from telethon.tl.types import Channel, Chat, PeerChannel, PeerUser, InputPeerUser
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
    print("❌ Set: BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, DATABASE_URL"); sys.exit(1)

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

# ══════════════════════════════
# ADMIN BOT CLIENT (always available)
# ══════════════════════════════
# This is a Telethon client using BOT TOKEN for public searches
# No phone login needed - works with bot token
bot_client: Optional[TelegramClient] = None

async def get_bot_client():
    global bot_client
    if bot_client and bot_client.is_connected():
        return bot_client
    bot_client = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot_client.start(bot_token=BOT_TOKEN)
    return bot_client

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
def sset(uid, state, **kw): user_states[uid] = {"s": state, **kw}
def sget(uid):
    d = user_states.get(uid, {})
    return d.get("s"), d
def sdel(uid): user_states.pop(uid, None)

# ══════════════════════════════
# TELEGRAM API
# ══════════════════════════════
async def tg(method, **kw):
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BOT_API}/{method}", json=kw)
            return r.json()
    except: return {"ok": False}

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
        rows = [["👁 Stalk", "🧹 My Footprint"], ["👤 Profile", "❓ Help"]]
        if is_admin: rows.append(["👑 Admin"])
    else:
        rows = [["👁 استاک", "🧹 ردپای من"], ["👤 پروفایل", "❓ راهنما"]]
        if is_admin: rows.append(["👑 مدیریت"])
    return {"keyboard": rows, "resize_keyboard": True}

def kb_back(la):
    return {"keyboard": [["🔙 بازگشت" if la == "fa" else "🔙 Back"]], "resize_keyboard": True}

def kb_admin_menu(la):
    if la == "en":
        return {"keyboard": [["💎 Credits", "🔧 Set"], ["🔎 Lookup", "🚫 Ban"],
                              ["✅ Unban", "📢 Broadcast"], ["🔙 Back"]], "resize_keyboard": True}
    return {"keyboard": [["💎 اعتبار", "🔧 تنظیم"], ["🔎 جستجو", "🚫 بن"],
                          ["✅ آنبن", "📢 پیام"], ["🔙 بازگشت"]], "resize_keyboard": True}

def kb_groups_inline(groups, page=0, per_page=8, prefix="sg"):
    start = page * per_page
    chunk = groups[start:start + per_page]
    rows = []
    for g in chunk:
        title = g["title"][:25]
        cnt = g.get("count", "?")
        rows.append([{"text": f"📂 {title} ({cnt})", "callback_data": f"{prefix}_{g['id']}"}])
    nav = []
    if page > 0: nav.append({"text": "⬅️", "callback_data": f"{prefix}p_{page - 1}"})
    if start + per_page < len(groups): nav.append({"text": "➡️", "callback_data": f"{prefix}p_{page + 1}"})
    if nav: rows.append(nav)
    rows.append([{"text": "🔙", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def kb_footprint(la, logged_in=False):
    if la == "en":
        rows = [
            [{"text": "📊 Scan My Messages", "callback_data": "fp_scan"}],
        ]
        if logged_in:
            rows.append([{"text": "🗑️ DELETE ALL MY MSGS", "callback_data": "fp_delete"}])
        else:
            rows.append([{"text": "📱 Login to Delete", "callback_data": "fp_login"}])
        rows.append([{"text": "🔙 Back", "callback_data": "back_main"}])
    else:
        rows = [
            [{"text": "📊 اسکن پیام‌های من", "callback_data": "fp_scan"}],
        ]
        if logged_in:
            rows.append([{"text": "🗑️ حذف همه پیام‌هام", "callback_data": "fp_delete"}])
        else:
            rows.append([{"text": "📱 ورود برای حذف", "callback_data": "fp_login"}])
        rows.append([{"text": "🔙 بازگشت", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def kb_confirm(la):
    if la == "en":
        return {"inline_keyboard": [[
            {"text": "✅ Yes DELETE ALL", "callback_data": "fp_yes"},
            {"text": "❌ Cancel", "callback_data": "back_main"}]]}
    return {"inline_keyboard": [[
        {"text": "✅ بله حذف کن", "callback_data": "fp_yes"},
        {"text": "❌ انصراف", "callback_data": "back_main"}]]}

# ══════════════════════════════
# TEXTS
# ══════════════════════════════
T = {
  "fa": {
    "welcome": "🌑 <b>ShadowClean Bot</b>\n\n👁 <b>استاک</b> - جستجوی پیام‌های کاربر در گروه‌های عمومی\n🧹 <b>ردپای من</b> - دیدن و حذف پیام‌هام\n\n💎 اعتبار: <b>{cr}</b>\n\n⚠️ فقط استفاده شخصی",
    "help": "❓ <b>راهنما</b>\n\n👁 <b>استاک</b> - یوزرنیم یا آیدی بده، پیام‌هاشو تو گروه‌های عمومی پیدا میکنه (بدون لاگین)\n\n🧹 <b>ردپای من</b> - پیام‌های خودتو ببین (بدون لاگین). برای حذف باید لاگین کنی\n\n📱 لاگین فقط برای حذف لازمه\n💎 {cr} اعتبار رایگان",
    "stalk_ask": "👁 <b>استاک</b>\n\n@username یا آیدی عددی هدف رو بفرستید:\n\n🔓 نیازی به لاگین نیست",
    "stalk_searching": "🔍 جستجو در گروه‌های عمومی...\nممکنه کمی طول بکشه",
    "stalk_panel": "👁 <b>{name}</b>\n\n📂 گروه‌ها: <b>{gr}</b>\n💬 کل پیام‌ها: <b>{msgs}</b>\n\nروی هر گروه بزنید:",
    "stalk_msgs": "👁 <b>{name} در {group}</b>\n\n",
    "stalk_not_found": "❌ کاربر یافت نشد یا پیامی در گروه‌های عمومی ندارد.\n\nمطمئن شوید یوزرنیم درسته.",
    "no_msgs": "💬 پیامی یافت نشد.",
    "footprint_info": "🧹 <b>ردپای دیجیتال من</b>\n\n📂 گروه‌ها: <b>{gr}</b>\n💬 پیام‌ها: <b>{msgs}</b>\n📸 مدیا: <b>{md}</b>\n📝 متن: <b>{tx}</b>",
    "footprint_need_login": "🧹 <b>ردپای من</b>\n\nبرای <b>اسکن</b> نیاز به لاگین دارید تا بتونم پیام‌هاتونو پیدا کنم.\n\n📱 ورود بزنید.",
    "footprint_confirm": "⚠️ <b>هشدار!</b>\n\n🗑️ <b>{msgs}</b> پیام از <b>{gr}</b> گروه حذف میشه!\n\n<b>برگشت‌ناپذیره!</b> مطمئنید؟",
    "footprint_done": "✅ <b>پاکسازی کامل!</b>\n\n🗑️ حذف: <b>{done}</b>\n📂 گروه: {gr}\n⏱️ {time}\n❌ خطا: {err}",
    "phone_ask": "📱 شماره با کد کشور:\n<code>+989121234567</code>\n\n🔐 AES-256 | ⏰ حذف ۲۴ ساعته",
    "code_ask": "📨 کد تأیید:", "2fa_ask": "🔐 رمز دوم:",
    "login_ok": "✅ ورود موفق!", "login_fail": "❌ خطا: {e}",
    "logout_ok": "✅ خارج شدید.", "not_logged": "❌ ابتدا 📱 ورود بزنید",
    "profile": "👤 <b>پروفایل</b>\n\n🆔 <code>{uid}</code>\n👤 {name}\n💎 اعتبار: <b>{cr}</b>\n📊 استفاده: {used}\n🔐 {login}\n📅 {date}",
    "processing": "⏳ صبر کنید...", "error": "❌ خطا: {e}",
    "banned": "🚫 مسدود شدید.",
    "no_credit": "❌ اعتبار تمام! با پشتیبانی تماس بگیرید.",
    "admin_panel": "👑 <b>مدیریت</b>\n👥 {total} | 🚫 {banned} | 🔐 {logged}",
    "a_credit_ask": "💎 <code>آیدی تعداد</code>\nمثال: <code>123456 10</code>",
    "a_credit_ok": "✅ +{n} به {uid} (فعلی: {total})",
    "a_credit_fail": "❌ فرمت: <code>آیدی تعداد</code>",
    "a_setcr_ask": "🔧 <code>آیدی تعداد</code>", "a_setcr_ok": "✅ {uid} = {n}",
    "a_ban_ask": "🚫 آیدی:", "a_ban_ok": "✅ {uid} بن شد.",
    "a_unban_ask": "✅ آیدی:", "a_unban_ok": "✅ {uid} آنبن شد.",
    "a_notfound": "❌ یافت نشد!",
    "a_lookup_ask": "🔎 آیدی:",
    "a_user_info": "📊 <code>{uid}</code>\n{name} | @{uname}\n💎{cr} | 📊{used} | {ban}\n📅 {date}",
    "a_bcast_ask": "📢 متن:", "a_bcast_ok": "✅ ارسال به {n} نفر.",
  },
  "en": {
    "welcome": "🌑 <b>ShadowClean</b>\n\n👁 <b>Stalk</b> - Find user msgs in public groups\n🧹 <b>Footprint</b> - View/delete my msgs\n\n💎 Credits: <b>{cr}</b>\n\n⚠️ Personal use only",
    "help": "❓ 👁Stalk=no login needed 🧹Footprint=login for delete only\n💎 {cr} free",
    "stalk_ask": "👁 Send @username or numeric ID:\n\n🔓 No login required",
    "stalk_searching": "🔍 Searching public groups...",
    "stalk_panel": "👁 <b>{name}</b>\n📂 {gr} groups | 💬 {msgs} msgs\nSelect:",
    "stalk_msgs": "👁 <b>{name} in {group}</b>\n\n",
    "stalk_not_found": "❌ User not found or no public messages.\nCheck username.",
    "no_msgs": "💬 No messages.", "footprint_need_login": "🧹 Login to scan your msgs.",
    "footprint_info": "🧹 📂{gr} 💬{msgs} 📸{md} 📝{tx}",
    "footprint_confirm": "⚠️ Delete {msgs} msgs from {gr} groups?\nIrreversible!",
    "footprint_done": "✅ Deleted:{done} Groups:{gr} Time:{time} Err:{err}",
    "phone_ask": "📱 <code>+989121234567</code>", "code_ask": "📨 Code:", "2fa_ask": "🔐 2FA:",
    "login_ok": "✅ OK!", "login_fail": "❌ {e}", "logout_ok": "✅ Out.",
    "not_logged": "❌ Login first", "profile": "👤 {uid}|{name}|💎{cr}|📊{used}|{login}|{date}",
    "processing": "⏳...", "error": "❌ {e}", "banned": "🚫 Banned.",
    "no_credit": "❌ No credits!",
    "admin_panel": "👑 {total}|🚫{banned}|🔐{logged}",
    "a_credit_ask": "💎 <code>ID amount</code>", "a_credit_ok": "✅ +{n} {uid} ({total})",
    "a_credit_fail": "❌ <code>ID amount</code>",
    "a_setcr_ask": "🔧 <code>ID amount</code>", "a_setcr_ok": "✅ {uid}={n}",
    "a_ban_ask": "🚫 ID:", "a_ban_ok": "✅ {uid} banned.",
    "a_unban_ask": "✅ ID:", "a_unban_ok": "✅ {uid} unbanned.",
    "a_notfound": "❌ Not found!", "a_lookup_ask": "🔎 ID:",
    "a_user_info": "{uid}|{name}|@{uname}|💎{cr}|📊{used}|{ban}|{date}",
    "a_bcast_ask": "📢 Text:", "a_bcast_ok": "✅ Sent {n}.",
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
        u = UserDB(id=uid, username=uname, first_name=fname, credits=DEFAULT_CREDITS, is_admin=uid in ADMIN_IDS)
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
    if u.is_admin or u.id in ADMIN_IDS: u.total_used += 1; await db.commit(); return True
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
    logged = len(r2.scalars().all()); return total, banned, logged

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
    s = SessionDB(user_id=uid, phone=phone, enc_session=fernet.encrypt(ss.encode()).decode(),
                   phone_hash=ph, expires=datetime.now(timezone.utc) + timedelta(hours=24))
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
# TELETHON (user sessions)
# ══════════════════════════════
user_clients: Dict[int, TelegramClient] = {}

async def get_user_client(uid, ss):
    if uid in user_clients and user_clients[uid].is_connected(): return user_clients[uid]
    c = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await c.connect(); user_clients[uid] = c; return c

async def new_user_client():
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect(); return c

# ══════════════════════════════
# MESSAGE LINK BUILDER
# ══════════════════════════════
def make_link(entity, msg_id):
    uname = getattr(entity, 'username', None)
    if uname: return f"https://t.me/{uname}/{msg_id}"
    eid = getattr(entity, 'id', 0)
    return f"https://t.me/c/{eid}/{msg_id}"

# ══════════════════════════════
# STALK ENGINE (using bot client - no login)
# ══════════════════════════════
async def resolve_user(client, target_str):
    """Find user by username or ID using multiple methods."""
    target_str = target_str.strip().lstrip("@")
    
    # Try as username
    try:
        return await client.get_entity(target_str)
    except: pass
    
    try:
        return await client.get_entity(f"@{target_str}")
    except: pass
    
    # Try as ID
    try:
        uid = int(target_str)
        return await client.get_entity(PeerUser(uid))
    except: pass
    
    try:
        uid = int(target_str)
        return await client.get_entity(uid)
    except: pass
    
    return None

async def stalk_search(client, target_id, cid, la):
    """Search target's messages in ALL dialogs the bot/user can see."""
    found = []
    total_msgs = 0
    
    try:
        dialogs = await client.get_dialogs(limit=500)
        
        pm = await send(cid, tx(la, "stalk_searching"))
        pmid = pm.get("result", {}).get("message_id")
        
        searchable = []
        for d in dialogs:
            ent = d.entity
            # Groups and supergroups
            if isinstance(ent, (Channel, Chat)):
                searchable.append(d)
        
        for i, d in enumerate(searchable):
            cnt = 0
            try:
                async for msg in client.iter_messages(d.entity, from_user=target_id, limit=200):
                    cnt += 1
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 30))
                continue
            except Exception:
                continue
            
            if cnt > 0:
                found.append({
                    "id": d.entity.id,
                    "title": getattr(d.entity, 'title', '?'),
                    "count": cnt,
                    "username": getattr(d.entity, 'username', None),
                })
                total_msgs += cnt
            
            # Update progress
            if pmid and (i + 1) % 10 == 0:
                pct = int((i + 1) / max(len(searchable), 1) * 100)
                try:
                    await edit(cid, pmid, f"🔍 {pct}% | {len(found)} groups found | {total_msgs} msgs")
                except: pass
        
        # Clean up progress message
        if pmid:
            try: await edit(cid, pmid, f"✅ Search done: {len(found)} groups, {total_msgs} messages")
            except: pass
    
    except Exception as e:
        print(f"stalk_search error: {e}\n{traceback.format_exc()}")
    
    return found, total_msgs

async def get_msgs_in_group(client, target_id, group_id, limit=30):
    """Get target's messages in specific group with links."""
    messages = []
    entity = None
    
    # Try to get entity
    try: entity = await client.get_entity(PeerChannel(group_id))
    except: pass
    if not entity:
        try: entity = await client.get_entity(group_id)
        except: pass
    if not entity:
        return messages
    
    try:
        async for msg in client.iter_messages(entity, from_user=target_id, limit=limit):
            txt = ""
            if msg.text:
                txt = msg.text[:200].replace("<", "&lt;").replace(">", "&gt;")
            elif msg.media:
                txt = "📎 [Media]"
            else:
                txt = "..."
            
            link = make_link(entity, msg.id)
            date = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
            
            messages.append({"text": txt, "date": date, "link": link})
    except Exception as e:
        print(f"get_msgs error: {e}")
    
    return messages

# ══════════════════════════════
# FOOTPRINT ENGINE (my own msgs)
# ══════════════════════════════
async def my_footprint_scan(client, cid, la):
    """Scan my own messages using user client."""
    res = {"groups": [], "total": 0, "media": 0, "text": 0}
    try:
        me = await client.get_me()
        dialogs = await client.get_dialogs(limit=500)
        groups = [d for d in dialogs if isinstance(d.entity, Channel) and getattr(d.entity, 'megagroup', False)]
        
        pm = await send(cid, tx(la, "processing"))
        pmid = pm.get("result", {}).get("message_id")
        
        for i, d in enumerate(groups):
            gc = gm = gt = 0
            try:
                async for m in client.iter_messages(d.entity, from_user=me.id):
                    gc += 1
                    if m.media: gm += 1
                    else: gt += 1
                if gc:
                    res["groups"].append({
                        "id": d.entity.id, "title": d.entity.title,
                        "count": gc, "media": gm, "text": gt
                    })
                    res["total"] += gc; res["media"] += gm; res["text"] += gt
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except: continue
            
            if pmid and (i + 1) % 3 == 0:
                pct = int((i + 1) / max(len(groups), 1) * 100)
                try: await edit(cid, pmid, f"📊 {pct}%...")
                except: pass
    except: pass
    return res

async def my_footprint_delete(client, cid, la):
    """Delete all my messages from supergroups."""
    res = {"done": 0, "err": 0, "gr": 0, "det": []}
    try:
        me = await client.get_me()
        dialogs = await client.get_dialogs(limit=500)
        groups = [d for d in dialogs if isinstance(d.entity, Channel) and getattr(d.entity, 'megagroup', False)]
        
        pm = await send(cid, tx(la, "processing"))
        pmid = pm.get("result", {}).get("message_id")
        start = time.time()
        
        for i, d in enumerate(groups):
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
                batch = ids[j:j + 50]
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
                pct = int((i + 1) / max(len(groups), 1) * 100)
                try: await edit(cid, pmid, f"🗑️ {pct}% | {res['done']} deleted")
                except: pass
    except: pass
    return res

# ══════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════
async def bg_stalk(uid, cid, target_str, la):
    """Stalk using bot client (no login needed) OR user client if logged in."""
    async with DBS() as db:
        # Try user client first (has more access)
        ss = await dec_sess(db, uid)
        if ss:
            client = await get_user_client(uid, ss)
        else:
            # Use bot client
            client = await get_bot_client()
        
        target = await resolve_user(client, target_str)
        if not target:
            await send(cid, tx(la, "stalk_not_found"))
            return
        
        target_id = target.id
        target_name = f'{getattr(target, "first_name", "") or ""} {getattr(target, "last_name", "") or ""}'.strip()
        if not target_name: target_name = target_str
        
        found, total = await stalk_search(client, target_id, cid, la)
        
        if not found:
            await send(cid, tx(la, "stalk_not_found"))
            return
        
        # Save state for group selection
        sset(uid, "stalk_view", target_id=target_id, target_name=target_name, items=found)
        
        txt = tx(la, "stalk_panel", name=target_name, gr=len(found), msgs=total)
        await send(cid, txt, kb_groups_inline(found, 0, 8, "sg"))

async def bg_stalk_msgs(uid, cid, group_id, la):
    """Show messages from target in specific group."""
    async with DBS() as db:
        _, sd = sget(uid)
        target_id = sd.get("target_id")
        target_name = sd.get("target_name", "?")
        if not target_id: return
        
        ss = await dec_sess(db, uid)
        if ss:
            client = await get_user_client(uid, ss)
        else:
            client = await get_bot_client()
        
        msgs = await get_msgs_in_group(client, target_id, group_id, limit=30)
        
        if not msgs:
            await send(cid, tx(la, "no_msgs")); return
        
        # Get group title
        group_title = "?"
        items = sd.get("items", [])
        for g in items:
            if g["id"] == group_id:
                group_title = g["title"]; break
        
        # Send in chunks
        for ci in range(0, len(msgs), 5):
            chunk = msgs[ci:ci + 5]
            txt = ""
            if ci == 0:
                txt = tx(la, "stalk_msgs", name=target_name, group=group_title)
            
            for m in chunk:
                link = f'(<a href="{m["link"]}">link</a>)' if m["link"] else ""
                txt += f'📅 <code>{m["date"]}</code> {link}\n💬 {m["text"]}\n{"─" * 25}\n'
            
            await send(cid, txt)
            await asyncio.sleep(0.3)

async def bg_footprint_scan(uid, cid, la):
    """Scan my footprint - needs user login."""
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "footprint_need_login"))
            return
        
        client = await get_user_client(uid, ss)
        r = await my_footprint_scan(client, cid, la)
        
        sset(uid, "fp_data", scan=r)
        
        logged = True
        txt = tx(la, "footprint_info", gr=len(r["groups"]), msgs=r["total"], md=r["media"], tx=r["text"])
        
        if r["groups"]:
            txt += "\n\n"
            for g in r["groups"][:20]:
                txt += f"• {g['title']}: {g['count']} ({g.get('media', 0)}📸)\n"
        
        await send(cid, txt, kb_footprint(la, logged_in=logged))

async def bg_footprint_delete(uid, cid, la):
    """Delete my footprint."""
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if not ss:
            await send(cid, tx(la, "not_logged")); return
        
        client = await get_user_client(uid, ss)
        start = time.time()
        r = await my_footprint_delete(client, cid, la)
        el = time.time() - start
        ts = f"{int(el // 60)}m {int(el % 60)}s"
        
        txt = tx(la, "footprint_done", done=r["done"], gr=r["gr"], time=ts, err=r["err"])
        if r["det"]:
            txt += "\n\n" + "\n".join(f"• {d}" for d in r["det"][:20])
        await send(cid, txt)

async def bg_login(uid, cid, phone, la):
    async with DBS() as db:
        try:
            client = await new_user_client()
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
            if not so or not so.enc_session: await send(cid, tx(la, "login_fail", e="No session")); return
            ss = fernet.decrypt(so.enc_session.encode()).decode()
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            _, sd = sget(uid)
            try:
                await client.sign_in(phone=sd.get("phone", so.phone), code=code,
                                      phone_code_hash=sd.get("ph", so.phone_hash))
                nss = client.session.save()
                await auth_sess(db, uid, nss); sdel(uid)
                await send(cid, tx(la, "login_ok"), kb_main(la, uid in ADMIN_IDS))
            except SessionPasswordNeededError:
                nss = client.session.save()
                so.enc_session = fernet.encrypt(nss.encode()).decode(); await db.commit()
                sset(uid, "2fa"); await send(cid, tx(la, "2fa_ask"))
            finally: await client.disconnect()
        except PhoneCodeInvalidError: await send(cid, tx(la, "login_fail", e="Wrong code"))
        except PhoneCodeExpiredError: sdel(uid); await send(cid, tx(la, "login_fail", e="Expired"))
        except Exception as e: await send(cid, tx(la, "login_fail", e=str(e)[:200]))

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
                await send(cid, tx(la, "login_ok"), kb_main(la, uid in ADMIN_IDS))
            finally: await client.disconnect()
        except PasswordHashInvalidError: await send(cid, tx(la, "login_fail", e="Wrong 2FA"))
        except Exception as e: await send(cid, tx(la, "login_fail", e=str(e)[:200]))

async def bg_logout(uid, cid, la):
    async with DBS() as db:
        ss = await dec_sess(db, uid)
        if ss:
            try:
                c = TelegramClient(StringSession(ss), API_ID, API_HASH)
                await c.connect(); await c.log_out(); await c.disconnect()
            except: pass
        await del_sess(db, uid); user_clients.pop(uid, None); sdel(uid)
        await send(cid, tx(la, "logout_ok"), kb_main(la, uid in ADMIN_IDS))

async def bg_broadcast(auid, cid, text, la):
    async with DBS() as db:
        users = await get_all_users(db); n = 0
        for u in users:
            if u.id == auid: continue
            try: await send(u.id, f"📢\n\n{text}"); n += 1; await asyncio.sleep(0.1)
            except: continue
        await send(cid, tx(la, "a_bcast_ok", n=n), kb_admin_menu(la))

# ══════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════
async def on_msg(db, msg, bg: BackgroundTasks):
    cid = msg.get("chat", {}).get("id")
    uid = msg.get("from", {}).get("id")
    fname = msg.get("from", {}).get("first_name", "")
    uname = msg.get("from", {}).get("username", "")
    text = (msg.get("text") or "").strip()
    if not cid or not uid or msg.get("chat", {}).get("type") != "private": return

    u = await get_user(db, uid, uname, fname)
    la = u.lang; ia = u.is_admin or uid in ADMIN_IDS
    if u.is_banned: await send(cid, tx(la, "banned")); return

    st, sd = sget(uid)

    # Login flow
    if st == "code": bg.add_task(bg_code, uid, cid, text, la); return
    if st == "2fa": bg.add_task(bg_2fa, uid, cid, text, la); return
    if st == "phone":
        ph = text if text.startswith("+") else "+" + text
        bg.add_task(bg_login, uid, cid, ph, la); return

    # Stalk target input
    if st == "stalk_input":
        sdel(uid)
        if not await has_credit(u): await send(cid, tx(la, "no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_stalk, uid, cid, text, la); return

    # Admin states
    if st == "a_credit" and ia:
        sdel(uid); parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            total = await add_credits(db, int(parts[0]), int(parts[1]))
            if total is not None: await send(cid, tx(la, "a_credit_ok", uid=parts[0], n=parts[1], total=total), kb_admin_menu(la))
            else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la, "a_credit_fail"), kb_admin_menu(la))
        return
    if st == "a_setcr" and ia:
        sdel(uid); parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            r = await set_credits(db, int(parts[0]), int(parts[1]))
            if r is not None: await send(cid, tx(la, "a_setcr_ok", uid=parts[0], n=parts[1]), kb_admin_menu(la))
            else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la, "a_credit_fail"), kb_admin_menu(la))
        return
    if st == "a_ban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await ban_user(db, int(text))
            await send(cid, tx(la, "a_ban_ok", uid=text) if ok else tx(la, "a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        return
    if st == "a_unban" and ia:
        sdel(uid)
        if text.isdigit():
            ok = await unban_user(db, int(text))
            await send(cid, tx(la, "a_unban_ok", uid=text) if ok else tx(la, "a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        return
    if st == "a_lookup" and ia:
        sdel(uid)
        if text.isdigit():
            tu = await lookup_user(db, int(text))
            if tu: await send(cid, tx(la, "a_user_info", uid=tu.id, name=tu.first_name or "?",
                uname=tu.username or "—", cr=tu.credits, used=tu.total_used,
                ban="🚫" if tu.is_banned else "✅",
                date=tu.joined.strftime("%Y-%m-%d") if tu.joined else "?"), kb_admin_menu(la))
            else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        else: await send(cid, tx(la, "a_notfound"), kb_admin_menu(la))
        return
    if st == "a_bcast" and ia:
        sdel(uid); bg.add_task(bg_broadcast, uid, cid, text, la); return

    # ── Keyboard Buttons ──
    if text in ["👁 استاک", "👁 Stalk"]:
        if not await has_credit(u): await send(cid, tx(la, "no_credit")); return
        sset(uid, "stalk_input")
        await send(cid, tx(la, "stalk_ask"), kb_back(la)); return

    if text in ["🧹 ردپای من", "🧹 My Footprint"]:
        # Check if logged in
        sess = await get_auth_session(db, uid)
        if sess:
            if not await has_credit(u): await send(cid, tx(la, "no_credit")); return
            await use_credit(db, uid)
            bg.add_task(bg_footprint_scan, uid, cid, la)
        else:
            await send(cid, tx(la, "footprint_need_login"), kb_main(la, ia))
        return

    if text in ["👤 پروفایل", "👤 Profile"]:
        sess = await get_auth_session(db, uid)
        await send(cid, tx(la, "profile", uid=uid, name=fname or uname or "?",
            cr="♾️" if ia else u.credits, used=u.total_used,
            login="✅" if sess else "❌",
            date=u.joined.strftime("%Y-%m-%d") if u.joined else "?"), kb_main(la, ia)); return

    if text in ["📱 ورود", "📱 Login"]:
        sset(uid, "phone"); await send(cid, tx(la, "phone_ask"), kb_back(la)); return

    if text in ["❓ راهنما", "❓ Help"]:
        await send(cid, tx(la, "help", cr=DEFAULT_CREDITS), kb_main(la, ia)); return

    if text in ["👑 مدیریت", "👑 Admin"] and ia:
        total, banned, logged = await get_stats(db)
        await send(cid, tx(la, "admin_panel", total=total, banned=banned, logged=logged), kb_admin_menu(la)); return

    if text in ["🔙 بازگشت", "🔙 Back"]:
        sdel(uid)
        await send(cid, tx(la, "welcome", cr="♾️" if ia else u.credits, used=u.total_used), kb_main(la, ia)); return

    # Admin buttons
    if ia:
        if text in ["💎 اعتبار", "💎 Credits"]: sset(uid, "a_credit"); await send(cid, tx(la, "a_credit_ask"), kb_back(la)); return
        if text in ["🔧 تنظیم", "🔧 Set"]: sset(uid, "a_setcr"); await send(cid, tx(la, "a_setcr_ask"), kb_back(la)); return
        if text in ["🔎 جستجو", "🔎 Lookup"]: sset(uid, "a_lookup"); await send(cid, tx(la, "a_lookup_ask"), kb_back(la)); return
        if text in ["🚫 بن", "🚫 Ban"]: sset(uid, "a_ban"); await send(cid, tx(la, "a_ban_ask"), kb_back(la)); return
        if text in ["✅ آنبن", "✅ Unban"]: sset(uid, "a_unban"); await send(cid, tx(la, "a_unban_ask"), kb_back(la)); return
        if text in ["📢 پیام", "📢 Broadcast"]: sset(uid, "a_bcast"); await send(cid, tx(la, "a_bcast_ask"), kb_back(la)); return

    # Commands
    if text.startswith("/start"):
        await send(cid, tx(la, "welcome", cr="♾️" if ia else u.credits, used=u.total_used), kb_main(la, ia)); return
    if text.startswith("/login"): sset(uid, "phone"); await send(cid, tx(la, "phone_ask"), kb_back(la)); return
    if text.startswith("/logout"): bg.add_task(bg_logout, uid, cid, la); return
    if text.startswith("/lang"):
        u.lang = "en" if u.lang == "fa" else "fa"; await db.commit()
        await send(cid, tx(u.lang, "welcome", cr="♾️" if ia else u.credits, used=u.total_used), kb_main(u.lang, ia)); return

    await send(cid, tx(la, "welcome", cr="♾️" if ia else u.credits, used=u.total_used), kb_main(la, ia))

# ══════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════
async def on_cb(db, cb, bg: BackgroundTasks):
    cbid = cb.get("id", "")
    uid = cb.get("from", {}).get("id")
    fname = cb.get("from", {}).get("first_name", "")
    uname = cb.get("from", {}).get("username", "")
    cid = cb.get("message", {}).get("chat", {}).get("id")
    mid = cb.get("message", {}).get("message_id")
    data = cb.get("data", "")
    if not uid or not cid: return
    await answer(cbid)

    u = await get_user(db, uid, uname, fname)
    la = u.lang; ia = u.is_admin or uid in ADMIN_IDS
    if u.is_banned: return

    # Stalk group click
    if data.startswith("sg_"):
        group_id = int(data[3:])
        bg.add_task(bg_stalk_msgs, uid, cid, group_id, la)
        return

    # Stalk pagination
    if data.startswith("sgp_"):
        page = int(data[4:])
        _, sd = sget(uid)
        items = sd.get("items", [])
        target_name = sd.get("target_name", "?")
        if items:
            total = sum(g.get("count", 0) for g in items)
            txt = tx(la, "stalk_panel", name=target_name, gr=len(items), msgs=total)
            await edit(cid, mid, txt, kb_groups_inline(items, page, 8, "sg"))
        return

    # Footprint
    if data == "fp_scan":
        if not await has_credit(u): await send(cid, tx(la, "no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_footprint_scan, uid, cid, la)
        return

    if data == "fp_delete":
        _, sd = sget(uid)
        scan = sd.get("scan", {})
        txt = tx(la, "footprint_confirm", msgs=scan.get("total", "?"), gr=len(scan.get("groups", [])))
        await edit(cid, mid, txt, kb_confirm(la))
        return

    if data == "fp_yes":
        if not await has_credit(u): await send(cid, tx(la, "no_credit")); return
        await use_credit(db, uid)
        bg.add_task(bg_footprint_delete, uid, cid, la)
        return

    if data == "fp_login":
        sset(uid, "phone")
        await send(cid, tx(la, "phone_ask"), kb_back(la))
        return

    if data == "back_main":
        sdel(uid)
        await send(cid, tx(la, "welcome", cr="♾️" if ia else u.credits, used=u.total_used), kb_main(la, ia))
        return

# ══════════════════════════════
# FASTAPI
# ══════════════════════════════
@asynccontextmanager
async def lifespan(a):
    print("🚀 ShadowClean v5.0")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Start bot client
    try:
        await get_bot_client()
        print("✅ Bot client connected!")
    except Exception as e:
        print(f"⚠️ Bot client failed: {e}")
    print(f"✅ DB | Admins: {ADMIN_IDS} | Credits: {DEFAULT_CREDITS}")
    yield
    if bot_client: await bot_client.disconnect()
    for c in user_clients.values():
        try: await c.disconnect()
        except: pass
    await engine.dispose()
    print("🛑 Off")

app = FastAPI(title="ShadowClean v5", lifespan=lifespan)

@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/")
async def root(): return {"ok": True}

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
