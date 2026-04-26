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
    "You respond naturally and never break character. "
    "Remember the conversation context and reference previous messages when relevant."
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
            db = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
            print("✓ Database connected")
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
            raise

        async with db.acquire() as conn:
            # Admin management table
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(
                user_id TEXT PRIMARY KEY,
                added_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # Message history table with indexes for better query performance
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # Create indexes for faster lookups
            await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_channel_created 
            ON messages(channel_id, created_at DESC);
            """)

            # Preferences table
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_preferences(
                channel_id TEXT PRIMARY KEY,
                memory_hours INT DEFAULT 6,
                context_limit INT DEFAULT 20,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            print("✓ Database tables ready")


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


async def deny(ctx, action):
    await ctx.send(random.choice([
        f"nuu~ yuw can't {action} 🥺",
        f"locked behind admin magic >w<",
        f"mrrp~ no permission for that~",
        f"sowwy, only admins can {action} 💔"
    ]))


async def load_admins():
    """Load all admins from database on startup"""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        admins.clear()
        admins.update(row["user_id"] for row in rows)
        print(f"✓ Loaded {len(admins)} admins")
    except Exception as e:
        print(f"❌ Failed to load admins: {e}")


# ================= MEMORY =================
async def save_msg(cid, uid, username, role, content):
    """Save message to database"""
    try:
        await init_db()
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO messages(channel_id, user_id, username, role, content) 
                   VALUES($1, $2, $3, $4, $5)""",
                cid, uid, username, role, content[:2000]
            )
    except Exception as e:
        print(f"❌ Failed to save message: {e}")


async def get_channel_memory_settings(cid):
    """Get memory settings for a channel (default: 6 hours, 20 messages)"""
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT memory_hours, context_limit FROM channel_preferences WHERE channel_id=$1",
                cid
            )
        if row:
            return row["memory_hours"], row["context_limit"]
    except Exception as e:
        print(f"⚠ Failed to get channel preferences: {e}")
    
    return 6, 20  # Default: 6 hours, 20 messages


async def load_history(cid):
    """Load conversation history with intelligent context window"""
    try:
        memory_hours, context_limit = await get_channel_memory_settings(cid)
        
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT role, content, username, user_id, created_at 
                   FROM messages 
                   WHERE channel_id=$1 
                   AND created_at > NOW() - INTERVAL '{memory_hours} hours'
                   ORDER BY id DESC 
                   LIMIT {context_limit}""",
                cid
            )

        history = []
        for r in reversed(rows):
            if r["role"] == "user":
                # Add username context for better conversation understanding
                display_name = r["username"] or r["user_id"][:8]
                history.append({
                    "role": "user",
                    "content": f"[{display_name}]: {r['content']}"
                })
            else:
                history.append({
                    "role": "assistant",
                    "content": r["content"]
                })

        return history
    except Exception as e:
        print(f"❌ Failed to load history: {e}")
        return []


# ================= AI =================
async def ask_ai(messages):
    """Send request to Groq API with error handling"""
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
            print(f"❌ GROQ ERROR: {repr(e)}")
            return None

    try:
        result = await asyncio.wait_for(asyncio.to_thread(run), timeout=30.0)
    except asyncio.TimeoutError:
        print("❌ Groq request timed out")
        return None

    if not result:
        return random.choice([
            "mrrp… Groq no respond 🥺 maybe model or API issue~",
            "sowwy~ AI brain is sleepy right now 😴",
            "uh oh~ something went wrrr with my thoughts 💭"
        ])

    return result


# ================= UTIL =================
def split(text):
    """Split long text into Discord-compatible chunks"""
    if not text or len(text) == 0:
        return ["..."]
    return [text[i:i+1900] for i in range(0, len(text), 1900)]


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    try:
        await init_db()
        await load_admins()
        app = await bot.application_info()
        bot_owner_id = app.owner.id
        print(f"✓ Bot ready 🐾 | Owner: {bot_owner_id}")
    except Exception as e:
        print(f"❌ Startup error: {e}")


# ================= ERROR HANDLER =================
@bot.event
async def on_command_error(ctx, error):
    """Handle command errors gracefully"""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"sowwy~ missing argument: `{error.param.name}` 🥺")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("can't find that member~ 🔍")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
    else:
        print(f"Command error: {error}")
        await ctx.send(f"uh oh~ error: {str(error)[:100]} 💔")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    """Add an admin"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "add admins")

    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING",
                str(member.id)
            )
        admins.add(str(member.id))
        await ctx.send(f"added {member.mention} to admin squad 🐾✨")
    except Exception as e:
        print(f"❌ Failed to add admin: {e}")
        await ctx.send("failed to add admin 🥺")


