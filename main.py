
import os
import requests
import tempfile
import threading
import time
from flask import Flask, request
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

def send_message(chat_id, text):
    requests.post(f"{TG}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    }, timeout=30)

def send_file(chat_id, path, caption):
    with open(path, "rb") as f:
        requests.post(f"{TG}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": f}, timeout=120)

def get_audio(message):
    for key in ("voice", "audio", "document", "video"):
        item = message.get(key)
        if item and (key != "document" or
                     item.get("mime_type", "").startswith("audio/")):
            return item
    return None

def transcribe(file_path):
    with open(file_path, "rb") as audio:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "json"
            },
            files={"file": audio},
            timeout=600
        )
    response.raise_for_status()
    return response.json().get("text", "").strip()

def make_pdf(text, output_path):
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib import colors
    from xml.sax.saxutils import escape

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(font_path):
        pdfmetrics.registerFont(TTFont("DejaVu", font_path))
        font = "DejaVu"
    else:
        font = "Helvetica"

    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        def shape(line):
            return get_display(arabic_reshaper.reshape(line))
    except ImportError:
        def shape(line):
            return line

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm
    )
    style = ParagraphStyle(
        "Body", fontName=font, fontSize=11,
        leading=19, alignment=TA_RIGHT,
        textColor=colors.HexColor("#222222")
    )
    story = []
    for line in text.splitlines():
        story.append(Paragraph(escape(shape(line)) or " ", style))
        story.append(Spacer(1, 5))
    doc.build(story)

def process_update(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    if message.get("text", "").startswith("/start"):
        send_message(chat_id,
            "به LIBRA Transcribe خوش آمدید.\n"
            "ویس یا فایل صوتی خود را ارسال کنید تا به متن و PDF تبدیل شود.")
        return

    audio = get_audio(message)
    if not audio:
        send_message(chat_id, "لطفاً ویس یا فایل صوتی ارسال کنید.")
        return

    if audio.get("file_size", 0) > 20 * 1024 * 1024:
        send_message(chat_id, "حجم فایل نباید بیشتر از ۲۰ مگابایت باشد.")
        return

    send_message(chat_id, "فایل دریافت شد؛ در حال تبدیل صوت به متن هستم...")

    try:
        info = requests.get(
            f"{TG}/getFile",
            params={"file_id": audio["file_id"]},
            timeout=30
        ).json()

        if not info.get("ok"):
            raise RuntimeError("Telegram file download failed")

        file_path = info["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

        with tempfile.TemporaryDirectory() as folder:
            audio_path = os.path.join(folder, "audio")
            r = requests.get(download_url, timeout=180)
            r.raise_for_status()

            with open(audio_path, "wb") as f:
                f.write(r.content)

            text = transcribe(audio_path)
            if not text:
                send_message(chat_id, "متنی از فایل تشخیص داده نشد.")
                return

            txt_path = os.path.join(folder, "transcript.txt")
            pdf_path = os.path.join(folder, "transcript.pdf")

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)

            make_pdf(text, pdf_path)

            send_message(chat_id, "تبدیل انجام شد. متن و PDF شما آماده است.")
            send_file(chat_id, txt_path, "خروجی متنی")
            send_file(chat_id, pdf_path, "خروجی PDF")

    except Exception:
        send_message(chat_id,
            "در تبدیل فایل خطایی رخ داد. لطفاً فایل دیگری امتحان کنید.")

@app.get("/")
def health():
    return "LIBRA Transcribe is running", 200

@app.post("/webhook/" + WEBHOOK_SECRET)
def webhook():
    update = request.get_json(silent=True)
    if update:
        threading.Thread(
            target=process_update, args=(update,), daemon=True
        ).start()
    return "OK", 200

def set_webhook():
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
    for _ in range(5):
        try:
            r = requests.post(
                f"{TG}/setWebhook",
                json={"url": url},
                timeout=20
            )
            print("Webhook:", r.text)
            return
        except Exception as e:
            print("Webhook setup retry:", e)
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=set_webhook, daemon=True).start()
    app.run(host="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)))
