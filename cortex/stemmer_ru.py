"""Minimal Russian suffix stripper.

Not a full Snowball implementation — just the set of common inflectional
suffixes that collapse the most frequent Cyrillic surface-form variants
to a shared stem. The goal is recall on trigger matching, not
linguistic correctness.

Examples:
    "бэктесты"  -> "бэктест"
    "бэктеста"  -> "бэктест"
    "бэктестов" -> "бэктест"
    "запустил"  -> "запуст"
    "запущенный"-> "запущ"

Only applied to Cyrillic-containing tokens by `cortex.tokenize.tokenize`.
Stdlib-only; no network; O(1) per token.
"""
from __future__ import annotations

# Ordered longest-first within each group so that longer suffixes are
# stripped before shorter prefixes of themselves (e.g. "ами" before "а").
_NOUN_SUFFIXES = (
    "иями", "ями", "ами",
    "ого", "его", "ому", "ему",
    "ыми", "ими", "ых", "их",
    "ой", "ей", "ою", "ею",
    "ья", "ье", "ью",
    "ов", "ев", "ям", "ом", "ем", "ах", "ях",
    "а", "я", "о", "е", "у", "ю", "ы", "и",
)

_VERB_SUFFIXES = (
    "ейшую", "ейшие", "ейший", "ейшая", "ейшее",
    "ивший", "ившая", "ившее", "ившие", "ивших",
    "ющий", "ющая", "ющее", "ющие", "ющих",
    "ящий", "ящая", "ящее", "ящие", "ящих",
    "вший", "вшая", "вшее", "вшие", "вших",
    "нный", "нная", "нное", "нные", "нных",
    "ться", "тся",
    "ивши", "ывши", "евши", "авши",
    "ивая", "ывая", "евая",
    "ешь", "ете", "ишь", "ите", "ем", "ём",
    "ите", "ут", "ют", "ат", "ят",
    "ала", "ало", "али", "яла", "яло", "яли",
    "ела", "ело", "ели", "ила", "ило", "или",
    "ула", "уло", "ули", "ыла", "ыло", "ыли",
    "ать", "ять", "ить", "еть", "уть", "ыть",
    "ал", "ял", "ел", "ил", "ул", "ыл",
    "ся", "сь",
)

_ADJ_SUFFIXES = (
    "ейшими", "ейших",
    "ими", "ыми",
    "ая", "яя", "ое", "ее", "ые", "ие",
    "ый", "ий", "ой",
    "ую", "юю",
    "ым", "им",
)

_REFLEXIVE = ("ся", "сь")

_MIN_STEM_LEN = 3


def _strip_one(token: str, suffixes: tuple[str, ...]) -> str:
    for suf in suffixes:
        if token.endswith(suf) and len(token) - len(suf) >= _MIN_STEM_LEN:
            return token[: -len(suf)]
    return token


def stem_ru(token: str) -> str:
    """Apply minimal suffix stripping to a Cyrillic token.

    Order matters: reflexive (ся/сь) -> verb -> adjective -> noun. A
    token is returned unchanged if stripping would leave fewer than
    `_MIN_STEM_LEN` characters.
    """
    if len(token) < _MIN_STEM_LEN + 1:
        return token

    # Reflexive suffix is separable and must come off first.
    for suf in _REFLEXIVE:
        if token.endswith(suf) and len(token) - len(suf) >= _MIN_STEM_LEN:
            token = token[: -len(suf)]
            break

    token = _strip_one(token, _VERB_SUFFIXES)
    token = _strip_one(token, _ADJ_SUFFIXES)
    token = _strip_one(token, _NOUN_SUFFIXES)

    # Normalize final soft/hard sign — they rarely carry discriminating
    # information after stripping and their presence/absence varies
    # across inflectional forms.
    if token.endswith(("ь", "ъ")) and len(token) - 1 >= _MIN_STEM_LEN:
        token = token[:-1]

    return token
