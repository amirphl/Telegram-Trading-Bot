import asyncio
import logging
import sys

from configs.config import load_config
from internal.services.runner import run_forever
from pkg.logger import setup_logging


if __name__ == "__main__":
    try:
        cfg = load_config()
        setup_logging(cfg)

        if cfg.enable_auto_execution:
            if cfg.exchange == 'xt' and not (cfg.xt_api_key and cfg.xt_secret):
                logging.getLogger(__name__).error("ENABLE_AUTO_EXECUTION is true but XT credentials are not provided. Set XT_API_KEY and XT_SECRET.")
                sys.exit(1)
            if cfg.exchange == 'bitunix' and not (cfg.bitunix_api_key and cfg.bitunix_secret):
                logging.getLogger(__name__).error("ENABLE_AUTO_EXECUTION is true but Bitunix credentials are not provided. Set BITUNIX_API_KEY and BITUNIX_SECRET.")
                sys.exit(1)

        asyncio.run(run_forever(cfg))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user.")
