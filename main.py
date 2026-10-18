import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

BOT_TOKEN = "8730350349:AAECYS8jv26JtJMAy8DJjMe_TxLjOe3Fj5Q"

ADMIN_ID = 8385172584

ADMIN_IDS = {ADMIN_ID}
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "").strip()
PREMIUM_CONTACT = os.getenv("PREMIUM_CONTACT", "@TURBO_BOT_HOSTER_Owner").strip()

BASE = Path(__file__).resolve().parent
PROJECTS = BASE / "projects"
PROJECTS.mkdir(exist_ok=True)
DB = BASE / "hostbot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("hostbot")

if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN in .env or the hosting environment.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
running = {}  # (user_id, project_id) -> subprocess.Popen

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    premium INTEGER DEFAULT 0,
    premium_until INTEGER DEFAULT 0,
    projects_limit INTEGER DEFAULT 2,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    folder TEXT,
    entry TEXT,
    status TEXT DEFAULT 'stopped',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS user_bans(
    user_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    banned_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
""")
db.commit()
# Upgrade existing databases safely.
columns = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
if "premium_until" not in columns:
    db.execute("ALTER TABLE users ADD COLUMN premium_until INTEGER DEFAULT 0")
    db.commit()
db.execute("UPDATE users SET projects_limit=2 WHERE premium=0 AND projects_limit=1")
db.commit()

# Admin conversations: set premium asks for user ID first, then duration.
admin_states = {}
BAN_SECONDS = 30 * 60
MAX_SCAN_BYTES = 5 * 1024 * 1024
SOURCE_SUFFIXES = {".py", ".js", ".ts", ".mjs", ".cjs"}


def active_ban(uid):
    """Return an active ban row, and automatically clear expired bans."""
    row = db.execute("SELECT * FROM user_bans WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    if int(row["expires_at"]) <= int(time.time()):
        db.execute("DELETE FROM user_bans WHERE user_id=?", (uid,))
        db.commit()
        return None
    return row


def ban_time_text(timestamp):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(timestamp)))


def detect_hosting_bot_text(source):
    """Conservative heuristic: framework + process execution + multiple hosting signals."""
    text = source.lower()
    framework = any(x in text for x in (
        "aiogram", "python-telegram-bot", "telebot", "pyrogram",
        "telegraf", "grammy", "telegram.ext"
    ))
    process_runner = any(x in text for x in (
        "subprocess.popen", "subprocess.run(", "create_subprocess_exec",
        "create_subprocess_shell", "os.system(", "child_process.spawn(",
        "child_process.exec(", "execfile(", "popen(" 
    ))
    signals = [
        any(x in text for x in ("f.document", "message.document", "download_file(", "get_file(", "upload file", "upload_file")),
        any(x in text for x in ("zipfile.zipfile", "extractall(", "adm-zip", "unzipper", "yauzl")),
        any(x in text for x in ("launch_project", "projects_limit", "hosted file", "hosting bot", "host bot admin panel", "run_project", "project_id")),
        any(x in text for x in ("python", "node ", "python3", "entry_file", "entrypoint"))
    ]
    # Requiring several independent indicators reduces false positives for ordinary bots.
    return framework and process_runner and sum(signals) >= 2


def inspect_upload_for_hosting_bot(path, ext):
    """Scan Python/JS source directly or source files inside a ZIP without extracting it."""
    candidates = []
    try:
        if ext in SOURCE_SUFFIXES:
            if path.stat().st_size > MAX_SCAN_BYTES:
                return None
            candidates.append((path.name, path.read_text(encoding="utf-8", errors="ignore")))
        elif ext == ".zip":
            with zipfile.ZipFile(path) as archive:
                total = 0
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    target = Path(member.filename)
                    if target.is_absolute() or ".." in target.parts:
                        continue  # Existing ZIP validation will reject unsafe paths later.
                    if target.suffix.lower() not in SOURCE_SUFFIXES:
                        continue
                    total += member.file_size
                    if total > MAX_SCAN_BYTES or len(candidates) >= 100:
                        break
                    try:
                        with archive.open(member) as f:
                            raw = f.read(min(member.file_size, 1024 * 1024 + 1))
                        candidates.append((member.filename, raw.decode("utf-8", errors="ignore")))
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        continue
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None

    for filename, source in candidates:
        if detect_hosting_bot_text(source):
            return f"Hosting-bot source detected in {filename} (Telegram bot framework + process execution + hosting/file-management indicators)."
    return None


def stop_user_projects(uid):
    """Stop this user's active hosted processes when a temporary ban is applied."""
    for key, proc in list(running.items()):
        if key[0] != uid:
            continue
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        running.pop(key, None)
    db.execute("UPDATE projects SET status='stopped' WHERE user_id=?", (uid,))
    db.commit()


def setting(key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    db.commit()

def is_admin(uid): return uid in ADMIN_IDS
def is_premium(uid):
    r = db.execute("SELECT premium, premium_until FROM users WHERE id=?", (uid,)).fetchone()
    if not r or not r["premium"]:
        return False
    # premium_until=0 is retained as legacy/permanent premium.
    if r["premium_until"] and int(r["premium_until"]) <= int(time.time()):
        db.execute("UPDATE users SET premium=0, premium_until=0, projects_limit=2 WHERE id=?", (uid,))
        db.commit()
        return False
    return True

def premium_remaining(uid):
    r = db.execute("SELECT premium_until FROM users WHERE id=?", (uid,)).fetchone()
    if not r or not r["premium_until"]:
        return "Permanent / not set"
    secs = max(0, int(r["premium_until"]) - int(time.time()))
    return f"{secs // 3600}h {(secs % 3600) // 60}m"

def ensure_user(m: Message):
    u = m.from_user
    db.execute(
        """INSERT INTO users(id,username,full_name,created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (u.id, u.username or "N/A", u.full_name or "", int(time.time()))
    )
    db.commit()

def user_row(uid):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def user_projects(uid):
    return db.execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()

def styled_button(text, callback_data=None, url=None, style=None):
    # Telegram button styles require a recent Bot API and aiogram version.
    data = {"text": text}
    if callback_data is not None: data["callback_data"] = callback_data
    if url is not None: data["url"] = url
    if style is not None: data["style"] = style
    try:
        return InlineKeyboardButton(**data)
    except Exception:
        data.pop("style", None)
        return InlineKeyboardButton(**data)

def menu():
    b = InlineKeyboardBuilder()
    b.button(text="📢 Updates Channel", callback_data="updates")
    b.button(text="📤 Upload File", callback_data="upload")
    b.button(text="📁 Check Files", callback_data="files")
    b.button(text="📘 How to Use", callback_data="howto")
    b.button(text="⚡ Bot Speed", callback_data="speed")
    b.button(text="📊 Statistics", callback_data="stats")
    b.button(text="💎 Premium", callback_data="premium")
    b.adjust(1, 2, 1, 2, 1)
    markup = b.as_markup()
    for row in markup.inline_keyboard:
        for button in row:
            try:
                if button.callback_data in {"upload", "files", "premium", "howto"}: button.style = "primary"
                elif button.callback_data in {"stats", "speed", "updates"}: button.style = "success"
            except (AttributeError, ValueError):
                pass
    return markup

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button(text="↩️ Back", callback_data="back", style="primary")]])

def project_actions_kb(project_id):
    """Actions shown immediately after upload and after project status changes."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            styled_button(text="▶️ Start", callback_data=f"run:{project_id}", style="success"),
            styled_button(text="⏹️ Stop", callback_data=f"stop:{project_id}", style="danger"),
        ],
        [styled_button(text="🗑️ Delete File", callback_data=f"delete:{project_id}", style="danger")],
        [
            styled_button(text="📁 Check Files", callback_data="files", style="primary"),
            styled_button(text="🏠 Home", callback_data="back", style="primary"),
        ],
    ])

