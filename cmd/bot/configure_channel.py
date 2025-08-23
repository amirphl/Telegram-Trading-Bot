import argparse
import json
import os
from pathlib import Path


def load_existing(path: Path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_channels(path: Path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Append a channel configuration to channels file"
    )
    parser.add_argument("--title", required=True, help="Channel title (human-readable)")
    parser.add_argument(
        "--id", default="", help="Channel id/username (e.g., @channel or numeric id)"
    )
    parser.add_argument(
        "--policy",
        default="single_message",
        choices=["single_message", "windowed_messages"],
        help="Signal discovery policy",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=5,
        help="Window size for windowed_messages policy",
    )
    parser.add_argument(
        "--enabled", type=int, default=1, help="1 to enable, 0 to disable"
    )
    parser.add_argument(
        "--file",
        default=os.getenv("CHANNELS_FILE", "./configs/channels.json"),
        help="Channels JSON file path",
    )
    parser.add_argument("--prompt", default="", help="Channel prompt")

    args = parser.parse_args()

    path = Path(args.file)
    channels = load_existing(path)

    new_entry = {
        "channel_id": args.id,
        "channel_title": args.title,
        "policy": args.policy,
        "window_size": int(args.window_size),
        "enabled": bool(int(args.enabled)),
        "channel_prompt": args.prompt,
    }

    channels.append(new_entry)
    save_channels(path, channels)

    print(f"[+] Appended channel to {path}: {new_entry}")


if __name__ == "__main__":
    main()

