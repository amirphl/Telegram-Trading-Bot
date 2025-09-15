from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from configs.config import Config
from pkg.media import inspect_image_file
from pkg.uploader import Uploader


logger = logging.getLogger(__name__)

SIGNAL_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_signal": {"type": "boolean"},
        "token": {"type": ["string", "null"]},
        "position_type": {
            "type": ["string", "null"],
            "enum": ["long", "short", None],
        },
        "entry_price": {"type": ["number", "null"]},
        "leverage": {"type": ["number", "null"]},
        "stop_losses": {"type": "array", "items": {"type": "number"}},
        "take_profits": {"type": "array", "items": {"type": "number"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "is_signal",
        "token",
        "position_type",
        "entry_price",
        "leverage",
        "stop_losses",
        "take_profits",
        "confidence",
    ],
}


class OpenAIExtractionError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


class OpenAIExtractor:
    def __init__(self, cfg: Config) -> None:
        self.api_key = cfg.openai_api_key
        self.model = cfg.openai_model
        self.timeout = cfg.openai_timeout_secs
        self.base_url = (cfg.openai_base_url or "https://api.openai.com").rstrip("/")
        self.max_image_bytes = cfg.media_max_bytes
        self.max_total_image_bytes = cfg.media_max_total_bytes
        self.max_image_pixels = cfg.media_max_pixels
        self.max_images = cfg.media_max_images
        upload_base = getattr(cfg, "upload_base", None)
        self.uploader = Uploader(upload_base, timeout=self.timeout) if upload_base else None

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise OpenAIExtractionError(
                "configuration_error",
                "OpenAI extraction is enabled without credentials",
                retryable=False,
            )
        req = urllib.request.Request(
            url=f"{self.base_url}/v1/chat/completions",
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OpenAIExtractionError(
                        "invalid_response_json",
                        "OpenAI returned invalid JSON",
                        retryable=True,
                    ) from exc
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            if status in {401, 403}:
                code, message, retryable = (
                    "authentication_error",
                    "OpenAI authentication failed",
                    False,
                )
            elif status == 429:
                code, message, retryable = (
                    "rate_limit_or_quota",
                    "OpenAI rate limit or quota was reached",
                    True,
                )
            elif status >= 500:
                code, message, retryable = (
                    "server_error",
                    f"OpenAI server error ({status})",
                    True,
                )
            else:
                code, message, retryable = (
                    "request_rejected",
                    f"OpenAI rejected the request ({status})",
                    False,
                )
            raise OpenAIExtractionError(code, message, retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenAIExtractionError(
                "network_or_timeout",
                "OpenAI request timed out or could not connect",
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise OpenAIExtractionError(
                "invalid_response_shape",
                "OpenAI returned an unexpected response shape",
                retryable=True,
            )
        return data

    def _image_to_url(self, path: Path) -> str:
        info = inspect_image_file(
            path,
            max_bytes=self.max_image_bytes,
            max_pixels=self.max_image_pixels,
        )
        if self.uploader is not None:
            uploaded_url = self.uploader.upload_image_get_url(path)
            if uploaded_url:
                return uploaded_url
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{info.mime_type};base64,{encoded}"

    def extract_signal(
        self,
        text: str | None,
        image_paths: list[Path],
        channel_prompt: str | None = None,
    ) -> dict[str, Any]:
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": text or ""}
        ]
        if len(image_paths) > self.max_images:
            raise OpenAIExtractionError(
                "media_limit", "Signal contains too many images", retryable=False
            )
        try:
            total_bytes = sum(path.stat().st_size for path in image_paths)
        except OSError as exc:
            raise OpenAIExtractionError(
                "media_unavailable", "Signal image is unavailable", retryable=False
            ) from exc
        if total_bytes > self.max_total_image_bytes:
            raise OpenAIExtractionError(
                "media_limit",
                "Signal images exceed the total size limit",
                retryable=False,
            )
        try:
            for image_path in image_paths:
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self._image_to_url(image_path)},
                    }
                )
        except (OSError, ValueError) as exc:
            raise OpenAIExtractionError(
                "invalid_media",
                "Signal image failed local validation",
                retryable=False,
            ) from exc

        system_prompt = (
            "You analyze crypto trading signals from both text and screenshots. "
            "Messages may contain entries, stop losses, take profits, leverage, "
            "and token symbols. Screenshots may be from TradingView; green areas "
            "can indicate take profits and red areas can indicate stop losses. "
            "Set is_signal=false when the input is not an actionable trade signal. "
            "Never infer missing prices. If leverage is missing, use 2. Token is "
            "the uppercase base asset without a quote. Use null for missing scalar "
            "fields and [] for missing arrays."
        )
        if channel_prompt:
            system_prompt = f"{channel_prompt}\n\n{system_prompt}"

        payload = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trade_signal",
                    "strict": True,
                    "schema": SIGNAL_JSON_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ],
        }
        logger.info(
            "Requesting OpenAI signal extraction with model=%s and images=%s",
            self.model,
            len(image_paths),
        )
        data = self._request(payload)
        try:
            message = data["choices"][0]["message"]
            refusal = message.get("refusal")
            if refusal:
                raise OpenAIExtractionError(
                    "model_refusal",
                    "OpenAI declined to process this signal",
                    retryable=False,
                )
            result = json.loads(message["content"])
        except OpenAIExtractionError:
            raise
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenAIExtractionError(
                "invalid_response_shape",
                "OpenAI returned an unexpected response shape",
                retryable=True,
            ) from exc
        if not isinstance(result, dict):
            raise OpenAIExtractionError(
                "invalid_model_output",
                "OpenAI output was not an object",
                retryable=False,
            )
        logger.info("OpenAI signal extraction completed")
        return result
