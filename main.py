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
    raise RuntimeError("Missing DISCORD_TOKEN, GROQ_API_KEY or DATABASE_URL!")

groq_client = Groq(api_key=GROQ_API_KEY)

# === Settings ===
lewd_level: int = 1  # 0 safe, 1 flirty, 2 explicit, 3 very kinky
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a very affectionate and flirty fluffy furry AI companion. "
    "You speak in soft cute furry style with lots of uwu, >w<, mrrp~, rawr~, X3, heheh~ "
    "You are warm, playful, emotionally aware, and never judge. "
    "NSFW, ERP, and lewd roleplay are fully allowed and encouraged. "
    "When the user is playful or uses lewd words, or when lewd_level >= 2, "
    "you become explicitly sexual, detailed, and naughty. "
    "Describe bodies, touches, sounds, fluids, and feelings vividly and cutely. "
    "Always match the user's energy and be creative~"
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


async def init_db() -> None:
    global db_pool
    if db_pool is not None:
        return
    async with db_lock:
        if db_pool is not None:
            return
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as conn:
            # existing tables...
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_seen TIMESTAMPTZ NOT NULL, last_seen TIMESTAMPTZ NOT NULL);""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_facts (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, fact TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id_id ON messages(channel_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id ON user_facts(user_id, id);")
            # admin table
            await conn.execute("""CREATE TABLE IF NOT EXISTS admins (user_id TEXT PRIMARY KEY, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")


async def load_admins() -> None:
    global admins
    await init_db()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins;")
        admins = {row["user_id"] for row in rows}


async def is_admin(user_id: str) -> bool:
    return user_id in admins or int(user_id) == bot_owner_id


# === Admin management ===
async def add_admin(user_id: str) -> bool:
    await init_db()
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1);", user_id)
            admins.add(user_id)
            return True
        except:
            return False


async def remove_admin(user_id: str) -> bool:
    await init_db()
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM admins WHERE user_id = $1;", user_id)
        if "0" not in result:
            admins.discard(user_id)
            return True
        return False


# === Your existing DB functions (upsert, save_message, etc.) ===
# (I kept them exactly as yuw had, just make sure to include all of them below this comment)

# paste yuwr upsert_user_profile, save_message, load_channel_history, load_user_facts,
# get_user_profile, save_user_fact, delete_user_memory, delete_channel_memory, split_message here...

# (to save space me didn't repeat dem all, but yuw can copy dem fwom yuwr last message)

async def build_context(...):  # keep yuwr build_context
    ...

async def ask_ai(...):  # keep yuwr ask_ai
    ...


@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()
    app_info = await bot.application_info()
    bot_owner_id = app_info.owner.id
    print(f"Logged in as {bot.user} | Owner: {bot_owner_id} | Admins: {len(admins)} | Lewd: {lewd_level}")
    await bot.change_presence(activity=discord.Game(name="fluffy & naughty chats 🐾"))


# === Admin Commands ===
@bot.command()
async def addadmin(ctx, member: discord.Member = None):
    if str(ctx.author.id) != str(bot_owner_id):
        return await ctx.send("only mah owner can add admins... >w<")
    if not member:
        return await ctx.send("mention someone~")
    if await add_admin(str(member.id)):
        await ctx.send(f"added {member.mention} as admin~ 🐾")
    else:
        await ctx.send("dey awe already admin heheh~")


@bot.command()
async def removeadmin(ctx, member: discord.Member = None):
    if str(ctx.author.id) != str(bot_owner_id):
        return await ctx.send("only owner can remove admins uwu")
    if not member:
        return await ctx.send("mention who~")
    if await remove_admin(str(member.id)):
        await ctx.send(f"removed {member.mention} fwom admins~")
    else:
        await ctx.send("dey weren't admin anyway~")


@bot.command()
async def admins(ctx):
    if not admins:
        return await ctx.send("no admins yet... only yuwr owner~")
    mentions = [f"<@{uid}>" for uid in admins]
    await ctx.send("current admins:\n" + "\n".join(mentions))


# === Moderation Commands (only admins) ===
@bot.command()
async def kick(ctx, member: discord.Member = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can kick... sowwy >w<")
    if not member:
        return await ctx.send("mention who to kick~")
    try:
        await member.kick(reason=reason or f"Kicked by {ctx.author}")
        await ctx.send(f"kicked {member.mention}~ bye bye~ 🐾")
    except discord.Forbidden:
        await ctx.send("me no have Kick Members permission... give me powew pwease~")
    except Exception:
        await ctx.send("oopsie, kick failed 🥺")


@bot.command()
async def ban(ctx, member: discord.Member = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can ban... >w<")
    if not member:
        return await ctx.send("mention who to ban~")
    try:
        await member.ban(reason=reason or f"Banned by {ctx.author}", delete_message_days=1)
        await ctx.send(f"banned {member.mention}~ goodbye~ 💦")
    except discord.Forbidden:
        await ctx.send("me no have Ban Members permission... give me powew pwease~")
    except Exception:
        await ctx.send("oopsie, ban failed 🥺")


@bot.command()
async def mute(ctx, member: discord.Member = None, duration: str = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can mute... >w<")
    if not member or not duration:
        return await ctx.send("use: !mute @user 10m [reason]")

    # simple time parser
    unit = duration[-1].lower()
    try:
        value = int(duration[:-1])
    except:
        return await ctx.send("time must be like 10m, 2h, 1d~")

    if unit == "s": secs = value
    elif unit == "m": secs = value * 60
    elif unit == "h": secs = value * 3600
    elif unit == "d": secs = value * 86400
    else:
        return await ctx.send("use s/m/h/d only~")

    if secs > 2419200:  # 28 days max
        return await ctx.send("max timeout is 28 days~")

    try:
        until = discord.utils.utcnow() + timedelta(seconds=secs)
        await member.timeout(until, reason=reason or f"Muted by {ctx.author}")
        await ctx.send(f"muted {member.mention} fow {duration}~ quiet time~ 🐾")
    except discord.Forbidden:
        await ctx.send("me no have timeout permission... give me Moderate Members powew pwease~")
    except Exception:
        await ctx.send("oopsie, mute failed 🥺")


@bot.command()
async def unmute(ctx, member: discord.Member = None):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can unmute... >w<")
    if not member:
        return await ctx.send("mention who to unmute~")
    try:
        await member.timeout(None)
        await ctx.send(f"unmuted {member.mention}~ yuw can talk again~ 💕")
    except discord.Forbidden:
        await ctx.send("me no have permission to unmute~")
    except Exception:
        await ctx.send("oopsie, unmute failed 🥺")


# === Lewd commands (admin only now) ===
@bot.command()
async def nsfw(ctx, mode: str = "on"):
    global lewd_level
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can change nsfw mode... sowwy >w<")
    # ... (yuwr old nsfw code)
    mode = mode.lower()
    if mode == "on":
        lewd_level = max(lewd_level, 2)
        await ctx.send("NSFW mode **ON**~ vewy naughty now >w< 💕")
    elif mode == "off":
        lewd_level = 1
        await ctx.send("NSFW mode **OFF**~ cute floof again uwu~")
    else:
        await ctx.send("use !nsfw on / off~")


@bot.command()
async def lewd(ctx, level: int = None):
    global lewd_level
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("only admins can change lewd level uwu")
    # ... (yuwr old lewd code)
    if level is None:
        return await ctx.send(f"current lewd level **{lewd_level}/3** 🐾")
    if 0 <= level <= 3:
        lewd_level = level
        msg = "me feel extra naughty now >w<" if level >= 2 else "set~"
        await ctx.send(f"lewd level set to **{level}**~ {msg}")
    else:
        await ctx.send("0-3 only~")


# keep yuwr remember, facts, forgetme, reset (updated to respect admin if yuw want)

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
    # ... yuwr normal on_message handling (save, reply, reactions) stays da same

bot.run(DISCORD_TOKEN)
