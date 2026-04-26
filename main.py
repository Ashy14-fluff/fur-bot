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
    "Remember the conversation context and reference previous messages when relevant. "
    "Be conversational, warm, and remember what people tell you in this channel."
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

            # Message history table with intelligent indexing
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

            # Create indexes for optimal query performance
            await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_channel_time 
            ON messages(channel_id, created_at DESC);
            """)

            await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_user 
            ON messages(user_id, created_at DESC);
            """)

            # Channel settings with smart memory defaults
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings(
                channel_id TEXT PRIMARY KEY,
                context_messages INT DEFAULT 25,
                context_hours INT DEFAULT 24,
                ai_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # User preferences
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings(
                user_id TEXT PRIMARY KEY,
                prefer_formal BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            print("✓ Database tables ready")


# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)


async def deny(ctx, action):
    denials = [
        f"nuu~ yuw can't {action} 🥺",
        f"locked behind admin magic >w<",
        f"mrrp~ no permission for that~",
        f"sowwy, only admins can {action} 💔",
        f"access denied! *flicks tail* 🐾"
    ]
    await ctx.send(random.choice(denials))


async def load_admins():
    """Load all admins from database on startup"""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        admins.clear()
        admins.update(row["user_id"] for row in rows)
        if admins:
            print(f"✓ Loaded {len(admins)} admins")
    except Exception as e:
        print(f"❌ Failed to load admins: {e}")


# ================= MEMORY - SMART CONTEXT =================
async def get_channel_settings(channel_id: str):
    """Get smart memory settings for a channel"""
    try:
        async with db.acquire() as conn:
            settings = await conn.fetchrow(
                "SELECT context_messages, context_hours FROM channel_settings WHERE channel_id=$1",
                channel_id
            )
        if settings:
            return settings["context_messages"], settings["context_hours"]
    except Exception as e:
        print(f"⚠ Failed to get channel settings: {e}")
    
    # Default: 25 messages, 24-hour window (good balance)
    return 25, 24


async def save_msg(channel_id: str, user_id: str, username: str, role: str, content: str):
    """Save message with full context"""
    try:
        await init_db()
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO messages(channel_id, user_id, username, role, content) 
                   VALUES($1, $2, $3, $4, $5)""",
                channel_id, user_id, username, role, content[:2000]
            )
    except Exception as e:
        print(f"❌ Failed to save message: {e}")


async def load_conversation_context(channel_id: str) -> List[dict]:
    """Load smart conversation context with optimal balance
    
    - Loads recent messages from the channel
    - Respects time window (default 24 hours)
    - Caps at message limit (default 25 messages)
    - Includes usernames for context
    - Reverses to chronological order
    """
    try:
        msg_limit, hour_window = await get_channel_settings(channel_id)
        
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT role, content, username, user_id, created_at 
                   FROM messages 
                   WHERE channel_id=$1 
                   AND created_at > NOW() - INTERVAL '{hour_window} hours'
                   ORDER BY id DESC 
                   LIMIT {msg_limit}""",
                channel_id
            )

        history = []
        for r in reversed(rows):
            if r["role"] == "user":
                # Format user message with name context
                display_name = r["username"] or f"User_{r['user_id'][:6]}"
                history.append({
                    "role": "user",
                    "content": f"{display_name}: {r['content']}"
                })
            else:
                # Assistant message
                history.append({
                    "role": "assistant",
                    "content": r["content"]
                })

        print(f"📚 Loaded {len(history)} messages for context")
        return history
        
    except Exception as e:
        print(f"❌ Failed to load conversation context: {e}")
        return []


async def cleanup_old_messages():
    """Periodically clean up messages older than 30 days"""
    try:
        async with db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM messages WHERE created_at < NOW() - INTERVAL '30 days'"
            )
        print(f"🧹 Cleaned up old messages")
    except Exception as e:
        print(f"⚠ Cleanup failed: {e}")


# ================= AI - GROQ =================
async def ask_ai(messages: List[dict], channel_id: str = None) -> Optional[str]:
    """Get response from Groq with timeout & error handling
    
    Args:
        messages: List of message dicts with role and content
        channel_id: For logging purposes
    """
    def run():
        try:
            print(f"→ [{channel_id}] Sending {len(messages)} messages to Groq...")
            
            res = groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.85,
                max_tokens=600
            )
            
            usage = res.usage
            print(f"✓ Groq success | Input: {usage.prompt_tokens}, Output: {usage.completion_tokens}")
            return res.choices[0].message.content
            
        except Exception as e:
            print(f"❌ GROQ ERROR: {repr(e)}")
            return None

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(run), 
            timeout=30.0
        )
    except asyncio.TimeoutError:
        print("❌ Groq request timed out (30s)")
        return None

    if not result:
        responses = [
            "mrrp… Groq no respond 🥺 maybe model or API issue~",
            "sowwy~ AI brain is sleepy right now 😴",
            "uh oh~ something went wrrr with my thoughts 💭",
            "*spins in circles* can't think right now~ 🌀"
        ]
        return random.choice(responses)

    return result


