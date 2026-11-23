#!/usr/bin/env python3
"""Telegram multi-platform video downloader bot (entry point).

Responsibilities: startup lifecycle and Application wiring only.
All bot logic lives in ``tgbot`` and all download logic in ``downloaders``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from telegram import BotCommand, Update
from telegram.error import InvalidToken
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from downloaders import clear_temp_dir, install_network_guard, prepare_temp_dir
from tgbot.handlers import error_handler, handle_choice, handle_text, health_command, ping_command, start_command
from tgbot.settings import (
    Settings,
    configure_ffmpeg,
    configure_logging,
    load_settings,
    load_token,
    redact_token_in_logs,
)
from tgbot.state import BotState

logger = logging.getLogger("video_bot")


def build_application(token: str, settings: Settings) -> Application:
    """Create the Application with concurrent updates and all handlers."""

    async def post_init(application: Application) -> None:
        application.bot_data["state"] = BotState(
            settings=settings,
            semaphore=asyncio.Semaphore(settings.max_concurrent_downloads),
            started_at=time.monotonic(),
        )
        try:
            await application.bot.set_my_commands([
                BotCommand("start", "Mulai & panduan singkat"),
                BotCommand("help", "Panduan singkat"),
                BotCommand("ping", "Cek bot aktif"),
                BotCommand("health", "Status & kesehatan bot"),
            ])
        except Exception as exc:
            logger.warning("Could not register bot commands: %s", exc)
        logger.info("Bot @%s is running (long polling)", application.bot.username)

    async def post_shutdown(_application: Application) -> None:
        removed = await asyncio.to_thread(clear_temp_dir)
        logger.info("Shutdown complete; removed %d leftover temp file(s)", removed)

    application = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler(["start", "help"], start_command))
    application.add_handler(CommandHandler(["ping"], ping_command))
    application.add_handler(CommandHandler(["health"], health_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^(dl|au|br|cx):"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """Run the startup lifecycle and start long polling."""
    # 1. Logging.
    configure_logging()
    # 2. Token.
    token = load_token()
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set. Export it as an environment variable "
            "or define TELEGRAM_BOT_TOKEN in config.py next to bot.py."
        )
        sys.exit(1)
    redact_token_in_logs(token)
    # 3. FFmpeg.
    configure_ffmpeg()
    # Downloads may only connect to public internet addresses (protects the LAN).
    install_network_guard()
    # 4. Temp directory.
    try:
        removed = prepare_temp_dir()
    except OSError as exc:
        logger.error("Cannot create temp directory: %s", exc)
        sys.exit(1)
    if removed:
        logger.info("Removed %d leftover temp file(s) from a previous run", removed)
    # 5. Application + long polling (SIGINT/SIGTERM trigger a graceful stop).
    application = build_application(token, load_settings())
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            stop_signals=(signal.SIGINT, signal.SIGTERM),
        )
    except InvalidToken:
        # PTB's own message contains the token, so it is deliberately not logged.
        logger.error("Telegram rejected TELEGRAM_BOT_TOKEN; check the token with @BotFather")
        sys.exit(1)


if __name__ == "__main__":
    main()
