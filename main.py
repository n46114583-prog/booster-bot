# -*- coding: utf-8 -*-
"""
Cloud Booster Bot
Multi-account Telegram manager with VC join, bulk join/leave, auto view & react.
Fully configured with all credentials and multi-account anti-drop engine.
"""

import os
import time
import json
import random
import asyncio
import certifi
import logging
from datetime import datetime, timezone

# ---- Web server (Render health check) ----
from aiohttp import web

# ---- Telegram ----
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
)
from telethon.tl.functions.messages import (
    ImportChatInviteRequest, CheckChatInviteRequest,
    SendReactionRequest, GetMessagesViewsRequest,
    GetFullChatRequest
)
from telethon.tl.functions.phone import (
    JoinGroupCallRequest, LeaveGroupCallRequest, CheckGroupCallRequest
)
from telethon.tl.types import (
    InputGroupCall, DataJSON, InputPeerSelf, ReactionEmoji, Channel, Chat
)
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, UserAlreadyParticipantError
)

# ---- MongoDB ----
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ---- Encryption ----
from cryptography.fernet import Fernet, InvalidToken

# ============ Logging ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("booster")

# ============ ক্রেডেনশিয়াল ও কনফিগারেশন ============
API_ID = 35686806
API_HASH = "4ba8915f6e4f3aa73933c540ddaff4f7"
BOT_TOKEN = "8635265114:AAGjfhrafuLucHYzd32BhFsVy6PdF_php1E"
MONGO_URI = "mongodb+srv://n46114583_db_user:0xzpnb1DlNfSCNA7@cluster0.de2uevc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# এনক্রিপশন কি (স্থায়ী ও নিরাপদ)
ENCRYPT_KEY = b"k9_8Z1eW2R4u_Q6vX8yB0cE2gH4jK6mP8rT0vX2zB4I="
fernet = Fernet(ENCRYPT_KEY)

# ============ MongoDB সংযোগ ============
try:
    mongo_client = MongoClient(
        MONGO_URI,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
    )
    db = mongo_client["booster_cloud_db"]
    sessions_col = db["user_sessions"]
    sessions_col.create_index("phone", unique=True)
    log.info("✅ MongoDB connected successfully")
except Exception as e:
    raise SystemExit(f"❌ MongoDB connection failed: {e}")

# ============ Telegram Bot ============
bot = TelegramClient("cloud_booster_bot", API_ID, API_HASH)

# ============ State ============
START_TIME = time.time()
user_states: dict = {}
active_vc_sessions: list = []
keep_alive_task: asyncio.Task | None = None
vc_lock = asyncio.Lock()

REACTIONS = ["🔥", "👍", "❤️", "🎉", "⚡", "💯", "🏆", "👏", "😍", "🚀"]

# ============ Helpers ============
def get_uptime() -> str:
    sec = int(time.time() - START_TIME)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return f"{d}D {h}H {m}M" if d > 0 else f"{h}H {m}M"

