import os
import re
import json
import time
import pymupdf  # PDF read karne ke liye
from docx import Document  # Word (.docx) read karne ke liye
import feedparser  # YouTube RSS feed read karne ke liye
import requests
import threading
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "@special_education_needs")

WA_API_URL = os.environ.get("WA_API_URL", "")
WA_API_TOKEN = os.environ.get("WA_API_TOKEN", "")
WA_CHANNEL_ID = os.environ.get("WA_CHANNEL_ID", "")
YT_CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "")

QUEUE_FILE = "quiz_queue.json"
STATE_FILE = "bot_state.json"

@app.route('/')
def home():
    q_count = len(load_queue())
    status = "RUNNING" if is_bot_active() else "PAUSED"
    return f"Bot Status: {status} | Pending Questions in Queue: {q_count}"

# Bot State (Active / Paused) Handle Karne Ka Logic
def is_bot_active():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("active", True)
        except:
            return True
    return True

def set_bot_active(state_bool):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"active": state_bool}, f)

# App Notification Webhook (Instant WhatsApp & Telegram Notification)
@app.route('/app-notification', methods=['POST'])
def app_notification():
    try:
        data = request.json or request.form
        notif_title = data.get("title", "App Notification")
        notif_msg = data.get("message", "Nayi update aayi hai!")
        
        formatted_msg = (
            f"🔔 *{notif_title}*\n\n"
            f"{notif_msg}\n\n"
            f"📘 *Special Education Notes | 9828625119*"
        )
        send_whatsapp_msg(formatted_msg)
        send_tg_channel_msg(formatted_msg)
        return {"status": "success", "message": "Notification sent to WhatsApp and Telegram!"}, 200
    except Exception as e:
        return {"status": "error", "error": str(e)}, 500

def load_queue():
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_queue(data):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_tg_msg_with_buttons(chat_id, text, keyboard=None):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram Send Error:", e)

def send_tg_msg(chat_id, text):
    send_tg_msg_with_buttons(chat_id, text)

def send_tg_channel_msg(text):
    if BOT_TOKEN and TG_CHANNEL_ID:
        send_tg_msg(TG_CHANNEL_ID, text)

def send_tg_channel_poll(quiz):
    if BOT_TOKEN and TG_CHANNEL_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll", json={
                "chat_id": TG_CHANNEL_ID,
                "question": quiz["question"][:290],
                "options": quiz["options"],
                "is_anonymous": True
            }, timeout=10)
        except Exception as e:
            print("Telegram Channel Poll Error:", e)

def send_whatsapp_msg(text):
    if WA_API_URL and WA_API_TOKEN and WA_CHANNEL_ID:
        headers = {
            "Authorization": f"Bearer {WA_API_TOKEN}",
            "Content-Type": "application/json"
        }
        try:
            requests.post(f"{WA_API_URL}/messages/text", headers=headers, json={
                "to": WA_CHANNEL_ID,
                "body": text
            }, timeout=15)
        except Exception as e:
            print("WhatsApp API Error:", e)

# DOCX file read
def extract_text_from_docx(docx_path):
    try:
        doc = Document(docx_path)
        full_text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        return full_text
    except Exception as e:
        print("DOCX Read Error:", e)
        return ""

# PDF file read
def extract_text_from_pdf(pdf_path):
    try:
        doc = pymupdf.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text("text") + "\n"
        return full_text
    except Exception as e:
        print("PDF Read Error:", e)
        return ""

