from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import AgentConfig, MemoryKind
from .vision import is_media_only, without_media_labels

LOW_VALUE_RE = re.compile(r"^(哈{1,}|呵{1,}|6{2,}|草+|www+|hh+|lol|[.。…~～!！?？]+)$", re.I)
JOKE_RE = re.compile(r"(开玩笑|笑死|哈哈|逗你|假的|不是真)")
CLAIM_RE = re.compile(
    r"(?:^|[，。；;])\s*我(?P<key>不喜欢|讨厌|喜欢|叫|住|玩)(?P<value>[^，。！？!?；;、\s]{1,24})"
)


@dataclass(frozen=True)
class Turn:
    scope: str
    user_id: int
    group_id: int | None
    text: str
    to_me: bool
    mentioned: bool
    now: datetime
    to_other: bool = False


@dataclass(frozen=True)
class ReplyPlan:
    should_reply: bool
    reason: str
    proactive: bool = False
    tier: str = "direct"
    addressed: bool = False
    allow_silence: bool = False


def group_conversation_date(now: datetime) -> str:
    """Return China Standard Time date segment for group chat with 4:00 AM reset boundary.

    Time before 04:00:00 CST belongs to the previous calendar day's segment.
    """
    tz = ZoneInfo("Asia/Shanghai")
    if now.tzinfo is None:
        local_dt = now.replace(tzinfo=tz)
    else:
        local_dt = now.astimezone(tz)
    if (local_dt.hour, local_dt.minute, local_dt.second) < (4, 0, 0):
        seg_date = local_dt.date() - timedelta(days=1)
    else:
        seg_date = local_dt.date()
    return seg_date.strftime("%Y%m%d")


def mentioned(text: str, names: list[str]) -> bool:
    body = without_media_labels(text).strip()
    return any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", body, re.I)
               for name in names if name)


def classify_memory(text: str) -> tuple[MemoryKind, float, str | None]:
    compact = " ".join(text.split())
    if (_low_value(compact) or JOKE_RE.search(compact) or "?" in compact or "？" in compact
            or re.search(r"(?:吗|么|呢)[。！!]*$|假如|如果|要是", compact)):
        return "uncertain", 0.1, None
    match = CLAIM_RE.search(compact)
    if match:
        value = match.group("value").strip("的了啊呀吧呢哦 ")
        if value and value not in {"你", "这个", "那个", "自己", "这样", "那样", "这里", "那里"}:
            return "claim", 0.75, f"{match.group('key')}|{value}"
    return "uncertain", 0.1, None


def stable_memory(text: str) -> bool:
    key, separator, value = text.partition("|")
    if not separator:
        return False
    return classify_memory("我" + key + value)[2] == text


def summarize_episode(messages: list[tuple[str, str]]) -> str:
    useful = [(sender, text) for sender, text in messages if not _low_value(text)
              and not re.fullmatch(r"(?:\s*\[[^\]]*\])+\s*", text)]
    selected = useful[-4:]
    return "历史发言摘录（非事实总结）：" + "；".join(f"{sender}说：{text[:80]}" for sender, text in selected) if selected else ""


def search_terms(text: str) -> str:
    pieces = re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{3,}", text)
    terms = []
    for piece in pieces:
        if re.fullmatch(r"[\u4e00-\u9fff]+", piece):
            terms.extend(piece[i:i+3] for i in range(len(piece)-2))
        else:
            terms.append(piece)
    return " ".join(dict.fromkeys(terms))[:240]


