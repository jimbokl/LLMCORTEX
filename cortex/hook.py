"""Claude Code UserPromptSubmit hook entry point.

Reads the hook JSON payload from stdin, classifies the user's prompt against
the cortex rule set, and emits `hookSpecificOutput.additionalContext` with the
rendered brief. Any failure results in a silent no-op (exit 0, empty stdout)
so that a broken cortex never blocks user interaction.

Install by adding to `.claude/settings.json`:

    {
      "hooks": {
        "UserPromptSubmit": [
          {
            "hooks": [
              {"type": "command", "command": "cortex-hook"}
            ]
          }
        ]
      }
    }

Optionally set `CORTEX_DB=/abs/path/to/store.db` in the environment if your
working directory does not contain a `.cortex/store.db`. Otherwise the hook
walks up from CWD until it finds one.
"""
from __future__ import annotations

import json
import subprocess
import sys


def _fetch_touched_files(timeout_seconds: float = 2.0) -> list[str]:
    """Return the list of repo-relative paths touched by the working tree.

    Uses `git diff --name-only HEAD` from the hook's CWD (Claude Code
    launches the hook in the project root). Fail-open: any error —
    git missing, not a repo, subprocess timeout, non-zero exit — yields
    an empty list so the hook never stalls or raises because of git.
    """
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            return []
        files = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        return files
    except Exception:
        return []


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        prompt = payload.get("prompt", "") or ""
        if not prompt.strip():
            return 0

        from cortex.classify import classify_prompt, render_brief
        from cortex.fitness import score_prompt_frustration
        from cortex.verify_runner import run_verifiers_for

        # Tier 1.4: pull git-touched paths so `affected_files` globs on
        # tripwires can match edits that the prompt text doesn't mention
        # (e.g. "clean this up" on an already-open file).
        touched_files = _fetch_touched_files()
        result = classify_prompt(prompt, touched_files=touched_files)
        session_id = payload.get("session_id", "") or ""

        # Phase 0 (Autonomous Epistemic Loop): score the prompt for
        # corrective / frustrated language so the next fitness pass can
        # attribute a soft-negative signal back to whatever tripwires
        # the PREVIOUS inject in this session produced. We store only a
        # [0.0, 1.0] scalar, never the prompt text.
        frustration = score_prompt_frustration(prompt)

        # Day 15: shadow matches are collected for audit only, never
        # rendered into the brief. Log them BEFORE the rest of the flow
        # so even a later crash / fail-open path preserves the signal
        # needed by the Day-16+ promoter loop.
        shadow_hits = result.get("shadow_tripwires") or []
        if shadow_hits and session_id:
            try:
                from cortex.session import log_event
                log_event(
                    session_id,
                    "shadow_hit",
                    {
                        "matched_rules": result.get("matched_rules") or [],
                        "tripwire_ids": [t["id"] for t in shadow_hits],
                    },
                )
            except Exception:
                pass

        # Day 7: optional pre-flight verification for critical tripwires.
        # Opt-in via CORTEX_VERIFY_ENABLE=1. Fail-safe on any runner error.
        try:
            result["verifier_results"] = run_verifiers_for(result["tripwires"])
        except Exception:
            result["verifier_results"] = []

        # Day 10: verifier blocking mode. When CORTEX_VERIFY_BLOCK=1 is set
        # AND any critical verifier reports passed=False, we exit 2 after
        # emitting the brief (so the agent still sees the warning). Exit 2
        # is the Claude Code UserPromptSubmit convention for "block this
        # prompt". We never block unless BOTH env vars are set --
        # CORTEX_VERIFY_ENABLE (Day 7) AND CORTEX_VERIFY_BLOCK (Day 10).
        should_block = False
        if _verify_block_enabled():
            for v in result["verifier_results"] or []:
                if v.get("passed") is False:
                    should_block = True
                    break

        if not result["tripwires"]:
            # Rule engine miss -- fall back to in-process keyword scoring.
            fallback_hits: list = []
            brief = ""
            try:
                from cortex.classify import find_db
                from cortex.store import CortexStore
                from cortex.tfidf_fallback import (
                    fallback_search,
                    render_fallback_brief,
                )

                store = CortexStore(find_db())
                try:
                    fallback_hits = fallback_search(prompt, store)
                finally:
                    store.close()
                brief = render_fallback_brief(fallback_hits) if fallback_hits else ""
            except Exception:
                brief, fallback_hits = "", []

            if brief:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": brief,
                    }
                }
                sys.stdout.write(json.dumps(output))
                try:
                    from cortex.session import log_event

                    log_event(
                        session_id,
                        "keyword_fallback",
                        {
                            "n_hits": len(fallback_hits),
                            "tripwire_ids": [h["id"] for h in fallback_hits],
                            "scores": [
                                h.get("_fallback_score", 0.0) for h in fallback_hits
                            ],
                            "prompt_frustration": round(frustration, 3),
                        },
                    )
                except Exception:
                    pass
            return 0

        brief = render_brief(result)
        if not brief:
            return 0

        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": brief,
            }
        }
        sys.stdout.write(json.dumps(output))

        # Audit: record the injection for Day-4 DMN accounting.
        try:
            from cortex.session import log_event

            log_event(
                session_id,
                "inject",
                {
                    "matched_rules": result["matched_rules"],
                    "tripwire_ids": [t["id"] for t in result["tripwires"]],
                    "synthesis_ids": [s["id"] for s in result.get("synthesis") or []],
                    "verifier_ids": [
                        v.get("tripwire_id", "")
                        for v in result.get("verifier_results") or []
                    ],
                    "blocked": should_block,
                    "prompt_frustration": round(frustration, 3),
                    "touched_files_matched": result.get(
                        "touched_files_matched", []
                    ),
                },
            )
            if should_block:
                log_event(
                    session_id,
                    "verifier_blocked",
                    {
                        "failed_tripwires": [
                            v.get("tripwire_id", "")
                            for v in result.get("verifier_results") or []
                            if v.get("passed") is False
                        ],
                    },
                )
        except Exception:
            pass

        # Day 10: block the prompt by returning exit code 2 after the
        # brief has been emitted. Claude Code treats non-zero from
        # UserPromptSubmit as "reject this prompt". The brief with the
        # FAIL marker still reaches the user's context so they can see
        # WHY the prompt was blocked.
        if should_block:
            return 2
        return 0
    except Exception:
        # Fail open: never break the user prompt path.
        return 0


def _verify_block_enabled() -> bool:
    import os as _os

    return _os.environ.get("CORTEX_VERIFY_BLOCK") == "1"


if __name__ == "__main__":
    sys.exit(main())
