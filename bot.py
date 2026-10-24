import asyncio
import io
import logging
import os
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID") or os.environ.get("TELEGRAM_CHANNEL_ID", "")
TELEGRAM_CHANNEL_USERNAME = os.environ.get("TG_CHANNEL_USERNAME") or os.environ.get("TELEGRAM_CHANNEL_USERNAME", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", 10000))

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
            is_anonymous INTEGER DEFAULT 1,
            allow_multiple_answers INTEGER DEFAULT 0,
            shuffle_options INTEGER DEFAULT 0,
            question_timer INTEGER DEFAULT 30,
            channel_lock INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

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
        "show_voter_names": "0",
        "allow_multiple_answers": "0",
        "shuffle_options": "0",
        "quiz_mode": "1",
        "show_explanations": "1",
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
    return get_setting("show_voter_names", "0") == "1"


def is_multiple_allowed() -> bool:
    return get_setting("allow_multiple_answers", "0") == "1"


def is_shuffle_enabled() -> bool:
    return get_setting("shuffle_options", "0") == "1"


def is_quiz_mode() -> bool:
    return get_setting("quiz_mode", "1") == "1"


def is_explanation_enabled() -> bool:
    return get_setting("show_explanations", "1") == "1"


def is_channel_lock_enabled() -> bool:
    return get_setting("channel_lock", "1") == "1"


def get_timer_sec() -> int:
    try:
        return int(get_setting("timer_sec", "30"))
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
    return (
        "⚙️ *ADMIN CONTROL PANEL*\n\n"
        f"👁️ Show Who Voted: **{'ON' if is_show_voter_names() else 'OFF'}**\n"
        f"☑️ Allow Multiple Answers: **{'ON' if is_multiple_allowed() else 'OFF'}**\n"
        f"🔀 Shuffle Options: **{'ON' if is_shuffle_enabled() else 'OFF'}**\n"
        f"🧠 Quiz Mode: **{'ON' if is_quiz_mode() else 'OFF'}**\n"
        f"💡 Explanations: **{'ON' if is_explanation_enabled() else 'OFF'}**\n"
        f"🔒 Channel Lock: **{'ON' if is_channel_lock_enabled() else 'OFF'}**\n"
        f"⏱️ Timer: `{get_timer_sec()} sec`"
    )


def admin_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                f"👁️ Show Who Voted: {'ON' if is_show_voter_names() else 'OFF'}",
                callback_data="toggle_show_voter_names",
            )
        ],
        [
            InlineKeyboardButton(
                f"☑️ Multi Answers: {'ON' if is_multiple_allowed() else 'OFF'}",
                callback_data="toggle_multiple_answers",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔀 Shuffle: {'ON' if is_shuffle_enabled() else 'OFF'}",
                callback_data="toggle_shuffle_options",
            )
        ],
        [
            InlineKeyboardButton(
                f"🧠 Quiz Mode: {'ON' if is_quiz_mode() else 'OFF'}",
                callback_data="toggle_quiz_mode",
            )
        ],
        [
            InlineKeyboardButton(
                f"💡 Explanations: {'ON' if is_explanation_enabled() else 'OFF'}",
                callback_data="toggle_explanations",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔒 Channel Lock: {'ON' if is_channel_lock_enabled() else 'OFF'}",
                callback_data="toggle_channel_lock",
            )
        ],
        [
            InlineKeyboardButton("🆕 Create New Exam", callback_data="admin_create_exam"),
            InlineKeyboardButton("📋 View Exams", callback_data="admin_view_exams"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


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
        "📚 Pro Quiz & Live Ranking Bot में आपका स्वागत है।\n\n"
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
            get_timer_sec(),
            scheduled_time,
            "quiz" if is_quiz_mode() else "poll",
            1 if is_show_voter_names() else 0,
            1 if is_multiple_allowed() else 0,
            1 if is_shuffle_enabled() else 0,
            get_timer_sec(),
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

    await post_advance_announcement(
        exam_id,
        draft["subject"],
        draft["topic"],
        draft["sub_topic"],
        scheduled_time,
        len(parsed_qs),
    )

    try:
        naive_dt = datetime.strptime(scheduled_time, "%Y-%m-%d %I:%M %p")
        run_time = IST.localize(naive_dt)
        scheduler.add_job(
            post_exam_to_channel,
            "date",
            run_date=run_time,
            args=[exam_id],
            id=f"exam_job_{exam_id}",
            replace_existing=True,
        )
    except Exception as e:
        logger.exception("Scheduler Error: %s", e)

    admin_drafts.pop(user_id, None)
    await update.message.reply_text(
        "✅ *EXAM CREATED SUCCESSFULLY* ✅\n\n"
        f"📚 Subject: `{draft['subject']}`\n"
        f"📖 Topic: `{draft['topic']}`\n"
        f"📑 Sub-Topic: `{draft['sub_topic']}`\n"
        f"⏰ Schedule: `{scheduled_time}`\n"
        f"❓ Total Questions: `{len(parsed_qs)}`",
        parse_mode="Markdown",
    )


async def ensure_channel_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_channel_lock_enabled():
        return True

    if not TELEGRAM_CHANNEL_ID:
        return True

    try:
        member = await application.bot.get_chat_member(TELEGRAM_CHANNEL_ID, update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        pass

    channel_link = f"https://t.me/{TELEGRAM_CHANNEL_USERNAME}" if TELEGRAM_CHANNEL_USERNAME else "https://t.me"
    keyboard = [[InlineKeyboardButton("🔔 Join Channel", url=channel_link)]]
    await update.message.reply_text(
        "🚫 *Access Restricted*\n\nJoin the channel first before taking the exam.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return False


async def post_advance_announcement(exam_id, subject, topic, sub_topic, sched_time, total_q):
    if not TELEGRAM_CHANNEL_ID or not application:
        return

    share_msg = (
        f"📢 *UPCOMING LIVE EXAM*\n\n"
        f"📚 *Subject:* {subject}\n"
        f"📖 *Topic:* {topic}\n"
        f"📑 *Sub-Topic:* {sub_topic}\n"
        f"⏰ *Exam Time:* {sched_time}\n"
        f"❓ *Total Questions:* {total_q}\n\n"
        f"🔥 Share with your friends!"
    )
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"
    keyboard = [[InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)]]

    text = (
        "⚡ ═══════════════════════ ⚡\n"
        "📢 LIVE EXAM ANNOUNCEMENT 📢\n"
        "⚡ ═══════════════════════ ⚡\n\n"
        f"📚 Subject: `{subject}`\n"
        f"📖 Topic: `{topic}`\n"
        f"📑 Sub-Topic: `{sub_topic}`\n\n"
        f"⏰ Exam Time: `{sched_time}`\n"
        f"❓ Total Questions: `{total_q}`\n"
        f"⏱️ Time Limit: `{get_timer_sec()} sec / Q`\n"
        f"🏆 Leaderboard: `Live Rank`\n\n"
        "👇 Share below."
    )

    try:
        msg = await application.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        await application.bot.pin_chat_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=False,
        )
    except Exception as e:
        logger.exception("Error in post_advance_announcement: %s", e)


async def post_exam_to_channel(exam_id):
    if not TELEGRAM_CHANNEL_ID or not application:
        return

    conn = get_conn()
    exam = conn.execute(
        "SELECT subject, topic, sub_topic, time_per_q FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    questions = conn.execute(
        "SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution FROM questions WHERE exam_id=? ORDER BY id",
        (exam_id,),
    ).fetchall()
    conn.close()

    if not exam or not questions:
        return

    bot_info = await application.bot.get_me()
    start_link = f"https://t.me/{bot_info.username}?start=exam_{exam_id}"

    share_msg = (
        f"🔥 *LIVE EXAM STARTED!*\n\n"
        f"📚 *Subject:* {exam['subject']}\n"
        f"📖 *Topic:* {exam['topic']}\n"
        f"📑 *Sub-Topic:* {exam['sub_topic']}\n"
        f"❓ *Questions:* {len(questions)}\n"
        f"⏱ *Timer:* {exam['time_per_q']} sec / Q\n\n"
        "Start now!"
    )
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

    announcement = (
        "⚡ ═══════════════════════ ⚡\n"
        "🚀 LIVE EXAM IS NOW STARTED! 🚀\n"
        "⚡ ═══════════════════════ ⚡\n\n"
        f"📚 Subject: `{exam['subject']}`\n"
        f"📖 Topic: `{exam['topic']}`\n"
        f"📑 Sub-Topic: `{exam['sub_topic']}`\n"
        f"❓ Total Questions: `{len(questions)}`\n"
        f"⏱️ Time Limit: `{exam['time_per_q']} sec / Q`\n"
        "🏆 Leaderboard: `Instant Rank`\n\n"
        "👇 Join now."
    )

    keyboard = [
        [InlineKeyboardButton("🚀 Start Live Exam Now", url=start_link)],
        [InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)],
    ]

    try:
        msg = await application.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=announcement,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        await application.bot.pin_chat_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=False,
        )
    except Exception as e:
        logger.exception("Error in live exam message: %s", e)

    # Post each question as a native Telegram poll/quiz
    for q in questions:
        await send_channel_poll(exam_id, q["id"])

    conn = get_conn()
    conn.execute("UPDATE exams SET status='LIVE' WHERE id=?", (exam_id,))
    conn.commit()
    conn.close()


