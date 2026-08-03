#!/usr/bin/env python3
"""
YouTube Video Clipper & AI Hook Finder Telegram Bot
Created for Render.com Deployment (yt-dlp + Webshare Proxy + 9:16 Crop)
Python 3.10+
"""

import asyncio
from datetime import datetime
from pathlib import Path
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Optional
import urllib.request
import urllib.error
import uuid

# Telegram Bot
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Flask for keep-alive
from flask import Flask, jsonify

# AI & Processing
from groq import AsyncGroq

# Downloader Engines
import yt_dlp
from pytubefix import YouTube

# Safe Auto-Install FFmpeg
import static_ffmpeg


def setup_ffmpeg_safely():
    for attempt in range(1, 4):
        try:
            static_ffmpeg.add_paths()
            print("✅ Mesin FFmpeg siap digunakan.")
            return
        except Exception as err:
            print(
                f"⚠️ [Attempt {attempt}/3] Gagal mengunduh FFmpeg, mencoba"
                f" lagi... ({err})"
            )
            time.sleep(2)


setup_ffmpeg_safely()

# Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ==================== CONFIGURATION ====================
class Config:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "YOUR_GROQ_API_KEY_HERE")
    BASE_DIR = Path(__file__).resolve().parent
    COOKIES_PATH = BASE_DIR / "cookies.txt"
    TEMP_DIR = Path("/tmp/yt_clipper_bot")

    MAX_CLIP_DURATION = 60  # seconds
    MIN_CLIP_DURATION = 20  # seconds
    USER_TIMEOUT = 300  # 5 minutes

    FLASK_PORT = int(os.getenv("PORT", 8080))
    FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

    # Webshare Proxy Configuration
    WEBSHARE_PROXY = os.getenv(
        "WEBSHARE_PROXY",
        "http://ymfbkidl:s2rh40rg42t9@31.59.20.176:6754/"
    )

    PYTUBEFIX_TOKEN_DIR = BASE_DIR / ".pytubefix_tokens"


# Setup Global Proxy untuk Request Cadangan
if Config.WEBSHARE_PROXY:
    proxy_support = urllib.request.ProxyHandler({
        "http": Config.WEBSHARE_PROXY,
        "https": Config.WEBSHARE_PROXY
    })
    opener = urllib.request.build_opener(proxy_support)
    urllib.request.install_opener(opener)
    logger.info("🌐 Webshare Proxy berhasil diaktifkan secara global!")


# ==================== FLASK KEEP-ALIVE ====================
class KeepAliveServer:

    def __init__(self):
        self.app = Flask(__name__)
        self.setup_routes()

    def setup_routes(self):

        @self.app.route("/")
        def home():
            return jsonify({
                "status": "alive",
                "bot": "YouTube Clipper AI",
                "timestamp": datetime.now().isoformat(),
            })

        @self.app.route("/health")
        def health():
            return jsonify({"status": "healthy"})

    def run(self):

        def start():
            self.app.run(host="0.0.0.0", port=Config.FLASK_PORT, debug=False)

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        logger.info(f"🌐 Keep-alive server running on port {Config.FLASK_PORT}")


