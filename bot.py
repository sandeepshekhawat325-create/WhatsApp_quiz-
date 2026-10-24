import asyncio
import io
import logging
import os
import random
import sqlite3
import threading
import urllib.parse
from datetime import datetime

import docx
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# Timezone & config
# ---------------------------
IST = pytz.timezone("Asia/Kolkata")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
RAW_CHANNEL_ID = (os.environ.get("TG_CHANNEL_ID") or os.environ.get("TELEGRAM_CHANNEL_ID", "")).strip()
TELEGRAM_CHANNEL_USERNAME = (os.environ.get("TG_CHANNEL_USERNAME") or os.environ.get("TELEGRAM_CHANNEL_USERNAME", "")).strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.environ.get("PORT", 10000))


def get_target_channel():
    """Sahi Channel ID ya @username return karta hai taaki post hamesha channel par jaye."""
    if RAW_CHANNEL_ID:
        # Agar numeric ID hai (jaise -100123456789)
        if RAW_CHANNEL_ID.lstrip("-").isdigit():
            if not RAW_CHANNEL_ID.startswith("-"):
                return int(f"-100{RAW_CHANNEL_ID}")
            return int(RAW_CHANNEL_ID)
        # Agar galti se username ID wale variable me daal diya ho
        return RAW_CHANNEL_ID if RAW_CHANNEL_ID.startswith("@") else f"@{RAW_CHANNEL_ID}"
    if TELEGRAM_CHANNEL_USERNAME:
        return TELEGRAM_CHANNEL_USERNAME if TELEGRAM_CHANNEL_USERNAME.startswith("@") else f"@{TELEGRAM_CHANNEL_USERNAME}"
    return None


# ---------------------------
# Flask + scheduler
# ---------------------------
app = Flask(__name__)
scheduler = AsyncIOScheduler(timezone=IST)
application = None
bot_loop = None

# ---------------------------
# DB
# ---------------------------
DB_PATH = "exam_system.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            topic TEXT,
            sub_topic TEXT,
            time_per_q INTEGER DEFAULT 30,
            scheduled_time TEXT,
            status TEXT DEFAULT 'DRAFT',
            poll_type TEXT DEFAULT 'quiz',
            is_anonymous INTEGER DEFAULT 0,
            allow_multiple_answers INTEGER DEFAULT 0,
            shuffle_options INTEGER DEFAULT 0,
            question_timer INTEGER DEFAULT 30,
            channel_lock INTEGER DEFAULT 1,
            scoreboard_msg_id INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    try:
        cursor.execute("ALTER TABLE exams ADD COLUMN scoreboard_msg_id INTEGER DEFAULT 0")
    except Exception:
        pass

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            question_text TEXT,
            opt_a TEXT,
            opt_b TEXT,
            opt_c TEXT,
            opt_d TEXT,
            correct_opt_id INTEGER,
            pos_marks REAL DEFAULT 1.0,
            neg_marks REAL DEFAULT 0.0,
            solution TEXT,
            explanation TEXT,
            FOREIGN KEY(exam_id) REFERENCES exams(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS student_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            score REAL,
            total_time_sec INTEGER,
            total_questions INTEGER DEFAULT 0,
            correct_answers INTEGER DEFAULT 0,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            value TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            question_id INTEGER,
            poll_id TEXT UNIQUE,
            correct_opt_id INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    try:
        cursor.execute("ALTER TABLE channel_polls ADD COLUMN correct_opt_id INTEGER DEFAULT 0")
    except Exception:
        pass

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            question_id INTEGER,
            poll_id TEXT,
            user_id INTEGER,
            user_name TEXT,
            selected_option TEXT,
            is_correct INTEGER DEFAULT 0,
            answered_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()
    ensure_default_settings()


def ensure_default_settings():
    defaults = {
        "show_voter_names": "1",
        "allow_multiple_answers": "0",
        "shuffle_options": "0",
        "quiz_mode": "1",
        "show_explanations": "1",
        "limit_duration": "1",
        "channel_lock": "1",
        "timer_sec": "30",
    }

    conn = get_conn()
    cursor = conn.cursor()
    for key, value in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO admin_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "0") -> str:
    conn = get_conn()
    cursor = conn.cursor()
    row = cursor.execute("SELECT value FROM admin_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO admin_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def is_show_voter_names() -> bool:
    return get_setting("show_voter_names", "1") == "1"


def is_multiple_allowed() -> bool:
    return get_setting("allow_multiple_answers", "0") == "1"


def is_shuffle_enabled() -> bool:
    return get_setting("shuffle_options", "0") == "1"


def is_quiz_mode() -> bool:
    return get_setting("quiz_mode", "1") == "1"


def is_explanation_enabled() -> bool:
    return get_setting("show_explanations", "1") == "1"


def is_limit_duration_enabled() -> bool:
    return get_setting("limit_duration", "1") == "1"


def is_channel_lock_enabled() -> bool:
    return get_setting("channel_lock", "1") == "1"


def get_timer_sec() -> int:
    try:
        val = int(get_setting("timer_sec", "30"))
        return val if val >= 5 else 30
    except Exception:
        return 30


init_db()
admin_drafts = {}

# ---------------------------
# DOCX parser
# ---------------------------
def parse_docx_content(file_bytes):
    doc = docx.Document(io.BytesIO(file_bytes))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    questions = []
    current_q = None
    options = []
    correct_idx = 0
    pos_m = 1.0
    neg_m = 0.0
    sol = ""

    for raw_line in lines:
        line = raw_line.strip()
        lower = line.lower()

        if lower.startswith("question") or lower.startswith("q:") or lower.startswith("q."):
            if current_q and len(options) >= 2:
                questions.append(
                    {
                        "question": current_q,
                        "options": options[:4],
                        "correct": correct_idx,
                        "pos_marks": pos_m,
                        "neg_marks": neg_m,
                        "solution": sol,
                    }
                )

            if ":" in line:
                current_q = line.split(":", 1)[-1].strip()
            else:
                current_q = line

            options = []
            correct_idx = 0
            pos_m = 1.0
            neg_m = 0.0
            sol = ""

        elif any(
            lower.startswith(prefix)
            for prefix in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]
        ):
            for prefix in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]:
                if lower.startswith(prefix):
                    opt_val = line[len(prefix):].strip()
                    break
            else:
                opt_val = line
            options.append(opt_val if opt_val else line)

        elif "answer" in lower or lower.startswith("ans"):
            ans_str = line.split(":", 1)[-1].strip().lower() if ":" in line else line.lower()
            if "a" in ans_str:
                correct_idx = 0
            elif "b" in ans_str:
                correct_idx = 1
            elif "c" in ans_str:
                correct_idx = 2
            elif "d" in ans_str:
                correct_idx = 3

        elif "solution" in lower or "explanation" in lower:
            sol = line.split(":", 1)[-1].strip() if ":" in line else line

        elif "positive marks" in lower:
            try:
                pos_m = float(line.split(":", 1)[-1].strip())
            except Exception:
                pos_m = 1.0

        elif "negative marks" in lower:
            try:
                neg_m = float(line.split(":", 1)[-1].strip())
            except Exception:
                neg_m = 0.0

    if current_q and len(options) >= 2:
        questions.append(
            {
                "question": current_q,
                "options": options[:4],
                "correct": correct_idx,
                "pos_marks": pos_m,
                "neg_marks": neg_m,
                "solution": sol,
            }
        )

    return questions


