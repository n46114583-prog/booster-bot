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
MONGO_URI = "mongodb+srv://n46114583_db_user:0xzpnb1DlNfSCNA7@cluster0.de2uevc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

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

# Render ফেক পোর্ট
async def handle_ping(request):
    return web.Response(text="Bot is Running 24/7!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Render Server Port: {port}")

# অ্যান্টি-ড্রপ ব্যাকগ্রাউন্ড পিং
async def vc_keep_alive():
    while True:
        await asyncio.sleep(12)
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
        await event.respond("📩 **যে চ্যানেল/গ্রুপ থেকে লিভ নিতে চান তার লিংক দিন:**")

    elif data == "join_vc":
        user_states[uid] = {'step': 'vc_target'}
        await event.respond("🎙 **গ্রুপের ইউজারনেম বা লিংক দিন (যেমন: `@mygroup` বা `https://t.me/...`):**")

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
            try:
                await c.disconnect()
            except Exception: pass
        active_vc_sessions.clear()
        if keep_alive_task:
            keep_alive_task.cancel()
            keep_alive_task = None
        await event.respond("🛑 সব অ্যাকাউন্ট সফলভাবে VC ছেড়েছে!", buttons=main_menu())

    elif data == "react_view":
        user_states[uid] = {'step': 'react_target'}
        await event.respond("⚡ **পোস্ট লিংক দিন (যেমন: `t.me/channel/123`):**")

    elif data == "list_acc":
        sessions = list(sessions_col.find())
        if not sessions:
            await event.respond("📋 কোনো অ্যাকাউন্ট সংরক্ষিত নেই।")
        else:
            txt = "\n".join([f"• `{s.get('phone')}`" for s in sessions])
            await event.respond(f"📋 **সংরক্ষিত মোট অ্যাকাউন্ট ({len(sessions)} টি):**\n\n{txt}")

