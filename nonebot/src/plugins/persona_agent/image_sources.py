from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx
from nonebot.log import logger
from PIL import Image, ImageOps


class SearchFailure(Exception):
    pass


class _SecretFilter(logging.Filter):
    """These providers require query-string keys; httpx INFO logs include the URL."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = {
            value
            for secret in secrets
            if secret
            for value in (secret, quote(secret, safe=""))
        }

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[redacted]")
        record.msg, record.args = message, ()
        return True


def _jpeg(payload: bytes) -> tuple[bytes, bool]:
    with Image.open(io.BytesIO(payload)) as original:
        if original.width * original.height > 20_000_000:
            raise ValueError("image_dimensions")
        animated = getattr(original, "is_animated", False)
        original.seek(0)
        frame = ImageOps.exif_transpose(original)
        frame.thumbnail((1200, 1200))
        rgba = frame.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        for quality in (85, 75, 65, 55):
            output = io.BytesIO()
            rgb.save(output, "JPEG", quality=quality)
            if output.tell() <= 500_000:
                return output.getvalue(), animated
            rgb.thumbnail((max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)))
    raise ValueError("image_size")


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    detail: str


_STATUS = {
    "missing_key": "未配置密钥",
    "quota": "额度不足或已到本地上限",
    "auth": "密钥被接口拒绝",
    "timeout": "检索超时",
    "network": "连接失败",
    "response": "接口返回异常",
    "http": "接口暂不可用",
    "daily_limit": "今天的图片检索次数已用完，明天再试。",
    "cooldown": "搜图间隔太短，等 10 秒再试。",
}


class ImageSourceSearch:
    """Fixed provider chain; only selected image bytes leave the bot, never QQ URLs."""

    def __init__(self, config, db):
        self.config, self.db = config.image_sources, db
        self.sauce_key = os.getenv(self.config.saucenao_api_key_env, "").strip()
        self.lens_key = os.getenv(self.config.serpapi_api_key_env, "").strip()
        self._filter = _SecretFilter((self.sauce_key, self.lens_key))
        logging.getLogger("httpx").addFilter(self._filter)
        self.client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
        )
        self._lock = asyncio.Lock()
        self._cache = OrderedDict()

    def status(self):
        return {
            "enabled": self.config.enabled,
            "saucenao": bool(self.sauce_key),
            "serpapi": bool(self.lens_key),
        }

    async def close(self):
        await self.client.aclose()
        logging.getLogger("httpx").removeFilter(self._filter)
        self._cache.clear()

    def _text(self, value, limit=80):
        text = " ".join(str(value or "").split())
        for secret in self._filter.secrets:
            text = text.replace(secret, "[redacted]")
        return text[:limit]

    def _url(self, value):
        if (
            not isinstance(value, str)
            or len(value) > 1800
            or any(c.isspace() for c in value)
        ):
            return ""
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return ""
            if any(
                key.lower() in {"api_key", "apikey", "token", "access_token"}
                for key, _ in parse_qsl(parsed.query)
            ):
                return ""
            if any(
                secret in unquote(value)
                for secret in (self.sauce_key, self.lens_key)
                if secret
            ):
                return ""
        except ValueError:
            return ""
        return value

    async def _json(self, method, url, **kwargs):
        async with self.client.stream(method, url, **kwargs) as response:
            if response.status_code in {401, 403}:
                raise SearchFailure("auth")
            if response.status_code in {402, 429}:
                raise SearchFailure("quota")
            if response.status_code in {408, 504}:
                raise SearchFailure("timeout")
            if response.status_code != 200:
                raise SearchFailure("http")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise SearchFailure("response")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("error"):
            raise SearchFailure("response")
        return data

    async def _sauce(self, payload, valid):
        if not self.sauce_key:
            raise SearchFailure("missing_key")
        if not await valid():
            return None
        data = await self._json(
            "POST",
            "https://saucenao.com/search.php",
            params={
                "api_key": self.sauce_key,
                "output_type": 2,
                "db": 999,
                "numres": 5,
                "hide": 3,
            },
            files={"file": ("image.jpg", payload, "image/jpeg")},
        )
        header = data["header"]
        if int(header["status"]) != 0:
            exhausted = any(
                float(header.get(key, 1)) <= 0
                for key in ("short_remaining", "long_remaining")
            )
            raise SearchFailure("quota" if exhausted else "response")
        candidates = []
        for result in data.get("results", []):
            score = float(result["header"]["similarity"])
            if (
                not math.isfinite(score)
                or not 70 <= score <= 100
                or result["header"].get("hidden")
            ):
                continue
            item = result["data"]
            urls = [self._url(link) for link in item.get("ext_urls", [])]
            url = next((link for link in urls if link), "") or self._url(
                item.get("source")
            )
            if not url:
                continue
            title = self._text(
                item.get("title")
                or item.get("material")
                or item.get("source")
                or "作品候选"
            )
            author = self._text(item.get("member_name") or item.get("creator"), 40)
            detail = f"SauceNAO 相似度 {score:.1f}%" + (
                f"，署名：{author}" if author else ""
            )
            candidates.append(Candidate(title, url, detail))
        return candidates

    async def _trace(self, payload, valid):
        if not await valid():
            return None
        data = await self._json(
            "POST",
            "https://api.trace.moe/search",
            params={"anilistInfo": "", "cutBorders": ""},
            content=payload,
            headers={"Content-Type": "image/jpeg"},
        )
        candidates = []
        for result in data["result"]:
            score = float(result["similarity"])
            if not math.isfinite(score) or not 0.90 <= score <= 1:
                continue
            info = result["anilist"]
            if info.get("isAdult"):
                continue
            title = info["title"]
            name = self._text(
                title.get("native") or title.get("romaji") or title.get("english")
            )
            episode = self._text(result.get("episode"), 25)
            at = max(0, int(float(result["from"])))
            detail = f"trace.moe 相似度 {score:.1%}"
            if episode:
                detail += f"，第 {episode} 集约 {at // 60:02d}:{at % 60:02d}"
            candidates.append(
                Candidate(name, f"https://anilist.co/anime/{int(info['id'])}", detail)
            )
        return candidates

    async def _lens(self, payload, valid):
        if not self.lens_key:
            raise SearchFailure("missing_key")
        if not await valid():
            return None
        reason = await self.db.run(
            self.db.store.runtime.reserve_image_search,
            lens=True,
            lens_limit=self.config.serpapi_31day_limit,
        )
        if reason:
            raise SearchFailure(reason)
        upload = await self._json(
            "POST",
            "https://serpapi.com/image",
            data={"api_key": self.lens_key},
            files={"image": ("image.jpg", payload, "image/jpeg")},
        )
        image_id = upload["image_id"]
        if not isinstance(image_id, str) or not image_id or len(image_id) > 512:
            raise SearchFailure("response")
        if not await valid():
            return None
        data = await self._json(
            "GET",
            "https://serpapi.com/search",
            params={
                "engine": "google_lens",
                "type": "exact_matches",
                "image_id": image_id,
                "api_key": self.lens_key,
                "hl": "zh-cn",
                "safe": "active",
            },
        )
        if data.get("search_metadata", {}).get("status") not in {None, "Success"}:
            raise SearchFailure("response")
        return [
            Candidate(
                self._text(item.get("title") or item.get("source")),
                url,
                "Google Lens 同图网页",
            )
            for item in data.get("exact_matches", [])[:10]
            if (url := self._url(item.get("link")))
        ]

    async def lookup(self, image, kind, scope, request_id, valid):
        reply = await self._lookup(image, kind, scope, request_id, valid)
        if reply is None:
            return None
        remaining = await self.db.run(
            self.db.store.runtime.image_search_remaining, self.config.daily_limit
        )
        return f"{reply}\n今日剩余 {remaining} 次（北京时间 08:00 重置）。"

    async def _lookup(self, image, kind, scope, request_id, valid):
        if not self.config.enabled:
            return "图片来源检索尚未启用。"
        payload = image.payload
        digest = hashlib.sha256(payload).hexdigest()
        key = (*scope, kind, digest)
        clock = asyncio.get_running_loop().time
        if not await valid():
            return None
        for cached, (expires, _) in list(self._cache.items()):
            if expires <= clock():
                del self._cache[cached]
        if key in self._cache:
            logger.info(
                "Image source cache: request_id={} image={}", request_id, digest[:12]
            )
            return self._cache[key][1]
        if self._lock.locked():
            return "正在查另一张图，稍后再试。"
        try:
            async with self._lock, asyncio.timeout(self.config.timeout_seconds):
                reason = await self.db.run(
                    self.db.store.runtime.reserve_image_search,
                    daily_limit=self.config.daily_limit,
                )
                if reason:
                    return _STATUS[reason]
                try:
                    jpeg, animated = await asyncio.to_thread(_jpeg, payload)
                except (OSError, ValueError, Image.DecompressionBombError):
                    return "这张图无法用于来源检索，请重发一张清晰静态图（不超过 2000 万像素）。"
                providers = [("SauceNAO", self._sauce), ("Google Lens", self._lens)]
                if kind == "anime":
                    providers.insert(0, ("trace.moe", self._trace))
                failures, searched = [], []
                for name, search in providers:
                    if not await valid():
                        return None
                    try:
                        async with asyncio.timeout(25 if name == "Google Lens" else 10):
                            candidates = await search(jpeg, valid)
                        if candidates is None:
                            return None
                        reason = "matched" if candidates else "no_match"
                        searched.append(name)
                    except (TimeoutError, httpx.TimeoutException):
                        reason, candidates = "timeout", []
                    except httpx.RequestError:
                        reason, candidates = "network", []
                    except SearchFailure as exc:
                        reason, candidates = str(exc), []
                    except (ValueError, TypeError, KeyError, AttributeError):
                        reason, candidates = "response", []
                    logger.info(
                        "Image source result: request_id={} image={} provider={} status={} candidates={}",
                        request_id,
                        digest[:12],
                        name,
                        reason,
                        len(candidates),
                    )
                    if reason not in {"matched", "no_match"}:
                        failures.append(f"{name}：{_STATUS[reason]}")
                    if candidates:
                        unique = list(
                            {item.url: item for item in reversed(candidates)}.values()
                        )[::-1][:3]
                        lines = ["查到这些候选，尚未确认原作者或首发："]
                        lines.extend(
                            f"{index}. {item.title}\n{item.detail}\n{item.url}"
                            for index, item in enumerate(unique, 1)
                        )
                        if animated:
                            lines.append("动态图仅检索了第一帧。")
                        reply = "\n".join(lines)
                        self._cache[key] = (clock() + 600, reply)
                        while len(self._cache) > 64:
                            self._cache.popitem(last=False)
                        return reply
                reply = "没查到可靠的出处候选。"
                if searched:
                    reply += "已查：" + "、".join(searched) + "。"
                if failures:
                    reply += "\n" + "；".join(failures) + "。"
                return reply
        except TimeoutError:
            logger.warning(
                "Image source timeout: request_id={} seconds={}",
                request_id,
                self.config.timeout_seconds,
            )
            return f"图片来源检索超时（{self.config.timeout_seconds:g} 秒），稍后再试。"
