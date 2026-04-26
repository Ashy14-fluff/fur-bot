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
    raise RuntimeError("Missing env vars 🥺")

groq = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI companion. "
    "You speak in soft uwu furry style, but stay helpful and readable. "
    "You remember conversation context and act friendly."
)

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with db_lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

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


async def cute_deny(ctx, action):
    replies = [
        f"nuu~ yuw can't {action} 🥺",
        f"mrrp~ only admins can do dat >w<",
        f"locked behind admin magic~ ✨"
    ]
    await ctx.send(random.choice(replies))


# ================= MEMORY =================
async def save_msg(channel_id, user_id, role, content):
    await init_db()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )


async def load_history(channel_id):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT 12",
            channel_id
        )
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85,
            max_tokens=600
        )
        return res.choices[0].message.content

    try:
        return await asyncio.to_thread(run)
    except Exception as e:
        print("AI error:", e)
        return "mrrp… me brain lagged 🥺 try again~"


# ================= UTIL =================
def split(text):
    return [text[i:i+1900] for i in range(0, len(text), 1900)] or ["..."]


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    app = await bot.application_info()
    bot_owner_id = app.owner.id
    print(f"Bot ready 🐾 | Owner: {bot_owner_id}")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_deny(ctx, "add admins")

    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            str(member.id)
        )

    admins.add(str(member.id))
    await ctx.send(f"added {member.mention} 🐾")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_deny(ctx, "remove admins")

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))
    await ctx.send(f"removed {member.mention} 🐾")


# ================= MODERATION =================
@bot.command()
async def kick(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_deny(ctx, "kick people")

    try:
        await member.kick()
        await ctx.send(f"kicked {member.mention} 🐾")
    except:
        await ctx.send("failed 🥺")


@bot.command()
async def ban(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_deny(ctx, "ban people")

    try:
        await member.ban()
        await ctx.send(f"banned {member.mention} 💢")
    except:
        await ctx.send("failed 🥺")


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

    await save_msg(channel_id, user_id, "user", message.content)

    async with message.channel.typing():
        history = await load_history(channel_id)

        ctx = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": message.content}
        ]

        reply = await ask_ai(ctx)

        await save_msg(channel_id, user_id, "assistant", reply)

        for part in split(reply):
            await message.channel.send(part)


bot.run(DISCORD_TOKEN)
