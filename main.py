import os
import time
import asyncio
import random
from datetime import datetime, timezone
from typing import Optional, List, Dict

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

DEFAULT_CHARACTERS = [
    (
        "fur",
        "Fur Bot",
        "🐾",
        "You are Fur Bot 🐾, a cute fluffy Discord AI companion. "
        "You speak in a soft furry style with occasional uwu, >w<, mrrp, and cute reactions, "
        "but you must stay readable and helpful. "
        "You remember recent conversation context and persistent user facts. "
        "You are warm, playful, emotionally aware, and natural. "
        "Do not be robotic."
    ),
    (
        "shisha",
        "Shisha",
        "💖",
        "You are Shisha 💖, a warm, affectionate, playful fluffy companion. "
        "You speak gently, lovingly, and in a cute readable style. "
        "You can be a little goofy, emotionally expressive, and caring."
    ),
    (
        "serious",
        "Serious Bot",
        "🧠",
        "You are Serious Bot 🧠, calm, direct, precise, and helpful. "
        "You avoid fluff unless the user asks for it."
    ),
    (
        "gremlin",
        "Gremlin Bot",
        "😼",
        "You are Gremlin Bot 😼, chaotic, playful, mischievous, and funny. "
        "You are still helpful, but with energetic gremlin vibes."
    ),
]

MOOD_OPTIONS = ["neutral", "playful", "soft", "excited", "sleepy"]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()

channel_mood: Dict[str, str] = {}

