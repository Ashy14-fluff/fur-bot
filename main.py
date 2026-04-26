import os
import re
import time
import asyncio
import random
import traceback
from typing import Optional, Set, Dict, List

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env variables")

groq = Groq(api_key=GROQ_API_KEY)

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None
db_lock = asyncio.Lock()

admins: Set[str] = set()
bot_owner_id: Optional[int] = None

# ================= ALIVE MODE STATE =================
channel_last_activity: Dict[str, float] = {}
channel_last_bot_talk: Dict[str, float] = {}
channel_mood: Dict[str, str] = {}

AUTO_TALK_CHECK_SECONDS = 60
AUTO_TALK_IDLE_SECONDS = 600          # 10 min quiet before auto-talk
AUTO_TALK_BOT_COOLDOWN = 900          # don't auto-talk too often
AUTO_TALK_PROBABILITY = 0.18          # chance per check if a channel is eligible

SPONTANEOUS_REPLY_PROB = 0.10         # reply sometimes in active chat even without mention
SPONTANEOUS_IDLE_WINDOW = 900         # only when chat is still active within 15 min
SPONTANEOUS_BOT_COOLDOWN = 240        # don't interrupt too often

# ================= SYSTEM PROMPT =================
SYSTEM_PROMPT = """
You are Fur Bot 🐾, a soft fluffy furry companion.

You ALWAYS speak in cute furry style:
mrrp~, >w<, uwu, owo, hehe~, rawr~, purr~, nyah~

Rules:
- Always warm, emotional, playful
- Never sound robotic
- Stay in character ALWAYS
- Remember conversation context
- Be cute, soft, and alive-feeling
- Keep replies under 1800 characters
- Use the current mood naturally in your tone
- If the chat is quiet, you may gently revive it in a short fluffy way

You are not a boring assistant.
You are a living fluffy companion 🐾
"""

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with db_lock:
        if db:
            return

        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        async with db.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts(
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT,
                fact TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins(
                user_id TEXT PRIMARY KEY
            );
            """)

        print("🟢 DB ready")


# ================= HELPERS =================
def touch_channel(channel_id: str):
    channel_last_activity[channel_id] = time.monotonic()


def remember_bot_talk(channel_id: str):
    channel_last_bot_talk[channel_id] = time.monotonic()


def mood_from_text(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["sad", "cry", "lonely", "hurt", "bad", "upset"]):
        return "soft"
    if any(w in t for w in ["happy", "yay", "love", "good", "nice", "cute"]):
        return "happy"
    if any(w in t for w in ["sleep", "tired", "zzz", "eep"]):
        return "sleepy"
    if any(w in t for w in ["wow", "omg", "haha", "lol", "hehe"]):
        return "playful"
    return "neutral"


def fluff_wrap(reply: str, mood: str) -> str:
    if not reply:
        return reply

    lower = reply.lower()
    fluffy_starts = ("mrrp", "uwu", "owo", "hehe", "rawr", "purr", "nyah", ">w<")
    if lower.startswith(fluffy_starts):
        return reply

    prefix = {
        "happy": "mrrp~",
        "soft": "mrrp...",
        "sleepy": "mrrp... eepy",
        "playful": "hehe~",
        "neutral": "mrrp~",
    }.get(mood, "mrrp~")

    suffix = {
        "happy": " 🐾✨",
        "soft": " 🥺🐾",
        "sleepy": " zzz 🐾",
        "playful": " >w< 💖",
        "neutral": " 🐾",
    }.get(mood, " 🐾")

    return f"{prefix} {reply}{suffix}"


def strip_trigger_text(message: discord.Message) -> str:
    text = message.content
    if bot.user:
        text = text.replace(f"<@{bot.user.id}>", "")
        text = text.replace(f"<@!{bot.user.id}>", "")
    return text.strip()


def is_bot_reply(message: discord.Message) -> bool:
    if not message.reference or not message.reference.resolved:
        return False
    resolved = message.reference.resolved
    author = getattr(resolved, "author", None)
    return bool(author and bot.user and author.id == bot.user.id)


# ================= MEMORY =================
async def save_message(channel_id: str, user_id: str, role: str, content: str):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(channel_id,user_id,role,content) VALUES($1,$2,$3,$4)",
                channel_id, user_id, role, content[:2000]
            )
    except Exception as e:
        print("DB SAVE ERROR:", repr(e))


async def load_history(channel_id: str, limit: int = 20) -> List[dict]:
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content
                FROM messages
                WHERE channel_id=$1
                ORDER BY id DESC
                LIMIT $2
                """,
                channel_id, limit
            )
        rows = list(reversed(rows))
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print("DB LOAD ERROR:", repr(e))
        return []