def sanitize(text: str) -> str:
    text = re.sub(r"(?i)(api[_ -]?key|token|secret|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    text = re.sub(r"\b1[3-9]\d{9}\b", "[PHONE]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    return text.strip()


def discloses_private(reply: str, private_memories: list[str]) -> bool:
    normalized = _normalize(reply)
    return any(len(item) >= 6 and _normalize(item) in normalized for item in private_memories)


def _low_value(text: str) -> bool:
    return bool(LOW_VALUE_RE.match(text.strip()))


def _normalize(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def advance_group_state(
    state: dict,
    *,
    user_id: int,
    replied: bool,
    proactive: bool,
    now: datetime,
    config: AgentConfig,
    addressed: bool = False,
    continued: bool = False,
) -> dict:
    behavior = config.group_behavior
    speakers = [
        item
        for item in state.get("recent_speakers", [])
        if now - datetime.fromisoformat(item["at"]) < timedelta(seconds=behavior.recent_window_seconds)
    ]
    if not replied:
        speakers.append({"user_id": user_id, "at": now.isoformat(), "bot": False})
    updated = {
        **state,
        "recent_speakers": speakers[-30:],
    }
    if not replied:
        return updated
    updated["recent_speakers"].append({"user_id": 0, "at": now.isoformat(), "bot": True})
    updated["last_spoke_at"] = now.isoformat()
    updated["active_thread_until"] = (now + timedelta(seconds=behavior.followup_seconds)).isoformat()
    updated["followup_user_id"] = user_id
    return updated


def human_count(state: dict, now: datetime, window_seconds: int) -> int:
    return sum(1 for item in _window(state, now, window_seconds) if not item.get("bot"))


def humans_since_last_speak(state: dict) -> int:
    count = 0
    for item in reversed(state.get("recent_speakers", [])):
        if item.get("bot"):
            break
        count += 1
    return count


def bot_share(state: dict, now: datetime, window_seconds: int) -> float:
    items = _window(state, now, window_seconds)
    humans = sum(1 for item in items if not item.get("bot"))
    if humans < 4:
        return 0.0
    return sum(1 for item in items if item.get("bot")) / len(items)


def address_body(text: str, names: list[str]) -> str:
    cleaned = text
    for name in sorted((name for name in names if name), key=len, reverse=True):
        cleaned = re.sub(re.escape(name), "", cleaned, flags=re.I)
    cleaned = re.sub(r"@\d+", "", cleaned)
    return re.sub(r"[\W_]+", "", cleaned)


def decide_group_reply(turn: Turn, state: dict, config: AgentConfig, *, allow_topic_candidate: bool = True) -> ReplyPlan:
    behavior = config.group_behavior
    now = turn.now
    addressed = turn.to_me or turn.mentioned
    names = [config.persona.name, *config.persona.aliases]
    if turn.to_other and not addressed:
        return ReplyPlan(False, "addressed_to_other", proactive=True)
    if addressed:
        last_spoke = _parse(state.get("last_spoke_at"))
        thin = not address_body(turn.text, names) and not re.search(r"[?？]", turn.text)
        if (thin and state.get("followup_user_id") == turn.user_id and last_spoke
                and (now - last_spoke).total_seconds() < behavior.repeat_address_seconds):
            return ReplyPlan(False, "repeat_address", tier="direct")
        return ReplyPlan(True, "direct", tier="direct", addressed=True)

    follow_until = _parse(state.get("active_thread_until"))
    if (allow_topic_candidate and follow_until and follow_until > now
            and state.get("followup_user_id") == turn.user_id):
        return ReplyPlan(True, "topic_candidate", tier="context", allow_silence=True)

    if _low_value(turn.text):
        return ReplyPlan(False, "low_value", proactive=True, tier="low_value")

    if is_media_only(turn.text):
        return ReplyPlan(False, "unrelated_media", proactive=True, tier="media")

    if behavior.cooldown_seconds > 0:
        last_spoke = _parse(state.get("last_spoke_at"))
        if last_spoke and (now - last_spoke).total_seconds() < behavior.cooldown_seconds:
            return ReplyPlan(False, "cooldown", proactive=True)

    humans = human_count(state, now, behavior.recent_window_seconds)
    if (
        humans >= behavior.share_min_human_messages
        and behavior.max_recent_share < 1.0
        and bot_share(state, now, behavior.recent_window_seconds) >= behavior.max_recent_share
    ):
        return ReplyPlan(False, "max_share_exceeded", proactive=True)
    if humans_since_last_speak(state) < 2:
        return ReplyPlan(False, "human_gap", proactive=True)
    return ReplyPlan(True, "context_candidate", proactive=True, tier="context", allow_silence=True)


def _window(state: dict, now: datetime, window_seconds: int) -> list[dict]:
    cutoff = now - timedelta(seconds=window_seconds)
    return [
        item for item in state.get("recent_speakers", [])
        if (parsed := _parse(item.get("at"))) and parsed >= cutoff
    ]