# ==================== YOUTUBE DOWNLOADER ====================
class YouTubeDownloader:
    """
    YouTube Downloader Multi-Engine:
    Engine 1: yt-dlp + Webshare Proxy (Sangat Stabil, Bebas HTTP 400)
    Engine 2: Cobalt Tunnel API
    """

    @staticmethod
    def _find_downloaded_file(output_dir: Path, file_prefix: str) -> Optional[Path]:
        valid_extensions = {".mp4", ".mkv", ".webm", ".m4v", ".mov"}
        
        candidate_files = [
            f for f in output_dir.glob(f"{file_prefix}_*.*")
            if f.suffix.lower() in valid_extensions 
            and not f.name.endswith(".part") 
            and not f.name.endswith(".ytdl")
            and f.stat().st_size > 10000
        ]
        
        if not candidate_files:
            all_files = sorted(output_dir.glob("*.*"), key=lambda x: x.stat().st_mtime, reverse=True)
            candidate_files = [f for f in all_files if f.suffix.lower() in valid_extensions and f.stat().st_size > 10000]

        if candidate_files:
            return candidate_files[0]

        return None

    @staticmethod
    async def download_video(url: str, output_dir: Path) -> Optional[Dict]:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_prefix = f"vid_{uuid.uuid4().hex}"

        # ENGINE 1: yt-dlp + Webshare Proxy (Solusi Utama Bebas HTTP 400)
        try:
            logger.info("🚀 [Engine 1] Mendownload via yt-dlp + Webshare Proxy...")
            ydl_opts = {
                "outtmpl": str(Path(output_dir) / f"{file_prefix}_%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
                "ffmpeg_location": Config.FFMPEG_PATH,
                "proxy": Config.WEBSHARE_PROXY,
                # Format fleksibel tanpa paksaan ext=mp4 agar tidak melempar error
                "format": "bestvideo*+bestaudio*/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios", "mweb"]
                    }
                },
                "quiet": True,
                "no_warnings": True,
                "nocheckcertificate": True,
                "geo_bypass": True,
                "socket_timeout": 30,
                "retries": 5,
            }

            cookie_file = Config.COOKIES_PATH
            if cookie_file.exists():
                ydl_opts["cookiefile"] = str(cookie_file)

            def _ytdlp_exec():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return info

            info = await asyncio.to_thread(_ytdlp_exec)
            downloaded_file = YouTubeDownloader._find_downloaded_file(output_dir, file_prefix)

            if downloaded_file and downloaded_file.exists() and downloaded_file.stat().st_size > 10000:
                logger.info(f"✅ SUCCESS! Download Berhasil via yt-dlp Engine: {downloaded_file.name}")
                final_path = downloaded_file
                if downloaded_file.suffix.lower() != ".mp4":
                    final_path = await YouTubeDownloader._convert_to_mp4(downloaded_file)

                return {
                    "video_path": final_path,
                    "title": info.get("title", "YouTube Video"),
                    "duration": info.get("duration", 0),
                    "video_id": info.get("id", ""),
                }

        except Exception as e:
            logger.warning(f"⚠️ Engine 1 (yt-dlp) gagal: {e}")

        # ENGINE 2: Cobalt Tunnel API Fallback
        try:
            logger.info("🛡️ [Engine 2] Mencoba Cobalt Tunnel API Fallback...")
            cobalt_file = Path(output_dir) / f"{file_prefix}_cobalt.mp4"
            
            def _cobalt_exec():
                endpoints = ["https://api.cobalt.tools/api/json", "https://co.wuk.sh/api/json"]
                for endpoint in endpoints:
                    try:
                        req = urllib.request.Request(
                            endpoint,
                            data=json.dumps({"url": url, "videoQuality": "720"}).encode('utf-8'),
                            headers={
                                "Accept": "application/json",
                                "Content-Type": "application/json",
                                "Origin": "https://cobalt.tools",
                                "Referer": "https://cobalt.tools/",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                            },
                            method="POST"
                        )
                        with urllib.request.urlopen(req, timeout=25) as response:
                            res_data = json.loads(response.read().decode('utf-8'))
                        
                        dl_url = res_data.get("url")
                        if dl_url:
                            dl_req = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(dl_req, timeout=60) as dl_res, open(cobalt_file, "wb") as f:
                                shutil.copyfileobj(dl_res, f)
                            if cobalt_file.exists() and cobalt_file.stat().st_size > 10000:
                                return True
                    except Exception:
                        continue
                return False

            success = await asyncio.to_thread(_cobalt_exec)
            if success:
                logger.info(f"🎉 SUCCESS! Download via Cobalt Engine: {cobalt_file.name}")
                return {
                    "video_path": cobalt_file,
                    "title": "YouTube Video",
                    "duration": 60,
                    "video_id": "cobalt",
                }
        except Exception as e:
            logger.warning(f"⚠️ Engine 2 (Cobalt) gagal: {e}")

        raise RuntimeError("Gagal mendownload video dari YouTube setelah mencoba seluruh engine.")

    @staticmethod
    async def _convert_to_mp4(input_path: Path) -> Path:
        try:
            output_path = input_path.parent / f"{input_path.stem}_converted.mp4"
            if input_path.suffix.lower() == '.mp4':
                return input_path

            cmd = [
                Config.FFMPEG_PATH, "-i", str(input_path),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(output_path), "-y"
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()

            if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                try: input_path.unlink()
                except: pass
                return output_path

            return input_path
        except Exception:
            return input_path


# ==================== AI PROCESSOR ====================
class AIProcessor:

    def __init__(self):
        self.client = AsyncGroq(api_key=Config.GROQ_API_KEY)

    async def transcribe_video(self, video_path: Path) -> Dict:
        audio_path = None
        try:
            logger.info(f"🎤 Extracting audio & Transcribing: {video_path.name}")

            audio_path = await self._extract_audio(video_path)

            with open(audio_path, "rb") as audio_file:
                transcription = await self.client.audio.transcriptions.create(
                    file=(audio_path.name, audio_file.read()),
                    model="whisper-large-v3",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )

            segments = []
            res_segments = getattr(transcription, "segments", []) or []
            for segment in res_segments:
                s_start = (
                    segment.get("start", 0)
                    if isinstance(segment, dict)
                    else getattr(segment, "start", 0)
                )
                s_end = (
                    segment.get("end", 0)
                    if isinstance(segment, dict)
                    else getattr(segment, "end", 0)
                )
                s_text = (
                    segment.get("text", "")
                    if isinstance(segment, dict)
                    else getattr(segment, "text", "")
                )
                segments.append(
                    {"start": s_start, "end": s_end, "text": s_text.strip()}
                )

            result = {
                "text": getattr(transcription, "text", ""),
                "segments": segments,
            }

            logger.info(f"✅ Transcription complete: {len(segments)} segments")
            return result

        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            raise
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    async def _extract_audio(self, video_path: Path) -> Path:
        if not video_path.exists():
            raise FileNotFoundError(
                f"File video tidak ditemukan di lokasi: {video_path}"
            )

        audio_path = video_path.parent / f"audio_{uuid.uuid4().hex}.wav"
        cmd = [
            Config.FFMPEG_PATH,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio_path),
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if (
            process.returncode != 0
            or not audio_path.exists()
            or audio_path.stat().st_size == 0
        ):
            err_log = (
                stderr.decode("utf-8", errors="ignore") if stderr else "Unknown Error"
            )
            clean_error = err_log.strip()[-300:]
            logger.error(f"FFmpeg audio extraction error log: {clean_error}")
            raise RuntimeError(
                "Gagal mengekstrak audio dari video (pastikan video memiliki suara)."
                f" Detail: {clean_error}"
            )

        return audio_path

    async def find_viral_hook(
        self, transcription: Dict, video_duration: int
    ) -> Dict:
        try:
            logger.info("🔍 Analyzing transcription for viral hooks...")
            segments_text = json.dumps(transcription["segments"], indent=2)

            prompt = f"""Kamu adalah pakar konten viral TikTok Indonesia.
Total durasi video asli: {video_duration} detik.

Tugasmu: Analisis transkrip di bawah ini dan pilih 1 BAGIAN PALING MENARIK & EDUKATIF yang berdurasi MINIMAL 25 DETIK dan MAKSIMAL 60 DETIK.

PENTING: 
- JANGAN cuma memilih 2-5 detik salam pembuka/intro! Pilih bagian pembahasan/tutorial/momen seru yang berbobot.
- SEMUA TULISAN/TEXT HARUS DALAM BAHASA INDONESIA YANG SANTAI DAN MENARIK!

Transkrip berserta timestamp:
{segments_text[:6000]}

Kembalikan respon DALAM FORMAT JSON SAJA:
{{
  "start_seconds": float (detik awal),
  "end_seconds": float (detik akhir, pastikan selisih end_seconds - start_seconds MINIMAL 25 detik!),
  "clip_transcript": "transkrip lengkap dari klip yang dipilih",
  "hook_strength": "skor dari 10 (contoh: 9/10)",
  "hook_type": "Edukasi/Hiburan/Kontroversial/Emosional",
  "hook_analysis": "alasan singkat kenapa bagian ini sangat menarik untuk audiens TikTok Indonesia",
  "cta_tiktok_affiliate": "1 kalimat Call-to-Action persuasif Bahasa Indonesia cocok untuk jualan affiliate TikTok",
  "virality_potential": "Sangat Tinggi/Tinggi/Sedang"
}}"""

            response = await self.client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Kamu adalah pakar strategi konten TikTok Indonesia."
                            " Return ONLY valid JSON dalam Bahasa Indonesia."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                response_format={"type": "json_object"},
            )

            analysis = json.loads(response.choices[0].message.content)

            # Guardrail Durasi
            start_sec = float(analysis.get("start_seconds", 0))
            end_sec = float(analysis.get("end_seconds", 30))

            if (end_sec - start_sec) < Config.MIN_CLIP_DURATION:
                end_sec = min(start_sec + 40, video_duration if video_duration > 0 else start_sec + 40)

            analysis["start_seconds"] = start_sec
            analysis["end_seconds"] = end_sec
            analysis["start_time"] = time.strftime('%M:%S', time.gmtime(start_sec))
            analysis["end_time"] = time.strftime('%M:%S', time.gmtime(end_sec))

            return analysis

        except Exception as e:
            logger.error(f"❌ Hook analysis failed: {e}")
            raise

    async def generate_smart_title(self, hook_analysis: Dict) -> str:
        try:
            prompt = f"""Buatkan 1 judul menarik Bahasa Indonesia gaya TikTok/Shorts yang bikin penasaran berdasarkan transkrip klip ini:
Transkrip: {hook_analysis['clip_transcript'][:200]}
Tipe Hook: {hook_analysis['hook_type']}

Kembalikan format JSON: {{"title": "judul viral di sini #fyp #viral"}}"""

            response = await self.client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                response_format={"type": "json_object"},
            )

            data = json.loads(response.choices[0].message.content)
            return data.get("title", "🔥 Trik Rahasia Yang Wajib Kamu Tahu! #fyp #viral")
        except:
            return "🔥 Trik Rahasia Yang Wajib Kamu Tahu! #fyp #viral"