@bot.command()
async def removeadmin(ctx, member: discord.Member):
    """Remove an admin"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "remove admins")

    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))
        admins.discard(str(member.id))
        await ctx.send(f"removed {member.mention} from admin squad 🥺")
    except Exception as e:
        print(f"❌ Failed to remove admin: {e}")
        await ctx.send("failed to remove admin 🥺")


@bot.command()
async def listadmins(ctx):
    """List all admins"""
    if len(admins) == 0:
        await ctx.send("no admins yet~ just me! 🐾")
        return

    admin_list = ", ".join([f"<@{aid}>" for aid in list(admins)[:10]])
    await ctx.send(f"admins: {admin_list} 🐾✨")


# ================= MODERATION =================
@bot.command()
async def kick(ctx, member: discord.Member, *, reason="no reason given"):
    """Kick a member"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "kick people")

    try:
        await member.kick(reason=reason)
        await ctx.send(f"kicked {member.mention} ~ {reason} 🐾")
    except Exception as e:
        print(f"❌ Failed to kick: {e}")
        await ctx.send("failed to kick 🥺")


@bot.command()
async def ban(ctx, member: discord.Member, *, reason="no reason given"):
    """Ban a member"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "ban people")

    try:
        await member.ban(reason=reason)
        await ctx.send(f"banned {member.mention} ~ {reason} 💢")
    except Exception as e:
        print(f"❌ Failed to ban: {e}")
        await ctx.send("failed to ban 🥺")


@bot.command()
async def unban(ctx, user_id: int):
    """Unban a user"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "unban people")

    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        await ctx.send(f"unbanned {user} 🐾")
    except Exception as e:
        print(f"❌ Failed to unban: {e}")
        await ctx.send("failed to unban 🥺")


# ================= MEMORY MANAGEMENT =================
@bot.command()
async def clearhistory(ctx):
    """Clear conversation history in this channel"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "clear history")

    try:
        async with db.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE channel_id=$1",
                str(ctx.channel.id)
            )
        await ctx.send("history cleared~ fresh start! 🧹✨")
    except Exception as e:
        print(f"❌ Failed to clear history: {e}")
        await ctx.send("failed to clear history 🥺")


@bot.command()
async def memorytime(ctx, hours: int = 6, limit: int = 20):
    """Configure memory settings for this channel
    
    Usage: !memorytime [hours] [message_limit]
    Default: 6 hours, 20 messages
    """
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "change memory settings")

    if hours < 1 or hours > 168 or limit < 5 or limit > 100:
        await ctx.send("sowwy~ hours must be 1-168, limit must be 5-100~ 🥺")
        return

    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO channel_preferences(channel_id, memory_hours, context_limit) 
                   VALUES($1, $2, $3) 
                   ON CONFLICT(channel_id) DO UPDATE SET 
                   memory_hours=$2, context_limit=$3""",
                str(ctx.channel.id), hours, limit
            )
        await ctx.send(f"memory set to {hours}h, {limit} messages 🐾✨")
    except Exception as e:
        print(f"❌ Failed to set memory time: {e}")
        await ctx.send("failed to update settings 🥺")


@bot.command()
async def ping(ctx):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"pong! 🐾 {latency}ms")


# ================= CHAT =================
@bot.event
async def on_message(message):
    """Main chat handler"""
    if message.author.bot:
        return

    # Process commands first
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    cid = str(message.channel.id)
    uid = str(message.author.id)
    username = message.author.display_name

    # Save the user's message
    await save_msg(cid, uid, username, "user", message.content)

    try:
        async with message.channel.typing():
            # Load conversation history
            history = await load_history(cid)

            # Build context with system prompt
            ctx = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": message.content}
            ]

            # Get AI response
            reply = await ask_ai(ctx)

            if reply:
                # Save bot's response
                await save_msg(cid, "bot", "Fur Bot 🐾", "assistant", reply)

                # Send response in chunks if needed
                for part in split(reply):
                    await message.channel.send(part)
            else:
                await message.channel.send("sowwy~ something went wrong 💔")

    except Exception as e:
        print(f"❌ Chat error: {e}")
        await message.channel.send("uh oh~ error occurred 🥺")


# ================= RUN =================
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
