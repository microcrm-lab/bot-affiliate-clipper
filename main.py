import os
import re
import uuid
import threading
import logging
import requests
import xml.etree.ElementTree as ET

from flask import Flask
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]

GROQ_MODEL    = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3"
TEMP_DIR      = "/tmp/yt_audio"

os.makedirs(TEMP_DIR, exist_ok=True)
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

def get_youtube_innertube_data(video_id: str) -> dict:
    """Mengambil data Player resmi YouTube menggunakan identitas Android Client (Bypass 429/CAPTCHA)"""
    url = "https://www.youtube.com/youtubei/v1/player"
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "19.08.35",
                "androidSdkVersion": 30,
                "hl": "id",
                "gl": "ID"
            }
        },
        "videoId": video_id
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "com.google.android.youtube/19.08.35 (Linux; U; Android 11; id_ID)"
    }
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    if r.status_code == 200:
        return r.json()
    raise RuntimeError(f"YouTube Innertube HTTP {r.status_code}")

def extract_innertube_captions(player_data: dict) -> list:
    """Ekstrak subtitle/auto-generated captions dari data Innertube"""
    captions = player_data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    if not captions:
        raise RuntimeError("Tidak ada subtitle bawaan.")

    # Prioritas: Bahasa Indonesia -> Inggris -> Subtitle pertama yang ada
    target = next((c for c in captions if c.get("languageCode") in ["id", "ind"]), None)
    if not target:
        target = next((c for c in captions if c.get("languageCode") in ["en", "eng"]), captions[0])

    base_url = target.get("baseUrl")
    if not base_url:
        raise RuntimeError("URL subtitle kosong.")

    xml_req = requests.get(base_url, timeout=10)
    if xml_req.status_code != 200:
        raise RuntimeError("Gagal mengunduh XML subtitle.")

    # Parse XML Subtitle YouTube
    root = ET.fromstring(xml_req.text)
    segments = []
    for text_node in root.findall('text'):
        start = float(text_node.attrib.get('start', 0))
        dur = float(text_node.attrib.get('dur', 1))
        txt = text_node.text or ""
        txt = txt.replace('\n', ' ').strip()
        if txt:
            segments.append({
                'start': start,
                'duration': dur,
                'text': txt
            })
    return segments

def download_innertube_audio(player_data: dict) -> str:
    """Download stream audio langsung dari YouTube Android API jika video tidak punya subtitle"""
    streaming_data = player_data.get("streamingData", {})
    adaptive_formats = streaming_data.get("adaptiveFormats", [])
    
    audio_format = next((f for f in adaptive_formats if "audio" in f.get("mimeType", "") and "url" in f), None)
    if not audio_format:
        # Cari format audio lain yang memiliki URL langsung
        for fmt in adaptive_formats:
            if "audio" in fmt.get("mimeType", "") and fmt.get("url"):
                audio_format = fmt
                break

    if not audio_format or not audio_format.get("url"):
        raise RuntimeError("Stream audio tidak ditemukan pada video ini.")

    audio_url = audio_format["url"]
    out_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.m4a")

    audio_req = requests.get(audio_url, stream=True, timeout=60)
    if audio_req.status_code == 200:
        with open(out_path, "wb") as f:
            for chunk in audio_req.iter_content(chunk_size=1024*1024):
                f.write(chunk)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return out_path

    raise RuntimeError("Gagal mengunduh stream audio dari YouTube.")

def transcribe_with_groq_whisper(audio_path: str) -> list:
    with open(audio_path, "rb") as f:
        response = groq_client.audio.transcriptions.create(
            file=(os.path.basename(audio_path), f),
            model=WHISPER_MODEL,
            response_format="verbose_json",
            language="id",
            timestamp_granularities=["segment"]
        )
    
    segments = []
    res_segments = getattr(response, "segments", []) or []
    for seg in res_segments:
        s_start = seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0)
        s_end = seg.get("end", 0) if isinstance(seg, dict) else getattr(seg, "end", 0)
        s_text = seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")
        if s_text.strip():
            segments.append({
                "start": s_start,
                "duration": max(0.5, s_end - s_start),
                "text": s_text.strip()
            })
    return segments

async def process_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str):
    video_id = extract_video_id(raw_url)
    if not video_id:
        await update.message.reply_text("❌ Link YouTube tidak valid!")
        return

    await update.message.reply_text("⚡ [Android Engine] Menghubungi server YouTube...")
    audio_path = None

    try:
        # Ambil metadata & player data via Innertube API Android
        player_data = get_youtube_innertube_data(video_id)
        transcript_list = None

        # 1. Coba Ambil Captions / Subtitle Otomatis
        try:
            transcript_list = extract_innertube_captions(player_data)
            logger.info("Berhasil mengambil subtitle via Innertube Android!")
        except Exception as e:
            logger.info(f"Captions tidak tersedia: {e}")

        # 2. Fallback: Download Stream Audio + Groq Whisper AI jika 0 Subtitle
        if not transcript_list:
            await update.message.reply_text("🎙️ Mengunduh audio resmi & memproses dengan Groq Whisper AI...")
            audio_path = download_innertube_audio(player_data)
            transcript_list = transcribe_with_groq_whisper(audio_path)

        if not transcript_list:
            await update.message.reply_text("⚠️ Gagal mengekstrak kata-kata dari video ini.")
            return

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
            f"❌ Gagal menganalisis video.\nError: `{e}`", 
            parse_mode="Markdown"
        )
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)

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
