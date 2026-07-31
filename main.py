import os
import re
import uuid
import threading
import logging
import requests

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

def get_captions_from_invidious(video_id: str) -> list:
    """Mengambil Auto-Generated Captions via Invidious Instances"""
    instances = [
        "https://inv.us.projectsegfau.lt",
        "https://invidious.nerdvpn.de",
        "https://invidious.drgns.space",
        "https://inv.tux.pizza"
    ]

    for instance in instances:
        try:
            url = f"{instance}/api/v1/captions/{video_id}"
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                captions = r.json().get("captions", [])
                if captions:
                    # Priority: Indonesian -> English -> First Available
                    target = next((c for c in captions if c.get("languageCode") in ["id", "ind"]), None)
                    if not target:
                        target = next((c for c in captions if c.get("languageCode") in ["en", "eng"]), captions[0])
                    
                    sub_url = target.get("url")
                    if sub_url:
                        full_sub_url = f"{instance}{sub_url}" if sub_url.startswith("/") else sub_url
                        vtt_req = requests.get(full_sub_url, timeout=6)
                        if vtt_req.status_code == 200:
                            parsed = parse_vtt_subtitles(vtt_req.text)
                            if parsed:
                                logger.info(f"Berhasil ambil subtitle via Invidious ({instance})")
                                return parsed
        except Exception as e:
            logger.warning(f"Invidious caption failed ({instance}): {e}")
    raise RuntimeError("Invidious no captions")

def get_captions_from_piped(video_id: str) -> list:
    """Mengambil Subtitle/Auto-CC via Piped API"""
    instances = [
        "https://api.piped.yt",
        "https://pipedapi.adminforge.de",
        "https://api.piped.privacydev.net",
        "https://pipedapi.kavin.rocks"
    ]

    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                subtitles = r.json().get("subtitles", [])
                if subtitles:
                    target = next((s for s in subtitles if s.get("code","").lower() in ["id","ind","id-id"]), subtitles[0])
                    sub_url = target.get("url")
                    if sub_url:
                        sub_req = requests.get(sub_url, timeout=6)
                        if sub_req.status_code == 200:
                            parsed = parse_vtt_subtitles(sub_req.text)
                            if parsed:
                                logger.info(f"Berhasil ambil subtitle via Piped ({instance})")
                                return parsed
        except Exception as e:
            logger.warning(f"Piped caption failed ({instance}): {e}")
    raise RuntimeError("Piped no captions")

def download_audio_cobalt_fallback(raw_url: str) -> str:
    """Download audio menggunakan Cobalt Public Endpoints"""
    instances = [
        "https://api.cobalt.tools",
        "https://cobalt-api.kwippy.com",
        "https://co.wuk.sh"
    ]
    payload = {"url": raw_url, "isAudioOnly": True}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    for instance in instances:
        try:
            r = requests.post(f"{instance}/", json=payload, headers=headers, timeout=10)
            if r.status_code == 200:
                audio_url = r.json().get("url")
                if audio_url:
                    out_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.mp3")
                    audio_req = requests.get(audio_url, stream=True, timeout=30)
                    if audio_req.status_code == 200:
                        with open(out_path, "wb") as f:
                            for chunk in audio_req.iter_content(chunk_size=1024*1024):
                                f.write(chunk)
                        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                            return out_path
        except Exception:
            pass
    raise RuntimeError("Gagal mengambil audio dari server.")

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

    await update.message.reply_text("⚡ [Ultra-Bypass System] Memproses teks video...")
    audio_path = None

    try:
        transcript_list = None
        
        # 1. Coba ambil Auto-Caption via Invidious Proxy
        try:
            transcript_list = get_captions_from_invidious(video_id)
        except Exception:
            pass

        # 2. Coba ambil Subtitle via Piped Proxy
        if not transcript_list:
            try:
                transcript_list = get_captions_from_piped(video_id)
            except Exception:
                pass

        # 3. Fallback ke Groq Whisper AI jika tidak ada caption sama sekali
        if not transcript_list:
            await update.message.reply_text("🎙️ Mengunduh suara & memproses dengan Groq Whisper AI...")
            audio_path = download_audio_cobalt_fallback(f"https://www.youtube.com/watch?v={video_id}")
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
