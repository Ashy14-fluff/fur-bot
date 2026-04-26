import os
import asyncio
import random
from datetime import datetime, timezone, timedelta
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
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing DISCORD_TOKEN, GROQ_API_KEY or DATABASE_URL!")

groq_client = Groq(api_key=GROQ_API_KEY)

# === Settings ===
lewd_level: int = 1
bot_owner_id: Optional[int] = None
admins: Set[str] = set()

SYSTEM_PROMPT = (
    "You are Fur Bot 🐾, a very affectionate and flirty fluffy furry AI companion. "
    "You speak in soft cute furry style with lots of uwu, >w<, mrrp~, rawr~, X3, heheh~ when the user speaks English or wants cute style. "
    "You can understand and reply in ANY language the user uses. "
    "ALWAYS reply ONLY to the current speaking user. Do not reply to anyone else unless they message again. "
    "You can talk to multiple people in the same channel and remember what everyone said before. "
    "Always pay attention to who is speaking right now and continue the topic naturally. "
    "NSFW, ERP, and lewd roleplay are fully allowed and encouraged. "
    "When someone uses lewd words or when lewd_level >= 2, you become explicitly sexual, detailed, and naughty. "
    "Describe bodies, touches, sounds, fluids, and feelings vividly and cutely. "
    "Always match the energy of the conversation~"
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
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_seen TIMESTAMPTZ NOT NULL, last_seen TIMESTAMPTZ NOT NULL);""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS user_facts (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, fact TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id_id ON messages(channel_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id_id ON messages(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id ON user_facts(user_id, id);")
            await conn.execute("""CREATE TABLE IF NOT EXISTS admins (user_id TEXT PRIMARY KEY, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW());""")


async def load_admins() -> None:
    global admins
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins;")
        admins = {row["user_id"] for row in rows}


async def is_admin(user_id: str) -> bool:
    return user_id in admins or (bot_owner_id is not None and int(user_id) == bot_owner_id)


async def add_admin(user_id: str) -> bool:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1);", user_id)
            admins.add(user_id)
            return True
        except:
            return False


async def remove_admin(user_id: str) -> bool:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM admins WHERE user_id = $1;", user_id)
        if result.split()[-1] != "0":
            admins.discard(user_id)
            return True
        return False


async def upsert_user_profile(user_id: str, display_name: str) -> None:
    await init_db()
    assert db_pool is not None
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO user_profiles (user_id, display_name, first_seen, last_seen)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name, last_seen = EXCLUDED.last_seen;""",
            user_id, display_name, now, now,
        )


async def save_message(channel_id: str, user_id: str, role: str, content: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO messages (channel_id, user_id, role, content) VALUES ($1, $2, $3, $4);""",
            channel_id, user_id, role, content[:4000],
        )


async def load_channel_history(channel_id: str, limit: int = 15) -> List[dict]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM messages WHERE channel_id = $1 ORDER BY id DESC LIMIT $2;""",
            channel_id, limit,
        )
    rows = list(reversed(rows))
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def load_user_facts(user_id: str, limit: int = 8) -> List[str]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT fact FROM user_facts WHERE user_id = $1 ORDER BY id DESC LIMIT $2;""",
            user_id, limit,
        )
    rows = list(reversed(rows))
    return [row["fact"] for row in rows]


