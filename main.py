import os
import asyncio
import traceback
from typing import List

import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncpg
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# ✅ FIXED: VALID GROQ MODELS ONLY
GROQ_MODEL = "llama3-70b-8192"  # This WORKS 100%

if not all([DISCORD_TOKEN, GROQ_API_KEY, DATABASE_URL]):
    raise RuntimeError("Missing env vars!")

print(f"✅ Using model: {GROQ_MODEL}")

groq = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = "You are Fur Bot 🐾, cute fluffy Discord companion. Speak softly with uwu/mrrp. Be helpful!"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db = None


async def init_db():
    global db
    if db:
        return
    db = await asyncpg.create_pool(DATABASE_URL)
    
    async with db.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            id BIGSERIAL PRIMARY KEY, channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)


async def save_message(channel_id: str, user_id: str, role: str, content: str):
    if not db: return
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
            channel_id, user_id, role, content[:1500]
        )


async def load_history(channel_id: str, limit=8):
    if not db: return []
    async with db.acquire() as c:
        rows = await c.fetch(
            "SELECT role, content FROM messages WHERE channel_id=$1 ORDER BY id DESC LIMIT $2",
            channel_id, limit
        )
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


async def ask_groq(messages: List[dict]):
    """Simple reliable Groq call"""
    completion = await asyncio.to_thread(
        lambda: groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=1000
        ).choices[0].message.content or "mrrp..."
    )
    return completion


@bot.event
async def on_ready():
    await init_db()
    print(f"✅ {bot.user} ready!")


@bot.command()
async def ping(ctx): await ctx.send("pong 🐾")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.content.strip():
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    bot_id = str(bot.user.id)

    print(f"🤖 {message.author}: {message.content}")

    try:
        history = await load_history(channel_id)
        
        ai_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history[-6:],  # Last 6 messages only
            {"role": "user", "content": message.content}
        ]

        reply = await ask_groq(ai_messages)
        
        # Save conversation
        await save_message(channel_id, user_id, "user", message.content)
        await save_message(channel_id, bot_id, "assistant", reply)
        
        # Send (split if too long)
        for chunk in [reply[i:i+1900] for i in range(0, len(reply), 1900)]:
            await message.channel.send(chunk)

    except Exception as e:
        print(f"❌ Error: {e}")
        await message.channel.send("mrrp… AI hiccup 🥺")

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
