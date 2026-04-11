"""Phase 0 fitness scoring tests."""
from cortex.fitness import (
    FRUSTRATION_THRESHOLD,
    W_CAUGHT,
    W_FRUSTRATION,
    W_IGNORED,
    W_SURPRISE,
    compute_fitness,
    match_surprise_to_tripwires,
    render_fitness_block,
    score_prompt_frustration,
)


# --------------------------------------------------------------------
# score_prompt_frustration
# --------------------------------------------------------------------


def test_frustration_neutral_prompt_is_zero():
    assert score_prompt_frustration("добавь fitness в stats") == 0.0
    assert score_prompt_frustration("please add fitness to stats") == 0.0


def test_frustration_russian_corrective_words_score_positive():
    assert score_prompt_frustration("нет, ты сломал") > 0.0
    assert score_prompt_frustration("откати последний коммит") > 0.0


def test_frustration_english_corrective_words_score_positive():
    assert score_prompt_frustration("no, revert that change") > 0.0
    assert score_prompt_frustration("undo it and try again") > 0.0


def test_frustration_saturates_at_one():
    loud = "нет, откати, сломал, верни, не работает, зачем"
    assert score_prompt_frustration(loud) == 1.0


def test_frustration_only_scans_head():
    """A normal prompt with 'no' deep in the middle should stay neutral."""
    prompt = "implement a new feature " * 20 + "there is no way this will break"
    assert score_prompt_frustration(prompt) == 0.0


def test_frustration_empty_and_none_safe():
    assert score_prompt_frustration("") == 0.0
    assert score_prompt_frustration(None) == 0.0  # type: ignore[arg-type]


# --------------------------------------------------------------------
# match_surprise_to_tripwires
# --------------------------------------------------------------------


def test_surprise_match_requires_minimum_overlap():
    bodies = {
        "fee_rule": "Polymarket fee formula 0.072 times min price times size",
    }
    # Only one content token ("fee") overlaps -- below min_overlap=3.
    assert match_surprise_to_tripwires("fee", bodies) == []


def test_surprise_match_detects_real_overlap():
    bodies = {
        "real_entry_price": (
            "Use real up_ask dn_ask entry never midpoint fiction inflation"
        ),
        "unrelated_rule": "git branches and commit messages rebasing",
    }
    fm = "agent used midpoint fiction instead of real entry ask"
    hits = match_surprise_to_tripwires(fm, bodies)
    assert "real_entry_price" in hits
    assert "unrelated_rule" not in hits


def test_surprise_match_ignores_stopwords():
    """'the and or but' share 4 stopwords -- must NOT count."""
    bodies = {"x": "the and or but"}
    fm = "the and or but"
    assert match_surprise_to_tripwires(fm, bodies) == []


def test_surprise_empty_failure_mode_returns_empty():
    assert match_surprise_to_tripwires("", {"x": "some body text here"}) == []


# --------------------------------------------------------------------
# compute_fitness: the core aggregator
# --------------------------------------------------------------------


def _inject(tripwire_ids: list[str], frustration: float = 0.0) -> dict:
    return {
        "event": "inject",
        "matched_rules": ["r"],
        "tripwire_ids": tripwire_ids,
        "prompt_frustration": frustration,
    }


def _violation(tw_id: str) -> dict:
    return {"event": "potential_violation", "tripwire_id": tw_id}


def _prediction(failure_mode: str) -> dict:
    return {"event": "prediction", "outcome": "ok", "failure_mode": failure_mode}


def test_compute_fitness_empty_sessions_returns_empty():
    assert compute_fitness([]) == {}


