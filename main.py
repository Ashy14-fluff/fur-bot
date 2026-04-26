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
    raise RuntimeError("Missing env 🥺")

groq_client = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
bot_owner_id: Optional[int] = None
admin_users: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾. "
    "You are cute, fluffy, helpful, and talk in uwu style. "
    "Never be rude. Keep replies short and natural."
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


# ================= DB INIT =================
async def init_db():
    global db_pool
    if db_pool:
        return

    async with db_lock:
        if db_pool:
            return

        db_pool = await asyncpg.create_pool(DATABASE_URL)

        async with db_pool.acquire() as conn:
            await conn.execute("""CREATE TABLE IF NOT EXISTS admins(user_id TEXT PRIMARY KEY);""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );""")


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admin_users or (bot_owner_id and int(uid) == bot_owner_id)


async def cute_refuse(ctx, action: str):
    msg = random.choice([
        f"nuu~ yuw can't {action} 🥺 only admins can do dat~",
        f"mrrp~ dat power is locked behind admin magic >w<",
        f"sowwy fluffbutt~ no permission to {action} 🐾"
    ])
    await ctx.send(msg)


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "add admins")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            str(member.id)
        )

    admin_users.add(str(member.id))
    await ctx.send(f"added {member.mention} 🐾")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "remove admins")

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admin_users.discard(str(member.id))
    await ctx.send(f"removed {member.mention} 🐾")


# ================= MODERATION =================
@bot.command()
async def kick(ctx, member: discord.Member, *, reason=None):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "kick people")

    try:
        await member.kick(reason=reason)
        await ctx.send(f"kicked {member.mention} 🐾")
    except discord.Forbidden:
        await ctx.send("me no have permission 🥺")


@bot.command()
async def ban(ctx, member: discord.Member, *, reason=None):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "ban people")

    try:
        await member.ban(reason=reason)
        await ctx.send(f"banned {member.mention} 💢")
    except discord.Forbidden:
        await ctx.send("me no have permission 🥺")


@bot.command()
async def mute(ctx, member: discord.Member, minutes: int):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "mute people")

    until = discord.utils.utcnow() + timedelta(minutes=minutes)

    try:
        await member.timeout(until)
        await ctx.send(f"muted {member.mention} for {minutes}m 🐾")
    except Exception:
        await ctx.send("failed to mute 🥺")


@bot.command()
async def unmute(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await cute_refuse(ctx, "unmute people")

    await member.timeout(None)
    await ctx.send(f"unmuted {member.mention} 💖")


# ================= MEMORY =================
async def save_message(channel_id, user_id, role, content):
    await init_db()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:2000]
        )


async def load_history(channel_id, limit=12):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            channel_id, limit
        )
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.9,
            max_tokens=800
        )
        return res.choices[0].message.content

    return await asyncio.to_thread(run)


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    bot_owner_id = (await bot.application_info()).owner.id
    print("bot ready 🐾")


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

    await save_message(channel_id, user_id, "user", message.content)

    async with message.channel.typing():
        history = await load_history(channel_id)

        ctx = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": message.content}
        ]

        reply = await ask_ai(ctx)

        await save_message(channel_id, user_id, "assistant", reply)

        await message.channel.send(reply)


bot.run(DISCORD_TOKEN)
