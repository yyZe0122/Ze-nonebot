from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationError


class ReplyMessage(BaseModel):
    text: str = Field(default="", max_length=12000)
    sticker_id: StrictInt | None = None


class SkillRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    references: list[str] = Field(default_factory=list, max_length=3)


class MemoryUpdate(BaseModel):
    memory_id: StrictInt = Field(gt=0)
    text: str = Field(min_length=2, max_length=320)
    source_id: StrictInt = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=240)
    change: Literal["correction", "update"] = "correction"


class ChatDecision(BaseModel):
    reply: StrictBool
    target_message_id: StrictInt | None = None
    messages: list[ReplyMessage] = Field(default_factory=list, max_length=3)
    continuation: StrictBool = False
    reason: str = Field(default="", max_length=240)
    next_check_minutes: float | None = Field(default=None, strict=True, ge=1, le=10080)
    activate_skills: list[SkillRequest] = Field(default_factory=list, max_length=3)
    memory_updates: list[MemoryUpdate] = Field(default_factory=list, max_length=3)


class ParticipationDecision(BaseModel):
    reply: StrictBool
    target_message_id: StrictInt
    continuation: StrictBool = False
    reason: str = Field(max_length=160)


class MemoryItem(BaseModel):
    kind: Literal["fact", "preference", "experience", "impression", "group"]
    subject_user_id: StrictInt = Field(ge=0)
    participant_user_ids: list[StrictInt] = Field(default_factory=list, max_length=12)
    text: str = Field(min_length=2, max_length=320)
    source_ids: list[StrictInt] = Field(min_length=1, max_length=8)
    evidence: str = Field(min_length=2, max_length=240)
    supersedes: StrictInt | None = Field(default=None, gt=0)
    change: Literal["correction", "update"] = "update"


class MemoryDecision(BaseModel):
    memories: list[MemoryItem] = Field(default_factory=list, max_length=12)


def parse_decision(text: str, schema: type[BaseModel] = ChatDecision):
    raw = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE
    ).strip()
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            # 保持与直接解析一致：语法错误最终以 ValidationError 暴露。
            return schema.model_validate_json(raw)
    return schema.model_validate(payload)


_POLICY_MARKERS = (
    "prohibited use policy",
    "prompt could not be submitted",
)


def policy_blocked(text: str) -> bool:
    """Gemini reports a safety block as ordinary 200 text instead of an error."""
    folded = text.casefold()
    return any(marker in folded for marker in _POLICY_MARKERS)


def skill_target_issue(decision: ChatDecision, message_id: int | None, known):
    """Missing target is fillable only for a pure, valid skill-activation turn."""
    if (
        decision.reply
        or decision.messages
        or decision.memory_updates
        or not decision.activate_skills
        or decision.target_message_id is not None
        or message_id is None
        or any(request.name not in known for request in decision.activate_skills)
    ):
        return None
    return decision.model_copy(update={"target_message_id": message_id})


def decision_issues(exc: ValidationError):
    return [(item["loc"], item["type"]) for item in exc.errors(include_input=False)]
