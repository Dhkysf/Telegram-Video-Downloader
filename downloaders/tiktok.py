import requests
import re
import os
import uuid
import logging
from typing import Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)
SSSTIK_URL = 'https://ssstik.io/abc?url=dl'
TEMP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "temp_downloads"
)
os.makedirs(TEMP_DIR, exist_ok=True)

def get_tiktok_file_path(tiktok_url: str) -> Optional[str]:
    """Mengambil URL unduhan video tanpa watermark dari SSSTik.io."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': 'https://ssstik.io/en',
    }
    try:
        response = requests.get(tiktok_url, allow_redirects=True, timeout=10)
        response.raise_for_status()
        final_url = response.url
    except requests.exceptions.RequestException as e:
        logger.error(f"Gagal mendapatkan URL Tiktok asli: {e}")
        return None
    data = {
        'id': final_url,
        'locale': 'id',
        'tt': '0'
    }

    try:
        post_response = requests.post(
            SSSTIK_URL, 
            data=data, 
            headers=headers, 
            timeout=15
        )
        post_response.raise_for_status()
        html_content = post_response.text
        video_download_url = None
        hd_match = re.search(
            r'<a\s+href="([^"]+)"[^>]*>\s*Tanpa tanda air HD<\/a>',
            html_content
        )
        if hd_match:
            video_download_url = hd_match.group(1)
            logger.info("Tautan Tiktok HD ditemukan")
        else:
            std_match = re.search(
                r'<a\s+href="([^"]+)"[^>]*>\s*Tanpa tanda air<\/a>',
                html_content
            )
            if std_match:
                video_download_url = std_match.group(1)
                logger.info("Tautan Tiktok standar ditemukan.")
            else:
                logger.warning("RegEx gagal menemukan tautan unduhan HD atau standar.")
        if video_download_url:
            if not video_download_url.startswith('http'):
                video_download_url = f"https:{video_download_url}"
            logger.info(f"Memulai unduhan Tiktok...")
            video_response = requests.get(
                video_download_url, 
                headers=headers, 
                stream=True, 
                timeout=30
            )
            video_response.raise_for_status()
            local_file_path = os.path.join(TEMP_DIR, f"tiktok_{uuid.uuid4()}.mp4")
            with open(local_file_path, 'wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"Video Tiktok berhasil diunduh ke: {local_file_path}")
            return local_file_path
        return None
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error saat berinteraksi dengan Tiktok Downloader: {e}")
        return None