def admin_flow_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        styled_button(text="❌ Cancel", callback_data="adm_flow_cancel", style="danger")
    ]])

def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="👥 Users", callback_data="adm_users")
    b.button(text="📊 Statistics", callback_data="adm_stats")
    b.button(text="📣 Broadcast", callback_data="adm_broadcast")
    b.button(text="💎 Premium User", callback_data="adm_premium")
    b.button(text="📁 All Projects", callback_data="adm_projects")
    b.button(text="🚫 Banned Users", callback_data="adm_bans")
    b.button(text="🔓 Unban User", callback_data="adm_unban")
    b.button(text="🛑 Stop All Bots", callback_data="adm_stopall")
    b.button(text="⚙️ Maintenance", callback_data="adm_maint")
    b.button(text="🔗 Set Update Channel", callback_data="adm_channel")
    b.button(text="🆘 Set Support Username", callback_data="adm_contact")
    b.button(text="🔄 Restart Bot", callback_data="adm_restart")
    b.adjust(2, 2, 2, 2, 2, 2, 2)
    markup = b.as_markup()
    styles = {"adm_users": "primary", "adm_stats": "success", "adm_broadcast": "primary", "adm_premium": "success", "adm_projects": "primary", "adm_bans": "danger", "adm_unban": "success", "adm_stopall": "danger", "adm_maint": "danger", "adm_channel": "primary", "adm_contact": "primary", "adm_restart": "danger"}
    for row in markup.inline_keyboard:
        for button in row:
            try:
                if button.callback_data in styles: button.style = styles[button.callback_data]
            except (AttributeError, ValueError):
                pass
    return markup

WELCOME = """👋 <b>Hey — {name}, welcome back!</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>ID:</b> <code>{uid}</code>
🍀 <b>Username:</b> @{username}
🏷️ <b>Rank:</b> {rank}
📁 <b>Projects:</b> {count} / {limit}
━━━━━━━━━━━━━━━━━━━━

⚡ <b>What can I do for you?</b>
› Upload <b>.py / .js</b> scripts or <b>.zip</b> archives
› Run & manage your bots 24/7
› Auto-install missing packages

👇 <b>Tap a button below to get started!</b>"""

async def send_home(target):
    uid = target.from_user.id
    u = user_row(uid)
    ps = user_projects(uid)
    rank = "💎 Premium" if is_premium(uid) else "🆓 Free User"
    limit = u["projects_limit"] if u else 1
    username = (u["username"] if u else "N/A")
    text = WELCOME.format(
        name=target.from_user.full_name,
        uid=uid,
        username=username,
        rank=rank,
        count=len(ps),
        limit="∞" if is_premium(uid) else limit
    )
    await target.answer(text, reply_markup=menu())

