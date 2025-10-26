import requests
import re
from urllib.parse import urlparse, parse_qs

SSSTIK_URL = 'https://ssstik.io/abc?url=dl'

def get_no_watermark_url(tiktok_url: str) -> str | None:
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
        print(f"DEBUG ERROR: Gagal mendapatkan URL Tiktok asli: {e}")
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

        with open("ssstik_response.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print("DEBUG: Respon HTML dari SSSTik.io telah disimpan ke ssstik_response.html")

        video_download_url = None

        hd_match = re.search(
            r'<a\s+href="([^"]+)"[^>]*>Tanpa tanda air HD<\/a>',
            html_content
        )

        if hd_match:
            video_download_url = hd_match.group(1)
            print("DEBUG: Tautan HD ditemukan.")
        else:
            std_match = re.search(
                r'<a\s+href="([^"]+)"[^>]*>Tanpa tanda air<\/a>',
                html_content
            )
            if std_match:
                video_download_url = std_match.group(1)
                print("DEBUG: Tautan standar ditemukan.")
            else:
                print("DEBUG: RegEx gagal menemukan tautan unduhan HD atau standar.")
        if video_download_url:
            if not video_download_url.startswith('http'):
                return f"https:{video_download_url}"
            return video_download_url
        
    except requests.exceptions.RequestException as e:
        print(f"Error saat berinteraksi dengan SSSTik.io: {e}")
        return None
        