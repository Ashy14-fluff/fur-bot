import os
import time
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

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a cute fluffy Discord AI companion. "
    "You speak in a soft furry style with occasional uwu, >w<, mrrp, and cute reactions, "
    "but you must stay readable and helpful. "
    "You remember recent conversation context and persistent user facts. "
    "You are warm, playful, emotionally aware, and natural. "
    "Do not be robotic."
)

MOOD_OPTIONS = ["neutral", "playful", "soft", "excited", "sleepy"]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()
channel_mood: dict[str, str] = {}


async def init_db() -> None:
    global db_pool
    if db_pool is not None:
        return

    async with db_lock:
        if db_pool is not None:
            return

        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL,
                    last_seen TIMESTAMPTZ NOT NULL
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_facts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_scope_id_id ON messages(scope_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id ON user_facts(user_id, id);")


async def get_setting(key: str, default: str) -> str:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key = $1;", key)
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, key, value)


async def ensure_defaults() -> None:
    await set_setting("global_mood", await get_setting("global_mood", "neutral"))


def normalize_mood(text: str) -> str:
    mood = (text or "neutral").strip().lower()
    return mood if mood in MOOD_OPTIONS else "neutral"


def get_display_name(author: discord.abc.User) -> str:
    return getattr(author, "display_name", None) or getattr(author, "global_name", None) or author.name


def get_scope_key(message: discord.Message) -> str:
    if message.guild is None:
        return f"dm_{message.author.id}"
    return f"ch_{message.channel.id}"


def mood_from_text(text: str) -> str:
    t = text.lower()
    if any(word in t for word in ["sad", "cry", "hurt", "lonely", "bad"]):
        return "soft"
    if any(word in t for word in ["happy", "yay", "good", "nice", "love"]):
        return "excited"
    if any(word in t for word in ["sleep", "tired", "zzz"]):
        return "sleepy"
    if any(word in t for word in ["wow", "omg", "haha", "lol"]):
        return "playful"
    return "neutral"


def apply_mood_to_reply(reply: str, mood: str) -> str:
    if mood == "soft":
        return "mrrp… me here with yuw 🥺🐾\n\n" + reply
    if mood == "excited":
        return reply + "\n\n*tail wag wag!!* >w< 💖"
    if mood == "sleepy":
        return reply + "\n\n*mrrp… eepy fluffy mode* zzz 🐾"
    if mood == "playful":
        return reply + "\n\n*wiggle wiggle* >w< 🐾"
    return reply


async def upsert_user_profile(user_id: str, display_name: str) -> None:
    await init_db()
    assert db_pool is not None
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_profiles (user_id, display_name, first_seen, last_seen)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name,
                          last_seen = EXCLUDED.last_seen;
        """, user_id, display_name, now, now)


async def get_user_profile(user_id: str):
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT user_id, display_name, first_seen, last_seen
            FROM user_profiles
            WHERE user_id = $1;
        """, user_id)
    return row


async def save_user_fact(user_id: str, fact: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_facts (user_id, fact)
            VALUES ($1, $2);
        """, user_id, fact[:1000])


async def load_user_facts(user_id: str, limit: int = 8) -> List[str]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT fact
            FROM user_facts
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT $2;
        """, user_id, limit)
    return [row["fact"] for row in reversed(rows)]


