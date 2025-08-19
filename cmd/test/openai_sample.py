import json
from pathlib import Path
from typing import List, Tuple

from configs.config import load_config
from internal.db.sqlite import connect_db
from internal.services.openai_client import OpenAIExtractor


def fetch_sample_messages(db_path: str, limit: int = 10) -> List[Tuple[int, int, str, str]]:
    conn = connect_db(db_path)
    cur = conn.cursor()
    # pick one media path per message; prefer newest by date_utc
    cur.execute(
        """
        SELECT m.chat_id, m.message_id, COALESCE(m.text, ''), MIN(mf.local_path) AS local_path
        FROM messages m
        JOIN media_files mf ON m.chat_id = mf.chat_id AND m.message_id = mf.message_id
        GROUP BY m.chat_id, m.message_id
        ORDER BY m.date_utc DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return [(int(r[0]), int(r[1]), str(r[2] or ''), str(r[3])) for r in rows]


def main():
    cfg = load_config()
    if not cfg.openai_api_key:
        print("[!] OPENAI_API_KEY is not set; cannot run test.")
        return

    messages = fetch_sample_messages(cfg.db_path, limit=10)
    if not messages:
        print("[*] No messages with media found in DB.")
        return

    extractor = OpenAIExtractor(cfg)

    for chat_id, message_id, text, media_path in messages:
        p = Path(media_path)
        if not p.exists():
            print(f"[!] Skipping missing media for message {message_id}: {media_path}")
            continue
        result = extractor.extract_signal(text, [p])
        print("\n=== Message", message_id, "===")
        print("Media:", str(p))
        print("Text:", (text or "").strip()[:200])
        print("OpenAI:")
        print(json.dumps(result or {}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main() 