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

groq = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = """
You are Fur Bot 🐾, a cute fluffy AI assistant.
You are helpful, friendly, emotional, and expressive in soft furry tone (uwu style).
You must:
- Remember conversation context
- Stay consistent per user
- Be natural and not robotic
- Be safe and non-explicit
"""

# ================= DISCORD =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DB =================
db: Optional[asyncpg.Pool] = None
lock = asyncio.Lock()


async def init_db():
    global db
    if db:
        return

    async with lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL)

        async with db.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(
                user_id TEXT PRIMARY KEY
            );
            """)

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


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


# ================= MEMORY =================
async def save_message(channel_id, user_id, role, content):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )


async def load_history(channel_id, limit=25):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            channel_id, limit
        )
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


async def save_fact(user_id, fact):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_facts(user_id,fact) VALUES($1,$2)",
            user_id, fact[:500]
        )


async def load_facts(user_id):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT fact FROM user_facts WHERE user_id=$1 LIMIT 10",
            user_id
        )
    return [r["fact"] for r in rows]


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


# ================= CONTEXT BUILDER =================
async def build_context(channel_id, user_id, username):
    history = await load_history(channel_id)
    facts = await load_facts(user_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"User: {username}"},
    ]

    if facts:
        messages.append({
            "role": "system",
            "content": "User memory:\n- " + "\n- ".join(facts)
        })

    messages.extend(history)
    return messages


# ================= EVENTS =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    bot_owner_id = (await bot.application_info()).owner.id
    print(f"Bot ready 🐾 | Owner: {bot_owner_id}")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", str(member.id))

    admins.add(str(member.id))
    await ctx.send("admin added 🐾")


@bot.command()
async def kick(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    await member.kick()
    await ctx.send("kicked 🐾")


@bot.command()
async def ban(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    await member.ban()
    await ctx.send("banned 💢")


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

        for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
            await message.channel.send(chunk)


bot.run(DISCORD_TOKEN)