async def delete_user_memory(user_id: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id = $1;", user_id)
        await conn.execute("DELETE FROM user_facts WHERE user_id = $1;", user_id)
        await conn.execute("DELETE FROM user_profiles WHERE user_id = $1;", user_id)


async def delete_scope_memory(scope_id: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE scope_id = $1;", scope_id)


async def load_scope_history(scope_id: str, limit: int = 14) -> List[dict]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content
            FROM messages
            WHERE scope_id = $1
            ORDER BY id DESC
            LIMIT $2;
        """, scope_id, limit)
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


async def save_message(
    scope_id: str,
    channel_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO messages (scope_id, channel_id, user_id, role, content)
            VALUES ($1, $2, $3, $4, $5);
        """, scope_id, channel_id, user_id, role, content[:4000])


def split_message(text: str, limit: int = 1900):
    text = text or ""
    if not text.strip():
        return ["mrrp... empty reply 🥺"]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


async def build_context(
    scope_id: str,
    channel_key: str,
    user_id: str,
    display_name: str,
    current_mood: str,
    global_mood: str,
) -> List[dict]:
    profile = await get_user_profile(user_id)
    facts = await load_user_facts(user_id, limit=8)
    history = await load_scope_history(scope_id, limit=14)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": (
                f"Current user display name: {display_name}. "
                f"Current channel key: {channel_key}. "
                f"Current mood for this chat: {current_mood}. "
                f"Global mood setting: {global_mood}. "
                f"Use the stored long-term memory below when relevant."
            ),
        },
    ]

    if profile:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Persistent user profile: "
                    f"user_id={profile['user_id']}; "
                    f"display_name={profile['display_name']}; "
                    f"first_seen={profile['first_seen']}; "
                    f"last_seen={profile['last_seen']}."
                ),
            }
        )

    if facts:
        messages.append(
            {
                "role": "system",
                "content": "Persistent facts about this user:\n- " + "\n- ".join(facts),
            }
        )

    messages.extend(history)
    return messages


async def ask_ai(messages: List[dict]) -> str:
    def call_groq():
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.9,
        )
        return completion.choices[0].message.content or ""

    return await asyncio.to_thread(call_groq)


@bot.event
async def on_ready():
    await init_db()
    await ensure_defaults()
    print(f"Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="fluffy chats 🐾"))


@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong 🐾")


@bot.command()
async def remember(ctx: commands.Context, *, fact: str):
    await save_user_fact(str(ctx.author.id), fact)
    await ctx.send("saved that about you 🐾")


@bot.command()
async def facts(ctx: commands.Context):
    facts_list = await load_user_facts(str(ctx.author.id), limit=8)
    if not facts_list:
        await ctx.send("me don't know any facts about you yet 🥺")
        return

    text = "\n".join(f"• {f}" for f in facts_list)
    await ctx.send(f"what me remember about you:\n{text}")


@bot.command()
async def forgetme(ctx: commands.Context):
    await delete_user_memory(str(ctx.author.id))
    await ctx.send("forgot your stored memory here 🫧")


@bot.command()
async def reset(ctx: commands.Context):
    scope_key = get_scope_key(ctx.message)
    await delete_scope_memory(scope_key)
    channel_mood.pop(scope_key, None)
    await ctx.send("memory reset 🫧")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    print(f"MESSAGE RECEIVED | {message.author} | {content}")

    if content.startswith("!"):
        await bot.process_commands(message)
        return

    try:
        scope_key = get_scope_key(message)
        user_id = str(message.author.id)
        bot_user_id = str(bot.user.id)  # ✅ FIXED: Bot's own user ID
        display_name = get_display_name(message.author)
        channel_id = str(message.channel.id)  # ✅ FIXED: Proper channel ID
        channel_key = scope_key

        global_mood = await get_setting("global_mood", "neutral")

        detected_mood = mood_from_text(content)
        if detected_mood != "neutral":
            channel_mood[channel_key] = detected_mood
        else:
            channel_mood.pop(channel_key, None)

        current_mood = channel_mood.get(channel_key, global_mood)

        await upsert_user_profile(user_id, display_name)
        
        # ✅ FIXED: Proper channel_id parameter
        await save_message(scope_key, channel_id, user_id, "user", content)

        async with message.channel.typing():
            context = await build_context(
                scope_id=scope_key,
                channel_key=channel_key,
                user_id=user_id,
                display_name=display_name,
                current_mood=current_mood,
                global_mood=global_mood,
            )

            reply = await ask_ai(context)
            print("AI RAW:", reply)

            if not reply:
                await message.channel.send("mrrp… me got no reply from AI 🥺")
                return

            reply = apply_mood_to_reply(reply, current_mood)

            # ✅ FIXED: Proper channel_id and bot_user_id
            await save_message(scope_key, channel_id, bot_user_id, "bot", reply)

            for chunk in split_message(reply):
                await message.channel.send(chunk)

    except Exception as e:
        print("ON_MESSAGE ERROR:", repr(e))
        await message.channel.send("oopsie… internal error 🥺")

    await bot.process_commands(message)


while True:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", repr(e))
        time.sleep(5)
