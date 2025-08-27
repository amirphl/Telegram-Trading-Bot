import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request

from configs.config import Config
from pkg.uploader import Uploader


logger = logging.getLogger(__name__)


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
        self.uploader = Uploader(cfg.upload_base, timeout=self.timeout)

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

    def extract_signal(
        self,
        text: Optional[str],
        image_paths: List[Path],
        channel_prompt: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            logger.debug("OpenAI API key not set; skipping extraction")
            return None
        content_parts: List[Dict[str, Any]] = []
        user_text = text or ""
        content_parts.append({"type": "text", "text": user_text})
        if image_paths:
            url = self.uploader.upload_image_get_url(image_paths[0])
            if url:
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": url},
                    }
                )

        system_prompt = (
            "You analyze crypto trading signals from both text and screenshots. "
            "Messages may contain images or text that include information about entries, stop losses, take profits, leverage, and token symbols. "
            "Images might contain screenshots from TradingView; lines in green areas indicate take profits, and lines in red areas indicate stop losses. "
            "Your task is to extract all this information and construct a trading signal. "
            "If leverage is missing, assume the default leverage is 2. "
            "Return ONLY a strict JSON object matching this schema: "
            '{"token": string|null, "position_type": "long"|"short"|null, '
            '"entry_price": number|null, "leverage": number|null, '
            '"stop_losses": number[] (ascending, may be empty), '
            '"take_profits": number[] (ascending, may be empty)}. '
            "If any information is missing or not visible, use null for scalar fields and [] for array fields. "
            "Do not include any extra fields, comments, or text outside the JSON object."
        )
        if channel_prompt:
            system_prompt = f"{channel_prompt}\n\n" + system_prompt

        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ],
            # "temperature": 0,
        }
        logger.info("OpenAI extraction payload: %s", json.dumps(payload, indent=2))
        try:
            data = self._request(payload)
            content = data["choices"][0]["message"]["content"]
            response = json.loads(content)
            logger.info("OpenAI extraction response: %s", json.dumps(response, indent=2))
            return response
        except Exception as e:
            logger.warning("OpenAI extraction failed: %s", e)
            return None
