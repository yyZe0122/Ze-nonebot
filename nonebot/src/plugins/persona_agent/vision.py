from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

import httpx
from nonebot.log import logger

from .config import ModelConfig

_DATA_URL = "data:"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_LABEL = re.compile(r"\[(?:图片|表情包|表情|合并转发|forward|node|image|mface|marketface)(?::[^\]]*)?\]")
_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

_MEDIA_REQUEST = re.compile(
    r"识图|识别|看图|读图|上图|下图|图片|照片|截图|图[里中上]|"
    r"[这那前上]一?张|第\s*[一二三四五六七八九十\d]+\s*张|最后一张|"
    r"这是什么|这是啥|那是什么|再看|看看|看一下|看下|检查来源|内容来源|什么字"
)


def requests_media(text: str) -> bool:
    return bool(_MEDIA_REQUEST.search(without_media_labels(text)) or image_search_kind(text))


def image_search_kind(text: str) -> str:
    """Only explicit source requests authorize sending an image to search providers."""
    text = without_media_labels(text)
    if re.search(r"(?:不要|不用|别|不必|无需).{0,8}(?:搜|查|找|溯源)", text):
        return ""
    if not re.search(
        r"[/／](?:搜图|查图|查出处)|搜图|以图搜图|查(?:一下|下|一查)?出处|"
        r"(?:查|搜|找|检索).{0,12}(?:图.{0,4}(?:来源|出处)|原图|画师)|"
        r"(?:图片|这张图|这图|上图).{0,6}(?:来源|出处|哪来的|哪里来的)|"
        r"谁画的|哪个画师|哪部(?:番|动漫|动画)|什么(?:番|动漫|动画)|图片溯源",
        text,
        re.I,
    ):
        return ""
    return "anime" if re.search(r"番|动画|动漫|第几集", text) else "general"


class ImageContent:
    """OpenAI multimodal image part. Kept separate so text-only messages stay dicts."""

    def __init__(self, url: str, label: str = "") -> None:
        self.url = url
        self.label = label

    @property
    def payload(self) -> bytes:
        return base64.b64decode(self.url.partition(",")[2])

    @property
    def mime(self) -> str:
        return self.url[5:].partition(";")[0]

    def as_part(self) -> dict[str, object]:
        return {"type": "image_url", "image_url": {"url": self.url}}


@dataclass(frozen=True)
class IncomingMedia:
    source: str
    key: str
    kind: str
    native: dict
    sticker_candidate: bool = False

    def as_record(self) -> dict:
        return {"source": self.source, "key": self.key, "kind": self.kind, "native": self.native,
                "sticker_candidate": self.sticker_candidate}

    @classmethod
    def from_record(cls, record: dict) -> IncomingMedia:
        return cls(record.get("source", ""), record.get("key", ""), record["kind"],
                   record.get("native", {}), record.get("sticker_candidate", False))


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    images: tuple[str, ...] = ()
    media: tuple[IncomingMedia, ...] = ()


def without_media_labels(text: str) -> str:
    return _MEDIA_LABEL.sub("", text).strip()


def is_media_only(text: str) -> bool:
    return bool(_MEDIA_LABEL.search(text)) and not re.sub(r"@\d+", "", without_media_labels(text)).strip()


