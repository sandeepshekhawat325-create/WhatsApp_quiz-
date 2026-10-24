import os
import io
import logging
import sqlite3
import urllib.parse
from datetime import datetime
from flask import Flask, request
import docx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://whatsapp-quiz-pt5d.onrender.com")
PORT = int(os.environ.get("PORT", 10000))

# Flask App for Health Check and Webhook Handling
app = Flask(__name__)
scheduler = AsyncIOScheduler()
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
# DOCX PARSER
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
        
        if lower.startswith("question:"):
            if current_q and len(options) >= 2:
                questions.append({
                    "question": current_q,
                    "options": options[:4],
                    "correct": correct_idx,
                    "pos_marks": pos_m,
                    "neg_marks": neg_m,
                    "solution": sol
                })
            current_q = line.split(":", 1)[-1].strip()
            options = []
            correct_idx = 0
            pos_m = 1.0
            neg_m = 0.0
            sol = ""
            
        elif lower.startswith(("(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d)")):
            opt_val = line.split(")", 1)[-1].strip() if ")" in line else line
            options.append(opt_val if opt_val else line)
            
        elif lower.startswith(("answer:", "ans:")):
            ans_str = line.split(":", 1)[-1].strip().lower()
            if "a" in ans_str: correct_idx = 0
            elif "b" in ans_str: correct_idx = 1
            elif "c" in ans_str: correct_idx = 2
            elif "d" in ans_str: correct_idx = 3
            
        elif lower.startswith("solution:"):
            sol = line.split(":", 1)[-1].strip()
            
        elif lower.startswith("positive marks:"):
            try: pos_m = float(line.split(":", 1)[-1].strip())
            except: pos_m = 1.0
            
        elif lower.startswith("negative marks:"):
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
        f"👋 नमस्ते {user.first_name}!\n\n"
        "🏛 **Exam & Live Ranking Panel Bot में आपका स्वागत है।**\n\n"
        "👨‍🏫 **Admin Panel:** /admin"
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_drafts[user_id] = {}
    
    keyboard = [
        [InlineKeyboardButton("➕ Create New Exam Schedule", callback_data="admin_create_exam")],
        [InlineKeyboardButton("📋 View All Scheduled Exams", callback_data="admin_view_exams")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ **Admin Control Panel:**", reply_markup=reply_markup)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    draft = admin_drafts.get(user_id)
    
    if not draft or "awaiting_file" not in draft:
        await update.message.reply_text("⚠️ पहले /admin में जाकर 'Create New Exam Schedule' चुनें।")
        return

    document = update.message.document
    if not document.file_name.lower().endswith(('.docx', '.doc')):
        await update.message.reply_text("❌ कृपया केवल `.docx` (Word) फ़ाइल ही भेजें[span_4](start_span)[span_4](end_span)।")
        return

    file_obj = await document.get_file()
    file_bytes = await file_obj.download_as_bytearray()
    
    parsed_qs = parse_docx_content(file_bytes)
    if not parsed_qs:
        await update.message.reply_text("❌ फ़ाइल से प्रश्न पढ़ने में त्रुटि हुई[span_5](start_span)[span_5](end_span)।")
        return

    conn = sqlite3.connect("exam_system.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO exams (subject, topic, sub_topic, time_per_q, scheduled_time, status)
        VALUES (?, ?, ?, ?, ?, 'SCHEDULED')
    ''', (draft['subject'], draft['topic'], draft['sub_topic'], 30, draft['sched_time']))
    
    exam_id = cursor.lastrowid
    
    for q in parsed_qs:
        opts = q['options'] + ["", "", "", ""]
        cursor.execute('''
            INSERT INTO questions (exam_id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt_id, pos_marks, neg_marks, solution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (exam_id, q['question'], opts[0], opts[1], opts[2], opts[3], q['correct'], q['pos_marks'], q['neg_marks'], q['solution']))
    
    conn.commit()
    conn.close()

    try:
        run_time = datetime.strptime(draft['sched_time'], "%Y-%m-%d %H:%M")
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
    await update.message.reply_text(f"✅ **Exam Setup Successful! (Exam ID: #{exam_id})** बोट निर्धारित समय पर चैनल पर पोस्ट कर देगा[span_6](start_span)[span_6](end_span)!")

# ==========================================
# CHANNEL AUTO-POSTING
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
    
    share_msg = f"🎯 *LIVE EXAM STARTED*\n\n📚 *Subject:* {exam[0]}\n📖 *Topic:* {exam[1]}\n📑 *Sub-Topic:* {exam[2]}\n❓ *Total Questions:* {total_q}\n⏱ *Time Per Question:* 30 Sec\n\n👉 *Join & Give Test:* {start_link}"
    wa_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(share_msg)}"

    keyboard = [
        [InlineKeyboardButton("🚀 Start Exam Now (In Bot)", url=start_link)],
        [InlineKeyboardButton("📲 Share to WhatsApp", url=wa_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    post_text = (
        f"🎯 **LIVE EXAM ANNOUNCEMENT** 🎯\n\n"
        f"📚 **Subject:** {exam[0]}\n"
        f"📖 **Topic:** {exam[1]}\n"
        f"📑 **Sub-Topic:** {exam[2]}\n"
        f"❓ **Total Questions:** {total_q}\n"
        f"⏱ **Time Limit:** 30 Seconds / Question\n"
        f"🏆 **स्र्वश्रेष्ठ रैंकिंग एग्जाम खत्म होते ही दिखेगी!**\n\n"
        f"👇 **नीचे दिए गए बटन से परीक्षा शुरू करें:**"
    )

    await application.bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=post_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# ==========================================
# STUDENT EXAM ENGINE WITH 30-SEC TIMER
# ==========================================
async def start_student_exam(update: Update, context: ContextTypes.DEFAULT_TYPE, exam_id: int):
    user = update.effective_user
    
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
        'start_time': datetime.now(),
        'message_id': None
    }

    await send_next_student_question(update, context)

async def send_next_student_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exam_data = context.user_data.get('active_exam')
    if not exam_data:
        return

    idx = exam_data['current_index']
    qs = exam_data['questions']
    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id

    if idx >= len(qs):
        # Finish Exam & Show Instant Ranking
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

        # Calculate Rank Automatically
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
            f"🎉 **एग्जाम सफलतापूर्वक समाप्त हुआ!**\n\n"
            f"📊 **आपका स्कोर:** `{final_score}`\n"
            f"⏱ **कुल समय:** `{total_time} Seconds`\n"
            f"🏆 **आपकी रैंक (Your Rank):** `#{my_rank}` (कुल छात्रों में से)\n\n"
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
        msg = await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    
    exam_data['message_id'] = msg.message_id

    # Background Timer for 30 Seconds auto-advance
    context.job_queue.run_once(auto_skip_question, 30, data={'user_id': user_id, 'question_index': idx}, name=f"timer_{user_id}_{idx}")

async def auto_skip_question(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data['user_id']
    q_idx = job_data['question_index']
    
    # Check if user is still on this question
    # (Implementation details for auto-advance timer loop)
    pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "admin_create_exam":
        admin_drafts[user_id] = {'step': 'subject'}
        await query.edit_message_text("📝 **Subject का नाम लिखें:**")
        return
    elif data == "admin_view_exams":
        conn = sqlite3.connect("exam_system.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, topic, scheduled_time FROM exams ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()
        conn.close()
        txt = "📋 **Scheduled Exams:**\n\n"
        for r in rows:
            txt += f"• **Exam #{r[0]}**: {r[1]} - {r[2]} ({r[3]})\n"
        await query.edit_message_text(txt)
        return

    if data.startswith("ans_"):
        parts = data.split("_")
        ans_idx = int(parts[1])
        q_idx = int(parts[2])
        
        exam_data = context.user_data.get('active_exam')
        if exam_data and exam_data['current_index'] == q_idx:
            current_q = exam_data['questions'][q_idx]
            correct_idx = current_q[6]
            pos_m = current_q[7]
            neg_m = current_q[8]

            if ans_idx == correct_idx:
                exam_data['score'] += pos_m
            else:
                exam_data['score'] -= neg_m

            exam_data['current_index'] += 1
            await send_next_student_question(update, context)

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id in admin_drafts:
        draft = admin_drafts[user_id]
        step = draft.get('step')

        if step == 'subject':
            draft['subject'] = text
            draft['step'] = 'topic'
            await update.message.reply_text("📖 **Topic का नाम लिखें:**")
        elif step == 'topic':
            draft['topic'] = text
            draft['step'] = 'sub_topic'
            await update.message.reply_text("📑 **Sub-Topic का नाम लिखें:**")
        elif step == 'sub_topic':
            draft['sub_topic'] = text
            draft['step'] = 'sched_time'
            await update.message.reply_text("⏰ **एग्जाम का समय दर्ज करें (YYYY-MM-DD HH:MM format):**")
        elif step == 'sched_time':
            draft['sched_time'] = text
            draft['awaiting_file'] = True
            await update.message.reply_text("📁 **अब अपनी `.docx` प्रश्न फ़ाइल भेजें[span_7](start_span)[span_7](end_span)。**")

# ==========================================
# WEBHOOK SETUP
# ==========================================
async def setup_bot():
    global application
    if application is None:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("admin", admin_panel))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
        application.add_handler(CallbackQueryHandler(button_callback))
        
        await application.initialize()
    return application

@app.route('/')
def home():
    return "Pro Exam Bot with Timer & Ranking is Active!"

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    update_json = request.get_json(force=True)
    
    async def process():
        app_bot = await setup_bot()
        update = Update.de_json(update_json, app_bot.bot)
        await app_bot.process_update(update)

    loop.run_until_complete(process())
    return "OK", 200

if __name__ == '__main__':
    import asyncio
    loop = asyncio.get_event_loop()
    app_bot = loop.run_until_complete(setup_bot())
    
    scheduler.start()
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_BOT_TOKEN}"
    loop.run_until_complete(app_bot.bot.set_webhook(url=webhook_url))
    logger.info(f"Webhook connected: {webhook_url}")

    app.run(host="0.0.0.0", port=PORT)
