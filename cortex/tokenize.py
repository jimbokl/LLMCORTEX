"""Shared tokenizer for the classifier and the TF-IDF fallback.

Default behavior is unchanged from Day 1: the ASCII-only pattern
`[a-z0-9_\\-]+` extracts lowercase English/code tokens and silently drops
everything else. Mixed RU+EN prompts still match on surviving English
tokens.

Opt-in via `CORTEX_UNICODE_TOKENS=1`: switches to a Unicode-aware pattern
`[\\w\\-]+` with case folding and (optional) Russian suffix-stripping so
that `бэктест`, `бэктесты`, `бэктеста` all collapse to the same token
and can match trigger words authored in English. Designed to be a
minimal addition, not a full IR stack.

Invariants:
- Off-by-default: `test_tokenize_splits_on_non_word` and every other
  Day-2 guard test continues to pass unchanged.
- Zero new runtime dependencies: Snowball-style stemmer is vendored in
  `stemmer_ru.py` as pure stdlib.
- Same `set[str]` return shape as the old tokenizers, so callers do not
  need to change their downstream logic.
"""
from __future__ import annotations

import os
import re

_ASCII_WORD_RE = re.compile(r"[a-z0-9_\-]+")
_UNICODE_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _unicode_enabled() -> bool:
    return os.environ.get("CORTEX_UNICODE_TOKENS") == "1"


def tokenize(text: str) -> set[str]:
    """Tokenize a prompt into a set of lowercase word-like tokens.

    Returns the ASCII-only token set by default. If `CORTEX_UNICODE_TOKENS=1`
    is set, returns a Unicode-aware token set that includes Cyrillic words
    with light Russian suffix stripping applied.
    """
    if not _unicode_enabled():
        return set(_ASCII_WORD_RE.findall(text.lower()))

    from cortex.stemmer_ru import stem_ru

    lowered = text.lower()
    out: set[str] = set()
    for raw in _UNICODE_WORD_RE.findall(lowered):
        out.add(raw)
        # Apply RU stem only to tokens that contain Cyrillic, so English
        # tokens are never mutated. The original form stays in the set as
        # well — that way an authored trigger of either surface form still
        # matches the same prompt.
        if _has_cyrillic(raw):
            stemmed = stem_ru(raw)
            if stemmed and stemmed != raw:
                out.add(stemmed)
    return out


def _has_cyrillic(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if 0x0400 <= code <= 0x04FF:
            return True
    return False
