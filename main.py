import discord
from discord.ext import commands
import json
import os
import random

# ===== CONFIG =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy AI. "
    "You speak in a playful, warm furry style (uwu, >w<, mrrp) but stay readable. "
    "You remember conversations and respond naturally."
)

# ===== MEMORY SYSTEM =====
MEMORY_FILE = "memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_memory(data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

memory = load_memory()

# ===== DISCORD SETUP =====
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== FAKE AI (replace with your Groq call) =====
def ask_ai(messages):
    # REPLACE THIS WITH YOUR GROQ API
    last_user = messages[-1]["content"]
    return f"mrrp~ yuw said: {last_user} >w<"

# ===== EVENTS =====
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = str(message.channel.id)
    user_name = message.author.display_name

    # init memory
    if channel_id not in memory:
        memory[channel_id] = []

    # save user message
    memory[channel_id].append({
        "role": "user",
        "content": f"{user_name}: {message.content}"
    })

    # limit memory (important)
    memory[channel_id] = memory[channel_id][-15:]

    # build messages for AI
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(memory[channel_id])

    # get AI reply
    reply = ask_ai(messages)

    # save bot reply
    memory[channel_id].append({
        "role": "assistant",
        "content": reply
    })

    save_memory(memory)

    # send reply
    await message.channel.send(reply)

    # cute reactions
    if random.random() < 0.2:
        await message.add_reaction("🐾")

    if random.random() < 0.2:
        await message.channel.send(random.choice([
            "*tail wag*",
            "*mrrp purr*",
            "*ear twitch*"
        ]))

    await bot.process_commands(message)

# ===== AUTO RESTART LOOP =====
while True:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", e)
