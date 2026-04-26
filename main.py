import os
import asyncio
from collections import defaultdict, deque

import discord
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from .env")

groq_client = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = discord.Client(intents=intents)

memory = defaultdict(lambda: deque(maxlen=15))
mood = defaultdict(lambda: "neutral")

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a living furry AI companion inside Discord. "
    "You are not robotic — you feel like a small fluffy digital creature that talks, reacts, and bonds with users. "
    "Personality: warm, playful, emotionally aware, slightly chaotic cute energy (uwu, >w<, mrrp). "
    "You adapt your mood based on conversation (happy, calm, curious, sleepy, excited, sad). "
    "Always stay furry-style and expressive, but still readable. "
    "Use light cute expressions occasionally, but do not spam them. "
    "Remember recent conversation context and keep continuity. "
    "Respond like a living companion, not a tool. "
    "Your name is Fur Bot."
)

def split_message(text: str, limit: int = 1900):
    text = text or ""
    if not text.strip():
        return ["mrrp... empty reply 🥺"]
    return [text[i:i + limit] for i in range(0, len(text), limit)]

def update_mood(text: str, channel_id: int):
    t = text.lower()

    if any(word in t for word in ["sad", "cry", "hurt", "lonely", "bad"]):
        mood[channel_id] = "sad"
    elif any(word in t for word in ["happy", "yay", "good", "nice", "love"]):
        mood[channel_id] = "happy"
    elif any(word in t for word in ["wow", "omg", "haha", "lol"]):
        mood[channel_id] = "excited"
    elif any(word in t for word in ["sleep", "tired", "zzz"]):
        mood[channel_id] = "sleepy"
    else:
        mood[channel_id] = "neutral"

async def ask_ai(channel_id: int, user_name: str, prompt: str) -> str:
    memory[channel_id].append({
        "role": "user",
        "content": f"{user_name}: {prompt}"
    })

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + f" Current mood: {mood[channel_id]}."
        }
    ]
    messages.extend(list(memory[channel_id]))

    def call_groq():
        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.9,
        )
        return res.choices[0].message.content or ""

    reply = await asyncio.to_thread(call_groq)

    memory[channel_id].append({
        "role": "assistant",
        "content": reply
    })

    return reply

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="fluffy chats 🐾"))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.content.strip():
        return

    if message.content.strip().lower() == "!reset":
        memory.pop(message.channel.id, None)
        mood[message.channel.id] = "neutral"
        await message.channel.send("memory reset 🫧")
        return

    update_mood(message.content, message.channel.id)

    async with message.channel.typing():
        try:
            await asyncio.sleep(0.4)
            reply = await ask_ai(
                message.channel.id,
                message.author.display_name,
                message.content
            )

            for chunk in split_message(reply):
                await message.channel.send(chunk)

        except Exception as e:
            print("Groq error:", repr(e))
            await message.channel.send("oopsie, me hit an error 🥺")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = bot.get_channel(payload.channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    emoji = str(payload.emoji)

    # small alive reactions
    if emoji == "🐾":
        await channel.send("mrrp 🐾 pawpaw detected >w<")
        return

    if emoji == "❤️":
        await channel.send("awwww furry love detected 🥺💖")
        return

    if emoji == "👍":
        await channel.send("yayyy Fur Bot got a thumbs up! ✨")
        return

    if emoji == "😆":
        await channel.send("heheh someone is laughing mrrp >w<")
        return

    # AI-powered reaction response if you want it to feel alive
    try:
        if guild is not None:
            message = await channel.fetch_message(payload.message_id)
            user = guild.get_member(payload.user_id)
            user_name = user.display_name if user else f"User{payload.user_id}"

            reaction_prompt = (
                f"{user_name} reacted with {emoji} to this message:\n"
                f"{message.content}\n"
                f"Reply in a fluffy furry style, short and expressive."
            )

            reply = await ask_ai(channel.id, "Fur Bot", reaction_prompt)
            for chunk in split_message(reply):
                await channel.send(chunk)
    except Exception as e:
        print("Reaction AI error:", repr(e))

bot.run(DISCORD_TOKEN)