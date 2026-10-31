from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from nonebot.log import logger

from .config import AgentConfig, Scope
from .vision import IncomingMedia, requests_media


@dataclass
class ChatEvent:
    scope: Scope
    bot_id: str
    user_id: int
    group_id: int | None
    message_id: int | None
    text: str
    to_me: bool
    now: datetime
    sender_name: str = ""
    images: tuple[str, ...] = ()
    reply_to_id: int | None = None
    reply_sender: str = ""
    reply_text: str = ""
    reply_media: tuple[IncomingMedia, ...] = ()
    to_other: bool = False
    proactive: bool = False
    raw_id: int | None = None
    media: tuple[IncomingMedia, ...] = ()
    request_id: str = field(default_factory=lambda: uuid4().hex)
    direct: bool = False
    related_raw_ids: tuple[int, ...] = ()
    media_followup: bool = False
    media_parent_id: int | None = None
    image_search: str = ""

    @property
    def route(self) -> tuple[str, str, int]:
        target = self.group_id if self.scope == "group" else self.user_id
        assert target is not None
        return self.bot_id, self.scope, target

    @property
    def command(self) -> str:
        parts = self.text.strip().split(maxsplit=1)
        first = parts[0].lower() if parts else ""
        if first in {"/new", "／new"}:
            return first if len(parts) == 1 else ""
        return (
            first
            if first
            in {
                "/model",
                "/模型",
                "/stickers",
                "/表情",
                "/memory",
                "/记忆",
                "/skills",
                "/技能",
            }
            else ""
        )


SendMessage = Callable[[ChatEvent, str, int | None, dict | None], Awaitable[int]]
ProcessBatch = Callable[[list[ChatEvent], Callable[[], bool]], Awaitable[bool]]


@dataclass
class _Conversation:
    pending: deque[ChatEvent] = field(default_factory=deque)
    revision: int = 0
    task: asyncio.Task | None = None


class ConversationQueue:
    """One worker per conversation; commands remain ordered between message batches."""

    def __init__(self, config: AgentConfig, process: ProcessBatch) -> None:
        self.config = config
        self.process = process
        self._conversations: dict[tuple[str, str, int], _Conversation] = {}
        self._accepting = True

    def busy(self, event: ChatEvent) -> bool:
        return event.route in self._conversations

    @property
    def has_work(self) -> bool:
        return bool(self._conversations)

    def submit(self, event: ChatEvent) -> bool:
        if not self._accepting:
            return False
        settings = self.config.scheduler
        total = sum(len(item.pending) + 1 for item in self._conversations.values())
        existing = self._conversations.get(event.route)
        if total >= settings.max_pending_total or (
            existing is not None
            and len(existing.pending) >= settings.max_pending_per_conversation
        ):
            ordinary = (
                next(
                    (
                        item
                        for item in existing.pending
                        if item.scope == "group"
                        and not item.direct
                        and not item.command
                    ),
                    None,
                )
                if existing
                else None
            )
            if ordinary is None:
                return False
            existing.pending.remove(ordinary)
            logger.info(
                "Queue candidate replaced: request_id={} source_message_id={}",
                ordinary.request_id,
                ordinary.message_id,
            )
        state = self._conversations.setdefault(event.route, _Conversation())
        state.pending.append(event)
        state.revision += 1
        if state.task is None:
            state.task = asyncio.create_task(self._run(event.route, state))
        logger.info(
            "Queue accepted: request_id={} scope={} pending={}",
            event.request_id,
            event.scope,
            len(state.pending),
        )
        return True

    async def _run(self, route: tuple[str, str, int], state: _Conversation) -> None:
        try:
            while state.pending:
                first = state.pending[0]
                if not first.command and not first.proactive:
                    delay = (
                        self.config.budget.private_interval_seconds
                        if first.scope == "private"
                        else self.config.group_behavior.message_batch_seconds
                    )
                    # A short look/identify request or bare call is often followed by a picture.
                    if (
                        first.direct
                        and not first.media
                        and (requests_media(first.text) or not first.text.strip())
                    ):
                        delay = self.config.budget.max_batch_wait_seconds
                    deadline = (
                        asyncio.get_running_loop().time()
                        + self.config.budget.max_batch_wait_seconds
                    )
                    while True:
                        revision = state.revision
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        await asyncio.sleep(min(delay, remaining))
                        if revision == state.revision or any(
                            item.command for item in state.pending
                        ):
                            break
                batch = [state.pending.popleft()]
                if not first.command and not first.proactive:
                    while (
                        state.pending
                        and not state.pending[0].command
                        and not state.pending[0].proactive
                    ):
                        batch.append(state.pending.popleft())

                def current(first=first) -> bool:
                    if any(
                        item.command in {"/memory", "/记忆"}
                        and any(
                            word in item.text
                            for word in ("忘记", "纠正", "forget", "correct")
                        )
                        for item in state.pending
                    ):
                        return False
                    if first.proactive:
                        return not any(not item.proactive for item in state.pending)
                    # Group replies have an explicit quoted target. Other speakers
                    # create new targets instead of invalidating an in-flight reply.
                    if first.scope == "group":
                        return True
                    if first.scope == "private" and any(
                        item.command in {"/new", "／new"} for item in state.pending
                    ):
                        return False
                    for item in state.pending:
                        # Messages after an ordered command belong to the next batch.
                        if item.command:
                            break
                        if not item.proactive:
                            return False
                    return True

                try:
                    consumed = await self.process(batch, current)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Conversation processing failed: scope={} error={}",
                        route[1],
                        type(exc).__name__,
                    )
                    continue
                if not consumed:
                    resetting = any(
                        item.command in {"/new", "／new", "/memory", "/记忆"}
                        for item in state.pending
                    )
                    if not resetting and not first.proactive:
                        for event in batch:
                            previous = event.request_id
                            event.request_id = uuid4().hex
                            logger.info(
                                "Turn superseded: previous_request_id={} request_id={}",
                                previous,
                                event.request_id,
                            )
                        state.pending.extendleft(reversed(batch))
        finally:
            self._conversations.pop(route, None)

    def stop_accepting(self) -> None:
        self._accepting = False

    async def close(self) -> None:
        self.stop_accepting()
        tasks = [
            state.task
            for state in self._conversations.values()
            if state.task is not None
        ]
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=self.config.scheduler.shutdown_grace_seconds
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