def test_compute_fitness_caught_only():
    import math

    sessions = [("s1", [_inject(["t1"])])]
    fit = compute_fitness(sessions, tripwire_costs={"t1": 100.0})
    assert fit["t1"]["hits"] == 1
    assert fit["t1"]["caught"] == 1
    assert fit["t1"]["ignored"] == 0
    expected_cw = round(math.log1p(100.0) * 0.01, 3)
    assert fit["t1"]["cost_weight"] == expected_cw
    # fitness = 1.0*1 + cost_weight
    assert fit["t1"]["fitness"] == round(1.0 + expected_cw, 3)


def test_compute_fitness_ignored_negative():
    sessions = [("s1", [_inject(["t1"]), _violation("t1")])]
    fit = compute_fitness(sessions, tripwire_costs={"t1": 500.0})
    assert fit["t1"]["caught"] == 0
    assert fit["t1"]["ignored"] == 1
    # No cost weight when ignored (cost only credits caught hits).
    assert fit["t1"]["cost_weight"] == 0.0
    # fitness = -2.0 * 1
    assert fit["t1"]["fitness"] == -2.0


def test_compute_fitness_surprise_confirmation():
    bodies = {"t1": "use real entry price ask never midpoint fiction inflation"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction("forgot real entry price and used midpoint"),
            ],
        )
    ]
    fit = compute_fitness(sessions, tripwire_bodies=bodies)
    assert fit["t1"]["surprise_ok"] == 1
    # caught + surprise = 1.0 + 0.5
    assert fit["t1"]["fitness"] == 1.5


def test_compute_fitness_frustration_penalty_from_next_inject():
    """Next inject's frustration score attributes back to previous tripwires."""
    sessions = [
        (
            "s1",
            [
                _inject(["t1"], frustration=0.0),
                _inject(["t2"], frustration=0.9),  # user is mad at next prompt
            ],
        )
    ]
    fit = compute_fitness(sessions)
    # t1 should be penalized (it's what was in the brief before the frustrated prompt)
    assert fit["t1"]["frustration"] == 1
    # t2 has no following inject, so no frustration attribution
    assert fit["t2"]["frustration"] == 0


def test_compute_fitness_frustration_below_threshold_ignored():
    sessions = [
        (
            "s1",
            [
                _inject(["t1"], frustration=0.0),
                _inject(["t2"], frustration=FRUSTRATION_THRESHOLD - 0.1),
            ],
        )
    ]
    fit = compute_fitness(sessions)
    assert fit["t1"]["frustration"] == 0


def test_compute_fitness_multi_session_aggregates_correctly():
    import math

    sessions = [
        ("s1", [_inject(["t1"])]),  # caught
        ("s2", [_inject(["t1"]), _violation("t1")]),  # ignored
        ("s3", [_inject(["t1"])]),  # caught
    ]
    fit = compute_fitness(sessions, tripwire_costs={"t1": 10.0})
    assert fit["t1"]["hits"] == 3
    assert fit["t1"]["caught"] == 2
    assert fit["t1"]["ignored"] == 1
    expected_cw = round(2 * math.log1p(10.0) * 0.01, 3)
    assert fit["t1"]["cost_weight"] == expected_cw
    # fitness = 1.0*2 - 2.0*1 + cost_weight = 0 + cw
    assert abs(fit["t1"]["fitness"] - expected_cw) < 1e-6


def test_compute_fitness_ignores_tripwire_without_id_in_violation():
    sessions = [
        (
            "s1",
            [
                _inject(["t1", "t2"]),
                _violation("t2"),
            ],
        )
    ]
    fit = compute_fitness(sessions)
    assert fit["t1"]["caught"] == 1
    assert fit["t1"]["ignored"] == 0
    assert fit["t2"]["caught"] == 0
    assert fit["t2"]["ignored"] == 1


def test_compute_fitness_violation_window_bounded_by_next_inject():
    """A violation AFTER the next inject must not attribute to the first inject."""
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _inject(["t2"]),  # window for t1 ends here
                _violation("t1"),  # this happens after t2's inject
            ],
        )
    ]
    fit = compute_fitness(sessions)
    # t1 was caught in its own window (no violation before the next inject)
    assert fit["t1"]["caught"] == 1
    assert fit["t1"]["ignored"] == 0
    # t2 has a window containing the violation for t1, but t1 is not in t2's
    # inject, so t2 is not affected
    assert fit["t2"]["caught"] == 1
    assert fit["t2"]["ignored"] == 0


