"""Compatibility module; prefer the installed ``telegram-trading-bot`` command."""

from telegram_trading_bot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
