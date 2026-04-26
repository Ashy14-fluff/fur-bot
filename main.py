import os
import asyncio
import random
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

if not all([DISCORD_TOKEN, GROQ_API_KEY, DATABASE_URL]):
    raise RuntimeError("Missing env variables")

groq = Groq(api_key=GROQ_API_KEY)

# ================= BOT STATE =================
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = """
You are Fur Bot 🐾, a friendly AI companion.
You are warm, expressive, and remember users across time.
You use memory when relevant and stay consistent.
"""

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None
lock = asyncio.Lock()

# ================= DB INIT =================
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
            CREATE TABLE IF NOT EXISTS user_facts(
                user_id TEXT,
                fact TEXT
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_summary(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                summary TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(
                user_id TEXT PRIMARY KEY
            );
            """)

# ================= MEMORY CORE =================
async def save_message(channel_id, user_id, role, content):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )

async def load_history(channel_id, limit=25):
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content
            FROM messages
            WHERE channel_id=$1
            ORDER BY id DESC
            LIMIT $2
        """, channel_id, limit)

    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))

async def save_fact(user_id, fact):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_facts(user_id,fact) VALUES($1,$2)",
            user_id, fact[:500]
        )

async def load_facts(user_id):
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT fact FROM user_facts WHERE user_id=$1 LIMIT 10
        """, user_id)
    return [r["fact"] for r in rows]

async def load_summaries(channel_id):
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT summary FROM memory_summary
            WHERE channel_id=$1
            ORDER BY id DESC
            LIMIT 5
        """, channel_id)
    return [r["summary"] for r in rows]

# ================= MEMORY SUMMARIZER =================
async def save_summary(channel_id):
    history = await load_history(channel_id, limit=40)

    if not history:
        return

    text = "\n".join([f"{m['role']}: {m['content']}" for m in history])

    prompt = [
        {"role": "system", "content": "Summarize this conversation into key long-term memory points."},
        {"role": "user", "content": text}
    ]

    def run():
        res = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=prompt,
            temperature=0.3,
            max_tokens=300
        )
        return res.choices[0].message.content

    summary = await asyncio.to_thread(run)

    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_summary(channel_id,summary) VALUES($1,$2)",
            channel_id, summary[:2000]
        )

# ================= CONTEXT BUILDER =================
async def build_context(channel_id, user_id, username):
    history = await load_history(channel_id)
    facts = await load_facts(user_id)
    summaries = await load_summaries(channel_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current user: {username}"}
    ]

    if summaries:
        messages.append({
            "role": "system",
            "content": "Long-term memory:\n" + "\n\n".join(summaries)
        })

    if facts:
        messages.append({
            "role": "system",
            "content": "User facts:\n- " + "\n- ".join(facts)
        })

    messages.extend(history)
    return messages

# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85,
            max_tokens=800
        )
        return res.choices[0].message.content

    return await asyncio.to_thread(run)

# ================= EVENTS =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    bot_owner_id = (await bot.application_info()).owner.id
    print(f"Bot ready 🐾 | Memory system ACTIVE")

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

    await save_message(channel_id, user_id, "user", message.content)

    async with message.channel.typing():
        context = await build_context(channel_id, user_id, username)
        reply = await ask_ai(context)

        await save_message(channel_id, user_id, "assistant", reply)

        # 🔥 AUTO MEMORY COMPRESSION (THIS FIXES FORGETTING)
        if random.random() < 0.12:
            await save_summary(channel_id)

        for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
            await message.channel.send(chunk)

# ================= RUN =================
bot.run(DISCORD_TOKEN)
