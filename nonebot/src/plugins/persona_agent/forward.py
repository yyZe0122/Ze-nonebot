from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from nonebot.adapters.onebot.v11 import Message
from nonebot.log import logger

from .vision import render_segments

ReadForward = Callable[[str, str], Awaitable[dict]]


class ForwardResolver:
    """Request-local OneBot forward expansion, with bounded network and node work."""

    def __init__(self, bot_id: str, read: ReadForward | None) -> None:
        self.bot_id = bot_id
        self.read = read
        self.cache: dict[str, list] = {}
        self.fetches = 0
        self.nodes = 0
        self.image_count = 0
        self.text_left = 12000
        self.deadline = asyncio.get_running_loop().time() + 20

    async def expand(
        self, media: list[dict], *, path: str = "", depth: int = 0,
        ancestors: frozenset[str] = frozenset(),
    ) -> tuple[list[tuple[str, str]], list[str]]:
        images: list[tuple[str, str]] = []
        lines: list[str] = []
        image_index = 0
        for position, item in enumerate(media[:128], 1):
            if item["kind"] in {"image", "mface"}:
                if self.image_count >= 256:
                    lines.append("【图片数量超过解析上限，剩余媒体未展开】")
                    break
                self.image_count += 1
                image_index += 1
                images.append((item["source"], f"{path}图片 {image_index}"))
                continue
            if item["kind"] not in {"forward", "node"}:
                continue
            label = f"{path}转发 {position}"
            if depth >= 3 or self.nodes >= 60 or self.text_left <= 0:
                lines.append(f"【{label}：达到展开上限，剩余内容未读取】")
                continue
            data = item["native"]
            forward_id = str(data.get("id") or "")
            branch = ancestors
            if item["kind"] == "node":
                nodes = [data]
            elif forward_id in ancestors:
                lines.append(f"【{label}：循环引用，未重复展开】")
                continue
            else:
                branch = ancestors | {forward_id}
                try:
                    nodes = await self._fetch(forward_id)
                except Exception as exc:
                    logger.warning("Forward read failed: error={} fetches={} nodes={}",
                                   type(exc).__name__, self.fetches, self.nodes)
                    lines.append(f"【{label}：读取失败或达到读取上限，内容不可见】")
                    continue
            if not nodes:
                lines.append(f"【{label}：接口返回空记录】")
            for node_index, node in enumerate(nodes, 1):
                if self.nodes >= 60 or self.text_left <= 0:
                    lines.append(f"【{label}：剩余节点超过展开上限，未读取】")
                    break
                self.nodes += 1
                node_path = f"{label}.{node_index}"
                try:
                    if node.get("type") == "node":
                        node = node["data"]
                    content = node.get("content", node.get("message", []))
                    message = Message(content)
                    incoming = render_segments([(part.type, dict(part.data)) for part in message])
                    sender = node.get("sender") or {}
                    speaker = str(sender.get("card") or sender.get("nickname") or node.get("name") or "未知")
                    speaker = speaker.replace("\n", " ")[:80]
                    sender_id = sender.get("user_id", node.get("uin", "未知"))
                    body = incoming.text[:self.text_left]
                    self.text_left -= len(body)
                    lines.append(f"【{node_path}；说话人：{speaker}；用户 {sender_id}】\n{body}")
                    if len(body) < len(incoming.text):
                        lines.append("【该节点文字已截断】")
                    nested_images, nested_lines = await self.expand(
                        [part.as_record() for part in incoming.media], path=f"{node_path} / ",
                        depth=depth + 1, ancestors=branch,
                    )
                    images.extend(nested_images)
                    lines.extend(nested_lines)
                except (TypeError, ValueError, KeyError, AttributeError):
                    lines.append(f"【{node_path}：节点格式无法解析】")
        if len(media) > 128:
            lines.append("【消息媒体段超过上限，剩余部分未读取】")
        return images, lines

    async def _fetch(self, forward_id: str) -> list:
        if forward_id in self.cache:
            return self.cache[forward_id]
        remaining = self.deadline - asyncio.get_running_loop().time()
        if self.read is None or not forward_id or self.fetches >= 8 or remaining <= 0:
            raise ValueError("forward_unavailable_or_limited")
        self.fetches += 1
        payload = await asyncio.wait_for(self.read(self.bot_id, forward_id), timeout=min(8, remaining))
        nodes = payload.get("messages", payload.get("message"))
        if not isinstance(nodes, list):
            raise ValueError("invalid_forward_response")
        self.cache[forward_id] = nodes
        return nodes