def test_compute_fitness_keyword_fallback_counts_as_injection_point():
    """Fallback hits should be scored just like primary injects."""
    sessions = [
        (
            "s1",
            [
                {
                    "event": "keyword_fallback",
                    "tripwire_ids": ["t1"],
                    "prompt_frustration": 0.0,
                },
            ],
        )
    ]
    fit = compute_fitness(sessions)
    assert fit["t1"]["hits"] == 1
    assert fit["t1"]["caught"] == 1


def test_compute_fitness_weights_constants_match_docstring():
    """Guard against someone changing weights without updating the formula."""
    assert W_CAUGHT == 1.0
    assert W_IGNORED == -2.0
    assert W_SURPRISE == 0.5
    assert W_FRUSTRATION == -0.3


# --------------------------------------------------------------------
# render_fitness_block
# --------------------------------------------------------------------


def test_render_fitness_block_empty_returns_empty_list():
    assert render_fitness_block({}) == []


def test_render_fitness_block_includes_formula_footer():
    fit = {
        "t1": {
            "hits": 5,
            "caught": 4,
            "ignored": 1,
            "surprise_ok": 2,
            "frustration": 0,
            "cost_weight": 0.5,
            "fitness": 3.0,
        }
    }
    out = "\n".join(render_fitness_block(fit))
    assert "t1" in out
    assert "fit=+3.00" in out
    assert "caught" in out
    assert "Strongly negative" in out


def test_render_fitness_block_sorts_descending_by_fitness():
    fit = {
        "low": {
            "hits": 1,
            "caught": 0,
            "ignored": 1,
            "surprise_ok": 0,
            "frustration": 0,
            "cost_weight": 0.0,
            "fitness": -2.0,
        },
        "high": {
            "hits": 1,
            "caught": 1,
            "ignored": 0,
            "surprise_ok": 0,
            "frustration": 0,
            "cost_weight": 0.0,
            "fitness": 1.0,
        },
    }
    out = render_fitness_block(fit)
    high_line = next(i for i, l in enumerate(out) if "high" in l)
    low_line = next(i for i, l in enumerate(out) if "low" in l)
    assert high_line < low_line


# --------------------------------------------------------------------
# Day 16: classification_index override vs heuristic fallback
# --------------------------------------------------------------------


def _prediction_at(failure_mode: str, at: str) -> dict:
    return {
        "event": "prediction",
        "outcome": "ok",
        "failure_mode": failure_mode,
        "at": at,
    }


def test_compute_fitness_classification_mismatch_overrides_heuristic():
    """A Haiku-labelled mismatch for a specific (session, at) pair
    should contribute surprise_ok += 1.0 to every tripwire in the
    surrounding inject AND increment the mismatches counter, while
    skipping the token-overlap heuristic for that pair."""
    bodies = {"t1": "completely unrelated body text about widgets"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction_at(
                    # Deliberately NO vocabulary overlap with the body
                    # above, so the heuristic would otherwise return 0.
                    "totally different wording with nothing matching",
                    at="2026-04-11T12:00:00+00:00",
                ),
            ],
        )
    ]
    cls_index = {("s1", "2026-04-11T12:00:00+00:00"): "mismatch"}
    fit = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        classification_index=cls_index,
    )
    assert fit["t1"]["surprise_ok"] == 1.0
    assert fit["t1"]["mismatches"] == 1
    # caught (1.0) + surprise (0.5 * 1.0) = 1.5
    assert fit["t1"]["fitness"] == 1.5


