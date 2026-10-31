from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from nonebot.log import logger

from .vision import IncomingMedia, requests_media, without_media_labels


class MessageReferences:
    """Keep reply provenance and short-lived media follow-ups in the existing store."""

    def __init__(self, config, db):
        self.config, self.db = config, db

    def key(self, event):
        return "media-context:" + ":".join(map(str, (*event.route, event.user_id)))

    async def hydrate_quote(self, event, context_id):
        if event.reply_to_id is None:
            return
        row = await self.db.run(
            self.db.store.quoted_message, context_id, event.bot_id, event.reply_to_id
        )
        if row is not None and not row["is_recalled"]:
            if not event.reply_text:
                event.reply_text = row["text"]
                event.reply_sender = (
                    f"{row['sender_name'] or row['sender']}（用户 {row['user_id']}）"
                )
            if not event.reply_media:
                event.reply_media = tuple(
                    IncomingMedia.from_record(item)
                    for item in json.loads(row["media_json"])
                    if item["kind"] in {"image", "mface", "forward", "node"}
                )
            if row["user_id"] == 0:
                event.to_me = True
            else:
                event.to_other = True
        logger.info(
            "Quote resolved: request_id={} source_message_id={} quote={} local={} content={}",
            event.request_id,
            event.message_id,
            event.reply_to_id,
            row is not None,
            bool(event.reply_text or event.reply_media),
        )

    async def link(self, event, context_id):
        runtime = self.db.store.runtime
        key = self.key(event)
        if event.command:
            if event.command in {"/new", "／new", "/memory", "/记忆"}:
                await self.db.run(runtime.put, key, {})
            return
        state = await self.db.run(runtime.get, key, {})
        epoch = await self.db.run(self.db.store.memory.epoch)
        active = (
            state
            and state["context_id"] == context_id
            and state["epoch"] == epoch
            and datetime.fromisoformat(state["until"]) > event.now
        )
        quoted_followup = False
        if (
            active
            and state.get("image_search")
            and event.to_me
            and event.reply_to_id is not None
        ):
            chain = await self.db.run(
                self.db.store.reply_chain, context_id, event.bot_id, event.reply_to_id
            )
            quoted_followup = any(row["id"] == state["request_raw_id"] for row in chain)
        if (
            active
            and (event.reply_to_id is None or quoted_followup)
            and not event.to_other
        ):
            ids = [state["request_raw_id"], *state["media_ids"]]
            rows = await self.db.run(
                self.db.store.context_messages, context_id, event.bot_id, ids
            )
            visible = {row["id"] for row in rows}
            needs_media = requests_media(event.text)
            refers_back = bool(
                re.search(
                    r"再看|刚才|这张|那张|图里|图中|图上|第.{1,3}张|最后一张",
                    event.text,
                )
            )
            choosing = state.get("choice") and bool(
                re.fullmatch(
                    r"\s*(?:全部|整组|所有|都看|都要|一起|第?\s*[一二三四五六七八九十\d]+\s*张?|最后一张)[。！!]?\s*",
                    event.text,
                )
            )
            if choosing:
                needs_media = True
                refers_back = True
            if state["request_raw_id"] in visible and (
                (state["pending"] and (event.media or needs_media))
                or (
                    needs_media
                    and refers_back
                    and any(raw_id in visible for raw_id in state["media_ids"])
                )
            ):
                event.related_raw_ids = tuple(row["id"] for row in rows)
                event.media_parent_id = state["request_raw_id"]
                event.media_followup = True
                if not event.image_search and (
                    choosing
                    or (state["pending"] and not without_media_labels(event.text))
                ):
                    event.image_search = state.get("image_search", "")
                logger.info(
                    "Media followup linked: request_id={} parent_raw_id={} sources={}",
                    event.request_id,
                    event.media_parent_id,
                    event.related_raw_ids,
                )
        if (
            event.direct
            and (requests_media(event.text) or not event.text.strip())
            and not event.media_followup
        ):
            await self.remember(event, context_id, [], pending=True)

    async def remember(self, event, context_id, media_ids, *, pending, choice=False):
        runtime = self.db.store.runtime
        key = self.key(event)
        state = await self.db.run(runtime.get, key, {})
        if state and state["request_raw_id"] > event.raw_id:
            return
        await self.db.run(
            runtime.put,
            key,
            {
                "context_id": context_id,
                "epoch": await self.db.run(self.db.store.memory.epoch),
                "request_raw_id": event.raw_id,
                "media_ids": sorted(set(media_ids)),
                "pending": pending,
                "choice": choice,
                "image_search": event.image_search,
                "until": (
                    event.now
                    + timedelta(seconds=self.config.group_behavior.followup_seconds)
                ).isoformat(),
            },
        )
