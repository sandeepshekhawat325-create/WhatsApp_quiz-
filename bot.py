import os
import io
import logging
import sqlite3
import urllib.parse
from datetime import datetime
import pytz
from flask import Flask, request
import docx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Indian Timezone Setup (IST)
IST = pytz.timezone('Asia/Kolkata')

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID") or os.environ.get("TELEGRAM_CHANNEL_ID", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://whatsapp-quiz-pt5d.onrender.com")
PORT = int(os.environ.get("PORT", 10000))

# Flask App & Scheduler with IST timezone
app = Flask(__name__)
scheduler = AsyncIOScheduler(timezone=IST)
application = None

# ==========================================
# DATABASE SETUP (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect("exam_system.db")
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            topic TEXT,
            sub_topic TEXT,
            time_per_q INTEGER DEFAULT 30,
            scheduled_time TEXT,
            status TEXT DEFAULT 'DRAFT'
        )
    ''')
    
    cursor.execute('''
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
            FOREIGN KEY(exam_id) REFERENCES exams(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS student_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            score REAL,
            total_time_sec INTEGER,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(exam_id) REFERENCES exams(id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()
admin_drafts = {}

# ==========================================
# ADVANCED DOCX PARSER
# ==========================================
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

    for line in lines:
        lower = line.lower()
        
        if lower.startswith("question") or lower.startswith("q:") or lower.startswith("q."):
            if current_q and len(options) >= 2:
                questions.append({
                    "question": current_q,
                    "options": options[:4],
                    "correct": correct_idx,
                    "pos_marks": pos_m,
                    "neg_marks": neg_m,
                    "solution": sol
                })
            if ":" in line:
                current_q = line.split(":", 1)[-1].strip()
            else:
                current_q = line
            options = []
            correct_idx = 0
            pos_m = 1.0
            neg_m = 0.0
            sol = ""
            
        elif any(lower.startswith(p) for p in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]):
            for p in ["(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)", "a.", "b.", "c.", "d."]:
                if lower.startswith(p):
                    opt_val = line[len(p):].strip()
                    break
            else:
                opt_val = line
            options.append(opt_val if opt_val else line)
            
        elif "answer" in lower or lower.startswith("ans"):
            ans_str = line.split(":", 1)[-1].strip().lower() if ":" in line else line.lower()
            if "a" in ans_str: correct_idx = 0
            elif "b" in ans_str: correct_idx = 1
            elif "c" in ans_str: correct_idx = 2
            elif "d" in ans_str: correct_idx = 3
            
        elif "solution" in lower:
            sol = line.split(":", 1)[-1].strip() if ":" in line else line
            
        elif "positive marks" in lower:
            try: pos_m = float(line.split(":", 1)[-1].strip())
            except: pos_m = 1.0
            
        elif "negative marks" in lower:
            try: neg_m = float(line.split(":", 1)[-1].strip())
            except: neg_m = 0.0

    if current_q and len(options) >= 2:
        questions.append({
            "question": current_q,
            "options": options[:4],
            "correct": correct_idx,
            "pos_marks": pos_m,
            "neg_marks": neg_m,
            "solution": sol
        })

    return questions

# ==========================================
# BOT HANDLERS & ADMIN PANEL
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    
    if args and args[0].startswith("exam_"):
        exam_id = int(args[0].split("_")[1])
        await start_student_exam(update, context, exam_id)
        return

    await update.message.reply_text(
        f"👋 **नमस्ते {user.first_name}!**\n\n"
        "🏛 **Pro Exam & Live Ranking System Bot में आपका स्वागत है।**\n\n"
        "👨‍🏫 **Admin Panel:** /admin"
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_drafts[user_id] = {}
    
    keyboard = [
        [InlineKeyboardButton("➕ Create New Exam Schedule", callback_data="admin_create_exam")],
        [InlineKeyboardButton("📋 View & Reschedule Exams", callback_data="admin_view_exams")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ **Admin Control Panel:**", reply_markup=reply_markup)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    draft = admin_drafts.get(user_id)
    
    if not draft or draft.get('action') != 'create' or draft.get('step') != 'file':
        await update.message.reply_text("⚠️ कृपया पहले /admin से प्रक्रिया शुरू करें।")
        return

    document = update.message.document
    if not document.file_name.lower().endswith(('.docx', '.doc')):
        await update.message.reply_text("❌ कृपया केवल `.docx` (Word) फ़ाइल ही भेजें!")
        return

    file_obj = await document.get_file()
    file_bytes = await file_obj.download_as_bytearray()
    
    parsed_qs = parse_docx_content(file_bytes)
    if not parsed_qs:
        await update.message.reply_text("❌ फ़ाइल से प्रश्न पढ़ने में त्रुटि हुई। फॉर्मेट जांचें!")
        return

    sched_time_str = f"{draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']}"
    
    conn = sqlite3.connect("exam_system.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO exams (subject, topic, sub_topic, time_per_q, scheduled_time, status)
        VALUES (?, ?, ?, ?, ?, 'SCHEDULED')
    ''', (draft['subject'], draft['topic'], draft['sub_topic'], 30, sched_time_str))
    
    exam_id = cursor.lastrowid
    
    for q in parsed_qs:
        opts = q['options'] + ["", "", "", ""]
        cursor.execute('''
            INSERT INTO questions (exam_id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (exam_id, q['question'], opts[0], opts[1], opts[2], opts[3], q['correct'], q['pos_marks'], q['neg_marks'], q['solution']))
    
    conn.commit()
    conn.close()

    await post_advance_announcement(exam_id, draft['subject'], draft['topic'], draft['sub_topic'], sched_time_str, len(parsed_qs))

    try:
        naive_dt = datetime.strptime(sched_time_str, "%Y-%m-%d %I:%M %p")
        run_time = IST.localize(naive_dt)
        scheduler.add_job(
            post_exam_to_channel,
            'date',
            run_date=run_time,
            args=[exam_id],
            id=f"exam_job_{exam_id}",
            replace_existing=True
        )
    except Exception as e:
        logger.error(f"Scheduler Error: {e}")

    del admin_drafts[user_id]
    await update.message.reply_text(f"✅ **Exam Setup Successful! (Exam ID: #{exam_id})**\n\n📢 चैनल पर अगामी परीक्षा की घोषणा पोस्ट और पिन कर दी गई है!")

# ==========================================
# ADVANCED STYLISH ANNOUNCEMENT POST
# ==========================================
async def post_advance_announcement(exam_id, subject, topic, sub_topic, sched_time, total_q):
    global application
    if not TELEGRAM_CHANNEL_ID or not application:
        return

    share_msg = (
        f"📢 *UPCOMING LIVE EXAM ANNOUNCEMENT*\n\n"
        f"📚 *Subject:* {subject}\n"
        f"📖 *Topic:* {topic}\n"
        f"📑 *Sub-Topic:* {sub_topic}\n"
        f"⏰ *Exam Time:* {sched_time}\n"
        f"❓ *Total Questions:* {total_q}\n\n"
        f"🔥 *Jyada se jyada dosto ke sath share karein!*"
    )
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

    keyboard = [[InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    announcement_text = (
        f"⚡ **══════════════════** ⚡\n"
        f"📢 **UPCOMING EXAM ANNOUNCEMENT** 📢\n"
        f"⚡ **══════════════════** ⚡\n\n"
        f"📚 **विषय (Subject):** `{subject}`\n"
        f"📖 **टॉपिक (Topic):** `{topic}`\n"
        f"📑 **सब-टॉपिक (Sub-Topic):** `{sub_topic}`\n\n"
        f"⏰ **परीक्षा का समय (Time):** 🗓 `{sched_time}`\n"
        f"❓ **कुल प्रश्न (Total Questions):** `{total_q}`\n"
        f"⏱ **समय सीमा (Timer):** `30 Sec / Question`\n"
        f"🏆 **रैंकिंग (Leaderboard):** `Instant Live Rank`\n\n"
        f"📌 **विशेष सूचना:**\n"
        f"सर्वश्रेष्ठ प्रदर्शन के लिए नियत समय पर ऑनलाइन रहें! अपने दोस्तों को भी यह पोस्ट शेयर करके आमंत्रित करें। 🚀\n\n"
        f"👇 **शेयर करने के लिए नीचे बटन पर क्लिक करें:**"
    )

    try:
        msg = await application.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=announcement_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        await application.bot.pin_chat_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=False
        )
    except Exception as e:
        logger.error(f"Error in announcement/pin: {e}")

# ==========================================
# STYLISH LIVE EXAM POST
# ==========================================
async def post_exam_to_channel(exam_id):
    global application
    if not TELEGRAM_CHANNEL_ID or not application:
        return

    conn = sqlite3.connect("exam_system.db")
    cursor = conn.cursor()
    cursor.execute("SELECT subject, topic, sub_topic, time_per_q FROM exams WHERE id=?", (exam_id,))
    exam = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) FROM questions WHERE exam_id=?", (exam_id,))
    total_q = cursor.fetchone()[0]
    conn.close()

    if not exam:
        return

    bot_info = await application.bot.get_me()
    start_link = f"https://t.me/{bot_info.username}?start=exam_{exam_id}"
    
    share_msg = f"🔥 *LIVE EXAM IS STARTED NOW!*\n\n📚 *Subject:* {exam[0]}\n📖 *Topic:* {exam[1]}\n📑 *Sub-Topic:* {exam[2]}\n❓ *Total Questions:* {total_q}\n⏱ *Timer:* 30 Sec / Question\n\n👉 *Join Test Now:* {start_link}"
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

    keyboard = [
        [InlineKeyboardButton("🚀 Start Live Exam Now (In Bot)", url=start_link)],
        [InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    post_text = (
        f"🔥 **══════════════════** 🔥\n"
        f"🚀 **LIVE EXAM IS NOW STARTED!** 🚀\n"
        f"🔥 **══════════════════** 🔥\n\n"
        f"📚 **Subject:** `{exam[0]}`\n"
        f"📖 **Topic:** `{exam[1]}`\n"
        f"📑 **Sub-Topic:** `{exam[2]}`\n"
        f"❓ **Total Questions:** `{total_q}`\n"
        f"⏱ **Time Limit:** `30 Seconds / Question`\n"
        f"🏆 **Live Leaderboard:** Instant Rank Generation\n\n"
        f"⚡ **परीक्षा शुरू हो चुकी है! अपनी तैयारी परखें और अपनी लाइव रैंक देखें!**\n\n"
        f"👇 **नीचे बटन पर क्लिक करके तुरंत टेस्ट शुरू करें:**"
    )

    try:
        msg = await application.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=post_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        await application.bot.pin_chat_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=False
        )
    except Exception as e:
        logger.error(f"Error in live post/pin: {e}")

def restore_jobs():
    try:
        conn = sqlite3.connect("exam_system.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, scheduled_time FROM exams WHERE status='SCHEDULED'")
        exams = cursor.fetchall()
        conn.close()

        now = datetime.now(IST)
        for exam in exams:
            exam_id, sched_str = exam
            try:
                naive_dt = datetime.strptime(sched_str.strip(), "%Y-%m-%d %I:%M %p")
                run_time = IST.localize(naive_dt)
                job_id = f"exam_job_{exam_id}"
                if run_time > now:
                    scheduler.add_job(
                        post_exam_to_channel,
                        'date',
                        run_date=run_time,
                        args=[exam_id],
                        id=job_id,
                        replace_existing=True
                    )
            except Exception as ex:
                logger.error(f"Could not restore job #{exam_id}: {ex}")
    except Exception as e:
        logger.error(f"Error in restore_jobs: {e}")

async def start_student_exam(update: Update, context: ContextTypes.DEFAULT_TYPE, exam_id: int):
    conn = sqlite3.connect("exam_system.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks FROM questions WHERE exam_id=?", (exam_id,))
    questions = cursor.fetchall()
    conn.close()

    if not questions:
        await update.message.reply_text("⚠️ इस एग्जाम में कोई प्रश्न उपलब्ध नहीं हैं।")
        return

    context.user_data['active_exam'] = {
        'exam_id': exam_id,
        'questions': questions,
        'current_index': 0,
        'score': 0.0,
        'start_time': datetime.now()
    }

    await send_next_student_question(update, context)

async def send_next_student_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exam_data = context.user_data.get('active_exam')
    if not exam_data:
        return

    idx = exam_data['current_index']
    qs = exam_data['questions']

    if idx >= len(qs):
        end_time = datetime.now()
        total_time = int((end_time - exam_data['start_time']).total_seconds())
        final_score = exam_data['score']
        user = update.effective_user or update.callback_query.from_user

        conn = sqlite3.connect("exam_system.db")
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO student_results (exam_id, user_id, user_name, score, total_time_sec)
            VALUES (?, ?, ?, ?, ?)
        ''', (exam_data['exam_id'], user.id, user.first_name, final_score, total_time))
        conn.commit()

        cursor.execute('''
            SELECT user_name, score, total_time_sec FROM student_results 
            WHERE exam_id=? ORDER BY score DESC, total_time_sec ASC
        ''', (exam_data['exam_id'],))
        all_results = cursor.fetchall()
        conn.close()

        my_rank = 1
        for r in all_results:
            if r[0] == user.first_name and r[1] == final_score:
                break
            my_rank += 1

        result_text = (
            f"🎉 **एग्जाम सफलतापूर्वक समाप्त हुआ!** 🎉\n\n"
            f"📊 **आपका स्कोर (Score):** `{final_score}`\n"
            f"⏱ **कुल समय (Total Time):** `{total_time} Seconds`\n"
            f"🏆 **आपकी रैंक (Your Rank):** `#{my_rank}`\n\n"
            f"🔄 यह एग्जाम री-अटेम्प्टेबल है, आप चाहें तो /start लिंक से इसे दोबारा भी दे सकते हैं!"
        )

        if update.callback_query:
            await update.callback_query.message.edit_text(result_text, parse_mode="Markdown")
        else:
            await update.message.reply_text(result_text, parse_mode="Markdown")

        del context.user_data['active_exam']
        return

    q = qs[idx]
    opts = [q[2], q[3], q[4], q[5]]
    
    keyboard = []
    for o_idx, o_text in enumerate(opts):
        if o_text:
            keyboard.append([InlineKeyboardButton(f"{chr(65+o_idx)}) {o_text}", callback_data=f"ans_{o_idx}_{idx}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"⏳ **Question {idx+1}/{len(qs)}** (Time limit: 30 Sec)\n\n{q[1]}"
    
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
        admin_drafts[user_id] = {'action': 'create', 'step': 'subject'}
        await query.edit_message_text("📝 **Subject का नाम लिखें:**")
        return

    elif data == "admin_view_exams":
        conn = sqlite3.connect("exam_system.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, topic, scheduled_time FROM exams ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            await query.edit_message_text("📋 कोई भी Scheduled Exam उपलब्ध नहीं है।")
            return

        keyboard = []
        txt = "📋 **Scheduled Exams:**\n\n"
        for r in rows:
            txt += f"• **Exam #{r[0]}**: {r[1]} - {r[2]} ({r[3]})\n"
            keyboard.append([InlineKeyboardButton(f"⏰ Reschedule Exam #{r[0]}", callback_data=f"resched_{r[0]}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(txt, reply_markup=reply_markup)
        return

    elif data.startswith("resched_"):
        exam_id = int(data.split("_")[1])
        admin_drafts[user_id] = {'action': 'reschedule_date', 'exam_id': exam_id}
        await query.edit_message_text(f"📅 **Exam #{exam_id} के लिए नई तारीख लिखें**\n\n(Format: `YYYY-MM-DD`, उदाहरण: `2026-10-24`):")
        return

    elif data in ["set_am", "set_pm"]:
        ampm_val = "AM" if data == "set_am" else "PM"
        draft = admin_drafts.get(user_id)
        if not draft:
            return

        if draft.get('action') == 'create':
            draft['ampm'] = ampm_val
            draft['step'] = 'file'
            await query.edit_message_text(f"📁 **समय सेट हो गया ({draft['date']} {draft['hour']}:{draft['minute']} {draft['ampm']})!**\n\nअब अपनी `.docx` प्रश्न फ़ाइल भेजें:")
        elif draft.get('action') == 'reschedule_ampm':
            exam_id = draft['exam_id']
            new_time_str = f"{draft['date']} {draft['hour']}:{draft['minute']} {ampm_val}"
            try:
                naive_dt = datetime.strptime