# MCQ Parser (Supports unlimited questions at once)
def extract_mcqs_from_text(full_text):
    blocks = re.split(r'\n(?=Question:|\bQ\.?\s*\d+|\b\d+[\.\)]|प्रश्न\s*\d+)', full_text)
    extracted = []
    
    for block in blocks:
        lines = [line.strip() for line in block.strip().split('\n') if line.strip()]
        if len(lines) < 3:
            continue
            
        question_lines = []
        options = []
        answer = "Check Solution"
        explanation = "Special Education Notes | 9828625119"
        
        mode = "q"
        for line in lines:
            if re.match(r'^[\(\[]?[A-Da-d1-4][\)\]\.]\s+', line) and len(options) < 4:
                mode = "opt"
                clean_opt = re.sub(r'^[\(\[]?[A-Da-d1-4][\)\]\.]\s*', '', line).strip()
                options.append(clean_opt[:100])
            elif re.match(r'^(?:Ans|Answer|उत्तर|सही उत्तर)\s*[:\-]', line, re.I):
                mode = "ans"
                answer = re.sub(r'^(?:Ans|Answer|उत्तर|सही उत्तर)\s*[:\-]\s*', '', line, flags=re.I).strip()
            elif re.match(r'^(?:Exp|Explanation|Solution|व्याख्या|हल)\s*[:\-]', line, re.I):
                mode = "exp"
                explanation = re.sub(r'^(?:Exp|Explanation|Solution|व्याख्या|हल)\s*[:\-]\s*', '', line, flags=re.I).strip()
            else:
                if mode == "q":
                    question_lines.append(line)
                elif mode == "exp":
                    explanation += " " + line
                    
        if question_lines and len(options) == 4:
            q_text = " ".join(question_lines)
            q_text = re.sub(r'^(?:Question:|\bQ\.?\s*\d+|\b\d+[\.\)]|प्रश्न\s*\d+)\s*', '', q_text, flags=re.I).strip()
            extracted.append({
                "question": q_text[:250],
                "options": options,
                "answer": answer,
                "explanation": explanation
            })
            
    return extracted

# Quiz Posting Logic
def post_next_quiz_manually(chat_id):
    if not is_bot_active():
        send_tg_msg(chat_id, "🛑 Bot abhi **PAUSED** hai! Pehle '▶️ Resume Queue' par click karein.")
        return
        
    queue = load_queue()
    if not queue:
        send_tg_msg(chat_id, "⚠️ Queue khali hai! Pehle koi PDF ya DOCX file bhejein.")
        return
    
    quiz = queue.pop(0)
    save_queue(queue)
    
    # 1. WhatsApp Channel
    if WA_API_URL and WA_API_TOKEN and WA_CHANNEL_ID:
        headers = {
            "Authorization": f"Bearer {WA_API_TOKEN}",
            "Content-Type": "application/json"
        }
        poll_payload = {
            "to": WA_CHANNEL_ID,
            "title": f"❓ {quiz['question']}",
            "options": quiz["options"],
            "count": 1
        }
        try:
            requests.post(f"{WA_API_URL}/messages/poll", headers=headers, json=poll_payload, timeout=15)
            time.sleep(2)
            sol_text = (
                f"✅ *सही उत्तर (Answer):* {quiz['answer']}\n"
                f"💡 *व्याख्या (Solution):* {quiz['explanation']}\n\n"
                f"📘 *Special Education Notes | 9828625119*"
            )
            requests.post(f"{WA_API_URL}/messages/text", headers=headers, json={
                "to": WA_CHANNEL_ID,
                "body": sol_text
            }, timeout=15)
        except Exception as e:
            print("WhatsApp API Error:", e)

    # 2. Telegram Channel
    if BOT_TOKEN and TG_CHANNEL_ID:
        try:
            send_tg_channel_poll(quiz)
            time.sleep(2)
            sol_card = (
                f"✅ *Answer:* {quiz['answer']}\n"
                f"💡 *Solution:* {quiz['explanation']}\n"
                f"📘 *Special Education Notes | 9828625119*"
            )
            send_tg_channel_msg(sol_card)
        except Exception as e:
            print("Telegram Channel Post Error:", e)
            
    remaining = len(queue)
    get_control_menu(chat_id, f"✅ Poll successfully post ho gaya hai!\n📊 Baaki Queue: *{remaining} Questions*")

def get_control_menu(chat_id, custom_msg="Control Panel:"):
    active = is_bot_active()
    status_str = "🟢 Active" if active else "🔴 Paused"
    q_list = load_queue()
    
    toggle_btn = {"text": "⏸️ Pause Queue", "callback_data": "pause"} if active else {"text": "▶️ Resume Queue", "callback_data": "resume"}
    
    control_menu = {
        "inline_keyboard": [
            [{"text": "📤 Post Next Poll Now", "callback_data": "post_next"}],
            [toggle_btn],
            [{"text": "📊 Check Status", "callback_data": "status"}, {"text": "🗑️ Clear Queue", "callback_data": "clear"}]
        ]
    }
    
    text = (
        f"{custom_msg}\n\n"
        f"🤖 Bot Status: *{status_str}*\n"
        f"📊 Total Queue: *{len(q_list)} Questions*"
    )
    send_tg_msg_with_buttons(chat_id, text, control_menu)

