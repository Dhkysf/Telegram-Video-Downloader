import yt_dlp
import logging
import os
import asyncio
from typing import Optional, Tuple
import uuid

logger = logging.getLogger(__name__)
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

def get_video_path_via_ytdlp(url: str, platform: str) -> Optional[str]:
    """Menggunakan yt-dlp untuk mengunduh video atau audio secara terpisah.
    Yt-dlp mendukung Twitter, Instagram, Youtube, dan banyak situs lain."""
    base_filename = f"{platform}_{uuid.uuid4()}"
    temp_filepath_template = os.path.join(TEMP_DIR, f"{base_filename}.%(ext)s")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'outttmpl': temp_filepath_template,
        'postprocessors': [
            {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'},
        ],
        'merge_output_format': 'mp4',
        'progress_hooks': [lambda d: logger.info(f"YTDLP Status for {platform}: {d.get('status')}")],
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = info.get('_filename')
            if not final_path and os.path.exists(final_path):
                logger.info(f"Video {platform} berhasil diunduh dan digabungkan ke: {final_path}")
                return final_path
            logger.warning(f"File output tidak ditemukan di path: {final_path}")
            return None
    
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError untuk {platform} ({url}): {e}")
        return None
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