@bot.on(events.NewMessage)
async def message_flow(event):
    if event.raw_text.startswith('/'): return
    uid = event.sender_id
    state = user_states.get(uid, {})
    step = state.get('step')

    # ১. ফোন নম্বর
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
            await event.respond("📩 **কোডটি প্রতিটি সংখ্যার মাঝে স্পেস দিয়ে লিখুন (যেমন: `1 2 3 4 5`):**")
        except Exception as e:
            await client.disconnect()
            user_states.pop(uid, None)
            await event.respond(f"❌ কোড পাঠাতে ব্যর্থ: {e}")

    # ২. ওটিপি
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
            user_states.pop(uid, None)
            total = sessions_col.count_documents({})
            await event.respond(f"✅ **{state['phone']} সেভ হয়েছে!** (মোট: {total} টি)", buttons=main_menu())
        except SessionPasswordNeededError:
            user_states[uid]['step'] = '2fa'
            await event.respond("🔐 **2-Step Verification পাসওয়ার্ড দিন:**")
        except Exception as e:
            await event.respond(f"❌ ওটিপি ভুল: {e}\nআবার কোড দিন:")

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
            user_states.pop(uid, None)
            total = sessions_col.count_documents({})
            await event.respond(f"✅ **{state['phone']} সেভ হয়েছে!** (মোট: {total} টি)", buttons=main_menu())
        except Exception as e:
            await event.respond(f"❌ পাসওয়ার্ড ভুল: {e}")

    # ৩. ভয়েস চ্যাট / লাইভ জয়েন (সবগুলো অ্যাকাউন্টের জন্য ফিক্সড)
    elif step == 'vc_target':
        link = event.raw_text.strip()
        all_s = list(sessions_col.find())
        if not all_s:
            await event.respond("❌ কোনো অ্যাকাউন্ট নেই!")
            user_states.pop(uid, None)
            return

        status_msg = await event.respond(f"🔍 **লাইভ স্ক্যান করা হচ্ছে... ({len(all_s)} টি অ্যাকাউন্ট প্রস্তুত)**")

        # স্ক্যানার দিয়ে কল আইডি নিশ্চিত করা
        finder = TelegramClient(StringSession(all_s[0]["session"]), API_ID, API_HASH)
        await finder.connect()
        try:
            ent = await resolve_target(finder, link)
            full = await finder(GetFullChannelRequest(ent))
            call = full.full_chat.call
            if not call or not getattr(call, 'id', None):
                await status_msg.edit("⚠️ **গ্রুপে কোনো লাইভ বা ভয়েস চ্যাট চালু নেই!**")
                await finder.disconnect()
                user_states.pop(uid, None)
                return
            input_call = InputGroupCall(id=call.id, access_hash=call.access_hash)
        except Exception as e:
            await status_msg.edit(f"❌ গ্রুপ ভেরিফিকেশন ব্যর্থ: {e}")
            await finder.disconnect()
            user_states.pop(uid, None)
            return
        finally:
            await finder.disconnect()

        global active_vc_sessions, keep_alive_task
        joined = 0

        # সব অ্যাকাউন্ট দিয়ে ক্রমানুসারে প্রবেশ
        for idx, doc in enumerate(all_s, 1):
            phone = doc.get("phone", f"Acc-{idx}")
            await status_msg.edit(f"⏳ **[{idx}/{len(all_s)}] {phone} লাইভে প্রবেশ করছে...**")
            
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                # ১. আগে গ্রুপে মেম্বার হিসেবে নিশ্চিত হওয়া
                try:
                    await resolve_target(c, link)
                except Exception:
                    pass

                # ২. ইউজারের নিজস্ব আইডেন্টিটি তৈরি
                me = await c.get_me()
                peer = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
                
                # ৩. ইউনিক WebRTC রেন্ডম SSRC
                ssrc = random.randint(100000000, 999999999)
                payload = json.dumps({"ssrc": ssrc, "muted": True, "video_stopped": True})

                # ৪. কলে যোগ দেওয়া
                await c(JoinGroupCallRequest(
                    call=input_call,
                    join_as=peer,
                    params=DataJSON(data=payload),
                    muted=True,
                    video_stopped=True
                ))
                
                # ক্লায়েন্ট কানেকশন ব্যাকগ্রাউন্ডে সচল রাখা
                active_vc_sessions.append((c, input_call))
                joined += 1
                await asyncio.sleep(2)  # টেলিগ্রাম রেট-লিমিট বিরতি
            except Exception as err:
                print(f"Failed {phone}: {err}")
                try:
                    await c.disconnect()
                except Exception:
                    pass

        # পিং টাস্ক সক্রিয় করা
        if active_vc_sessions and not keep_alive_task:
            keep_alive_task = asyncio.create_task(vc_keep_alive())

        await status_msg.edit(
            f"🎉 **মোট `{joined}` টি অ্যাকাউন্ট সফলভাবে কলে যুক্ত হয়েছে!**\n"
            f"অ্যান্টি-ড্রপ গার্ড সক্রিয় আছে। বের করতে নিচের `LEAVE VC` চাপুন।", 
            buttons=main_menu()
        )
        user_states.pop(uid, None)

    # ৪. চ্যানেল জয়েন
    elif step == 'join_target':
        link = event.raw_text.strip()
        all_s = list(sessions_col.find())
        done = 0
        status_msg = await event.respond(f"⏳ সব অ্যাকাউন্ট জয়েন করানো হচ্ছে...")
        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                await resolve_target(c, link)
                done += 1
                await c.disconnect()
                await asyncio.sleep(2)
            except Exception: pass
        await status_msg.edit(f"🚀 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট জয়েন করেছে।", buttons=main_menu())
        user_states.pop(uid, None)

    # ৫. চ্যানেল লিভ
    elif step == 'leave_target':
        link = event.raw_text.strip().replace("https://t.me/", "").replace("@", "").split("/")[0]
        all_s = list(sessions_col.find())
        done = 0
        status_msg = await event.respond(f"⏳ সব অ্যাকাউন্ট থেকে লিভ নেওয়া হচ্ছে...")
        for doc in all_s:
            c = TelegramClient(StringSession(doc["session"]), API_ID, API_HASH)
            try:
                await c.connect()
                await c(LeaveChannelRequest(link))
                done += 1
                await c.disconnect()
                await asyncio.sleep(1.5)
            except Exception: pass
        await status_msg.edit(f"📩 **সম্পন্ন!** মোট `{done}` টি অ্যাকাউন্ট লিভ নিয়েছে।", buttons=main_menu())
        user_states.pop(uid, None)

    # ৬. ভিউ ও রিঅ্যাকশন
    elif step == 'react_target':
        link = event.raw_text.strip()
        try:
            parts = link.replace("https://t.me/", "").split("/")
            ch = parts[0]
            mid = int(parts[1])
        except Exception:
            await event.respond("❌ পোস্টের লিংক ফরম্যাট সঠিক নয়!")
            user_states.pop(uid, None)
            return

        all_s = list(sessions_col.find())
        done = 0
        status_msg = await event.respond(f"⚡ সব অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন দেওয়া হচ্ছে...")
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
        await status_msg.edit(f"⚡ **সম্পন্ন!** `{done}` টি অ্যাকাউন্ট দিয়ে ভিউ ও রিঅ্যাকশন সম্পন্ন।", buttons=main_menu())
        user_states.pop(uid, None)

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
