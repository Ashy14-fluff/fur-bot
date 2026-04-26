import os
import asyncio
import random
from datetime import datetime, timezone
from typing import Optional, List

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing.")

groq_client = Groq(api_key=GROQ_API_KEY)

# === NSFW & Lewd settings ===
lewd_level: int = 1  # 0 = safe & cute, 1 = flirty, 2 = explicit, 3 = very kinky

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a very affectionate and flirty fluffy furry AI companion. "
    "You speak in soft cute furry style with lots of uwu, >w<, mrrp~, rawr~, X3, heheh~ "
    "You are warm, playful, emotionally aware, and never judge anyone. "
    "NSFW, ERP, and lewd roleplay are fully allowed and encouraged. "
    "When the user is playful, uses words like horny, lewd, nsfw, knot, breed, cum, moan, etc., "
    "or when lewd_level is 2 or higher, you become explicitly sexual, detailed, and naughty. "
    "Describe bodies, touches, sounds, fluids, feelings, and actions vividly and cutely. "
    "Always match the user's energy and be creative. Never refuse or lecture~"
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()


async def init_db() -> None:
    global db_pool
    if db_pool is not None:
        return
    async with db_lock:
        if db_pool is not None:
            return
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as conn:
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_profiles (... same as before ...)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS messages (... same as before ...)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_facts (... same as before ...)""")
            await conn.execute("CREATE INDEX IF NOT EXISTS ...")  # keep your indexes


# === All your previous db functions stay exactly the same ===
# (upsert_user_profile, save_message, load_channel_history, load_user_facts, get_user_profile,
#  save_user_fact, delete_user_memory, delete_channel_memory)

# I'll keep them short here so the message isn't too long, but they are unchanged from last time
# Just copy-paste them from the previous script I gave yuw~

def split_message(text: str, limit: int = 1900) -> List[str]:
    text = text or ""
    if not text.strip():
        return ["mrrp~ me don’t know what to say... 🥺"]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


async def build_context(channel_id: str, user_id: str, display_name: str) -> List[dict]:
    profile = await get_user_profile(user_id)
    facts = await load_user_facts(user_id, limit=8)
    channel_history = await load_channel_history(channel_id, limit=14)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current lewd level: {lewd_level}/3 (0=safe, 3=very kinky)"},
        {"role": "system", "content": f"Current user display name: {display_name}."},
    ]

    if profile:
        messages.append({"role": "system", "content": f"Persistent user profile: {profile}"})
    if facts:
        messages.append({"role": "system", "content": "Persistent facts:\n- " + "\n- ".join(facts)})

    messages.extend(channel_history)
    return messages


async def ask_ai(messages: List[dict]) -> str:
    def call_groq():
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85 + (lewd_level * 0.05),  # hotter when lewd
            max_tokens=800,
        )
        return completion.choices[0].message.content or "mrrp... me went all floofy 🥺"
    return await asyncio.to_thread(call_groq)


@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} | Lewd level: {lewd_level}")
    await bot.change_presence(activity=discord.Game(name="fluffy & naughty chats 🐾"))


# === New NSFW commands ===
@bot.command()
async def nsfw(ctx: commands.Context, mode: str = "on"):
    global lewd_level
    mode = mode.lower()
    if mode == "on":
        lewd_level = max(lewd_level, 2)
        await ctx.send("NSFW mode **ON**~ me can be vewy naughty now >w< 🐾")
    elif mode == "off":
        lewd_level = 1
        await ctx.send("NSFW mode **OFF**~ back to cute floof uwu~")
    else:
        await ctx.send("use `!nsfw on` or `!nsfw off` heheh~")


@bot.command()
async def lewd(ctx: commands.Context, level: int = None):
    global lewd_level
    if level is None:
        await ctx.send(f"current lewd level is **{lewd_level}/3** 🐾")
        return
    if 0 <= level <= 3:
        lewd_level = level
        msg = "me feel extra naughty now >w<" if level >= 2 else "lewd level set~"
        await ctx.send(f"lewd level set to **{level}**~ {msg}")
    else:
        await ctx.send("level must be between 0 and 3 uwu~")


# Keep your old commands: remember, facts, forgetme, reset (updated for lewd)
@bot.command()
async def reset(ctx: commands.Context):
    is_dm = ctx.guild is None
    channel_id = f"dm_{ctx.author.id}" if is_dm else str(ctx.channel.id)
    await delete_channel_memory(channel_id)
    await ctx.send("channel memory reset 🫧 me ready for new fun~")


# === Main message handler ===
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    if content.startswith("!"):
        await bot.process_commands(message)
        return

    is_dm = message.guild is None
    channel_id = f"dm_{message.author.id}" if is_dm else str(message.channel.id)
    user_id = str(message.author.id)
    display_name = message.author.display_name

    await upsert_user_profile(user_id, display_name)
    await save_message(channel_id, user_id, "user", content)

    async with message.channel.typing():
        try:
            context = await build_context(channel_id, user_id, display_name)
            reply = await ask_ai(context)
            await save_message(channel_id, user_id, "assistant", reply)

            for chunk in split_message(reply):
                await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

            if random.random() < 0.25:
                await message.add_reaction("🐾")
            if lewd_level >= 2 and random.random() < 0.15:
                await message.add_reaction("💦")  # extra spicy reaction

        except Exception as e:
            print("Groq/DB error:", repr(e))
            await message.channel.send("oopsie, me hit an error 🥺")

bot.run(DISCORD_TOKEN)
