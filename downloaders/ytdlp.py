import yt_dlp
import logging
import os
import asyncio
from typing import Optional, Tuple
import uuid

logger = logging.getLogger(__name__)
TEMP_DIR = 'temp_downloads'
MAX_FILE_SIZE_MB = 50
INSTAGRAM_COOKIE_FILE = "instagram_cookies.txt"
os.makedirs(TEMP_DIR, exist_ok=True)

def get_video_path_via_ytdlp(url: str, platform: str) -> Optional[str]:
    """Menggunakan yt-dlp untuk mengunduh video atau audio secara terpisah.
    Yt-dlp mendukung Twitter, Instagram, Youtube, dan banyak situs lain."""
    temp_filename_base = f"{platform}_{uuid.uuid4()}"
    temp_filename_template = os.path.join(TEMP_DIR, f"{temp_filename_base}.%(ext)s")
    final_path = os.path.join(TEMP_DIR, f"{temp_filename_base}.mp4")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'outttmpl': temp_filename_template,
        'postprocessors': [],
        'merge_output_format': 'mp4',
        'max_filesize': MAX_FILE_SIZE_MB * 1024 * 1024,
        'progress_hooks': [lambda d: logger.info(f"YTDLP Status for {platform}: {d.get('status')}")],
        'quiet': True,
        'no_warnings': True,
        'abort_on_error': True,
    }
    if platform == 'instagram':
        if os.path.exists(INSTAGRAM_COOKIE_FILE):
            ydl_opts['cookiefile'] = INSTAGRAM_COOKIE_FILE
            logger.info("Menggunakan cookie untuk autentikasi Instagram.")
        else:
            logger.warning(f"File cookie Instagram tidak ditemukan di {INSTAGRAM_COOKIE_FILE}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = ydl.prepare_filename(info)
            if not final_path.endswith('.mp4'):
                final_path += '.mp4'
            if os.path.exists(final_path):
                logger.info(f"Video {platform} berhasil diunduh dan digabungkan ke: {final_path}")
                return final_path
            logger.warning(f"File output tidak ditemukan di path: {final_path}")
            return None
    
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError untuk {platform} ({url}): {e}")
        return None
    except yt_dlp.utils.ExtractorError as e:
        logger.error(f"yt-dlp ExtractorError untuk {platform} ({url}): {e}")
    except Exception as e:
        logger.error(f"Error tidak terduga saat menggunakan yt-dlp untuk {platform}: {e}")
        return None
    
def get_twitter_file_path(url: str) -> Optional[str]:
    """Mengunduh video X/Twitter dan mengembalikan path lokal."""
    return get_video_path_via_ytdlp(url, 'twitter')

def get_instagram_file_path(url: str) -> Optional[str]:
    """Mengunduh video reels Instagram dan mengembalikan path lokal."""
    return get_video_path_via_ytdlp(url, 'instagram')

def get_youtube_file_path(url: str) -> Optional[str]:
    """Mengunduh video Youtube dan mengembalikan path lokal."""
    return get_video_path_via_ytdlp(url, 'youtube')