import asyncio
import html
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
    """सही Channel ID या @username रिटर्न करता है।"""
    if RAW_CHANNEL_ID:
        if RAW_CHANNEL_ID.lstrip("-").isdigit():
            if not RAW_CHANNEL_ID.startswith("-"):
                return int(f"-100{RAW_CHANNEL_ID}")
            return int(RAW_CHANNEL_ID)
        return RAW_CHANNEL_ID if RAW_CHANNEL_ID.startswith("@") else f"@{RAW_CHANNEL_ID}"
    if TELEGRAM_CHANNEL_USERNAME:
        return TELEGRAM_CHANNEL_USERNAME if TELEGRAM_CHANNEL_USERNAME.startswith("@") else f"@{TELEGRAM_CHANNEL_USERNAME}"
    return None


# ---------------------------
# Flask + scheduler + Locks
# ---------------------------
app = Flask(__name__)
scheduler = AsyncIOScheduler(timezone=IST)
application = None
bot_loop = None

# डुप्लीकेट क्विज़ और रिपीट रोकने के लिए ग्लोबल ट्रैकर
running_channel_exams = set()
channel_poll_events = {}  # {exam_id: asyncio.Event()}
active_student_locks = set()  # एक साथ 2 सवाल आने से रोकने के लिए

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
# DOCX parser (Supports Multi-line Long Questions)
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
    reading_question = False

    for raw_line in lines:
        line = raw_line.strip()
        lower = line.lower()

        if lower.startswith("question") or lower.startswith("q:") or lower.startswith("q.") or lower.startswith("प्रश्न"):
            if current_q and len(options) >= 2:
                questions.append(
                    {
                        "question": current_q.strip(),
                        "options": options[:4],
                        "correct": correct_idx,
                        "pos_marks": pos_m,
                        "neg_marks": neg_m,
                        "solution": sol.strip(),
                    }
                )

            if ":" in line:
                current_q = line.split(":", 1)[-1].strip()
            elif "." in line and lower.startswith("q."):
                current_q = line.split(".", 1)[-1].strip()
            else:
                current_q = line

            options = []
            correct_idx = 0
            pos_m = 1.0
            neg_m = 0.0
            sol = ""
            reading_question = True

        elif any(
            lower.startswith(prefix)
            for prefix in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]
        ):
            reading_question = False
            for prefix in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]:
                if lower.startswith(prefix):
                    opt_val = line[len(prefix):].strip()
                    break
            else:
                opt_val = line
            options.append(opt_val if opt_val else line)

        elif lower.startswith("answer") or lower.startswith("ans") or lower.startswith("उत्तर"):
            reading_question = False
            ans_str = line.split(":", 1)[-1].strip().lower() if ":" in line else line.lower()
            if "a" in ans_str or "1" in ans_str or "अ" in ans_str:
                correct_idx = 0
            elif "b" in ans_str or "2" in ans_str or "ब" in ans_str:
                correct_idx = 1
            elif "c" in ans_str or "3" in ans_str or "स" in ans_str:
                correct_idx = 2
            elif "d" in ans_str or "4" in ans_str or "द" in ans_str:
                correct_idx = 3

        elif lower.startswith("solution") or lower.startswith("explanation") or lower.startswith("व्याख्या"):
            reading_question = False
            sol = line.split(":", 1)[-1].strip() if ":" in line else line

        elif "positive marks" in lower:
            reading_question = False
            try:
                pos_m = float(line.split(":", 1)[-1].strip())
            except Exception:
                pos_m = 1.0

        elif "negative marks" in lower:
            reading_question = False
            try:
                neg_m = float(line.split(":", 1)[-1].strip())
            except Exception:
                neg_m = 0.0

        else:
            # अगर सवाल बड़ा है और अगली लाइन या पैराग्राफ में जा रहा है तो उसे कटने न दें, जोड़ दें
            if reading_question and current_q is not None:
                current_q += "\n" + line
            elif sol:
                sol += "\n" + line

    if current_q and len(options) >= 2:
        questions.append(
            {
                "question": current_q.strip(),
                "options": options[:4],
                "correct": correct_idx,
                "pos_marks": pos_m,
                "neg_marks": neg_m,
                "solution": sol.strip(),
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
        f"⏳ Question Timer: `{get_timer_sec()} sec` _(Overtime: 🔴 -1s, -2s...)_\n"
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
    """शेड्यूल हुए जॉब को हटाकर तुरंत एक ही बार चैनल पर एग्जाम शुरू करता है।"""
    global bot_loop
    try:
        scheduler.remove_job(f"exam_job_{exam_id}")
    except Exception:
        pass
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
            misfire_grace_time=300,
        )
        return True, local_dt
    except Exception as exc:
        logger.exception("schedule_exam_job failed: %s", exc)
        return False, None


def update_exam_schedule(exam_id: int, scheduled_datetime: str):
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE exams SET scheduled_time = ?, status = 'SCHEDULED' WHERE id = ?",
            (scheduled_datetime, exam_id),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.exception("update_exam_schedule failed: %s", exc)
        return False


# ---------------------------
# Single Pinned / Auto-Delete Live Scoreboard System
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

    status_badge = "🟢 LIVE QUIZ IN PROGRESS" if exam and exam["status"] == "LIVE" else "✅ EXAM ACTIVE"
    if current_q_idx is not None:
        status_badge = f"🟢 LIVE (Q {current_q_idx}/{total_qs})"

    subj = html.escape(str(exam["subject"])) if exam else "Quiz"
    topc = html.escape(str(exam["topic"])) if exam else ""

    text = (
        "🏆 ═══════════════════════ 🏆\n"
        "📊 <b>LIVE CHANNEL SCOREBOARD</b> 📊\n"
        "🏆 ═══════════════════════ 🏆\n\n"
        f"📚 <b>Subject:</b> <code>{subj}</code> ({topc})\n"
        f"📡 <b>Status:</b> <code>{status_badge}</code>\n"
        f"❓ <b>Total Questions:</b> <code>{total_qs}</code>\n"
        f"👥 <b>Total Students Ranked:</b> <code>{len(sorted_users)}</code>\n\n"
    )

    if not sorted_users:
        text += (
            "⏳ <i>Waiting for students to attempt...</i>\n"
            "<i>जैसे ही कोई स्टूडेंट टेस्ट अटेम्प्ट करेगा, उसकी लाइव रैंक यहाँ दिखेगी!</i>"
        )
    else:
        text += "🥇 <b>LIVE RANKING LIST:</b>\n"
        medals = ["🥇", "🥈", "🥉"]
        for idx, u in enumerate(sorted_users[:35], 1):
            prefix = medals[idx - 1] if idx <= 3 else f"#{idx}"
            safe_name = html.escape(str(u["name"]))
            text += (
                f"{prefix} <b>{safe_name}</b> — <code>{u['score']} pts</code> "
                f"(✅ {u['correct']}/{u['attempted']})\n"
            )

    now_str = datetime.now(IST).strftime("%I:%M:%S %p")
    text += f"\n🔄 <i>Auto-Updated: {now_str}</i>"
    return text


async def update_channel_scoreboard(exam_id: int, current_q_idx: int = None, total_qs: int = None, repost_at_bottom: bool = False):
    """
    केवल एक ही लीडरबोर्ड रखेगा।
    - सामान्य अपडेट पर उसी पिन किए हुए मैसेज को Edit करेगा (सवालों के बीच नया मैसेज नहीं भेजेगा)।
    - अगर repost_at_bottom=True है (जैसे क्विज़ के अंत में), तो पुराने लीडरबोर्ड को चैट से Delete करके सबसे नीचे नया भेजेगा और Pin करेगा।
    """
    target_channel = get_target_channel()
    if not target_channel or not application:
        return

    text = await build_scoreboard_text(exam_id, current_q_idx, total_qs)

    conn = get_conn()
    row = conn.execute("SELECT scoreboard_msg_id FROM exams WHERE id=?", (exam_id,)).fetchone()
    old_msg_id = row["scoreboard_msg_id"] if row and row["scoreboard_msg_id"] else 0
    conn.close()

    bot_info = await application.bot.get_me()
    start_link = f"https://t.me/{bot_info.username}?start=exam_{exam_id}"
    keyboard = [
        [InlineKeyboardButton("🚀 Attempt Live Test (With 30s Timer)", url=start_link)],
        [InlineKeyboardButton("🔄 Refresh Live Scoreboard", callback_data=f"refresh_sb_{exam_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # अगर सबसे नीचे लाना है तो पहले पुराने वाले को डिलीट करें ताकि चैट में सिर्फ लास्ट वाला ही दिखे
    if repost_at_bottom and old_msg_id:
        try:
            await application.bot.delete_message(chat_id=target_channel, message_id=old_msg_id)
        except Exception:
            pass
        old_msg_id = 0

    # अगर पहले से लीडरबोर्ड मैसेज मौजूद है, तो उसी को Edit करें (सवालों के बीच नया न भेजें)
    if old_msg_id:
        try:
            await application.bot.edit_message_text(
                chat_id=target_channel,
                message_id=old_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception as e:
            if "Message is not modified" in str(e):
                return
            # अगर किसी कारण एडिट फेल हो तो पुराना डिलीट करने की कोशिश करें
            try:
                await application.bot.delete_message(chat_id=target_channel, message_id=old_msg_id)
            except Exception:
                pass

    # नया लीडरबोर्ड भेजें और उसे पिन करें
    try:
        msg = await application.bot.send_message(
            chat_id=target_channel,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        conn = get_conn()
        conn.execute("UPDATE exams SET scoreboard_msg_id=? WHERE id=?", (msg.message_id, exam_id))
        conn.commit()
        conn.close()
        try:
            await application.bot.pin_chat_message(
                chat_id=target_channel,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception:
            pass
    except Exception as e:
        logger.exception("Failed to send scoreboard message: %s", e)


# ---------------------------
# Channel Live Quiz Flow (No Duplicate, Wait for Attempt + Red Overtime)
# ---------------------------
async def post_exam_to_channel(exam_id: int):
    # 1. डुप्लीकेट/रिपीट रोकने का लॉक
    if exam_id in running_channel_exams:
        logger.info("Exam #%s is already running in channel, skipping duplicate call.", exam_id)
        return

    target_channel = get_target_channel()
    if not target_channel or not application:
        logger.error("post_exam_to_channel aborted: target_channel is missing!")
        return

    conn = get_conn()
    exam = conn.execute(
        "SELECT subject, topic, sub_topic, time_per_q, question_timer, status FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    questions = conn.execute(
        "SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution FROM questions WHERE exam_id=? ORDER BY id",
        (exam_id,),
    ).fetchall()

    if not exam or not questions:
        conn.close()
        return

    # अगर पहले से LIVE चल रहा है तो दोबारा शुरू न करें
    if exam["status"] == "LIVE":
        conn.close()
        return

    running_channel_exams.add(exam_id)
    conn.execute("UPDATE exams SET status='LIVE' WHERE id=?", (exam_id,))
    conn.commit()
    conn.close()

    try:
        delay_sec = int(exam["question_timer"] or exam["time_per_q"] or 30)
        bot_info = await application.bot.get_me()
        start_link = f"https://t.me/{bot_info.username}?start=exam_{exam_id}"

        share_msg = (
            f"🔥 *LIVE QUIZ STARTED!*\n\n"
            f"📚 *Subject:* {exam['subject']}\n"
            f"📖 *Topic:* {exam['topic']}\n"
            f"📑 *Sub-Topic:* {exam['sub_topic']}\n"
            f"❓ *Questions:* {len(questions)}\n"
            f"⏱ *Timer:* {delay_sec} sec / Q\n\n"
            "Join Now!"
        )
        wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

        announcement = (
            "⚡ ═══════════════════════ ⚡\n"
            "🚀 <b>LIVE QUIZ EXAM STARTED!</b> 🚀\n"
            "⚡ ═══════════════════════ ⚡\n\n"
            f"📚 <b>Subject:</b> <code>{html.escape(exam['subject'])}</code>\n"
            f"📖 <b>Topic:</b> <code>{html.escape(exam['topic'])}</code>\n"
            f"📑 <b>Sub-Topic:</b> <code>{html.escape(exam['sub_topic'])}</code>\n"
            f"❓ <b>Total Questions:</b> <code>{len(questions)}</code>\n"
            f"⏱️ <b>Timer per Question:</b> <code>{delay_sec} sec</code>\n"
            "🏆 <b>Scoreboard:</b> <code>Single Pinned Auto-Updating</code>\n\n"
            "👇 <b>नीचे चैनल में या बॉट बटन से लाइव टेस्ट अटेम्प्ट करें:</b>"
        )

        keyboard = [
            [InlineKeyboardButton("🚀 Start Interactive Quiz (30s Timer)", url=start_link)],
            [InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)],
        ]

        await application.bot.send_message(
            chat_id=target_channel,
            text=announcement,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

        # 2. केवल एक बार लीडरबोर्ड बनाएं और पिन कर दें (सवालों के बीच में दोबारा नया नहीं आएगा)
        await update_channel_scoreboard(exam_id, 1, len(questions), repost_at_bottom=True)

        # 3. एक-एक करके सवाल भेजें (जब तक 30s पूरे न हों और अटेम्प्ट न हो, अगला सवाल नहीं आएगा)
        total_qs = len(questions)
        for idx, q in enumerate(questions, 1):
            await run_single_channel_question(exam_id, q, idx, total_qs, delay_sec)

        conn = get_conn()
        conn.execute("UPDATE exams SET status='COMPLETED' WHERE id=?", (exam_id,))
        conn.commit()
        conn.close()

        # 4. सारे सवाल खत्म होने पर पुराना लीडरबोर्ड डिलीट करके सबसे नीचे फाइनल लीडरबोर्ड दिखाएं
        await update_channel_scoreboard(exam_id, repost_at_bottom=True)

    except Exception as e:
        logger.exception("Error in post_exam_to_channel: %s", e)
    finally:
        running_channel_exams.discard(exam_id)
        channel_poll_events.pop(exam_id, None)


async def run_single_channel_question(exam_id: int, q, q_num: int, total_qs: int, timer_sec: int):
    """
    चैनल पर एक सवाल भेजता है:
    - अगर सवाल या ऑप्शन बड़ा है तो बिना कटे पूरा टेक्स्ट दिखाता है।
    - 30 सेकंड काउंटडाउन चलाता है।
    - अगर अटेम्प्ट नहीं हुआ है तो 30 सेकंड बाद 🔴 -1s, -2s, -3s रेड कलर में दिखाता है और अटेम्प्ट होने के बाद ही उत्तर व अगला सवाल आता है।
    """
    target_channel = get_target_channel()
    if not target_channel or not application:
        return

    raw_options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    indexed_options = [(i, str(opt).strip()) for i, opt in enumerate(raw_options) if opt and str(opt).strip()]
    if len(indexed_options) < 2:
        return

    correct_orig_idx = int(q["correct_opt_id"] or 0)
    if is_shuffle_enabled():
        random.shuffle(indexed_options)

    full_options = [opt for _, opt in indexed_options]
    new_correct_idx = 0
    for new_i, (orig_i, _) in enumerate(indexed_options):
        if orig_i == correct_orig_idx:
            new_correct_idx = new_i
            break

    q_text = str(q["question_text"]).strip()
    question_header = f"[Q {q_num}/{total_qs}] {q_text}"

    # चेक करें कि क्या सवाल (>280 chars) या कोई ऑप्शन (>95 chars) टेलीग्राम पोल की लिमिट से बड़ा है
    is_long_content = len(question_header) > 285 or any(len(opt) > 95 for opt in full_options)

    # इवेंट रीसेट करें (पता लगाने के लिए कि किसी स्टूडेंट ने अटेम्प्ट किया या नहीं)
    attempt_event = asyncio.Event()
    channel_poll_events[exam_id] = attempt_event

    status_msg = None
    full_q_Base_html = ""

    # अगर सवाल बड़ा है तो पूरा सवाल और पूरे ऑप्शंस ऊपर साफ मैसेज में भेजें ताकि बिल्कुल न कटे
    if is_long_content:
        opts_lines = "\n".join(
            f"<b>({chr(65 + i)})</b> {html.escape(opt)}" for i, opt in enumerate(full_options)
        )
        full_q_Base_html = (
            f"📌 <b>Question {q_num} of {total_qs}</b>\n\n"
            f"<b>{html.escape(q_text)}</b>\n\n"
            f"{opts_lines}\n\n"
        )
        try:
            status_msg = await application.bot.send_message(
                chat_id=target_channel,
                text=full_q_Base_html + f"⏱️ <b>Time Remaining:</b> <code>{timer_sec} sec</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Could not send full question text: %s", e)

    # पोल के लिए ऑप्शंस तैयार करें (अगर ऊपर पूरा दिखा दिया है तो पोल में साफ छोटा रूप दें)
    poll_question = f"[Q {q_num}/{total_qs}] {q_text}"
    if len(poll_question) > 295:
        poll_question = f"[Q {q_num}/{total_qs}] {q_text[:260]}... (पूरा प्रश्न ऊपर पढ़ें ⬆️)"

    poll_options = []
    for i, opt in enumerate(full_options):
        prefix = f"({chr(65 + i)}) "
        max_len = 98 - len(prefix)
        short_opt = opt if len(opt) <= max_len else (opt[: max_len - 3] + "...")
        poll_options.append(f"{prefix}{short_opt}")

    is_anonymous = not is_show_voter_names()
    allow_multiple = is_multiple_allowed()
    quiz_mode = is_quiz_mode()
    if quiz_mode:
        allow_multiple = False

    explanation = q["solution"] if (is_explanation_enabled() and q["solution"]) else None

    # ध्यान दें: open_period नहीं लगाया गया है ताकि बिना अटेम्प्ट किए 30s पर पोल बंद न हो और उत्तर पहले न दिखे!
    poll_msg = None
    try:
        poll_msg = await application.bot.send_poll(
            chat_id=target_channel,
            question=poll_question,
            options=poll_options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allow_multiple,
            type="quiz" if quiz_mode else "regular",
            correct_option_id=new_correct_idx if quiz_mode else None,
            explanation=explanation[:200] if explanation else None,
        )
    except Exception:
        try:
            poll_msg = await application.bot.send_poll(
                chat_id=target_channel,
                question=poll_question,
                options=poll_options,
                is_anonymous=True,
                allows_multiple_answers=allow_multiple,
                type="quiz" if quiz_mode else "regular",
                correct_option_id=new_correct_idx if quiz_mode else None,
                explanation=explanation[:200] if explanation else None,
            )
        except Exception as e2:
            logger.exception("Critical error sending poll: %s", e2)
            return

    if poll_msg:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO channel_polls (exam_id, question_id, poll_id, correct_opt_id) VALUES (?, ?, ?, ?)",
            (exam_id, q["id"], poll_msg.poll.id, new_correct_idx),
        )
        conn.commit()
        conn.close()

    # अगर सवाल छोटा था तो टाइमर दिखाने के लिए एक छोटा लाइव टाइमर बार नीचे भेजें
    if not status_msg:
        try:
            status_msg = await application.bot.send_message(
                chat_id=target_channel,
                text=f"⏱️ <b>Q{q_num} Timer:</b> <code>{timer_sec} sec</code> <i>(जवाब देने के बाद अगला सवाल आएगा)</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # --- 30 सेकंड काउंटडाउन ---
    step = 5
    for remaining in range(timer_sec - step, -1, -step):
        await asyncio.sleep(step)
        if status_msg:
            try:
                if remaining > 0:
                    timer_line = f"⏱️ <b>Time Remaining:</b> <code>{remaining} sec</code>"
                else:
                    timer_line = "⏱️ <b>Time Up (0 sec)!</b>"
                await application.bot.edit_message_text(
                    chat_id=target_channel,
                    message_id=status_msg.message_id,
                    text=(full_q_Base_html + timer_line) if full_q_Base_html else timer_line,
                    parse_mode="HTML",
                )
            except Exception:
                pass

    # --- अगर 30 सेकंड में किसी ने अटेम्प्ट नहीं किया, तो लाल रंग में -1, -2, -3... चलाएं और अटेम्प्ट का इंतज़ार करें ---
    overtime_sec = 1
    while not attempt_event.is_set() and overtime_sec <= 60:
        if status_msg:
            red_line = (
                f"🔴 <b>ओवरटाइम: -{overtime_sec} sec</b> "
                f"<i>(जब तक आप ऊपर प्रश्न अटेम्प्ट नहीं करेंगे, अगला प्रश्न नहीं आएगा!)</i>"
            )
            try:
                await application.bot.edit_message_text(
                    chat_id=target_channel,
                    message_id=status_msg.message_id,
                    text=(full_q_Base_html + red_line) if full_q_Base_html else red_line,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(attempt_event.wait(), timeout=2.0)
            break
        except asyncio.TimeoutError:
            overtime_sec += 2

    # अटेम्प्ट पूरा होने के बाद टाइमर मैसेज को हटा दें (अगर सिर्फ टाइमर था) या सवाल के नीचे Done लिख दें
    if status_msg:
        try:
            if not full_q_Base_html:
                await application.bot.delete_message(
                    chat_id=target_channel, message_id=status_msg.message_id
                )
            else:
                await application.bot.edit_message_text(
                    chat_id=target_channel,
                    message_id=status_msg.message_id,
                    text=full_q_Base_html + "✅ <b>Question Attempted!</b>",
                    parse_mode="HTML",
                )
        except Exception:
            pass

    # लीडरबोर्ड को बिना नया मैसेज भेजे चुपचाप उसी पिन मैसेज में अपडेट करें
    await update_channel_scoreboard(exam_id, q_num, total_qs, repost_at_bottom=False)


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
        await update_channel_scoreboard(exam_id, repost_at_bottom=False)
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

    # अगर चैनल सवाल अटेम्प्ट के इंतज़ार में रुका है तो सिग्नल दें कि अटेम्प्ट हो गया है
    if exam_id in channel_poll_events:
        channel_poll_events[exam_id].set()

    # पिन किए हुए लीडरबोर्ड को उसी जगह अपडेट करें
    await update_channel_scoreboard(exam_id, repost_at_bottom=False)


# ---------------------------
# Student Interactive Exam Flow (30s -> 🔴 -1s, -2s, -3s Overtime until Attempted)
# ---------------------------
async def start_student_exam(update: Update, context: ContextTypes.DEFAULT_TYPE, exam_id: int):
    user = update.effective_user
    if user.id in active_student_locks:
        return

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution, explanation FROM questions WHERE exam_id=? ORDER BY id",
        (exam_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("⚠️ इस एग्जाम में कोई प्रश्न उपलब्ध नहीं हैं।")
        return

    # अगर पहले से कोई टाइमर चल रहा हो तो उसे टोकन बदलकर रोक दें
    old_exam = context.user_data.get("active_exam")
    new_token = (old_exam.get("q_token", 0) + 10) if old_exam else 1

    context.user_data["active_exam"] = {
        "exam_id": exam_id,
        "questions": [dict(r) for r in rows],
        "current_index": 0,
        "score": 0.0,
        "correct_count": 0,
        "start_time": datetime.now(),
        "q_token": new_token,
        "msg_id": None,
        "answered_current": False,
    }

    await send_next_student_question(update, context)


def build_student_question_view(q: dict, idx: int, total_qs: int, elapsed: int, timer_sec: int):
    """
    पूरा सवाल और पूरे ऑप्शंस बिना कटे बनाता है।
    30 सेकंड तक सामान्य टाइमर और 30 सेकंड के बाद लाल रंग में -1, -2, -3 दिखाता है।
    """
    remaining = timer_sec - elapsed
    if remaining >= 0:
        timer_badge = f"⏱️ <b>समय शेष (Timer):</b> <code>{remaining} sec</code>"
    else:
        overtime = remaining  # negative number: -1, -2, -3...
        timer_badge = (
            f"🔴 <b>ओवरटाइम (Time Over): <code>{overtime} sec</code></b>\n"
            f"⚠️ <i>जब तक आप कोई ऑप्शन अटेम्प्ट नहीं करेंगे, अगला प्रश्न और उत्तर नहीं दिखेगा!</i>"
        )

    raw_options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    options = [str(o).strip() for o in raw_options if o and str(o).strip()]

    # पूरा सवाल और सारे ऑप्शंस टेक्स्ट में साफ दिखें ताकि बड़े से बड़ा ऑप्शन भी कभी न कटे
    opts_text = "\n\n".join(
        f"<b>({chr(65 + i)})</b> {html.escape(opt)}" for i, opt in enumerate(options)
    )

    text = (
        f"🎯 <b>QUESTION {idx + 1} OF {total_qs}</b>\n"
        f"{timer_badge}\n"
        "───────────────────\n"
        f"<b>{html.escape(str(q['question_text']))}</b>\n\n"
        f"{opts_text}\n"
        "───────────────────\n"
        "👇 <i>नीचे दिए गए बटन से अपना उत्तर चुनें:</i>"
    )

    keyboard = []
    for o_idx, o_text in enumerate(options):
        btn_label = f"({chr(65 + o_idx)}) {o_text}"
        if len(btn_label) > 60:
            btn_label = btn_label[:57] + "..."
        keyboard.append([
            InlineKeyboardButton(btn_label, callback_data=f"ans_{o_idx}_{idx}")
        ])

    return text, InlineKeyboardMarkup(keyboard)


async def live_question_timer_task(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    expected_idx: int,
    token: int,
    timer_sec: int,
):
    """
    सवाल पर पहले 30 सेकंड का काउंटडाउन चलाता है और अगर स्टूडेंट अटेम्प्ट नहीं करता,
    तो अगला सवाल देने के बजाय 🔴 -1, -2, -3... रेड टाइमर चलाता रहता है जब तक वह अटेम्प्ट न कर ले!
    """
    elapsed = 0
    while True:
        # 30s से पहले हर 5 सेकंड में और 30s के बाद (ओवरटाइम में) हर 1 सेकंड में -1, -2, -3 अपडेट करेगा
        step = 5 if elapsed < timer_sec else 1
        await asyncio.sleep(step)
        elapsed += step

        exam_data = context.user_data.get("active_exam")
        if not exam_data:
            break
        if (
            exam_data.get("current_index") != expected_idx
            or exam_data.get("q_token") != token
            or exam_data.get("answered_current")
        ):
            break

        msg_id = exam_data.get("msg_id")
        if not msg_id:
            break

        q = exam_data["questions"][expected_idx]
        total_qs = len(exam_data["questions"])
        text, reply_markup = build_student_question_view(q, expected_idx, total_qs, elapsed, timer_sec)

        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("Timer edit stopped: %s", e)

        # सुरक्षा सीमा: 10 मिनट बाद लूप अपने आप रुक जाए
        if elapsed > timer_sec + 600:
            break


async def send_next_student_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user or update.callback_query.from_user
    chat_id = update.effective_chat.id
    await send_next_student_question_by_chat(chat_id, user.id, context, update)


async def send_next_student_question_by_chat(
    chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Update = None
):
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

        # जब कोई नया स्टूडेंट टेस्ट पूरा करे तो चैनल के पिन लीडरबोर्ड को तुरंत अपडेट करें
        await update_channel_scoreboard(exam_id, repost_at_bottom=False)

        result_text = (
            "🎉 <b>EXAM COMPLETED!</b> 🎉\n\n"
            f"👤 <b>Candidate:</b> <code>{html.escape(full_name)}</code>\n"
            f"📊 <b>Final Score:</b> <code>{final_score}</code>\n"
            f"✅ <b>Correct Answers:</b> <code>{correct_cnt}/{len(qs)}</code>\n"
            f"⏱️ <b>Total Time Taken:</b> <code>{total_time} sec</code>\n\n"
            "🏆 <b>आपकी रैंक चैनल के Live Scoreboard पर अपडेट कर दी गई है!</b>"
        )

        if update and update.callback_query:
            await update.callback_query.message.edit_text(result_text, parse_mode="HTML")
        else:
            await application.bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
        return

    q = qs[idx]
    timer_sec = get_timer_sec()
    exam_data["q_token"] = exam_data.get("q_token", 0) + 1
    exam_data["answered_current"] = False
    current_token = exam_data["q_token"]

    text, reply_markup = build_student_question_view(q, idx, len(qs), 0, timer_sec)

    if update and update.callback_query:
        msg = await update.callback_query.message.edit_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
        exam_data["msg_id"] = msg.message_id
    else:
        msg = await application.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML"
        )
        exam_data["msg_id"] = msg.message_id

    # लाइव टाइमर टास्क शुरू करें (30s -> 🔴 -1s, -2s, -3s)
    if is_limit_duration_enabled():
        asyncio.create_task(
            live_question_timer_task(context, chat_id, idx, current_token, timer_sec)
        )


# ---------------------------
# Bot Commands & Callbacks
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
        [InlineKeyboardButton(f"▶️ अभी चैनल पर लाइव करें (Exam #{exam_id})", callback_data=f"startnow_{exam_id}")]
    ]
    await update.message.reply_text(
        "✅ *EXAM CREATED SUCCESSFULLY* ✅\n\n"
        f"📚 Subject: `{draft['subject']}`\n"
        f"📖 Topic: `{draft['topic']}`\n"
        f"📑 Sub-Topic: `{draft['sub_topic']}`\n"
        f"⏰ Schedule: `{scheduled_time}`\n"
        f"❓ Total Questions: `{len(parsed_qs)}`\n"
        f"⏱️ Question Timer: `{timer_sec} sec`\n\n"
        "👇 अगर आप अभी तुरंत चैनल पर क्विज़ शुरू करना चाहते हैं तो नीचे बटन दबाएं:",
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
        "🚫 *Access Restricted*\n\nक्विज़ देने के लिए पहले हमारा Telegram Channel जॉइन करें।",
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
        "📢 <b>UPCOMING LIVE CHANNEL QUIZ</b> 📢\n"
        "⚡ ═══════════════════════ ⚡\n\n"
        f"📚 <b>Subject:</b> <code>{html.escape(subject)}</code>\n"
        f"📖 <b>Topic:</b> <code>{html.escape(topic)}</code>\n"
        f"📑 <b>Sub-Topic:</b> <code>{html.escape(sub_topic)}</code>\n\n"
        f"⏰ <b>Quiz Time:</b> <code>{sched_time}</code>\n"
        f"❓ <b>Total Questions:</b> <code>{total_q}</code>\n"
        f"⏱️ <b>Time per Question:</b> <code>{get_timer_sec()} sec</code>\n"
        f"🏆 <b>Scoreboard:</b> <code>Live on Channel</code>\n\n"
        "👇 <i>तैयार रहें! क्विज़ इसी चैनल पर पोस्ट होगा।</i>"
    )

    try:
        msg = await application.bot.send_message(
            chat_id=target_channel,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # स्टूडेंट के जवाब देने पर: पहले सही/गलत का पॉपअप दिखाएं और तभी अगला सवाल लाएं
    if data.startswith("ans_"):
        if user_id in active_student_locks:
            await query.answer()
            return

        parts = data.split("_")
        ans_idx = int(parts[1])
        q_idx = int(parts[2])

        exam_data = context.user_data.get("active_exam")
        if not exam_data or exam_data["current_index"] != q_idx or exam_data.get("answered_current"):
            await query.answer()
            return

        active_student_locks.add(user_id)
        try:
            exam_data["answered_current"] = True
             exam_data["q_token"] = exam_data.get("q_token", 0) + 1  # टाइमर को तुरंत रोकें

            current_q = exam_data["questions"][q_idx]
            correct_idx = int(current_q["correct_opt_id"] or 0)
            pos_m = float(current_q["pos_marks"] or 1.0)
            neg_m = float(current_q["neg_marks"] or 0.0)
            sol_text = (current_q.get("solution") or current_q.get("explanation") or "").strip()

            raw_opts = [current_q["opt_a"], current_q["opt_b"], current_q["opt_c"], current_q["opt_d"]]
            correct_opt_text = raw_opts[correct_idx] if 0 <= correct_idx < len(raw_opts) else ""

            if ans_idx == correct_idx:
                exam_data["score"] += pos_m
                exam_data["correct_count"] = exam_data.get("correct_count", 0) + 1
                feedback = f"✅ सही जवाब! (+{pos_m})"
                if sol_text and is_explanation_enabled():
                    feedback += f"\n💡 {sol_text}"
                await query.answer(feedback[:200], show_alert=False)
            else:
                exam_data["score"] -= neg_m
                feedback = f"❌ गलत जवाब! (-{neg_m})\n✔️ सही उत्तर: ({chr(65 + correct_idx)}) {correct_opt_text}"
                if sol_text and is_explanation_enabled():
                    feedback += f"\n💡 {sol_text}"
                await query.answer(feedback[:200], show_alert=True)

            exam_data["current_index"] += 1
            await send_next_student_question(update, context)
        finally:
            active_student_locks.discard(user_id)
        return

    await query.answer()

    if data == "admin_test_channel":
        target = get_target_channel()
        if not target:
            await query.edit_message_text(
                "❌ **Channel Set Nahi Hai!**\n\nRender Environment Variables mein `TELEGRAM_CHANNEL_ID` या `TELEGRAM_CHANNEL_USERNAME` जोड़ें।",
                parse_mode="Markdown",
            )
            return
        try:
            await application.bot.send_poll(
                chat_id=target,
                question="🔔 [Test Quiz] क्या चैनल पर क्विज़ सही से पोस्ट हो रहा है?",
                options=["हाँ, 100% Working!", "बहुत बढ़िया"],
                is_anonymous=True,
                type="quiz",
                correct_option_id=0,
            )
            await query.edit_message_text(
                f"✅ **Success!** चैनल (`{target}`) पर टेस्ट क्विज़ पोस्ट हो गया है!",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Channel Par Post Fail Hua!**\n\nTarget: `{target}`\nError: `{e}`",
                parse_mode="Markdown",
            )
        return

    if data.startswith("refresh_sb_"):
        exam_id = int(data.split("_")[-1])
        await update_channel_scoreboard(exam_id, repost_at_bottom=False)
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
        if exam_id in running_channel_exams:
            await query.edit_message_text(f"⚠️ **Exam #{exam_id} पहले से चैनल पर लाइव चल रहा है!**", parse_mode="Markdown")
            return
        target = get_target_channel()
        await query.edit_message_text(
            f"🚀 **Exam #{exam_id} को चैनल (`{target}`) पर लाइव किया जा रहा है!**\n\nएक-एक करके सवाल पोस्ट होंगे (बिना रिपीट हुए)।",
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
                f"📁 **Time Set**: `{draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']}`\n\nअब अपनी `.docx` फ़ाइल भेजें।",
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
                        misfire_grace_time=300,
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
