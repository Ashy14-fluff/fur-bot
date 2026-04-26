import os
import asyncio
import random
from datetime import datetime
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
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env 🥺")

groq = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI companion. "
    "You speak naturally, warm, and consistent. "
    "Remember conversation context. Stay in character."
)

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

        db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

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
                username TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_msg_channel
            ON messages(channel_id, id DESC);
            """)


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


async def deny(ctx, action):
    await ctx.send(random.choice([
        f"nuu~ no permission to {action} 🥺",
        f"locked behind admin magic >w<",
        f"sowwy~ only admins can {action}"
    ]))


# ================= ADMIN LOAD =================
async def load_admins():
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    admins.clear()
    admins.update(r["user_id"] for r in rows)


# ================= MEMORY =================
async def save_msg(channel_id, user_id, username, role, content):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,username,role,content) VALUES($1,$2,$3,$4,$5)",
            channel_id, user_id, username, role, content[:2000]
        )


async def load_history(channel_id, limit=25):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content,username FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            channel_id, limit
        )

    rows = list(reversed(rows))

    out = []
    for r in rows:
        if r["role"] == "user":
            out.append({"role": "user", "content": f"{r['username']}: {r['content']}"})
        else:
            out.append({"role": "assistant", "content": r["content"]})

    return out


# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85,
            max_tokens=700
        )
        return res.choices[0].message.content

    try:
        return await asyncio.wait_for(asyncio.to_thread(run), timeout=30)
    except:
        return None


# ================= EVENTS =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()

    bot_owner_id = (await bot.application_info()).owner.id
    print(f"Bot ready 🐾 | admins: {len(admins)}")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "add admins")

    async with db.acquire() as conn:
        await conn.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", str(member.id))

    admins.add(str(member.id))
    await ctx.send(f"added {member.mention} 🐾")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "remove admins")

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))
    await ctx.send(f"removed {member.mention} 🐾")


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
        await init_db()

        # ✅ FIXED ORDER: save FIRST
        await save_msg(channel_id, user_id, username, "user", message.content)

        async with message.channel.typing():
            history = await load_history(channel_id)

            context = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": message.content}
            ]

            reply = await ask_ai(context)

            if not reply:
                reply = "mrrp… brain lagged 🥺"

            await save_msg(channel_id, "bot", "Fur Bot", "assistant", reply)

            await message.channel.send(reply)

    except Exception as e:
        print("error:", e)
        await message.channel.send("mrrp… error happened 🥺")


bot.run(DISCORD_TOKEN)