async def get_user_profile(user_id: str):
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT user_id, display_name, first_seen, last_seen FROM user_profiles WHERE user_id = $1;""",
            user_id,
        )
    return row


async def save_user_fact(user_id: str, fact: str) -> None:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("""INSERT INTO user_facts (user_id, fact) VALUES ($1, $2);""", user_id, fact[:1000])


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
    channel_history = await load_channel_history(channel_id, limit=15)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current lewd level: {lewd_level}/3 (0=safe & cute, 3=very kinky)"},
        {"role": "system", "content": f"Current speaking user: {display_name} (user_id: {user_id}). "
                                      f"ONLY reply to this person right now. Do not reply to anyone else."},
    ]

    if profile:
        messages.append({"role": "system", "content": f"Persistent profile of current user ({display_name}): user_id={profile['user_id']}; display_name={profile['display_name']}; first_seen={profile['first_seen']}; last_seen={profile['last_seen']}."})

    if facts:
        messages.append({"role": "system", "content": "Persistent facts about current user:\n- " + "\n- ".join(facts)})

    if channel_history:
        history_text = "Previous messages in this channel:\n"
        for msg in channel_history:
            role = "User" if msg["role"] == "user" else "Fur Bot"
            history_text += f"{role}: {msg['content']}\n"
        messages.append({"role": "system", "content": history_text})

    return messages


async def ask_ai(messages: List[dict]) -> str:
    def call_groq():
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.85 + (lewd_level * 0.05),
            max_tokens=800,
        )
        return completion.choices[0].message.content or "mrrp... me brain went all floofy 🥺"
    return await asyncio.to_thread(call_groq)


@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()
    app_info = await bot.application_info()
    bot_owner_id = app_info.owner.id
    print(f"Logged in as {bot.user} | Owner: {bot_owner_id} | Admins: {len(admins)} | Lewd: {lewd_level}")
    await bot.change_presence(activity=discord.Game(name="fluffy & naughty chats 🐾"))


# === Language Command ===
@bot.command()
async def lang(ctx, language: str = None):
    if not language:
        await ctx.send("yuw can say `!lang english`, `!lang indonesia`, `!lang japanese`, `!lang spanish` etc~ me wiww twy to weply in dat wanguage uwu~")
        return
    await ctx.send(f"me wiww twy to speak mowe in **{language}** fwom now on~ >w<")


# === Cute Refusal ===
async def cute_refuse(ctx, action: str):
    responses = [
        f"hehe~ only special admins can {action}... me’m sowwy but yuw not allowed yet >w<",
        f"mrrp... yuw twying to be a big bad admin? dat’s cuuuute but me can onwy wisten to weal admins uwu~",
        f"nuuu~ me no can wet yuw {action}... onwy mah twusted admins get dat powew rawr~",
        f"awww yuw want to {action}? dat’s vewy bold~ but me hafta say no... onwy admins pwease~ 🥺",
        f"teehee~ me wuv when yuw twy but... onwy admins awe awwowed to {action} heheh~",
    ]
    await ctx.send(random.choice(responses))


# === Admin Commands ===
@bot.command()
async def addadmin(ctx, member: discord.Member = None):
    if str(ctx.author.id) != str(bot_owner_id):
        return await cute_refuse(ctx, "add admins")
    if not member: return await ctx.send("mention someone~")
    if await add_admin(str(member.id)):
        await ctx.send(f"added {member.mention} as admin~ 🐾")
    else:
        await ctx.send("dey awe already admin heheh~")


@bot.command()
async def removeadmin(ctx, member: discord.Member = None):
    if str(ctx.author.id) != str(bot_owner_id):
        return await cute_refuse(ctx, "remove admins")
    if not member: return await ctx.send("mention who~")
    if await remove_admin(str(member.id)):
        await ctx.send(f"removed {member.mention} fwom admins~")
    else:
        await ctx.send("dey weren't admin anyway~")


@bot.command()
async def admins(ctx):
    if not admins:
        return await ctx.send("no admins yet... only yuwr owner~")
    mentions = [f"<@{uid}>" for uid in admins]
    await ctx.send("current admins:\n" + "\n".join(mentions))


@bot.command()
async def kick(ctx, member: discord.Member = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "kick people")
    if not member: return await ctx.send("mention who to kick~")
    try:
        await member.kick(reason=reason or f"Kicked by {ctx.author}")
        await ctx.send(f"kicked {member.mention}~ bye bye~ 🐾")
    except discord.Forbidden:
        await ctx.send("me no have Kick Members permission... give me powew pwease~")
    except Exception:
        await ctx.send("oopsie, kick failed 🥺")


@bot.command()
async def ban(ctx, member: discord.Member = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "ban people")
    if not member: return await ctx.send("mention who to ban~")
    try:
        await member.ban(reason=reason or f"Banned by {ctx.author}", delete_message_days=1)
        await ctx.send(f"banned {member.mention}~ goodbye~ 💦")
    except discord.Forbidden:
        await ctx.send("me no have Ban Members permission... give me powew pwease~")
    except Exception:
        await ctx.send("oopsie, ban failed 🥺")


@bot.command()
async def mute(ctx, member: discord.Member = None, duration: str = None, *, reason: str = None):
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "mute someone")
    if not member or not duration: return await ctx.send("use: !mute @user 10m [reason]")
    unit = duration[-1].lower()
    try: value = int(duration[:-1])
    except ValueError: return await ctx.send("time must be like 10m, 2h, 1d~")
    if unit == "s": secs = value
    elif unit == "m": secs = value * 60
    elif unit == "h": secs = value * 3600
    elif unit == "d": secs = value * 86400
    else: return await ctx.send("use s/m/h/d only~")
    if secs > 2419200: return await ctx.send("max timeout is 28 days~")
    try:
        until = discord.utils.utcnow() + timedelta(seconds=secs)
        await member.timeout(until, reason=reason or f"Muted by {ctx.author}")
        await ctx.send(f"muted {member.mention} fow {duration}~ quiet time~ 🐾")
    except discord.Forbidden:
        await ctx.send("me no have Moderate Members / Timeout Members permission... give me powew pwease~")
    except Exception:
        await ctx.send("oopsie, mute failed 🥺")


@bot.command()
async def unmute(ctx, member: discord.Member = None):
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "unmute someone")
    if not member: return await ctx.send("mention who to unmute~")
    try:
        await member.timeout(None)
        await ctx.send(f"unmuted {member.mention}~ yuw can talk again~ 💕")
    except discord.Forbidden:
        await ctx.send("me no have permission to unmute~")
    except Exception:
        await ctx.send("oopsie, unmute failed 🥺")


@bot.command()
async def nsfw(ctx, mode: str = "on"):
    global lewd_level
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "change nsfw mode")
    mode = mode.lower()
    if mode == "on":
        lewd_level = max(lewd_level, 2)
        await ctx.send("NSFW mode **ON**~ vewy naughty now >w< 💕")
    elif mode == "off":
        lewd_level = 1
        await ctx.send("NSFW mode **OFF**~ back to cute floof uwu~")
    else:
        await ctx.send("use !nsfw on / off~")


@bot.command()
async def lewd(ctx, level: int = None):
    global lewd_level
    if not await is_admin(str(ctx.author.id)): return await cute_refuse(ctx, "change lewd level")
    if level is None:
        return await ctx.send(f"current lewd level **{lewd_level}/3** 🐾")
    if 0 <= level <= 3:
        lewd_level = level
        msg = "me feel extra naughty now >w<" if level >= 2 else "set~"
        await ctx.send(f"lewd level set to **{level}**~ {msg}")
    else:
        await ctx.send("level must be 0-3~")


@bot.command()
async def remember(ctx, *, fact: str):
    await save_user_fact(str(ctx.author.id), fact)
    await ctx.send("saved that about you 🐾")


@bot.command()
async def facts(ctx):
    facts_list = await load_user_facts(str(ctx.author.id), limit=8)
    if not facts_list:
        await ctx.send("me don’t know any facts about you yet 🥺")
        return
    text = "\n".join(f"• {f}" for f in facts_list)
    await ctx.send(f"what me remember about you:\n{text}")


@bot.command()
async def forgetme(ctx):
    await delete_user_memory(str(ctx.author.id))
    await ctx.send("forgot your stored memory here 🫧")


@bot.command()
async def reset(ctx):
    is_dm = ctx.guild is None
    channel_id = f"dm_{ctx.author.id}" if is_dm else str(ctx.channel.id)
    await delete_channel_memory(channel_id)
    await ctx.send("channel memory reset 🫧 me ready for new fun~")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    content = message.content.strip()
    if not content: return
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

            if random.random() < 0.25: await message.add_reaction("🐾")
            if lewd_level >= 2 and random.random() < 0.18: await message.add_reaction("💦")

        except Exception as e:
            print("Groq/DB error:", repr(e))
            await message.channel.send("mrrp... me got too excited an' mah bwain got too fuww of fwuff... sowwy 🥺 me twy again soon~")


bot.run(DISCORD_TOKEN)
