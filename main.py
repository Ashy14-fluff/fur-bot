import os
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
import asyncpg
from groq import Groq

load_dotenv()

# REQUIRED ENV VARS
TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")
DB_URL = os.getenv("DATABASE_URL")

if not all([TOKEN, GROQ_KEY, DB_URL]):
    print("❌ MISSING ENV VARS!")
    exit(1)

print("✅ All env vars OK")

# ✅ HARDCODED WORKING MODEL - NO .env OVERRIDE
client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None

async def setup_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_URL)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT NOW()
            )
        ''')

SYSTEM_PROMPT = "You are Fur Bot 🐾! Cute, friendly, fluffy AI. Use uwu/mrrp sometimes. Be helpful!"

@bot.event
async def on_ready():
    await setup_db()
    print(f'✅ {bot.user} is online! 🐾')

@bot.command()
async def ping(ctx):
    await ctx.send('Pong! 🐾')

async def chat_ai(messages):
    """Direct Groq call with WORKING model"""
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="llama3-70b-8192",  # ✅ KNOWN WORKING
            messages=messages,
            temperature=0.7,
            max_tokens=800
        )
        return response.choices[0].message.content or "mrrp?"
    except Exception as e:
        print(f"Groq error: {e}")
        return "mrrp… AI nap time 🥺"

async def get_history(channel_id, limit=6):
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT role, content FROM chat WHERE channel_id=$1 ORDER BY timestamp DESC LIMIT $2',
            channel_id, limit
        )
    return [{'role': r['role'], 'content': r['content']} for r in reversed(rows)]

async def save_chat(channel_id, user_id, role, content):
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO chat (channel_id, user_id, role, content) VALUES ($1, $2, $3, $4)',
            channel_id, user_id, role, content[:1000]
        )

@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.content.strip():
        return
    
    if msg.content.startswith('!'):
        await bot.process_commands(msg)
        return
    
    print(f"🐾 {msg.author}: {msg.content}")
    
    try:
        # Get recent chat
        history = await get_history(str(msg.channel.id))
        
        # Build prompt
        ai_msgs = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            *history,
            {'role': 'user', 'content': msg.content}
        ]
        
        # Get AI reply
        reply = await chat_ai(ai_msgs)
        
        # Save conversation
        await save_chat(str(msg.channel.id), str(msg.author.id), 'user', msg.content)
        await save_chat(str(msg.channel.id), str(bot.user.id), 'assistant', reply)
        
        # Send reply
        await msg.reply(reply)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        await msg.reply("mrrp… technical difficulties 🥺")
    
    await bot.process_commands(msg)

if __name__ == "__main__":
    bot.run(TOKEN)