# ---------------------------
# Admin UI
# ---------------------------
def build_admin_message():
    target_ch = get_target_channel() or "❌ NOT SET"
    return (
        "⚙️ *NEW QUIZ & EXAM SETTINGS*\n\n"
        f"📢 Target Channel: `{target_ch}`\n\n"
        f"👁️ Show Who Voted: **{'ON' if is_show_voter_names() else 'OFF'}**\n"
        f"☑️ Allow Multiple Answers: **{'ON' if is_multiple_allowed() else 'OFF'}**\n"
        f"🔀 Shuffle Options: **{'ON' if is_shuffle_enabled() else 'OFF'}**\n"
        f"✅ Set Correct Answer (Quiz Mode): **{'ON' if is_quiz_mode() else 'OFF'}**\n"
        f"⏱️ Limit Duration (30s Timer): **{'ON' if is_limit_duration_enabled() else 'OFF'}**\n"
        f"⏳ Question Interval / Timer: `{get_timer_sec()} sec`\n"
        f"💡 Explanation: **{'ON' if is_explanation_enabled() else 'OFF'}**\n"
        f"🔒 Channel Lock: **{'ON' if is_channel_lock_enabled() else 'OFF'}**"
    )


def admin_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                f"👁️ Show Who Voted: {'ON 🔵' if is_show_voter_names() else 'OFF ⚪'}",
                callback_data="toggle_show_voter_names",
            )
        ],
        [
            InlineKeyboardButton(
                f"☑️ Allow Multiple Answers: {'ON 🔵' if is_multiple_allowed() else 'OFF ⚪'}",
                callback_data="toggle_multiple_answers",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔀 Shuffle Options: {'ON 🔵' if is_shuffle_enabled() else 'OFF ⚪'}",
                callback_data="toggle_shuffle_options",
            )
        ],
        [
            InlineKeyboardButton(
                f"✅ Set Correct Answer (Quiz): {'ON 🔵' if is_quiz_mode() else 'OFF ⚪'}",
                callback_data="toggle_quiz_mode",
            )
        ],
        [
            InlineKeyboardButton(
                f"⏱️ Limit Duration: {'ON 🔵' if is_limit_duration_enabled() else 'OFF ⚪'} ({get_timer_sec()}s)",
                callback_data="toggle_limit_duration",
            )
        ],
        [
            InlineKeyboardButton(
                f"💡 Explanation: {'ON 🔵' if is_explanation_enabled() else 'OFF ⚪'}",
                callback_data="toggle_explanations",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔒 Channel Lock: {'ON 🔵' if is_channel_lock_enabled() else 'OFF ⚪'}",
                callback_data="toggle_channel_lock",
            )
        ],
        [
            InlineKeyboardButton("📢 Test Channel Connection", callback_data="admin_test_channel"),
        ],
        [
            InlineKeyboardButton("🆕 Create New Exam", callback_data="admin_create_exam"),
            InlineKeyboardButton("📋 View Exams", callback_data="admin_view_exams"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_schedule_string(date_text: str, hour_text: str, minute_text: str, ampm_text: str) -> str:
    try:
        return f"{date_text} {hour_text}:{minute_text} {ampm_text}"
    except Exception:
        return ""


def trigger_channel_exam_background(exam_id: int):
    """Scheduler ya button se bina timeout ke channel par quiz post karne ke liye."""
    global bot_loop
    if bot_loop and bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(post_exam_to_channel(exam_id), bot_loop)


def schedule_exam_job(exam_id: int, scheduled_datetime: str):
    try:
        parsed = datetime.strptime(scheduled_datetime.strip(), "%Y-%m-%d %I:%M %p")
        local_dt = IST.localize(parsed)
        scheduler.add_job(
            post_exam_to_channel,
            "date",
            run_date=local_dt,
            args=[exam_id],
            id=f"exam_job_{exam_id}",
            replace_existing=True,
        )
        return True, local_dt
    except Exception as exc:
        logger.exception("schedule_exam_job failed: %s", exc)
        return False, None


def update_exam_schedule(exam_id: int, scheduled_datetime: str):
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE exams SET scheduled_time = ? WHERE id = ?",
            (scheduled_datetime, exam_id),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.exception("update_exam_schedule failed: %s", exc)
        return False


# ---------------------------
# Live Scoreboard System (Channel Par)
# ---------------------------
async def build_scoreboard_text(exam_id: int, current_q_idx: int = None, total_qs: int = None) -> str:
    conn = get_conn()
    exam = conn.execute(
        "SELECT subject, topic, sub_topic, status FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    if total_qs is None:
        total_q_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM questions WHERE exam_id=?",
            (exam_id,),
        ).fetchone()
        total_qs = total_q_row["cnt"] if total_q_row else 0

    poll_rows = conn.execute(
        """
        SELECT ca.user_id, ca.user_name,
               SUM(CASE WHEN ca.is_correct = 1 THEN q.pos_marks ELSE -q.neg_marks END) as poll_score,
               SUM(CASE WHEN ca.is_correct = 1 THEN 1 ELSE 0 END) as correct_cnt,
               COUNT(ca.id) as attempted_cnt
        FROM channel_answers ca
        JOIN questions q ON ca.question_id = q.id
        WHERE ca.exam_id = ?
        GROUP BY ca.user_id
        """,
        (exam_id,),
    ).fetchall()

    dm_rows = conn.execute(
        """
        SELECT user_id, user_name, MAX(score) as dm_score,
               MIN(total_time_sec) as time_sec, MAX(correct_answers) as correct_cnt
        FROM student_results
        WHERE exam_id = ?
        GROUP BY user_id
        """,
        (exam_id,),
    ).fetchall()
    conn.close()

    combined = {}
    for r in poll_rows:
        uid = r["user_id"]
        combined[uid] = {
            "name": r["user_name"] or "Student",
            "score": round(float(r["poll_score"] or 0.0), 2),
            "correct": r["correct_cnt"] or 0,
            "attempted": r["attempted_cnt"] or 0,
        }

    for r in dm_rows:
        uid = r["user_id"]
        dm_score = round(float(r["dm_score"] or 0.0), 2)
        if uid not in combined or dm_score >= combined[uid]["score"]:
            combined[uid] = {
                "name": r["user_name"] or "Student",
                "score": dm_score,
                "correct": r["correct_cnt"] or 0,
                "attempted": total_qs,
            }

    sorted_users = sorted(
        combined.values(),
        key=lambda x: (x["score"], x["correct"]),
        reverse=True,
    )

    status_badge = "🟢 LIVE QUIZ IN CHANNEL" if exam and exam["status"] == "LIVE" else "✅ EXAM COMPLETED"
    if current_q_idx is not None:
        status_badge = f"🟢 LIVE (Q {current_q_idx}/{total_qs})"

    subj = exam["subject"] if exam else "Quiz"
    topc = exam["topic"] if exam else ""

    text = (
        "🏆 ═══════════════════════ 🏆\n"
        "📊 *LIVE CHANNEL SCOREBOARD* 📊\n"
        "🏆 ═══════════════════════ 🏆\n\n"
        f"📚 *Subject:* `{subj}` ({topc})\n"
        f"📡 *Status:* `{status_badge}`\n"
        f"❓ *Total Questions:* `{total_qs}`\n"
        f"👥 *Total Ranked Students:* `{len(sorted_users)}`\n\n"
    )

    if not sorted_users:
        text += (
            "⏳ _Waiting for students to answer..._\n"
            "_Jaise hi koi student answer dega, uski Live Rank yahan update ho jayegi!_"
        )
    else:
        text += "🥇 *LIVE RANKING LIST:*\n"
        medals = ["🥇", "🥈", "🥉"]
        for idx, u in enumerate(sorted_users[:30], 1):
            prefix = medals[idx - 1] if idx <= 3 else f"#{idx}"
            safe_name = str(u["name"]).replace("*", "").replace("_", "").replace("`", "")
            text += (
                f"{prefix} *{safe_name}* — `{u['score']} pts` "
                f"(✅ {u['correct']}/{u['attempted']})\n"
            )

    now_str = datetime.now(IST).strftime("%I:%M:%S %p")
    text += f"\n🔄 _Last Auto-Update: {now_str}_"
    return text


async def update_channel_scoreboard(exam_id: int, current_q_idx: int = None, total_qs: int = None):
    target_channel = get_target_channel()
    if not target_channel or not application:
        return

    text = await build_scoreboard_text(exam_id, current_q_idx, total_qs)

    conn = get_conn()
    row = conn.execute("SELECT scoreboard_msg_id FROM exams WHERE id=?", (exam_id,)).fetchone()
    msg_id = row["scoreboard_msg_id"] if row and row["scoreboard_msg_id"] else 0
    conn.close()

    bot_info = await application.bot.get_me()
    start_link = f"https://t.me/{bot_info.username}?start=exam_{exam_id}"
    keyboard = [
        [InlineKeyboardButton("📝 Re-Attempt / Get Your Rank on Board", url=start_link)],
        [InlineKeyboardButton("🔄 Refresh Live Rank", callback_data=f"refresh_sb_{exam_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if msg_id:
        try:
            await application.bot.edit_message_text(
                chat_id=target_channel,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            if "Message is not modified" in str(e):
                return
            logger.warning("Could not edit scoreboard message, sending new: %s", e)

    try:
        msg = await application.bot.send_message(
            chat_id=target_channel,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        conn = get_conn()
        conn.execute("UPDATE exams SET scoreboard_msg_id=? WHERE id=?", (msg.message_id, exam_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to send scoreboard message to channel %s: %s", target_channel, e)


# ---------------------------
# Bot handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if args and args[0].startswith("exam_"):
        exam_id = int(args[0].split("_")[1])
        if await ensure_channel_access(update, context):
            await start_student_exam(update, context, exam_id)
        return

    await update.message.reply_text(
        "🚀 *WELCOME TO THE QUIZ BOT* 🚀\n\n"
        f"👋 Namaste {user.first_name}!\n\n"
        "📚 Live Channel Quiz & Ranking Bot में आपका स्वागत है।\n\n"
        "⚙️ Admin Panel: `/admin`",
        parse_mode="Markdown",
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_drafts[user_id] = {}
    await update.message.reply_text(
        build_admin_message(),
        reply_markup=admin_keyboard(),
        parse_mode="Markdown",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    draft = admin_drafts.get(user_id)

    if not draft or draft.get("action") != "create" or draft.get("step") != "file":
        await update.message.reply_text("⚠️ कृपया पहले /admin से प्रक्रिया शुरू करें।")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith((".docx", ".doc")):
        await update.message.reply_text("❌ कृपया केवल `.docx` या `.doc` फ़ाइल ही भेजें।")
        return

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()
    parsed_qs = parse_docx_content(file_bytes)

    if not parsed_qs:
        await update.message.reply_text("❌ फ़ाइल से प्रश्न नहीं पढ़े। फॉर्मेट जांचें।")
        return

    scheduled_time = f"{draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']}"
    timer_sec = get_timer_sec()

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO exams (
            subject, topic, sub_topic, time_per_q, scheduled_time, status,
            poll_type, is_anonymous, allow_multiple_answers, shuffle_options,
            question_timer, channel_lock
        )
        VALUES (?, ?, ?, ?, ?, 'SCHEDULED', ?, ?, ?, ?, ?, ?)
        """,
        (
            draft["subject"],
            draft["topic"],
            draft["sub_topic"],
            timer_sec,
            scheduled_time,
            "quiz" if is_quiz_mode() else "regular",
            0 if is_show_voter_names() else 1,
            1 if is_multiple_allowed() else 0,
            1 if is_shuffle_enabled() else 0,
            timer_sec,
            1 if is_channel_lock_enabled() else 0,
        ),
    )
    exam_id = cursor.lastrowid

    for q in parsed_qs:
        opts = q["options"] + ["", "", "", ""]
        cursor.execute(
            """
            INSERT INTO questions (
                exam_id, question_text, opt_a, opt_b, opt_c, opt_d,
                correct_opt_id, pos_marks, neg_marks, solution, explanation
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exam_id,
                q["question"],
                opts[0],
                opts[1],
                opts[2],
                opts[3],
                q["correct"],
                q["pos_marks"],
                q["neg_marks"],
                q["solution"],
                q["solution"],
            ),
        )

    conn.commit()
    conn.close()

    success, _ = schedule_exam_job(exam_id, scheduled_time)
    if not success:
        await update.message.reply_text("⚠️ डेट फॉर्मेट सही नहीं है (Format: YYYY-MM-DD)। फिर से try करें।")
        return

    await post_advance_announcement(
        exam_id,
        draft["subject"],
        draft["topic"],
        draft["sub_topic"],
        scheduled_time,
        len(parsed_qs),
    )

    admin_drafts.pop(user_id, None)
    keyboard = [
        [InlineKeyboardButton(f"▶️ Abhi Channel Par Live Karein (Exam #{exam_id})", callback_data=f"startnow_{exam_id}")]
    ]
    await update.message.reply_text(
        "✅ *EXAM CREATED SUCCESSFULLY* ✅\n\n"
        f"📚 Subject: `{draft['subject']}`\n"
        f"📖 Topic: `{draft['topic']}`\n"
        f"📑 Sub-Topic: `{draft['sub_topic']}`\n"
        f"⏰ Schedule: `{scheduled_time}`\n"
        f"❓ Total Questions: `{len(parsed_qs)}`\n"
        f"⏱️ Channel Question Gap: `{timer_sec} sec`\n\n"
        "👇 Agar aap abhi turant channel par quiz post karna chahte hain toh niche button dabayein:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def ensure_channel_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_channel_lock_enabled():
        return True

    target_channel = get_target_channel()
    if not target_channel:
        return True

    try:
        member = await application.bot.get_chat_member(target_channel, update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        pass

    channel_link = f"https://t.me/{TELEGRAM_CHANNEL_USERNAME.lstrip('@')}" if TELEGRAM_CHANNEL_USERNAME else "https://t.me"
    keyboard = [[InlineKeyboardButton("🔔 Join Channel First", url=channel_link)]]
    msg_target = update.message or update.callback_query.message
    await msg_target.reply_text(
        "🚫 *Access Restricted*\n\nQuiz dene ke liye pehle hamara Telegram Channel join karein.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return False


async def post_advance_announcement(exam_id, subject, topic, sub_topic, sched_time, total_q):
    target_channel = get_target_channel()
    if not target_channel or not application:
        return

    share_msg = (
        f"📢 *UPCOMING LIVE CHANNEL QUIZ*\n\n"
        f"📚 *Subject:* {subject}\n"
        f"📖 *Topic:* {topic}\n"
        f"📑 *Sub-Topic:* {sub_topic}\n"
        f"⏰ *Exam Time:* {sched_time}\n"
        f"❓ *Total Questions:* {total_q}\n\n"
        f"🔥 Join the channel for Live Quiz!"
    )
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"
    keyboard = [[InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)]]

    text = (
        "⚡ ═══════════════════════ ⚡\n"
        "📢 UPCOMING LIVE CHANNEL QUIZ 📢\n"
        "⚡ ═══════════════════════ ⚡\n\n"
        f"📚 Subject: `{subject}`\n"
        f"📖 Topic: `{topic}`\n"
        f"📑 Sub-Topic: `{sub_topic}`\n\n"
        f"⏰ Quiz Time: `{sched_time}`\n"
        f"❓ Total Questions: `{total_q}`\n"
        f"⏱️ Time per Question: `{get_timer_sec()} sec`\n"
        f"🏆 Scoreboard: `Live on Channel`\n\n"
        "👇 *Taiyar rahein! Quiz isi channel par post hoga.*"
    )

    try:
        msg = await application.bot.send_message(
            chat_id=target_channel,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        try:
            await application.bot.pin_chat_message(
                chat_id=target_channel,
                message_id=msg.message_id,
                disable_notification=False,
            )
        except Exception:
            pass
    except Exception as e:
        logger.exception("Error in post_advance_announcement: %s", e)


async def post_exam_to_channel(exam_id):
    target_channel = get_target_channel()
    if not target_channel or not application:
        logger.error("post_exam_to_channel aborted: target_channel is missing!")
        return

    conn = get_conn()
    exam = conn.execute(
        "SELECT subject, topic, sub_topic, time_per_q, question_timer FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    questions = conn.execute(
        "SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution FROM questions WHERE exam_id=? ORDER BY id",
        (exam_id,),
    ).fetchall()
    conn.execute("UPDATE exams SET status='LIVE' WHERE id=?", (exam_id,))
    conn.commit()
    conn.close()

    if not exam or not questions:
        logger.error("No exam or questions found for exam_id=%s", exam_id)
        return

    delay_sec = int(exam["question_timer"] or exam["time_per_q"] or 30)

    share_msg = (
        f"🔥 *LIVE CHANNEL QUIZ STARTED!*\n\n"
        f"📚 *Subject:* {exam['subject']}\n"
        f"📖 *Topic:* {exam['topic']}\n"
        f"📑 *Sub-Topic:* {exam['sub_topic']}\n"
        f"❓ *Questions:* {len(questions)}\n"
        f"⏱ *Timer:* {delay_sec} sec / Q\n\n"
        "Join Channel Now!"
    )
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

    announcement = (
        "⚡ ═══════════════════════ ⚡\n"
        "🚀 LIVE CHANNEL QUIZ STARTED! 🚀\n"
        "⚡ ═══════════════════════ ⚡\n\n"
        f"📚 Subject: `{exam['subject']}`\n"
        f"📖 Topic: `{exam['topic']}`\n"
        f"📑 Sub-Topic: `{exam['sub_topic']}`\n"
        f"❓ Total Questions: `{len(questions)}`\n"
        f"⏱️ Har Question ka Samay: `{delay_sec} sec`\n"
        "🏆 Scoreboard: `Live Updating in Channel`\n\n"
        "👇 *Abhi niche har 30 second mein ek naya Quiz Poll aayega!*"
    )

    keyboard = [[InlineKeyboardButton("📲 Share Quiz to WhatsApp", url=wa_url)]]

    try:
        msg = await application.bot.send_message(
            chat_id=target_channel,
            text=announcement,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        try:
            await application.bot.pin_chat_message(
                chat_id=target_channel,
                message_id=msg.message_id,
                disable_notification=False,
            )
        except Exception:
            pass
    except Exception as e:
        logger.exception("Error sending start message to channel %s: %s", target_channel, e)
        return

    # 1. Channel par Live Scoreboard post karna
    await update_channel_scoreboard(exam_id, 1, len(questions))

    # 2. Channel par ek-ek karke 30 sec (delay_sec) ke gap par saare Quiz Polls post karna
    total_qs = len(questions)
    for idx, q in enumerate(questions, 1):
        await send_channel_poll(exam_id, q["id"], idx, total_qs, delay_sec)
        await update_channel_scoreboard(exam_id, idx, total_qs)
        if idx < total_qs:
            await asyncio.sleep(delay_sec)

    conn = get_conn()
    conn.execute("UPDATE exams SET status='COMPLETED' WHERE id=?", (exam_id,))
    # Final Scoreboard ko sabse niche naye message ke roop mein post karne ke liye msg_id reset
    conn.execute("UPDATE exams SET scoreboard_msg_id=0 WHERE id=?", (exam_id,))
    conn.commit()
    conn.close()

    await update_channel_scoreboard(exam_id)


async def send_channel_poll(exam_id, question_id, q_num=1, total_qs=1, timer_sec=30):
    target_channel = get_target_channel()
    if not target_channel or not application:
        return

    conn = get_conn()
    q = conn.execute(
        "SELECT question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, solution FROM questions WHERE id=? AND exam_id=?",
        (question_id, exam_id),
    ).fetchone()
    conn.close()

    if not q:
        return

    raw_options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    indexed_options = [(i, str(opt)[:100]) for i, opt in enumerate(raw_options) if opt and str(opt).strip()]

    if len(indexed_options) < 2:
        logger.warning("Question %s has fewer than 2 options, skipping.", question_id)
        return

    correct_orig_idx = int(q["correct_opt_id"] or 0)
    if is_shuffle_enabled():
        random.shuffle(indexed_options)

    options = [opt for _, opt in indexed_options]
    new_correct_idx = 0
    for new_i, (orig_i, _) in enumerate(indexed_options):
        if orig_i == correct_orig_idx:
            new_correct_idx = new_i
            break

    is_anonymous = not is_show_voter_names()
    allow_multiple = is_multiple_allowed()
    quiz_mode = is_quiz_mode()
    if quiz_mode:
        allow_multiple = False

    explanation = q["solution"] if (is_explanation_enabled() and q["solution"]) else None
    open_period = timer_sec if (is_limit_duration_enabled() and 5 <= timer_sec <= 600) else None

    question_header = f"[Q {q_num}/{total_qs}] {q['question_text']}"

    poll = None
    try:
        # Pehle user setting ke hisaab se poll bhejne ki koshish karein
        poll = await application.bot.send_poll(
            chat_id=target_channel,
            question=question_header[:300],
            options=options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allow_multiple,
            type="quiz" if quiz_mode else "regular",
            correct_option_id=new_correct_idx if quiz_mode else None,
            explanation=explanation[:200] if explanation else None,
            open_period=open_period,
        )
    except Exception as e:
        # Agar Broadcast Channel ki wajah se non-anonymous poll fail ho, toh turant Anonymous Quiz Poll bhejein!
        logger.warning("Non-anonymous poll rejected by channel (%s). Retrying with is_anonymous=True...", e)
        try:
            poll = await application.bot.send_poll(
                chat_id=target_channel,
                question=question_header[:300],
                options=options,
                is_anonymous=True,
                allows_multiple_answers=allow_multiple,
                type="quiz" if quiz_mode else "regular",
                correct_option_id=new_correct_idx if quiz_mode else None,
                explanation=explanation[:200] if explanation else None,
                open_period=open_period,
            )
        except Exception as e2:
            logger.exception("Critical error sending poll to channel %s: %s", target_channel, e2)
            return

    if poll:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO channel_polls (exam_id, question_id, poll_id, correct_opt_id) VALUES (?, ?, ?, ?)",
            (exam_id, question_id, poll.poll.id, new_correct_idx),
        )
        conn.commit()
        conn.close()


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    user = poll_answer.user
    user_id = user.id
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Student"
    selected = poll_answer.option_ids

    conn = get_conn()
    row = conn.execute(
        "SELECT exam_id, question_id, correct_opt_id FROM channel_polls WHERE poll_id = ?",
        (poll_id,),
    ).fetchone()
    if not row:
        conn.close()
        return

    exam_id = row["exam_id"]
    question_id = row["question_id"]
    correct_opt = int(row["correct_opt_id"])

    if not selected:
        conn.execute(
            "DELETE FROM channel_answers WHERE exam_id=? AND question_id=? AND user_id=?",
            (exam_id, question_id, user_id),
        )
        conn.commit()
        conn.close()
        await update_channel_scoreboard(exam_id)
        return

    selected_option = ",".join(str(i) for i in selected)
    is_correct = 1 if int(selected[0]) == correct_opt else 0

    existing = conn.execute(
        "SELECT id FROM channel_answers WHERE exam_id=? AND question_id=? AND user_id=?",
        (exam_id, question_id, user_id),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE channel_answers SET selected_option=?, is_correct=?, user_name=? WHERE id=?",
            (selected_option, is_correct, full_name, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO channel_answers (exam_id, question_id, poll_id, user_id, user_name, selected_option, is_correct) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                exam_id,
                question_id,
                poll_id,
                user_id,
                full_name,
                selected_option,
                is_correct,
            ),
        )
    conn.commit()
    conn.close()

    await update_channel_scoreboard(exam_id)


# ---------------------------
# Student Re-Attempt Flow (For Live Scoreboard Ranking)
# ---------------------------
async def start_student_exam(update: Update, context: ContextTypes.DEFAULT_TYPE, exam_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, explanation FROM questions WHERE exam_id=? ORDER BY id",
        (exam_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("⚠️ इस एग्जाम में कोई प्रश्न उपलब्ध नहीं हैं।")
        return

    context.user_data["active_exam"] = {
        "exam_id": exam_id,
        "questions": [dict(r) for r in rows],
        "current_index": 0,
        "score": 0.0,
        "correct_count": 0,
        "start_time": datetime.now(),
        "q_token": 0,
    }

    await send_next_student_question(update, context)


async def auto_advance_dm_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, expected_idx: int, token: int, delay: int):
    await asyncio.sleep(delay)
    exam_data = context.user_data.get("active_exam")
    if not exam_data:
        return
    if exam_data.get("current_index") == expected_idx and exam_data.get("q_token") == token:
        exam_data["current_index"] += 1
        exam_data["q_token"] += 1
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ *Time Up ({delay}s)!* Moving to next question...",
                parse_mode="Markdown",
            )
            await send_next_student_question_by_chat(chat_id, user_id, context)
        except Exception as e:
            logger.warning("Auto advance error: %s", e)


async def send_next_student_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user or update.callback_query.from_user
    chat_id = update.effective_chat.id
    await send_next_student_question_by_chat(chat_id, user.id, context, update)


async def send_next_student_question_by_chat(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Update = None):
    exam_data = context.user_data.get("active_exam")
    if not exam_data:
        return

    idx = exam_data["current_index"]
    qs = exam_data["questions"]

    if idx >= len(qs):
        end_time = datetime.now()
        total_time = int((end_time - exam_data["start_time"]).total_seconds())
        final_score = round(exam_data["score"], 2)
        correct_cnt = exam_data.get("correct_count", 0)

        user_obj = (update.effective_user or update.callback_query.from_user) if update else None
        full_name = f"{user_obj.first_name or ''} {user_obj.last_name or ''}".strip() if user_obj else "Student"

        conn = get_conn()
        conn.execute(
            "INSERT INTO student_results (exam_id, user_id, user_name, score, total_time_sec, total_questions, correct_answers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (exam_data["exam_id"], user_id, full_name, final_score, total_time, len(qs), correct_cnt),
        )
        conn.commit()
        conn.close()

        exam_id = exam_data["exam_id"]
        context.user_data.pop("active_exam", None)

        await update_channel_scoreboard(exam_id)

        result_text = (
            "🎉 *TEST COMPLETED!* 🎉\n\n"
            f"👤 Name: `{full_name}`\n"
            f"📊 Score: `{final_score}`\n"
            f"✅ Correct: `{correct_cnt}/{len(qs)}`\n"
            f"⏱️ Time: `{total_time} sec`\n\n"
            "🏆 *Aapka Naam aur Rank Channel ke Live Scoreboard par update ho gaya hai!*"
        )

        if update and update.callback_query:
            await update.callback_query.message.edit_text(result_text, parse_mode="Markdown")
        else:
            await application.bot.send_message(chat_id=chat_id, text=result_text, parse_mode="Markdown")
        return

    q = qs[idx]
    options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    options = [o for o in options if o]

    keyboard = []
    for o_idx, o_text in enumerate(options):
        keyboard.append([
            InlineKeyboardButton(f"{chr(65 + o_idx)}) {o_text}", callback_data=f"ans_{o_idx}_{idx}")
        ])

    timer_sec = get_timer_sec()
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🎯 *QUIZ QUESTION* 🎯\n\n"
        f"📌 Q {idx + 1}/{len(qs)}\n"
        f"⏱️ Time Limit: `{timer_sec} sec`\n\n"
        f"{q['question_text']}"
    )

    exam_data["q_token"] = exam_data.get("q_token", 0) + 1
    current_token = exam_data["q_token"]

    if update and update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")

    if is_limit_duration_enabled():
        asyncio.create_task(
            auto_advance_dm_question(context, chat_id, user_id, idx, current_token, timer_sec)
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "admin_test_channel":
        target = get_target_channel()
        if not target:
            await query.edit_message_text(
                "❌ **Channel Set Nahi Hai!**\n\nRender Environment Variables mein `TELEGRAM_CHANNEL_ID` (jaise `-100xxxxxxxxxx`) ya `TELEGRAM_CHANNEL_USERNAME` (jaise `@mychannel`) add karein.",
                parse_mode="Markdown",
            )
            return
        try:
            test_poll = await application.bot.send_poll(
                chat_id=target,
                question="🔔 [Test Quiz] Kya Channel par Quiz Sahi se Post ho raha hai?",
                options=["Haan, 100% Working!", "Bahut Badhiya"],
                is_anonymous=True,
                type="quiz",
                correct_option_id=0,
                open_period=30,
            )
            await query.edit_message_text(
                f"✅ **Success!** Channel (`{target}`) par 30 sec timer ke sath Test Quiz Poll post ho gaya hai! Ab aapke saare exams channel par aayenge.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Channel Par Post Fail Hua!**\n\nTarget: `{target}`\nError: `{e}`\n\n⚠️ *Check karein ki Bot aapke Channel mein ADMIN hai aur uske paas 'Post Messages' ki permission ON hai.*",
                parse_mode="Markdown",
            )
        return

    if data.startswith("refresh_sb_"):
        exam_id = int(data.split("_")[-1])
        await update_channel_scoreboard(exam_id)
        return

    if data == "admin_create_exam":
        admin_drafts[user_id] = {"action": "create", "step": "subject"}
        await query.edit_message_text("📝 **Subject का नाम लिखें:**", parse_mode="Markdown")
        return

    if data == "admin_view_exams":
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, subject, topic, scheduled_time, status FROM exams ORDER BY id DESC LIMIT 5"
        ).fetchall()
        conn.close()

        if not rows:
            await query.edit_message_text("📋 कोई Scheduled Exam नहीं है।")
            return

        keyboard = []
        text = "📋 **Recent / Scheduled Exams:**\n\n"
        for r in rows:
            text += f"• **Exam #{r['id']}**: {r['subject']} - {r['topic']} (`{r['scheduled_time']}`) [{r['status']}]\n"
            keyboard.append([
                InlineKeyboardButton(f"⏰ Reschedule #{r['id']}", callback_data=f"resched_{r['id']}"),
                InlineKeyboardButton(f"📢 Post to Channel #{r['id']}", callback_data=f"startnow_{r['id']}"),
            ])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("startnow_"):
        exam_id = int(data.split("_")[1])
        target = get_target_channel()
        await query.edit_message_text(
            f"🚀 **Exam #{exam_id} को चैनल (`{target}`) पर लाइव किया जा रहा है!**\n\nहर `{get_timer_sec()} sec` में एक-एक करके सारे सवाल चैनल पर पोस्ट होंगे।",
            parse_mode="Markdown",
        )
        trigger_channel_exam_background(exam_id)
        return

    if data.startswith("resched_"):
        exam_id = int(data.split("_")[1])
        admin_drafts[user_id] = {"action": "reschedule_date", "exam_id": exam_id}
        await query.edit_message_text(
            f"📅 **Exam #{exam_id} के लिए नई तारीख लिखें**\n\nFormat: `YYYY-MM-DD`",
            parse_mode="Markdown",
        )
        return

    if data == "toggle_limit_duration":
        if not is_limit_duration_enabled():
            set_setting("limit_duration", "1")
            set_setting("timer_sec", "30")
        else:
            cur_t = get_timer_sec()
            if cur_t == 30:
                set_setting("timer_sec", "45")
            elif cur_t == 45:
                set_setting("timer_sec", "60")
            else:
                set_setting("limit_duration", "0")
                set_setting("timer_sec", "30")
        await query.edit_message_text(build_admin_message(), reply_markup=admin_keyboard(), parse_mode="Markdown")
        return

    if data.startswith("toggle_"):
        key_map = {
            "toggle_show_voter_names": "show_voter_names",
            "toggle_multiple_answers": "allow_multiple_answers",
            "toggle_shuffle_options": "shuffle_options",
            "toggle_quiz_mode": "quiz_mode",
            "toggle_explanations": "show_explanations",
            "toggle_channel_lock": "channel_lock",
        }
        key = key_map.get(data)
        if key:
            current = get_setting(key, "0")
            new_value = "1" if current != "1" else "0"
            set_setting(key, new_value)
            await query.edit_message_text(build_admin_message(), reply_markup=admin_keyboard(), parse_mode="Markdown")
        return

    if data in ["set_am", "set_pm"]:
        ampm_val = "AM" if data == "set_am" else "PM"
        draft = admin_drafts.get(user_id)
        if not draft:
            return

        if draft.get("action") == "create":
            draft["ampm"] = ampm_val
            draft["step"] = "file"
            await query.edit_message_text(
                f"📁 **Time Set**: `{draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']}`\n\nAb apni `.docx` file bhejein.",
                parse_mode="Markdown",
            )
            return

        if draft.get("action") in ["reschedule_date", "reschedule_ampm"]:
            draft["ampm"] = ampm_val
            exam_id = int(draft.get("exam_id", 0))
            if exam_id:
                scheduled_time = build_schedule_string(
                    draft.get("date", ""),
                    draft.get("hour", "00"),
                    draft.get("minute", "00"),
                    ampm_val,
                )
                if scheduled_time and update_exam_schedule(exam_id, scheduled_time):
                    schedule_exam_job(exam_id, scheduled_time)
                    await query.edit_message_text(
                        f"✅ *Exam #{exam_id} rescheduled successfully*\n\n⏰ New Time: `{scheduled_time}`",
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        "❌ Reschedule failed. Please retry with proper date format.",
                        parse_mode="Markdown",
                    )
            admin_drafts.pop(user_id, None)
            return

    if data.startswith("ans_"):
        parts = data.split("_")
        ans_idx = int(parts[1])
        q_idx = int(parts[2])

        exam_data = context.user_data.get("active_exam")
        if exam_data and exam_data["current_index"] == q_idx:
            current_q = exam_data["questions"][q_idx]
            correct_idx = int(current_q["correct_opt_id"])
            pos_m = float(current_q["pos_marks"])
            neg_m = float(current_q["neg_marks"])

            if ans_idx == correct_idx:
                exam_data["score"] += pos_m
                exam_data["correct_count"] = exam_data.get("correct_count", 0) + 1
            else:
                exam_data["score"] -= neg_m

            exam_data["current_index"] += 1
            await send_next_student_question(update, context)
        return


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if user_id not in admin_drafts:
        return

    draft = admin_drafts[user_id]
    action = draft.get("action")

    if action == "reschedule_date":
        draft["date"] = text
        draft["action"] = "reschedule_hour"
        await update.message.reply_text("⏰ **घंटा (Hour) लिखें** (जैसे: 02 या 08):", parse_mode="Markdown")
        return

    if action == "reschedule_hour":
        draft["hour"] = text.zfill(2)
        draft["action"] = "reschedule_min"
        await update.message.reply_text("⏱ **मिनट (Minute) लिखें** (जैसे: 00 या 30):", parse_mode="Markdown")
        return

    if action == "reschedule_min":
        draft["minute"] = text.zfill(2)
        draft["action"] = "reschedule_ampm"
        keyboard = [[
            InlineKeyboardButton("🌅 AM", callback_data="set_am"),
            InlineKeyboardButton("🌇 PM", callback_data="set_pm"),
        ]]
        await update.message.reply_text("☀️ **AM या PM चुनें:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    step = draft.get("step")
    if step == "subject":
        draft["subject"] = text
        draft["step"] = "topic"
        await update.message.reply_text("📖 **Topic का नाम लिखें:**", parse_mode="Markdown")
    elif step == "topic":
        draft["topic"] = text
        draft["step"] = "sub_topic"
        await update.message.reply_text("📑 **Sub-Topic का नाम लिखें:**", parse_mode="Markdown")
    elif step == "sub_topic":
        draft["sub_topic"] = text
        draft["step"] = "date"
        await update.message.reply_text("📅 **तारीख (Date) लिखें** (Format: YYYY-MM-DD):", parse_mode="Markdown")
    elif step == "date":
        draft["date"] = text
        draft["step"] = "hour"
        await update.message.reply_text("⏰ **घंटा (Hour) लिखें** (उदा: 02 या 08):", parse_mode="Markdown")
    elif step == "hour":
        draft["hour"] = text.zfill(2)
        draft["step"] = "minute"
        await update.message.reply_text("⏱ **मिनट (Minute) लिखें** (उदा: 00 या 30):", parse_mode="Markdown")
    elif step == "minute":
        draft["minute"] = text.zfill(2)
        draft["step"] = "ampm"
        keyboard = [[
            InlineKeyboardButton("🌅 AM", callback_data="set_am"),
            InlineKeyboardButton("🌇 PM", callback_data="set_pm"),
        ]]
        await update.message.reply_text("☀️ **AM या PM चुनें:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ---------------------------
# Webhook support
# ---------------------------
def dispatch_update(update_json):
    global application, bot_loop
    if not application or not bot_loop or not bot_loop.is_running():
        return False

    async def process_update():
        update = Update.de_json(update_json, application.bot)
        await application.process_update(update)

    try:
        asyncio.run_coroutine_threadsafe(process_update(), bot_loop)
        return True
    except Exception:
        logger.exception("Webhook update processing failed")
        return False


@app.route("/")
def home():
    return "Pro Quiz Bot is Active!"


@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    dispatch_update(update_json)
    return "OK", 200


async def main_async():
    global application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_BOT_TOKEN}"
        await application.bot.set_webhook(url=webhook_url)
        logger.info("Webhook connected successfully: %s", webhook_url)
    else:
        logger.warning("RENDER_EXTERNAL_URL is empty; starting polling mode.")
        await application.updater.start_polling()

    scheduler.start()
    restore_jobs()
    await application.start()

    while True:
        await asyncio.sleep(3600)


def restore_jobs():
    try:
        conn = get_conn()
        rows = conn.execute("SELECT id, scheduled_time FROM exams WHERE status='SCHEDULED'").fetchall()
        conn.close()

        now = datetime.now(IST)
        for row in rows:
            exam_id = row["id"]
            scheduled_time = row["scheduled_time"]
            try:
                ct = datetime.strptime(scheduled_time.strip(), "%Y-%m-%d %I:%M %p")
                run_time = IST.localize(ct)
                if run_time > now:
                    scheduler.add_job(
                        post_exam_to_channel,
                        "date",
                        run_date=run_time,
                        args=[exam_id],
                        id=f"exam_job_{exam_id}",
                        replace_existing=True,
                    )
            except Exception as e:
                logger.exception("Could not restore job #%s: %s", exam_id, e)
    except Exception as e:
        logger.exception("restore_jobs failed: %s", e)


if __name__ == "__main__":
    def run_bot():
        global bot_loop
        bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop)
        bot_loop.run_until_complete(main_async())

    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT)