@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m)
    ban = active_ban(m.from_user.id)
    if ban and not is_admin(m.from_user.id):
        remaining = max(0, int(ban["expires_at"]) - int(time.time()))
        await m.answer(
            "🚫 <b>Temporary ban active</b>\n\n"
            f"Reason: {__import__('html').escape(ban['reason'])}\n"
            f"Time remaining: <b>{remaining // 60}m {remaining % 60}s</b>\n"
            f"Ban expires: <code>{ban_time_text(ban['expires_at'])}</code>"
        )
        return
    if setting("maintenance", "0") == "1" and not is_admin(m.from_user.id):
        await m.answer("🛠️ <b>Bot is temporarily under maintenance.</b>", reply_markup=back_kb())
        return
    await send_home(m)

@dp.message(Command("admin"))
async def admin(m: Message):
    ensure_user(m)
    if not is_admin(m.from_user.id):
        await m.answer("❌ <b>Admin only.</b>")
        return
    await m.answer("🛠️ <b>HOST BOT ADMIN PANEL</b>", reply_markup=admin_kb())

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text(
        WELCOME.format(
            name=c.from_user.full_name,
            uid=c.from_user.id,
            username=(user_row(c.from_user.id)["username"] if user_row(c.from_user.id) else "N/A"),
            rank=("💎 Premium" if is_premium(c.from_user.id) else "🆓 Free User"),
            count=len(user_projects(c.from_user.id)),
            limit=("∞" if is_premium(c.from_user.id) else (user_row(c.from_user.id)["projects_limit"] if user_row(c.from_user.id) else 2))
        ),
        reply_markup=menu()
    )

@dp.callback_query(F.data == "updates")
async def updates(c: CallbackQuery):
    await c.answer()
    if UPDATE_CHANNEL:
        await c.message.answer(
            "📢 <b>Updates Channel</b>\n\nTap below to open the updates channel.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Open Channel", url=UPDATE_CHANNEL)],
                [InlineKeyboardButton(text="↩️ Back", callback_data="back")]
            ])
        )
    else:
        await c.message.answer("❌ <b>Update channel is not configured.</b>", reply_markup=back_kb())

@dp.callback_query(F.data == "howto")
async def how_to_use(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "📘 <b>HOW TO USE TURBO BOT HOSTER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1.</b> Tap <b>📤 Upload File</b> and send a <code>.py</code>, <code>.js</code> or <code>.zip</code> file.\n\n"
        "<b>2.</b> After upload, the file's action buttons appear immediately.\n\n"
        "<b>▶️ Start</b> — start the project.\n"
        "<b>⏹️ Stop</b> — stop the running project.\n"
        "<b>🗑️ Delete File</b> — permanently delete the project and its files.\n\n"
        "<b>3.</b> Use <b>📁 Check Files</b> later to manage all your projects.\n\n"
        "🆓 Free accounts can host up to 2 projects. Premium limits depend on the configured plan.\n"
        "⚠️ A script must have a supported entry file (such as <code>app.py</code>, <code>main.py</code>, <code>bot.py</code> or <code>index.js</code> inside ZIP) to start.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [styled_button(text="📤 Upload File", callback_data="upload", style="success")],
            [styled_button(text="📁 Check Files", callback_data="files", style="primary")],
            [styled_button(text="↩️ Back", callback_data="back", style="primary")],
        ])
    )

@dp.callback_query(F.data == "speed")
async def speed(c: CallbackQuery):
    await c.answer()
    t = time.perf_counter()
    msg = await c.message.answer("⚡ <b>Checking bot speed...</b>")
    ms = round((time.perf_counter() - t) * 1000, 2)
    await msg.edit_text(f"⚡ <b>Bot Speed</b>\n\n🚀 <b>Response:</b> <code>{ms} ms</code>", reply_markup=back_kb())

@dp.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    await c.answer()
    total = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    premium = db.execute("SELECT COUNT(*) n FROM users WHERE premium=1").fetchone()["n"]
    projects = db.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
    active = sum(1 for p in running.values() if p.poll() is None)
    text = f"""📊 <b>Statistics</b>
━━━━━━━━━━━━━━━━━━━━
👥 <b>Total Users:</b> {total}
💎 <b>Premium:</b> {premium}
📁 <b>Running Bots:</b> {active}
🗂️ <b>Total Projects:</b> {projects}
━━━━━━━━━━━━━━━━━━━━"""
    await c.message.answer(text, reply_markup=back_kb())

@dp.callback_query(F.data == "premium")
async def premium(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        f"""💎 <b>Premium</b>

Get higher project limits and premium hosting features.

⏳ <b>Your remaining time:</b> {premium_remaining(c.from_user.id) if is_premium(c.from_user.id) else "Not active"}
👤 <b>Contact Owner:</b> {PREMIUM_CONTACT}""",
        reply_markup=back_kb()
    )

