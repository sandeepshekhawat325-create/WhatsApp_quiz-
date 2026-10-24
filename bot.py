import os
import io
import logging
import urllib.parse
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import fitz  # PyMuPDF
import docx

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://whatsapp-quiz-pt5d.onrender.com")
PORT = int(os.environ.get("PORT", 10000))

# Flask App for Render Web Service Health Check
app = Flask(__name__)

# Global In-Memory Queue for Quizzes
quiz_queue = []
is_auto_posting = False

@app.route('/')
def home():
    return "Quiz Bot Webhook is active and running smoothly!"

# Global application variable for webhook handling
application = None

async def setup_bot():
    global application
    if application is None:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(CallbackQueryHandler(button_callback))
        await application.initialize()
    return application

def extract_text_from_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    return text

def extract_text_from_docx(file_bytes):
    doc = docx.Document(io.BytesIO(file_bytes))
    text = "\n".join([p.text for p in doc.paragraphs])
    return text

def parse_mcqs_from_text(text):
    """
    Parser configured for:
    Question: ...
    (a) ...
    (b) ...
    Answer: ...
    Solution: ...
    """
    questions = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    
    current_q = None
    options = []
    correct_idx = 0

    for line in lines:
        lower_line = line.lower()
        
        # Detect Question
        if lower_line.startswith(("question:", "question", "q:", "q.")):
            if current_q and len(options) >= 2:
                questions.append({
                    "question": current_q,
                    "options": options[:4],
                    "correct_option_id": correct_idx
                })
            if ":" in line:
                current_q = line.split(":", 1)[-1].strip()
            else:
                current_q = line
            options = []
            correct_idx = 0
            
        # Detect Options (a), (b), (c), (d) or a), b), c), d)
        elif lower_line.startswith(("(a)", "(b)", "(c)", "(d)", "a)", "b)", "c)", "d.", "a.", "b.", "c.", "d.")):
            if ")" in line:
                opt_text = line.split(")", 1)[-1].strip()
            elif "." in line:
                opt_text = line.split(".", 1)[-1].strip()
            else:
                opt_text = line
            
            if not opt_text:
                opt_text = line
            options.append(opt_text)
            
        # Detect Answer
        elif lower_line.startswith(("answer:", "ans:", "answer")):
            ans_str = line.split(":", 1)[-1].strip().lower() if ":" in line else line.lower()
            if "a" in ans_str: correct_idx = 0
            elif "b" in ans_str: correct_idx = 1
            elif "c" in ans_str: correct_idx = 2
            elif "d" in ans_str: correct_idx = 3

    if current_q and len(options) >= 2:
        questions.append({
            "question": current_q,
            "options": options[:4],
            "correct_option_id": correct_idx
        })

    return questions

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to MCQ Quiz Automation Bot!\n\n"
        "📁 Send me your DOCX/PDF file containing MCQs.\n"
        "I will extract them into quizzes and allow auto-posting to your Telegram Channel with a 📲 *Share to WhatsApp* button!"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_queue
    document = update.message.document
    file_name = document.file_name.lower()
    
    bot_file = await document.get_file()
    file_bytes = await bot_file.download_as_bytearray()
    
    await update.message.reply_text("⏳ Document receive ho gaya hai, MCQs extract kar rahe hain...")
    
    extracted_text = ""
    if file_name.endswith('.pdf'):
        extracted_text = extract_text_from_pdf(file_bytes)
    elif file_name.endswith(('.docx', '.doc')):
        extracted_text = extract_text_from_docx(file_bytes)
    else:
        await update.message.reply_text("❌ Unsupported file format. Please send PDF or DOCX.")
        return

    parsed_quizzes = parse_mcqs_from_text(extracted_text)
    
    if not parsed_quizzes:
        await update.message.reply_text(
            "⚠️ File mein koi valid MCQs nahi mile.\n"
            "Kripya check karein ki file mein 'Question:' aur options '(a)', '(b)', '(c)', '(d)' format mein ho."
        )
        return

    quiz_queue.extend(parsed_quizzes)
    
    keyboard = [
        [InlineKeyboardButton("📤 Post 1 Next Poll Now", callback_data="post_one")],
        [InlineKeyboardButton("▶️ Start Auto Posting", callback_data="start_auto")],
        [InlineKeyboardButton("⏸ Stop Auto Posting", callback_data="stop_auto")],
        [InlineKeyboardButton("📊 View Queue Status", callback_data="status")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Total {len(parsed_quizzes)} MCQs successfully extract ho gaye hain!\n"
        f"📋 Total Queue Size: {len(quiz_queue)}\n\n"
        f"Niche diye gaye options mein se select karein:",
        reply_markup=reply_markup
    )

async def send_next_quiz(context: ContextTypes.DEFAULT_TYPE):
    global quiz_queue
    if not quiz_queue:
        logger.info("Quiz queue empty hai.")
        return False

    item = quiz_queue.pop(0)
    q_text = item["question"]
    opts = item["options"]
    correct = item["correct_option_id"]

    # WhatsApp Link Generator
    wa_text = f"❓ *{q_text}*\n\n"
    for idx, opt in enumerate(opts):
        prefix = ["A", "B", "C", "D"][idx]
        wa_text += f"{prefix}) {opt}\n"
    
    encoded_wa_text = urllib.parse.quote(wa_text)
    whatsapp_share_url = f"https://api.whatsapp.com/send?text={encoded_wa_text}"

    keyboard = [
        [InlineKeyboardButton("📲 Share to WhatsApp", url=whatsapp_share_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if TELEGRAM_CHANNEL_ID:
        try:
            await context.bot.send_poll(
                chat_id=TELEGRAM_CHANNEL_ID,
                question=q_text if len(q_text) <= 300 else q_text[:297] + "...",
                options=opts[:10],
                type=Poll.QUIZ,
                correct_option_id=correct,
                reply_markup=reply_markup
            )
            return True
        except Exception as e:
            logger.error(f"Error posting poll to channel: {e}")
            return False
    return False

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    global is_auto_posting
    
    if query.data == "post_one":
        success = await send_next_quiz(context)
        if success:
            await query.edit_message_text(f"✅ Channel par 1 poll post kar diya gaya hai!\nRemaining Queue: {len(quiz_queue)}")
        else:
            await query.edit_message_text("⚠️ Queue khali hai ya TELEGRAM_CHANNEL_ID configured nahi hai.")
            
    elif query.data == "start_auto":
        is_auto_posting = True
        await query.edit_message_text("▶️ Auto posting start ho gayi hai! Har 5 minute mein 1 poll post hoga...")
        context.job_queue.run_repeating(auto_post_job, interval=300, first=1)

    elif query.data == "stop_auto":
        is_auto_posting = False
        jobs = context.job_queue.get_jobs_by_name("auto_quiz")
        for job in jobs:
            job.schedule_removal()
        await query.edit_message_text("⏸ Auto posting stop kar di gayi hai.")

    elif query.data == "status":
        await query.edit_message_text(f"📊 Current Queue Status:\nTotal Remaining MCQs: {len(quiz_queue)}")

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    global is_auto_posting, quiz_queue
    if is_auto_posting and quiz_queue:
        await send_next_quiz(context)

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
    
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_BOT_TOKEN}"
    loop.run_until_complete(app_bot.bot.set_webhook(url=webhook_url))
    logger.info(f"Webhook set to: {webhook_url}")

    app.run(host="0.0.0.0", port=PORT)
                         
