from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.log import logger

from .storage import utcnow


@dataclass
class DeliveryResult:
    sent: int
    outcome: str
    sent_at: datetime


class ReplyDelivery:
    def __init__(self, config, db, stickers, send_message, context_guard) -> None:
        self.config, self.db, self.stickers, self.send_message = (
            config,
            db,
            stickers,
            send_message,
        )
        self._locks = {}
        self.context_guard = context_guard

    def lock(self, event):
        return self._locks.setdefault(event.route, asyncio.Lock())

    async def _part(
        self,
        event,
        text,
        sticker,
        quote,
        request_id,
        index,
        total,
        *,
        control=False,
        epoch=None,
        valid=None,
    ):
        runtime = self.db.store.runtime
        await self.db.run(runtime.part, request_id, index, "preparing")
        try:
            segment = (
                await asyncio.to_thread(self.stickers.segment, sticker)
                if sticker
                else None
            )
        except (OSError, ValueError) as exc:
            await self.db.run(runtime.part, request_id, index, "failed")
            logger.warning(
                "Reply media failed: request_id={} part={} error={}",
                request_id,
                index,
                type(exc).__name__,
            )
            return None, "failed"
        async with self.context_guard:
            if (
                epoch is not None
                and epoch != await self.db.run(self.db.store.memory.epoch)
            ) or (valid is not None and not await valid()):
                await self.db.run(runtime.part, request_id, index, "stale")
                return None, "stale"
            return await self._send_recorded(
                event,
                text,
                sticker,
                segment,
                quote,
                request_id,
                index,
                total,
                control=control,
            )

    async def _send_recorded(
        self, event, text, sticker, segment, quote, request_id, index, total, *, control
    ):
        runtime = self.db.store.runtime
        await self.db.run(runtime.part, request_id, index, "sending")
        try:
            async with asyncio.timeout(self.config.scheduler.send_timeout_seconds):
                sent_id = await self.send_message(event, text, quote, segment)
        except asyncio.CancelledError:
            await self.db.run(runtime.part, request_id, index, "unknown")
            raise
        except Exception as exc:
            outcome = "failed" if isinstance(exc, ActionFailed) else "unknown"
            await self.db.run(runtime.part, request_id, index, outcome)
            logger.warning(
                "QQ send stopped: request_id={} part={}/{} outcome={} error={}",
                request_id,
                index,
                total,
                outcome,
                type(exc).__name__,
            )
            return None, outcome
        await self.db.run(runtime.part, request_id, index, "sent", sent_id)
        at = utcnow()
        if event.scope == "group":
            conversation = await self.db.run(
                self.db.store.get_group_conversation_id, event.group_id, at
            )
        else:
            conversation = await self.db.run(
                self.db.store.get_active_private_session, event.user_id
            )
        raw_id = await self.db.run(
            self.db.store.add_raw,
            scope=event.scope,
            conversation_id=conversation,
            user_id=0,
            group_id=event.group_id,
            sender=self.config.persona.name,
            sender_name=self.config.persona.name,
            text=text,
            retention=timedelta(days=self.config.memory.raw_retention_days),
            now=at,
            bot_id=event.bot_id,
            message_id=sent_id,
            reply_to_id=event.message_id,
            is_proactive=event.proactive,
            reply_sender=f"{event.sender_name}（用户 {event.user_id}）"
            if not event.proactive
            else "",
            reply_text=event.text if not event.proactive else "",
            media=[{"kind": "sticker", "asset_id": sticker["id"]}] if sticker else [],
            is_control=control,
        )
        await self.db.run(runtime.part, request_id, index, "sent", sent_id, raw_id)
        if sticker:
            await self.db.run(self.db.store.media.note_media_sent, sticker["id"])
        logger.info(
            "QQ message sent: request_id={} scope={} source_message_id={} sent_message_id={} quote={} sticker_id={} part={}/{}",
            request_id,
            event.scope,
            event.message_id,
            sent_id,
            quote,
            sticker["id"] if sticker else None,
            index,
            total,
        )
        return sent_id, "sent"

    async def send(self, event, decision, context, valid) -> DeliveryResult:
        if decision.target_message_id != event.message_id:
            return DeliveryResult(0, "invalid_target", utcnow())
        allowed = {row["id"] for row in context.stickers}
        available = {
            row["id"]: row
            for row in await self.db.run(self.db.store.media.favorite_media)
            if row["id"] in allowed
        }
        parts = [
            (item.text.strip(), available.get(item.sticker_id))
            for item in decision.messages
        ]
        if (
            not parts
            or any(not text and not sticker for text, sticker in parts)
            or any(
                item.sticker_id is not None and item.sticker_id not in available
                for item in decision.messages
            )
        ):
            return DeliveryResult(0, "invalid_parts", utcnow())
        runtime = self.db.store.runtime
        await self.db.run(
            runtime.turn_start,
            event.request_id,
            event,
            context.source_ids,
            context.memory_ids,
            context.episode_ids,
        )
        sent = 0
        outcome = "interrupted"
        at = utcnow()
        try:
            async with self.lock(event):
                for index, (text, sticker) in enumerate(parts, 1):
                    if index > 1:
                        await asyncio.sleep(0.6)
                    if (
                        not await valid()
                        or await self.db.run(self.db.store.memory.epoch)
                        != context.epoch
                    ):
                        outcome = "stale"
                        break
                    quote = (
                        event.message_id
                        if event.scope == "group" and index == 1
                        else None
                    )
                    sent_id, outcome = await self._part(
                        event,
                        text,
                        sticker,
                        quote,
                        event.request_id,
                        index,
                        len(parts),
                        epoch=context.epoch,
                        valid=valid,
                    )
                    if sent_id is None:
                        break
                    sent += 1
                    at = utcnow()
                else:
                    outcome = "replied"
            if sent and sent < len(parts):
                outcome = "partial_" + outcome
            return DeliveryResult(sent, outcome, at)
        except asyncio.CancelledError:
            outcome = "partial_unknown" if sent else "unknown"
            raise
        finally:
            await self.db.run(runtime.turn_finish, event.request_id, outcome)
            logger.info(
                "Reply delivery: request_id={} planned={} sent={} outcome={}",
                event.request_id,
                len(parts),
                sent,
                outcome,
            )

    async def command(
        self,
        event,
        text,
        *,
        memory_ids=None,
        epoch=None,
        request_id=None,
        quote=False,
        valid=None,
    ) -> None:
        if request_id is None:
            request_id = event.request_id + ":control:" + uuid4().hex[:8]
        runtime = self.db.store.runtime
        started = await self.db.run(
            runtime.turn_start,
            request_id,
            event,
            [],
            memory_ids or [],
            [],
            once=True,
        )
        if not started:
            logger.info(
                "Control message skipped: request_id={} reason=already_recorded",
                request_id,
            )
            return
        outcome = "interrupted"
        try:
            async with self.lock(event):
                _, outcome = await self._part(
                    event,
                    text,
                    None,
                    event.message_id if event.scope == "group" or quote else None,
                    request_id,
                    1,
                    1,
                    control=True,
                    epoch=epoch,
                    valid=valid,
                )
        except asyncio.CancelledError:
            outcome = "unknown"
            raise
        finally:
            await self.db.run(runtime.turn_finish, request_id, outcome)
