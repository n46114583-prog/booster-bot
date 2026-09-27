import os
import time
import json
import random
import asyncio
import certifi
from datetime import datetime
from aiohttp import web
from pymongo import MongoClient
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest, SendReactionRequest, GetMessagesViewsRequest
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest, CheckGroupCallRequest
from telethon.tl.types import InputGroupCall, DataJSON, InputPeerUser, ReactionEmoji
from telethon.errors import FloodWaitError, SessionPasswordNeededError

# --- কনফিগারেশন ---
API_ID = 35686806
API_HASH = "4ba8915f6e4f3aa73933c540ddaff4f7"
BOT_TOKEN = "8635265114:AAGjfhrafuLucHYzd32BhFsVy6PdF_php1E"
MONGO_URI = "mongodb+srv://N46114583_db_user:0xzpnb1DlNfSCNA7@cluster0.de2uevc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# ডাটাবেজ কানেকশন
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=5000
)
db = mongo_client["booster_cloud_db"]
sessions_col = db["user_sessions"]

bot = TelegramClient('cloud_booster_bot', API_ID, API_HASH)

START_TIME = time.time()
user_states = {}
active_vc_sessions = []
keep_alive_task = None

def get_uptime():
    sec = int(time.time() - START_TIME)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return f"{d}D {h}H {m}M" if d > 0 else f"{h}H {m}M"

def main_menu():
    return [
        [Button.inline("➕ ADD ACCOUNT", data="add_acc")],
        [Button.inline("🚀 JOIN CHANNEL", data="join_ch"), Button.inline("📩 LEAVE CHAT", data="leave_ch")],
        [Button.inline("🎙 JOIN VC", data="join_vc"), Button.inline("🛑 LEAVE VC", data="leave_vc")],
        [Button.inline("⚡ REACT + VIEW", data="react_view"), Button.inline("📋 ACCOUNTS LIST", data="list_acc")]
    ]

# Render-এর জন্য ব্যাকগ্রাউন্ড ডামি ওয়েব সার্ভার
async def handle_ping(request):
    return web.Response(text="Bot is Alive & Running 24/7 on Render!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Render Web Server started on port {port}")

async def vc_keep_alive():
    while True:
        await asyncio.sleep(15)
        for client, input_call in active_vc_sessions:
            try:
                await client(CheckGroupCallRequest(call=input_call))
            except Exception:
                pass

async def resolve_target(client, link):
    link = link.strip()
    if "t.me/+" in link or "joinchat/" in link:
        h = link.split("+")[-1].split("/")[-1]
        try:
            up = await client(ImportChatInviteRequest(h))
            return up.chats[0]
        except Exception:
            c = await client(CheckChatInviteRequest(h))
            return c.chat
    else:
        u = link.replace("https://t.me/", "").replace("@", "").split("/")[0]
        try:
            await client(JoinChannelRequest(u))
        except Exception:
            pass
        return await client.get_entity(u)

# --- /start ড্যাশবোর্ড ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    try:
        acc_count = sessions_col.count_documents({})
    except Exception:
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

# --- বাটন হ্যান্ডলার ---
@bot.on(events.CallbackQuery)
async def callback(event):
    uid = event.sender_id
    data = event.data.decode('utf-8')

    if data == "add_acc":
        user_states[uid] = {'step': 'phone'}
        await event.respond("📱 **টেলিগ্রাম নম্বর দিন (+880...):**")

    elif data == "join_ch":
        user_states[uid] = {'step': 'join_target'}
        await event.respond("🚀 **যে চ্যানেল বা গ্রুপে জয়েন করাতে চান তার লিংক দিন:**")

    elif data == "leave_ch":
        user_states[uid] = {'step': 'leave_target'}
        await event.respond("📩 **যে চ্যানেল/গ্রুপ থেকে লিভ নিতে চান তার লিংক বা ইউজারনেম দিন:**")

    elif data == "join_vc":
        user_states[uid] = {'step': 'vc_target'}
        await event.respond("🎙 **লাইভ/ভয়েস চ্যাট চলছে এমন গ্রুপের লিংক দিন:**")

    elif data == "leave_vc":
        global active_vc_sessions, keep_alive_task
        if not active_vc_sessions:
            await event.respond("⚠️ কোনো অ্যাকাউন্ট বর্তমানে VC-তে নেই।")
            return
        await event.respond("⏳ সব অ্যাকাউন্ট VC থেকে বের করা হচ্ছে...")
        for c, ic in active_vc_sessions:
            try:
                await c(LeaveGroupCallRequest(call=ic))
            except Exception: pass
            await c.disconnect()
        active_vc_sessions.clear()
        if keep_alive_task:
            keep_alive_task.cancel()
            keep_alive_task = None
        await event.respond("🛑 সব অ্যাকাউন্ট সফলভাবে VC ছেড়েছে!", buttons=main_menu())

    elif data == "react_view":
        user_states[uid] = {'step': 'react_target'}
        await event.respond("⚡ **পোস্টের লিংক দিন (যেমন: `t.me/channel/123`):**")

    elif data == "list_acc":
        sessions = list(sessions_col.find())
        if not sessions:
            await event.respond("📋 কোনো অ্যাকাউন্ট সংরক্ষিত নেই।")
        else:
            txt = "\n".join([f"• `{s.get('phone')}`" for s in sessions[:20]])
            await event.respond(f"📋 **মোট অ্যাকাউন্ট ({len(sessions)} টি):**\n\n{txt}")

