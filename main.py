import os
import re
import json
import uuid
import threading
import logging
import requests
import yt_dlp

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

def parse_json3_subtitles(json_data: dict) -> list:
    segments = []
    events = json_data.get("events", [])
    for ev in events:
        start_ms = ev.get("tStartMs", 0)
        dur_ms = ev.get("dDurationMs", 1000)
        segs = ev.get("segs", [])
        text = "".join([s.get("utf8", "") for s in segs if s.get("utf8")]).strip()
        text = re.sub(r'\s+', ' ', text)
        if text and text != "\n":
            segments.append({
                'start': start_ms / 1000.0,
                'duration': max(0.5, dur_ms / 1000.0),
                'text': text
            })
    return segments

def extract_yt_data_and_captions(video_id: str) -> tuple[list | None, str | None]:
    """
    Gunakan format='all' agar yt-dlp TIDAK MEMILIK FILTER ALASAN 'Requested format is not available'.
    Ekstrak auto-caption langsung dari info_dict yt-dlp.
    """
    cookie_candidate = None
    for candidate in ["cookies.txt", "cookies.txt.txt", "youtube_cookies.txt"]:
        if os.path.exists(candidate):
            cookie_candidate = candidate
            break

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'format': 'all',  # KUNCI UTAMA: Mematikan filter pembawa error
    }
    if cookie_candidate:
        ydl_opts['cookiefile'] = cookie_candidate

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("Gagal mengekstrak informasi video dari YouTube.")

    # 1. PERIKSA SUBTITLE/AUTO-CAPTIONS LANGSUNG DARI YT-DLP METADATA
    subtitles_dict = info.get('subtitles') or {}
    auto_captions_dict = info.get('automatic_captions') or {}

    all_caps = {}
    all_caps.update(auto_captions_dict)
    all_caps.update(subtitles_dict)

    if all_caps:
        target_lang = None
        for lang_code in ['id', 'ind', 'id-ID', 'en', 'en-US']:
            if lang_code in all_caps:
                target_lang = lang_code
                break
        
        if not target_lang:
            target_lang = list(all_caps.keys())[0]

        track_list = all_caps.get(target_lang, [])
        vtt_track = next((t for t in track_list if t.get('ext') == 'vtt'), None)
        json3_track = next((t for t in track_list if t.get('ext') in ['json3', 'json']), None)
        chosen_track = vtt_track or json3_track or (track_list[0] if track_list else None)

        if chosen_track and chosen_track.get('url'):
            sub_url = chosen_track['url']
            resp = requests.get(sub_url, timeout=10)
            if resp.status_code == 200:
                if chosen_track.get('ext') == 'vtt' or 'fmt=vtt' in sub_url:
                    parsed = parse_vtt_subtitles(resp.text)
                    if parsed:
                        return parsed, None
                else:
                    try:
                        parsed = parse_json3_subtitles(resp.json())
                        if parsed:
                            return parsed, None
                    except Exception:
                        parsed = parse_vtt_subtitles(resp.text)
                        if parsed:
                            return parsed, None

    # 2. JIKA CAPTION TIDAK ADA -> AMBIL LINK DIRECT AUDIO STREAM
    formats = info.get('formats', [])
    selected_fmt = next(
        (f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none' and f.get('url')),
        None
    )
    if not selected_fmt:
        selected_fmt = next(
            (f for f in formats if f.get('acodec') != 'none' and f.get('url')),
            None
        )
    if not selected_fmt and formats:
        selected_fmt = formats[0]

    if selected_fmt and selected_fmt.get('url'):
        stream_url = selected_fmt['url']
        ext = selected_fmt.get('ext', 'm4a')
        headers = selected_fmt.get('http_headers', {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        })
        
        out_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.{ext}")
        r = requests.get(stream_url, headers=headers, stream=True, timeout=60)
        if r.status_code == 200:
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                return None, out_path

    raise RuntimeError("Sistem tidak menemukan subtitle maupun link audio pada video ini.")

def transcribe_with_groq_whisper(media_path: str) -> list:
    with open(media_path, "rb") as f:
        response = groq_client.audio.transcriptions.create(
            file=(os.path.basename(media_path), f),
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

    await update.message.reply_text("⚡ [Cookie Engine] Memproses data YouTube...")
    media_path = None

    try:
        transcript_list, media_path = extract_yt_data_and_captions(video_id)

        if not transcript_list and media_path:
            await update.message.reply_text("🎙️ Subtitle tidak tersedia. Memproses audio dengan Groq Whisper AI...")
            transcript_list = transcribe_with_groq_whisper(media_path)

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
        if media_path and os.path.exists(media_path):
            try: os.remove(media_path)
            except Exception: pass

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
