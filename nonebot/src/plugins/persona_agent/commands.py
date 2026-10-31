from __future__ import annotations

import asyncio

from nonebot.log import logger


class Commands:
    def __init__(self, service) -> None:
        self.service = service
        self.config, self.db = service.config, service.db

    async def model(self, text: str) -> str:
        parts = text.strip().split()
        if len(parts) == 1 or parts[1].lower() in {"list", "ls"}:
            lines = ["当前配置的模型："]
            for name, settings in self.config.models.items():
                mark = "★" if name == self.config.active_model else "☆"
                lines.append(f"{mark} {name}: {settings.model}")
            labels = {
                "participation": "参与判断",
                "media": "表情分析与复核",
                "memory": "记忆提取",
            }
            for task, label in labels.items():
                binding = getattr(self.config.model_tasks, task).model
                lines.append(f"{label}：{binding or '跟随聊天模型'}")
            return "\n".join(lines) + "\n切换：/model <名称>"
        target = parts[1]
        if target not in self.config.models:
            return "未知模型，可选：" + "、".join(self.config.models)

        def persist():
            from dotenv import load_dotenv

            env = self.service.config_path.parent / "persona_agent.env"
            if env.is_file():
                load_dotenv(env, override=True)
            state = self.config.database_path.parent / "active_model.txt"
            temporary = state.with_suffix(".tmp")
            temporary.write_text(target, encoding="utf-8")
            temporary.replace(state)

        try:
            await asyncio.to_thread(persist)
        except OSError:
            return "模型选择无法保存，本次未切换。"
        self.config.active_model = target
        return f"已切换至「{target}」。新请求生效；正在处理的请求继续使用原模型。"

    async def stickers(self, text: str) -> str:
        library = self.service.stickers
        if not self.config.stickers.enabled:
            return "表情功能尚未启用。"
        parts = text.strip().split()
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action in {"sync", "同步"}:
            try:
                count = await library.sync()
                return f"QQ 收藏列表已同步：{count} 个，新增表情正在建立目录。"
            except Exception as exc:
                logger.warning(
                    "Manual sticker sync failed: error={}", type(exc).__name__
                )
                return "QQ 收藏列表暂时无法同步。"
        if (
            action in {"disable", "停用", "enable", "启用"}
            and len(parts) == 3
            and parts[2].isdigit()
        ):
            enabled = action in {"enable", "启用"}
            try:
                changed = await library.set_enabled(int(parts[2]), enabled)
                return (
                    (
                        "已启用该表情。"
                        if enabled
                        else "已手动停用该表情，保留 QQ 收藏和文件。"
                    )
                    if changed
                    else "目录里没有这个表情。"
                )
            except (OSError, ValueError):
                return "无法修改状态：原图已清理、不可读取或文件校验不符。"
        if action in {"review", "复核"}:
            rows = await self.db.run(self.db.store.media.media_review_status)
            labels = {
                "pending": "处理中",
                "keep": "保留",
                "retire": "停用",
                "defer": "暂缓",
            }
            return "最近的自动复核：\n" + (
                "\n".join(
                    f"{row['asset_id']}. {labels[row['decision']]}：{row['reason']}"
                    for row in rows
                )
                or "暂无记录"
            )
        if action in {"status", "状态"}:
            state = await self.db.run(self.db.store.runtime.get, "qq-upload", {})
            return f"QQ 列表同步可用：{library.qq_available}\n原生收藏连续失败：{state.get('failures', 0)}\n本地表情目录继续可用。"
        rows = await self.db.run(self.db.store.media.favorite_media)
        return (
            "表情目录（全局共用）：\n"
            + (
                "\n".join(
                    f"{row['id']}. {row['description'][:60]} [{row['storage']}] 使用 {row['use_count']} 次"
                    for row in rows[:30]
                )
                or "暂无收藏"
            )
            + f"\n共 {len(rows)} 个可用。/表情 同步；复核；状态；停用 <ID>；启用 <ID>"
        )

    async def handle(self, event) -> None:
        service = self.service
        memory_ids = []
        epoch = None
        if event.command in {"/memory", "/记忆"}:
            async with service.context_guard:
                rows = await self.db.run(
                    self.db.store.memory.list,
                    event.bot_id,
                    event.user_id,
                    event.scope,
                    event.group_id,
                    limit=100000,
                )
                memory_ids = [row["id"] for row in rows]
                reply = await service.memory.command(event)
                epoch = await self.db.run(self.db.store.memory.epoch)
            outcome = "memory_command"
        elif event.command in {
            "/model",
            "/模型",
            "/stickers",
            "/表情",
            "/skills",
            "/技能",
        }:
            if not self.config.is_owner(event.user_id):
                return
            if event.scope != "private":
                reply = "请在私聊中使用管理命令。"
            elif event.command in {"/model", "/模型"}:
                reply = await self.model(event.text)
            elif event.command in {"/skills", "/技能"}:
                reply = service.skills.describe()
            else:
                reply = await self.stickers(event.text)
            outcome = "management_command"
        elif event.command in {"/new", "／new"}:
            if event.scope == "private":
                await self.db.run(
                    self.db.store.reset_private_session, event.user_id, now=event.now
                )
                await self.db.run(
                    self.db.store.note_private_message,
                    event.bot_id,
                    event.user_id,
                    incoming=True,
                    now=event.now,
                    next_check_at=service.initial_check_at(event.now),
                )
                reply = "已重置对话上下文，开启新会话。长期记忆保留；可用 /记忆 管理。"
                outcome = "session_reset"
            elif event.direct:
                reply, outcome = (
                    "群聊滚动保留最近上下文，/new 仅支持私聊。",
                    "group_new_forbidden",
                )
            else:
                return
        else:
            return
        await service.delivery.command(event, reply, memory_ids=memory_ids, epoch=epoch)
        await service.audit(event, outcome)