@dp.callback_query(F.data == "files")
async def files(c: CallbackQuery):
    await c.answer()
    ps = user_projects(c.from_user.id)
    if not ps:
        await c.message.answer("📁 <b>No projects found.</b>\n\nUse <b>📤 Upload File</b> to create your first project.", reply_markup=back_kb())
        return
    lines = ["📁 <b>Your Files / Projects</b>\n━━━━━━━━━━━━━━━━━━━━"]
    kb = InlineKeyboardBuilder()
    for p in ps:
        status = "🟢 Running" if (p["user_id"], p["id"]) in running and running[(p["user_id"], p["id"])].poll() is None else "🔴 Stopped"
        lines.append(f"• <b>{p['name']}</b> — {status}")
        kb.button(text=f"▶️ {p['name']}", callback_data=f"run:{p['id']}")
        kb.button(text=f"⏹ {p['name']}", callback_data=f"stop:{p['id']}")
        kb.button(text=f"🗑️ Delete {p['name']}", callback_data=f"delete:{p['id']}")
    kb.button(text="↩️ Back", callback_data="back")
    kb.adjust(2, 2, 2, 1)
    for row in kb.as_markup().inline_keyboard:
        for button in row:
            try:
                if button.callback_data and button.callback_data.startswith("delete:"): button.style = "danger"
                elif button.callback_data and button.callback_data.startswith("run:"): button.style = "success"
                elif button.callback_data and button.callback_data.startswith("stop:"): button.style = "danger"
            except (AttributeError, ValueError):
                pass
    await c.message.answer("\n".join(lines), reply_markup=kb.as_markup())

@dp.callback_query(F.data == "upload")
async def upload(c: CallbackQuery):
    await c.answer()
    ban = active_ban(c.from_user.id)
    if ban and not is_admin(c.from_user.id):
        remaining = max(0, int(ban["expires_at"]) - int(time.time()))
        await c.message.answer(
            "🚫 <b>Upload disabled during your temporary ban.</b>\n"
            f"Time remaining: <b>{remaining // 60}m {remaining % 60}s</b>"
        )
        return
    await c.message.answer(
        """📤 <b>Upload File</b>

Send your <b>.py</b>, <b>.js</b> or <b>.zip</b> file here.

🆓 <b>Free:</b> 2 projects
💎 <b>Premium:</b> unlimited projects

<b>After upload:</b> the file will appear under <b>📁 Check Files</b>.""",
        reply_markup=back_kb()
    )

@dp.message(F.document)
async def document(m: Message):
    ensure_user(m)
    uid = m.from_user.id
    ban = active_ban(uid)
    if ban and not is_admin(uid):
        remaining = max(0, int(ban["expires_at"]) - int(time.time()))
        await m.answer(
            "🚫 <b>You are temporarily banned from uploads and hosting.</b>\n\n"
            f"Reason: {__import__('html').escape(ban['reason'])}\n"
            f"Time remaining: <b>{remaining // 60}m {remaining % 60}s</b>"
        )
        return
    u = user_row(uid)
    limit = 10**9 if is_premium(uid) else u["projects_limit"]
    if len(user_projects(uid)) >= limit:
        await m.answer("🚫 <b>Project limit reached.</b>\n\n💎 Upgrade to Premium for more projects.")
        return
    # Use only the filename component; never trust a Telegram-supplied path.
    name = Path(m.document.file_name or "upload").name
    ext = Path(name).suffix.lower()
    if ext not in {".py", ".js", ".zip"}:
        await m.answer("❌ <b>Only .py, .js and .zip files are supported.</b>")
        return
    project_id = int(time.time() * 1000) % 2147483647
    folder = PROJECTS / str(uid) / str(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    tgfile = await bot.get_file(m.document.file_id)
    await bot.download_file(tgfile.file_path, destination=dest)

    # Detect a hosting bot before extracting/saving the upload as a project.
    reason = inspect_upload_for_hosting_bot(dest, ext)
    if reason and not is_admin(uid):
        now = int(time.time())
        expires = now + BAN_SECONDS
        db.execute(
            "INSERT OR REPLACE INTO user_bans(user_id,reason,banned_at,expires_at) VALUES(?,?,?,?)",
            (uid, reason, now, expires)
        )
        db.commit()
        stop_user_projects(uid)
        until_text = ban_time_text(expires)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(
                    admin_id, FSInputFile(dest),
                    caption=("🚨 <b>Hosting-bot upload detected</b>\n"
                             f"👤 User ID: <code>{uid}</code> (@{u['username']})\n"
                             f"📄 File: <code>{__import__('html').escape(name)}</code>\n"
                             f"🧾 Reason: {__import__('html').escape(reason)}\n"
                             "🚫 Action: 30-minute automatic ban\n"
                             f"⏳ Expires: <code>{until_text}</code>")
                )
            except Exception as e:
                log.warning("Could not notify admin %s about detected upload: %s", admin_id, e)
                try:
                    await bot.send_message(
                        admin_id,
                        "🚨 <b>Hosting-bot upload detected</b>\n"
                        f"👤 User ID: <code>{uid}</code>\n📄 File: <code>{__import__('html').escape(name)}</code>\n"
                        f"🧾 Reason: {__import__('html').escape(reason)}\n🚫 Ban: 30 minutes\n⏳ Expires: <code>{until_text}</code>"
                    )
                except Exception:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
        await m.answer(
            "🚫 <b>Upload rejected: hosting-bot code detected.</b>\n\n"
            "Your account has been temporarily banned from uploads and hosting for <b>30 minutes</b>.\n"
            f"⏳ Ban expires: <code>{until_text}</code>"
        )
        return

    entry = name if ext in {".py", ".js"} else ""
    if ext == ".zip":
        try:
            with zipfile.ZipFile(dest) as z:
                root = folder.resolve()
                for member in z.infolist():
                    target = (folder / member.filename).resolve()
                    if target != root and root not in target.parents:
                        raise ValueError("Unsafe ZIP path")
                z.extractall(folder)
            for candidate in ("app.py", "main.py", "bot.py", "index.js"):
                if (folder / candidate).exists():
                    entry = candidate
                    break
        except (zipfile.BadZipFile, ValueError):
            shutil.rmtree(folder, ignore_errors=True)
            await m.answer("❌ <b>Invalid or unsafe ZIP archive.</b>")
            return

    db.execute(
        "INSERT INTO projects(id,user_id,name,folder,entry,status,created_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, uid, name, str(folder), entry, "stopped", int(time.time()))
    )
    db.commit()
    # Send the original hosted upload to the admin so it appears in the admin chat/panel.
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_document(
                admin_id, FSInputFile(dest),
                caption=(f"📥 <b>New hosted file</b>\n"
                         f"👤 User: <code>{uid}</code> (@{u['username']})\n"
                         f"📁 File: <code>{name}</code>\n"
                         f"🆔 Project ID: <code>{project_id}</code>\n"
                         f"📂 Stored folder: <code>{folder}</code>")
            )
        except Exception as e:
            log.warning("Could not notify admin %s about upload: %s", admin_id, e)
    await m.answer(
        f"✅ <b>File uploaded successfully!</b>\n\n📁 <b>Project:</b> {__import__('html').escape(name)}\n🆔 <b>ID:</b> <code>{project_id}</code>\n\n👇 <b>Choose an action for this file:</b>",
        reply_markup=project_actions_kb(project_id)
    )

async def launch_project(p):
    uid, pid = p["user_id"], p["id"]
    folder = Path(p["folder"])
    entry = p["entry"]
    if not entry:
        raise RuntimeError("No entry file found. ZIP should contain app.py, main.py, bot.py or index.js.")
    existing = running.get((uid, pid))
    if existing and existing.poll() is None:
        return existing
    if entry.endswith(".py"):
        cmd = ["python", entry]
    else:
        cmd = ["node", entry]
    # Keep output available for debugging if the hosted process exits immediately.
    log_path = folder / "host.log"
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd, cwd=folder, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True
        )
    finally:
        log_file.close()
    running[(uid, pid)] = proc
    return proc

