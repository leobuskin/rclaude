"""Main bot application setup."""

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from glaude.config import TG_BOT_TOKEN, ALLOWED_USERS, logger
from glaude.handlers import (
    handle_start,
    handle_new,
    handle_stop,
    handle_status,
    handle_cwd,
    handle_callback_query,
    handle_message,
    error_handler,
)


def create_app() -> Application:
    """Create and configure the Telegram application."""
    if not TG_BOT_TOKEN:
        raise ValueError('TG_BOT_TOKEN not set in .env')

    if not ALLOWED_USERS:
        raise ValueError('ALLOWED_USERS not set in .env - add comma-separated Telegram user IDs')

    app = Application.builder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler('start', handle_start))
    app.add_handler(CommandHandler('new', handle_new))
    app.add_handler(CommandHandler('stop', handle_stop))
    app.add_handler(CommandHandler('status', handle_status))
    app.add_handler(CommandHandler('cwd', handle_cwd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    return app


def run() -> None:
    """Run the bot."""
    app = create_app()
    logger.info('Starting bot...')
    app.run_polling(allowed_updates=Update.ALL_TYPES)
