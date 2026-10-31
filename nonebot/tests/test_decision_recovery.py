from pydantic import ValidationError

from src.plugins.persona_agent.decisions import (
    ChatDecision,
    MemoryUpdate,
    ReplyMessage,
    parse_decision,
    policy_blocked,
    skill_target_issue,
)
from src.plugins.persona_agent.service import POLICY_GARBLED, POLICY_RETRIES


POLICY = (
    "The prompt could not be submitted. The prompt contains sensitive words "
    "that violate Google's Generative AI Prohibited Use policy."
)


def test_policy_text_is_blocked_but_invalid_json_is_not():
    assert policy_blocked(POLICY)
    assert policy_blocked(f"```\n{POLICY}\n```")
    assert not policy_blocked('{"reply": false}')
    assert not policy_blocked("not json at all")


def test_policy_failure_is_symbolic_after_three_silent_retries():
    assert POLICY_RETRIES == 3
    assert POLICY_GARBLED == "#&@*|@#"
    assert not any("\u4e00" <= char <= "\u9fff" for char in POLICY_GARBLED)


def test_missing_skill_target_is_filled_only_when_activation_is_pure():
    decision = ChatDecision.model_validate(
        {"reply": False, "messages": [], "activate_skills": [{"name": "image-reading"}]}
    )

    filled = skill_target_issue(decision, -1234567890, {"image-reading"})

    assert filled is not None
    assert filled.target_message_id == -1234567890
    assert decision.target_message_id is None


def test_skill_target_is_not_filled_when_another_field_is_invalid():
    pure = ChatDecision.model_validate(
        {"reply": False, "activate_skills": [{"name": "image-reading"}]}
    )
    with_reply = pure.model_copy(update={"reply": True})
    with_message = pure.model_copy(
        update={"messages": [ReplyMessage(text="看过了")]}
    )
    with_memory = pure.model_copy(
        update={
            "memory_updates": [
                MemoryUpdate(
                    memory_id=1,
                    text="状态变了",
                    source_id=2,
                    evidence="原话",
                )
            ]
        }
    )

    assert skill_target_issue(with_reply, 7, {"image-reading"}) is None
    assert skill_target_issue(with_message, 7, {"image-reading"}) is None
    assert skill_target_issue(with_memory, 7, {"image-reading"}) is None
    assert skill_target_issue(pure, 7, {"natural-chat"}) is None
    assert skill_target_issue(pure, None, {"image-reading"}) is None


def test_wrong_target_still_parses_for_the_activation_retry():
    decision = parse_decision(
        '{"reply":false,"target_message_id":1,"messages":[],'
        '"activate_skills":[{"name":"missing-skill"}]}'
    )

    assert decision.target_message_id == 1
    assert skill_target_issue(decision, 7, {"image-reading"}) is None


def test_json_is_extracted_from_surrounding_text_and_any_fence():
    payload = '{"reply":false,"messages":[]}'
    assert parse_decision(payload) == parse_decision(f"好的，这是结果：\n{payload}")
    assert parse_decision(f"{payload}\n以上。").reply is False
    assert parse_decision(f"```JSON\n{payload}\n```").reply is False

def test_malformed_json_exposes_validation_issue_for_one_retry():
    try:
        parse_decision("this is not a decision")
    except ValidationError as exc:
        issues = [(item["loc"], item["type"]) for item in exc.errors(include_input=False)]
    else:
        raise AssertionError("malformed text should not parse")

    assert issues == [((), "json_invalid")]