# ==================== VIDEO CLIPPER ====================
class VideoClipper:

    @staticmethod
    async def clip_video(
        input_path: Path, output_path: Path, start_time: float, end_time: float
    ) -> Path:
        try:
            logger.info(f"✂️ Clipping & Vertical Cropping (9:16): {start_time:.2f}s to {end_time:.2f}s")
            duration = max(1.0, end_time - start_time)

            # Filter FFmpeg: Crop Center 9:16 Vertikal Penuh (No Blackbar)
            crop_filter = "crop=ih*(9/16):ih,scale=1080:1920"

            cmd = [
                Config.FFMPEG_PATH,
                "-ss",
                str(start_time),
                "-i",
                str(input_path),
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "26",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-vf",
                crop_filter,
                str(output_path),
                "-y",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()

            if not output_path.exists() or output_path.stat().st_size == 0:
                raise Exception("Failed to generate clip file.")

            return output_path
        except Exception as e:
            logger.error(f"❌ Clipping failed: {e}")
            raise


# ==================== USER SESSION MANAGER ====================
class UserSession:

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.temp_dir = Config.TEMP_DIR / str(user_id)
        self.created_at = datetime.now()
        self.video_path: Optional[Path] = None
        self.clip_path: Optional[Path] = None
        self.transcription: Optional[Dict] = None
        self.hook_analysis: Optional[Dict] = None

    def cleanup(self):
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"Failed to cleanup session: {e}")


