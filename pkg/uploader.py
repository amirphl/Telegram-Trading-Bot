import logging
import mimetypes
from pathlib import Path
from typing import Optional

import requests


logger = logging.getLogger(__name__)


class Uploader:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # Avoid using system/environment proxies
        self.session.trust_env = False
        self._no_proxies = {"http": None, "https": None}

    def _guess_mime(self, path: Path) -> str:
        mtype, _ = mimetypes.guess_type(path.name)
        return mtype or "application/octet-stream"

    def upload_image_get_url(self, path: Path) -> Optional[str]:
        try:
            mime = self._guess_mime(path)
            safe_name = f"upload{path.suffix.lower() or '.bin'}"

            with path.open("rb") as f:
                files = {"file": (safe_name, f, mime)}
                resp = self.session.post(
                    f"{self.base_url}/upload",
                    files=files,
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                    proxies=self._no_proxies,
                )

            if resp.status_code not in (200, 201):
                logger.warning(
                    "Upload failed %s: %s", resp.status_code, resp.text[:500]
                )
                return None

            try:
                data = resp.json()
            except Exception:
                logger.warning("Upload response not JSON: %s", resp.text[:500])
                return None

            rel = data.get("url")
            if isinstance(rel, str):
                if rel.startswith("/"):
                    return f"{self.base_url}{rel}"
                return rel

            uid = data.get("uuid")
            if isinstance(uid, str):
                return f"{self.base_url}/images/{uid}"

            return None
        except Exception as e:
            logger.warning("Image upload failed for %s: %s", path, e)
            return None