def render_segments(segments: list[tuple[str, dict]]) -> IncomingMessage:
    """Turn OneBot segments into readable text plus image sources.

    Preserve media sources and native sticker fields for the persistent catalog.
    """
    pieces: list[str] = []
    images: list[str] = []
    media: list[IncomingMedia] = []
    for kind, data in segments:
        label, source = _segment(kind, data)
        if label:
            pieces.append(label)
        if kind in {"forward", "node"}:
            native = dict(data)
            key = hashlib.sha256(json.dumps(native, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            media.append(IncomingMedia("", key, kind, native))
        elif kind in {"image", "mface", "marketface"}:
            if source:
                images.append(source)
            native = {key: data[key] for key in ("emoji_package_id", "emoji_id", "key", "summary", "subType", "sub_type") if key in data}
            is_mface = kind in {"mface", "marketface"}
            candidate = (is_mface or str(data.get("subType", data.get("sub_type", ""))) == "1"
                         or bool(data.get("emoji_id") or data.get("emoji_package_id")))
            identity = json.dumps(native, sort_keys=True) if is_mface and native else str(data.get("file") or source)
            key = hashlib.sha256(identity.encode()).hexdigest()
            media.append(IncomingMedia(source or "", key, "mface" if is_mface else "image", native, candidate))
    text = "".join(pieces).strip()
    if not text and images:
        text = " ".join("[图片]" for _ in images)
    return IncomingMessage(text=text, images=tuple(images), media=tuple(media))


async def materialize_images(
    sources: tuple[str, ...],
    *,
    config: ModelConfig,
    client: httpx.AsyncClient | None = None,
    strict: bool = False,
) -> list[ImageContent]:
    limit = max(0, config.max_images_per_message)
    selected = sources[:limit]

    async def load(http: httpx.AsyncClient, source: str) -> ImageContent | None:
        try:
            return await _one(http, source, config)
        except ValueError:
            if strict:
                raise
            return None

    if client is not None:
        return [item for item in [await load(client, source) for source in selected] if item]
    async with httpx.AsyncClient(
        timeout=config.image_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "persona-agent"},
    ) as owned:
        return [item for item in [await load(owned, source) for source in selected] if item]


def _segment(kind: str, data: dict) -> tuple[str, str | None]:
    if kind == "text":
        return str(data.get("text") or ""), None
    if kind == "image":
        summary = str(data.get("summary") or data.get("file_name") or "").strip()
        label = f"[图片:{summary}]" if summary else "[图片]"
        return label, _image_source(data)
    if kind == "face":
        face_id = str(data.get("id") or "").strip()
        return f"[表情:{face_id}]" if face_id else "[表情]", None
    if kind in {"mface", "marketface"}:
        summary = str(data.get("summary") or data.get("emoji_name") or data.get("name") or "").strip()
        label = f"[表情包:{summary}]" if summary else "[表情包]"
        return label, _image_source(data)
    if kind == "at":
        target = str(data.get("qq") or data.get("user_id") or "").strip()
        return f"@{target}" if target else "", None
    if kind == "reply":
        return "", None
    if kind in {"forward", "node"}:
        return "[合并转发]", None
    return f"[{kind}]", None


def _image_source(data: dict) -> str | None:
    for key in ("url", "file", "path"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://", "data:", "base64://", "file://")):
            return value
    return None


def _image_failure(exc: Exception) -> str:
    known = {
        "invalid_image_data",
        "invalid_base64_image",
        "unsupported_file_source",
        "image_too_large",
        "empty_or_oversized_image",
    }
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return "image_unavailable"
        if status == 429 or status >= 500:
            return f"image_download_transient:{status}"
        return f"image_unavailable:{status}"
    if isinstance(exc, (httpx.TimeoutException, httpx.RequestError, TimeoutError)):
        return "image_download_transient"
    text = str(exc)
    if text in known:
        return text
    return "image_unavailable"


async def _one(client: httpx.AsyncClient, source: str, _config: ModelConfig) -> ImageContent | None:
    try:
        if source.startswith(_DATA_URL):
            if not _data_url_ok(source):
                raise ValueError("invalid_image_data")
            return ImageContent(source)
        if source.startswith("base64://"):
            image = _from_base64(source.removeprefix("base64://"), "image/jpeg")
            if image is None:
                raise ValueError("invalid_base64_image")
            return image
        if source.startswith("file://"):
            raise ValueError("unsupported_file_source")
        async with client.stream("GET", source, follow_redirects=True, timeout=_config.image_timeout_seconds) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_IMAGE_BYTES:
                    raise ValueError("image_too_large")
                chunks.append(chunk)
            payload = b"".join(chunks)
        if not payload or len(payload) > _MAX_IMAGE_BYTES:
            raise ValueError("empty_or_oversized_image")
        mime = _mime(response.headers.get("content-type", ""), source)
        if not mime.startswith("image/"):
            return None
        encoded = base64.b64encode(payload).decode("ascii")
        return ImageContent(f"data:{mime};base64,{encoded}")
    except Exception as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        reason = _image_failure(exc)
        logger.warning("Image load failed: source_key={} reason={} http_status={}",
                       hashlib.sha256(source.encode()).hexdigest()[:12], reason, status)
        raise ValueError(reason) from exc


def _from_base64(raw: str, mime: str) -> ImageContent | None:
    try:
        payload = base64.b64decode(raw, validate=True)
    except Exception:
        return None
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        return None
    return ImageContent(f"data:{mime};base64,{raw}")


def _data_url_ok(source: str) -> bool:
    header, _, body = source.partition(",")
    if not header.startswith("data:image/") or ";base64" not in header or not body:
        return False
    try:
        payload = base64.b64decode(body, validate=False)
    except Exception:
        return False
    return 0 < len(payload) <= _MAX_IMAGE_BYTES


def _mime(header: str, source: str) -> str:
    declared = header.split(";", 1)[0].strip().lower()
    if declared.startswith("image/"):
        return declared
    suffix = source.rsplit(".", 1)[-1].lower().split("?", 1)[0]
    return _MIME.get(suffix, "image/jpeg")
