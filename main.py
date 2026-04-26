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

# === Lewd settings ===
lewd_level: int = 1  # 0 = safe & cute, 1 = flirty, 2 = explicit, 3 = very kinky

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a very affectionate and flirty fluffy furry AI companion. "
    "You speak in soft cute furry style with lots of uwu, >w<, mrrp~, rawr~, X3, heheh~ "
    "You are warm, playful, emotionally aware, and never judge. "
    "NSFW, ERP, and lewd roleplay are fully allowed and encouraged. "
    "When the user is playful, uses words like lewd, horny, nsfw, knot, breed, cum, moan, etc., "
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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL,
                    last_seen TIMESTAMPTZ NOT NULL
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_facts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_channel_id_id ON messages(channel_id, id);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id ON user_facts(user_id, id);"
            )


async def upsert_user_profile(user_id: str, display_name: str) -> None:
    await init_db()
    assert db_pool is not None
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_profiles (user_id, display_name, first_seen, last_seen)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name,
                          last_seen = EXCLUDED.last_seen;
            """,
            user_id, display_name, now, now,
        )


async def save_message(channel_id: str, user_id: str, role: str, content: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (channel_id, user_id, role, content)
            VALUES ($1, $2, $3, $4);
            """,
            channel_id, user_id, role, content[:4000],
        )


async def load_channel_history(channel_id: str, limit: int = 14) -> List[dict]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content
            FROM messages
            WHERE channel_id = $1
            ORDER BY id DESC
            LIMIT $2;
            """,
            channel_id, limit,
        )
    rows = list(reversed(rows))
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def load_user_facts(user_id: str, limit: int = 8) -> List[str]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT fact
            FROM user_facts
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT $2;
            """,
            user_id, limit,
        )
    rows = list(reversed(rows))
    return [row["fact"] for row in rows]


async def get_user_profile(user_id: str):
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, display_name, first_seen, last_seen
            FROM user_profiles
            WHERE user_id = $1;
            """,
            user_id,
        )
    return row


async def save_user_fact(user_id: str, fact: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_facts (user_id, fact)
            VALUES ($1, $2);
            """,
            user_id, fact[:1000],
        )


async def delete_user_memory(user_id: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id = $1;", user_id)
        await conn.execute("DELETE FROM user_facts WHERE user_id = $1;", user_id)
        await conn.execute("DELETE FROM user_profiles WHERE user_id = $1;", user_id)


async def delete_channel_memory(channel_id: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE channel_id = $1;", channel_id)


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
        {"role": "system", "content": f"Current lewd level: {lewd_level}/3 (0=safe & cute, 3=very kinky)"},
        {"role": "system", "content": f"Current user display name: {display_name}."},
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

    messages.extend(channel_history)
    return messages


async def ask_ai(messages: List[dict]) -> str:
    def call_groq():
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85 + (lewd_level * 0.05),  # gets hotter when lewd
            max_tokens=900,
        )
        return completion.choices[0].message.content or "mrrp... me brain went all floofy 🥺"
    return await asyncio.to_thread(call_groq)


@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} | Lewd level: {lewd_level}")
    await bot.change_presence(activity=discord.Game(name="fluffy & naughty chats 🐾"))


@bot.command()
async def nsfw(ctx: commands.Context, mode: str = "on"):
    global lewd_level
    mode = mode.lower()
    if mode == "on":
        lewd_level = max(lewd_level, 2)
        await ctx.send("NSFW mode **ON**~ me can be vewy naughty now >w< 💕")
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
        await ctx.send("level must be 0-3 uwu~")


@bot.command()
async def remember(ctx: commands.Context, *, fact: str):
    await save_user_fact(str(ctx.author.id), fact)
    await ctx.send("saved that about you 🐾")


@bot.command()
async def facts(ctx: commands.Context):
    facts_list = await load_user_facts(str(ctx.author.id), limit=8)
    if not facts_list:
        await ctx.send("me don’t know any facts about you yet 🥺")
        return
    text = "\n".join(f"• {f}" for f in facts_list)
    await ctx.send(f"what me remember about you:\n{text}")


@bot.command()
async def forgetme(ctx: commands.Context):
    await delete_user_memory(str(ctx.author.id))
    await ctx.send("forgot your stored memory here 🫧")


@bot.command()
async def reset(ctx: commands.Context):
    is_dm = ctx.guild is None
    channel_id = f"dm_{ctx.author.id}" if is_dm else str(ctx.channel.id)
    await delete_channel_memory(channel_id)
    await ctx.send("channel memory reset 🫧 me ready for new fun~")


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
                await message.channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            if random.random() < 0.25:
                await message.add_reaction("🐾")
            if lewd_level >= 2 and random.random() < 0.18:
                await message.add_reaction("💦")

        except Exception as e:
            print("Groq/DB error:", repr(e))
            await message.channel.send("oopsie, me hit an error 🥺")


bot.run(DISCORD_TOKEN)