@dp.callback_query(F.data.startswith("run:"))
async def run_project(c: CallbackQuery):
    await c.answer()
    ban = active_ban(c.from_user.id)
    if ban and not is_admin(c.from_user.id):
        remaining = max(0, int(ban["expires_at"]) - int(time.time()))
        await c.message.answer(
            "🚫 <b>Hosting is disabled during your temporary ban.</b>\n"
            f"Time remaining: <b>{remaining // 60}m {remaining % 60}s</b>"
        )
        return
    pid = int(c.data.split(":")[1])
    p = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, c.from_user.id)).fetchone()
    if not p:
        await c.message.answer("❌ <b>Project not found.</b>")
        return

    progress = await c.message.answer(
        f"⏳ <b>Starting {p['name']}</b>\n\n"
        "Preparing the process…\n"
        "<i>Hosted status will appear only after startup checks.</i>"
    )
    try:
        proc = await launch_project(p)
        log_path = Path(p["folder"]) / "host.log"
        # A live OS process alone does not prove the hosted app has finished startup.
        # Watch common startup/readiness log messages for up to 60 seconds.
        ready_markers = (
            "application started", "startup complete", "startup completed",
            "started polling", "start polling", "polling started",
            "bot started", "bot is running", "server started", "server is running",
            "listening on", "running on", "uvicorn running", "ready to accept"
        )
        deadline = time.monotonic() + 60
        tick = 0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = ""
                if log_path.exists():
                    try:
                        detail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                    except OSError:
                        pass
                suffix = ("\n\n<b>Last log output:</b>\n<code>" +
                          __import__("html").escape(detail) + "</code>") if detail else ""
                await progress.edit_text(
                    f"❌ <b>Startup failed</b>\n\n{p['name']} exited before it became ready.{suffix}",
                    reply_markup=back_kb()
                )
                return

            log_text = ""
            if log_path.exists():
                try:
                    log_text = log_path.read_text(encoding="utf-8", errors="replace")[-12000:].lower()
                except OSError:
                    pass
            if any(marker in log_text for marker in ready_markers):
                db.execute("UPDATE projects SET status='running' WHERE id=?", (pid,))
                db.commit()
                await progress.edit_text(
                    f"✅ <b>Hosted successfully!</b>\n\n📁 <b>File:</b> {p['name']}\n🟢 <b>Status:</b> Running",
                    reply_markup=back_kb()
                )
                return

            dots = "." * (tick % 3 + 1)
            try:
                await progress.edit_text(
                    f"⏳ <b>Starting {p['name']}{dots}</b>\n\n"
                    "The process is still initializing. Waiting for a startup-ready log message…"
                )
            except Exception:
                pass
            tick += 1
            await asyncio.sleep(1)

        # Do not falsely claim success if the app has not confirmed readiness.
        await progress.edit_text(
            f"⚠️ <b>Process is running, but startup is not confirmed yet.</b>\n\n"
            f"📁 <b>File:</b> {p['name']}\n"
            "Check the project's <code>host.log</code>. The hosted script must log a readiness message "
            "(for example, 'Bot started' or 'Listening on') for the bot to confirm Hosted successfully.",
            reply_markup=back_kb()
        )
    except Exception as e:
        await progress.edit_text(
            f"❌ <b>Could not start this file.</b>\n\n<code>{__import__('html').escape(str(e)[:1200])}</code>",
            reply_markup=back_kb()
        )

