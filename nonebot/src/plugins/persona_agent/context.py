from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from nonebot.log import logger

from .config import AgentConfig, ModelConfig
from .conversation import ChatEvent
from .forward import ForwardResolver, ReadForward
from .images import ImageLoader
from .policy import mentioned, sanitize, search_terms
from .prompt import build_messages, select_context
from .storage import Database, utcnow
from .vision import ImageContent, requests_media, without_media_labels


@dataclass
class TurnContext:
    messages: list[dict]
    stickers: list[dict]
    source_ids: list[int]
    memory_ids: list[int]
    episode_ids: list[int]
    epoch: int
    source_reply: str | None = None
    source_image: ImageContent | None = None


class ContextBuilder:
    def __init__(
        self,
        config: AgentConfig,
        db: Database,
        images: ImageLoader,
        stickers,
        read_forward: ReadForward,
        references,
        memory,
    ) -> None:
        self.config = config
        self.db = db
        self.images = images
        self.stickers = stickers
        self._read_forward = read_forward
        self.references = references
        self.memory = memory

    async def conversation_id(self, event: ChatEvent) -> str:
        if event.scope == "group":
            return f"group:{event.group_id}"
        return await self.db.run(
            self.db.store.get_active_private_session, event.user_id
        )

    async def build(
        self,
        focus: ChatEvent,
        batch: list[ChatEvent],
        model_config: ModelConfig,
        *,
        engaged=False,
        participating=False,
        proactive_state=None,
        skill_instructions="",
        skill_catalog="",
    ) -> TurnContext | None:
        store = self.db.store
        epoch = await self.db.run(store.memory.epoch)
        context_id = await self.conversation_id(focus)
        history_limit = self.config.memory.recent_message_limit
        before = (
            None
            if focus.proactive
            else max(event.now for event in [focus, *batch]) + timedelta(microseconds=1)
        )
        candidates = [
            dict(row)
            for row in await self.db.run(
                store.recent_messages,
                context_id,
                max(history_limit * 4, len(batch) + 4),
                before,
            )
        ]
        if focus.raw_id is not None and all(
            row["id"] != focus.raw_id for row in candidates
        ):
            row = await self.db.run(store.raw_message, focus.raw_id)
            if row["context_hidden"] or row["is_recalled"]:
                return None
            candidates.append(row)
        dialogue = await self.db.run(
            store.recent_dialogue, context_id, focus.bot_id, focus.user_id, focus.now
        )
        preferred_ids = {row["id"] for row in dialogue}
        additions = list(dialogue)
        for row in dialogue:
            if row["reply_to_id"] is not None:
                additions.extend(
                    await self.db.run(
                        store.reply_chain, context_id, focus.bot_id, row["reply_to_id"]
                    )
                )
        additions.extend(
            await self.db.run(
                store.context_messages,
                context_id,
                focus.bot_id,
                list(focus.related_raw_ids),
            )
        )
        quote_chain = []
        if focus.reply_to_id is not None:
            quote_chain = await self.db.run(
                store.reply_chain, context_id, focus.bot_id, focus.reply_to_id
            )
            additions.extend(quote_chain)
        elif requests_media(focus.text):
            latest_media = await self.db.run(
                store.recent_media_message,
                context_id,
                focus.bot_id,
                focus.now,
                self.config.group_behavior.recent_window_seconds,
            )
            if latest_media is not None:
                additions.append(latest_media)
        candidates = list(
            {row["id"]: row for row in [*candidates, *additions]}.values()
        )
        candidates.sort(key=lambda row: (row["created_at"], row["id"]))
        candidates = await self.db.run(store.media.enrich_media, candidates)
        if not candidates:
            return None
        current_ids = (
            {focus.raw_id}
            if focus.scope == "group"
            else {event.raw_id for event in batch if event.raw_id is not None}
        )
        required_ids = set(current_ids)
        required_ids.update(row["id"] for row in quote_chain)
        required_ids.update(
            raw_id
            for raw_id in focus.related_raw_ids
            if any(row["id"] == raw_id for row in candidates)
        )
        if focus.scope == "group":
            required_ids.update(
                event.raw_id
                for event in batch
                if event.user_id == focus.user_id and event.now <= focus.now
            )
        if focus.proactive:
            users = [row for row in candidates if row["user_id"]]
            if not users:
                return None
            required_ids.add(users[-1]["id"])
        image_config = (
            model_config.model_copy(
                update={"supports_images": True, "max_images_per_message": 1}
            )
            if focus.image_search
            else model_config
        )
        (
            images,
            quoted_images,
            media_notes,
            media_ids,
            source_error,
        ) = await self._turn_images(focus, batch, candidates, quote_chain, image_config)
        required_ids.update(media_ids)
        if focus.image_search:
            selected = [
                *quoted_images,
                *(image for items in images.values() for image in items),
            ]
            return TurnContext(
                [],
                [],
                sorted(required_ids),
                [],
                [],
                epoch,
                source_reply=source_error,
                source_image=selected[0] if selected else None,
            )
        recent, required_ids = select_context(
            candidates, history_limit, required_ids, preferred_ids=preferred_ids
        )
        memories = await self.memory.recall(focus, recent)
        # Shared experiences now come from attributed memory entries, not line-tail summaries.
        episodes = []
        recalled = await self.db.run(
            store.search_raw, context_id, search_terms(focus.text), 3
        )
        recent_texts = {f"{row['sender']}: {row['text']}" for row in recent}
        recalled = [
            item
            for item in recalled
            if f"{item['sender']}: {item['text']}" not in recent_texts
        ]
        sticker_query = without_media_labels(focus.text + " " + focus.reply_text)
        focus_row = next((row for row in recent if row["id"] == focus.raw_id), None)
        if focus_row is not None:
            sticker_query += " " + " ".join(focus_row["media_descriptions"])
        if not sticker_query.strip() and not focus.proactive:
            related = [
                row
                for row in recent
                if row["id"] < focus.raw_id and row["user_id"] in {0, focus.user_id}
            ]
            sticker_query = " ".join(
                without_media_labels(row["text"])[:160] for row in related[-2:]
            )
        sticker_candidates = await self.stickers.candidates(sticker_query)
        names = [self.config.persona.name, *self.config.persona.aliases]
        packing = {}
        messages = build_messages(
            self.config,
            scope=focus.scope,
            persona_budget=max(100, self.config.budget.context_token_budget // 4),
            memories=memories,
            episodes=[item["summary"] for item in episodes],
            recalled=[f"{item['sender']}: {item['text']}" for item in recalled],
            recent=recent,
            focus=focus,
            required_ids=required_ids,
            current_ids=current_ids,
            now=utcnow(),
            user_id=focus.user_id,
            images=images,
            quoted_images=quoted_images,
            media_notes=media_notes,
            proactive_state=proactive_state,
            to_me=focus.to_me,
            mentioned=mentioned(focus.text, names),
            engaged=engaged,
            stickers=sticker_candidates,
            preferred_ids=preferred_ids,
            selection=packing,
            skill_instructions=skill_instructions,
            skill_catalog=skill_catalog,
        )
        if participating:
            participation_note = "\n本轮已通过参与判断；有具体内容可接时自然回应，无需仅因未被@而再次拒绝参与。"
            messages[-1]["content"] += participation_note
            packing["prompt_chars"] += len(participation_note)
        logger.info(
            "Context built: request_id={} sources={} dropped={} memories={} images={} prompt_chars={}",
            focus.request_id,
            packing["source_ids"],
            [row["id"] for row in recent if row["id"] not in packing["source_ids"]],
            packing["memory_ids"],
            sum(len(items) for items in images.values()) + len(quoted_images),
            packing["prompt_chars"],
        )
        return TurnContext(
            messages,
            sticker_candidates,
            packing["source_ids"]
            + [recalled[index]["id"] for index in packing["recalled_indexes"]],
            packing["memory_ids"],
            [episodes[index]["id"] for index in packing["episode_indexes"]],
            epoch,
        )

    async def _turn_images(
        self, focus, batch, recent, quote_chain, model_config
    ) -> tuple[
        dict[int, list[ImageContent]], list[ImageContent], list[str], set[int], str
    ]:
        images: dict[int, list[ImageContent]] = {}
        quoted_images: list[ImageContent] = []
        notes: list[str] = []
        required: set[int] = set()
        if focus.proactive:
            return images, quoted_images, notes, required, ""
        assert focus.raw_id is not None
        by_id = {int(row["id"]): row for row in recent}
        requests: list[tuple[int, int, str, list[dict], bool]] = []
        media_kinds = {"image", "mface", "forward", "node"}

        def add(row, reason):
            media = [
                item
                for item in json.loads(row["media_json"])
                if item["kind"] in media_kinds
            ]
            if not media or any(
                item[0] == row["id"] and item[1] == row["message_id"]
                for item in requests
            ):
                return False
            requests.append(
                (row["id"], row["message_id"], reason, media, row["user_id"] == 0)
            )
            required.add(row["id"])
            return True

        # A quoted answer to our image-choice question retains the whole original selection.
        if (
            focus.image_search
            and focus.media_followup
            and focus.reply_to_id is not None
        ):
            for raw_id in focus.related_raw_ids:
                if raw_id in by_id:
                    add(by_id[raw_id], "media_followup")
        # A quote has priority over proximity; follow a bot reply back to its original image.
        if focus.reply_to_id is not None and not requests:
            for row in quote_chain:
                if add(row, "quote"):
                    break
            if not requests and focus.reply_media:
                requests.append(
                    (
                        focus.raw_id,
                        focus.reply_to_id,
                        "quote",
                        [item.as_record() for item in focus.reply_media],
                        True,
                    )
                )
            if not requests:
                notes.append(
                    "本条有明确引用；引用链中没有可读取的原图，不改用其他人的邻近图片。"
                )
            if not quote_chain and not focus.reply_text and not focus.reply_media:
                notes.append(
                    f"引用消息 {focus.reply_to_id} 的正文暂不可用；请让对方补充，不能猜测引用内容。"
                )

        if not focus.image_search or focus.reply_to_id is None:
            add(by_id[focus.raw_id], "current")
        if focus.reply_to_id is None:
            # Preserve a picture followed immediately by a question in the same batch.
            preceding = []
            for event in batch:
                if event.raw_id == focus.raw_id:
                    break
                if event.user_id != focus.user_id:
                    continue
                if event.media and not without_media_labels(event.text):
                    preceding.append(event)
                else:
                    preceding.clear()
            for event in preceding:
                if event.raw_id in by_id:
                    add(by_id[event.raw_id], "preceding_media")
        after_target = False
        for event in batch:
            if focus.image_search and focus.reply_to_id is not None:
                break
            if event.raw_id == focus.raw_id:
                after_target = True
                continue
            if not after_target or event.user_id != focus.user_id:
                continue
            if not event.media or without_media_labels(event.text):
                break
            if event.raw_id in by_id:
                add(by_id[event.raw_id], "following_media")

        if not requests and focus.reply_to_id is None:
            for raw_id in focus.related_raw_ids:
                if raw_id in by_id:
                    add(by_id[raw_id], "media_followup")
        if not requests and focus.reply_to_id is None and requests_media(focus.text):
            # The nearest group image can belong to another participant.
            for row in reversed(recent):
                if row["id"] >= focus.raw_id or row["user_id"] == 0:
                    continue
                age = (
                    focus.now - datetime.fromisoformat(row["created_at"])
                ).total_seconds()
                if age > self.config.group_behavior.recent_window_seconds:
                    break
                if add(row, "recent_media"):
                    break

        ordinal = re.search(r"第\s*(\d+|[一二三四五六七八九十]+)\s*张", focus.text)
        if ordinal is None and focus.media_followup:
            ordinal = re.fullmatch(
                r"\s*(\d+|[一二三四五六七八九十]+)[。！!]?\s*", focus.text
            )
        selected_index = None
        if ordinal:
            value = ordinal.group(1)
            digits = {word: index for index, word in enumerate("一二三四五六七八九", 1)}
            if value.isdigit():
                selected_index = int(value)
            elif "十" in value:
                tens, _, units = value.partition("十")
                selected_index = digits.get(tens, 1) * 10 + digits.get(units, 0)
            else:
                selected_index = digits.get(value)
        whole_set = bool(
            re.search(r"全部|所有|整组|每[一]?张|逐张|这些|这几张|一起", focus.text)
        )
        last_image = bool(re.search(r"最后一张|最后那张", focus.text))
        resolver = ForwardResolver(focus.bot_id, self._read_forward)
        pending: list[tuple[int, bool, str, str]] = []
        seen: dict[str, str] = {}
        limit = model_config.max_images_per_message
        expanded = []
        for raw_id, message_id, reason, media, attach_to_focus in requests:
            refs, forward_lines = await resolver.expand(media)
            if forward_lines:
                text = sanitize("\n".join(forward_lines))
                if attach_to_focus:
                    by_id[focus.raw_id]["reply_text"] += "\n" + text
                else:
                    by_id[raw_id]["text"] += "\n" + text
            expanded.append((raw_id, message_id, reason, attach_to_focus, refs))
        total_images = sum(len(item[4]) for item in expanded)
        one_index = total_images if last_image else selected_index
        needs_choice = (
            total_images > 1
            and one_index is None
            and (
                bool(focus.image_search)
                or (
                    not whole_set
                    and (
                        any(
                            item[2] in {"recent_media", "media_followup"}
                            for item in expanded
                        )
                        or bool(re.search(r"上图|这张|那张", focus.text))
                    )
                )
            )
        )
        invalid_index = one_index is not None and not 1 <= one_index <= total_images
        ambiguous = needs_choice or invalid_index
        if needs_choice:
            notes.append(
                f"所指消息共含 {total_images} 张图，指向不明确。本轮未读取；先问要看第几张还是整组。"
            )
        elif invalid_index:
            notes.append(
                f"所指消息共含 {total_images} 张图，找不到第 {one_index} 张；请澄清编号。"
            )
        position = 0
        for raw_id, message_id, reason, attach_to_focus, refs in expanded:
            omitted = 0
            for index, (source, path) in enumerate(refs, 1):
                position += 1
                if ambiguous or (one_index is not None and position != one_index):
                    continue
                label = f"消息 {message_id} 的第 {index} 张图（{path}）"
                if not model_config.supports_images or limit == 0:
                    notes.append(f"{label}：当前模型配置不支持图片输入。")
                    continue
                if source and source in seen:
                    notes.append(
                        f"{label}与{seen[source]}引用同一来源，读取状态以该图为准。"
                    )
                    continue
                if len(pending) >= limit:
                    omitted += 1
                    continue
                if source:
                    seen[source] = label
                pending.append((raw_id, attach_to_focus, source, label))
            if omitted:
                notes.append(
                    f"消息 {message_id} 另有 {omitted} 张图未读取：本轮上限 {limit} 张。"
                )
            logger.info(
                "Chat media selection: request_id={} source_message_id={} raw_id={} reason={} images={} omitted={} ambiguous={} forward_nodes={}",
                focus.request_id,
                focus.message_id,
                raw_id,
                reason,
                len(refs),
                omitted,
                ambiguous,
                resolver.nodes,
            )

        results = await asyncio.gather(
            *(self.images.load(source, model_config) for _, _, source, _ in pending)
        )
        for (raw_id, attach_to_focus, source, label), loaded in zip(pending, results):
            status = (
                "loaded" if loaded else "load_failed" if source else "missing_source"
            )
            notes.append(
                f"{label}：{'已提供原图' if loaded else '读取失败' if source else '缺少可读取的图片地址'}。"
            )
            if loaded:
                image = ImageContent(loaded[0].url, label)
                if attach_to_focus:
                    quoted_images.append(image)
                else:
                    images.setdefault(raw_id, []).append(image)
            logger.info(
                "Chat image result: request_id={} source_message_id={} raw_id={} label={} status={}",
                focus.request_id,
                focus.message_id,
                raw_id,
                label,
                status,
            )
        if not pending:
            notes.append(
                "本轮没有提供原图；占位符不是图像，不能据此声称看过或下载失败。"
            )
        if focus.media_followup:
            notes.append(
                "这是对前一次请求的补图或澄清；结合关联的原请求回答，先前未看到图片不代表这次也看不到。"
            )
        if (
            requests
            or requests_media(focus.text)
            or focus.media_followup
            or (focus.direct and not focus.text.strip())
        ):
            await self.references.remember(
                focus,
                await self.conversation_id(focus),
                sorted(required),
                pending=not requests and not ambiguous,
                choice=ambiguous,
            )
        source_error = ""
        if focus.image_search:
            if needs_choice:
                source_error = (
                    f"这里有 {total_images} 张图，要查第几张的出处？一次查一张。"
                )
            elif invalid_index:
                source_error = f"这里有 {total_images} 张图，找不到第 {one_index} 张，请重新选编号。"
            elif not total_images:
                source_error = (
                    "引用链里没找到可检索的原图，请重新引用图片或直接发图。"
                    if focus.reply_to_id is not None
                    else "请发图或引用要查出处的图片。"
                )
            elif not images and not quoted_images:
                source_error = "原图读取失败，请重新发图后再查。"
        return images, quoted_images, notes, required, source_error
