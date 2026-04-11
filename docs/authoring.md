# Authoring guide

How to add content to Cortex: tripwires, rules, cost components, synthesis
rules, and verifiers.

## Anatomy of a tripwire

A tripwire is a structured lesson. Fields:

| Field | Type | Purpose |
|---|---|---|
| `id` | str | Unique snake_case id |
| `title` | str | One-line summary (≤80 chars displays cleanly in `cortex list`) |
| `severity` | `critical` / `high` / `medium` / `low` | How prominently to inject |
| `domain` | str | `polymarket`, `generic`, or your project tag |
| `triggers` | list[str] | Keyword vocabulary for the classifier |
| `body` | str | WHY + HOW TO APPLY, 3-6 short paragraphs |
| `verify_cmd` | str, optional | Bash command that re-checks whether the lesson still holds |
| `cost_usd` | float | Dollar cost of the past violation (0 is fine for research-time lessons) |
| `source_file` | str, optional | Backlink to origin memory file |
| `violation_patterns` | list[str], optional | Regex patterns matched against runtime `tool_input` for silent violation detection (Day 6) |

## What makes a good tripwire

### Body template

```
<one-sentence rule statement>

Why: <specific incident with date, numbers, evidence>

How to apply: (1) <concrete action>. (2) <concrete action>. (3) <edge case>.
```

### Good examples (from the seed set)

- **Specific numbers**: *"Polymarket net fee = 0.072 × min(p, 1−p) × size,
  NOT 10% flat. At mid prices net fee ~3.6% per side; at extremes <0.4%."*
- **Quantified past cost**: *"Bots deployed live — backtest showed near-100%
  WR, real WR 69.6%/74.4%, auto-killed on 3rd consecutive loss."*
- **Actionable steps**: *"(1) Before any backtest, run a SHIFT TEST — replay
  with the feature shifted back one slot. If WR drops >10pp, it contains
  lookahead. (2) 100% WR on >100 trades is ALWAYS suspect — first
  hypothesis must be lookahead, not 'edge so strong it's perfect'."*

### Bad examples

- **Too vague**: "Always be careful with backtests"
- **Too narrow**: "Use exactly 5.2 as the slippage constant"
- **Unfalsifiable**: "Consider all relevant factors"
- **Missing WHY**: a rule with no incident behind it is a preference, not
  a tripwire

**Litmus test**: if you can't point to a specific past failure, it's not
ready to be a tripwire. Put it in a notes file until it bites you.

## Adding a tripwire

### Via CLI (ad-hoc)

```bash
cortex add \
  --id my_rule \
  --title "Short summary" \
  --severity high \
  --domain polymarket \
  --triggers "word1,word2,word3" \
  --body "Rule. Why: incident. How to apply: (1) thing. (2) thing." \
  --cost-usd 42.00
```

### Via inbox (recommended for tripwires drafted from Palace search or
other discovery flows — Day 8)

`cortex import-palace --to-inbox "your query"` stages draft JSON files
under `.cortex/inbox/*.json`. Each draft has validation status visible
in `cortex inbox list`:

```
$ cortex inbox list
DRAFT_ID                             SOURCE              ID_FIELD              STATUS
palace_polymarket_20260411_a3f2c1    palace_polymarket   TODO_snake_case_id    TODO: id,title,triggers
```

Edit the draft file in your editor to fill in real `id`, `title`,
`triggers`, and `body`. Then:

```bash
cortex inbox show palace_polymarket_20260411_a3f2c1    # verify status READY
cortex inbox approve palace_polymarket_20260411_a3f2c1  # promotes to store
```

Use `cortex inbox reject <draft_id>` to discard a draft you decided
isn't worth a tripwire. The `--force` flag on approve bypasses the
TODO placeholder check if you really want to ship a draft as-is.

### Via importer (recommended for anything you want to version in git)

Edit [`cortex/importers/memory_md.py`](../cortex/importers/memory_md.py),
append to `SEED_TRIPWIRES`:

```python
{
    "id": "my_rule",
    "title": "Short summary",
    "severity": "high",
    "domain": "polymarket",
    "triggers": ["word1", "word2", "word3"],
    "body": (
        "Rule statement here.\n"
        "\n"
        "Why: specific incident, date, numbers.\n"
        "\n"
        "How to apply: (1) action. (2) action. (3) edge case."
    ),
    "verify_cmd": None,          # or a bash command string
    "cost_usd": 42.00,
    "source_file": "feedback_my_rule.md",   # optional backlink
},
```

Then `cortex migrate`. Upsert preserves violation stats, so re-runs are
safe.

## Writing a rule

Rules live in `cortex/rules/*.yml` and map prompt keywords to tripwires.

```yaml
- id: my_rule_id
  description: "Human-readable intent"
  match_any: [word1, word2]   # at least one must appear in prompt tokens
  and_any:   [word3, word4]   # AND at least one of these
  inject:
    - tripwire_id_1
    - tripwire_id_2
```

