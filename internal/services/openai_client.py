import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request

from configs.config import Config


@dataclass
class OpenAIMessage:
    role: str
    content: List[Dict[str, Any]]  # supports text and image_url parts


class OpenAIExtractor:
    def __init__(self, cfg: Config) -> None:
        self.api_key = cfg.openai_api_key
        self.model = cfg.openai_model
        self.timeout = cfg.openai_timeout_secs
        self.base_url = (cfg.openai_base_url or "https://api.openai.com").rstrip("/")

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self.base_url}/v1/chat/completions",
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _image_to_data_url(path: Path) -> str:
        mime = "image/jpeg"
        if path.suffix.lower() in (".png", ".webp"):
            mime = "image/png" if path.suffix.lower() == ".png" else "image/webp"
        b = path.read_bytes()
        b64 = base64.b64encode(b).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def extract_signal(self, text: Optional[str], image_paths: List[Path]) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            return None
        content_parts: List[Dict[str, Any]] = []
        user_text = text or ""
        content_parts.append({"type": "text", "text": user_text})
        # limit to first image for cost; can extend to multiple images if needed
        if image_paths:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": self._image_to_data_url(image_paths[0])},
            })

        system_prompt = (
            "You analyze crypto trading signals from text and screenshots. "
            "Return ONLY a strict JSON object matching this schema: "
            "{\"token\": string|null, \"position_type\": \"long\"|\"short\"|null, \"entry_price\": number|null, "
            "\"leverage\": number|null, \"stop_losses\": number[] (ascending, may be empty), \"take_profits\": number[] (ascending, may be empty)}. "
            "If information is missing or not visible, use null for scalars and [] for arrays. Do not include any extra fields or text."
        )

        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0,
        }
        try:
            data = self._request(payload)
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:
            return None 