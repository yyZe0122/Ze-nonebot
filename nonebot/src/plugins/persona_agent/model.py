from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from .config import ModelConfig
from .vision import ImageContent

Message = dict[str, object]


@dataclass(frozen=True)
class Completion:
    text: str
    total_tokens: int
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated: bool = False


class ModelClient:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.api_key = os.getenv(config.api_key_env, "")
        self._client = httpx.AsyncClient(timeout=config.timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self, messages: list[Message], max_tokens: int, *, timeout: float | None = None
    ) -> Completion:
        api_key = self.api_key
        if not self.config.base_url or not self.config.model or not api_key:
            raise RuntimeError("model is not configured")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await self._client.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.config.timeout_seconds if timeout is None else timeout,
        )
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        text = _choice_text(choice["message"]["content"]).strip()
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        total = int(usage.get("total_tokens") or _estimate(messages, text))
        return Completion(
            text=text,
            total_tokens=total,
            model=data.get("model"),
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            estimated=not bool(usage.get("total_tokens")),
        )


def _choice_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return str(content)


def _estimate(messages: list[Message], text: str) -> int:
    content = "".join(_content_text(item.get("content")) for item in messages) + text
    return max(1, len(content) // 2)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, ImageContent):
                parts.append("[图片]")
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or "[图片]"))
        return "".join(parts)
    return ""
