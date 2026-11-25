"""User-facing Bahasa Indonesia texts and error messages."""

from __future__ import annotations

from downloaders import MAX_FILE_SIZE_MB, ErrorCode

# --- User-facing texts (Bahasa Indonesia) -------------------------------------
WELCOME_TEXT = (
    "👋 <b>Halo!</b> Kirim link video, nanti aku unduhkan dan kirim balik ke sini.\n\n"
    "<b>Bisa dari banyak situs</b>, misalnya TikTok, Instagram, X/Twitter, YouTube, "
    "Facebook, Reddit, Vimeo, Dailymotion, Twitch, Bilibili, sampai link langsung "
    "ke file video (.mp4). Totalnya lebih dari 1.000 situs.\n\n"
    f"📦 Batas ukuran video: <b>{MAX_FILE_SIZE_MB} MB</b> (batas upload bot Telegram).\n"
    "🔒 Video berbayar/ber-DRM (Netflix, Disney+, dll.) tidak bisa diunduh.\n"
    "💡 Link boleh diselipkan di dalam kalimat.\n"
    "✅ Sebelum mengunduh, aku tunjukkan dulu videonya supaya kamu bisa memastikan "
    "itu video yang benar (bukan iklan).\n"
    "🎵 Mau suaranya saja? Tekan tombol <b>Audio saja</b> di pratinjau.\n"
    "🩺 Perintah: /ping (cek aktif) · /health (status bot)."
)
PING_TEXT = "🏓 Pong! Bot aktif. Uptime: {uptime}"
INVALID_LINK_TEXT = (
    "🤔 Aku tidak menemukan link yang bisa diproses.\n"
    "Kirim link video yang diawali http:// atau https:// ya."
)
ACTIVE_JOB_TEXT = "⏳ Video kamu sebelumnya masih diproses. Tunggu sampai selesai dulu ya."
COOLDOWN_TEXT = "🕐 Pelan-pelan ya, tunggu {seconds} detik lagi sebelum kirim link baru."
STATUS_TEXT = "⏳ Mengunduh video dari {platform}..."
STATUS_AUDIO_TEXT = "⏳ Mengunduh audio dari {platform}..."
CHECKING_TEXT = "🔎 Memeriksa link dari {site}..."
BROWSER_TEXT = (
    "🌐 Videonya belum ketemu dengan cara biasa. Sekarang aku buka halamannya pakai "
    "browser, tunggu sebentar ya (bisa sampai 1 menit)..."
)
BROWSER_FORCED_TEXT = "🌐 Oke, aku buka halamannya pakai browser. Tunggu sebentar ya (bisa sampai 1 menit)..."
CONFIRM_QUESTION = "Betul ini video yang mau kamu unduh?"
CONFIRM_AUDIO_QUESTION = "Betul ini audio yang mau kamu unduh?"
CHOOSE_QUESTION = "Pilih video yang mau diunduh:"
ONLY_ADS_TEXT = (
    "⚠️ Aku cuma menemukan video yang kelihatannya iklan, jadi tidak aku unduh. "
    "Nyalakan konfirmasi (CONFIRM_BEFORE_DOWNLOAD) kalau mau memilih sendiri."
)
EXPIRED_TEXT = "⌛ Pilihan ini sudah kedaluwarsa. Kirim ulang link-nya ya."
NOT_YOURS_TEXT = "🙅 Tombol ini hanya untuk orang yang mengirim link-nya."
CANCELLED_NOTE = "❌ Dibatalkan."
CHOSEN_NOTE = "⬇️ Dipilih: {label}. Sedang diunduh..."
AUDIO_BUTTON_TEXT = "🎵 Audio saja"
BROWSER_NOTE = "🔍 Mencari ulang pakai browser..."
SUCCESS_CAPTION = "✅ Video {platform} berhasil diunduh ({size:.2f} MB)"
SUCCESS_AUDIO_CAPTION = "✅ Audio {platform} berhasil diunduh ({size:.2f} MB)"
GENERIC_APOLOGY = "⚠️ Maaf, terjadi kesalahan. Silakan coba lagi nanti."
ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.TOO_LARGE: (
        f"❌ Video terlalu besar (lebih dari {MAX_FILE_SIZE_MB} MB). "
        f"Bot Telegram hanya bisa mengirim file maksimal {MAX_FILE_SIZE_MB} MB."
    ),
    ErrorCode.PRIVATE_OR_LOGIN: "🔒 Konten ini privat atau butuh login, jadi tidak bisa diunduh.",
    ErrorCode.NOT_FOUND: "🔍 Video tidak ditemukan di link itu.",
    ErrorCode.UNSUPPORTED: (
        "🔍 Link ini tidak didukung atau tidak berisi video yang bisa diunduh "
        "(misalnya video ber-DRM seperti Netflix)."
    ),
    ErrorCode.RATE_LIMITED: "🚦 Platform sedang membatasi permintaan. Coba lagi beberapa menit lagi ya.",
    ErrorCode.NETWORK: "📡 Ada masalah koneksi atau proses terlalu lama. Silakan coba lagi.",
    ErrorCode.TIMEOUT: "📡 Ada masalah koneksi atau proses terlalu lama. Silakan coba lagi.",
    ErrorCode.LIVE: "🔴 Ini siaran langsung (live), jadi belum bisa diunduh. Coba lagi setelah siarannya selesai.",
    ErrorCode.DISK_FULL: (
        "💾 Penyimpanan di PC bot hampir penuh, jadi unduhan dibatalkan. "
        "Kosongkan sedikit ruang disk lalu coba lagi."
    ),
    ErrorCode.UNKNOWN: "⚠️ Maaf, video gagal diproses. Silakan coba lagi nanti.",
}