def test_compute_fitness_classification_partial_contributes_half():
    bodies = {"t1": "xyz"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction_at("anything", at="2026-04-11T12:00:00+00:00"),
            ],
        )
    ]
    cls_index = {("s1", "2026-04-11T12:00:00+00:00"): "partial"}
    fit = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        classification_index=cls_index,
    )
    assert fit["t1"]["surprise_ok"] == 0.5
    assert fit["t1"]["mismatches"] == 0
    # caught (1.0) + surprise (0.5 * 0.5) = 1.25
    assert fit["t1"]["fitness"] == 1.25


def test_compute_fitness_classification_match_suppresses_surprise():
    """A `match` label contributes 0 surprise even if the heuristic
    would otherwise have fired. This is the key claim: classification
    REPLACES, not sums."""
    bodies = {"t1": "use real entry price ask never midpoint fiction inflation"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction_at(
                    # Would match the heuristic (3+ tokens overlap).
                    "forgot real entry price and used midpoint fiction",
                    at="2026-04-11T12:00:00+00:00",
                ),
            ],
        )
    ]
    cls_index = {("s1", "2026-04-11T12:00:00+00:00"): "match"}
    fit = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        classification_index=cls_index,
    )
    assert fit["t1"]["surprise_ok"] == 0.0
    assert fit["t1"]["mismatches"] == 0
    # Only caught contributes. Heuristic was skipped.
    assert fit["t1"]["fitness"] == 1.0


def test_compute_fitness_classification_error_label_is_inert():
    bodies = {"t1": "real entry price midpoint inflation"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction_at(
                    "real entry price midpoint inflation story",
                    at="2026-04-11T12:00:00+00:00",
                ),
            ],
        )
    ]
    cls_index = {("s1", "2026-04-11T12:00:00+00:00"): "error"}
    fit = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        classification_index=cls_index,
    )
    # Error label suppresses both the heuristic (we still skip it)
    # and any positive signal, so surprise_ok stays at 0.
    assert fit["t1"]["surprise_ok"] == 0.0
    assert fit["t1"]["fitness"] == 1.0


def test_compute_fitness_unclassified_pair_falls_back_to_heuristic():
    """Regression guard: when classification_index is missing or empty,
    behavior must be bit-identical to Day 14 (token-overlap heuristic
    fires exactly as before)."""
    bodies = {"t1": "use real entry price ask never midpoint fiction inflation"}
    sessions = [
        (
            "s1",
            [
                _inject(["t1"]),
                _prediction_at(
                    "forgot real entry price and used midpoint",
                    at="2026-04-11T12:00:00+00:00",
                ),
            ],
        )
    ]
    # No classification_index -> heuristic fires
    fit_day14 = compute_fitness(sessions, tripwire_bodies=bodies)
    assert fit_day14["t1"]["surprise_ok"] == 1.0
    assert fit_day14["t1"]["fitness"] == 1.5
    # Empty dict -> same result
    fit_empty = compute_fitness(
        sessions, tripwire_bodies=bodies, classification_index={}
    )
    assert fit_empty["t1"]["surprise_ok"] == 1.0
    assert fit_empty["t1"]["fitness"] == 1.5
    # Classification for a DIFFERENT pair leaves this one untouched.
    fit_other = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        classification_index={("sX", "zzz"): "mismatch"},
    )
    assert fit_other["t1"]["surprise_ok"] == 1.0


def test_compute_fitness_tracks_distinct_sessions():
    sessions = [
        ("s1", [_inject(["t1"])]),
        ("s2", [_inject(["t1"])]),
        ("s1", [_inject(["t1"])]),  # same session id as first
    ]
    fit = compute_fitness(sessions)
    # 3 hits but only 2 distinct session ids.
    assert fit["t1"]["hits"] == 3
    assert fit["t1"]["distinct_sessions"] == 2
    # session_ids set should be stripped from the returned row.
    assert "session_ids" not in fit["t1"]
