from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from nonebot.log import logger
from pydantic import ValidationError

from .commands import Commands
from .config import AgentConfig, load_config, validate_runtime_layout
from .context import ContextBuilder
from .conversation import ChatEvent, ConversationQueue, SendMessage
from .decisions import (
    ChatDecision,
    ReplyMessage,
    decision_issues,
    parse_decision,
    policy_blocked,
    skill_target_issue,
)
from .delivery import ReplyDelivery
from .forward import ReadForward
from .gateway import ModelFailure, ModelGateway
from .images import ImageLoader
from .image_sources import ImageSourceSearch
from .memory import MemoryService
from .participation import ParticipationGate
from .policy import advance_group_state, discloses_private, mentioned, sanitize
from .references import MessageReferences
from .skills import SkillLibrary
from .stickers import StickerLibrary
from .storage import Database, utcnow
from .vision import image_search_kind


POLICY_GARBLED = "#&@*|@#"
POLICY_RETRIES = 3


def _decision_correction(detail) -> str:
    fields = "、".join(str(item[0][-1]) for item in detail if item and item[0])
    problem = (
        f"需要修正的字段：{fields}。"
        if fields
        else "上一轮没有输出合法的 JSON 对象，可能写成了普通聊天文本。"
    )
    return (
        problem
        + "只重新输出一个完整 JSON 对象，不要解释、不要用代码块、不要在 JSON 外写任何文字。"
        "激活技能时 reply=false、messages=[]、memory_updates=[]，"
        "并填写本轮指定的 target_message_id。"
    )