A rule fires when `(match_any ∩ prompt_tokens) AND (and_any ∩ prompt_tokens)`
are both non-empty. Its `inject` list becomes part of the matched-tripwire
set.

### Rule authoring tips

- **Narrow triggers beat broad triggers.** `match_any: [poly, polymarket]`
  is fine; `match_any: [p]` is not.
- **Always require an `and_any` disambiguator.** A rule with only
  `match_any: [fee]` fires on every mention of "fee" regardless of context.
  Add `and_any: [poly, backtest]` to narrow it.
- **Test before shipping.** Run the prompt through `cortex-hook` in the
  terminal to see what it matches:

  ```bash
  echo '{"prompt":"your test prompt"}' | cortex-hook | python -m json.tool
  ```

- **Negative-test too.** Run it against unrelated prompts to confirm it
  stays silent when it should.

## Cost components

A cost component attaches a numeric drag or boost to a tripwire, so the
synthesizer can sum them.

```python
{
    "id": "pm_5m_spread",
    "tripwire_id": "directional_5m_dead",
    "metric": "spread_slip",
    "value": 2.4,
    "unit": "pp",
    "sign": "drag",          # or "boost"
},
```

Add to `SEED_COST_COMPONENTS` in the importer.

**When to add a cost component**: only when the tripwire represents a
*quantifiable cumulative cost* that can be summed with other drags. The
`cost_usd` field on a tripwire is NOT a cost component — that's the
past incident cost. Cost components are for ongoing drags like
`spread = 2.4pp`, `latency_penalty = 50ms`, `edge_per_trade = −0.3pp`
that only matter when combined.

## Synthesis rules

A synthesis rule declares: *when these tripwires' cost components sum
above a threshold, fire with this message*.

```python
{
    "id": "pm_5m_directional_block",
    "triggers": ["5m", "directional", "poly"],
    "sum_over": [
        "pm_5m_spread",
        "pm_5m_info_decay",
        "pm_5m_adverse_sel",
    ],
    "threshold": 5.0,
    "op": "gte",                 # gte | gt | lte | lt
    "message": (
        "Sum drag = {sum}pp ({n} components) >= {threshold}pp floor. "
        "Any directional 5m strategy needs pre-fee edge > {sum}pp."
    ),
},
```

Placeholders available in `message`:

- `{sum}` — the computed total (signed)
- `{total}` — alias for `{sum}`
- `{threshold}` — the rule's threshold
- `{n}` — number of active components

**Partial matches work**. If `sum_over` lists 3 components but only 2 are
active (because the third component's tripwire wasn't matched), the rule
fires if those 2 components alone cross the threshold. This is
intentional — real-world strategies fail on subsets of structural costs.

## Violation patterns (Day 6)

A tripwire can declare `violation_patterns` — regex strings that
`cortex-watch` checks against runtime `tool_input` snippets after the
tripwire has been injected in the current session. When a pattern matches,
a `potential_violation` event is logged, and `cortex stats --sessions`
shows it in the effectiveness report.

```python
{
    "id": "lookahead_parquet",
    ...,
    "violation_patterns": [
        r"slot_ts[^\n]*?=[^\n]*?//\s*\d+[^\n]*?\*\s*\d+\b(?!\s*\+)",
    ],
},
```

### What tool_input looks like

`cortex-watch` summarizes tool input into a 500-char snippet per tool:

- **Bash**: the `command` string, truncated
- **Edit / Write / MultiEdit**: `file=<path> | old=<snippet> | new=<snippet>`
- **Read / Glob / Grep**: `file_path=...` / `pattern=...` / `path=...`
- **Other**: JSON-serialized `tool_input`, truncated

Your patterns match against this summary, not the raw tool input.

### Auto-generating patterns from session data (Day 9)

After a week of real usage, the `violation_patterns` you need are
discoverable from the session logs. Run:

```bash
cortex suggest-patterns <tripwire_id>
```

The tool reads all session logs, finds past injections of the given
tripwire, collects the tool_calls that followed, and emits auto-regex
candidates using a longest-common-substring + generalization heuristic.
Output includes HIGH/MEDIUM/LOW confidence tags.

If you already know what a correct fix looks like, pass it as
`--fix-example` and the tool will verify the candidate does NOT match
the fix:

```bash
cortex suggest-patterns lookahead_parquet \
  --fix-example "file=DETECTOR/backfill.py | old=old | new=df['slot_ts'] = (df['ts'] // 300) * 300 + 300"
```

Any candidate that matches the fix is flagged `[LOW CONFIDENCE]` and
annotated `fix: MATCHES the given fix example — too broad, narrow
manually`. That's your cue to add a negative lookahead for the
forward-shift signature (the trailing `+ \d+` in this example).