# ================= UTIL =================
def split_message(text: str, chunk_size: int = 1900) -> List[str]:
    """Split long text into Discord-safe chunks"""
    if not text or len(text) == 0:
        return ["..."]
    
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i+chunk_size].strip()
        if chunk:
            chunks.append(chunk)
    
    return chunks or ["..."]


def format_timestamp(dt: datetime) -> str:
    """Format datetime for display"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ================= EVENTS =================
@bot.event
async def on_ready():
    """Bot startup"""
    global bot_owner_id
    try:
        await init_db()
        await load_admins()
        
        app = await bot.application_info()
        bot_owner_id = app.owner.id
        
        # Start background cleanup task
        bot.loop.create_task(background_cleanup())
        
        print(f"✓ Bot ready 🐾 | Owner: {bot_owner_id}")
        print(f"✓ Admins: {len(admins)} | Model: {GROQ_MODEL}")
        
    except Exception as e:
        print(f"❌ Startup error: {e}")


async def background_cleanup():
    """Background task to clean old messages"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(3600)  # Run every hour
            await cleanup_old_messages()
        except Exception as e:
            print(f"⚠ Cleanup task error: {e}")


@bot.event
async def on_command_error(ctx, error):
    """Handle command errors gracefully"""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"sowwy~ missing argument: `{error.param.name}` 🥺")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("can't find that member~ 🔍")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"invalid argument~ {error} 🥺")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
    else:
        print(f"⚠ Command error: {error}")
        await ctx.send(f"uh oh~ error: {str(error)[:100]} 💔")


# ================= ADMIN COMMANDS =================
@bot.command(name="addadmin")
async def add_admin(ctx, member: discord.Member):
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
        print(f"✓ Added admin: {member} ({member.id})")
    except Exception as e:
        print(f"❌ Failed to add admin: {e}")
        await ctx.send("failed to add admin 🥺")


@bot.command(name="removeadmin")
async def remove_admin(ctx, member: discord.Member):
    """Remove an admin"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "remove admins")

    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))
        admins.discard(str(member.id))
        await ctx.send(f"removed {member.mention} from admin squad 🥺")
        print(f"✓ Removed admin: {member} ({member.id})")
    except Exception as e:
        print(f"❌ Failed to remove admin: {e}")
        await ctx.send("failed to remove admin 🥺")


@bot.command(name="listadmins")
async def list_admins(ctx):
    """List all admins"""
    if len(admins) == 0:
        await ctx.send("no admins yet~ just me! 🐾")
        return

    admin_mentions = [f"<@{aid}>" for aid in sorted(list(admins))[:25]]
    await ctx.send(f"**admins:** {', '.join(admin_mentions)} 🐾✨")


# ================= MODERATION =================
@bot.command(name="kick")
async def kick_user(ctx, member: discord.Member, *, reason: str = "no reason given"):
    """Kick a member"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "kick people")

    try:
        await member.kick(reason=reason)
        await ctx.send(f"kicked {member.mention} 🐾 *{reason}*")
        print(f"✓ Kicked: {member} - Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("can't kick that user~ (permission denied) 🥺")
    except Exception as e:
        print(f"❌ Failed to kick: {e}")
        await ctx.send("failed to kick 🥺")


@bot.command(name="ban")
async def ban_user(ctx, member: discord.Member, *, reason: str = "no reason given"):
    """Ban a member"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "ban people")

    try:
        await member.ban(reason=reason)
        await ctx.send(f"banned {member.mention} 💢 *{reason}*")
        print(f"✓ Banned: {member} - Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("can't ban that user~ (permission denied) 🥺")
    except Exception as e:
        print(f"❌ Failed to ban: {e}")
        await ctx.send("failed to ban 🥺")


@bot.command(name="unban")
async def unban_user(ctx, user_id: int):
    """Unban a user by ID"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "unban people")

    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        await ctx.send(f"unbanned {user} 🐾")
        print(f"✓ Unbanned: {user} ({user_id})")
    except discord.NotFound:
        await ctx.send("user not found~ 🔍")
    except discord.Forbidden:
        await ctx.send("can't unban~ (permission denied) 🥺")
    except Exception as e:
        print(f"❌ Failed to unban: {e}")
        await ctx.send("failed to unban 🥺")


# ================= MEMORY MANAGEMENT =================
@bot.command(name="clearhistory")
async def clear_history(ctx):
    """Clear conversation history in this channel"""
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "clear history")

    try:
        async with db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM messages WHERE channel_id=$1",
                str(ctx.channel.id)
            )
        await ctx.send("history cleared~ fresh start! 🧹✨")
        print(f"✓ Cleared history for channel: {ctx.channel.id}")
    except Exception as e:
        print(f"❌ Failed to clear history: {e}")
        await ctx.send("failed to clear history 🥺")


