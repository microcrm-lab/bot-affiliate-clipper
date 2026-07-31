import os
import re
import threading
import logging

from youtube_transcript_api import YouTubeTranscriptApi
from flask import Flask
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GROQ_MODEL    = "llama-3.1-8b-instant"

groq_client = Groq(api_key=GROQ_API_KEY)

# Flask Keep-Alive Server
flask_app = Flask(__name__)
@flask_app.route("/")
def index(): return "Bot Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

YT_PATTERN = re.compile(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_\-]{11})")

def extract_video_id(text: str) -> str | None:
    m = YT_PATTERN.search(text)
    return m.group(1) if m else None

def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

async def process_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str):
    video_id = extract_video_id(raw_url)
    if not video_id:
        await update.message.reply_text("❌ Link YouTube tidak valid!")
        return

    await update.message.reply_text("⚡ [GitHub Server] Mengambil transkrip teks langsung...")

    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['id', 'en', 'id-ID'])
        
        segment_lines = []
        for item in transcript_list:
            start = item['start']
            end = start + item['duration']
            text = item['text'].strip()
            segment_lines.append(f"[{fmt_time(start)} - {fmt_time(end)}] {text}")
        
        segments_text = "\n".join(segment_lines)

        prompt = f"""Berikut transkrip video YouTube:
{segments_text}

---
Tugasmu:
1. Pilih 1 momen paling viral/hook terkuat (durasi 30-60 detik).
2. Tuliskan Timestamp (MM:SS - MM:SS).
3. Tuliskan kutipan transkripnya.
4. Buatkan 1 Teks CTA TikTok Affiliate yang bagus.

Format Jawaban:
⏱️ Timestamp: MM:SS - MM:SS
📝 Transkrip:
[isi transkrip]

🛒 CTA TikTok:
[teks CTA]
"""
        await update.message.reply_text("🧠 Menganalisis momen viral dengan Groq LLaMA...")
        
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Kamu analis konten TikTok Affiliate. Jawab Bahasa Indonesia."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        result_text = response.choices[0].message.content
        await update.message.reply_text(result_text)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(
            f"❌ Gagal mengambil transkrip.\nError: `{e}`\n(Pastikan video YouTube ini memiliki Subtitle/Teks bawaan!)", 
            parse_mode="Markdown"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if extract_video_id(text):
        await process_youtube(update, context, text)
    else:
        await update.message.reply_text("Kirimkan link YouTube untuk mencari momen viral!")

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
