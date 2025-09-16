from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from configs.config import ConfigValidationError, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-trading-bot",
        description="Monitor a Telegram channel and safely process trading signals.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration without creating files or using the network",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config()
    except (ConfigValidationError, ValueError) as exc:
        _parser().error(str(exc))

    if args.check_config:
        print(
            "Configuration is valid: "
            f"trading_mode={cfg.trading_mode}, "
            f"auto_execution={cfg.enable_auto_execution}, "
            f"signal_extraction={cfg.signal_extraction_enabled}"
        )
        return 0

    # Import the runtime only after configuration succeeds. This keeps the
    # validation command free of Telegram/exchange initialization and network I/O.
    from internal.services.runner import run_forever

    try:
        asyncio.run(run_forever(cfg))
    except KeyboardInterrupt:
        print("\n[!] Stopped by user.")
    return 0