# YouTube RSS feed listener
def youtube_feed_listener():
    if not YT_CHANNEL_ID:
        return
    last_video_id = None
    while True:
        try:
            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
            feed = feedparser.parse(feed_url)
            if feed.entries:
                latest = feed.entries[0]
                vid_id = latest.get("id", "")
                if last_video_id is None:
                    last_video_id = vid_id
                elif vid_id != last_video_id:
                    last_video_id = vid_id
                    title = latest.get("title", "")
                    link = latest.get("link", "")
                    msg = (
                        f"📺 *Naya YouTube Video Upload Hua Hai!*\n\n"
                        f"*{title}*\n"
                        f"🔗 {link}\n\n"
                        f"📘 *Special Education Notes | 9828625119*"
                    )
                    send_whatsapp_msg(msg)
                    send_tg_channel_msg(msg)
        except Exception as e:
            print("YouTube Feed Error:", e)
        time.sleep(300) # Har 5 minute me YouTube check karega

# Telegram Listener
def telegram_file_listener():
    if not BOT_TOKEN:
        return
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
    except Exception as e:
        print("Webhook Clear Error:", e)

    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params={"timeout": 20, "offset": offset}, timeout=25).json()
            
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                
                # Callback Queries (Buttons)
                if "callback_query" in update:
                    cq = update["callback_query"]
                    query_id = cq["id"]
                    data = cq["data"]
                    chat_id = cq["message"]["chat"]["id"]
                    
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": query_id})
                    
                    if data == "post_next":
                        post_next_quiz_manually(chat_id)
                    elif data == "pause":
                        set_bot_active(False)
                        get_control_menu(chat_id, "⏸️ *Queue Rok Di Gayi Hai!* Ab koi automatic ya manual post tab tak nahi jayega jab tak aap Resume nahi karte.")
                    elif data == "resume":
                        set_bot_active(True)
                        get_control_menu(chat_id, "▶️ *Queue Wapas Chalu Ho Gayi Hai!*")
                    elif data == "status":
                        get_control_menu(chat_id, "📊 Current Queue Status Update:")
                    elif data == "clear":
                        save_queue([])
                        get_control_menu(chat_id, "🗑️ Queue poori tarah khali kar di gayi hai!")
                    continue
                
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                
                if "document" in msg:
                    doc = msg["document"]
                    file_name = doc.get("file_name", "")
                    file_id = doc["file_id"]
                    
                    f_info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}", timeout=10).json()
                    file_path = f_info["result"]["file_path"]
                    file_bytes = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=15).content
                    
                    full_text = ""
                    if file_name.lower().endswith(".pdf"):
                        send_tg_msg(chat_id, "⏳ *PDF mil gayi hai! Sabhi questions extract kiye ja rahe hain...*")
                        with open("temp_quiz.pdf", "wb") as f:
                            f.write(file_bytes)
                        full_text = extract_text_from_pdf("temp_quiz.pdf")
                        
                    elif file_name.lower().endswith(".docx"):
                        send_tg_msg(chat_id, "⏳ *Word File (.docx) mil gayi hai! Sabhi questions extract kiye ja rahe hain...*")
                        with open("temp_quiz.docx", "wb") as f:
                            f.write(file_bytes)
                        full_text = extract_text_from_docx("temp_quiz.docx")
                        
                    if full_text:
                        new_mcqs = extract_mcqs_from_text(full_text)
                        if new_mcqs:
                            q_list = load_queue()
                            q_list.extend(new_mcqs)
                            save_queue(q_list)
                            
                            get_control_menu(
                                chat_id,
                                f"✅ *Success!* File se kul *{len(new_mcqs)} Questions* safaltapoorvak extract ho gaye hain."
                            )
                        else:
                            send_tg_msg(chat_id, "⚠️ File mein standard format (Question: / (a) (b) (c) (d) / Answer:) nahi mila.")
                
                elif msg.get("text") in ["/start", "/menu", "/status"]:
                    get_control_menu(chat_id, "👋 *Welcome to Control Menu!*")
                    
        except Exception as e:
            print("Listener Error:", e)
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=youtube_feed_listener, daemon=True).start()
    threading.Thread(target=telegram_file_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
