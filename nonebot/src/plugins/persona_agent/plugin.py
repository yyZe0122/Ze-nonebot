from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from nonebot import get_bot, get_bots, get_driver, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException

from .config import PROJECT_ROOT
from .conversation import ChatEvent
from .service import PersonaService
from .vision import render_segments

async def _send_message(
    event: ChatEvent, text: str, reply_to_id: int | None, sticker: dict | None
) -> int:
    bot = get_bot(event.bot_id)
    message = Message()
    if reply_to_id is not None:
        message += MessageSegment.reply(reply_to_id)
    if text:
        message += MessageSegment.text(text)
    if sticker is not None:
        message += MessageSegment(sticker["type"], sticker["data"])
    if event.scope == "group":
        result = await bot.send_group_msg(group_id=event.group_id, message=message)
    else:
        result = await bot.send_private_msg(user_id=event.user_id, message=message)
    return int(result["message_id"])


async def _read_forward(bot_id: str, forward_id: str) -> dict:
    return await get_bot(bot_id).get_forward_msg(id=forward_id)


service: PersonaService
driver = get_driver()
chat = on_message(priority=5, block=False)
recall = on_notice(priority=5, block=False)
_cleanup_task: asyncio.Task | None = None
_proactive_task: asyncio.Task | None = None


async def _periodic_cleanup() -> None:
    while True:
        try:
            await asyncio.sleep(1800)
            await service.purge()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"persona_agent cleanup failed: {type(exc).__name__}")


async def _periodic_proactive() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await service.enqueue_proactive(set(get_bots()), datetime.now(UTC))
        except Exception as exc:
            logger.warning(
                f"persona_agent proactive scheduling failed: {type(exc).__name__}"
            )


@driver.on_startup
async def _start_cleanup() -> None:
    global service, _cleanup_task, _proactive_task
    service = await PersonaService.from_files(
        Path(os.environ.get("PERSONA_AGENT_CONFIG", PROJECT_ROOT / "config/persona_agent.yaml")),
        _send_message,
        read_forward=_read_forward,
    )
    await service.start()
    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    if service.config.private_proactive.enabled:
        _proactive_task = asyncio.create_task(_periodic_proactive())


@driver.on_shutdown
async def _close_store() -> None:
    tasks = [task for task in (_cleanup_task, _proactive_task) if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await service.close()


@recall.handle()
async def _(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    if event.group_id in service.config.access.allowed_groups:
        async with service.context_guard:
            await service.db.run(
                service.store.recall_message,
                bot.self_id,
                event.group_id,
                event.message_id,
            )


@chat.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    if str(event.user_id) == bot.self_id:
        return
    if isinstance(event, GroupMessageEvent):
        scope = "group"
        group_id = event.group_id
    elif isinstance(event, PrivateMessageEvent):
        scope = "private"
        group_id = None
    else:
        return
    incoming = render_segments(
        [(segment.type, dict(segment.data)) for segment in event.get_message()]
    )
    reply_to_id = None
    reply_sender = ""
    reply_text = ""
    reply_media = ()
    # The adapter leaves the reply segment intact when get_msg fails.
    for segment in event.get_message():
        if segment.type == "reply":
            try:
                reply_to_id = int(segment.data["id"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Malformed incoming reply identifier")
            break
    to_other = any(
        segment.type == "at"
        and str(segment.data.get("qq", "")) not in {"", bot.self_id}
        for segment in event.get_message()
    )
    if event.reply is not None:
        reply_to_id = event.reply.message_id
        quoted = render_segments(
            [(segment.type, dict(segment.data)) for segment in event.reply.message]
        )
        reply_text = quoted.text
        reply_media = quoted.media
        sender = event.reply.sender
        to_other = to_other or str(sender.user_id) != bot.self_id
        reply_sender = (
            f"{sender.card or sender.nickname or '未知'}（用户 {sender.user_id}）"
        )
    await service.receive(
        ChatEvent(
            scope=scope,
            bot_id=bot.self_id,
            user_id=event.user_id,
            group_id=group_id,
            message_id=event.message_id,
            text=incoming.text,
            to_me=event.is_tome()
            or any(
                segment.type == "at" and str(segment.data.get("qq", "")) == bot.self_id
                for segment in event.get_message()
            ),
            now=datetime.now(UTC),
            images=incoming.images,
            media=incoming.media,
            sender_name=event.sender.card
            or event.sender.nickname
            or str(event.user_id),
            reply_to_id=reply_to_id,
            reply_sender=reply_sender,
            reply_text=reply_text,
            reply_media=reply_media,
            to_other=to_other,
        )
    )
    raise FinishedException
