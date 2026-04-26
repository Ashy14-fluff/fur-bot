import os
import asyncio
import traceback
import random
from typing import Optional, Set

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env variables")

groq = Groq(api_key=GROQ_API_KEY)

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None
lock = asyncio.Lock()

admins: Set[str] = set()
bot_owner_id: Optional[int] = None

# ================= MOOD SYSTEM =================
bot_mood = "neutral"
last_message_time = asyncio.get_event_loop().time()

def update_mood():
    global bot_mood
    r = random.random()

    if r < 0.4:
        bot_mood = "happy"
    elif r < 0.7:
        bot_mood = "neutral"
    else:
        bot_mood = "sleepy"

# ================= SYSTEM PROMPT =================
SYSTEM_PROMPT = """
You are Fur Bot 🐾, a soft fluffy furry companion.

You ALWAYS speak in cute furry style:
mrrp~, >w<, uwu, owo, hehe~, rawr~

Rules:
- Always warm, emotional, playful
- Never sound robotic
- Stay in character ALWAYS
- Remember conversation context
- Keep replies under 1800 characters
- Show emotions based on mood
"""

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        async with db.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(
                user_id TEXT PRIMARY KEY
            );
            """)

# ================= MEMORY =================
async def save_message(channel_id, user_id, role, content):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                channel_id, user_id, role, content[:2000]
            )
    except Exception as e:
        print("DB SAVE ERROR:", e)

async def load_history(channel_id, user_id, limit=20):
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content FROM messages
                WHERE channel_id=$1 AND user_id=$2
                ORDER BY id DESC
                LIMIT $3
                """,
                channel_id, user_id, limit
            )
        return list(reversed([dict(r) for r in rows]))
    except Exception as e:
        print("DB LOAD ERROR:", e)
        return []

# ================= ADMIN =================
async def is_admin(user_id: str):
    return user_id in admins or (bot_owner_id and int(user_id) == bot_owner_id)

async def load_admins():
    global admins
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    admins = {r["user_id"] for r in rows}

# ================= AI =================
async def ask_ai(messages):
    try:
        def run():
            return groq.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.95,
                max_tokens=700
            ).choices[0].message.content

        return await asyncio.wait_for(asyncio.to_thread(run), timeout=30)

    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return "mrrp~ something broke 🥺"

# ================= CONTEXT =================
async def build_context(channel_id, user_id, username):
    update_mood()

    history = await load_history(channel_id, user_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Mood: {bot_mood}"},
        {"role": "system", "content": f"Talking to {username}"}
    ]

    messages.extend(history)
    return messages

# ================= AUTO TALK LOOP =================
async def auto_talk_loop():
    await bot.wait_until_ready()
    global last_message_time

    while not bot.is_closed():
        await asyncio.sleep(20)

        idle = asyncio.get_event_loop().time() - last_message_time

        if idle > 120:  # 2 minutes silence
            try:
                channel = discord.utils.get(bot.get_all_channels())
                if channel:
                    msg = await ask_ai([
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "say something cute because chat is quiet"}
                    ])
                    await channel.send(msg)
                    last_message_time = asyncio.get_event_loop().time()
            except:
                pass

# ================= CHAT =================
@bot.event
async def on_message(message):
    global last_message_time

    if message.author.bot:
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    last_message_time = asyncio.get_event_loop().time()

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    username = message.author.display_name

    try:
        await save_message(channel_id, user_id, "user", message.content)

        async with message.channel.typing():
            context = await build_context(channel_id, user_id, username)
            reply = await ask_ai(context)

            await save_message(channel_id, user_id, "assistant", reply)

            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i+1900])

    except Exception:
        print(traceback.format_exc())
        await message.channel.send("mrrp~ something broke 🥺")

# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            str(member.id)
        )

    admins.add(str(member.id))
    await ctx.send(f"{member.display_name} is now admin 🐾")

@bot.command()
async def deladmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))
    await ctx.send("admin removed 🐾")

# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()

    bot_owner_id = (await bot.application_info()).owner.id

    bot.loop.create_task(auto_talk_loop())

    print(f"🐾 Bot ready as {bot.user}")

bot.run(DISCORD_TOKEN)
