"""Day 16 -- DMN promoter tests.

Stage 2 scope: classification layer only. `parse_classification`,
`build_classification_prompt`, and `classify_pair` with an injected
call_fn so the Haiku SDK is never imported. Decider and applier tests
arrive in stage 3.
"""
from __future__ import annotations

from cortex import promoter
from cortex.promoter_prompt import build_classification_prompt


# ---- parse_classification ----


def test_parse_classification_valid_match():
    text = (
        '{"label": "match", "confidence": 0.92, '
        '"reasoning": "tool output aligned with predicted outcome"}'
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "match"
    assert p["confidence"] == 0.92
    assert "aligned" in p["reasoning"]


def test_parse_classification_valid_mismatch():
    text = (
        '{"label": "mismatch", "confidence": 0.77, '
        '"reasoning": "predicted failure_mode occurred"}'
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "mismatch"
    assert p["confidence"] == 0.77


def test_parse_classification_strips_code_fence():
    text = (
        "```json\n"
        '{"label": "partial", "confidence": 0.4, "reasoning": "ambiguous"}'
        "\n```"
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "partial"
    assert p["confidence"] == 0.4


def test_parse_classification_clamps_confidence_above_one():
    text = '{"label": "match", "confidence": 1.5, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["confidence"] == 1.0


def test_parse_classification_clamps_confidence_below_zero():
    text = '{"label": "match", "confidence": -0.3, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["confidence"] == 0.0


def test_parse_classification_error_label_on_bad_json():
    p = promoter.parse_classification("not actually json")
    assert p["label"] == "error"
    assert p["confidence"] == 0.0


def test_parse_classification_error_label_on_unknown_label():
    text = '{"label": "bogus", "confidence": 0.5}'
    p = promoter.parse_classification(text)
    assert p["label"] == "error"


def test_parse_classification_error_label_on_empty_input():
    assert promoter.parse_classification("").get("label") == "error"
    assert promoter.parse_classification(None).get("label") == "error"  # type: ignore[arg-type]


def test_parse_classification_missing_confidence_uses_default():
    text = '{"label": "match", "reasoning": "no conf given"}'
    p = promoter.parse_classification(text)
    assert p["label"] == "match"
    assert p["confidence"] == 0.5


def test_parse_classification_reasoning_truncated_to_300():
    long_reason = "x" * 500
    text = f'{{"label": "match", "confidence": 0.8, "reasoning": "{long_reason}"}}'
    p = promoter.parse_classification(text)
    assert len(p["reasoning"]) == 300


def test_parse_classification_tolerates_prose_around_json():
    text = (
        "Here is my classification:\n"
        '{"label": "mismatch", "confidence": 0.9, "reasoning": "tool crashed"}'
        "\nLet me know if you need more context."
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "mismatch"


def test_parse_classification_case_insensitive_label():
    text = '{"label": "MATCH", "confidence": 0.8, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["label"] == "match"


# ---- build_classification_prompt ----


def test_build_prompt_contains_all_pair_fields():
    pair = {
        "session_id": "sess1",
        "at": "2026-04-11T12:00:00+00:00",
        "outcome": "test suite passes cleanly",
        "failure_mode": "slot_ts floor bug reappears",
        "tool_name": "Bash",
        "tool_snippet": "pytest -q",
        "tool_response": "332 passed in 5.76s",
        "tripwire_ids": ["lookahead_parquet"],
    }
    prompt = build_classification_prompt(pair)
    assert "test suite passes cleanly" in prompt
    assert "slot_ts floor bug reappears" in prompt
    assert "Bash" in prompt
    assert "pytest -q" in prompt
    assert "332 passed" in prompt
    # Classification enum + the conservative rule must be present.
    assert "mismatch" in prompt
    assert "when in doubt, choose \"partial\"" in prompt


def test_build_prompt_handles_missing_fields():
    pair = {"outcome": "", "failure_mode": "", "tool_name": None}
    prompt = build_classification_prompt(pair)
    assert "(none recorded)" in prompt
    assert "(no tool call)" in prompt
    assert "(no output)" in prompt


def test_build_prompt_truncates_long_fields():
    pair = {
        "outcome": "x" * 5000,
        "failure_mode": "y" * 5000,
        "tool_name": "Bash",
        "tool_snippet": "z" * 5000,
        "tool_response": "w" * 5000,
    }
    prompt = build_classification_prompt(pair)
    # 1200-char cap per field plus template overhead; nowhere near 5000.
    assert len(prompt) < 8000


# ---- classify_pair with injected call_fn ----


def test_classify_pair_uses_injected_call_fn():
    captured: dict[str, object] = {}

    def fake_call(prompt: str, model: str, max_tokens: int, client) -> str:
        captured["prompt"] = prompt
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return '{"label": "mismatch", "confidence": 0.88, "reasoning": "diverged"}'

    pair = {
        "outcome": "passes",
        "failure_mode": "crashes",
        "tool_name": "Bash",
        "tool_snippet": "ls",
        "tool_response": "error",
    }
    result = promoter.classify_pair(pair, call_fn=fake_call)
    assert result["label"] == "mismatch"
    assert result["confidence"] == 0.88
    assert result["model"] == promoter.DEFAULT_MODEL
    assert result["prompt_tokens"] > 0
    # Prompt was built and passed through.
    assert "passes" in captured["prompt"]
    assert "crashes" in captured["prompt"]


def test_classify_pair_override_model():
    def fake_call(prompt, model, max_tokens, client):
        return '{"label": "match", "confidence": 0.9, "reasoning": ""}'

    result = promoter.classify_pair(
        {"outcome": "ok", "failure_mode": "", "tool_name": "Read"},
        call_fn=fake_call,
        model="claude-sonnet-4-6",
    )
    assert result["model"] == "claude-sonnet-4-6"


def test_classify_pair_error_on_parser_failure():
    def fake_call(prompt, model, max_tokens, client):
        return "this is not json at all"

    result = promoter.classify_pair(
        {"outcome": "x", "failure_mode": "y"},
        call_fn=fake_call,
    )
    assert result["label"] == "error"
    assert result["model"] == promoter.DEFAULT_MODEL


def test_classify_pair_handles_call_fn_exception():
    def failing_call(prompt, model, max_tokens, client):
        raise RuntimeError("network down")

    result = promoter.classify_pair(
        {"outcome": "x", "failure_mode": "y"},
        call_fn=failing_call,
    )
    assert result["label"] == "error"
    assert "RuntimeError" in result["reasoning"]
