"""Guards for the shared tokenizer and its opt-in Unicode mode.

Default (no env var): ASCII-only behavior, identical to the Day-1
`[a-z0-9_\\-]+` regex — cyrillic silently dropped, matches every Day-2
guard test already in the suite.

Opt-in (CORTEX_UNICODE_TOKENS=1): Unicode-aware capture plus light RU
suffix stripping, so russian inflectional forms collapse to a shared
stem and can match english-authored triggers in the same prompt.
"""
from __future__ import annotations

import os

import pytest

from cortex import tokenize as tokenize_mod
from cortex.tokenize import tokenize


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CORTEX_UNICODE_TOKENS", raising=False)
    yield


def test_tokenize_unicode_flag_off_by_default():
    # Mixed RU + EN prompt, default mode. Cyrillic must be dropped so
    # the Day-1 invariant survives — the english tokens still come
    # through unchanged for downstream rule matching.
    out = tokenize("запусти бэктест polymarket 5 минут")
    assert "polymarket" in out
    assert "5" in out
    # No cyrillic tokens in default mode.
    assert all(not _has_cyrillic(t) for t in out)


def test_tokenize_unicode_flag_preserves_cyrillic(monkeypatch):
    monkeypatch.setenv("CORTEX_UNICODE_TOKENS", "1")
    out = tokenize("запусти бэктест polymarket")
    assert "polymarket" in out
    # The russian forms — either the raw surface form or the stem —
    # must be present so a trigger authored either way still matches.
    assert any("бэктест" in t for t in out)
    assert any("запуст" in t or "запусти" in t for t in out)


def test_tokenize_unicode_collapses_russian_inflection(monkeypatch):
    monkeypatch.setenv("CORTEX_UNICODE_TOKENS", "1")
    out = tokenize("бэктесты бэктеста бэктестом бэктесту")
    # All four inflected forms must share at least one common stem
    # after RU suffix stripping. Exact match on `бэктест` required.
    assert "бэктест" in out


def test_tokenize_default_identical_to_legacy_regex():
    # Byte-for-byte parity with the old `[a-z0-9_\-]+` behavior on
    # an ASCII prompt, so every Day-2 guard keeps passing.
    out = tokenize("run a 5m poly directional backtest")
    assert out == {"run", "a", "5m", "poly", "directional", "backtest"}


def test_tokenize_unicode_does_not_corrupt_ascii_tokens(monkeypatch):
    # English tokens must not be passed through the RU stemmer even
    # under the flag — the stemmer is gated on Cyrillic presence.
    monkeypatch.setenv("CORTEX_UNICODE_TOKENS", "1")
    out = tokenize("backtest slots directional")
    assert "backtest" in out
    assert "slots" in out
    assert "directional" in out


def test_unicode_enabled_helper_honors_env(monkeypatch):
    assert tokenize_mod._unicode_enabled() is False
    monkeypatch.setenv("CORTEX_UNICODE_TOKENS", "1")
    assert tokenize_mod._unicode_enabled() is True
    # Any other value keeps the legacy behavior.
    monkeypatch.setenv("CORTEX_UNICODE_TOKENS", "true")
    assert tokenize_mod._unicode_enabled() is False


def _has_cyrillic(s: str) -> bool:
    return any(0x0400 <= ord(ch) <= 0x04FF for ch in s)
