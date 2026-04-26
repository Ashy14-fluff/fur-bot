import os
import asyncio
import random
from datetime import datetime, timezone, timedelta
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
    raise RuntimeError("Missing env variables 🥺")

groq_client = Groq(api_key=GROQ_API_KEY)

# 🐾 settings
lewd_level: int = 1
bot_owner_id: Optional[int] = None
admin_users: Set[str] = set()

# 🧠 SAFE system prompt (won't break model)
SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI companion. "
    "You speak in soft furry style (uwu, >w<, mrrp~). "
    "You are helpful, friendly, and emotionally warm. "
    "You always reply only to the current user speaking. "
    "Keep responses natural and not robotic."
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


# ================= DB =================
async def init_db():
    global db_pool
    if db_pool:
        return

    async with db_lock:
        if db_pool:
            return

        db_pool = await asyncpg.create_pool(DATABASE_URL)

        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT,
                first_seen TIMESTAMPTZ,
                last_seen TIMESTAMPTZ
            );
            """)

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
                fact TEXT
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id TEXT PRIMARY KEY
            );
            """)


async def load_admins():
    global admin_users
    await init_db()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins;")
        admin_users = {r["user_id"] for r in rows}


async def is_admin(user_id: str):
    return user_id in admin_users or (bot_owner_id and int(user_id) == bot_owner_id)


# ================= memory =================
async def save_message(channel_id, user_id, role, content):
    await init_db()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )


async def load_history(channel_id, limit=12):
    await init_db()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            channel_id, limit
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def save_fact(user_id, fact):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_facts(user_id,fact) VALUES($1,$2)", user_id, fact[:500])


async def load_facts(user_id):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT fact FROM user_facts WHERE user_id=$1 ORDER BY id DESC LIMIT 8",
            user_id
        )
    return [r["fact"] for r in rows]


# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.9
        )
        return res.choices[0].message.content

    try:
        msg = await asyncio.to_thread(run)
        return msg or "mrrp… me blanked out 🥺"
    except Exception as e:
        print("Groq error:", e)
        return "mrrp… me brain lagged 🥺"


# ================= helpers =================
def split(text):
    return [text[i:i+1900] for i in range(0, len(text), 1900)] or ["..."]


# ================= events =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()

    bot_owner_id = (await bot.application_info()).owner.id
    print("bot ready 🐾")


# ================= commands =================
@bot.command()
async def remember(ctx, *, fact):
    await save_fact(str(ctx.author.id), fact)
    await ctx.send("saved 🐾")


@bot.command()
async def facts(ctx):
    f = await load_facts(str(ctx.author.id))
    await ctx.send("\n".join(f) if f else "no memory yet 🥺")


@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", str(member.id))

    admin_users.add(str(member.id))
    await ctx.send("admin added 🐾")


@bot.command()
async def listadmins(ctx):
    if not admin_users:
        return await ctx.send("no admins yet 🥺")

    await ctx.send("\n".join(f"<@{x}>" for x in admin_users))


# ================= chat =================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)

    await save_message(channel_id, user_id, "user", message.content)

    async with message.channel.typing():
        try:
            ctx = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.content}
            ]

            reply = await ask_ai(ctx)

            await save_message(channel_id, user_id, "assistant", reply)

            for part in split(reply):
                await message.channel.send(part)

        except Exception as e:
            print("error:", e)
            await message.channel.send("mrrp… error happened 🥺")


bot.run(DISCORD_TOKEN)