class PersonaService:
    """Wire ingestion, participation, context, generation and delivery in order."""

    def __init__(
        self,
        config: AgentConfig,
        db: Database,
        send_message: SendMessage,
        *,
        config_path: Path,
        read_forward: ReadForward | None = None,
    ) -> None:
        self.config, self.db, self.config_path = config, db, config_path
        self.store = db.store
        self._ingest = asyncio.Lock()
        self.context_guard = asyncio.Lock()
        self._busy_notice: dict[tuple, float] = {}
        self.queue = ConversationQueue(config, self._handle_batch)
        self.gateway = ModelGateway(config, db)
        self.images = ImageLoader()
        self.image_sources = ImageSourceSearch(config, db)

        def idle():
            return not self.queue.has_work and self.gateway.slots.idle

        self.stickers = StickerLibrary(config, db, self.gateway, self.images, idle)
        self.memory = MemoryService(config, db, self.gateway, idle, self.context_guard)
        self.skills = SkillLibrary(config)
        self.references = MessageReferences(config, db)
        self.context = ContextBuilder(
            config,
            db,
            self.images,
            self.stickers,
            read_forward,
            self.references,
            self.memory,
        )
        self.participation = ParticipationGate(config, db, self.gateway)
        self.delivery = ReplyDelivery(
            config, db, self.stickers, send_message, self.context_guard
        )
        self.commands = Commands(self)

    @classmethod
    async def from_files(
        cls,
        config_path: Path,
        send_message: SendMessage,
        *,
        read_forward: ReadForward | None = None,
    ) -> "PersonaService":
        config = await asyncio.to_thread(load_config, config_path)
        validate_runtime_layout(config)
        db = await Database.open(config.database_path)
        service = cls(
            config, db, send_message, config_path=config_path, read_forward=read_forward
        )
        logger.info(
            "Persona agent initialized: schema=5 model={} task_models={}",
            config.active_model,
            {
                name: getattr(config.model_tasks, name).model or config.active_model
                for name in ("participation", "media", "memory")
            },
        )
        return service

    async def start(self) -> None:
        await asyncio.to_thread(self.skills.load)
        logger.info("Image source configuration: {}", self.image_sources.status())
        await self.purge()
        await self.stickers.start()
        self.memory.start()

    async def close(self) -> None:
        self.queue.stop_accepting()
        await self.memory.close()
        await self.stickers.close()
        await self.queue.close()
        await self.images.close()
        await self.image_sources.close()
        await self.gateway.close()
        await self.db.close()

    async def purge(self) -> None:
        result = await self.db.run(
            self.store.purge,
            utcnow(),
            usage_retention_days=self.config.memory.usage_retention_days,
            observed_media_retention_days=self.config.memory.observed_media_retention_days,
        )
        await self.db.run(
            self.store.runtime.purge, self.config.memory.audit_retention_days
        )
        logger.info("Data retention cleanup: {}", result)

    def initial_check_at(self, now: datetime) -> datetime:
        return now + timedelta(
            minutes=self.config.private_proactive.initial_check_minutes
        )

    async def receive(self, event: ChatEvent) -> None:
        event.text, event.reply_text = sanitize(event.text), sanitize(event.reply_text)
        if not event.text and event.images:
            event.text = "[图片]"
        if not self.config.allows(event.scope, event.user_id, event.group_id):
            await self.audit(event, "access_denied", allowed=False)
            return
        context_id = await self.context.conversation_id(event)
        await self.references.hydrate_quote(event, context_id)
        event.image_search = image_search_kind(event.text)
        event.direct = (
            event.scope == "private"
            or event.to_me
            or event.text.lstrip().startswith(
                ("/搜图", "/查图", "/查出处", "／搜图", "／查图", "／查出处")
            )
            or (
                self.config.skills.enabled
                and self.skills.explicit(event.text) is not None
            )
            or mentioned(
                event.text, [self.config.persona.name, *self.config.persona.aliases]
            )
        )
        async with self._ingest:
            if not await self._record(event):
                return
            await self.references.link(event, context_id)
            accepted = self.queue.submit(event)
        if not accepted:
            await self.audit(event, "queue_full")
            now = asyncio.get_running_loop().time()
            if (event.scope == "private" or event.at_me) and now >= self._busy_notice.get(
                event.route, 0
            ):
                self._busy_notice[event.route] = now + 30
                await self.delivery.command(
                    event, "这边的消息队列暂时满了，这条没有进入回复队列，请稍后再发。"
                )

    async def _record(self, event: ChatEvent) -> bool:
        if event.scope == "group":
            conversation = await self.db.run(
                self.store.get_group_conversation_id, event.group_id, event.now
            )
        else:
            conversation = await self.context.conversation_id(event)
        event.raw_id = await self.db.run(
            self.store.add_raw,
            scope=event.scope,
            conversation_id=conversation,
            user_id=event.user_id,
            group_id=event.group_id,
            sender=str(event.user_id),
            text=event.text,
            retention=timedelta(days=self.config.memory.raw_retention_days),
            now=event.now,
            bot_id=event.bot_id,
            message_id=event.message_id,
            sender_name=event.sender_name,
            reply_to_id=event.reply_to_id,
            reply_sender=event.reply_sender,
            reply_text=event.reply_text,
            media=[item.as_record() for item in event.media],
            is_control=bool(event.command),
        )
        if event.raw_id is None:
            return False
        if not await self.db.run(self.store.raw_available, event.raw_id):
            await self.audit(event, "source_suppressed")
            return False
        if event.scope == "private":
            await self.db.run(
                self.store.note_private_message,
                event.bot_id,
                event.user_id,
                incoming=True,
                now=event.now,
                next_check_at=self.initial_check_at(event.now),
            )
        else:
            await self._group_state(event, replied=False, now=event.now)
        if not event.command:
            await self.stickers.observe(
                event.raw_id, event.user_id, event.media, route=event.route
            )
        return True

    async def _group_state(
        self, event, *, replied: bool, now, continuation=False
    ) -> None:
        def update():
            state = advance_group_state(
                self.store.group_state(event.group_id),
                user_id=event.user_id,
                replied=replied,
                proactive=replied and not event.direct and not continuation,
                now=now,
                config=self.config,
                addressed=event.direct,
                continued=continuation,
            )
            self.store.save_group_state(event.group_id, state)
            return state

        state = await self.db.run(update)
        if replied:
            logger.info(
                "Group followup opened: request_id={} group_id={} user_id={} until={}",
                event.request_id,
                event.group_id,
                event.user_id,
                state["active_thread_until"],
            )

    async def enqueue_proactive(self, connected_bots: set[str], now: datetime) -> None:
        if not self.config.private_proactive.enabled:
            return
        rows = await self.db.run(self.store.proactive_candidates, now)
        for row in rows:
            event = ChatEvent(
                scope="private",
                bot_id=str(row["bot_id"]),
                user_id=int(row["user_id"]),
                group_id=None,
                message_id=None,
                text="",
                to_me=False,
                now=now,
                proactive=True,
            )
            if (
                event.bot_id in connected_bots
                and self.config.allows("private", event.user_id, None)
                and not self.queue.busy(event)
            ):
                self.queue.submit(event)

    async def _handle_batch(
        self, batch: list[ChatEvent], current: Callable[[], bool]
    ) -> bool:
        first = batch[0]
        if first.command:
            await self.commands.handle(first)
            return True
        if first.proactive:

            async def available():
                return current()

            return await self._respond(first, [], available)
        accepted = []
        for event in batch:
            age = (utcnow() - event.now).total_seconds()
            limit = (
                self.config.scheduler.direct_max_age_seconds
                if event.direct
                else self.config.scheduler.ordinary_max_age_seconds
            )
            if age > limit:
                await self.audit(event, "queue_expired")
                continue
            if await self.db.run(self.store.raw_available, event.raw_id):
                if event.scope == "private":
                    await self.db.run(
                        self.store.assign_private_session, event.raw_id, event.user_id
                    )
                accepted.append(event)
        if not accepted:
            return True
        batch_ids = {event.raw_id for event in accepted}
        targets = []
        media_parents = set()
        for event in accepted:
            if event.direct:
                targets.append(event)
            elif (
                event.media_followup
                and event.media_parent_id not in batch_ids | media_parents
            ):
                targets.append(event)
                media_parents.add(event.media_parent_id)
        targets = targets or [accepted[-1]]
        if first.scope == "private":
            targets = [accepted[-1]]
        for focus in targets:

            async def available(focus=focus):
                return current() and await self.db.run(
                    self.store.raw_available, focus.raw_id
                )

            if not await available():
                if first.scope == "private":
                    return False
                continue
            consumed = await self._respond(focus, accepted, available)
            if not consumed and first.scope == "private":
                return False
        return True

    async def _respond(self, focus: ChatEvent, batch: list[ChatEvent], valid) -> bool:
        epoch = await self.db.run(self.store.memory.epoch)
        plan = await self.participation.consider(focus)
        if not plan.reply:
            await self.audit(focus, plan.reason)
            return True
        if not await valid():
            return False
        proactive_state = None
        if focus.proactive:
            rows = await self.db.run(self.store.proactive_candidates, utcnow())
            proactive_state = next(
                (
                    dict(row)
                    for row in rows
                    if str(row["bot_id"]) == focus.bot_id
                    and row["user_id"] == focus.user_id
                ),
                None,
            )
            if proactive_state is None:
                return True
            await self.db.run(
                self.store.schedule_proactive,
                focus.bot_id,
                focus.user_id,
                utcnow(),
                utcnow()
                + timedelta(minutes=self.config.private_proactive.error_retry_minutes),
            )
        try:
            active_skills = await asyncio.to_thread(self.skills.initial, focus.text)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Skill selection failed: request_id={} reason={}",
                focus.request_id,
                str(exc),
            )
            await self.delivery.command(
                focus,
                "指定的技能未启用、不可用或超过本轮容量。管理员可在私聊用 /skills 查看。",
            )
            await self.audit(focus, "skill_unavailable")
            return True
        clock = asyncio.get_running_loop().time
        deadline = clock() + self.config.scheduler.chat_timeout_seconds
        policy_attempts = 0
        correction = ""
        retried = False
        skill_round = 0
        decision = None
        turn = None
        async with self.gateway.lease("chat") as lease:
            while skill_round < 2:
                catalog = (
                    self.skills.catalog(active_skills)
                    if skill_round == 0 and not focus.proactive
                    else ""
                )
                context = await self.context.build(
                    focus,
                    batch,
                    lease.config,
                    engaged=plan.continuation,
                    participating=focus.scope == "group" and not plan.direct,
                    proactive_state=proactive_state,
                    skill_instructions=active_skills.instructions,
                    skill_catalog=catalog,
                )
                if context is None:
                    return True
                turn = context
                if correction:
                    context.messages.append({"role": "user", "content": correction})
                    correction = ""
                if not await valid() or context.epoch != await self.db.run(
                    self.store.memory.epoch
                ):
                    return False
                if context.source_reply is not None:

                    async def source_valid(base_valid=valid, context=context):
                        if not await base_valid() or context.epoch != await self.db.run(
                            self.store.memory.epoch
                        ):
                            return False
                        return await self.db.run(
                            lambda: all(
                                self.store.raw_available(raw_id)
                                for raw_id in context.source_ids
                            )
                        )

                    reply_text = context.source_reply
                    if context.source_image is not None:
                        reply_text = await self.image_sources.lookup(
                            context.source_image,
                            focus.image_search,
                            (
                                *focus.route,
                                await self.context.conversation_id(focus),
                                context.epoch,
                            ),
                            focus.request_id,
                            source_valid,
                        )
                    if reply_text is None or not await source_valid():
                        return False
                    valid = source_valid
                    decision = ChatDecision(
                        reply=True,
                        target_message_id=focus.message_id,
                        continuation=plan.continuation,
                        messages=[ReplyMessage(text=reply_text)],
                    )
                    break
                logger.info(
                    "Turn skills: request_id={} round={} names={} versions={} reason={}",
                    focus.request_id,
                    skill_round + 1,
                    active_skills.requests,
                    active_skills.versions,
                    "model" if skill_round else "explicit_or_default",
                )
                remaining = deadline - clock()
                completion = (
                    await self.gateway.complete(
                        context.messages,
                        focus.user_id,
                        request_id=focus.request_id,
                        priority=2 if focus.proactive else 0 if plan.direct else 1,
                        lease=replace(lease, timeout=remaining),
                    )
                    if remaining > 0
                    else ModelFailure("request_timeout")
                )
                if isinstance(completion, ModelFailure):
                    await self.audit(focus, "reply_failed:" + completion.reason)
                    await self._failure_notice(
                        focus, plan, completion.reason, context.epoch, valid
                    )
                    return True
                reply_text = completion.text
                if not await valid() or context.epoch != await self.db.run(
                    self.store.memory.epoch
                ):
                    await self.audit(focus, "context_changed")
                    return False
                if policy_blocked(completion.text):
                    policy_attempts += 1
                    logger.warning(
                        "Policy block: request_id={} attempt={}/{} model={}",
                        focus.request_id,
                        policy_attempts,
                        POLICY_RETRIES + 1,
                        lease.config.model,
                    )
                    if policy_attempts <= POLICY_RETRIES:
                        continue
                    await self.audit(focus, "policy_blocked")
                    await self._failure_notice(
                        focus, plan, "policy_blocked", context.epoch, valid
                    )
                    return True
                try:
                    decision = parse_decision(completion.text, ChatDecision)
                except ValidationError as exc:
                    issues = decision_issues(exc)
                    logger.warning(
                        "Reply rejected: request_id={} reason=invalid_decision issues={} "
                        "finish_reason={} chars={} text={!r}",
                        focus.request_id,
                        issues,
                        completion.finish_reason,
                        len(completion.text),
                        completion.text[:200],
                    )
                    if not retried:
                        retried = True
                        correction = _decision_correction(issues)
                        continue
                    await self.audit(focus, "invalid_reply_plan")
                    await self._failure_notice(
                        focus, plan, "invalid_reply", context.epoch, valid
                    )
                    return True
                if decision.activate_skills:
                    filled = skill_target_issue(
                        decision, focus.message_id, self.skills.skills
                    )
                    if filled is not None:
                        logger.info(
                            "Skill target filled: request_id={} target_message_id={}",
                            focus.request_id,
                            focus.message_id,
                        )
                        decision = filled
                if not decision.activate_skills:
                    break
                try:
                    if (
                        not catalog
                        or skill_round
                        or decision.reply
                        or decision.messages
                        or decision.memory_updates
                        or decision.target_message_id != focus.message_id
                    ):
                        raise ValueError("invalid_skill_activation")
                    active_skills = await asyncio.to_thread(
                        self.skills.activate, active_skills, decision.activate_skills
                    )
                    skill_round += 1
                except (OSError, ValueError) as exc:
                    if not retried:
                        logger.warning(
                            "Skill activation rejected: request_id={} reason={}",
                            focus.request_id,
                            str(exc),
                        )
                        retried = True
                        correction = _decision_correction(str(exc))
                        continue
                    logger.warning(
                        "Skill activation rejected: request_id={} reason={}",
                        focus.request_id,
                        str(exc),
                    )
                    await self.audit(focus, "invalid_skill_activation")
                    await self._failure_notice(
                        focus, plan, "invalid_reply", context.epoch, valid
                    )
                    return True

        if decision is None:
            await self.audit(focus, "reply_failed:request_timeout")
            await self._failure_notice(
                focus,
                plan,
                "request_timeout",
                turn.epoch if turn is not None else epoch,
                valid,
            )
            return True
        if (
            decision.reply or decision.memory_updates
        ) and decision.target_message_id != focus.message_id:
            logger.warning(
                "Reply rejected: request_id={} reason=wrong_target expected={} actual={}",
                focus.request_id,
                focus.message_id,
                decision.target_message_id,
            )
            await self.audit(focus, "invalid_reply_target")
            await self._failure_notice(
                focus, plan, "invalid_reply", context.epoch, valid
            )
            return True
        if focus.proactive and (
            decision.next_check_minutes is None
            or not 1 <= decision.next_check_minutes <= 10080
        ):
            logger.warning(
                "Reply rejected: request_id={} reason=invalid_proactive_schedule",
                focus.request_id,
            )
            await self.audit(focus, "invalid_proactive_schedule")
            return True
        if decision.memory_updates:
            if focus.proactive:
                await self.audit(focus, "proactive_memory_update_rejected")
                return True
            current_sources = {
                item.raw_id
                for item in batch
                if item.user_id == focus.user_id
                and (focus.scope == "private" or item.raw_id == focus.raw_id)
            } & set(context.source_ids)
            async with self.context_guard:
                if not await valid() or context.epoch != await self.db.run(
                    self.store.memory.epoch
                ):
                    return False
                updates = await self.db.run(
                    self.memory.apply_updates,
                    focus,
                    decision.memory_updates,
                    context.memory_ids,
                    current_sources,
                    context.epoch,
                )
                context.epoch = await self.db.run(self.store.memory.epoch)
            if updates is None:
                await self.audit(focus, "memory_update_rejected")
                await self.delivery.command(
                    focus,
                    "这次记忆纠正没有全部保存，可以用 /记忆 查看并指定编号纠正。",
                    epoch=context.epoch,
                    valid=valid,
                )
                return True
            context.memory_ids = [
                updates.get(ident, ident) for ident in context.memory_ids
            ]
            logger.info(
                "Memory corrected in chat: request_id={} revisions={}",
                focus.request_id,
                updates,
            )
        next_check = self.initial_check_at(utcnow())
        if focus.proactive:
            next_check = utcnow() + timedelta(minutes=decision.next_check_minutes)
            await self.db.run(
                self.store.schedule_proactive,
                focus.bot_id,
                focus.user_id,
                utcnow(),
                next_check,
            )
        if not decision.reply:
            await self.participation.note(focus, False)
            await self.audit(focus, "model_silence")
            return True
        if focus.scope == "group":
            private = await self.db.run(
                self.store.memory.list, focus.bot_id, focus.user_id, "private", None
            )
            if discloses_private(
                reply_text,
                [row["text"] for row in private if row["scope"] == "private"],
            ):
                await self.audit(focus, "private_disclosure_blocked")
                return True
        result = await self.delivery.send(focus, decision, context, valid)
        if result.sent:
            await self.participation.note(focus, True)
            if focus.scope == "group":
                await self._group_state(
                    focus,
                    replied=True,
                    now=result.sent_at,
                    continuation=plan.continuation,
                )
            else:
                await self.db.run(
                    self.store.note_private_message,
                    focus.bot_id,
                    focus.user_id,
                    incoming=False,
                    proactive=focus.proactive,
                    now=result.sent_at,
                    next_check_at=next_check
                    if focus.proactive
                    else self.initial_check_at(result.sent_at),
                )
        await self.audit(focus, result.outcome)
        if not result.sent and result.outcome in {
            "invalid_target",
            "invalid_parts",
            "failed",
        }:
            await self._failure_notice(
                focus, plan, result.outcome, context.epoch, valid
            )
        return result.sent > 0 or result.outcome != "stale"

    async def _failure_notice(
        self, event, plan, reason: str, epoch: int, valid
    ) -> None:
        if (
            event.proactive
            or not plan.direct
            or (event.scope == "group" and not event.at_me)
        ):
            return
        if reason in {"request_timeout", "queue_timeout", "upstream_timeout"}:
            limit = self.config.scheduler.chat_timeout_seconds
            detail = {
                "request_timeout": "超时",
                "queue_timeout": "排队超时",
                "upstream_timeout": "收到上游超时",
            }[reason]
            text = f"聊天生成{detail}（本地上限 {limit:g} 秒），本条未生成回复。"
        elif reason == "rate_limited":
            text = "这边暂时忙不过来，这句没答上，稍后再叫我一下。"
        elif reason == "policy_blocked":
            text = POLICY_GARBLED
        else:
            text = "这次没处理成功，稍后再叫我一下。"
        request_id = (
            f"failure:{event.bot_id}:{event.scope}:{event.route[2]}:{event.message_id}"
        )
        logger.info(
            "Failure notice: request_id={} notice_id={} source_message_id={} reason={}",
            event.request_id,
            request_id,
            event.message_id,
            reason,
        )
        await self.delivery.command(
            event,
            text,
            epoch=epoch,
            request_id=request_id,
            quote=True,
            valid=valid,
        )

    async def audit(self, event: ChatEvent, decision: str, *, allowed=True) -> None:
        await self.db.run(
            self.store.audit,
            scope=event.scope,
            allowed=allowed,
            decision=decision,
            user_id=event.user_id,
            group_id=event.group_id,
            message_id=event.message_id,
            text=event.text,
            retention=timedelta(days=self.config.memory.audit_retention_days),
            now=utcnow(),
        )
        logger.info(
            "Turn outcome: request_id={} scope={} source_message_id={} outcome={}",
            event.request_id,
            event.scope,
            event.message_id,
            decision,
        )