def encrypt_session(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def decrypt_session(s: str) -> str:
    try:
        return fernet.decrypt(s.encode()).decode()
    except InvalidToken:
        return s  # যদি কোনোটি আগে এনক্রিপ্ট ছাড়া সেভ হয়ে থাকে

def main_menu():
    return [
        [Button.inline("➕ ADD ACCOUNT", data="add_acc")],
        [Button.inline("🚀 JOIN CHANNEL", data="join_ch"),
         Button.inline("📩 LEAVE CHAT", data="leave_ch")],
        [Button.inline("🎙 JOIN VC", data="join_vc"),
         Button.inline("🛑 LEAVE VC", data="leave_vc")],
        [Button.inline("⚡ REACT + VIEW", data="react_view"),
         Button.inline("📋 ACCOUNTS LIST", data="list_acc")],
        [Button.inline("❌ CANCEL", data="cancel")],
    ]

async def safe_disconnect(client: TelegramClient):
    try:
        if client and client.is_connected():
            await client.disconnect()
    except Exception:
        pass

async def safe_edit(msg, text: str, **kwargs):
    try:
        await msg.edit(text, **kwargs)
    except Exception as e:
        log.debug(f"edit failed: {e}")

async def make_client(session_str: str = "") -> TelegramClient:
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    return c

# ============ Render Dummy Server (Port 8080/Env) ============
async def handle_ping(request):
    return web.Response(text="Bot is Running 24/7 on Render!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Web server running on port {port}")

# ============ VC Anti-Drop ============
async def vc_keep_alive():
    while True:
        try:
            await asyncio.sleep(15)
            async with vc_lock:
                snapshot = list(active_vc_sessions)
            if not snapshot:
                break
            for entry in snapshot:
                client = entry["client"]
                input_call = entry["input_call"]
                try:
                    if not client.is_connected():
                        await client.connect()
                    await client(CheckGroupCallRequest(call=input_call))
                except FloodWaitError:
                    pass
                except Exception as e:
                    log.debug(f"keepalive {entry.get('phone')}: {e}")
        except asyncio.CancelledError:
            log.info("Keep-alive task cancelled")
            break
        except Exception as e:
            log.error(f"keep-alive loop error: {e}")
            await asyncio.sleep(5)

# ============ Target Resolvers ============
async def resolve_and_join(client: TelegramClient, link: str):
    link = link.strip()
    if "t.me/+" in link or "joinchat/" in link:
        h = link.split("+")[-1].split("/")[-1]
        try:
            up = await client(ImportChatInviteRequest(h))
            return up.chats[0]
        except UserAlreadyParticipantError:
            c = await client(CheckChatInviteRequest(h))
            return c.chat
        except Exception:
            c = await client(CheckChatInviteRequest(h))
            return c.chat
    else:
        u = link.replace("https://t.me/", "").replace("@", "").split("/")[0]
        try:
            await client(JoinChannelRequest(u))
        except UserAlreadyParticipantError:
            pass
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
        except Exception:
            pass
        return await client.get_entity(u)

async def resolve_only(client: TelegramClient, link: str):
    link = link.strip()
    if "t.me/+" in link or "joinchat/" in link:
        h = link.split("+")[-1].split("/")[-1]
        c = await client(CheckChatInviteRequest(h))
        return c.chat
    u = link.replace("https://t.me/", "").replace("@", "").split("/")[0]
    return await client.get_entity(u)

async def get_call_from_entity(client: TelegramClient, ent):
    try:
        if isinstance(ent, Channel):
            full = await client(GetFullChannelRequest(ent))
            call = full.full_chat.call
        else:
            full = await client(GetFullChatRequest(chat_id=ent.id))
            call = getattr(full.full_chat, "call", None)

        if not call or not getattr(call, "id", None):
            return None
        return InputGroupCall(id=call.id, access_hash=call.access_hash)
    except Exception as e:
        log.debug(f"get_call_from_entity: {e}")
        return None

# ============ Bot: /start ============
@bot.on(events.NewMessage(pattern="/start"))
async def start_cmd(event):
    try:
        acc_count = sessions_col.count_documents({})
    except PyMongoError:
        acc_count = 0

    text = (
        "🤖 **JOIN / LEAVE & AUTO VIEWER PRO**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 **ACCOUNTS :** `{acc_count}`\n"
        f"🎙 **ACTIVE VC :** `{len(active_vc_sessions)}`\n"
        f"🕒 **UPTIME :** `{get_uptime()}`\n"
        "⚡ **AUTO MONITOR :** `ACTIVE (ANTI-DROP)` 🟢\n"
        "🛡 **DATABASE :** `CLOUD ATLAS` 🔒\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 **SELECT AN OPTION FROM BELOW :**"
    )
    await event.respond(text, buttons=main_menu())

# ============ Bot: Callbacks ============
@bot.on(events.CallbackQuery)
async def callback(event):
    uid = event.sender_id
    data = event.data.decode("utf-8")

    if data == "cancel":
        user_states.pop(uid, None)
        await event.respond("❌ বাতিল করা হয়েছে।", buttons=main_menu())
        return

    if data == "add_acc":
        user_states[uid] = {"step": "phone"}
        await event.respond("📱 **টেলিগ্রাম নম্বর দিন (+880...):**")

    elif data == "join_ch":
        user_states[uid] = {"step": "join_target"}
        await event.respond("🚀 **যে চ্যানেল/গ্রুপে জয়েন করাতে চান তার লিংক দিন:**")

    elif data == "leave_ch":
        user_states[uid] = {"step": "leave_target"}
        await event.respond("📩 **যে চ্যানেল/গ্রুপ থেকে লিভ নিতে চান তার লিংক দিন:**")

    elif data == "join_vc":
        user_states[uid] = {"step": "vc_target"}
        await event.respond("🎙 **গ্রুপের ইউজারনেম বা লিংক দিন:**")

    elif data == "leave_vc":
        global keep_alive_task
        async with vc_lock:
            snapshot = list(active_vc_sessions)
            active_vc_sessions.clear()

        if not snapshot:
            await event.respond("⚠️ কোনো অ্যাকাউন্ট বর্তমানে VC-তে নেই।")
            return

        await event.respond(f"⏳ {len(snapshot)} টি অ্যাকাউন্ট VC থেকে বের করা হচ্ছে...")
        for entry in snapshot:
            c = entry["client"]
            try:
                await c(LeaveGroupCallRequest(call=entry["input_call"]))
            except Exception:
                pass
            await safe_disconnect(c)

        if keep_alive_task:
            keep_alive_task.cancel()
            try:
                await keep_alive_task
            except asyncio.CancelledError:
                pass
            keep_alive_task = None

        await event.respond("🛑 সব অ্যাকাউন্ট সফলভাবে VC ছেড়েছে!", buttons=main_menu())

    elif data == "react_view":
        user_states[uid] = {"step": "react_target"}
        await event.respond("⚡ **পোস্ট লিংক দিন (যেমন: `t.me/channel/123`):**")

    elif data == "list_acc":
        sessions = list(sessions_col.find({}, {"phone": 1, "updated_at": 1}))
        if not sessions:
            await event.respond("📋 কোনো অ্যাকাউন্ট সংরক্ষিত নেই।")
            return
        txt = "\n".join([f"• `{s.get('phone', '?')}`" for s in sessions])
        await event.respond(
            f"📋 **সংরক্ষিত মোট অ্যাকাউন্ট ({len(sessions)} টি):**\n\n{txt}"
        )

# ============ Bot: Message Flow ============
@bot.on(events.NewMessage)
async def message_flow(event):
    uid = event.sender_id
    if not event.raw_text or event.raw_text.startswith("/"):
        return

    state = user_states.get(uid, {})
    step = state.get("step")
    if not step:
        return

    # ---- 1. Phone ----
    if step == "phone":
        phone = event.raw_text.strip().replace(" ", "")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            req = await client.send_code_request(phone)
            user_states[uid] = {
                "step": "otp",
                "client": client,
                "phone": phone,
                "hash": req.phone_code_hash,
            }
            await event.respond(
                "📩 **কোডটি প্রতিটি সংখ্যার মাঝে স্পেস দিয়ে লিখুন (যেমন `1 2 3 4 5`):**"
            )
        except Exception as e:
            await safe_disconnect(client)
            user_states.pop(uid, None)
            await event.respond(f"❌ কোড পাঠাতে ব্যর্থ: {e}", buttons=main_menu())

    # ---- 2. OTP ----
    elif step == "otp":
        code = event.raw_text.strip().replace(" ", "")
        client = state["client"]
        try:
            await client.sign_in(
                phone=state["phone"], code=code, phone_code_hash=state["hash"]
            )
            s_str = client.session.save()
            sessions_col.update_one(
                {"phone": state["phone"]},
                {"$set": {
                    "phone": state["phone"],
                    "session": encrypt_session(s_str),
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            await safe_disconnect(client)
            user_states.pop(uid, None)
            total = sessions_col.count_documents({})
            await event.respond(
                f"✅ **{state['phone']} সেভ হয়েছে!** (মোট: {total} টি)",
                buttons=main_menu(),
            )
        except SessionPasswordNeededError:
            user_states[uid]["step"] = "2fa"
            await event.respond("🔐 **2-Step Verification পাসওয়ার্ড দিন:**")
        except Exception as e:
            await event.respond(f"❌ ওটিপি ভুল: {e}\nআবার কোড দিন:")

    # ---- 2FA ----
    elif step == "2fa":
        pwd = event.raw_text.strip()
        client = state["client"]
        try:
            await client.sign_in(password=pwd)
            s_str = client.session.save()
            sessions_col.update_one(
                {"phone": state["phone"]},
                {"$set": {
                    "phone": state["phone"],
                    "session": encrypt_session(s_str),
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            await safe_disconnect(client)
            user_states.pop(uid, None)
            total = sessions_col.count_documents({})
            await event.respond(
                f"✅ **{state['phone']} সেভ হয়েছে!** (মোট: {total} টি)",
                buttons=main_menu(),
            )
        except Exception as e:
            await event.respond(f"❌ পাসওয়ার্ড ভুল: {e}")

    # ---- 3. JOIN VC (সমান্তরাল ও পার-অ্যাকাউন্ট হ্যাশ ফিক্সড) ----
    elif step == "vc_target":
        link = event.raw_text.strip()
        all_s = list(sessions_col.find())
        if not all_s:
            await event.respond("❌ কোনো অ্যাকাউন্ট নেই!", buttons=main_menu())
            user_states.pop(uid, None)
            return

        status = await event.respond(
            f"🔍 **লাইভ স্ক্যান করা হচ্ছে... ({len(all_s)} টি অ্যাকাউন্ট)**"
        )

        # প্রাথমিক স্ক্যানার
        try:
            finder = await make_client(decrypt_session(all_s[0]["session"]))
            ent = await resolve_and_join(finder, link)
            input_call = await get_call_from_entity(finder, ent)
            await safe_disconnect(finder)
            if not input_call:
                await safe_edit(status, "⚠️ **গ্রুপে কোনো লাইভ বা ভয়েস চ্যাট চালু নেই!**")
                user_states.pop(uid, None)
                return
        except Exception as e:
            await safe_edit(status, f"❌ গ্রুপ ভেরিফিকেশন ব্যর্থ: {e}")
            user_states.pop(uid, None)
            return

        # আগের ঝুলন্ত সেশন ডিসকানেক্ট করা
        global keep_alive_task
        async with vc_lock:
            for old in active_vc_sessions:
                try:
                    await old["client"](LeaveGroupCallRequest(call=old["input_call"]))
                except Exception: pass
                await safe_disconnect(old["client"])
            active_vc_sessions.clear()

        joined = 0
        success_list = []

        # প্রতিটি অ্যাকাউন্ট দিয়ে ক্রমানুসারে প্রবেশ
        for idx, doc in enumerate(all_s, 1):
            phone = doc.get("phone", f"Acc-{idx}")
            await safe_edit(status, f"⏳ **[{idx}/{len(all_s)}] {phone} লাইভে প্রবেশ করছে...**")

            c = None
            try:
                c = await make_client(decrypt_session(doc["session"]))
                ent_acc = await resolve_and_join(c, link)
                
                # এই অ্যাকাউন্টের নিজস্ব access_hash গ্রহণ করা
                ic = await get_call_from_entity(c, ent_acc) or input_call

                ssrc = random.randint(10000000, 99999999)
                payload = json.dumps({
                    "ssrc": ssrc,
                    "muted": True,
                    "video_stopped": True
                })

                await c(JoinGroupCallRequest(
                    call=ic,
                    join_as=InputPeerSelf(),
                    params=DataJSON(data=payload),
                    muted=True,
                    video_stopped=True,
                ))

                async with vc_lock:
                    active_vc_sessions.append({
                        "client": c,
                        "input_call": ic,
                        "phone": phone,
                    })
                joined += 1
                success_list.append(phone)
                await asyncio.sleep(2.5)  # টেলিগ্রাম রেট লিমিট নিরাপদ বিরতি

            except FloodWaitError as e:
                log.warning(f"FloodWait for {phone}: {e.seconds}s")
                await safe_disconnect(c)
                await asyncio.sleep(min(e.seconds, 30))
            except Exception as err:
                log.error(f"VC join {phone}: {err}")
                await safe_disconnect(c)

        if active_vc_sessions and (not keep_alive_task or keep_alive_task.done()):
            keep_alive_task = asyncio.create_task(vc_keep_alive())

        res_str = "\n".join([f"• `{p}` 🟢" for p in success_list]) if success_list else "কোনোটি নয়"
        await safe_edit(
            status,
            f"🎉 **মোট `{joined}` টি অ্যাকাউন্ট কলে যুক্ত হয়েছে!**\n\n{res_str}\n\n"
            f"🛡 Anti-drop guard সক্রিয়। LEAVE VC চাপলে সব বের হবে।",
            buttons=main_menu(),
        )
        user_states.pop(uid, None)

    # ---- 4. JOIN CHANNEL ----
    elif step == "join_target":
        link = event.raw_text.strip()
        all_s = list(sessions_col.find())
        done = 0
        status = await event.respond("⏳ সব অ্যাকাউন্ট জয়েন করানো হচ্ছে...")
        for doc in all_s:
            c = None
            try:
                c = await make_client(decrypt_session(doc["session"]))
                await resolve_and_join(c, link)
                done += 1
                await asyncio.sleep(2)
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                log.debug(f"join fail: {e}")
            finally:
                if c:
                    await safe_disconnect(c)
        await safe_edit(
            status,
            f"🚀 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট জয়েন করেছে।",
            buttons=main_menu(),
        )
        user_states.pop(uid, None)

    # ---- 5. LEAVE CHANNEL ----
    elif step == "leave_target":
        raw = event.raw_text.strip()
        target = raw.replace("https://t.me/", "").replace("@", "").split("/")[0]
        all_s = list(sessions_col.find())
        done = 0
        status = await event.respond("⏳ সব অ্যাকাউন্ট থেকে লিভ নেওয়া হচ্ছে...")
        for doc in all_s:
            c = None
            try:
                c = await make_client(decrypt_session(doc["session"]))
                try:
                    await c(LeaveChannelRequest(target))
                    done += 1
                except Exception:
                    try:
                        ent = await resolve_only(c, raw)
                        await c(LeaveChannelRequest(ent))
                        done += 1
                    except Exception:
                        pass
                await asyncio.sleep(1.5)
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception:
                pass
            finally:
                if c:
                    await safe_disconnect(c)
        await safe_edit(
            status,
            f"📩 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট লিভ নিয়েছে।",
            buttons=main_menu(),
        )
        user_states.pop(uid, None)

    # ---- 6. REACT + VIEW ----
    elif step == "react_target":
        link = event.raw_text.strip()
        try:
            parts = link.replace("https://t.me/", "").split("/")
            ch = parts[0]
            mid = int(parts[1])
        except Exception:
            await event.respond("❌ পোস্টের লিংক ফরম্যাট সঠিক নয়!",
                                buttons=main_menu())
            user_states.pop(uid, None)
            return

        all_s = list(sessions_col.find())
        done = 0
        status = await event.respond(
            "⚡ সব অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন দেওয়া হচ্ছে..."
        )
        for doc in all_s:
            c = None
            try:
                c = await make_client(decrypt_session(doc["session"]))
                ent = await c.get_entity(ch)
                await c(GetMessagesViewsRequest(
                    peer=ent, id=[mid], increment=True
                ))
                emoji = random.choice(REACTIONS)
                try:
                    await c(SendReactionRequest(
                        peer=ent, msg_id=mid,
                        reaction=[ReactionEmoji(emoticon=emoji)],
                    ))
                except Exception:
                    pass
                done += 1
                await asyncio.sleep(1.2)
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception:
                pass
            finally:
                if c:
                    await safe_disconnect(c)
        await safe_edit(
            status,
            f"⚡ **সম্পন্ন!** `{done}` টি অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন সম্পন্ন।",
            buttons=main_menu(),
        )
        user_states.pop(uid, None)

# ============ Boot ============
async def run_bot():
    await start_dummy_server()
    await bot.start(bot_token=BOT_TOKEN)
    log.info("🚀 Cloud Booster Bot is Running...")
    await bot.run_until_disconnected()

async def main():
    while True:
        try:
            await run_bot()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.exception(f"[CRASH] {e} — restarting in 10s")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
