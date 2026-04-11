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
import sys


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
        from cortex.verify_runner import run_verifiers_for

        result = classify_prompt(prompt)
        session_id = payload.get("session_id", "") or ""

        # Day 7: optional pre-flight verification for critical tripwires.
        # Opt-in via CORTEX_VERIFY_ENABLE=1. Fail-safe on any runner error.
        try:
            result["verifier_results"] = run_verifiers_for(result["tripwires"])
        except Exception:
            result["verifier_results"] = []

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
                },
            )
        except Exception:
            pass

        return 0
    except Exception:
        # Fail open: never break the user prompt path.
        return 0


if __name__ == "__main__":
    sys.exit(main())
