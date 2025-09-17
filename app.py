"""Compatibility wrapper; prefer the installed ``telegram-trading-bot`` command."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from configs.config import load_config
from pkg.logger import setup_logging
from telegram_trading_bot.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shared CLI, adding file logging for normal bot execution."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check-config" not in args:
        try:
            setup_logging(load_config())
        except ValueError:
            # Let the shared CLI render configuration errors without a traceback.
            pass
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