# ==================== TELEGRAM BOT HANDLERS ====================
class YouTubeClipperBot:

    def __init__(self):
        self.application = None
        self.ai_processor = AIProcessor()
        self.sessions: Dict[int, UserSession] = {}

    async def initialize(self):
        self.application = (
            Application.builder().token(Config.TELEGRAM_TOKEN).build()
        )
        self._register_handlers()
        logger.info("🤖 Bot initialized successfully")

    def _register_handlers(self):
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("cancel", self.cmd_cancel))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

    async def _update_status(self, message, text: str, progress: int):
        progress_bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
        full_text = f"{text}\n\n[{progress_bar}] {progress}%"
        try:
            await message.edit_text(full_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = (
            "🎬 *YouTube Video Clipper Bot* 🎬\n\nKirimkan link YouTube (Shorts atau"
            " Video biasa) untuk mulai menganalisis momen viral dan memotong klip"
            " otomatis!"
        )
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = update.effective_user.id
        if user_id in self.sessions:
            self.sessions[user_id].cleanup()
            del self.sessions[user_id]
            await update.message.reply_text(
                "❌ Proses dibatalkan. File sementara dihapus."
            )
        else:
            await update.message.reply_text("Tidak ada proses yang berjalan.")

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        message_text = update.message.text
        url_pattern = (
            r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+"
        )
        urls = re.findall(url_pattern, message_text)

        if urls:
            await self.process_video(update, context, urls[0])
        else:
            await update.message.reply_text(
                "🔗 Silakan kirimkan link YouTube yang valid."
            )

    async def process_video(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
    ):
        user_id = update.effective_user.id

        if user_id in self.sessions:
            self.sessions[user_id].cleanup()

        session = UserSession(user_id)
        self.sessions[user_id] = session

        status_msg = await update.message.reply_text(
            "🎬 *Memproses video...*", parse_mode=ParseMode.MARKDOWN
        )

        try:
            # 1. Download
            await self._update_status(
                status_msg, "📥 Mendownload video dari YouTube...", 10
            )
            downloader = YouTubeDownloader()
            metadata = await downloader.download_video(url, session.temp_dir)
            session.video_path = metadata["video_path"]

            # 2. Transcribe
            await self._update_status(
                status_msg, "🎤 Mentranskripsi audio dengan Groq Whisper...", 35
            )
            transcription = await self.ai_processor.transcribe_video(
                session.video_path
            )
            session.transcription = transcription

            # 3. Analyze Hook
            await self._update_status(
                status_msg, "🧠 Menganalisis hook viral dengan LLaMA AI...", 65
            )
            hook_analysis = await self.ai_processor.find_viral_hook(
                transcription, metadata["duration"]
            )
            session.hook_analysis = hook_analysis

            # 4. Clip Video
            await self._update_status(
                status_msg, "✂️ Memotong klip video 9:16 (Full Screen)...", 85
            )
            clipper = VideoClipper()
            clip_path = session.temp_dir / f"clip_{user_id}.mp4"
            session.clip_path = await clipper.clip_video(
                session.video_path,
                clip_path,
                hook_analysis["start_seconds"],
                hook_analysis["end_seconds"],
            )

            await self._update_status(status_msg, "📤 Mengirimkan hasil...", 95)
            smart_title = await self.ai_processor.generate_smart_title(hook_analysis)

            # Format Response
            response_text = f"""
🎬 *HASIL ANALISIS AI HOOK FINDER*

🏷️ *Judul Viral:* {smart_title}
📹 *Video:* {metadata['title'][:100]}
⏱️ *Durasi Klip:* {hook_analysis['start_time']} - {hook_analysis['end_time']}
🎯 *Kekuatan Hook:* {hook_analysis['hook_strength']}
📊 *Tipe Hook:* {hook_analysis['hook_type']}

💡 *Analisis Hook:*
{hook_analysis['hook_analysis']}

📝 *Transkrip Klip:*
_{hook_analysis['clip_transcript'][:300]}_

🛒 *CTA TikTok Affiliate:*
"{hook_analysis['cta_tiktok_affiliate']}"

✨ _Klip video vertical (9:16 Full Screen) siap diupload ke TikTok/Shorts!_
"""
            # Send Video with Caption
            with open(session.clip_path, "rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    caption=response_text,
                    parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True,
                )

            await status_msg.delete()

        except Exception as e:
            logger.error(f"Error in process_video: {e}")
            await update.message.reply_text(
                f"❌ *Gagal memproses video.*\nError: `{e}`", parse_mode="Markdown"
            )
        finally:
            session.cleanup()
            if user_id in self.sessions:
                del self.sessions[user_id]

    def run(self):
        logger.info("🚀 Starting YouTube Clipper Bot...")
        self.application.run_polling(drop_pending_updates=True)


# ==================== MAIN APPLICATION ====================
def main():
    Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

    keep_alive = KeepAliveServer()
    keep_alive.run()

    bot = YouTubeClipperBot()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.initialize())

    bot.run()


if __name__ == "__main__":
    main()