The full workflow:

1. `cortex stats --sessions` shows a `[WARN]` or `[FAIL]` tripwire
2. `cortex suggest-patterns <id> --fix-example "<known fix>"`
3. Copy the HIGH/MEDIUM candidate into `cortex/importers/memory_md.py`
4. `cortex migrate`
5. `cortex stats --sessions` now reports an effectiveness rate

### Pattern authoring tips

- **Test patterns before shipping.** The regex engine can backtrack in
  surprising ways. Add `\b` or anchor end-of-match where possible.
- **Prefer false negatives to false positives.** A missed violation is
  acceptable; a false positive poisons the effectiveness metric.
- **Avoid patterns that match common phrases.** `fee = 10%` matches
  anywhere the word "fee" appears with 10% nearby — probably too broad.
- **Test the fix pattern, not just the bug pattern.** If the common fix
  is `(ts // N) * N + N`, your regex must NOT match it.
- **Leave `violation_patterns` out if in doubt.** A tripwire without
  patterns is still useful — it gets injected and the agent sees it.
  It just doesn't contribute to effectiveness measurement.

### Effectiveness interpretation

After a week of real usage, `cortex stats --sessions` shows:

```
Tripwire effectiveness (violation rate = viol / hits):
  [OK  ] poly_fee_empirical      hits=16  viol=0   rate=0.00
  [WARN] lookahead_parquet       hits=6   viol=1   rate=0.17
  [FAIL] bad_tripwire            hits=8   viol=5   rate=0.62
```

- **OK** (rate=0): lesson applied every time it was shown. Keep.
- **WARN** (0 < rate < 0.5): occasional ignore. Consider rephrasing.
- **FAIL** (rate >= 0.5): lesson mostly ignored. Either the brief isn't
  being read, the pattern is too sensitive (false positives), or the
  rule needs blocking enforcement rather than advisory injection.

## Writing a verifier

A verifier is a standalone Python module under `cortex/verifiers/` that
scans code or config for a specific failure pattern.

### Contract

- Takes CLI args via `argparse`
- Exits **0** on pass, **non-zero** on fail
- Supports `--json` for machine-readable output
- Fails gracefully if the target directory doesn't exist (exit 0, don't
  crash)

### Skeleton

```python
# cortex/verifiers/check_my_thing.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def scan_file(path: Path) -> list[dict]:
    """Scan one file and return a list of findings (dicts)."""
    ...


def scan_directory(root: Path) -> list[dict]:
    findings: list[dict] = []
    for py in root.rglob("*.py"):
        findings.extend(scan_file(py))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-dir", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.target_dir.exists():
        return 0  # nothing to verify, not a failure

    findings = scan_directory(args.target_dir)
    if args.json:
        print(json.dumps({"findings": findings}))
    else:
        for f in findings:
            print(f)
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
```

### Registration

Add a script entry in [`pyproject.toml`](../pyproject.toml):

```toml
[project.scripts]
cortex-check-my-thing = "cortex.verifiers.check_my_thing:main"
```

Wire it to a tripwire:

```python
"verify_cmd": "cortex-check-my-thing --target-dir POLY/SRC/",
```

## Severity picker

| Severity | When to use |
|---|---|
| `critical` | Past violation cost real money or many hours of dev time. Recurrence is unacceptable. |
| `high` | Past violation cost research time or caused a bad decision. Recurrence is painful. |
| `medium` | Known pitfall, not yet violated. Worth surfacing but not blocking. |
| `low` | Style or preference. Consider whether it's worth injecting at all. |

Cortex doesn't auto-delete `low` severities, but if a tripwire hasn't
matched in 30 days it's a candidate for removal. Day-5 `cortex stats
--sessions` will surface cold tripwires automatically.

## Tests

Every new tripwire should include at least one positive and one negative
test. Add them to `tests/test_classify.py` or a new file:

```python
def test_my_rule_fires_on_expected_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        result = classify_prompt(
            "my test prompt with my trigger words",
            db_path=db,
        )
        ids = {t["id"] for t in result["tripwires"]}
        assert "my_rule" in ids


def test_my_rule_does_not_fire_on_unrelated():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        result = classify_prompt("unrelated prompt", db_path=db)
        ids = {t["id"] for t in result["tripwires"]}
        assert "my_rule" not in ids
```

Run the full suite before committing:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

## The "should this be a tripwire" decision tree

```
Did this cost real money or > 2 hours of dev time?
│
├── YES → Did the agent/user already know the lesson but ignore it?
│        │
│        ├── YES → critical tripwire (this is the sweet spot)
│        │
│        └── NO → high tripwire + write a verifier if code-checkable
│
└── NO → Is it quantifiable as a drag in combination with others?
         │
         ├── YES → cost component + synthesis rule (composes into one)
         │
         └── NO → probably a note, not a tripwire. Skip it.
```
