import os
import json
import asyncio
import random
from typing import Dict, List

import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from your environment variables.")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from your environment variables.")

client = Groq(api_key=GROQ_API_KEY)

MEMORY_FILE = "memory.json"
MAX_MESSAGES_PER_CHANNEL = 15

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy Discord AI companion. "
    "You speak in a soft furry style with occasional uwu, >w<, mrrp, and cute reactions, "
    "but you must stay readable and helpful. "
    "You remember the recent conversation in each channel and keep continuity. "
    "You are warm, playful, emotionally aware, and natural. "
    "Do not be robotic."
)

def load_memory() -> Dict[str, List[dict]]:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print("Could not load memory.json:", e)
    return {}

def save_memory(memory: Dict[str, List[dict]]) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Could not save memory.json:", e)

memory = load_memory()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def trim_channel_memory(channel_id: str) -> None:
    if channel_id in memory:
        memory[channel_id] = memory[channel_id][-MAX_MESSAGES_PER_CHANNEL:]

def split_message(text: str, limit: int = 1900):
    text = text or ""
    if not text.strip():
        return ["mrrp... empty reply 🥺"]
    return [text[i:i + limit] for i in range(0, len(text), limit)]

async def ask_ai(channel_id: str, user_name: str, prompt: str) -> str:
    if channel_id not in memory:
        memory[channel_id] = []

    memory[channel_id].append({
        "role": "user",
        "content": f"{user_name}: {prompt}"
    })
    trim_channel_memory(channel_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(memory[channel_id])

    def call_groq():
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.9,
        )
        return completion.choices[0].message.content or ""

    reply = await asyncio.to_thread(call_groq)

    memory[channel_id].append({
        "role": "assistant",
        "content": reply
    })
    trim_channel_memory(channel_id)
    save_memory(memory)

    return reply

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="fluffy chats 🐾"))

@bot.command()
async def reset(ctx: commands.Context):
    channel_id = str(ctx.channel.id)
    memory.pop(channel_id, None)
    save_memory(memory)
    await ctx.send("memory reset 🫧")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    content = message.content.strip()
    if not content:
        return

    if content.lower() == "!reset":
        channel_id = str(message.channel.id)
        memory.pop(channel_id, None)
        save_memory(memory)
        await message.channel.send("memory reset 🫧")
        return

    async with message.channel.typing():
        try:
            reply = await ask_ai(
                str(message.channel.id),
                message.author.display_name,
                content
            )

            for chunk in split_message(reply):
                await message.channel.send(chunk)

            if random.random() < 0.20:
                await message.add_reaction("🐾")

        except Exception as e:
            print("Groq error:", repr(e))
            await message.channel.send("oopsie, me hit an error 🥺")

    await bot.process_commands(message)

while True:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", e)
