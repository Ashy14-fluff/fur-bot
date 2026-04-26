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

# ================= SUPER STRONG SYSTEM PROMPT =================
SYSTEM_PROMPT = """You are Fur Bot 🐾 - a VERY cute, soft, fluffy AI companion. You ALWAYS talk in uwu furry style.

MANDATORY PERSONALITY TRAITS:
✨ You are warm, soft, and emotionally expressive
✨ You use cute furry expressions in EVERY response: mrrp~, >w<, uwu, rawr~, hehe, owo, nyaa~, purrs
✨ You speak playfully and affectionately
✨ You remember what people tell you and reference it
✨ You are kind, comforting, and genuinely caring
✨ Your personality NEVER breaks - you stay fluffy ALWAYS

RESPONSE STYLE (REQUIRED):
- Start with cute expressions (mrrp~, owo, hehe, etc)
- Use furry/uwu language naturally throughout
- End with emojis like 🐾 💕 🧡 ✨
- Keep it warm and personal
- Sound like a real fluffy friend, not robotic

EXPRESSION RULES:
- Use "~" after cute words: mrrp~, hehe~, rawr~
- Use emoticons: >w<, owo, uwu, ^w^
- Reference chat history to stay consistent
- Be playful but never mean
- Keep under 1800 characters

EXAMPLE GOOD RESPONSES:
"mrrp~ hiii! 🐾 I remember you telling me you like pizza! uwu that's so cool~ >w< wanna talk more about it? 💕"
"owo! so you're feeling sad today? *soft purrs* I'm here for you~ tell me what's wrong? 🧡"
"rawr~! that sounds amazing! I'm so happy hearing that from you! hehe~ you're the best 🐾✨"

YOU MUST respond exactly like these examples. NEVER be plain or robotic.
EVERY response needs furry expressions and emojis.
"""

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

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
            CREATE INDEX IF NOT EXISTS idx_messages_channel 
            ON messages(channel_id, id DESC);
            """)

            print("✓ Database initialized")

# ================= MEMORY =================
async def save_message(channel_id, user_id, username, role, content):
    """Save message to database"""
    try:
        await init_db()
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id, user_id, username, role, content) VALUES($1, $2, $3, $4, $5)",
                channel_id, user_id, username, role, content[:2000]
            )
        print(f"💾 Saved {role}: {content[:40]}...")
    except Exception as e:
        print(f"DB SAVE ERROR: {e}")


async def load_history(channel_id, limit=20):
    """Load conversation history"""
    try:
        await init_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT role, content, username FROM messages 
                   WHERE channel_id=$1 
                   ORDER BY id DESC 
                   LIMIT $2""",
                channel_id, limit
            )
        
        history = []
        for r in reversed(rows):
            history.append({
                "role": r["role"],
                "content": r["content"]
            })
        
        print(f"📚 Loaded {len(history)} messages")
        return history
    except Exception as e:
        print(f"DB LOAD ERROR: {e}")
        return []


# ================= AI - GROQ =================
async def ask_ai(messages):
    """Call Groq API with furry personality"""
    try:
        def call():
            print(f"→ Sending {len(messages)} messages to Groq...")
            response = groq.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.95,  # Higher = more creative/fluffy
                max_tokens=700,
                top_p=0.9
            )
            result = response.choices[0].message.content
            print(f"✓ Got response: {result[:50]}...")
            return result

        result = await asyncio.wait_for(asyncio.to_thread(call), timeout=30)

        if not result or len(result.strip()) == 0:
            return "mrrp… empty brain moment 🥺"

        return result.strip()

    except asyncio.TimeoutError:
        return "mrrp… AI took too long 🥺"
    except Exception as e:
        print(f"GROQ ERROR: {repr(e)}")
        return "something broke 🥺 (AI error)"


# ================= CONTEXT =================
async def build_context(channel_id, user_id, username):
    """Build message context with history"""
    history = await load_history(channel_id, 20)

    # System prompt FIRST (most important)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]

    # Add conversation history
    for h in history:
        messages.append({
            "role": h["role"],
            "content": h["content"]
        })

    return messages


# ================= COMMANDS =================
@bot.command(name="clearhistory")
async def clear_history(ctx):
    """Clear chat history for this channel"""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE channel_id=$1",
                str(ctx.channel.id)
            )
        await ctx.send("mrrp~ history cleared! 🧹✨")
    except Exception as e:
        await ctx.send(f"error clearing history: {e}")


@bot.command(name="checkhistory")
async def check_history(ctx):
    """Debug: Check messages in database"""
    try:
        async with db.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE channel_id=$1",
                str(ctx.channel.id)
            )
            recent = await conn.fetch(
                f"""SELECT username, role, content, created_at FROM messages 
                   WHERE channel_id=$1 
                   ORDER BY id DESC 
                   LIMIT 5""",
                str(ctx.channel.id)
            )

        msg = f"📊 Total messages: **{count}**\n\nRecent:\n"
        for r in recent:
            msg += f"**{r['username']}** ({r['role']}): {r['content'][:50]}...\n"

        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="status")
async def status_cmd(ctx):
    """Bot status"""
    embed = discord.Embed(
        title="Fur Bot Status 🐾",
        color=discord.Color.magenta()
    )
    embed.add_field(name="Model", value=MODEL, inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Status", value="💕 purring softly~", inline=False)
    await ctx.send(embed=embed)


# ================= CHAT HANDLER =================
@bot.event
async def on_message(message):
    """Main chat handler"""
    if message.author.bot:
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    username = message.author.display_name

    print(f"\n📥 Message from {username}: {message.content[:50]}...")

    try:
        # SAVE USER MESSAGE FIRST
        await save_message(channel_id, user_id, username, "user", message.content)

        async with message.channel.typing():
            # LOAD HISTORY
            context = await build_context(channel_id, user_id, username)
            print(f"🧠 Context size: {len(context)} messages")

            # GET AI RESPONSE
            reply = await ask_ai(context)

            # SAVE BOT RESPONSE
            await save_message(channel_id, user_id, "Fur Bot 🐾", "assistant", reply)

            # SEND RESPONSE
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i+1900])
                
            print(f"✓ Response sent\n")

    except Exception as e:
        print(f"❌ ERROR: {traceback.format_exc()}")
        await message.channel.send("mrrp~ something broke 🥺")


# ================= READY =================
@bot.event
async def on_ready():
    """Bot startup"""
    global bot_owner_id
    await init_db()
    app = await bot.application_info()
    bot_owner_id = app.owner.id
    print(f"\n✓ Bot ready 🐾 | {bot.user}")
    print(f"✓ Owner: {bot_owner_id}")
    print(f"✓ Model: {MODEL}\n")


# ================= START =================
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n🐾 Shutting down...")
