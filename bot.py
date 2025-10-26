import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from tiktok_downloader import get_no_watermark_url
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

async def start(update: Update, context):
    """Menanggapi perintah /start."""
    await update.message.reply_text(
        '👋 Halo! Kirimkan saya tautan URL video TikTok, dan saya akan mengunduhnya untuk Anda tanpa watermark dan kualitas terbaik.'
    )

async def download_tiktok(update: Update, context):
    """Memproses tautan TikTok yang dikirim oleh pengguna."""
    tiktok_link = update.message.text
    if not any(domain in tiktok_link for domain in ["tiktok.com", "vt.tiktok.com"]):
        await update.message.reply_text(
            '❌ Tautan tidak valid. Mohon kirimkan tautan URL yang benar dari TikTok.'
        )
        return
    status_message = await update.message.reply_text(
        '⏳ Sedang memproses video... Mohon tunggu sebentar.'
    )
    try:
        video_url = get_no_watermark_url(tiktok_link)
        if video_url:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=video_url, # Telegram dapat mengunduh dari URL ini
                caption="✅ Video Berhasil Diunduh (Tanpa Watermark & Kualitas Terbaik)"
            )
            await status_message.delete()        
        else:
            await status_message.edit_text(
                '⚠️ Gagal mendapatkan URL unduhan. Pastikan video tidak disetel ke pribadi (private).'
            )
    except Exception as e:
        logging.error(f"Error saat memproses unduhan: {e}")
        await status_message.edit_text(
            '❌ Terjadi kesalahan saat memproses permintaan Anda. Coba lagi nanti.'
        )

def main():
    """Menjalankan bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_tiktok))
    print("Bot sedang berjalan...")
    application.run_polling(poll_interval=3)

if __name__ == '__main__':
    main()

    
