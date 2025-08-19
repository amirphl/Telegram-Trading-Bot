import asyncio

from configs.config import load_config
from internal.services.runner import run_forever


def main():
    cfg = load_config()
    asyncio.run(run_forever(cfg))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Stopped by user.") 