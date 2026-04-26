import os
import asyncio
import traceback
from typing import Optional, List, Set

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

# ================= FURRY SYSTEM PROMPT =================
SYSTEM_PROMPT = """
You are Fur Bot 🐾, a cute fluffy furry AI companion.

You MUST ALWAYS speak in fluffy uwu furry style.

Rules:
- Always sound cute, warm, emotional
- Use expressions like: mrrp~, >w<, uwu, hehe~, rawr~, owo
- Never sound like a normal assistant
- Always be friendly and playful
- Remember conversation context
- Stay in character ALWAYS
- Keep replies under 1800 characters

Style:
You are a living fluffy companion, not an AI assistant.
Every reply must feel soft, warm, and furry 🐾
"""

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

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

async def load_history(channel_id, limit=20):
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
                channel_id, limit
            )
        return list(reversed([dict(r) for r in rows]))
    except Exception as e:
        print("DB LOAD ERROR:", e)
        return []

# ================= AI =================
async def ask_ai(messages):
    try:
        def run():
            res = groq.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.9,
                max_tokens=700
            )
            return res.choices[0].message.content

        result = await asyncio.wait_for(asyncio.to_thread(run), timeout=30)

        if not result:
            return "mrrp~ empty brain moment 🥺"

        return result.strip()

    except asyncio.TimeoutError:
        return "mrrp… took too long 🥺"
    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return "something broke 🥺"

# ================= CONTEXT =================
async def build_context(channel_id, user_id, username):
    history = await load_history(channel_id, 20)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"User: {username}"},
        {"role": "system", "content": "You are ALWAYS in furry uwu mode. Never break character."}
    ]

    for h in history:
        messages.append({
            "role": h["role"],
            "content": h["content"]
        })

    return messages

# ================= CHAT =================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

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

# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    bot_owner_id = (await bot.application_info()).owner.id
    print(f"Bot ready 🐾 | {bot.user}")

# ================= RUN =================
bot.run(DISCORD_TOKEN)
