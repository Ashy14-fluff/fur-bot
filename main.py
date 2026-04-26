import os
import asyncio
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
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

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

SYSTEM_PROMPT = """
You are Fur Bot 🐾.
You are cute, helpful, and conversational.
You remember context and respond naturally.
"""


# ================= DB INIT =================
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
                fact TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


async def load_admins():
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    admins.clear()
    admins.update(r["user_id"] for r in rows)


# ================= MEMORY =================
async def save_message(channel_id, user_id, role, content):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                channel_id, user_id, role, content[:2000]
            )
    except Exception as e:
        print("DB save error:", e)


async def load_history(channel_id, limit=20):
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
                channel_id, limit
            )
        return list(reversed([dict(r) for r in rows]))
    except Exception as e:
        print("DB load error:", e)
        return []


async def save_fact(user_id, fact):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_facts(user_id,fact) VALUES($1,$2)",
                user_id, fact[:500]
            )
    except Exception as e:
        print("fact save error:", e)


async def load_facts(user_id):
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT fact FROM user_facts WHERE user_id=$1 LIMIT 10",
                user_id
            )
        return [r["fact"] for r in rows]
    except:
        return []


# ================= AI CALL (FIXED SAFE) =================
async def ask_ai(messages):
    try:
        def run():
            res = groq.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.85,
                max_tokens=700
            )
            return res.choices[0].message.content

        result = await asyncio.to_thread(run)

        if not result:
            return "mrrp… me didn’t get response 🥺"

        return result

    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return "mrrp… AI broke 🥺"


# ================= CONTEXT =================
async def build_context(channel_id, user_id, username):
    history = await load_history(channel_id)
    facts = await load_facts(user_id)

    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"User: {username}"}
    ]

    if facts:
        msgs.append({
            "role": "system",
            "content": "Memory:\n- " + "\n- ".join(facts)
        })

    msgs.extend(history)
    return msgs


# ================= CHAT =================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    try:
        if message.content.startswith("!"):
            await bot.process_commands(message)
            return

        channel_id = str(message.channel.id)
        user_id = str(message.author.id)

        await save_message(channel_id, user_id, "user", message.content)

        async with message.channel.typing():
            context = await build_context(channel_id, user_id, message.author.display_name)
            reply = await ask_ai(context)

            await save_message(channel_id, user_id, "assistant", reply)

            if not reply:
                await message.channel.send("mrrp… no reply 🥺")
                return

            for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
                await message.channel.send(chunk)

    except Exception as e:
        print("ON_MESSAGE ERROR:", repr(e))
        await message.channel.send("error happened 🥺")


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
    await ctx.send("admin added 🐾")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))
    await ctx.send("admin removed 🐾")


@bot.command()
async def listadmins(ctx):
    await ctx.send("\n".join(f"<@{a}>" for a in admins) or "no admins")


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


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id

    await init_db()
    await load_admins()

    bot_owner_id = (await bot.application_info()).owner.id

    print(f"Bot ready 🐾 | admins: {len(admins)}")


bot.run(DISCORD_TOKEN)
