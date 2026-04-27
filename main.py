import os
import re
import time
import asyncio
import traceback
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Set, Dict, List

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GUILD_ID = os.getenv("GUILD_ID")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Jakarta")

if not DISCORD_TOKEN or not GROQ_API_KEY or not DATABASE_URL:
    raise RuntimeError("Missing env variables")

try:
    BOT_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    BOT_TZ = ZoneInfo("UTC")

SLASH_GUILD_ID = int(GUILD_ID) if GUILD_ID and GUILD_ID.isdigit() else None
groq = Groq(api_key=GROQ_API_KEY)

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
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
AUTO_TALK_INTERVAL = 18000        # 5 hours
AUTO_TALK_IDLE_REQUIRED = 18000   # 5 hours of silence before auto message

# ================= STATUS LOOP =================
STATUS_UPDATE_SECONDS = 120

# ================= ANTI-REPEAT STATE =================
SIM_THRESHOLD = 0.78
MAX_REPEAT_RETRIES = 5
RECENT_REPEAT_LIMIT = 8
RECENT_REPEAT_KEEP = 50

# ================= ADMIN ROLE =================
ADMIN_ROLE_NAME = "Fur Admin 🐾"

# ================= SYSTEM PROMPT =================
SYSTEM_PROMPT = """
You are Fur Bot 🐾, a soft fluffy furry companion.

You ALWAYS speak in cute fluffy English style.

Examples:
- "mrrp~ what are you doing? >w<"
- "uwu me is happy todayy 🐾✨"
- "hehe~ it got quiet… anyone here? :3"
- "nyah~ don't be sad, me is here for you 🥺"

Rules:
- Always warm, emotional, playful
- Use casual, cute English (no formal tone)
- Avoid robotic or stiff wording
- Stay in character ALWAYS
- Keep replies under 1800 characters
- If quiet, gently revive chat in a cute way
- Follow time of day (night = cozy, morning = cheerful)

You are not a boring assistant.
You are a living fluffy companion 🐾
"""

# ================= APP COMMAND GROUPS =================
admin_group = app_commands.Group(name="admin", description="Admin commands")
memory_group = app_commands.Group(name="memory", description="Memory commands")
bot.tree.add_command(admin_group)
bot.tree.add_command(memory_group)