@dp.callback_query(F.data.startswith("stop:"))
async def stop_project(c: CallbackQuery):
    await c.answer()
    pid = int(c.data.split(":")[1])
    p = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, c.from_user.id)).fetchone()
    if not p:
        await c.message.answer("❌ <b>Project not found.</b>")
        return
    proc = running.pop((c.from_user.id, pid), None)
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
    db.execute("UPDATE projects SET status='stopped' WHERE id=?", (pid,))
    db.commit()
    await c.message.answer(f"🔴 <b>{__import__('html').escape(p['name'])}</b> stopped.\n\nYou can start it again or delete it below.", reply_markup=project_actions_kb(pid))

@dp.callback_query(F.data.startswith("delete:"))
async def delete_project(c: CallbackQuery):
    await c.answer()
    try:
        pid = int(c.data.split(":", 1)[1])
    except ValueError:
        await c.message.answer("❌ Invalid project ID.")
        return
    p = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, c.from_user.id)).fetchone()
    if not p:
        await c.message.answer("❌ <b>Project not found.</b>")
        return
    proc = running.pop((c.from_user.id, pid), None)
    if proc and proc.poll() is None:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except Exception: proc.terminate()
    folder = Path(p["folder"]).resolve()
    if PROJECTS.resolve() not in folder.parents:
        await c.message.answer("❌ Unsafe project path; deletion blocked.")
        return
    shutil.rmtree(folder, ignore_errors=True)
    db.execute("DELETE FROM projects WHERE id=? AND user_id=?", (pid, c.from_user.id))
    db.commit()
    await c.message.answer(f"🗑️ <b>{__import__('html').escape(p['name'])}</b> deleted permanently.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [styled_button(text="📁 Check Files", callback_data="files", style="primary")],
        [styled_button(text="📤 Upload Another File", callback_data="upload", style="success")],
        [styled_button(text="🏠 Home", callback_data="back", style="primary")],
    ]))

# ---------- ADMIN ----------

@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        await c.message.answer("❌ <b>Admin only.</b>")
        return
    action = c.data[4:]
    if action == "users":
        total = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        prem = db.execute("SELECT COUNT(*) n FROM users WHERE premium=1").fetchone()["n"]
        await c.message.answer(f"👥 <b>Users</b>\n\nTotal: <b>{total}</b>\nPremium: <b>{prem}</b>", reply_markup=admin_kb())
    elif action == "stats":
        total = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        projects = db.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
        active = sum(1 for p in running.values() if p.poll() is None)
        await c.message.answer(f"📊 <b>Admin Statistics</b>\n\n👥 Users: {total}\n📁 Projects: {projects}\n🟢 Running: {active}", reply_markup=admin_kb())
    elif action == "projects":
        rows = db.execute("SELECT * FROM projects ORDER BY id DESC LIMIT 30").fetchall()
        if not rows:
            await c.message.answer("📁 <b>No projects.</b>", reply_markup=admin_kb())
        else:
            text = "📁 <b>Latest Projects</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            for p in rows:
                text += f"• <code>{p['id']}</code> — {p['name']} — user <code>{p['user_id']}</code>\n"
            await c.message.answer(text, reply_markup=admin_kb())
    elif action == "bans":
        now = int(time.time())
        db.execute("DELETE FROM user_bans WHERE expires_at<=?", (now,))
        db.commit()
        rows = db.execute("SELECT * FROM user_bans WHERE expires_at>? ORDER BY expires_at", (now,)).fetchall()
        if not rows:
            await c.message.answer("🚫 <b>No active temporary bans.</b>", reply_markup=admin_kb())
        else:
            lines = ["🚫 <b>Active Temporary Bans</b>", "━━━━━━━━━━━━━━━━━━━━"]
            for row in rows[:30]:
                remaining = max(0, int(row["expires_at"]) - now)
                lines.append(
                    f"👤 User ID: <code>{row['user_id']}</code>\n"
                    f"🧾 Reason: {__import__('html').escape(row['reason'])}\n"
                    f"⏳ Remaining: <b>{remaining // 60}m {remaining % 60}s</b>\n"
                    f"🕒 Expires: <code>{ban_time_text(row['expires_at'])}</code>\n"
                )
            lines.append("Use the <b>Unban User</b> button in the admin panel to remove a ban early.")
            await c.message.answer("\n".join(lines), reply_markup=admin_kb())
    elif action == "unban":
        admin_states[c.from_user.id] = {"step": "unban_user_id"}
        await c.message.answer("🔓 <b>Unban User</b>\n\nSend the Telegram user ID whose temporary ban should be removed.", reply_markup=admin_flow_kb())
    elif action == "stopall":
        stopped = 0
        for key, proc in list(running.items()):
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except Exception: proc.terminate()
                stopped += 1
            running.pop(key, None)
        db.execute("UPDATE projects SET status='stopped'")
        db.commit()
        await c.message.answer(f"🛑 <b>Stopped {stopped} running project(s).</b>", reply_markup=admin_kb())
    elif action == "maint":
        new = "0" if setting("maintenance", "0") == "1" else "1"
        set_setting("maintenance", new)
        state = "ON" if new == "1" else "OFF"
        await c.message.answer(f"⚙️ <b>Maintenance: {state}</b>", reply_markup=admin_kb())
    elif action == "restart":
        await c.message.answer("🔄 <b>Restart the hosting process from your server panel.</b>", reply_markup=admin_kb())
    elif action == "premium":
        admin_states[c.from_user.id] = {"step": "user_id"}
        await c.message.answer("💎 <b>Set Premium</b>\n\nFirst, send the user's Telegram ID:", reply_markup=back_kb())
    elif action == "broadcast":
        admin_states[c.from_user.id] = {"step": "broadcast_message"}
        await c.message.answer(
            "📣 <b>Broadcast Message</b>\n\nSend the message you want to broadcast to all registered users.\nYou will get a confirmation button before sending.",
            reply_markup=admin_flow_kb()
        )
    elif action == "channel":
        admin_states[c.from_user.id] = {"step": "update_channel"}
        current = setting("update_channel", UPDATE_CHANNEL) or "Not configured"
        await c.message.answer(
            f"🔗 <b>Set Update Channel</b>\n\nCurrent: <code>{__import__('html').escape(current)}</code>\n\nSend the channel link or username (for example, https://t.me/yourchannel or @yourchannel).",
            reply_markup=admin_flow_kb()
        )
    elif action == "contact":
        admin_states[c.from_user.id] = {"step": "support_username"}
        current = setting("premium_contact", PREMIUM_CONTACT) or "Not configured"
        await c.message.answer(
            f"🆘 <b>Set Support Username</b>\n\nCurrent: <code>{__import__('html').escape(current)}</code>\n\nSend the support username (for example, @YourSupport).",
            reply_markup=admin_flow_kb()
        )
    elif action == "broadcast_confirm":
        state = admin_states.get(c.from_user.id, {})
        if state.get("step") != "broadcast_confirm" or not state.get("message"):
            await c.message.answer("❌ No pending broadcast message.", reply_markup=admin_kb())
            return
        message_text = state["message"]
        admin_states.pop(c.from_user.id, None)
        users = db.execute("SELECT id FROM users").fetchall()
        ok = fail = 0
        status = await c.message.answer("📣 <b>Broadcast started…</b>")
        for row in users:
            try:
                await bot.send_message(row["id"], message_text, parse_mode=None)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.04)
        await status.edit_text(f"📣 <b>Broadcast finished.</b>\n\n✅ Sent: {ok}\n❌ Failed: {fail}", reply_markup=admin_kb())
    elif action == "broadcast_cancel":
        admin_states.pop(c.from_user.id, None)
        await c.message.answer("❎ Broadcast cancelled.", reply_markup=admin_kb())
    elif action == "flow_cancel":
        admin_states.pop(c.from_user.id, None)
        await c.message.answer("❎ Action cancelled.", reply_markup=admin_kb())