async def send_channel_poll(exam_id, question_id):
    if not TELEGRAM_CHANNEL_ID or not application:
        return

    conn = get_conn()
    q = conn.execute(
        "SELECT question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, solution FROM questions WHERE id=? AND exam_id=?",
        (question_id, exam_id),
    ).fetchone()
    conn.close()

    if not q:
        return

    options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    options = [opt for opt in options if opt]

    is_anonymous = not is_show_voter_names()
    allow_multiple = is_multiple_allowed()
    quiz_mode = is_quiz_mode()
    explanation = q["solution"] if is_explanation_enabled() else None

    try:
        poll = await application.bot.send_poll(
            chat_id=TELEGRAM_CHANNEL_ID,
            question=q["question_text"],
            options=options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allow_multiple,
            type="quiz" if quiz_mode else "regular",
            correct_option_id=int(q["correct_opt_id"]) if quiz_mode else None,
            explanation=explanation,
            open_period=get_timer_sec(),
        )

        conn = get_conn()
        conn.execute(
            "INSERT INTO channel_polls (exam_id, question_id, poll_id) VALUES (?, ?, ?)",
            (exam_id, question_id, poll.poll.id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Error while posting poll in channel: %s", e)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    user_name = poll_answer.user.first_name or "User"
    selected = poll_answer.option_ids

    conn = get_conn()
    row = conn.execute(
        "SELECT exam_id, question_id FROM channel_polls WHERE poll_id = ?",
        (poll_id,),
    ).fetchone()
    if not row:
        conn.close()
        return

    q = conn.execute(
        "SELECT correct_opt_id, pos_marks, neg_marks FROM questions WHERE id = ?",
        (row["question_id"],),
    ).fetchone()

    if not q:
        conn.close()
        return

    selected_option = ",".join(str(i) for i in selected)
    is_correct = 0
    if selected and int(selected[0]) == int(q["correct_opt_id"]):
        is_correct = 1

    conn.execute(
        "INSERT INTO channel_answers (exam_id, question_id, poll_id, user_id, user_name, selected_option, is_correct) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["exam_id"],
            row["question_id"],
            poll_id,
            user_id,
            user_name,
            selected_option,
            is_correct,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------
# Student exam flow
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
        "start_time": datetime.now(),
    }

    await send_next_student_question(update, context)


async def send_next_student_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exam_data = context.user_data.get("active_exam")
    if not exam_data:
        return

    idx = exam_data["current_index"]
    qs = exam_data["questions"]

    if idx >= len(qs):
        end_time = datetime.now()
        total_time = int((end_time - exam_data["start_time"]).total_seconds())
        final_score = exam_data["score"]
        user = update.effective_user or update.callback_query.from_user

        conn = get_conn()
        conn.execute(
            "INSERT INTO student_results (exam_id, user_id, user_name, score, total_time_sec, total_questions) VALUES (?, ?, ?, ?, ?, ?)",
            (exam_data["exam_id"], user.id, user.first_name, final_score, total_time, len(qs)),
        )
        conn.commit()
        conn.close()

        result_text = (
            "🎉 **EXAM COMPLETED!** 🎉\n\n"
            f"📊 Score: `{final_score}`\n"
            f"⏱️ Time Taken: `{total_time} sec`\n\n"
            "🔥 Good luck for the next round!"
        )

        if update.callback_query:
            await update.callback_query.message.edit_text(result_text, parse_mode="Markdown")
        else:
            await update.message.reply_text(result_text, parse_mode="Markdown")

        context.user_data.pop("active_exam", None)
        return

    q = qs[idx]
    options = [q["opt_a"], q["opt_b"], q["opt_c"], q["opt_d"]]
    options = [o for o in options if o]

    keyboard = []
    for o_idx, o_text in enumerate(options):
        keyboard.append([
            InlineKeyboardButton(f"{chr(65 + o_idx)}) {o_text}", callback_data=f"ans_{o_idx}_{idx}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🎯 **QUIZ QUESTION** 🎯\n\n"
        f"📌 Q {idx + 1}/{len(qs)}\n"
        f"⏱️ Timer: `{get_timer_sec()} sec`\n\n"
        f"{q['question_text']}"
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "admin_create_exam":
        admin_drafts[user_id] = {"action": "create", "step": "subject"}
        await query.edit_message_text("📝 **Subject का नाम लिखें:**", parse_mode="Markdown")
        return

    if data == "admin_view_exams":
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, subject, topic, scheduled_time FROM exams ORDER BY id DESC LIMIT 5"
        ).fetchall()
        conn.close()

        if not rows:
            await query.edit_message_text("📋 कोई Scheduled Exam नहीं है।")
            return

        keyboard = []
        text = "📋 **Scheduled Exams:**\n\n"
        for r in rows:
            text += f"• **Exam #{r['id']}**: {r['subject']} - {r['topic']} ({r['scheduled_time']})\n"
            keyboard.append([
                InlineKeyboardButton(f"⏰ Reschedule #{r['id']}", callback_data=f"resched_{r['id']}")
            ])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("resched_"):
        exam_id = int(data.split("_")[1])
        admin_drafts[user_id] = {"action": "reschedule_date", "exam_id": exam_id}
        await query.edit_message_text(
            f"📅 **Exam #{exam_id} के लिए नई तारीख लिखें**\n\nFormat: `YYYY-MM-DD`",
            parse_mode="Markdown",
        )
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
                f"📁 **Time Set**: `{draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']}`\n\nNow send your `.docx` file.",
                parse_mode="Markdown",
            )
        return

    if data.startswith("ans_"):
        parts = data.split("_")
        ans_idx = int(parts[1])
        q_idx = int(parts[2])

        exam_data = context.user_data.get("active_exam")
        if exam_data and exam_data["current_index"] == q_idx:
            current_q = exam_data["questions"][q_idx]
            correct_idx = current_q["correct_opt_id"]
            pos_m = current_q["pos_marks"]
            neg_m = current_q["neg_marks"]

            if ans_idx == correct_idx:
                exam_data["score"] += pos_m
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
        future = asyncio.run_coroutine_threadsafe(process_update(), bot_loop)
        future.result(timeout=12)
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
        logger.warning("RENDER_EXTERNAL_URL is empty; webhook not set.")

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
