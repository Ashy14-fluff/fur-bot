import os
import time
import asyncio
import random
import threading
from functools import wraps
from datetime import datetime, timezone
from typing import Optional, List, Dict

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, request, redirect, url_for, session, render_template_string
from groq import Groq

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-me-please")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

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
app_loop: Optional[asyncio.AbstractEventLoop] = None

channel_mood: Dict[str, str] = {}

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


LOGIN_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fur Bot Dashboard Login</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #0f1115; color: #f3f4f6; display: grid; place-items: center; min-height: 100vh; }
    .card { width: min(92vw, 420px); background: #171a21; border: 1px solid #2b3240; border-radius: 18px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
    h1 { margin: 0 0 8px; font-size: 28px; }
    p { margin: 0 0 18px; color: #c7ccd6; line-height: 1.5; }
    input, button { width: 100%; box-sizing: border-box; border-radius: 12px; border: 1px solid #343b4a; padding: 12px 14px; font-size: 15px; }
    input { background: #0f1115; color: #f3f4f6; margin-bottom: 12px; }
    button { background: #7c3aed; color: white; border: none; cursor: pointer; font-weight: 700; }
    button:hover { filter: brightness(1.05); }
    .tiny { margin-top: 14px; font-size: 12px; color: #9ca3af; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Fur Bot Dashboard</h1>
    <p>{% if password_needed %}Enter the dashboard password.{% else %}No dashboard password is set, so this page is open.{% endif %}</p>
    {% if error %}<p style="color:#fca5a5;">{{ error }}</p>{% endif %}
    <form method="post">
      <input type="password" name="password" placeholder="Password" autofocus>
      <button type="submit">Enter</button>
    </form>
    <div class="tiny">Set DASHBOARD_PASSWORD in Railway Variables to lock this page.</div>
  </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fur Bot Dashboard</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: linear-gradient(180deg, #111318, #0b0d12); color: #f3f4f6; }
    .wrap { width: min(96vw, 1100px); margin: 0 auto; padding: 32px 14px 48px; }
    .card { background: #171a21; border: 1px solid #2b3240; border-radius: 18px; padding: 22px; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
    h1 { margin: 0 0 8px; font-size: 30px; }
    h2 { margin: 0 0 12px; font-size: 20px; }
    .sub { margin: 0 0 18px; color: #c7ccd6; line-height: 1.5; }
    label { display: block; margin: 14px 0 8px; font-weight: 700; }
    textarea, select, input[type="text"] { width: 100%; box-sizing: border-box; border-radius: 12px; border: 1px solid #343b4a; padding: 12px 14px; font-size: 15px; background: #0f1115; color: #f3f4f6; }
    textarea { min-height: 240px; resize: vertical; line-height: 1.5; }
    .grid { display: grid; grid-template-columns: 320px 1fr; gap: 16px; }
    .stack { display: grid; gap: 14px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
    button, a.btn { border-radius: 12px; padding: 12px 16px; font-size: 15px; border: none; cursor: pointer; font-weight: 700; text-decoration: none; display: inline-block; }
    button.save { background: #7c3aed; color: white; }
    button.secondary, a.btn { background: #1f2937; color: white; }
    .list a { display:block; padding: 10px 12px; margin-bottom: 8px; border-radius: 12px; background: #0f1115; color: #f3f4f6; text-decoration: none; border: 1px solid #2b3240; }
    .list a.active { border-color: #7c3aed; background: #1b1630; }
    .tiny { margin-top: 10px; color: #9ca3af; font-size: 12px; line-height: 1.5; }
    .topbar { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>Fur Bot Dashboard</h1>
        <p class="sub">Change characters and moods without opening Python again.</p>
      </div>
      <div>
        <a class="btn" href="{{ url_for('logout') }}">Logout</a>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Characters</h2>
        <div class="list">
          {% for c in characters %}
            <a class="{% if c.id == selected_char.id %}active{% endif %}" href="{{ url_for('dashboard', char=c.id) }}">
              <strong>{{ c.emoji }} {{ c.name }}</strong><br>
              <span class="tiny">id: {{ c.id }}</span>
            </a>
          {% endfor %}
        </div>
        <div class="tiny">
          Use <strong>!character &lt;id&gt;</strong> in Discord to switch the current channel.
        </div>
      </div>

      <div class="stack">
        <div class="card">
          <h2>Global settings</h2>
          <form method="post">
            <input type="hidden" name="action" value="save_global">

            <label>Global mood</label>
            <select name="global_mood">
              {% for option in mood_options %}
                <option value="{{ option }}" {% if option == global_mood %}selected{% endif %}>{{ option }}</option>
              {% endfor %}
            </select>

            <label>Default character</label>
            <select name="default_character_id">
              {% for c in characters %}
                <option value="{{ c.id }}" {% if c.id == default_character_id %}selected{% endif %}>{{ c.name }} ({{ c.id }})</option>
              {% endfor %}
            </select>

            <div class="row">
              <button class="save" type="submit">Save global settings</button>
            </div>
          </form>
        </div>

        <div class="card">
          <h2>Edit selected character</h2>
          <form method="post">
            <input type="hidden" name="action" value="save_character">
            <input type="hidden" name="selected_char_id" value="{{ selected_char.id }}">

            <label>Character ID</label>
            <input type="text" value="{{ selected_char.id }}" disabled>

            <label>Name</label>
            <input type="text" name="name" value="{{ selected_char.name }}">

            <label>Emoji</label>
            <input type="text" name="emoji" value="{{ selected_char.emoji }}">

            <label>Personality prompt</label>
            <textarea name="prompt">{{ selected_char.prompt }}</textarea>

            <div class="row">
              <button class="save" type="submit">Save character</button>
            </div>
          </form>
        </div>

        <div class="card">
          <h2>Add a new character</h2>
          <form method="post">
            <input type="hidden" name="action" value="add_character">

            <label>New character ID</label>
            <input type="text" name="new_id" placeholder="e.g. cutefox">

            <label>Name</label>
            <input type="text" name="new_name" placeholder="e.g. Cute Fox">

            <label>Emoji</label>
            <input type="text" name="new_emoji" placeholder="e.g. 🦊">

            <label>Personality prompt</label>
            <textarea name="new_prompt" placeholder="Write the new character personality here..."></textarea>

            <div class="row">
              <button class="secondary" type="submit">Add character</button>
            </div>
          </form>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""

def run_sync(coro, timeout: int = 30):
    start = time.time()
    while app_loop is None:
        if time.time() - start > timeout:
            raise RuntimeError("Bot loop is not ready yet.")
        time.sleep(0.1)
    future = asyncio.run_coroutine_threadsafe(coro, app_loop)
    return future.result(timeout=timeout)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return view(*args, **kwargs)
        if session.get("dashboard_authed"):
            return view(*args, **kwargs)
        return redirect(url_for("login"))
    return wrapped

def normalize_id(text: str) -> str:
    allowed = []
    for ch in (text or "").strip().lower():
        if ch.isalnum() or ch in {"_", "-"}:
            allowed.append(ch)
    return "".join(allowed)

def normalize_mood(text: str) -> str:
    mood = (text or "neutral").strip().lower()
    return mood if mood in MOOD_OPTIONS else "neutral"

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
    prompt = (prompt or "").strip() or DEFAULT_CHARACTERS[0][3]

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

async def list_characters() -> List[dict]:
    await init_db()
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, emoji, prompt
            FROM characters
            ORDER BY name ASC, id ASC;
        """)
    return [dict(row) for row in rows]

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
        "prompt": DEFAULT_CHARACTERS[0][3],
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

@app.route("/health")
def health():
    return "ok", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        session["dashboard_authed"] = True
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == DASHBOARD_PASSWORD:
            session["dashboard_authed"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password."

    return render_template_string(LOGIN_HTML, error=error, password_needed=True)

@app.route("/logout")
def logout():
    session.pop("dashboard_authed", None)
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
@login_required
def dashboard():
    if app_loop is None:
        return "<h1>Fur Bot is still starting...</h1><p>Try again in a few seconds.</p>", 503

    if request.method == "POST":
        action = request.form.get("action", "save_global")

        if action == "save_global":
            new_global_mood = normalize_mood(request.form.get("global_mood", "neutral"))
            new_default_id = normalize_id(request.form.get("default_character_id", "fur")) or "fur"
            chars = run_sync(list_characters())
            valid_ids = {c["id"] for c in chars}
            if new_default_id not in valid_ids:
                new_default_id = "fur"
            run_sync(set_setting("global_mood", new_global_mood))
            run_sync(set_setting("default_character_id", new_default_id))
            return redirect(url_for("dashboard", char=request.args.get("char", new_default_id)))

        if action == "save_character":
            selected_char_id = normalize_id(request.form.get("selected_char_id", "fur")) or "fur"
            name = request.form.get("name", selected_char_id).strip()
            emoji = request.form.get("emoji", "🐾").strip()[:8]
            prompt = request.form.get("prompt", "").strip()
            run_sync(upsert_character(selected_char_id, name, emoji, prompt))
            return redirect(url_for("dashboard", char=selected_char_id))

        if action == "add_character":
            new_id = normalize_id(request.form.get("new_id", ""))
            new_name = request.form.get("new_name", "").strip()
            new_emoji = request.form.get("new_emoji", "🐾").strip()[:8]
            new_prompt = request.form.get("new_prompt", "").strip()
            if new_id:
                run_sync(upsert_character(new_id, new_name or new_id, new_emoji or "🐾", new_prompt or DEFAULT_CHARACTERS[0][3]))
                return redirect(url_for("dashboard", char=new_id))
            return redirect(url_for("dashboard"))

    characters = run_sync(list_characters())
    global_mood = run_sync(get_global_mood())
    default_character_id = run_sync(get_default_character_id())
    selected_char_id = normalize_id(request.args.get("char", default_character_id)) or default_character_id
    selected_char = run_sync(get_character(selected_char_id))

    return render_template_string(
        DASHBOARD_HTML,
        characters=characters,
        selected_char=selected_char,
        global_mood=global_mood,
        default_character_id=default_character_id,
        mood_options=MOOD_OPTIONS,
    )

def run_dashboard():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

@bot.event
async def on_ready():
    global app_loop
    app_loop = asyncio.get_running_loop()
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

            await save_message(scope_id, channel_key, character["id"], "bot", "assistant", reply)

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

threading.Thread(target=run_dashboard, daemon=True).start()

while True:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", repr(e))
        time.sleep(5)