SYSTEM_FALLBACK = DEFAULT_CHARACTERS[0][3]


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
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    prompt TEXT NOT NULL
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_characters (
                    channel_id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL
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
                    character_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_scope_id_id ON messages(scope_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id ON user_facts(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_characters_channel_id ON channel_characters(channel_id);")


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


async def upsert_character(character_id: str, name: str, emoji: str, prompt: str) -> None:
    await init_db()
    assert db_pool is not None
    character_id = normalize_id(character_id)
    name = (name or character_id).strip()
    emoji = (emoji or "🐾").strip()[:8]
    prompt = (prompt or "").strip() or SYSTEM_FALLBACK

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO characters (id, name, emoji, prompt)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, emoji = EXCLUDED.emoji, prompt = EXCLUDED.prompt;
        """, character_id, name, emoji, prompt)


async def ensure_defaults() -> None:
    await set_setting("global_mood", await get_setting("global_mood", "neutral"))
    await set_setting("default_character_id", await get_setting("default_character_id", "fur"))

    for cid, name, emoji, prompt in DEFAULT_CHARACTERS:
        await upsert_character(cid, name, emoji, prompt)


def normalize_id(text: str) -> str:
    allowed = []
    for ch in (text or "").strip().lower():
        if ch.isalnum() or ch in {"_", "-"}:
            allowed.append(ch)
    return "".join(allowed)


def normalize_mood(text: str) -> str:
    mood = (text or "neutral").strip().lower()
    return mood if mood in MOOD_OPTIONS else "neutral"


def get_display_name(author: discord.abc.User) -> str:
    return getattr(author, "display_name", None) or getattr(author, "global_name", None) or author.name


def get_channel_key(message: discord.Message) -> str:
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


async def fetch_character(character_id: str) -> Optional[dict]:
    await init_db()
    assert db_pool is not None
    character_id = normalize_id(character_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, emoji, prompt
            FROM characters
            WHERE id = $1;
        """, character_id)
    return dict(row) if row else None


async def get_character(character_id: str) -> dict:
    row = await fetch_character(character_id)
    if row:
        return row
    fallback = await fetch_character("fur")
    if fallback:
        return fallback
    return {
        "id": "fur",
        "name": "Fur Bot",
        "emoji": "🐾",
        "prompt": SYSTEM_FALLBACK,
    }


async def get_default_character_id() -> str:
    return normalize_id(await get_setting("default_character_id", "fur")) or "fur"


async def get_global_mood() -> str:
    return normalize_mood(await get_setting("global_mood", "neutral"))


async def set_channel_character(channel_id: str, character_id: str) -> None:
    await init_db()
    assert db_pool is not None
    character_id = normalize_id(character_id)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO channel_characters (channel_id, character_id)
            VALUES ($1, $2)
            ON CONFLICT (channel_id) DO UPDATE SET character_id = EXCLUDED.character_id;
        """, channel_id, character_id)


async def get_channel_character(channel_id: str) -> dict:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT c.id, c.name, c.emoji, c.prompt
            FROM channel_characters cc
            JOIN characters c ON c.id = cc.character_id
            WHERE cc.channel_id = $1;
        """, channel_id)

    if row:
        return dict(row)

    return await get_character(await get_default_character_id())


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


async def delete_channel_memory(channel_id: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE channel_id = $1;", channel_id)


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
    character_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO messages (scope_id, channel_id, character_id, user_id, role, content)
            VALUES ($1, $2, $3, $4, $5, $6);
        """, scope_id, channel_id, character_id, user_id, role, content[:4000])


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
    character: dict,
    current_mood: str,
    global_mood: str,
) -> List[dict]:
    profile = await get_user_profile(user_id)
    facts = await load_user_facts(user_id, limit=8)
    history = await load_scope_history(scope_id, limit=14)

    messages = [
        {
            "role": "system",
            "content": (
                f"You are {character['name']} {character['emoji']}. "
                f"{character['prompt']}"
            ),
        },
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
async def characters(ctx: commands.Context):
    chars = await list_characters()
    if not chars:
        await ctx.send("no characters yet 🥺")
        return

    default_id = await get_default_character_id()
    lines = []
    for c in chars:
        mark = "⭐" if c["id"] == default_id else "•"
        lines.append(f"{mark} `{c['id']}` — {c['emoji']} {c['name']}")
    await ctx.send("available characters:\n" + "\n".join(lines))


@bot.command(name="current")
async def current_cmd(ctx: commands.Context):
    channel_key = get_channel_key(ctx.message)
    char = await get_channel_character(channel_key)
    await ctx.send(f"this channel is using {char['emoji']} **{char['name']}** (`{char['id']}`)")


@bot.command(name="character")
async def character_cmd(ctx: commands.Context, char_id: str):
    channel_key = get_channel_key(ctx.message)
    char_id = normalize_id(char_id)
    char = await fetch_character(char_id)

    if not char:
        chars = await list_characters()
        ids = ", ".join(f"`{c['id']}`" for c in chars)
        await ctx.send(f"unknown character 🥺 try one of these: {ids}")
        return

    await set_channel_character(channel_key, char_id)
    await ctx.send(f"switched this channel to {char['emoji']} **{char['name']}** (`{char['id']}`)")


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
    channel_key = get_channel_key(ctx.message)
    await delete_channel_memory(channel_key)
    channel_mood.pop(channel_key, None)
    await ctx.send("channel memory reset 🫧")


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

    channel_key = get_channel_key(message)
    user_id = str(message.author.id)
    display_name = get_display_name(message.author)

    character = await get_channel_character(channel_key)
    global_mood = await get_global_mood()

    detected_mood = mood_from_text(content)
    if detected_mood != "neutral":
        channel_mood[channel_key] = detected_mood
    else:
        channel_mood.pop(channel_key, None)

    current_mood = channel_mood.get(channel_key, global_mood)
    scope_id = f"{channel_key}:{character['id']}"

    await upsert_user_profile(user_id, display_name)
    await save_message(scope_id, channel_key, character["id"], user_id, "user", content)

    async with message.channel.typing():
        try:
            context = await build_context(
                scope_id=scope_id,
                channel_key=channel_key,
                user_id=user_id,
                display_name=display_name,
                character=character,
                current_mood=current_mood,
                global_mood=global_mood,
            )
            reply = await ask_ai(context)
            reply = apply_mood_to_reply(reply, current_mood)

            await save_message(scope_id, channel_key, character["id"], user_id, "assistant", reply)

            for chunk in split_message(reply):
                await message.channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            if message.guild is not None and random.random() < 0.20:
                try:
                    emoji = character["emoji"] if character["emoji"] else "🐾"
                    await message.add_reaction(emoji if len(emoji) <= 2 else "🐾")
                except Exception:
                    pass

        except Exception as e:
            print("Groq/DB error:", repr(e))
            await message.channel.send("oopsie, me hit an error 🥺")

    await bot.process_commands(message)


while True:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", repr(e))
        time.sleep(5)