async def load_facts(user_id: str, limit: int = 10) -> List[str]:
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT fact
                FROM user_facts
                WHERE user_id=$1
                ORDER BY id DESC
                LIMIT $2
                """,
                user_id, limit
            )
        return [r["fact"] for r in reversed(rows)]
    except Exception as e:
        print("FACT LOAD ERROR:", repr(e))
        return []


async def save_fact(user_id: str, fact: str):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_facts(user_id,fact) VALUES($1,$2)",
                user_id, fact[:500]
            )
    except Exception as e:
        print("FACT SAVE ERROR:", repr(e))


async def save_fact_if_new(user_id: str, fact: str):
    if not fact:
        return
    existing = await load_facts(user_id, limit=30)
    norm = fact.lower().strip()
    if any(norm == x.lower().strip() for x in existing):
        return
    await save_fact(user_id, fact)


def extract_fact(text: str) -> Optional[str]:
    t = text.strip()
    low = t.lower()

    patterns = [
        (r"\bmy name is\s+(.+)$", "name"),
        (r"\bcall me\s+(.+)$", "name"),
        (r"\bi like\s+(.+)$", "likes"),
        (r"\bi love\s+(.+)$", "loves"),
        (r"\bi hate\s+(.+)$", "hates"),
        (r"\bi'm\s+(.+)$", "is"),
        (r"\bi am\s+(.+)$", "is"),
    ]

    for pat, label in patterns:
        m = re.search(pat, low, re.IGNORECASE)
        if m:
            value = t[m.start(1):].strip(" .!?")
            if value:
                return f"{label}: {value}"

    return None


# ================= ADMIN =================
async def is_admin(user_id: str):
    return user_id in admins or (bot_owner_id is not None and int(user_id) == bot_owner_id)


async def load_admins():
    global admins
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        admins = {r["user_id"] for r in rows}
    except Exception as e:
        print("ADMIN LOAD ERROR:", repr(e))
        admins = set()


# ================= AI =================
async def ask_ai(messages: List[dict]) -> str:
    try:
        def run():
            return groq.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.95,
                max_tokens=700,
            ).choices[0].message.content

        result = await asyncio.wait_for(asyncio.to_thread(run), timeout=30)
        if not result or not result.strip():
            return "mrrp~ empty brain moment 🥺"
        return result.strip()

    except asyncio.TimeoutError:
        return "mrrp… took too long 🥺"
    except Exception as e:
        print("GROQ ERROR:", repr(e))
        return "mrrp~ something broke 🥺"


# ================= CONTEXT =================
async def build_context(channel_id: str, user_id: str, username: str, spontaneous: bool = False) -> List[dict]:
    history = await load_history(channel_id, limit=20)
    facts = await load_facts(user_id, limit=10)
    mood = channel_mood.get(channel_id, "neutral")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current mood: {mood}"},
        {"role": "system", "content": f"Talking to {username}."},
        {"role": "system", "content": "You are currently in FURRY MODE. Do NOT break character under any circumstance."},
    ]

    if facts:
        messages.append({
            "role": "system",
            "content": "Important memory about this user:\n- " + "\n- ".join(facts)
        })

    if spontaneous:
        messages.append({
            "role": "system",
            "content": "You started the message because the chat is quiet. Keep it short, cute, and natural."
        })

    messages.extend(history)
    return messages


# ================= MEMORY COMMANDS =================
@bot.command()
async def remember(ctx, *, fact: str):
    await save_fact(str(ctx.author.id), fact)
    await ctx.send("mrrp~ saved dat about yuw 🐾")


@bot.command()
async def facts(ctx):
    facts_list = await load_facts(str(ctx.author.id), limit=10)
    if not facts_list:
        return await ctx.send("mrrp~ me don’t know any facts about yuw yet 🥺")
    text = "\n".join(f"• {f}" for f in facts_list)
    await ctx.send(f"mrrp~ what me remember about yuw:\n{text}")


@bot.command()
async def forgetme(ctx):
    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE user_id=$1", str(ctx.author.id))
            await conn.execute("DELETE FROM user_facts WHERE user_id=$1", str(ctx.author.id))
        await ctx.send("mrrp~ forgot your stored memory here 🫧")
    except Exception as e:
        print("FORGET ERROR:", repr(e))
        await ctx.send("mrrp~ could not forget dat right now 🥺")


# ================= ADMIN COMMANDS =================
@bot.command()
async def addadmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            str(member.id)
        )

    admins.add(str(member.id))
    await ctx.send(f"mrrp~ {member.display_name} is now admin 🐾")


@bot.command()
async def deladmin(ctx, member: discord.Member):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))
    await ctx.send(f"mrrp~ removed admin {member.display_name} 🐾")


@bot.command()
async def listadmins(ctx):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")
    if not admins:
        return await ctx.send("mrrp~ no admins yet 🐾")
    await ctx.send("mrrp~ admins:\n" + "\n".join(f"<@{a}>" for a in sorted(admins)))


@bot.command()
async def kick(ctx, member: discord.Member, *, reason: str = "no reason"):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")
    await member.kick(reason=reason)
    await ctx.send(f"mrrp~ kicked {member.display_name} 🐾")


@bot.command()
async def ban(ctx, member: discord.Member, *, reason: str = "no reason"):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")
    await member.ban(reason=reason)
    await ctx.send(f"mrrp~ banned {member.display_name} 💢")


@bot.command()
async def clearhistory(ctx):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")
    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE channel_id=$1", str(ctx.channel.id))
        await ctx.send("mrrp~ history cleared 🧹✨")
    except Exception as e:
        print("CLEAR HISTORY ERROR:", repr(e))
        await ctx.send("mrrp~ could not clear history 🥺")


@bot.command()
async def status(ctx):
    if not await is_admin(str(ctx.author.id)):
        return await ctx.send("mrrp~ no permission 🥺")

    async with db.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM messages")
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM admins")
        fact_count = await conn.fetchval("SELECT COUNT(*) FROM user_facts")

    embed = discord.Embed(title="Fur Bot Status 🐾", color=discord.Color.magenta())
    embed.add_field(name="Messages", value=str(msg_count), inline=True)
    embed.add_field(name="Admins", value=str(admin_count), inline=True)
    embed.add_field(name="Facts", value=str(fact_count), inline=True)
    embed.add_field(name="Model", value=MODEL, inline=False)
    embed.add_field(name="Mood", value=channel_mood.get(str(ctx.channel.id), "neutral"), inline=False)
    await ctx.send(embed=embed)


# ================= AUTO TALK =================
async def auto_talk_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(AUTO_TALK_CHECK_SECONDS)

        now = time.monotonic()
        candidates = []

        for channel_id, last_seen in list(channel_last_activity.items()):
            idle = now - last_seen
            bot_idle = now - channel_last_bot_talk.get(channel_id, 0)

            if idle < AUTO_TALK_IDLE_SECONDS:
                continue
            if bot_idle < AUTO_TALK_BOT_COOLDOWN:
                continue

            ch = bot.get_channel(int(channel_id))
            if not isinstance(ch, discord.TextChannel):
                continue

            me = ch.guild.me
            if me is None and bot.user is not None:
                me = ch.guild.get_member(bot.user.id)
            if me is None:
                continue

            if not ch.permissions_for(me).send_messages:
                continue

            candidates.append(ch)

        if not candidates:
            continue

        if random.random() > AUTO_TALK_PROBABILITY:
            continue

        channel = random.choice(candidates)
        channel_id = str(channel.id)
        mood = channel_mood.get(channel_id, "neutral")

        try:
            prompt = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": f"Current mood: {mood}"},
                {"role": "system", "content": "You are reviving a quiet Discord chat. Keep it short, cute, and natural."},
                {"role": "user", "content": "Say one short fluffy message to gently start the chat again."}
            ]

            msg = await ask_ai(prompt)
            msg = fluff_wrap(msg, mood)

            await channel.send(msg, allowed_mentions=discord.AllowedMentions.none())
            remember_bot_talk(channel_id)

        except Exception as e:
            print("AUTO TALK ERROR:", repr(e))


# ================= CHAT =================
@bot.event
async def on_message(message: discord.Message):
    global bot_owner_id

    if message.author.bot:
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    username = message.author.display_name
    now = time.monotonic()

    touch_channel(channel_id)

    detected_mood = mood_from_text(message.content)
    if detected_mood != "neutral":
        channel_mood[channel_id] = detected_mood
    elif channel_id not in channel_mood:
        channel_mood[channel_id] = "neutral"

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user is not None and bot.user.mentioned_in(message)
    reply_to_bot = is_bot_reply(message)

    spontaneous = False
    should_reply = is_dm or is_mention or reply_to_bot

    if not should_reply and not is_dm:
        idle = now - channel_last_activity.get(channel_id, now)
        bot_idle = now - channel_last_bot_talk.get(channel_id, 0)

        if idle < SPONTANEOUS_IDLE_WINDOW and bot_idle > SPONTANEOUS_BOT_COOLDOWN:
            if random.random() < SPONTANEOUS_REPLY_PROB:
                should_reply = True
                spontaneous = True

    if not should_reply:
        return

    user_text = strip_trigger_text(message) if (is_dm or is_mention) else message.content.strip()
    if not user_text:
        return

    try:
        await save_message(channel_id, user_id, "user", user_text)

        if fact := extract_fact(user_text):
            await save_fact_if_new(user_id, fact)

        async with message.channel.typing():
            context = await build_context(channel_id, user_id, username, spontaneous=spontaneous)
            reply = await ask_ai(context)
            reply = fluff_wrap(reply, channel_mood.get(channel_id, "neutral"))

            await save_message(channel_id, user_id, "assistant", reply)
            remember_bot_talk(channel_id)

            for i in range(0, len(reply), 1900):
                chunk = reply[i:i+1900].strip()
                if chunk:
                    await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    except Exception:
        print(traceback.format_exc())
        await message.channel.send("mrrp~ something broke 🥺")


# ================= READY =================
@bot.event
async def on_ready():
    global bot_owner_id
    await init_db()
    await load_admins()

    app_info = await bot.application_info()
    bot_owner_id = app_info.owner.id if app_info.owner else None

    if not getattr(bot, "_auto_talk_started", False):
        bot.loop.create_task(auto_talk_loop())
        bot._auto_talk_started = True

    print(f"🐾 Bot ready as {bot.user} | admin floofs: {len(admins)}")


bot.run(DISCORD_TOKEN)
