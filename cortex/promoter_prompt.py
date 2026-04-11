"""Day 16 -- Haiku classification prompt builder.

Kept in its own module (isolated from `promoter.py`) so unit tests can
assert the exact prompt text without importing the Haiku call path or
the decider. Every string in this file is part of the agent's
observable behavior and must be change-reviewable in git.

The prompt asks Haiku to classify a raw surprise pair into one of
three labels: `match`, `mismatch`, or `partial`. The wording is
deliberately conservative: when evidence is thin, the agent should
prefer `partial` over `mismatch`, because a `mismatch` verdict is
what drives tripwire reinforcement in the Phase-0 fitness score and
false positives actively harm the agent's future briefings.
"""
from __future__ import annotations

from typing import Any

# Max chars we pass through to Haiku per field. Predictions and tool
# responses can be multi-KB; truncation keeps per-pair prompt cost
# bounded and deterministic. The hook already caps outcome and
# failure_mode at 500 chars during capture, so those rarely trigger.
_FIELD_CAP = 1200

_CLASSIFICATION_TEMPLATE = """You are classifying a prediction-outcome pair from an AI agent's work session.

The agent emitted a structured prediction BEFORE executing a tool call, then
executed the tool. Your job is to decide whether reality matched the prediction.

PREDICTION:
  expected outcome:  {outcome}
  predicted failure: {failure_mode}

ACTUAL EXECUTION:
  tool:           {tool_name}
  tool input:     {tool_snippet}
  tool response:  {tool_response}

Classify this pair into exactly one of three labels:

  "match"    -- the actual outcome aligns with the predicted outcome, AND the
                predicted failure did NOT occur. The agent correctly anticipated
                what would happen.

  "mismatch" -- reality diverged from the prediction in a MATERIAL way. Either
                the outcome differs substantively, OR the predicted failure_mode
                actually occurred, OR a different unexpected failure occurred.
                A mismatch means the world surprised the agent.

  "partial"  -- the outcome was roughly correct but some detail differed, OR
                the tool response is ambiguous, OR the evidence is insufficient
                to call a clean match/mismatch.

Conservative rule: when in doubt, choose "partial", NOT "mismatch". A "mismatch"
label will be used to reinforce a tripwire rule, so over-labeling mismatches
causes false positives that harm the agent's future briefings. If tool_response
is empty or only "(no output)", choose "partial" with confidence 0.3.

Respond with ONE JSON object, no prose, no code fences:

{{"label": "match|mismatch|partial",
 "confidence": 0.0-1.0,
 "reasoning": "one short sentence, max 200 chars"}}
"""


def _truncate(s: Any, cap: int = _FIELD_CAP) -> str:
    if s is None:
        return ""
    text = str(s)
    if len(text) <= cap:
        return text
    return text[: cap - 3] + "..."


def build_classification_prompt(pair: dict[str, Any]) -> str:
    """Render the Haiku classification prompt for one surprise pair.

    The `pair` dict shape matches `cortex.surprise.collect_pairs()`
    output: session_id, at, outcome, failure_mode, tool_name,
    tool_snippet, tool_response, tripwire_ids. Missing fields render
    as empty strings / "(none)" sentinels so Haiku never sees `None`.
    """
    outcome = _truncate(pair.get("outcome")) or "(none recorded)"
    failure_mode = _truncate(pair.get("failure_mode")) or "(none recorded)"
    tool_name = _truncate(pair.get("tool_name"), 80) or "(no tool call)"
    tool_snippet = _truncate(pair.get("tool_snippet")) or "(empty)"
    tool_response = _truncate(pair.get("tool_response")) or "(no output)"

    return _CLASSIFICATION_TEMPLATE.format(
        outcome=outcome,
        failure_mode=failure_mode,
        tool_name=tool_name,
        tool_snippet=tool_snippet,
        tool_response=tool_response,
    )