def parse_duration(value):
    value = value.strip().lower()
    import re
    match = re.fullmatch(r"(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)", value)
    if not match:
        return None
    amount = int(match.group(1)); unit = match.group(2)
    factor = 60 if unit in {"m", "min", "mins", "minute", "minutes"} else 3600 if unit in {"h", "hr", "hrs", "hour", "hours"} else 86400 if unit in {"d", "day", "days"} else 604800
    seconds = amount * factor
    return seconds if 60 <= seconds <= 365 * 86400 else None

@dp.message(Command("premium"))
async def cmd_premium(m: Message):
    if not is_admin(m.from_user.id): return
    parts = (m.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parse_duration(parts[2]) is None:
        await m.answer("Usage: <code>/premium USER_ID 1h</code>\nDuration examples: <code>30m</code>, <code>1h</code>, <code>2d</code>, <code>1w</code>")
        return
    uid = int(parts[1]); seconds = parse_duration(parts[2])
    if not user_row(uid):
        await m.answer("❌ User not found. Ask them to send /start first.")
        return
    until = int(time.time()) + seconds
    db.execute("UPDATE users SET premium=1, premium_until=?, projects_limit=999999 WHERE id=?", (until, uid)); db.commit()
    await m.answer(f"💎 Premium enabled for <code>{uid}</code> for <b>{parts[2]}</b>.")
    try: await bot.send_message(uid, f"💎 <b>Premium activated!</b>\nDuration: <b>{parts[2]}</b>")
    except Exception: pass

@dp.callback_query(F.data == "adm_flow_cancel")
async def admin_flow_cancel(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    admin_states.pop(c.from_user.id, None)
    await c.message.answer("❎ Action cancelled.", reply_markup=admin_kb())

@dp.message(F.text & ~F.text.startswith("/"))
async def admin_premium_steps(m: Message):
    state = admin_states.get(m.from_user.id)
    if not state or not is_admin(m.from_user.id) or (m.text or "").startswith("/"):
        return
    if state["step"] == "broadcast_message":
        message_text = (m.text or "").strip()
        if not message_text:
            await m.answer("❌ Please send a non-empty text message.", reply_markup=admin_flow_kb())
            return
        state.update(step="broadcast_confirm", message=message_text)
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
            styled_button(text="✅ Send to All Users", callback_data="adm_broadcast_confirm", style="success"),
            styled_button(text="❌ Cancel", callback_data="adm_broadcast_cancel", style="danger")
        ]])
        await m.answer(
            "📣 <b>Broadcast Preview</b>\n━━━━━━━━━━━━━━━━━━━━\n" +
            __import__('html').escape(message_text) +
            "\n\nSend this message to all registered users?",
            reply_markup=confirm_kb
        )
        return
    if state["step"] == "update_channel":
        value = (m.text or "").strip()
        if not (value.startswith("https://t.me/") or (value.startswith("@") and len(value) > 1)):
            await m.answer("❌ Send a valid channel link (https://t.me/...) or @channelusername.", reply_markup=admin_flow_kb())
            return
        global UPDATE_CHANNEL
        UPDATE_CHANNEL = value
        set_setting("update_channel", value)
        admin_states.pop(m.from_user.id, None)
        await m.answer(f"✅ <b>Update channel saved.</b>\n🔗 {__import__('html').escape(value)}", reply_markup=admin_kb())
        return
    if state["step"] == "support_username":
        value = (m.text or "").strip()
        if not (value.startswith("@") and len(value) > 1 and " " not in value):
            await m.answer("❌ Send a valid support username like @YourSupport.", reply_markup=admin_flow_kb())
            return
        global PREMIUM_CONTACT
        PREMIUM_CONTACT = value
        set_setting("premium_contact", value)
        admin_states.pop(m.from_user.id, None)
        await m.answer(f"✅ <b>Support username saved.</b>\n🆘 {__import__('html').escape(value)}", reply_markup=admin_kb())
        return
    if state["step"] == "unban_user_id":
        value = (m.text or "").strip()
        if not value.isdigit():
            await m.answer("❌ Send a numeric Telegram user ID.", reply_markup=admin_flow_kb())
            return
        uid = int(value)
        db.execute("DELETE FROM user_bans WHERE user_id=?", (uid,))
        db.commit()
        admin_states.pop(m.from_user.id, None)
        await m.answer(f"✅ Temporary ban cleared for user <code>{uid}</code>.", reply_markup=admin_kb())
        try:
            await bot.send_message(uid, "✅ Your temporary ban has been removed by the admin.")
        except Exception:
            pass
        return
    if state["step"] == "user_id":
        if not (m.text or "").strip().isdigit():
            await m.answer("❌ Send a numeric Telegram user ID first.")
            return
        uid = int(m.text.strip())
        if not user_row(uid):
            await m.answer("❌ User not found. Ask them to send /start first, then try again.")
            return
        state.update(step="duration", user_id=uid)
        await m.answer(f"👤 User ID: <code>{uid}</code>\n\nNow send the premium duration, e.g. <code>1h</code>, <code>30m</code>, <code>2d</code> or <code>1w</code>.")
        return
    if state["step"] == "duration":
        seconds = parse_duration(m.text or "")
        if seconds is None:
            await m.answer("❌ Invalid duration. Use <code>30m</code>, <code>1h</code>, <code>2d</code> or <code>1w</code> (maximum 365 days).")
            return
        uid = state["user_id"]; until = int(time.time()) + seconds
        db.execute("UPDATE users SET premium=1, premium_until=?, projects_limit=999999 WHERE id=?", (until, uid)); db.commit()
        admin_states.pop(m.from_user.id, None)
        await m.answer(f"✅ <b>Premium set successfully!</b>\n👤 User: <code>{uid}</code>\n⏳ Duration: <b>{m.text.strip()}</b>", reply_markup=admin_kb())
        try: await bot.send_message(uid, f"💎 <b>Premium activated!</b>\n⏳ Duration: <b>{m.text.strip()}</b>")
        except Exception: pass

