import os
import asyncio
import random
import traceback
from datetime import datetime, timezone
from typing import Optional, List

import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncpg
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-70b-8192")  # ✅ FIXED: Valid model name

print(f"🔧 Config: Discord={bool(DISCORD_TOKEN)}, Groq={bool(GROQ_API_KEY)}, DB={bool(DATABASE_URL)}")

if not all([DISCORD_TOKEN, GROQ_API_KEY, DATABASE_URL]):
    raise RuntimeError("Missing env vars!")

groq = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """
You are Fur Bot 🐾.
A cute, fluffy Discord AI companion.

Style:
- soft furry tone (uwu, >w<, mrrp allowed but not spammy)
- friendly, emotional, helpful
- always readable
- never break character

You remember context and user facts.
"""

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None
channel_mood = {}


# ---------------- DB ----------------

async def init_db():
    global db
    if db:
        print("✅ DB already initialized")
        return

    print("🗄️ Connecting to database...")
    try:
        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        print("✅ DB pool created")

        async with db.acquire() as c:
            # ✅ FIXED: Added NOT NULL constraints & indexes
            await c.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await c.execute("""
            CREATE TABLE IF NOT EXISTS user_facts(
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                fact TEXT NOT NULL
            );
            """)

            # ✅ Added indexes for performance
            await c.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id DESC);")
        
        print("✅ Tables ready")
    except Exception as e:
        print(f"❌ DB init failed: {repr(e)}")
        raise


# ---------------- MEMORY ----------------

async def save_message(channel_id, user_id, role, content):
    if not db:
        print("⚠️ DB not ready, skipping save")
        return
    try:
        async with db.acquire() as c:
            await c.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                channel_id, user_id, role, content[:2000]
            )
    except Exception as e:
        print(f"⚠️ Save message failed: {repr(e)}")


async def load_history(channel_id, limit=10):
    if not db:
        print("⚠️ DB not ready, empty history")
        return []

    try:
        async with db.acquire() as c:
            rows = await c.fetch("""
                SELECT role, content
                FROM messages
                WHERE channel_id=$1
                ORDER BY id DESC
                LIMIT $2
            """, channel_id, limit)

        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        print(f"📚 Loaded {len(history)} messages from history")
        return history
    except Exception as e:
        print(f"⚠️ Load history failed: {repr(e)}")
        return []


# ---------------- AI ----------------

async def ask_groq(messages):
    """✅ FIXED: Proper error handling + valid model"""
    def call():
        try:
            print(f"🌐 Groq call: {len(messages)} msgs, model={GROQ_MODEL}")
            completion = groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.9,
                max_tokens=1500  # ✅ Added token limit
            )
            content = completion.choices[0].message.content or ""
            print(f"✅ Groq reply: {len(content)} chars")
            return content
        except Exception as e:
            print(f"❌ Groq error: {repr(e)}")
            return f"mrrp… AI having trouble (error: {str(e)[:50]}) 🥺"

    return await asyncio.to_thread(call)


# ---------------- BOT EVENTS ----------------

@bot.event
async def on_ready():
    print("🚀 Bot starting...")
    try:
        await init_db()
        print(f"✅ Logged in as {bot.user}")
        await bot.change_presence(activity=discord.Game(name="🐾 fluffy chats"))
    except Exception as e:
        print(f"❌ on_ready failed: {repr(e)}")
        traceback.print_exc()


@bot.command()
async def ping(ctx):
    await ctx.send("pong 🐾")


@bot.command()
async def debug(ctx):
    """Debug command to test everything"""
    await ctx.send(f"```yaml\nDB: {'✅' if db else '❌'}\nHistory len: {len(await load_history(str(ctx.channel.id)))}\n```")


# ---------------- MAIN CHAT ----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    print(f"\n📨 [{message.author}] {content}")

    # allow commands
    if content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    bot_id = str(bot.user.id)

    try:
        print("📚 Loading history...")
        history = await load_history(channel_id, limit=12)

        # ✅ FIXED: Proper message structure
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": f"{message.author.display_name}: {content}"}
        ]

        print(f"🤖 Calling AI with {len(messages)} total messages...")
        reply = await ask_groq(messages)

        if not reply or len(reply.strip()) < 5:
            reply = "mrrp… me got no good answer 🥺"

        # ✅ FIXED: Save BOTH user and bot messages with correct roles
        await save_message(channel_id, user_id, "user", content)
        await save_message(channel_id, bot_id, "assistant", reply)

        print(f"📤 Sending: {reply[:100]}...")
        # Split long replies
        for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
            await message.channel.send(chunk)

    except Exception as e:
        print(f"\n💥 FULL ERROR:")
        print(f"Type: {type(e).__name__}")
        print(f"Msg: {repr(e)}")
        traceback.print_exc()
        
        await message.channel.send("oopsie… something broke 🥺\nTry `!debug` or check console!")

    await bot.process_commands(message)


# ---------------- RUN ----------------

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("👋 Bot stopped")
    except Exception as e:
        print(f"💥 Fatal: {repr(e)}")
        traceback.print_exc()
