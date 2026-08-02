#!/usr/bin/env python3
"""
YouTube Video Clipper & AI Hook Finder Telegram Bot
Created for Render.com Deployment
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
import yt_dlp

# Safe Auto-Install FFmpeg dengan sistem Retry Anti-Disconnect
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
    MIN_CLIP_DURATION = 15  # seconds
    USER_TIMEOUT = 300  # 5 minutes

    FLASK_PORT = int(os.getenv("PORT", 8080))
    FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"


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
    YouTube Downloader dengan Rotasi Player Client & Path Cookies Presisi
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

        # Rotasi player_client & format strategy
        strategies = [
            {"client": ["mweb"], "fmt": "b/best"},
            {"client": ["android"], "fmt": "bv*+ba/b"},
            {"client": ["ios"], "fmt": "best"},
            {"client": ["web"], "fmt": None},
        ]

        last_error = None

        for attempt, strat in enumerate(strategies, 1):
            try:
                logger.info(f"🔄 Mencoba strategi #{attempt} (Client: {strat['client']}, Format: {strat['fmt'] or 'default'})...")
                
                ydl_opts = {
                    "outtmpl": str(Path(output_dir) / f"{file_prefix}_%(id)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "ffmpeg_location": Config.FFMPEG_PATH,
                    "extractor_args": {
                        "youtube": {
                            "player_client": strat["client"]
                        }
                    },
                    "quiet": True,
                    "no_warnings": True,
                    "nocheckcertificate": True,
                    "geo_bypass": True,
                    "socket_timeout": 30,
                    "retries": 5,
                }

                if strat["fmt"] is not None:
                    ydl_opts["format"] = strat["fmt"]

                # Cek dan muat cookies
                cookie_file = Config.COOKIES_PATH
                if cookie_file.exists():
                    logger.info(f"🍪 Menggunakan cookies dari: {cookie_file}")
                    ydl_opts["cookiefile"] = str(cookie_file)
                else:
                    logger.warning(f"⚠️ File cookies tidak ditemukan di: {cookie_file}")

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    
                    downloaded_file = YouTubeDownloader._find_downloaded_file(output_dir, file_prefix)

                    if downloaded_file and downloaded_file.exists():
                        logger.info(f"✅ Download Berhasil: {downloaded_file.name}")
                        
                        final_path = downloaded_file
                        if downloaded_file.suffix.lower() != '.mp4':
                            final_path = await YouTubeDownloader._convert_to_mp4(downloaded_file)

                        return {
                            "video_path": final_path,
                            "title": info.get("title", "Unknown Title"),
                            "duration": info.get("duration", 0),
                            "video_id": info.get("id", ""),
                        }

            except Exception as e:
                logger.warning(f"⚠️ Strategi #{attempt} gagal: {e}")
                last_error = e
                await asyncio.sleep(1)

        raise RuntimeError(f"Gagal mendownload video dari YouTube setelah mencoba semua strategi. Error: {last_error}")

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

            prompt = f"""You are a viral content expert analyzing a YouTube video transcript.
Video total duration: {video_duration} seconds.

Analyze these timestamped segments and find the SINGLE most engaging, viral-worthy clip (15-60 seconds).

Segments with timestamps:
{segments_text[:6000]}

Return your analysis as valid JSON ONLY:
{{
  "start_time": "MM:SS format",
  "end_time": "MM:SS format",
  "start_seconds": float,
  "end_seconds": float,
  "clip_transcript": "the exact transcript of the selected clip",
  "hook_strength": "score out of 10",
  "hook_type": "emotional/educational/controversial/entertaining",
  "hook_analysis": "brief explanation why this is viral-worthy",
  "cta_tiktok_affiliate": "1 persuasive TikTok CTA optimized for affiliate marketing",
  "virality_potential": "high/medium/low"
}}"""

            response = await self.client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a viral content strategist. Return ONLY valid"
                            " JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                response_format={"type": "json_object"},
            )

            analysis = json.loads(response.choices[0].message.content)

            clip_duration = analysis["end_seconds"] - analysis["start_seconds"]
            if (
                clip_duration < Config.MIN_CLIP_DURATION
                or clip_duration > Config.MAX_CLIP_DURATION
            ):
                analysis["end_seconds"] = min(
                    analysis["start_seconds"] + 45, video_duration
                )

            return analysis

        except Exception as e:
            logger.error(f"❌ Hook analysis failed: {e}")
            raise

    async def generate_smart_title(self, hook_analysis: Dict) -> str:
        try:
            prompt = f"""Generate 1 viral TikTok-style title for this clip:
Transcript: {hook_analysis['clip_transcript'][:200]}
Hook Type: {hook_analysis['hook_type']}

Return as JSON: {{"title": "your viral title"}}"""

            response = await self.client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                response_format={"type": "json_object"},
            )

            data = json.loads(response.choices[0].message.content)
            return data.get("title", "🔥 Must Watch! #viral #fyp")
        except:
            return "🔥 Must Watch! #viral #fyp"


# ==================== VIDEO CLIPPER ====================
class VideoClipper:

    @staticmethod
    async def clip_video(
        input_path: Path, output_path: Path, start_time: float, end_time: float
    ) -> Path:
        try:
            logger.info(f"✂️ Clipping video: {start_time:.2f}s to {end_time:.2f}s")
            duration = max(1.0, end_time - start_time)

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
                "-vf", (
                    "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
                ),
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
                status_msg, "✂️ Memotong klip video 9:16...", 85
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
🎯 *Kekuatan Hook:* {hook_analysis['hook_strength']}/10
📊 *Tipe Hook:* {hook_analysis['hook_type'].title()}

💡 *Analisis Hook:*
{hook_analysis['hook_analysis']}

📝 *Transkrip Klip:*
_{hook_analysis['clip_transcript'][:300]}_

🛒 *CTA TikTok Affiliate:*
"{hook_analysis['cta_tiktok_affiliate']}"

✨ _Klip video vertical (9:16) siap diupload ke TikTok/Shorts!_
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
