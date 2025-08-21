import asyncio

from configs.config import load_config
from internal.services.runner import run_forever


if __name__ == "__main__":
    try:
        asyncio.run(run_forever(load_config()))
    except KeyboardInterrupt:
        print("\n[!] Stopped by user.")
