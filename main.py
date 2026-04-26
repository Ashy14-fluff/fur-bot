import os
import asyncio
import traceback
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

MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env variables: DISCORD_TOKEN, GROQ_API_KEY, DATABASE_URL")

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
You remember context and speak naturally.
Keep responses under 1800 characters.
"""

# ================= DB INIT =================
async def init_db():
    global db
    if db:
        return

    async with lock:
        if db:
            return

        try:
            db = await asyncpg.create_pool(
                DATABASE_URL, 
                min_size=1, 
                max_size=10,
                command_timeout=30,
                server_settings={'jit': 'off'}
            )

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
            print("✅ Database initialized")
        except Exception as e:
            print(f"❌ DB INIT FAILED: {repr(e)}")
            raise

# ================= ADMIN =================
async def is_admin(uid: str):
    return uid in admins or (bot_owner_id and int(uid) == bot_owner_id)

async def load_admins():
    global admins
    try:
        async with db.acquire(timeout=5.0) as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        admins.clear()
        admins.update(r["user_id"] for r in rows)
        print(f"✅ Loaded {len(admins)} admins")
    except Exception as e:
        print(f"❌ Admin load error: {repr(e)}")

# ================= MEMORY =================
async def save_message(channel_id, user_id, role, content):
    try:
        async with db.acquire(timeout=5.0) as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                channel_id, user_id, role, content[:2000]
            )
    except Exception as e:
        print("DB save error:", repr(e))

async def load_history(channel_id, limit=20):
    try:
        async with db.acquire(timeout=5.0) as conn:
            rows = await conn.fetch(
                "SELECT role,content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
                channel_id, limit
            )
        return list(reversed([dict(r) for r in rows]))
    except Exception as e:
        print("DB load error:", repr(e))
        return []

# ================= AI CALL (ROBUST + RETRY) =================
async def ask_ai(messages, max_retries=3):
    for attempt in range(max_retries):
        try:
            def run():
                print("🧠 Sending request to Groq...")
                res = groq.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    temperature=0.85,
                    max_tokens=700
                )
                return res.choices[0].message.content

            result = await asyncio.to_thread(run, timeout=30)

            if not result or len(result.strip()) == 0:
                return "mrrp… empty response 🥺"

            return result.strip()

        except asyncio.TimeoutError:
            print(f"⏰ Groq timeout (attempt {attempt+1}/{max_retries})")
        except Exception as e:
            error_str = str(e).lower()
            if "rate_limit" in error_str and attempt < max_retries-1:
                wait_time = 2 ** attempt
                print(f"⏳ Rate limited, waiting {wait_time}s (attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            print(f"❌ GROQ ERROR (attempt {attempt+1}): {repr(e)}")
    
    return "mrrp… AI is taking a nap 🥺"

# ================= CONTEXT =================
async def build_context(channel_id, user_id, username):
    history = await load_history(channel_id)
    history = history[-15:]  # Prevent token overflow

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current user: {username}"}
    ]
    
    # Convert history to proper format
    for msg in history:
        messages.append({
            "role": msg["role"], 
            "content": msg["content"][:1000]  # Truncate long messages
        })
    
    return messages

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

        typing_task = asyncio.create_task(message.channel.trigger_typing())
        
        context = await build_context(channel_id, user_id, message.author.display_name)
        reply = await ask_ai(context)

        typing_task.cancel()
        
        await save_message(channel_id, user_id, "assistant", reply)

        if not reply or len(reply.strip()) == 0:
            await message.channel.send("mrrp… no response 🥺")
            return

        # Smart chunking
        for i in range(0, len(reply), 1900):
            chunk = reply[i:i+1900]
            if chunk.strip():
                await message.channel.send(chunk)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("ON_MESSAGE ERROR:", traceback.format_exc())
        try:
            await message.channel.send("something broke 🥺")
        except:
            pass

# ================= ADMIN COMMANDS =================
@bot.command(name="addadmin")
async def add_admin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")

    try:
        async with db.acquire(timeout=5.0) as conn:
            await conn.execute(
                "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
                str(member.id)
            )
        admins.add(str(member.id))
        await ctx.send(f"✅ `{member.display_name}` is now admin 🐾")
    except Exception as e:
        await ctx.send("❌ Database error 🥺")
        print(f"Addadmin error: {repr(e)}")

@bot.command()
async def remadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")

    try:
        async with db.acquire(timeout=5.0) as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))
        admins.discard(str(member.id))
        await ctx.send(f"✅ `{member.display_name}` removed from admins 🐾")
    except Exception as e:
        await ctx.send("❌ Database error 🥺")

@bot.command()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")

    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 `{member.display_name}` kicked\n**Reason:** {reason}")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to kick")
    except Exception as e:
        await ctx.send("❌ Kick failed")
        print(f"Kick error: {repr(e)}")

@bot.command()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")

    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 `{member.display_name}` banned\n**Reason:** {reason}")
    except discord.Forbidden:
        await ctx.send("❌ Missing permissions to ban")
    except Exception as e:
        await ctx.send("❌ Ban failed")
        print(f"Ban error: {repr(e)}")

@bot.command()
async def cleanup(ctx, days: int = 7):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")

    try:
        async with db.acquire(timeout=10.0) as conn:
            result = await conn.execute(
                "DELETE FROM messages WHERE created_at < NOW() - INTERVAL '%s days'", days
            )
        count = int(result.split()[1])
        await ctx.send(f"🧹 Cleaned **{count}** old messages (> {days} days)")
    except Exception as e:
        await ctx.send("❌ Cleanup failed")
        print(f"Cleanup error: {repr(e)}")

@bot.command()
async def status(ctx):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("❌ no permission 🥺")
    
    async with db.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM messages")
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM admins")
    
    embed = discord.Embed(title="🐾 Fur Bot Status", color=0x9b59b6)
    embed.add_field(name="Messages stored", value=f"{msg_count:,}", inline=True)
    embed.add_field(name="Admins", value=str(admin_count), inline=True)
    embed.add_field(name="Model", value=MODEL, inline=True)
    await ctx.send(embed=embed)

# ================= ERROR HANDLING =================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing arguments! Check command usage 🥺")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument! 🥺")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
    else:
        print(f"Command error: {traceback.format_exc()}")
        await ctx.send("❌ Command failed internally 🥺")

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ Bot error in {event}:")
    print(traceback.format_exc())

# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    try:
        await init_db()
        await load_admins()
        
        app_info = await bot.application_info()
        bot_owner_id = app_info.owner.id if app_info.owner else None
        
        print(f"""
🐾 Fur Bot ready!
📊 Guilds: {len(bot.guilds)}
👥 Users: {len(set(bot.get_all_members()))}
⚙️  Model: {MODEL}
👑 Owner: {app_info.owner}
🔧 Admins: {len(admins)}
        """)
        
        # Set status
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="mrrp! 🐾")
        )
    except Exception as e:
        print(f"❌ on_ready failed: {traceback.format_exc()}")
        raise

# Graceful shutdown
async def cleanup():
    global db
    if db:
        await db.close()
    print("👋 Bot shutdown complete")

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    finally:
        if db:
            asyncio.run(cleanup())
