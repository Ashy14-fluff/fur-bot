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

# 🔥 FIXED MODEL (important)
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env 🥺")

groq = Groq(api_key=GROQ_API_KEY)

# ================= STATE =================
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI companion. "
    "You speak in soft uwu furry style but stay helpful and readable. "
    "You respond naturally and never break character."
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

        try:
            db = await asyncpg.create_pool(DATABASE_URL)
            print("✓ Database connected")
        except Exception as e:
            print(f"❌ Database connection failed: {repr(e)}")
            raise

        async with db.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(user_id TEXT PRIMARY KEY);
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

            print("✓ Database tables initialized")


async def load_admins():
    """Load all admins from database into memory"""
    if not db:
        return
    
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        admins.clear()
        admins.update(row["user_id"] for row in rows)
        print(f"✓ Loaded {len(admins)} admins")
    except Exception as e:
        print(f"❌ Failed to load admins: {repr(e)}")

# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


async def deny(ctx, action):
    await ctx.send(random.choice([
        f"nuu~ yuw can't {action} 🥺",
        f"locked behind admin magic >w<",
        f"mrrp~ no permission"
    ]))

# ================= MEMORY =================
async def save_msg(cid, uid, role, content):
    await init_db()
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                cid, uid, role, content[:2000]
            )
    except Exception as e:
        print(f"❌ Failed to save message: {repr(e)}")


async def load_history(cid):
    """Load last 12 messages from channel history"""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT 12",
                cid
            )
        return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))
    except Exception as e:
        print(f"❌ Failed to load history: {repr(e)}")
        return []

# ================= AI =================
async def ask_ai(messages):
    def run():
        try:
            print("→ sending request to Groq...")

            res = groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.85,
                max_tokens=600
            )

            print("✓ Groq success")
            return res.choices[0].message.content

        except Exception as e:
            print("❌ GROQ ERROR:", repr(e))
            return None

    result = await asyncio.to_thread(run)

    if not result:
        return "mrrp… Groq no respond 🥺 maybe model or API issue~"

    return result


# ================= UTIL =================
def split(text):
    """Split text into chunks of 1900 characters"""
    if not text:
        return ["..."]
    return [text[i:i+1900] for i in range(0, len(text), 1900)] or ["..."]


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()
    app = await bot.application_info()
    bot_owner_id = app.owner.id
    print(f"✓ Bot ready 🐾 | Owner: {bot_owner_id}")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    """Add a user as admin"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "add admins")

    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING",
                str(member.id)
            )

        admins.add(str(member.id))
        await ctx.send(f"added {member.mention} 🐾")
    except Exception as e:
        print(f"❌ Failed to add admin: {repr(e)}")
        await ctx.send("failed to add admin 🥺")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    """Remove a user as admin"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "remove admins")

    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

        admins.discard(str(member.id))
        await ctx.send(f"removed {member.mention} 🐾")
    except Exception as e:
        print(f"❌ Failed to remove admin: {repr(e)}")
        await ctx.send("failed to remove admin 🥺")


@bot.command()
async def listadmins(ctx):
    """List all admins"""
    if not admins:
        await ctx.send("no admins yet 🥺")
        return
    
    admin_list = "\n".join([f"• <@{uid}>" for uid in admins])
    await ctx.send(f"admins 🐾:\n{admin_list}")


# ================= MODERATION =================
@bot.command()
async def kick(ctx, member: discord.Member):
    """Kick a member from the server"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "kick people")

    try:
        await member.kick()
        await ctx.send(f"kicked {member.mention} 🐾")
    except Exception as e:
        print(f"❌ Failed to kick: {repr(e)}")
        await ctx.send("failed 🥺")


@bot.command()
async def ban(ctx, member: discord.Member):
    """Ban a member from the server"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "ban people")

    try:
        await member.ban()
        await ctx.send(f"banned {member.mention} 💢")
    except Exception as e:
        print(f"❌ Failed to ban: {repr(e)}")
        await ctx.send("failed 🥺")


@bot.command()
async def unban(ctx, user: discord.User):
    """Unban a user"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "unban people")

    try:
        await ctx.guild.unban(user)
        await ctx.send(f"unbanned {user.mention} 🐾")
    except Exception as e:
        print(f"❌ Failed to unban: {repr(e)}")
        await ctx.send("failed 🥺")


# ================= CHAT =================
@bot.event
async def on_message(message):
    """Handle incoming messages"""
    if message.author.bot:
        return

    # Process commands first
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    cid = str(message.channel.id)
    uid = str(message.author.id)

    # Save user message
    await save_msg(cid, uid, "user", message.content)

    # Generate AI response
    async with message.channel.typing():
        history = await load_history(cid)

        ctx = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": message.content}
        ]

        reply = await ask_ai(ctx)

        # Save AI response
        await save_msg(cid, uid, "assistant", reply)

        # Send response in chunks
        for part in split(reply):
            try:
                await message.channel.send(part)
            except Exception as e:
                print(f"❌ Failed to send message: {repr(e)}")


@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"mrrp~ missing argument 🥺: {error.param}")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("couldn't find that member 🥺")
    elif isinstance(error, commands.UserNotFound):
        await ctx.send("couldn't find that user 🥺")
    else:
        print(f"❌ Command error: {repr(error)}")
        await ctx.send("something went wrong 🥺")


# ================= RUN =================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
