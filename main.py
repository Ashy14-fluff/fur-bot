import os
import asyncio
from typing import Optional, List, Set
from datetime import datetime, timezone

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

groq = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
admin_users: Set[str] = set()
bot_owner_id: Optional[int] = None

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI companion. "
    "You speak naturally with soft furry tone (uwu, >w<, mrrp~). "
    "You are helpful and friendly. Reply only to the current user."
)

# ================= DISCORD =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DB =================
db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


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
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                role TEXT,
                content TEXT
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts (
                user_id TEXT,
                fact TEXT
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id TEXT PRIMARY KEY
            );
            """)


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admin_users or (bot_owner_id and int(uid) == bot_owner_id)


async def load_admins():
    global admin_users
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
        admin_users = {r["user_id"] for r in rows}


# ================= MEMORY =================
async def save_message(cid, uid, role, content):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            cid, uid, role, content[:2000]
        )


async def load_history(cid, limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            cid, limit
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def save_fact(uid, fact):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_facts(user_id,fact) VALUES($1,$2)",
            uid, fact[:500]
        )


# ================= AI =================
async def ask_ai(messages):
    def run():
        res = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.9,
            max_tokens=800
        )
        return res.choices[0].message.content

    return await asyncio.to_thread(run)


def split(text):
    return [text[i:i+1900] for i in range(0, len(text), 1900)]


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()

    bot_owner_id = (await bot.application_info()).owner.id
    print("🐾 Fur Bot ready")


# ================= COMMANDS =================
@bot.command()
async def remember(ctx, *, fact):
    await save_fact(str(ctx.author.id), fact)
    await ctx.send("saved 🐾")


@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("no permission 🥺")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            str(member.id)
        )

    admin_users.add(str(member.id))
    await ctx.send("admin added 🐾")


@bot.command()
async def admins(ctx):
    await ctx.send("\n".join(f"<@{x}>" for x in admin_users) or "no admins 🥺")


# ================= SMART CHAT TRIGGER =================
def should_ai_reply(message: discord.Message) -> bool:
    if message.author.bot:
        return False

    # trigger AI only if:
    return (
        bot.user in message.mentions or   # mention bot
        isinstance(message.channel, discord.DMChannel)  # DM always
    )


# ================= MAIN MESSAGE =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    cid = str(message.channel.id)
    uid = str(message.author.id)

    # save user message
    await save_message(cid, uid, "user", message.content)

    # ONLY AI IF TRIGGERED
    if not should_ai_reply(message):
        return

    async with message.channel.typing():
        try:
            history = await load_history(cid)

            context = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": message.content}
            ]

            reply = await ask_ai(context)

            await save_message(cid, uid, "assistant", reply)

            for part in split(reply):
                await message.channel.send(part)

        except Exception as e:
            print("AI error:", e)
            await message.channel.send("mrrp… me crashed 🥺")


bot.run(DISCORD_TOKEN)