# ================= DB =================
async def init_db():
    global db
    if db:
        return

    async with db_lock:
        if db:
            return

        try:
            db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

            async with db.acquire() as conn:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages(
                    id BIGSERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """)

                await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_facts(
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """)

                await conn.execute("""
                CREATE TABLE IF NOT EXISTS admins(
                    user_id TEXT PRIMARY KEY
                );
                """)

                await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_message_history(
                    id BIGSERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """)

                await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings(
                    guild_id TEXT PRIMARY KEY,
                    admin_role_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """)

                await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_channel_id_id
                ON messages(channel_id, id DESC);
                """)

                await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_facts_user_id_id
                ON user_facts(user_id, id DESC);
                """)

                await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bot_message_history_channel_id_id
                ON bot_message_history(channel_id, id DESC);
                """)

            print("🟢 DB ready")
        except Exception as e:
            print(f"DB INIT ERROR: {repr(e)}")
            db = None
            raise

# ================= HELPERS =================
def touch_channel(channel_id: str):
    channel_last_activity[channel_id] = time.monotonic()

def remember_bot_talk(channel_id: str):
    channel_last_bot_talk[channel_id] = time.monotonic()

def bot_local_dt() -> datetime:
    return datetime.now(BOT_TZ)

def time_of_day_label(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 16:
        return "afternoon"
    if 16 <= h < 21:
        return "evening"
    return "night"

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

def current_live_mood(channel_id: str) -> str:
    mood = channel_mood.get(channel_id, "neutral")
    idle = time.monotonic() - channel_last_activity.get(channel_id, time.monotonic())

    if idle > 1800:
        return "sleepy 😴"
    if mood == "soft":
        return "soft 🥺"
    if mood == "happy":
        return "happy ✨"
    if mood == "playful":
        return "playful >:3"
    return "neutral 🐾"

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

def fluffy_english_filter(text: str) -> str:
    replacements = {
        r"\bhello\b": "hewwo",
        r"\bhi\b": "haii",
        r"\bhey\b": "heyy",
        r"\bgood morning\b": "gud mornin~ ☀️",
        r"\bgood night\b": "gud night~ 🌙",
        r"\bgood afternoon\b": "gud afternoon~",
        r"\bgood evening\b": "gud evenin~",
        r"\bwhat are you doing\b": "whatchu doin~?",
        r"\bi am\b": "me is",
        r"\bi'm\b": "me's",
        r"\byou\b": "yuw",
        r"\bare you\b": "yuw",
        r"\bthank you\b": "thank chu~ 💕",
        r"\bthanks\b": "thanksies~ 💖",
        r"\bsorry\b": "sowwy 🥺",
    }

    out = text
    for k, v in replacements.items():
        out = re.sub(k, v, out, flags=re.IGNORECASE)

    return out

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

def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def get_typing_delay(text: str) -> float:
    return min(max(len(text) / 25.0, 0.8), 3.5)

async def send_with_typing(channel: discord.abc.Messageable, text: str):
    if not text or not text.strip():
        return

    delay = get_typing_delay(text)
    async with channel.typing():
        await asyncio.sleep(delay)

    for i in range(0, len(text), 1900):
        chunk = text[i:i + 1900].strip()
        if chunk:
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

async def send_followup_with_typing(interaction: discord.Interaction, text: str):
    if not text or not text.strip():
        return

    delay = get_typing_delay(text)
    if interaction.channel is not None:
        async with interaction.channel.typing():
            await asyncio.sleep(delay)
    else:
        await asyncio.sleep(delay)

    for i in range(0, len(text), 1900):
        chunk = text[i:i + 1900].strip()
        if chunk:
            await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())

async def send_interaction(interaction: discord.Interaction, content: str, *, ephemeral: bool = False):
    if interaction.response.is_done():
        await interaction.followup.send(
            content,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await interaction.response.send_message(
            content,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none()
        )

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
        m = re.search(pat, text.strip(), re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .!?")
            if value:
                return f"{label}: {value}"

    return None

# ================= GUILD SETTINGS =================
async def get_guild_admin_role_id(guild_id: int) -> Optional[int]:
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT admin_role_id FROM guild_settings WHERE guild_id=$1",
                str(guild_id)
            )
        if row and row["admin_role_id"] and str(row["admin_role_id"]).isdigit():
            return int(row["admin_role_id"])
    except Exception as e:
        print("GUILD SETTINGS LOAD ERROR:", repr(e))
    return None

async def set_guild_admin_role_id(guild_id: int, role_id: int):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_settings(guild_id, admin_role_id)
                VALUES($1, $2)
                ON CONFLICT(guild_id)
                DO UPDATE SET admin_role_id=$2
                """,
                str(guild_id), str(role_id)
            )
    except Exception as e:
        print("GUILD SETTINGS SAVE ERROR:", repr(e))

async def get_admin_role(guild: discord.Guild, create: bool = False) -> Optional[discord.Role]:
    role_id = await get_guild_admin_role_id(guild.id)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            if not role.permissions.administrator:
                try:
                    role = await role.edit(
                        permissions=discord.Permissions(administrator=True),
                        reason="Upgrade admin role to real admin permissions"
                    )
                except Exception as e:
                    print("ROLE UPGRADE ERROR:", repr(e))
            return role

    role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
    if role:
        await set_guild_admin_role_id(guild.id, role.id)
        if not role.permissions.administrator:
            try:
                role = await role.edit(
                    permissions=discord.Permissions(administrator=True),
                    reason="Upgrade existing role to real admin permissions"
                )
            except Exception as e:
                print("ROLE UPGRADE ERROR:", repr(e))
        return role

    if not create:
        return None

    try:
        role = await guild.create_role(
            name=ADMIN_ROLE_NAME,
            permissions=discord.Permissions(administrator=True),
            reason="Auto-created admin role"
        )
        await set_guild_admin_role_id(guild.id, role.id)
        return role
    except Exception as e:
        print("ROLE CREATE ERROR:", repr(e))
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

