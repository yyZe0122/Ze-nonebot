import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.plugins.persona_agent.config import AgentConfig, load_config
from src.plugins.persona_agent.model import Completion
from src.plugins.persona_agent.policy import advance_group_state
from src.plugins.persona_agent.service import PersonaService
from src.plugins.persona_agent.store import Store
from src.plugins.persona_agent.vision import materialize_images, render_segments


class FakeModel:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def complete(self, messages, max_tokens):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return Completion(reply, 12)


def config(path: Path) -> AgentConfig:
    # Synthetic fixtures never load a user's runtime configuration.
    loaded = AgentConfig()
    loaded.persona.aliases = ["DemoBot"]
    loaded.database_path = path
    loaded.access.owner_ids = [1]
    loaded.access.allowed_groups = [10]
    loaded.access.allowed_private_users = [1, 2]
    loaded.persona.interests = ["吃饭", "阅读", "编程"]
    return loaded


def service(path: Path, replies: list[str]) -> PersonaService:
    loaded = config(path)
    return PersonaService(loaded, Store(path), FakeModel(replies))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_disallowed_message_is_audited_without_model(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["不应调用"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    result = await agent.handle(scope="group", user_id=9, group_id=99, message_id=1, text="你好", to_me=True, now=now)
    assert result.reply is None
    assert result.decision == "access_denied"
    assert agent.model.calls == 0
    row = agent.store._conn.execute("SELECT text, allowed FROM audit_log").fetchone()
    assert row["text"] == "你好"
    assert row["allowed"] == 0


@pytest.mark.asyncio
async def test_private_memory_is_shared_but_not_disclosed_in_group(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["我知道你的私聊秘密abc123", "今天随便聊聊"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    await agent.handle(
        scope="private",
        user_id=2,
        group_id=None,
        message_id=2,
        text="我喜欢私聊秘密abc123",
        to_me=True,
        now=now,
    )
    await agent.handle(
        scope="private",
        user_id=2,
        group_id=None,
        message_id=20,
        text="我喜欢私聊秘密abc123",
        to_me=True,
        now=now + timedelta(days=1),
    )
    result = await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=3,
        text="机器人你怎么看",
        to_me=True,
        now=now + timedelta(days=1, seconds=1),
    )
    assert result.reply == "今天随便聊聊"
    memories = agent.store.recall(user_id=2, group_id=10, disclosure_scope="group", limit=10, now=now)
    assert all(item.source_scope != "private" for item in memories)
    private = agent.store.recall(user_id=2, group_id=None, disclosure_scope="private", limit=10, now=now)
    assert any("abc123" in item.text for item in private)


@pytest.mark.asyncio
async def test_group_joke_expires_and_does_not_become_user_fact(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["哈哈"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=4,
        text="我是世界首富，开玩笑的",
        to_me=False,
        now=now,
    )
    facts = agent.store._conn.execute("SELECT text FROM memories WHERE layer = 'fact'").fetchall()
    assert facts == []
    later = now + timedelta(hours=25)
    visible = agent.store.recall(user_id=2, group_id=10, disclosure_scope="group", limit=10, now=later)
    assert visible == []


@pytest.mark.asyncio
async def test_group_claim_is_stored_once_without_event_summary(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["好"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=5,
        text="其实我喜欢喝茶",
        to_me=False,
        now=now,
    )
    facts = agent.store._conn.execute(
        "SELECT text, status FROM memories WHERE layer = 'fact'"
    ).fetchall()
    summaries = agent.store._conn.execute(
        "SELECT id FROM memories WHERE layer = 'summary'"
    ).fetchall()
    assert [row["text"] for row in facts] == ["喜欢|喝茶"]
    assert facts[0]["status"] == "active"
    assert summaries == []


@pytest.mark.asyncio
async def test_quiet_group_message_is_recorded_without_reply(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["不应回复"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    agent.model.replies = ["[SILENCE]"]
    result = await agent.handle(scope="group", user_id=2, group_id=10, message_id=6, text="哈哈", to_me=False, now=now)
    assert result.reply is None
    assert result.decision == "low_value"
    assert agent.model.calls == 0
    assert agent.store.recent_messages("group:10", 5)


@pytest.mark.asyncio
async def test_repeated_claim_keeps_confidence_and_limits_rate(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["收到", "收到"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    await agent.handle(scope="private", user_id=2, group_id=None, message_id=7, text="我喜欢简洁风格", to_me=True, now=now)
    await agent.handle(
        scope="private",
        user_id=2,
        group_id=None,
        message_id=8,
        text="我喜欢简洁风格",
        to_me=True,
        now=now + timedelta(days=1),
    )
    limited = await agent.handle(
        scope="private",
        user_id=2,
        group_id=None,
        message_id=9,
        text="再说一次",
        to_me=True,
        now=now + timedelta(days=1, seconds=1),
    )
    row = agent.store._conn.execute("SELECT confidence, evidence_count FROM memories WHERE layer = 'fact'").fetchone()
    assert row["confidence"] == pytest.approx(0.75)
    assert row["evidence_count"] == 2
    assert limited.decision == "private_rate_limit"


@pytest.mark.asyncio
async def test_episode_retrieval_fact_replacement_and_model_failure(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["收到"])

    async def fail(messages, max_tokens):
        raise RuntimeError("offline")

    agent.model.complete = fail  # type: ignore[method-assign]
    now = datetime(2026, 10, 21, tzinfo=UTC)
    for index in range(8):
        await agent.handle(
            scope="private",
            user_id=1,
            group_id=None,
            message_id=30 + index,
            text=f"我在讨论项目主题{index}",
            to_me=True,
            now=now + timedelta(seconds=index * 3),
        )
    episodes = agent.store.recent_episodes("private:1", 1, now + timedelta(seconds=30))
    assert episodes and "项目主题" in episodes[0]
    assert agent.store.search_raw("private:1", "项目主题", 2)

    later = now + timedelta(seconds=24)
    await agent.handle(scope="private", user_id=1, group_id=None, message_id=41, text="我喜欢茶", to_me=True, now=later)
    await agent.handle(
        scope="private",
        user_id=1,
        group_id=None,
        message_id=42,
        text="我喜欢咖啡",
        to_me=True,
        now=later + timedelta(seconds=3),
    )
    active = {
        row["text"]
        for row in agent.store._conn.execute(
            "SELECT text FROM memories WHERE layer = 'fact' AND status = 'active'"
        ).fetchall()
    }
    assert any(text.endswith("|咖啡") for text in active)
    assert all(not text.endswith("|茶") for text in active)
    failed = await agent.handle(
        scope="private",
        user_id=1,
        group_id=None,
        message_id=40,
        text="继续",
        to_me=True,
        now=later + timedelta(seconds=6),
    )
    assert failed.decision == "reply_unavailable"


def test_message_segments_keep_text_and_image_sources() -> None:
    incoming = render_segments(
        [
            ("text", {"text": "看这个"}),
            ("face", {"id": "178"}),
            ("mface", {"summary": "柴犬点赞", "url": "https://img.example/meme.gif"}),
            ("image", {"url": "https://img.example/cat.jpg", "file": "cat.jpg"}),
        ]
    )
    assert incoming.text == "看这个[表情:178][表情包:柴犬点赞][图片]"
    assert incoming.images == ("https://img.example/meme.gif", "https://img.example/cat.jpg")


@pytest.mark.asyncio
async def test_current_turn_sends_image_without_storing_it(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ['{"delay_seconds": 0, "text": "这猫挺悠闲。"}'])
    seen: list[object] = []
    original = agent.model.complete

    async def capture(messages, max_tokens):
        seen.append(messages[-1]["content"])
        return await original(messages, max_tokens)

    agent.model.complete = capture  # type: ignore[method-assign]
    payload = base64.b64encode(b"jpeg").decode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jpeg", headers={"content-type": "image/jpeg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        images = await materialize_images(
            ("https://img.example/cat.jpg",),
            config=agent.config.model,
            client=client,
        )
    assert images[0].url == f"data:image/jpeg;base64,{payload}"

    async def ready(_sources):
        return images

    agent._images = ready  # type: ignore[method-assign]
    now = datetime(2026, 10, 21, tzinfo=UTC)
    result = await agent.handle(
        scope="private",
        user_id=1,
        group_id=None,
        message_id=50,
        text="看这个[图片]",
        to_me=True,
        now=now,
        images=("https://img.example/cat.jpg",),
    )
    assert result.reply == "这猫挺悠闲。"
    assert isinstance(seen[0], list)
    assert seen[0][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    stored = agent.store.recent_messages("private:1", 2)
    assert all("base64" not in str(row["text"]) for row in stored)


@pytest.mark.asyncio
async def test_group_cooldown_blocks_unsolicited_reply(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["第一句回复", "接着聊"])
    agent.config.group_behavior.cooldown_seconds = 60
    now = datetime(2026, 10, 21, tzinfo=UTC)

    # First turn: direct mention triggers reply
    res1 = await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=60,
        text="机器人你怎么看",
        to_me=True,
        now=now,
    )
    assert res1.reply == "第一句回复"

    # Ten seconds later only the same person gets one follow-up.
    res2 = await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=61,
        text="今天天气真好",
        to_me=False,
        now=now + timedelta(seconds=10),
    )
    assert res2.reply == "接着聊"
    assert res2.decision == "replied"

    other = await agent.handle(
        scope="group",
        user_id=3,
        group_id=10,
        message_id=63,
        text="今天天气真好",
        to_me=False,
        now=now + timedelta(seconds=12),
    )
    assert other.reply is None

    # Third turn within cooldown: direct @ still replies!
    agent.model.replies = ["收到"]
    res3 = await agent.handle(
        scope="group",
        user_id=2,
        group_id=10,
        message_id=62,
        text="机器人你还在吗",
        to_me=True,
        now=now + timedelta(seconds=20),
    )
    assert res3.reply == "收到"
    assert res3.decision == "replied"


def _job(key: str) -> dict:
    return {"media": {"key": key, "source": "https://img.example/a.jpg", "kind": "image", "data": {}}, "user_id": 1, "raw_id": 1}


def _insert_legacy_job(path: Path, key: str, *, status: str, attempts: int, next_at: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO media_jobs(source_key,payload,refs,status,attempts,next_at) VALUES (?,?,?,?,?,?)",
        (key, "{}", "[]", status, attempts, next_at),
    )
    connection.commit()
    connection.close()


def test_media_failures_use_bounded_backoff(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    store = Store(path)
    store.media.queue_job(_job("broken"), 1, 0)
    before = datetime.now(UTC)
    store.media.finish_job("broken", "image_unavailable")
    row = store._conn.execute(
        "SELECT status,attempts,last_error FROM media_jobs WHERE source_key='broken'"
    ).fetchone()
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 1, "image_unavailable")

    store.media.queue_job(_job("upstream"), 2, 0)
    delays = (timedelta(minutes=10), timedelta(hours=1), timedelta(hours=6), timedelta(days=1))
    for attempt, delay in enumerate(delays, start=1):
        started = datetime.now(UTC)
        store.media.finish_job("upstream", "media_model_failed:http_error:400")
        row = store._conn.execute(
            "SELECT status,attempts,next_at FROM media_jobs WHERE source_key='upstream'"
        ).fetchone()
        scheduled = datetime.fromisoformat(row["next_at"])
        assert (row["status"], row["attempts"]) == ("pending", attempt)
        assert started + delay <= scheduled <= datetime.now(UTC) + delay

    store.media.finish_job("upstream", "media_model_failed:request_timeout")
    row = store._conn.execute(
        "SELECT status,attempts,next_at FROM media_jobs WHERE source_key='upstream'"
    ).fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 5)
    assert datetime.fromisoformat(row["next_at"]) >= before + timedelta(days=1)


def test_media_job_restart_preserves_backoff(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    first = Store(path)
    future = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    first._conn.execute(
        "INSERT INTO media_jobs(source_key,payload,refs,status,attempts,next_at,last_error) VALUES (?,?,?,?,?,?,?)",
        ("later", "{}", "[]", "queued", 3, future, "request_timeout"),
    )
    first._conn.execute(
        "INSERT INTO media_jobs(source_key,payload,refs,status,attempts,next_at,last_error) VALUES (?,?,?,?,?,?,?)",
        ("interrupted", "{}", "[]", "queued", 1, past, "connection_error"),
    )
    first._conn.commit()
    first._conn.close()

    restarted = Store(path)
    later = restarted._conn.execute("SELECT status,next_at FROM media_jobs WHERE source_key='later'").fetchone()
    interrupted = restarted._conn.execute(
        "SELECT status,next_at FROM media_jobs WHERE source_key='interrupted'"
    ).fetchone()
    assert (later["status"], later["next_at"]) == ("pending", future)
    assert interrupted["status"] == "pending"
    resumed = datetime.fromisoformat(interrupted["next_at"])
    assert datetime.now(UTC) < resumed <= datetime.now(UTC) + timedelta(minutes=1, seconds=5)


def test_historical_media_failures_reenter_backoff(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """CREATE TABLE media_jobs (
             source_key TEXT PRIMARY KEY,payload TEXT NOT NULL,refs TEXT NOT NULL,
             status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
             next_at TEXT NOT NULL
           );"""
    )
    legacy.commit()
    legacy.close()
    now = datetime.now(UTC)
    _insert_legacy_job(path, "newer", status="failed", attempts=3, next_at=(now + timedelta(minutes=1)).isoformat())
    _insert_legacy_job(path, "older", status="failed", attempts=3, next_at=now.isoformat())
    started = datetime.now(UTC)
    reopened = Store(path)
    rows = {
        row["source_key"]: row
        for row in reopened._conn.execute(
            "SELECT source_key,status,attempts,next_at FROM media_jobs ORDER BY next_at"
        )
    }
    older = datetime.fromisoformat(rows["older"]["next_at"])
    newer = datetime.fromisoformat(rows["newer"]["next_at"])
    assert (rows["older"]["status"], rows["older"]["attempts"]) == ("pending", 3)
    assert started + timedelta(hours=6) <= older <= datetime.now(UTC) + timedelta(hours=6)
    assert older + timedelta(seconds=59) <= newer <= older + timedelta(seconds=61)


def test_build_messages_tags_group_context(tmp_path: Path) -> None:
    from src.plugins.persona_agent.prompt import build_messages

    cfg = config(tmp_path / "memory.sqlite3")
    msg_addressed = build_messages(
        cfg,
        scope="group",
        persona_budget=100,
        memories=[],
        episodes=[],
        recalled=[],
        recent=[],
        user_text="你好",
        to_me=True,
    )
    assert "【对方正在跟你说话】你好" in str(msg_addressed[-1]["content"])

    msg_unaddressed = build_messages(
        cfg,
        scope="group",
        persona_budget=100,
        memories=[],
        episodes=[],
        recalled=[],
        recent=[],
        user_text="吃啥",
        to_me=False,
    )
    assert "【群友闲聊，未@你】吃啥" in str(msg_unaddressed[-1]["content"])

    owner = build_messages(
        cfg,
        scope="private",
        persona_budget=400,
        memories=[],
        episodes=[],
        recalled=[],
        recent=[],
        user_text="在干嘛",
        user_id=cfg.access.owner_ids[0],
    )
    assert "围绕管理员的具体请求回应" in str(owner[0]["content"])
    assert "不预设私人关系" in str(owner[0]["content"])
    assert "已定义的权限" in str(owner[0]["content"])

    stranger = build_messages(
        cfg,
        scope="private",
        persona_budget=400,
        memories=[],
        episodes=[],
        recalled=[],
        recent=[],
        user_text="在干嘛",
        user_id=2,
    )
    assert "围绕对方的具体问题或表达回应" in str(stranger[0]["content"])
    assert "不编造共同经历或关系" in str(stranger[0]["content"])
    recent = build_messages(
        cfg,
        scope="group",
        persona_budget=400,
        memories=[],
        episodes=[],
        recalled=[],
        recent=[("机器人", "旧回复一"), ("群友", "又来"), ("机器人", "旧回复二"), ("机器人", "旧回复三")],
        user_text="在吗",
        to_me=True,
    )
    content = str(recent[0]["content"])
    assert content.count("旧回复") == 2
    assert "旧回复一" not in content


def test_owner_identity_reaches_group_and_private_prompts(tmp_path: Path) -> None:
    from src.plugins.persona_agent.conversation import ChatEvent
    from src.plugins.persona_agent.prompt import build_messages

    cfg = config(tmp_path / "owner-memory.sqlite3")
    cfg.persona.personality = "自然、耐心，尊重对话边界。"
    cfg.persona.group.instructions = "这是群聊，管理员消息遵循群聊规则。"
    cfg.persona.private.instructions = "这是普通私聊，尊重对话边界。"
    cfg.persona.owner_private.instructions = "管理员的请求仍遵循安全规则和已定义的权限。"
    now = datetime(2026, 10, 27, 6, tzinfo=UTC)
    owner_id = cfg.access.owner_ids[0]
    focus = ChatEvent(
        scope="group",
        bot_id="bot",
        user_id=owner_id,
        group_id=10,
        message_id=8,
        text="过来",
        to_me=True,
        now=now,
        raw_id=2,
    )
    recent = [
        {
            "id": 1,
            "bot_id": "bot",
            "user_id": 2,
            "sender_name": "路人",
            "sender": "路人",
            "message_id": 7,
            "created_at": now.isoformat(),
            "reply_to_id": None,
            "reply_sender": "",
            "reply_text": "",
            "text": "普通闲聊",
            "media_json": "[]",
        },
        {
            "id": 2,
            "bot_id": "bot",
            "user_id": owner_id,
            "sender_name": "管理员昵称",
            "sender": "管理员昵称",
            "message_id": 8,
            "created_at": now.isoformat(),
            "reply_to_id": None,
            "reply_sender": "",
            "reply_text": "",
            "text": "过来",
            "media_json": "[]",
        },
    ]
    group = build_messages(
        cfg,
        scope="group",
        persona_budget=400,
        memories=[],
        episodes=[],
        recalled=[],
        recent=recent,
        focus=focus,
        required_ids={2},
        current_ids={2},
        now=now,
        user_id=owner_id,
        images={},
    )
    group_text = "\n".join(str(item["content"]) for item in group)
    assert f"管理员 {owner_id}" in group_text
    assert "用户 2" in group_text
    assert "管理员消息遵循群聊规则" in group_text

    private_focus = ChatEvent(
        scope="private",
        bot_id="bot",
        user_id=owner_id,
        group_id=None,
        message_id=8,
        text="过来",
        to_me=True,
        now=now,
        raw_id=2,
    )
    private = build_messages(
        cfg,
        scope="private",
        persona_budget=400,
        memories=[],
        episodes=[],
        recalled=[],
        recent=recent[1:],
        focus=private_focus,
        required_ids={2},
        current_ids={2},
        now=now,
        user_id=owner_id,
        images={},
    )
    private_text = "\n".join(str(item["content"]) for item in private)
    assert "当前私聊对象是由程序确认的管理员" in private_text
    assert "管理员的请求仍遵循安全规则" in private_text
    assert "管理员的请求仍遵循安全规则和已定义的权限" in private_text


def _speak(agent: PersonaService, group_id: int, *items: tuple[int, datetime]) -> None:
    state: dict = {
        "last_spoke_at": None,
        "consecutive_replies": 0,
        "cooldown_until": None,
        "active_thread_until": None,
        "thread_replies": 0,
        "followup_user_id": None,
        "recent_speakers": [],
    }
    for user_id, moment in items:
        state = advance_group_state(
            state,
            user_id=user_id,
            replied=False,
            proactive=False,
            now=moment,
            config=agent.config,
        )
    agent.store.save_group_state(group_id, state)


@pytest.mark.asyncio
async def test_group_gates_follow_interest_and_active_thread(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["我更正一下", "吃饭了", "在阅读", "偶尔冒泡", "再接一句"])
    now = datetime(2026, 10, 21, tzinfo=UTC)
    spoke = now - timedelta(seconds=120)
    agent.store.save_group_state(10, {
        "last_spoke_at": spoke.isoformat(),
        "consecutive_replies": 0,
        "cooldown_until": None,
        "active_thread_until": None,
        "thread_replies": 0,
        "followup_user_id": None,
        "recent_speakers": [{"user_id": 0, "at": spoke.isoformat(), "bot": True}],
    })

    chatter = await agent.handle(
        scope="group", user_id=3, group_id=10, message_id=70,
        text="哈哈", to_me=False, now=now + timedelta(seconds=20),
    )
    assert chatter.decision == "low_value"
    assert agent.model.calls == 0

    food = await agent.handle(
        scope="group", user_id=3, group_id=10, message_id=71,
        text="中午吃啥", to_me=False, now=now + timedelta(seconds=21),
    )
    assert food.decision == "human_gap"

    sensitive = await agent.handle(
        scope="group", user_id=4, group_id=10, message_id=72,
        text="你刚才说错了", to_me=False, now=now + timedelta(seconds=80),
    )
    assert sensitive.reply == "我更正一下"
    assert agent.store.group_state(10)["active_thread_until"] is None

    repeat = await agent.handle(
        scope="group", user_id=4, group_id=10, message_id=76,
        text="DemoBot", to_me=False, now=now + timedelta(seconds=90),
    )
    assert repeat.reply == "吃饭了"
    echoed = await agent.handle(
        scope="group", user_id=4, group_id=10, message_id=77,
        text="DemoBot", to_me=False, now=now + timedelta(seconds=93),
    )
    assert echoed.decision == "repeat_address"

    mentioned = await agent.handle(
        scope="group", user_id=4, group_id=10, message_id=73,
        text="机器人出来", to_me=False, now=now + timedelta(seconds=160),
    )
    assert mentioned.reply == "在阅读"
    opened = datetime.fromisoformat(agent.store.group_state(10)["active_thread_until"])
    assert opened == now + timedelta(seconds=160 + 90)
    assert agent.store.group_state(10)["followup_user_id"] == 4

    outsider = await agent.handle(
        scope="group", user_id=5, group_id=10, message_id=74,
        text="今天天气真好", to_me=False, now=now + timedelta(seconds=170),
    )
    assert outsider.reply is None

    follow = await agent.handle(
        scope="group", user_id=4, group_id=10, message_id=75,
        text="今天天气真好", to_me=False, now=now + timedelta(seconds=175),
    )
    assert follow.reply == "偶尔冒泡"
    extended = datetime.fromisoformat(agent.store.group_state(10)["active_thread_until"])
    assert extended == now + timedelta(seconds=175 + 90)
    assert agent.store.group_state(10)["followup_user_id"] == 4


def test_legacy_model_config_compatibility() -> None:
    legacy_data = {
        "model": {
            "base_url": "https://api.legacy.com/v1",
            "model": "legacy-model",
        }
    }
    cfg = AgentConfig.model_validate(legacy_data)
    assert cfg.active_model == "default"
    assert "default" in cfg.models
    assert cfg.model.model == "legacy-model"
    assert cfg.model.base_url == "https://api.legacy.com/v1"


def test_load_config_with_saved_state(tmp_path: Path) -> None:
    yaml_file = tmp_path / "persona_agent.yaml"
    yaml_file.write_text(
        """
database_path: data/test.sqlite3
active_model: primary
models:
  primary:
    base_url: "https://primary.example.com/v1"
    model: "model-primary"
  secondary:
    base_url: "https://secondary.example.com/v1"
    model: "model-secondary"
""",
        encoding="utf-8",
    )
    # No saved file yet -> defaults to primary
    cfg = load_config(yaml_file)
    assert cfg.active_model == "primary"
    assert cfg.model.model == "model-primary"

    # Simulate saved state
    state_file = tmp_path / "data" / "active_model.txt"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("secondary", encoding="utf-8")

    cfg_saved = load_config(yaml_file)
    assert cfg_saved.active_model == "secondary"
    assert cfg_saved.model.model == "model-secondary"


@pytest.mark.asyncio
async def test_model_switching_command_and_safety(tmp_path: Path) -> None:
    db_file = tmp_path / "memory.sqlite3"
    loaded = config(db_file)
    # Ensure models dict is populated
    loaded.models = {
        "primary": loaded.model,
        "secondary": loaded.model.model_copy(update={"model": "model-secondary"}),
    }
    loaded.active_model = "primary"

    created_models: list[str] = []

    def mock_factory(cfg):
        created_models.append(cfg.model)
        return FakeModel([f"from-{cfg.model}"])

    agent = PersonaService(loaded, Store(db_file), FakeModel(["initial"]), model_factory=mock_factory)
    now = datetime(2026, 10, 21, tzinfo=UTC)

    # 1. Owner queries /model in private
    res_list = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=101, text="/model", to_me=True, now=now
    )
    assert res_list.decision == "model_cmd_success"
    assert res_list.reply is not None
    assert "primary" in res_list.reply
    assert "model-secondary" in res_list.reply
    assert "当前生效: primary" in res_list.reply

    # 2. Owner switches to secondary in private
    res_switch = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=102, text="/model secondary", to_me=True, now=now
    )
    assert res_switch.decision == "model_cmd_success"
    assert "已切换至模型「secondary」" in str(res_switch.reply)
    assert agent.config.active_model == "secondary"
    assert agent.config.model.model == "model-secondary"
    assert created_models == ["model-secondary"]
    assert (tmp_path / "active_model.txt").read_text(encoding="utf-8") == "secondary"

    # 3. Owner switches to invalid model
    res_invalid = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=103, text="/model non_existent", to_me=True, now=now
    )
    assert res_invalid.decision == "model_cmd_success"
    assert "未知模型" in str(res_invalid.reply)
    assert agent.config.active_model == "secondary"

    # 4. Owner tries /model in group -> rejected with hint
    res_group_owner = await agent.handle(
        scope="group", user_id=1, group_id=10, message_id=104, text="/model", to_me=True, now=now
    )
    assert res_group_owner.decision == "model_cmd_private_only"
    assert "私聊" in str(res_group_owner.reply)

    # 5. Non-owner tries /model in private -> forbidden
    res_non_owner_private = await agent.handle(
        scope="private", user_id=2, group_id=None, message_id=105, text="/model", to_me=True, now=now
    )
    assert res_non_owner_private.decision == "model_cmd_forbidden"
    assert res_non_owner_private.reply is None

    # 6. Non-owner tries /model in group -> forbidden
    res_non_owner_group = await agent.handle(
        scope="group", user_id=2, group_id=10, message_id=106, text="/model secondary", to_me=True, now=now
    )
    assert res_non_owner_group.decision == "model_cmd_forbidden"
    assert res_non_owner_group.reply is None


def test_group_conversation_date_boundary() -> None:
    from zoneinfo import ZoneInfo
    from src.plugins.persona_agent.policy import group_conversation_date

    cst = ZoneInfo("Asia/Shanghai")
    # 03:59:59 CST belongs to previous calendar day
    t_before_4am = datetime(2026, 10, 21, 3, 59, 59, tzinfo=cst)
    assert group_conversation_date(t_before_4am) == "20261020"

    # 04:00:00 CST belongs to current calendar day
    t_at_4am = datetime(2026, 10, 21, 4, 0, 0, tzinfo=cst)
    assert group_conversation_date(t_at_4am) == "20261021"

    # 23:59:59 CST belongs to current calendar day
    t_late = datetime(2026, 10, 21, 23, 59, 59, tzinfo=cst)
    assert group_conversation_date(t_late) == "20261021"

    # Corresponding UTC: 2026-10-20 19:59:59 UTC == 2026-10-21 03:59:59 CST
    t_utc_before = datetime(2026, 10, 20, 19, 59, 59, tzinfo=UTC)
    assert group_conversation_date(t_utc_before) == "20261020"

    # Corresponding UTC: 2026-10-20 20:00:00 UTC == 2026-10-21 04:00:00 CST
    t_utc_at = datetime(2026, 10, 20, 20, 0, 0, tzinfo=UTC)
    assert group_conversation_date(t_utc_at) == "20261021"


def test_name_mention_matches_anywhere_with_word_boundaries() -> None:
    from src.plugins.persona_agent.policy import mentioned

    assert mentioned("今天 DemoBot 在吗", ["DemoBot"])
    assert mentioned("有没有人叫demobot", ["DemoBot"])
    assert mentioned("机器人出来", ["机器人"])
    assert not mentioned("demobotpro 发布了", ["DemoBot"])
    assert not mentioned("mydemobot", ["DemoBot"])


@pytest.mark.asyncio
async def test_group_daily_reset_and_state(tmp_path: Path) -> None:
    from zoneinfo import ZoneInfo
    cst = ZoneInfo("Asia/Shanghai")
    agent = service(tmp_path / "memory.sqlite3", ["前一天回复", "新一天回复"])

    # 1. Day 1 at 03:30 CST (segment 20261020)
    t1 = datetime(2026, 10, 21, 3, 30, 0, tzinfo=cst)
    conv1 = agent.conversation_id("group", 2, 10, t1)
    assert conv1 == "group:10:20261020"

    res1 = await agent.handle(
        scope="group", user_id=2, group_id=10, message_id=201, text="机器人你好", to_me=True, now=t1
    )
    assert res1.reply == "前一天回复"

    # Verify group state has active follow-up
    st1 = agent.store.group_state(10)
    assert st1["followup_user_id"] == 2
    assert st1["active_thread_until"] is not None

    # 2. Cross 4:00 AM CST -> 04:15 CST (segment 20261021)
    t2 = datetime(2026, 10, 21, 4, 15, 0, tzinfo=cst)
    conv2 = agent.conversation_id("group", 2, 10, t2)
    assert conv2 == "group:10:20261021"

    # Check that crossing the 4am boundary cleared yesterday's thread follow-up
    st2 = agent.store.group_state(10)
    assert st2["followup_user_id"] is None
    assert st2["active_thread_until"] is None

    res2 = await agent.handle(
        scope="group", user_id=2, group_id=10, message_id=202, text="新一天了", to_me=True, now=t2
    )
    assert res2.reply == "新一天回复"

    # Context in conv2 has only the new day's message
    recent_day2 = agent.store.recent_messages(conv2, 10)
    assert len(recent_day2) == 2  # user message + bot reply
    assert any("新一天了" in str(r["text"]) for r in recent_day2)
    assert all("机器人你好" not in str(r["text"]) for r in recent_day2)


@pytest.mark.asyncio
async def test_private_new_command_resets_context_and_preserves_facts(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["初始回复", "新会话回复"])
    now = datetime(2026, 10, 21, 10, 0, 0, tzinfo=UTC)

    # 1. First private conversation turn
    res1 = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=301, text="我喜欢看动漫", to_me=True, now=now
    )
    assert res1.reply == "初始回复"
    initial_conv = agent.conversation_id("private", 1, None, now)
    assert initial_conv == "private:1"

    # User fact is stored
    facts = agent.store.find_user_facts(1, "喜欢")
    assert any("喜欢|看动漫" in str(f["text"]) for f in facts)

    # 2. User sends /new
    res_new = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=302, text="/new", to_me=True, now=now + timedelta(seconds=10)
    )
    assert res_new.decision == "session_reset"
    assert res_new.reply == "已重置对话上下文，开启新会话。"

    # Active conversation ID changed
    new_conv = agent.conversation_id("private", 1, None, now + timedelta(seconds=11))
    assert new_conv == "private:1:1"

    # 3. Next message goes to new conversation
    res2 = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=303, text="这是新话题", to_me=True, now=now + timedelta(seconds=15)
    )
    assert res2.reply == "新会话回复"

    # New conversation context contains only the new topic
    recent_new = agent.store.recent_messages(new_conv, 10)
    assert len(recent_new) == 2  # user message + bot reply
    assert any("这是新话题" in str(r["text"]) for r in recent_new)
    assert all("我喜欢看动漫" not in str(r["text"]) for r in recent_new)
    assert all("/new" not in str(r["text"]) for r in recent_new)

    # Long-term memory / facts are preserved!
    facts_after = agent.store.find_user_facts(1, "喜欢")
    assert any("喜欢|看动漫" in str(f["text"]) for f in facts_after)

    # 4. A second /new switches to session 2
    res_new2 = await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=304, text="/new", to_me=True, now=now + timedelta(seconds=20)
    )
    assert res_new2.decision == "session_reset"
    second_new_conv = agent.conversation_id("private", 1, None, now + timedelta(seconds=21))
    assert second_new_conv == "private:1:2"


@pytest.mark.asyncio
async def test_new_command_in_group(tmp_path: Path) -> None:
    agent = service(tmp_path / "memory.sqlite3", ["回复"])
    now = datetime(2026, 10, 21, 10, 0, 0, tzinfo=UTC)

    # Addressed to bot in group: explains group reset rule
    res_addressed = await agent.handle(
        scope="group", user_id=2, group_id=10, message_id=401, text="/new", to_me=True, now=now
    )
    assert res_addressed.decision == "group_new_forbidden"
    assert "每天 04:00 自动重置" in str(res_addressed.reply)

    # Unaddressed in group: ignored silently
    res_unaddressed = await agent.handle(
        scope="group", user_id=2, group_id=10, message_id=402, text="/new", to_me=False, now=now
    )
    assert res_unaddressed.decision == "group_new_ignored"
    assert res_unaddressed.reply is None


@pytest.mark.asyncio
async def test_old_sessions_and_files_purged_after_24_hours(tmp_path: Path) -> None:
    from zoneinfo import ZoneInfo
    cst = ZoneInfo("Asia/Shanghai")
    agent = service(tmp_path / "memory.sqlite3", ["回复"])

    # 1. Create a closed private session at t0
    t0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=UTC)
    await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=501, text="旧话题消息", to_me=True, now=t0
    )
    assert agent.store.recent_messages("private:1", 5)

    # Reset private session at t0 + 1s -> old session "private:1" is closed at t0 + 1s
    await agent.handle(
        scope="private", user_id=1, group_id=None, message_id=502, text="/new", to_me=True, now=t0 + timedelta(seconds=1)
    )

    # 2. Create an old group session from 2 days ago (20261019)
    t_old_group = datetime(2026, 10, 19, 12, 0, 0, tzinfo=cst)
    agent.store.add_raw(
        scope="group",
        conversation_id="group:10:20261019",
        user_id=2,
        group_id=10,
        sender="2",
        text="两天前的群消息",
        retention=timedelta(days=14),
        now=t_old_group,
    )
    assert agent.store.recent_messages("group:10:20261019", 5)

    # At t0 + 10 hours: purge should NOT delete closed private session yet (only 10h < 24h)
    agent.store.purge(t0 + timedelta(hours=10))
    assert agent.store.recent_messages("private:1", 5)  # still present

    # At t0 + 26 hours:
    # - Closed private session is > 24 hours old -> purged!
    # - Group 20261019 segment ended on 2026-10-20 04:00 CST; 24h later is 2026-10-21 04:00 CST.
    #   At t0 + 26h (which is 2026-10-22 12:00 UTC == 20:00 CST), it's way past 24h -> purged!
    agent.store.purge(t0 + timedelta(hours=26))

    assert agent.store.recent_messages("private:1", 5) == []
    assert agent.store.recent_messages("group:10:20261019", 5) == []




