import os
import re
import threading
import logging
import requests

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

def parse_vtt_timestamp(ts_str: str) -> float:
    parts = ts_str.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h)*3600 + int(m)*60 + float(s.replace(',', '.'))
    elif len(parts) == 2:
        m, s = parts
        return int(m)*60 + float(s.replace(',', '.'))
    return 0.0

def parse_vtt_subtitles(vtt_text: str) -> list:
    lines = vtt_text.splitlines()
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if '-->' in line:
            times = line.split('-->')
            start = parse_vtt_timestamp(times[0])
            end = parse_vtt_timestamp(times[1].split()[0])
            
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip() and '-->' not in lines[i]:
                clean_line = re.sub(r'<[^>]+>', '', lines[i].strip())
                if clean_line:
                    text_lines.append(clean_line)
                i += 1
            
            text = " ".join(text_lines)
            if text:
                segments.append({
                    'start': start,
                    'duration': max(0.5, end - start),
                    'text': text
                })
        else:
            i += 1
    return segments

def get_transcript_piped_proxy(video_id: str) -> list:
    """Mengambil transkrip lewat Jaringan Piped Proxy untuk menembus IP Block YouTube 429."""
    piped_instances = [
        "https://api.piped.privacydev.net",
        "https://pipedapi.kavin.rocks",
        "https://piped-api.garudalinux.org",
        "https://pipedapi.mha.fi"
    ]

    for instance in piped_instances:
        try:
            logger.info(f"Mencoba mengambil transkrip dari Piped Proxy: {instance}")
            url = f"{instance}/streams/{video_id}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                subtitles = data.get("subtitles", [])
                if not subtitles:
                    continue
                
                # Cari bahasa Indonesia, Inggris, atau subtitle apa saja yang ada
                target_sub = None
                for sub in subtitles:
                    code = sub.get("code", "").lower()
                    if code in ["id", "ind", "id-id"]:
                        target_sub = sub
                        break
                if not target_sub:
                    target_sub = subtitles[0] # Ambil subtitle pertama yang ada
                
                sub_url = target_sub.get("url")
                if sub_url:
                    sub_req = requests.get(sub_url, timeout=10)
                    if sub_req.status_code == 200:
                        parsed = parse_vtt_subtitles(sub_req.text)
                        if parsed:
                            logger.info("Berhasil mengambil transkrip via Piped Proxy!")
                            return parsed
        except Exception as e:
            logger.warning(f"Gagal mengambil dari {instance}: {e}")

    # Fallback ke youtube-transcript-api standar jika proxy tidak merespon
    return YouTubeTranscriptApi.get_transcript(video_id, languages=['id', 'en', 'id-ID'])

async def process_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str):
    video_id = extract_video_id(raw_url)
    if not video_id:
        await update.message.reply_text("❌ Link YouTube tidak valid!")
        return

    await update.message.reply_text("⚡ [Bypass Server] Mengambil transkrip teks...")

    try:
        transcript_list = get_transcript_piped_proxy(video_id)
        
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