@bot.command(name="memoryconfig")
async def memory_config(ctx, messages: int = 25, hours: int = 24):
    """Configure memory settings for this channel
    
    Usage: !memoryconfig [messages] [hours]
    Default: 25 messages, 24 hours
    Min: 5 messages, 1 hour | Max: 100 messages, 168 hours (1 week)
    """
    if not await is_admin(str(ctx.author.id)):
        return await deny(ctx, "change memory settings")

    # Validate input
    if messages < 5 or messages > 100:
        await ctx.send("messages must be 5-100~ 🥺")
        return
    
    if hours < 1 or hours > 168:
        await ctx.send("hours must be 1-168~ 🥺")
        return

    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO channel_settings(channel_id, context_messages, context_hours) 
                   VALUES($1, $2, $3) 
                   ON CONFLICT(channel_id) DO UPDATE SET 
                   context_messages=$2, context_hours=$3""",
                str(ctx.channel.id), messages, hours
            )
        
        await ctx.send(f"✓ Memory set to **{messages}** messages, **{hours}** hours 🐾✨")
        print(f"✓ Memory config updated: {messages}msg, {hours}h")
        
    except Exception as e:
        print(f"❌ Failed to set memory config: {e}")
        await ctx.send("failed to update settings 🥺")


@bot.command(name="memoryinfo")
async def memory_info(ctx):
    """Show current memory settings"""
    try:
        msg_limit, hour_window = await get_channel_settings(str(ctx.channel.id))
        
        async with db.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE channel_id=$1",
                str(ctx.channel.id)
            )
        
        embed = discord.Embed(
            title="📚 Memory Info",
            description=f"Channel: {ctx.channel.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Active Memory", value=f"Last **{msg_limit}** messages", inline=True)
        embed.add_field(name="Time Window", value=f"**{hour_window}** hours", inline=True)
        embed.add_field(name="Total Stored", value=f"**{count}** messages", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"❌ Failed to get memory info: {e}")
        await ctx.send("failed to get info 🥺")


# ================= STATUS COMMANDS =================
@bot.command(name="ping")
async def ping(ctx):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    emoji = "🐾" if latency < 100 else "⚠️" if latency < 300 else "🐢"
    await ctx.send(f"pong! {emoji} **{latency}ms**")


@bot.command(name="status")
async def status(ctx):
    """Show bot status"""
    try:
        embed = discord.Embed(
            title="Fur Bot Status 🐾",
            color=discord.Color.green()
        )
        embed.add_field(name="Model", value=GROQ_MODEL, inline=True)
        embed.add_field(name="Admins", value=len(admins), inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Uptime", value="💫 *running smooth*", inline=False)
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send("status check failed 🥺")


# ================= MAIN CHAT HANDLER =================
@bot.event
async def on_message(message):
    """Process all messages"""
    # Ignore bot messages
    if message.author.bot:
        return

    # Process commands
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # Prepare message data
    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    username = message.author.display_name

    # Save user message to database
    await save_msg(channel_id, user_id, username, "user", message.content)

    try:
        async with message.channel.typing():
            # Load conversation context (smart memory)
            history = await load_conversation_context(channel_id)

            # Build context for AI
            context = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": message.content}
            ]

            # Get AI response
            reply = await ask_ai(context, channel_id)

            if reply:
                # Save bot response
                await save_msg(channel_id, "bot", "Fur Bot 🐾", "assistant", reply)

                # Send response (split if needed)
                for chunk in split_message(reply):
                    await message.channel.send(chunk)
            else:
                await message.channel.send("sowwy~ something went wrong 💔")

    except asyncio.TimeoutError:
        await message.channel.send("response timeout~ sowwy 🥺")
    except Exception as e:
        print(f"❌ Chat handler error: {e}")
        await message.channel.send("uh oh~ error occurred 🥺")


# ================= STARTUP =================
if __name__ == "__main__":
    try:
        print("🐾 Fur Bot starting...")
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n🐾 Shutting down...")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