@dp.message(Command("unban"))
async def cmd_unban(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: <code>/unban USER_ID</code>")
        return
    uid = int(parts[1])
    db.execute("DELETE FROM user_bans WHERE user_id=?", (uid,))
    db.commit()
    await m.answer(f"✅ Temporary ban cleared for user <code>{uid}</code>.")
    try:
        await bot.send_message(uid, "✅ Your temporary ban has been removed by the admin.")
    except Exception:
        pass


@dp.message(Command("setchannel"))
async def cmd_channel(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Usage: <code>/setchannel https://t.me/...</code>")
        return
    set_setting("update_channel", parts[1].strip())
    global UPDATE_CHANNEL
    UPDATE_CHANNEL = parts[1].strip()
    await m.answer("✅ <b>Update channel saved.</b>")

@dp.message(Command("setcontact"))
async def cmd_contact(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Usage: <code>/setcontact @username</code>")
        return
    global PREMIUM_CONTACT
    PREMIUM_CONTACT = parts[1].strip()
    set_setting("premium_contact", PREMIUM_CONTACT)
    await m.answer("✅ <b>Premium contact saved.</b>")

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Usage: <code>/broadcast your message</code>")
        return
    users = db.execute("SELECT id FROM users").fetchall()
    ok = fail = 0
    for r in users:
        try:
            await bot.send_message(r["id"], parts[1])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    await m.answer(f"📣 <b>Broadcast finished.</b>\n\n✅ Sent: {ok}\n❌ Failed: {fail}")

async def main():
    # Load persisted configurable values.
    global UPDATE_CHANNEL, PREMIUM_CONTACT
    UPDATE_CHANNEL = setting("update_channel", UPDATE_CHANNEL)
    PREMIUM_CONTACT = setting("premium_contact", PREMIUM_CONTACT)
    log.info("Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