# --- ইনপুট প্রসেসিং ---
@bot.on(events.NewMessage)
async def message_flow(event):
    if event.raw_text.startswith('/'): return
    uid = event.sender_id
    state = user_states.get(uid, {})
    step = state.get('step')

    # ১. ফোন নম্বর গ্রহণ
    if step == 'phone':
        phone = event.raw_text.strip().replace(" ", "")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            req = await client.send_code_request(phone)
            user_states[uid] = {
                'step': 'otp',
                'client': client,
                'phone': phone,
                'hash': req.phone_code_hash
            }
            await event.respond("📩 **কোডটি পাঠান (প্রতিটি সংখ্যার মাঝে স্পেস দিয়ে লিখুন, যেমন: `1 2 3 4 5`):**")
        except Exception as e:
            await event.respond(f"❌ কোড পাঠাতে ব্যর্থ: {e}")
            user_states.pop(uid, None)

    # ২. ওটিপি ও 2FA গ্রহণ
    elif step == 'otp':
        code = event.raw_text.strip().replace(" ", "")
        client = state['client']
        try:
            await client.sign_in(phone=state['phone'], code=code, phone_code_hash=state['hash'])
            s_str = client.session.save()
            sessions_col.update_one(
                {"phone": state['phone']},
                {"$set": {"phone": state['phone'], "session": s_str, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            await client.disconnect()
            await event.respond(f"✅ **{state['phone']} সফলভাবে সেভ হয়েছে!**", buttons=main_menu())
            user_states.pop(uid, None)
        except SessionPasswordNeededError:
            user_states[uid]['step'] = '2fa'
            await event.respond("🔐 **2-Step Verification পাসওয়ার্ড দিন:**")
        except Exception as e:
            await event.respond(f"❌ ওটিপি ভুল: {e}")

    elif step == '2fa':
        pwd = event.raw_text.strip()
        client = state['client']
        try:
            await client.sign_in(password=pwd)
            s_str = client.session.save()
            sessions_col.update_one(
                {"phone": state['phone']},
                {"$set": {"phone": state['phone'], "session": s_str, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            await client.disconnect()
            await event.respond(f"✅ **{state['phone']} সফলভাবে সেভ হয়েছে!**", buttons=main_menu())
            user_states.pop(uid, None)
        except Exception as e:
            await event.respond(f"❌ পাসওয়ার্ড ভুল: {e}")

    # ৩. চ্যানেল জয়েন
    elif step == 'join_target':
        link = event.raw_text.strip()
        await event.respond("⏳ সব অ্যাকাউন্ট জয়েন করানো হচ্ছে...")
        all_s = list(sessions_col.find())
        done = 0
        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                await resolve_target(c, link)
                done += 1
                await c.disconnect()
                await asyncio.sleep(2)
            except Exception: pass
        await event.respond(f"🚀 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট জয়েন করেছে।", buttons=main_menu())
        user_states.pop(uid, None)

    # ৪. চ্যানেল লিভ
    elif step == 'leave_target':
        link = event.raw_text.strip().replace("https://t.me/", "").replace("@", "").split("/")[0]
        await event.respond("⏳ সব অ্যাকাউন্ট থেকে লিভ নেওয়া হচ্ছে...")
        all_s = list(sessions_col.find())
        done = 0
        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                await c(LeaveChannelRequest(link))
                done += 1
                await c.disconnect()
                await asyncio.sleep(1.5)
            except Exception: pass
        await event.respond(f"📩 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট লিভ নিয়েছে।", buttons=main_menu())
        user_states.pop(uid, None)

    # ৫. লাইভ / VC জয়েন (Anti-Drop Enabled)
    elif step == 'vc_target':
        link = event.raw_text.strip()
        await event.respond("🔍 **লাইভ স্ক্যান করা হচ্ছে...**")
        all_s = list(sessions_col.find())
        if not all_s:
            await event.respond("❌ কোনো অ্যাকাউন্ট নেই!")
            user_states.pop(uid, None)
            return

        finder = TelegramClient(StringSession(all_s[0]["session"]), API_ID, API_HASH)
        await finder.connect()
        try:
            ent = await resolve_target(finder, link)
            full = await finder(GetFullChannelRequest(ent))
            call = full.full_chat.call
            if not call or not getattr(call, 'id', None):
                await event.respond("⚠️ এই গ্রুপে বর্তমানে কোনো লাইভ বা VC চালু নেই!")
                await finder.disconnect()
                user_states.pop(uid, None)
                return
            input_call = InputGroupCall(id=call.id, access_hash=call.access_hash)
        except Exception as e:
            await event.respond(f"❌ গ্রুপ ভেরিফিকেশন ব্যর্থ: {e}")
            await finder.disconnect()
            user_states.pop(uid, None)
            return
        finally:
            await finder.disconnect()

        await event.respond("🎙 **অ্যাকাউন্টগুলো লাইভে প্রবেশ করছে (Anti-Drop গার্ড সহ)...**")
        global active_vc_sessions, keep_alive_task
        joined = 0

        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                await resolve_target(c, link)
                me = await c.get_me()
                peer = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
                
                ssrc = random.randint(10000000, 99999999)
                payload = json.dumps({"ssrc": ssrc, "muted": True, "video_stopped": True})
                
                await c(JoinGroupCallRequest(
                    call=input_call,
                    join_as=peer,
                    params=DataJSON(data=payload),
                    muted=True,
                    video_stopped=True
                ))
                active_vc_sessions.append((c, input_call))
                joined += 1
                await asyncio.sleep(2)
            except Exception:
                await c.disconnect()

        if active_vc_sessions and not keep_alive_task:
            keep_alive_task = asyncio.create_task(vc_keep_alive())

        await event.respond(f"🎉 **মোট `{joined}` টি অ্যাকাউন্ট লাইভে প্রবেশ করেছে!**\nবের করতে নিচের `LEAVE VC` বাটনে চাপুন।", buttons=main_menu())
        user_states.pop(uid, None)

    # ৬. রিঅ্যাকশন ও ভিউ
    elif step == 'react_target':
        link = event.raw_text.strip()
        try:
            parts = link.replace("https://t.me/", "").split("/")
            ch = parts[0]
            mid = int(parts[1])
        except Exception:
            await event.respond("❌ পোস্ট লিংক ভুল! আবার চেষ্টা করুন।")
            user_states.pop(uid, None)
            return

        await event.respond("⚡ **সব অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন দেওয়া হচ্ছে...**")
        all_s = list(sessions_col.find())
        done = 0
        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                ent = await c.get_entity(ch)
                await c(GetMessagesViewsRequest(peer=ent, id=[mid], increment=True))
                await c(SendReactionRequest(peer=ent, msg_id=mid, reaction=[ReactionEmoji(emoticon="🔥")]))
                done += 1
                await c.disconnect()
                await asyncio.sleep(1.2)
            except Exception: pass
        await event.respond(f"⚡ **সম্পন্ন!** `{done}` টি অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন দেওয়া হয়েছে।", buttons=main_menu())
        user_states.pop(uid, None)

# মূল স্টার্টআপ ফাংশন
async def main():
    await start_dummy_server()
    await bot.start(bot_token=BOT_TOKEN)
    print("Cloud Booster Bot is Running...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
