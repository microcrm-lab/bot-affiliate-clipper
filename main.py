import os
import re
import json
import uuid
import threading
import logging
import subprocess
import requests
import yt_dlp
import static_ffmpeg

# Pasang FFmpeg otomatis ke PATH sistem
static_ffmpeg.add_paths()

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
TEMP_DIR      = "/tmp/yt_bot"

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

def parse_timestamp(ts_str: str) -> float:
    parts = ts_str.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h)*3600 + int(m)*60 + float(s.replace(',', '.'))
    elif len(parts) == 2:
        m, s = parts
        return int(m)*60 + float(s.replace(',', '.'))
    return 0.0

def get_cookie_file() -> str | None:
    for candidate in ["cookies.txt", "cookies.txt.txt", "youtube_cookies.txt"]:
        if os.path.exists(candidate):
            return candidate
    return None

def download_full_video(video_id: str) -> str:
    """Download video lengkap ke lokal disk agar siap dipotong offline oleh FFmpeg"""
    file_prefix = os.path.join(TEMP_DIR, f"full_{uuid.uuid4().hex}")
    out_tmpl = f"{file_prefix}.%(ext)s"
    cookie_file = get_cookie_file()

    client_strategies = [
        ['android', 'ios', 'mweb'],
        ['tv', 'mweb'],
        ['ios']
    ]

    last_error = None
    for client in client_strategies:
        ydl_opts = {
            'outtmpl': out_tmpl,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'extractor_args': {
                'youtube': {
                    'player_client': client
                }
            }
        }
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                downloaded_file = ydl.prepare_filename(info)

            if os.path.exists(downloaded_file) and os.path.getsize(downloaded_file) > 1000:
                return downloaded_file
        except Exception as e:
            last_error = e
            logger.warning(f"Download video dengan client {client} gagal: {e}")
            continue

    # Fallback standar
    try:
        ydl_opts_basic = {
            'outtmpl': out_tmpl,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
        }
        if cookie_file:
            ydl_opts_basic['cookiefile'] = cookie_file

        with yt_dlp.YoutubeDL(ydl_opts_basic) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            downloaded_file = ydl.prepare_filename(info)

        if os.path.exists(downloaded_file) and os.path.getsize(downloaded_file) > 1000:
            return downloaded_file
    except Exception as e:
        last_error = e

    raise RuntimeError(f"Gagal mengunduh video dari YouTube. Detail: `{last_error}`")

def transcribe_with_groq_whisper(media_path: str) -> list:
    """Groq Whisper AI menerima file MP4 lokal secara langsung"""
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

def cut_video_locally(input_video_path: str, start_str: str, end_str: str) -> str:
    """Potong video secara OFFLINE menggunakan FFmpeg dari file lokal (100% cepat & bebas error)"""
    start_sec = parse_timestamp(start_str)
    end_sec = parse_timestamp(end_str)

    if end_sec <= start_sec:
        end_sec = start_sec + 15

    duration = end_sec - start_sec
    output_clip_path = os.path.join(TEMP_DIR, f"clip_{uuid.uuid4().hex}.mp4")

    # Fast Stream Copy
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start_sec),
        '-i', input_video_path,
        '-t', str(duration),
        '-c', 'copy',
        output_clip_path
    ]

    logger.info(f"Menjalankan FFmpeg pemotong lokal...")
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if os.path.exists(output_clip_path) and os.path.getsize(output_clip_path) > 1000:
        return output_clip_path

    # Fallback re-encode jika stream copy gagal
    cmd_reencode = [
        'ffmpeg', '-y',
        '-ss', str(start_sec),
        '-i', input_video_path,
        '-t', str(duration),
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast',
        output_clip_path
    ]
    subprocess.run(cmd_reencode, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if os.path.exists(output_clip_path) and os.path.getsize(output_clip_path) > 1000:
        return output_clip_path

    raise RuntimeError("FFmpeg gagal memotong video lokal.")

async def process_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str):
    video_id = extract_video_id(raw_url)
    if not video_id:
        await update.message.reply_text("❌ Link YouTube tidak valid!")
        return

    status_msg = await update.message.reply_text("⚡ [Local Engine] Mengunduh video YouTube...")
    raw_video_path = None
    clip_path = None

    try:
        # 1. Download Video Utuh ke Server
        raw_video_path = download_full_video(video_id)

        # 2. Transkripkan via Groq Whisper AI
        await status_msg.edit_text("🎙️ Memproses transkripsi suara dengan Groq Whisper AI...")
        transcript_list = transcribe_with_groq_whisper(raw_video_path)

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
1. Pilih 1 momen paling viral/hook terkuat (durasi 15-60 detik).
2. Tuliskan Timestamp persis dengan format MM:SS - MM:SS.
3. Tuliskan kutipan transkripnya.
4. Buatkan 1 Teks CTA TikTok Affiliate yang bagus.

SANGAT PENTING:
Sertakan baris ini persis di paling bawah jawabanmu:
CLIP_TIME: MM:SS - MM:SS

Format Jawaban:
⏱️ Timestamp: MM:SS - MM:SS
📝 Transkrip:
[isi transkrip]

🛒 CTA TikTok:
[teks CTA]

CLIP_TIME: MM:SS - MM:SS
"""
        await status_msg.edit_text("🧠 Menganalisis momen viral dengan Groq LLaMA...")
        
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Kamu analis konten TikTok Affiliate. Jawab Bahasa Indonesia."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        result_text = response.choices[0].message.content
        
        clip_match = re.search(r"CLIP_TIME:\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", result_text)
        clean_result_text = re.sub(r"\n*CLIP_TIME:.*", "", result_text).strip()

        await update.message.reply_text(clean_result_text)

        # 3. Potong Klip Secara OFFLINE & Kirim File MP4
        if clip_match:
            start_str = clip_match.group(1)
            end_str = clip_match.group(2)
            
            await update.message.reply_text(f"✂️ Memotong klip video MP4 [{start_str} - {end_str}] secara offline...")
            try:
                clip_path = cut_video_locally(raw_video_path, start_str, end_str)
                with open(clip_path, "rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=f"🎬 Klip Momen Viral [{start_str} - {end_str}]\nSiap diposting ke TikTok!"
                    )
            except Exception as clip_err:
                logger.error(f"Gagal memotong klip: {clip_err}")
                await update.message.reply_text(f"⚠️ Teks analisis berhasil, tapi gagal memotong klip video: `{clip_err}`", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(
            f"❌ Gagal menganalisis video.\nError: `{e}`", 
            parse_mode="Markdown"
        )
    finally:
        # Bersihkan file dari memori server
        if raw_video_path and os.path.exists(raw_video_path):
            try: os.remove(raw_video_path)
            except Exception: pass
        if clip_path and os.path.exists(clip_path):
            try: os.remove(clip_path)
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
