import os
import re
import json
import time
import fitz  # PyMuPDF (PDF read karne ke liye bilkul free)
import requests
import threading
from flask import Flask

app = Flask(__name__)

# Render Environment Variables
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

# WhatsApp Channel Bridge Details (Whapi.cloud Free / WAHA / GreenAPI)
WA_API_URL = os.environ.get("WA_API_URL", "")       # Example: https://gate.whapi.cloud
WA_API_TOKEN = os.environ.get("WA_API_TOKEN", "")   # Free WhatsApp API Token
WA_CHANNEL_ID = os.environ.get("WA_CHANNEL_ID", "") # Example: 1203631xxx@newsletter

QUEUE_FILE = "quiz_queue.json"
DAILY_LIMIT = 20  # Rozana 20 Polls

@app.route('/')
def home():
    q_count = len(load_queue())
    return f"PDF-to-WhatsApp Auto Poll Bot is Running! Pending Questions in Queue: {q_count}"

# Queue Load aur Save karne ka system
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

# Telegram par notification bhejne ke liye
def send_tg_msg(chat_id, text):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

# 1. PDF Read karke Bilingual MCQs + Solution nikalne wala Free Parser
def extract_mcqs_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    full_text = ""
    for page in doc:
        full_text += page.get_text("text") + "\n"
    
    # Question blocks ko alag karna (Q1., Q.1, 1., प्रश्न 1. aadi)
    blocks = re.split(r'\n(?=(?:Q\.?\s*\d+|\d+[\.\)]|प्रश्न\s*\d+))', full_text)
    extracted = []
    
    for block in blocks:
        lines = [line.strip() for line in block.strip().split('\n') if line.strip()]
        if len(lines) < 5:
            continue
            
        question_lines = []
        options = []
        answer = "Check Solution"
        explanation = "Special Education Notes | 9828625119"
        
        mode = "q"
        for line in lines:
            # Options pehchanna: (A), A), a., (1) aadi
            if re.match(r'^[\(\[]?[A-Da-d1-4][\)\]\.]\s+', line) and len(options) < 4:
                mode = "opt"
                clean_opt = re.sub(r'^[\(\[]?[A-Da-d1-4][\)\]\.]\s*', '', line).strip()
                options.append(clean_opt[:100])
            # Answer pehchanna
            elif re.match(r'^(?:Ans|Answer|उत्तर|सही उत्तर)\s*[:\-]', line, re.I):
                mode = "ans"
                answer = re.sub(r'^(?:Ans|Answer|उत्तर|सही उत्तर)\s*[:\-]\s*', '', line, flags=re.I).strip()
            # Explanation / Solution pehchanna
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
            # Question number hatakar saaf bilingual question banana
            q_text = re.sub(r'^(?:Q\.?\s*\d+|\d+[\.\)]|प्रश्न\s*\d+)\s*', '', q_text).strip()
            extracted.append({
                "question": q_text[:250],
                "options": options,
                "answer": answer,
                "explanation": explanation
            })
            
    return extracted

# 2. Seedha WhatsApp Channel par Poll + Solution Post karne ka Function
def post_to_whatsapp_channel(quiz):
    # A. Agar WhatsApp API connected hai toh seedha WhatsApp Channel par Poll jayega
    if WA_API_URL and WA_API_TOKEN and WA_CHANNEL_ID:
        headers = {
            "Authorization": f"Bearer {WA_API_TOKEN}",
            "Content-Type": "application/json"
        }
        # Step 1: WhatsApp Native Poll Bhejna
        poll_payload = {
            "to": WA_CHANNEL_ID,
            "title": f"❓ {quiz['question']}",
            "options": quiz["options"],
            "count": 1
        }
        try:
            requests.post(f"{WA_API_URL}/messages/poll", headers=headers, json=poll_payload, timeout=15)
            time.sleep(2)
            # Step 2: Poll ke niche Solution Box Bhejna
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

    # B. Admin ko Telegram par confirmation aur backup Poll bhejna
    if BOT_TOKEN and ADMIN_CHAT_ID:
        # Telegram Poll
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll", json={
            "chat_id": ADMIN_CHAT_ID,
            "question": quiz["question"][:290],
            "options": quiz["options"],
            "is_anonymous": True
        })
        # Solution Card
        sol_card = (
            f"✅ *Answer:* {quiz['answer']}\n"
            f"💡 *Solution:* {quiz['explanation']}\n"
            f"📘 *Special Education Notes | 9828625119*"
        )
        send_tg_msg(ADMIN_CHAT_ID, sol_card)

# 3. Rozana Automatic 20 Polls Bhejne Wala Scheduler (Har ~45 min me 1 poll = Din me 20)
def daily_20_scheduler():
    interval = int((16 * 3600) / DAILY_LIMIT) # Din ke 16 active ghanto me 20 polls (~48 mins gap)
    while True:
        try:
            queue = load_queue()
            if queue:
                next_quiz = queue.pop(0)
                save_queue(queue)
                post_to_whatsapp_channel(next_quiz)
        except Exception as e:
            print("Scheduler Error:", e)
        time.sleep(interval)

# 4. Telegram Par PDF File Receive Karne Wala Listener
def telegram_pdf_listener():
    offset = None
    while True:
        try:
            if not BOT_TOKEN:
                time.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params={"timeout": 20, "offset": offset}, timeout=25).json()
            
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                
                # Jab aap Telegram par PDF dalenge
                if "document" in msg:
                    doc = msg["document"]
                    file_name = doc.get("file_name", "")
                    if file_name.lower().endswith(".pdf"):
                        send_tg_msg(chat_id, "⏳ *PDF mil gayi hai! Questions extract kiye ja rahe hain...*")
                        
                        # Download PDF
                        file_id = doc["file_id"]
                        f_info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}").json()
                        file_path = f_info["result"]["file_path"]
                        pdf_bytes = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}").content
                        
                        local_pdf = "temp_quiz.pdf"
                        with open(local_pdf, "wb") as f:
                            f.write(pdf_bytes)
                            
                        # Extract MCQs
                        new_mcqs = extract_mcqs_from_pdf(local_pdf)
                        if new_mcqs:
                            q_list = load_queue()
                            q_list.extend(new_mcqs)
                            save_queue(q_list)
                            send_tg_msg(
                                chat_id,
                                f"✅ *Success!* PDF se *{len(new_mcqs)} Bilingual Questions* nikal liye gaye hain.\n"
                                f"📊 Total Pending Queue: *{len(q_list)} Questions*\n"
                                f"🚀 Bot ab bina kisi command ke rozana automatic 20 Polls + Solution bhejta rahega!"
                            )
                        else:
                            send_tg_msg(chat_id, "⚠️ PDF me standard format (Q. / A) B) C) D) / Ans:) nahi mila. Kripya text-based PDF bhejein.")
                
                elif msg.get("text") == "/status":
                    q_list = load_queue()
                    send_tg_msg(chat_id, f"📊 *Queue Status:* Abhi *{len(q_list)}* questions line me hain. Rozana 20 post ho rahe hain.")
                    
        except Exception as e:
            print("Listener Error:", e)
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=daily_20_scheduler, daemon=True).start()
    threading.Thread(target=telegram_pdf_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