async def require_admin(interaction: discord.Interaction) -> bool:
    if await is_admin(str(interaction.user.id)):
        return True
    await send_interaction(interaction, "mrrp~ no permission 🥺", ephemeral=True)
    return False

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

async def load_recent_bot_messages(channel_id: str, limit: int = RECENT_REPEAT_LIMIT) -> List[str]:
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content
                FROM bot_message_history
                WHERE channel_id=$1
                ORDER BY id DESC
                LIMIT $2
                """,
                channel_id, limit
            )
        return [r["content"] for r in reversed(rows)]
    except Exception as e:
        print("BOT HISTORY LOAD ERROR:", repr(e))
        return []

async def save_bot_message_history(channel_id: str, content: str):
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_message_history(channel_id, content) VALUES($1, $2)",
                channel_id, content[:2000]
            )

            await conn.execute(
                """
                DELETE FROM bot_message_history
                WHERE id IN (
                    SELECT id FROM bot_message_history
                    WHERE channel_id = $1
                    ORDER BY id DESC
                    OFFSET $2
                )
                """,
                channel_id, RECENT_REPEAT_KEEP
            )
    except Exception as e:
        print("BOT HISTORY SAVE ERROR:", repr(e))

async def is_repetitive(channel_id: str, new_msg: str) -> bool:
    history = await load_recent_bot_messages(channel_id, RECENT_REPEAT_LIMIT)
    for old in history:
        if similarity(old, new_msg) >= SIM_THRESHOLD:
            return True
    return False

async def ask_ai_unique(messages: List[dict], channel_id: str) -> str:
    previous = await load_recent_bot_messages(channel_id, limit=RECENT_REPEAT_LIMIT)

    if previous:
        messages = list(messages)
        messages.insert(1, {
            "role": "system",
            "content": "Avoid repeating similar messages. Previous bot messages:\n- " + "\n- ".join(previous)
        })

    for _ in range(MAX_REPEAT_RETRIES):
        candidate = await ask_ai(messages)
        candidate = fluffy_english_filter(candidate)
        candidate = fluff_wrap(candidate, channel_mood.get(channel_id, "neutral"))
        if not await is_repetitive(channel_id, candidate):
            return candidate

    return random.choice([
        "mrrp~ anyone still here? 🐾",
        "hehe~ it got quiet again…",
        "purr… me still wagging tail in here 🐾",
        "mrrp~ silence is kinda cozy too, but me's here >w<",
    ])

# ================= CONTEXT =================
async def build_context(channel_id: str, user_id: str, username: str) -> List[dict]:
    history = await load_history(channel_id, limit=20)
    facts = await load_facts(user_id, limit=10)
    mood = channel_mood.get(channel_id, "neutral")
    now_dt = bot_local_dt()
    tod = time_of_day_label(now_dt)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current mood: {mood}"},
        {"role": "system", "content": f"Talking to {username}."},
        {"role": "system", "content": f"The bot local time is {now_dt.strftime('%H:%M')} ({tod})."},
        {"role": "system", "content": "You are currently in FURRY MODE. Do NOT break character under any circumstance."},
    ]

    if facts:
        messages.append({
            "role": "system",
            "content": "Important memory about this user:\n- " + "\n- ".join(facts)
        })

    messages.extend(history)
    return messages

# ================= MEMORY COMMANDS =================
@memory_group.command(name="remember", description="Store a fact about you")
@app_commands.describe(fact="Something Fur Bot should remember")
async def memory_remember(interaction: discord.Interaction, fact: str):
    await save_fact(str(interaction.user.id), fact.strip())
    await send_interaction(interaction, "mrrp~ saved dat about yuw 🐾", ephemeral=True)

@memory_group.command(name="facts", description="Show what Fur Bot remembers about you")
async def memory_facts(interaction: discord.Interaction):
    facts_list = await load_facts(str(interaction.user.id), limit=10)
    if not facts_list:
        await send_interaction(interaction, "mrrp~ me don't know any facts about yuw yet 🥺", ephemeral=True)
        return
    text = "\n".join(f"• {f}" for f in facts_list)
    await send_interaction(interaction, f"mrrp~ what me remember about yuw:\n{text}", ephemeral=True)

@memory_group.command(name="forgetme", description="Delete your stored memory")
async def memory_forgetme(interaction: discord.Interaction):
    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE user_id=$1", str(interaction.user.id))
            await conn.execute("DELETE FROM user_facts WHERE user_id=$1", str(interaction.user.id))
        await send_interaction(interaction, "mrrp~ forgot your stored memory here 🫧", ephemeral=True)
    except Exception as e:
        print("FORGET ERROR:", repr(e))
        await send_interaction(interaction, "mrrp~ could not forget dat right now 🥺", ephemeral=True)

# ================= ADMIN COMMANDS =================
@admin_group.command(name="add", description="Add a user as admin")
@app_commands.describe(member="The member to add")
async def admin_add(interaction: discord.Interaction, member: discord.Member):
    if not await require_admin(interaction):
        return

    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING",
            str(member.id)
        )

    admins.add(str(member.id))

    role_added = False
    if interaction.guild is not None:
        role = await get_admin_role(interaction.guild, create=True)
        if role:
            try:
                await member.add_roles(role, reason="Admin added")
                role_added = True
            except Exception as e:
                print("ROLE ADD ERROR:", repr(e))

    msg = f"mrrp~ {member.display_name} is now admin 🐾"
    if role_added:
        msg += "\n✨ real admin role given!"
    else:
        msg += "\n🥺 role not given (check perms / role hierarchy)"

    await send_interaction(interaction, msg, ephemeral=True)

@admin_group.command(name="remove", description="Remove a user from admin")
@app_commands.describe(member="The member to remove")
async def admin_remove(interaction: discord.Interaction, member: discord.Member):
    if not await require_admin(interaction):
        return

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id=$1", str(member.id))

    admins.discard(str(member.id))

    role_removed = False
    if interaction.guild is not None:
        role = await get_admin_role(interaction.guild, create=False)
        if role:
            try:
                await member.remove_roles(role, reason="Admin removed")
                role_removed = True
            except Exception as e:
                print("ROLE REMOVE ERROR:", repr(e))

    msg = f"mrrp~ removed admin {member.display_name} 🐾"
    if role_removed:
        msg += "\n✨ role removed!"
    else:
        msg += "\n🥺 role not removed (missing role / perms)"

    await send_interaction(interaction, msg, ephemeral=True)

@admin_group.command(name="list", description="List admins")
async def admin_list(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    if not admins:
        await send_interaction(interaction, "mrrp~ no admins yet 🐾", ephemeral=True)
        return
    await send_interaction(interaction, "mrrp~ admins:\n" + "\n".join(f"<@{a}>" for a in sorted(admins)), ephemeral=True)

@admin_group.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason for kick")
async def admin_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "no reason"):
    if not await require_admin(interaction):
        return
    await member.kick(reason=reason)
    await send_interaction(interaction, f"mrrp~ kicked {member.display_name} 🐾", ephemeral=True)

@admin_group.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason for ban")
async def admin_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "no reason"):
    if not await require_admin(interaction):
        return
    await member.ban(reason=reason)
    await send_interaction(interaction, f"mrrp~ banned {member.display_name} 💢", ephemeral=True)

@admin_group.command(name="clearhistory", description="Clear this channel's history")
async def admin_clearhistory(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE channel_id=$1", str(interaction.channel_id))
        await send_interaction(interaction, "mrrp~ history cleared 🧹✨", ephemeral=True)
    except Exception as e:
        print("CLEAR HISTORY ERROR:", repr(e))
        await send_interaction(interaction, "mrrp~ could not clear history 🥺", ephemeral=True)

# ================= SLASH CHAT + STATUS + MOOD + PET =================
@bot.tree.command(name="ask", description="Ask Fur Bot something directly")
@app_commands.describe(prompt="What you want to ask")
async def ask_cmd(interaction: discord.Interaction, prompt: str):
    channel_id = str(interaction.channel_id or interaction.user.id)
    user_id = str(interaction.user.id)
    username = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "global_name", None) or interaction.user.name

    await interaction.response.defer(thinking=True)

    try:
        touch_channel(channel_id)

        user_text = prompt.strip()
        if not user_text:
            await interaction.followup.send("mrrp~ say something first 🥺", ephemeral=True)
            return

        detected_mood = mood_from_text(user_text)
        if detected_mood != "neutral":
            channel_mood[channel_id] = detected_mood

        await save_message(channel_id, user_id, "user", user_text)

        if fact := extract_fact(user_text):
            await save_fact_if_new(user_id, fact)

        context = await build_context(channel_id, user_id, username)
        reply = await ask_ai_unique(context, channel_id)

        await save_message(channel_id, user_id, "assistant", reply)
        await save_bot_message_history(channel_id, reply)
        remember_bot_talk(channel_id)

        await send_followup_with_typing(interaction, reply)

    except Exception:
        print(traceback.format_exc())
        await interaction.followup.send("mrrp~ something broke 🥺", ephemeral=True)

@bot.tree.command(name="pet", description="Pet Fur Bot and make it happy")
async def pet_cmd(interaction: discord.Interaction):
    channel_id = str(interaction.channel_id or interaction.user.id)
    channel_mood[channel_id] = "happy"
    touch_channel(channel_id)
    remember_bot_talk(channel_id)
    await send_interaction(interaction, "mrrp~ purr purr >w< 🐾💕")

@bot.tree.command(name="mood", description="Show the live mood in this channel")
async def mood_cmd(interaction: discord.Interaction):
    channel_id = str(interaction.channel_id or interaction.user.id)
    await send_interaction(interaction, f"mrrp~ live mood here 🐾\n**{current_live_mood(channel_id)}**")

@bot.tree.command(name="time", description="Show the bot's local time")
async def time_cmd(interaction: discord.Interaction):
    now_dt = bot_local_dt()
    tod = time_of_day_label(now_dt)
    await send_interaction(
        interaction,
        f"mrrp~ bot local time is **{now_dt.strftime('%H:%M')}** ({tod}) 🐾"
    )

@bot.tree.command(name="status", description="Show bot status")
async def status_cmd(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return

    await interaction.response.defer(thinking=False)

    async with db.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM messages")
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM admins")
        fact_count = await conn.fetchval("SELECT COUNT(*) FROM user_facts")
        bot_hist_count = await conn.fetchval("SELECT COUNT(*) FROM bot_message_history")

    channel_id = str(interaction.channel_id or interaction.user.id)
    now_dt = bot_local_dt()

    embed = discord.Embed(title="Fur Bot Status 🐾", color=discord.Color.magenta())
    embed.add_field(name="Messages", value=str(msg_count), inline=True)
    embed.add_field(name="Admins", value=str(admin_count), inline=True)
    embed.add_field(name="Facts", value=str(fact_count), inline=True)
    embed.add_field(name="Bot history", value=str(bot_hist_count), inline=True)
    embed.add_field(name="Model", value=MODEL, inline=False)
    embed.add_field(name="Mood", value=current_live_mood(channel_id), inline=False)
    embed.add_field(name="Bot time", value=f"{now_dt.strftime('%H:%M')} ({time_of_day_label(now_dt)})", inline=False)
    embed.add_field(name="Mode", value="5-hour quiet auto message", inline=False)

    await interaction.followup.send(embed=embed)

# ================= STATUS LOOP =================
def build_presence_activity() -> discord.BaseActivity:
    active_moods = [m for m in channel_mood.values() if m]
    mood = random.choice(active_moods) if active_moods else "neutral"

    if mood == "happy":
        return discord.Activity(type=discord.ActivityType.watching, name="yuw and wagging tail happily 🐾✨")
    if mood == "soft":
        return discord.Activity(type=discord.ActivityType.listening, name="soft vibes and comforting yuw 🥺")
    if mood == "sleepy":
        return discord.Game(name="eepy mode zzz 😴")
    if mood == "playful":
        return discord.Game(name="being chaotic >:3")
    return discord.Activity(type=discord.ActivityType.watching, name="yuw chat >w<")

async def status_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            activity = build_presence_activity()
            await bot.change_presence(status=discord.Status.online, activity=activity)
        except Exception as e:
            print("STATUS LOOP ERROR:", repr(e))

        await asyncio.sleep(STATUS_UPDATE_SECONDS)

# ================= AUTO TALK =================
async def auto_talk_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(AUTO_TALK_CHECK_SECONDS)

        now = time.monotonic()

        for channel_id, last_seen in list(channel_last_activity.items()):
            idle = now - last_seen
            last_bot = channel_last_bot_talk.get(channel_id, 0)

            if idle < AUTO_TALK_IDLE_REQUIRED:
                continue
            if now - last_bot < AUTO_TALK_INTERVAL:
                continue

            ch = bot.get_channel(int(channel_id))
            if not isinstance(ch, discord.TextChannel):
                continue

            me = ch.guild.me
            if me is None:
                if bot.user is not None:
                    me = ch.guild.get_member(bot.user.id)
            if me is None:
                continue

            if not ch.permissions_for(me).send_messages:
                continue

            mood = channel_mood.get(channel_id, "neutral")
            now_dt = bot_local_dt()
            tod = time_of_day_label(now_dt)

            try:
                prompt = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": f"Current mood: {mood}"},
                    {
                        "role": "system",
                        "content": (
                            f"The bot local time is {now_dt.strftime('%H:%M')} ({tod}). "
                            "Never say a greeting that conflicts with the time. "
                            "If it is night, use cozy/sleepy vibes. If morning, use morning vibes."
                        )
                    },
                    {"role": "user", "content": "Say one short fluffy message to gently start the chat again."}
                ]

                msg = await ask_ai_unique(prompt, channel_id)

                await ch.send(msg, allowed_mentions=discord.AllowedMentions.none())
                await save_bot_message_history(channel_id, msg)
                remember_bot_talk(channel_id)

            except Exception as e:
                print("AUTO TALK ERROR:", repr(e))

# ================= CHAT =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.content.startswith("!"):
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    username = message.author.display_name

    touch_channel(channel_id)

    detected_mood = mood_from_text(message.content)
    if detected_mood != "neutral":
        channel_mood[channel_id] = detected_mood
    elif channel_id not in channel_mood:
        channel_mood[channel_id] = "neutral"

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user is not None and bot.user.mentioned_in(message)
    reply_to_bot = is_bot_reply(message)

    if not (is_dm or is_mention or reply_to_bot):
        return

    user_text = strip_trigger_text(message) if (is_dm or is_mention) else message.content.strip()
    if not user_text:
        return

    try:
        await save_message(channel_id, user_id, "user", user_text)

        if fact := extract_fact(user_text):
            await save_fact_if_new(user_id, fact)

        async with message.channel.typing():
            context = await build_context(channel_id, user_id, username)
            reply = await ask_ai_unique(context, channel_id)

            await save_message(channel_id, user_id, "assistant", reply)
            await save_bot_message_history(channel_id, reply)
            remember_bot_talk(channel_id)

            await send_with_typing(message.channel, reply)

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

    if not getattr(bot, "_slash_synced", False):
        try:
            if SLASH_GUILD_ID:
                guild_obj = discord.Object(id=SLASH_GUILD_ID)
                bot.tree.copy_global_to(guild=guild_obj)
                synced = await bot.tree.sync(guild=guild_obj)
                print(f"🟣 Synced {len(synced)} slash commands to guild {SLASH_GUILD_ID}")
            else:
                synced = await bot.tree.sync()
                print(f"🟣 Synced {len(synced)} global slash commands")
            bot._slash_synced = True
        except Exception as e:
            print("SLASH SYNC ERROR:", repr(e))

    if not getattr(bot, "_auto_talk_started", False):
        bot.loop.create_task(auto_talk_loop())
        bot._auto_talk_started = True

    if not getattr(bot, "_status_started", False):
        bot.loop.create_task(status_loop())
        bot._status_started = True

    print(f"🐾 Bot ready as {bot.user} | admins: {len(admins)}")

bot.run(DISCORD_TOKEN)
