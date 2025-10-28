import logging
import os
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from urllib.parse import urlparse
from typing import Optional
from downloaders.tiktok import get_tiktok_file_path
from downloaders.ytdlp import get_twitter_file_path, get_instagram_file_path, get_youtube_file_path
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def get_platform(url: str) -> str:
    """Mendeteksi platform berdasarkan URL."""
    try:
        netloc = urlparse(url).netloc
        if any(domain in netloc for domain in ["tiktok.com"]):
            return 'tiktok'
        elif any(domain in netloc for domain in ["twitter.com", "x.com", "t.co"]):
            return 'twitter'
        elif any(domain in netloc for domain in ["instagram.com"]):
            return 'instagram'
        elif any(domain in netloc for domain in ["youtu.be", "youtube.com"]):
            return 'youtube'
    except Exception:
        pass
    return 'unknown'

def cleanup_file(file_path: Optional[str]):
    """Menghapus file lokal setelah proses selesai."""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Cleanup: File lokal berhasil dihapus: {file_path}")
        except OSError as e:
            logger.error(f"Cleanup Error: Gagal menghapus file {file_path}: {e}")

async def start(update: Update, context):
    """Menanggapi perintah /start."""
    await update.message.reply_text(
        '👋 Halo! Kirimkan saya tautan URL video dari social media seperti **Tiktok, Instagram, Twitter, atau Youtube**, dan saya akan mengunduhnya untuk Anda tanpa watermark dan kualitas terbaik.'
    )

async def handle_download(update: Update, context):
    """Mendeteksi platform, mengunduh file, dan mengunggah ke Telegram."""
    url = update.message.text.strip()
    platform = get_platform(url)
    local_file_path = None
    MAX_FILE_SIZE_MB = 50

    if platform == 'unknown':
        await update.message.reply_text(
            '❌ Tautan tidak valid. Mohon kirimkan tautan URL yang benar dari platform yang didukung.'
        )
        return
    
    status_message = await update.message.reply_text(
        f'⏳ Sedang memproses video dari {platform.capitalize()}... Mohon tunggu.'
    )

    try:
        logger.info(f"Menerima tautan: {url} ({platform.capitalize()})")
        if platform == 'tiktok':
            local_file_path = get_tiktok_file_path(url)
        elif platform == 'twitter':
            local_file_path = get_twitter_file_path(url)
        elif platform == 'instagram':
            local_file_path = get_instagram_file_path(url)
        elif platform == 'youtube':
            local_file_path= get_youtube_file_path(url)
        
        if local_file_path and os.path.exists(local_file_path):
            file_size_bytes = os.path.getsize(local_file_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                await status_message.edit_text(
                    f'⚠️ Gagal: File terlalu besar ({file_size_mb:.2f} MB). Batas Telegram adalah {MAX_FILE_SIZE_MB} MB.'
                )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=open(local_file_path, 'rb'),
                    caption=f"✅ Video {platform.capitalize()} Berhasil Diunduh! (Ukuran: {file_size_mb:.2f} MB)"
                )
                await status_message.delete()
        else:
            await status_message.edit_text(
                f'⚠️ Gagal mendapatkan video dari {platform.capitalize()}. Pastikan tautan valid dan konten publik.'
            )
    
    except Exception as e:
        logger.error(f"Error utama saat memproses {platform}: {e}", exc_info=True)
        await status_message.edit_text(
            f'❌ Terjadi kesalahan tak terduga saat memproses permintaan Anda ({platform}). Silakan periksa log server.'
        )
    finally:
        cleanup_file(local_file_path)

def main():
    """Menjalankan bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_download))
    logger.info("Bot sedang berjalan dan siap menerima tautan multi-platform...")
    application.run_polling(poll_interval=3)

if __name__ == '__main__':
    FFMPEG_BIN_PATH = r"C:\Program Files\FFmpeg\ffmpeg-win64\bin"
    ffmpeg_exe_path = os.path.join(FFMPEG_BIN_PATH, 'ffmpeg.exe')
    if os.path.exists(ffmpeg_exe_path):
        os.environ['PATH'] += os.pathsep + FFMPEG_BIN_PATH
        logger.info("FFmpeg Path berhasil ditambahkan secara runtime.")
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            logger.info("FFmpeg berhasil terdeteksi di sistem.")
        else:
            logger.warning("Peringatan: FFmpeg mungkin tidak terdeteksi atau gagal dijalankan")
    except Exception:
        logger.warning("Peringatan: FFmpeg mungkin tidak terdeteksi. Penggabungan video/audio mungkin gagal. Pastikan FFmpeg terinstal dan ada di PATH.")
    main()

    
