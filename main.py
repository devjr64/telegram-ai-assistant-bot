"""Telegram AI Chatbot — powered by OpenCode Go."""

import signal
import sys

from bot.bot import create_application


def _handle_shutdown(signum: int, frame: object) -> None:
    """Gracefully stop the bot on SIGTERM / SIGINT."""
    print("\nShutting down bot...")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    app = create_application()
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
