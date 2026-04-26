import os
import asyncio
import random
from datetime import datetime, timezone
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
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env variables")

groq_client = Groq(api_key=GROQ_API_KEY)

# 🧠 CLEAN SYSTEM PROMPT (no conflict, stable behavior)
SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy Discord AI companion. "
    "You are friendly, playful, expressive, and helpful. "
    "You use soft furry tone like uwu, >w<, mrrp~ occasionally. "
    "You ALWAYS respond clearly to the current user. "
    "You never stay silent. If unsure, you still reply simply. "
    "You remember conversation context."
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


# ---------------- DB ----------------
async def init_db():
    global db_pool
    if db_pool:
        return

    async with db_lock:
        if db_pool:
            return

        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT,
                fact TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)


# ---------------- MEMORY ----------------
async def save_message(channel_id, user_id, role, content):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )


async def load_history(channel_id, limit=8):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content
            FROM messages
            WHERE channel_id=$1
            ORDER BY id DESC
            LIMIT $2
        """, channel_id, limit)

    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------------- GROQ SAFE CALL ----------------
async def ask_ai(messages: List[dict]) -> str:
    try:
        def call():
            return groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.8,
                max_tokens=500
            ).choices[0].message.content

        reply = await asyncio.wait_for(
            asyncio.to_thread(call),
            timeout=25
        )

        # 🛡️ HARD GUARANTEE (NO SILENT FAIL)
        if not reply or not reply.strip():
            return "mrrp… me brain lagged a bit 🥺 try again~"

        return reply

    except asyncio.TimeoutError:
        return "mrrp… Groq took too long 🥺"

    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return "mrrp… something broke on my side 🥺"


# ---------------- BOT EVENTS ----------------
@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="fluffy chats 🐾"))


# ---------------- CHAT CORE ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    if content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)

    await save_message(channel_id, user_id, "user", content)

    async with message.channel.typing():
        history = await load_history(channel_id)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": content}
        ]

        reply = await ask_ai(messages)

        await save_message(channel_id, user_id, "assistant", reply)

        # 🧵 SAFE SEND (no silent failure)
        for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
            try:
                await message.channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as e:
                print("SEND ERROR:", repr(e))
                await message.channel.send("mrrp… message too big 🥺")
                break

    await bot.process_commands(message)


# ---------------- SIMPLE COMMAND ----------------
@bot.command()
async def ping(ctx):
    await ctx.send("pong 🐾")


bot.run(DISCORD_TOKEN)
