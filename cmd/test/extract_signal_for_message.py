import argparse
import json
import sys
from pathlib import Path
import logging

from configs.config import load_config
from internal.db.sqlite import connect_db
from internal.services.openai_client import OpenAIExtractor


logger = logging.getLogger(__name__)


def _fetch_message_by_id(conn, message_id: int):
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT chat_id, message_id, text
            FROM messages
            WHERE message_id = ?
            ORDER BY date_utc DESC
            LIMIT 1
            """,
            (message_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "chat_id": int(row[0]),
            "message_id": int(row[1]),
            "text": row[2] or "",
        }
    finally:
        cur.close()


def _fetch_media_paths(conn, chat_id: int, message_id: int) -> list[Path]:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT local_path FROM media_files
            WHERE chat_id = ? AND message_id = ?
            ORDER BY file_name ASC
            """,
            (chat_id, message_id),
        )
        paths: list[Path] = []
        for (p,) in cur.fetchall():
            pp = Path(p)
            if pp.exists():
                paths.append(pp)
        return paths
    finally:
        cur.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract OpenAI signal for a message id from the database"
    )
    parser.add_argument("message_id", type=int, help="Telegram message id")
    args = parser.parse_args()

    cfg = load_config()
    logging.getLogger().setLevel(
        getattr(logging, (cfg.log_level or "INFO").upper(), logging.INFO)
    )
    conn = connect_db(cfg.db_path)

    rec = _fetch_message_by_id(conn, args.message_id)
    if not rec:
        print(f"Message id {args.message_id} not found in DB", file=sys.stderr)
        return 2

    media_paths = _fetch_media_paths(conn, rec["chat_id"], rec["message_id"])

    extractor = OpenAIExtractor(cfg)
    result = extractor.extract_signal(rec["text"], media_paths)

    if result is None:
        print("No result (OpenAI key not set or extraction failed)")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

