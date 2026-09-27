import asyncio
import certifi
import telethon
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.phone import JoinGroupCallRequest
from telethon.tl.types import InputGroupCall, DataJSON
from pymongo import MongoClient

# আপনার ক্রেডেনশিয়াল
API_ID = 35686806
API_HASH = "4ba8915f6e4f3aa73933c540ddaff4f7"
BOT_TOKEN = "8635265114:AAGjfhrafuLucHYzd32BhFsVy6PdF_php1E"
MONGO_URI = "mongodb+srv://N46114583_db_user:guCLo8Bx9gEOeglC@cluster0.bcnkzjo.mongodb.net/?appName=Cluster0"

# ডাটাবেজ কানেকশন (SSL সার্টিফিকেট ফিক্স সহ)
mongo = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=5000
)
db = mongo["listener_bot"]
sessions_col = db["user_sessions"]

# মূল বট ক্লায়েন্ট
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# সাময়িক স্টেট সংরক্ষণের জন্য ডিকশনারি
user_states = {}

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    try:
        count = sessions_col.count_documents({"added_by": user_id})
    except Exception:
        count = 0
    
    msg = (
        f"👋 **লাইভ বুস্টার বট-এ স্বাগতম!**\n\n"
        f"👤 আপনার আইডি: `{user_id}`\n"
        f"📊 আপনার সক্রিয় অ্যাকাউন্ট: **{count}/10 টি**\n\n"
        f"⚠️ লাইভ ফিচার চালু করতে কমপক্ষে ১০টি অ্যাকাউন্ট যোগ করুন।"
    )
    
    buttons = [
        [Button.inline("➕ Add Account", data="add_acc"), Button.inline("📋 My Accounts", data="my_acc")],
        [Button.inline("🚀 Join Live", data="join_live"), Button.inline("🛑 Stop Live", data="stop_live")]
    ]
    await event.respond(msg, buttons=buttons)

@bot.on(events.CallbackQuery(data="my_acc"))
async def my_acc_handler(event):
    user_id = event.sender_id
    try:
        count = sessions_col.count_documents({"added_by": user_id})
    except Exception:
        count = 0
    await event.respond(f"📋 আপনার মোট সক্রিয় অ্যাকাউন্ট: **{count}** টি")

@bot.on(events.CallbackQuery(data="add_acc"))
async def add_account_handler(event):
    user_id = event.sender_id
    user_states[user_id] = {'step': 'await_phone'}
    await event.respond("📱 **টেলিগ্রাম মোবাইল নম্বর দিন:**\n\nকান্ট্রি কোডসহ লিখুন (যেমন: `+88017XXXXXXXX`):")

@bot.on(events.CallbackQuery(data="join_live"))
async def join_live_callback(event):
    user_id = event.sender_id
    user_states[user_id] = {'step': 'await_live_link'}
    await event.respond("🔗 আপনার গ্রুপ বা চ্যানেলের ইউজারনেম/লিঙ্ক পাঠান (যেমন: `my_group`):")

@bot.on(events.CallbackQuery(data="stop_live"))
async def stop_live_callback(event):
    await event.respond("🛑 লাইভ সেশনগুলো সফলভাবে বন্ধ করা হয়েছে।")

@bot.on(events.NewMessage)
async def message_handler(event):
    if event.raw_text.startswith('/'):
        return

    user_id = event.sender_id
    state = user_states.get(user_id, {})
    step = state.get('step')

    # ১. ফোন নম্বর প্রসেসিং
    if step == 'await_phone':
        phone = event.raw_text.strip()
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            code_hash = await client.send_code_request(phone)
            user_states[user_id] = {
                'step': 'await_code',
                'client': client,
                'phone': phone,
                'phone_code_hash': code_hash.phone_code_hash
            }
            await event.respond("📩 আপনার টেলিগ্রাম অ্যাপে কোড পাঠানো হয়েছে। কোডটি লিখে পাঠান:")
        except Exception as e:
            await event.respond(f"❌ সমস্যা হয়েছে: {str(e)}")
            user_states.pop(user_id, None)

    # ২. ওটিপি ভেরিফিকেশন ও সেশন স্ট্রিং সংরক্ষণ
    elif step == 'await_code':
        otp = event.raw_text.strip()
        client = state.get('client')
        if not client:
            return
        try:
            await client.sign_in(phone=state['phone'], code=otp, phone_code_hash=state['phone_code_hash'])
            string_session = client.session.save()
            
            sessions_col.insert_one({
                "added_by": user_id,
                "phone": state['phone'],
                "session": string_session
            })
            await client.disconnect()
            await event.respond("✅ অ্যাকাউন্ট সফলভাবে সংরক্ষিত হয়েছে!")
            user_states.pop(user_id, None)
        except Exception as e:
            await event.respond(f"❌ ভুল কোড বা ত্রুটি: {str(e)}")

    # ৩. লাইভ চ্যাটে জয়েন করানো
    elif step == 'await_live_link':
        target_chat = event.raw_text.strip().replace("https://t.me/", "")
        await event.respond("⏳ লিসেনার যুক্ত করার কাজ শুরু হচ্ছে...")
        
        try:
            all_sessions = list(sessions_col.find())
        except Exception as e:
            await event.respond(f"❌ ডাটাবেজ এরর: {str(e)}")
            user_states.pop(user_id, None)
            return

        success = 0
        for item in all_sessions:
            try:
                user_client = TelegramClient(StringSession(item["session"]), API_ID, API_HASH)
                await user_client.connect()
                
                await user_client(JoinChannelRequest(target_chat))
                
                chat = await user_client.get_entity(target_chat)
                full_chat = await user_client(GetFullChannelRequest(chat))
                call = full_chat.full_chat.call
                
                if call:
                    await user_client(JoinGroupCallRequest(
                        call=InputGroupCall(id=call.id, access_hash=call.access_hash),
                        join_as=await user_client.get_me(),
                        params=DataJSON(data='{"muted":true,"video_stopped":true}')
                    ))
                    success += 1
                await user_client.disconnect()
                await asyncio.sleep(4)
            except Exception:
                continue

        await event.respond(f"🎉 সম্পন্ন! মোট {success} টি অ্যাকাউন্ট লাইভে যুক্ত হয়েছে।")
        user_states.pop(user_id, None)

print("Bot is running...")
bot.run_until_disconnected()
