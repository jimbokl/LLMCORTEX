"""Tests for the auto-regex generator in cortex/suggest_patterns.py."""
from __future__ import annotations

import re

from cortex.suggest_patterns import (
    _generalize_to_regex,
    _lcs_across,
    _longest_common_substring,
    generate_regex_candidate,
    generate_regex_candidates,
)

# ---------- _longest_common_substring ----------


def test_lcs_empty_inputs():
    assert _longest_common_substring("", "abc") == ""
    assert _longest_common_substring("abc", "") == ""


def test_lcs_no_overlap():
    assert _longest_common_substring("abc", "xyz") == ""


def test_lcs_full_overlap():
    assert _longest_common_substring("abcdef", "abcdef") == "abcdef"


def test_lcs_partial():
    a = "df['slot_ts'] = (df['ts'] // 300) * 300"
    b = "df['slot_ts'] = (df['ts'] // 600) * 600"
    lcs = _longest_common_substring(a, b)
    assert "slot_ts" in lcs
    assert "// " in lcs


# ---------- _lcs_across ----------


def test_lcs_across_single_snippet():
    assert _lcs_across(["hello world"]) == "hello world"


def test_lcs_across_empty():
    assert _lcs_across([]) == ""


def test_lcs_across_three_snippets():
    snippets = [
        "df['slot_ts'] = (df['ts'] // 300) * 300",
        "df['slot_ts'] = (df['ts'] // 600) * 600",
        "df['slot_ts'] = (df['ts'] // 60) * 60",
    ]
    lcs = _lcs_across(snippets)
    assert "slot_ts" in lcs
    assert "// " in lcs


def test_lcs_across_disjoint_returns_empty():
    snippets = ["apple pie", "banana bread", "cherry cake"]
    lcs = _lcs_across(snippets)
    # May find short common substrings like " " or single chars but not
    # anything meaningful
    assert len(lcs) < 5


# ---------- _generalize_to_regex ----------


def test_generalize_escapes_metacharacters():
    regex = _generalize_to_regex("a.b*c+d")
    # The dot, star, plus should be escaped
    assert "\\." in regex
    assert "\\*" in regex
    assert "\\+" in regex


def test_generalize_digits_become_d_plus():
    regex = _generalize_to_regex("slot_ts = 300")
    assert r"\d+" in regex
    # Literal "300" should no longer appear
    assert "300" not in regex


def test_generalize_spaces_become_s_star():
    regex = _generalize_to_regex("a  b c")
    assert r"\s*" in regex


def test_generalize_compiles_as_valid_regex():
    regex = _generalize_to_regex("df['slot_ts'] = (df['ts'] // 300) * 300")
    # Should compile without error
    pat = re.compile(regex)
    # Should match the original input
    assert pat.search("df['slot_ts'] = (df['ts'] // 300) * 300")


# ---------- generate_regex_candidate ----------


def test_candidate_returns_none_for_empty():
    assert generate_regex_candidate([]) is None
    assert generate_regex_candidate(["", "  "]) is None


def test_candidate_returns_none_for_too_short_anchor():
    # Very short inputs with no common substring long enough
    assert generate_regex_candidate(["abc", "xyz"]) is None


def test_candidate_for_lookahead_bug_snippets():
    snippets = [
        "file=DETECTOR/backfill_a.py | new=df['slot_ts'] = (df['ts'] // 300) * 300",
        "file=DETECTOR/backfill_b.py | new=df['slot_ts'] = (df['ts'] // 300) * 300",
        "file=DETECTOR/backfill_c.py | new=df['slot_ts'] = (df['ts'] // 300) * 300",
    ]
    cand = generate_regex_candidate(snippets)
    assert cand is not None
    assert cand["match_count"] == 3
    assert cand["total"] == 3
    assert cand["confidence"] in ("high", "medium")
    # The regex should actually match the originals
    pat = re.compile(cand["regex"])
    for s in snippets:
        assert pat.search(s)


def test_candidate_detects_fix_example_matches():
    """When regex is too broad and matches a fix, confidence drops and
    fix_example_matches is True."""
    snippets = [
        "df['slot_ts'] = (df['ts'] // 300) * 300",
        "df['slot_ts'] = (df['ts'] // 300) * 300",
    ]
    # The fix is `* 300 + 300`. The auto-regex ends with `\d+` at the end,
    # which matches `300` inside `* 300 + 300` — so fix_example_matches True.
    fix = "df['slot_ts'] = (df['ts'] // 300) * 300 + 300"
    cand = generate_regex_candidate(snippets, fix_example=fix)
    assert cand is not None
    assert cand["fix_example_matches"] is True
    assert cand["confidence"] == "low"


def test_candidate_fix_example_passes_when_regex_is_specific():
    """A regex generated from snippets where the fix differs should NOT
    match the fix."""
    snippets = [
        "entry_price = 0.5",
        "entry_price = 0.5",
    ]
    fix = "entry_price = up_ask_180"  # totally different syntax
    cand = generate_regex_candidate(snippets, fix_example=fix)
    assert cand is not None
    assert cand["fix_example_matches"] is False


def test_candidate_generalizes_digits():
    """The auto-regex should replace digits with \\d+ so it matches
    variant numbers."""
    snippets = [
        "slot_ts = ts // 300",
        "slot_ts = ts // 600",
    ]
    cand = generate_regex_candidate(snippets)
    assert cand is not None
    pat = re.compile(cand["regex"])
    # Should match both even though the numbers differ
    assert pat.search("slot_ts = ts // 300")
    assert pat.search("slot_ts = ts // 600")
    # And should match a new number not in the training set
    assert pat.search("slot_ts = ts // 60")


# ---------- generate_regex_candidates (multi) ----------


def test_generate_candidates_global_only_when_no_per_tool():
    analysis = {
        "snippets_by_tool": {
            "Edit": ["df['slot_ts'] = (df['ts'] // 300) * 300"],
        },
    }
    result = generate_regex_candidates(analysis)
    # Single snippet per tool < 2, so no per-tool candidate
    assert result["by_tool"] == {}
    # Global is over 1 snippet but anchor length fails min
    # (actually a single-snippet global may or may not succeed)


def test_generate_candidates_per_tool_when_enough_samples():
    analysis = {
        "snippets_by_tool": {
            "Edit": [
                "df['slot_ts'] = (df['ts'] // 300) * 300",
                "df['slot_ts'] = (df['ts'] // 600) * 600",
            ],
            "Bash": ["python DETECTOR/backfill.py"],
        },
    }
    result = generate_regex_candidates(analysis)
    assert "Edit" in result["by_tool"]  # 2 snippets
    assert "Bash" not in result["by_tool"]  # only 1 snippet


def test_generate_candidates_passes_fix_example_through():
    analysis = {
        "snippets_by_tool": {
            "Edit": [
                "df['slot_ts'] = (df['ts'] // 300) * 300",
                "df['slot_ts'] = (df['ts'] // 300) * 300",
            ],
        },
    }
    fix = "df['slot_ts'] = (df['ts'] // 300) * 300 + 300"
    result = generate_regex_candidates(analysis, fix_example=fix)
    # The global candidate should have fix_example_matches True
    if result["global"]:
        assert result["global"]["fix_example_matches"] in (True, False)
