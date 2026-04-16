"""Seed the cortex store with tripwires distilled from BOTWA MEMORY.md.

Each seed entry is hand-compressed to 3-6 short paragraphs with WHY + HOW TO
APPLY so that hook-time injection stays cheap. Source files referenced under
`source_file` live in the user's auto-memory directory.

This is NOT a scraper. Re-running `cortex migrate` is idempotent (upsert)
and preserves accumulated `violation_count` / `last_violated_at` stats on
existing tripwires.

Day-1 seed: 11 tripwires spanning two domains (polymarket, generic).
10 came from MEMORY.md files directly; one (lag_arb_slippage_kill) was
surfaced via Palace semantic search during Day-1 dog-food validation.
"""
from __future__ import annotations

from cortex.store import CortexStore

SEED_TRIPWIRES: list[dict] = [
    {
        "id": "poly_fee_empirical",
        "title": "Polymarket net fee = 0.072 x min(p, 1-p) x size  -  NOT 10% flat",
        "severity": "critical",
        "domain": "polymarket",
        "triggers": [
            "poly", "polymarket", "fee", "backtest", "taker", "maker",
            "pnl", "mid-price", "mid_price", "clob", "base_fee",
        ],
        "body": (
            "Polymarket net taker fee follows formula `fee_shares = 0.072 * min(p, 1-p) * size`.\n"
            "At mid prices (p=0.5) net fee ~3.6% per side. At extremes (p>0.95) net fee <0.4%.\n"
            "The `base_fee=1000` flag from /fee-rate is a GROSS rate -- ~60% is rebated to takers\n"
            "via fee collector 0xe3f18a. Maker Rebates Program pays liquidity providers 20% of the\n"
            "taker fee pool (source: Palace research/trun_da69...json).\n"
            "\n"
            "Why: empirical on-chain decode of a $2.28 round-trip on 2026-04-11 confirmed 3.10% per\n"
            "side at p=0.57 and 0.072% at p=0.99 -- linear formula verified at both endpoints.\n"
            "\n"
            "How to apply: (1) Never assume 10% flat in PnL models. (2) Mid-price classical alpha\n"
            "is DEAD -- 3% per side kills any <5pp edge. (3) Prefer extreme-price structural paths\n"
            "(late-lock p>0.95, tail-MM, settlement arb). (4) For paper logs saying 'fee=1000 bps',\n"
            "subtract empirical fee before interpretation."
        ),
        "verify_cmd": (
            "BOT/target/release/place_test.exe --asset btc --side UP --market --size 4 --flip-close"
        ),
        "cost_usd": 500.0,
        "source_file": "project_poly_fee_empirical.md",
    },
    {
        "id": "lookahead_parquet",
        "title": "Feature parquets must be computable STRICTLY before decision time",
        "severity": "critical",
        "domain": "generic",
        "triggers": [
            "backtest", "parquet", "features", "slot", "lookahead",
            "replay", "walkforward", "detector",
        ],
        "body": (
            "A feature is HONEST only if computable from data with timestamps strictly BEFORE the\n"
            "decision time. Before trusting any feature parquet, grep for `slot_ts = (ts // N) * N`.\n"
            "This pattern floors the bar OPEN time, so any computed value (ret, vol, ratios) is for\n"
            "the window AFTER slot_ts -- pure lookahead.\n"
            "\n"
            "Why: feature pipelines had this bug. Bots deployed live with backtests showing\n"
            "near-100% WR; real WR was ~70%, auto-killed on 3rd consecutive loss within the hour.\n"
            "\n"
            "How to apply: (1) Before any backtest, run a SHIFT TEST -- replay with the feature\n"
            "shifted back one slot. If WR drops >10pp, it contains lookahead. (2) 100% WR on >100\n"
            "trades is ALWAYS suspect -- first hypothesis must be lookahead, not 'edge so strong\n"
            "it is perfect'. (3) Walk-forward does NOT save you -- the hold-out has the same bug.\n"
            "(4) Prefer honest tiers from AUTOPOLY/prepare.py (dir_early_pct) over DETECTOR parquets."
        ),
        # Day 7: auto-run when CORTEX_VERIFY_ENABLE=1. cortex-check-lookahead
        # exits 0 gracefully if DETECTOR/ does not exist in CWD, so this is
        # safe to ship as a seed default even for users whose layout differs.
        "verify_cmd": "cortex-check-lookahead --features-dir DETECTOR",
        "cost_usd": 0.0,
        "source_file": "feedback_lookahead_in_features_parquet.md",
        # Day 6: detect the bare `(ts // N) * N` pattern without forward shift.
        # Same logic as cortex/verifiers/check_feature_lookahead.py but applied
        # to runtime tool_input rather than static code scans. Permissive between
        # `slot_ts` and `=` to handle bracketed forms like df['slot_ts'] = ...
        # The trailing \b forces \d+ to match the full integer -- without it,
        # the regex engine can backtrack to a single digit, hiding the
        # subsequent ` + 300` from the negative lookahead.
        "violation_patterns": [
            r"slot_ts[^\n]*?=[^\n]*?//\s*\d+[^\n]*?\*\s*\d+\b(?!\s*\+)",
        ],
        "affected_files": [
            "*features*.py", "*feature*.py", "*_parquet.py",
            "*prepare.py", "*prepare_*.py", "*slot*.py",
        ],
    },
    {
        "id": "directional_5m_dead",
        "title": "Directional 5m Polymarket is structurally dead (sum drag ~19.7pp)",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "5m", "5-min", "slot", "directional", "poly", "replay",
            "signal", "prediction",
        ],
        "body": (
            "Directional prediction on BTC/ETH/SOL 5m Polymarket slots is structurally dead on\n"
            "current data (<=14 days, 36% slot coverage). Sum structural drag per trade ~19.7pp:\n"
            "spread+slip 2.4pp, info decay 1.45pp/min x5, adverse selection 10pp. Any directional\n"
            "hypothesis needs pre-fee edge > 20pp to be testable.\n"
            "\n"
            "Why: 2026-04-11 full-day session, 9 honest replay tests across 6 hypothesis families\n"
            "(trend / vol / fut_obi / liq / fvg / exhaustion) on PIT-healed parquets. 0 survivors\n"
            "under Bonferroni z>=3.03. Wave Context (1h trend follow) = z=-2.19 loser -- the\n"
            "stronger the 1h trend, the more MM prices it into up_mid before slot_ts.\n"
            "\n"
            "How to apply: (1) Don't retest directional on the same 6-day data. (2) Path forward is\n"
            "STRUCTURAL: MM / late-lock / settlement arb / whale copy -- not prediction. (3) Unblocks\n"
            "after slot recorder hits 30+ days at >=80% coverage. (4) If building a non-directional\n"
            "play, acknowledge with --cortex-ack=structural."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "project_directional_5m_dead.md",
    },
    {
        "id": "adverse_selection_maker",
        "title": "Maker buys on 5m Polymarket lose 10-14pp WR to Winner's Curse",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "maker", "limit", "fill", "poly", "5m", "pierce", "inside-spread",
        ],
        "body": (
            "Maker limit buys on Polymarket 5m binary markets lose 10-14pp of WR to Winner's Curse\n"
            "adverse selection -- direction-symmetric, does NOT invert when you flip signal.\n"
            "Apparent spread savings (~2.5pp) are eaten 4-5x by adverse fills.\n"
            "\n"
            "Why: 2026-04-11 test on H6 CONT (bet same direction as big ret_5m_bps). Taker at t=5\n"
            "edge=-0.35pp (break-even). Maker pierce-below edge=-10.58pp z=-4.12. Flipped to H6 REV\n"
            "inside-spread maker: still -11.83pp. Mechanism: a maker BUY fills only when someone\n"
            "aggressively SELLS -- and that seller has short-term info that the market is moving\n"
            "AGAINST your direction.\n"
            "\n"
            "How to apply: (1) Never propose maker-entry as a fix for taker-negative edges -- it\n"
            "makes them worse. (2) Taker at t=5 no-slip is the HONEST floor -- the signal's true\n"
            "edge. (3) Viable maker plays are ONLY non-directional: extreme-price ladders\n"
            "(LATE_LOCK p>0.95), symmetric MM with queue priority. Never directional."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_adverse_selection_maker.md",
    },
    {
        "id": "information_decay_5m",
        "title": "Enter at t=5s -- each minute of delay costs ~1.45pp edge",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "5m", "slot", "entry", "timing", "poly", "t=5", "t=60",
        ],
        "body": (
            "Same signal on Polymarket 5m loses ~1.45pp of edge per minute of entry delay. Default\n"
            "entry time for any new strategy = t=5s (earliest row where book is populated).\n"
            "\n"
            "Why: 2026-04-11 naive baseline on BTC+ETH+SOL. T2 CHEAPER_SIDE at t=5: edge -1.34pp.\n"
            "T4 at t=60 (same signal, 55s later): edge -2.79pp. WR collapsed 44% -> 33%. MMs\n"
            "process slot-open info within first 10-20s; by t=60 the signal is already in the mid.\n"
            "\n"
            "How to apply: (1) Any new strategy defaults to t=5 entry. (2) If you need intra-slot\n"
            "observation (gap fill etc), enter IMMEDIATELY after the window -- don't wait 'for\n"
            "confirmation'. (3) L3 replay at t=60 adds a systematic -1.5pp handicap that masks\n"
            "real signal quality. (4) Nuance: optimal window varies by signal type -- large volume\n"
            "bursts 0-5s, L1-L20 OBI 5-30s. t=5 is the safe default."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_information_decay_5m.md",
    },
    {
        "id": "late_lock_replay_traps",
        "title": "Late-lock replay has 3 traps: asymmetric risk, stale asks, volume illusion",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "late_lock", "late-lock", "replay", "favorite", "extreme",
            "tail", "resolution",
        ],
        "body": (
            "Late-lock replay (buy favorites near resolution, p>0.95) has three structural traps\n"
            "that inflate paper PnL vs reality. A late-lock replay is trustworthy ONLY if the\n"
            "report addresses all three:\n"
            "\n"
            "Trap 1 -- ASYMMETRIC RISK: buying at $0.97 earns gross $0.028 per share, loss costs\n"
            "$0.97. Break-even WR ~97.1%. WR must strictly exceed entry_price + fee wedge. A single\n"
            "loss erases 30-40 wins.\n"
            "\n"
            "Trap 2 -- ADVERSE SELECTION ON STALE ASKS: if an ask is still resting at $0.97 in the\n"
            "last 30s of a slot, the MM hasn't pulled because Binance moved adversely -- you are\n"
            "hitting losers, not winners. Filter or segment by sign(bin_ret_last_10s).\n"
            "\n"
            "Trap 3 -- VOLUME ILLUSION: tail books hold $5-20 at top-of-book, not $500. Use real\n"
            "up_ask0_sz from slot parquets; never extrapolate TARGET_SIZE.\n"
            "\n"
            "Required reporting: break-even WR line, real avg fill size, WR segmented by bin\n"
            "momentum sign, honest fee formula 0.072*min(p,1-p)*size."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_late_lock_replay_traps.md",
        "affected_files": [
            "late_lock*.rs", "late_lock*.py",
            "replay_late_lock*.py", "*late_lock*.rs", "*late_lock*.py",
        ],
    },
    {
        "id": "never_single_strategy",
        "title": "NEVER deploy a single strategy live -- minimum 5 uncorrelated",
        "severity": "critical",
        "domain": "polymarket",
        "triggers": [
            "live", "deploy", "bot", "portfolio", "single", "diversify",
        ],
        "body": (
            "NEVER deploy a single strategy live. Minimum 5 uncorrelated strategies from different\n"
            "signal families, with slot budget capping total exposure.\n"
            "\n"
            "Why: single strategy = one saw-tooth curve; one regime shift wipes out weeks of gains.\n"
            "Lost $57 in 2 days on sequential single-strategy deployments (sol_midrange ->\n"
            "s7_polyflow -> e_temporal -> btc_eth_lead_pro -> flat_dn). The same strategies in a\n"
            "15-strategy paper portfolio returned +$71 on 432 trades because losses were offset\n"
            "by other strategies' wins.\n"
            "\n"
            "How to apply: (1) Minimum 5 strategies from different signal families. (2) 2 shares\n"
            "each, not 3+. (3) Slot budget caps total exposure per slot to <15% of bankroll.\n"
            "(4) Collect 200+ paper trades per strategy before ANY live deployment."
        ),
        "verify_cmd": None,
        "cost_usd": 57.0,
        "source_file": "feedback_never_single_strategy.md",
    },
    {
        "id": "no_budget_paper",
        "title": "NEVER set max_slot_cost or max_concurrent on paper trading",
        "severity": "high",
        "domain": "generic",
        "triggers": [
            "paper", "budget", "max_slot_cost", "max_concurrent",
            "survivorship",
        ],
        "body": (
            "NEVER set max_slot_cost or max_concurrent_positions on paper trading. Budget on paper\n"
            "= poisoned evaluation data -- survivorship bias in reverse.\n"
            "\n"
            "Why: Derby V2 had $5/slot budget. Fast strategies (lag_arb, flat_dn) ate budget first.\n"
            "Slow but higher-edge strategies (e_family t=60-90s, upup/dndn t=120s) got systematically\n"
            "starved. Backfill of 6,317 blocked trades: WR=53.2%, PnL=+$201 -- the BLOCKED trades\n"
            "were MORE profitable than those that got through. e_lob_only flipped from -$25 to +$23\n"
            "when unblocked.\n"
            "\n"
            "How to apply: paper config MUST have max_slot_cost=9999 and max_concurrent_positions=999.\n"
            "The entire purpose of paper is data collection; caps only belong in live. If a paper\n"
            "report shows 'budget exceeded X times', re-run without caps before interpreting any\n"
            "PnL number."
        ),
        "verify_cmd": None,
        "cost_usd": 201.0,
        "source_file": "feedback_no_budget_paper.md",
    },
    {
        "id": "backtest_must_match_prod",
        "title": "Backtest MUST use EXACT production parameters -- caps, risk, limits",
        "severity": "critical",
        "domain": "generic",
        "triggers": [
            "backtest", "prod", "config", "live", "validate", "cap",
            "risk", "position",
        ],
        "body": (
            "Every backtest MUST use EXACT production parameters -- especially position caps, risk%,\n"
            "max trades per day. Never silently change caps between backtest and prod.\n"
            "\n"
            "Why: showed user a backtest with MAX_POSITION_SOL=50 (+1,875%) when prod had\n"
            "MAX_POSITION_SOL=5 (real +473%) -- 7x overstatement. The user makes financial decisions\n"
            "on these numbers; inflation is dangerous and dishonest.\n"
            "\n"
            "How to apply: (1) Before ANY backtest, READ prod config.py first. (2) Use EXACT same\n"
            "values for RISK_PCT, MAX_POSITION, MAX_TRADES_PER_DAY, and ALL other params. (3) If\n"
            "testing alternative configs, label them clearly 'PROD' vs 'HYPOTHETICAL'. (4) When\n"
            "user asks 'does backtest match prod?', verify ALL parameters, not just strategy params."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_backtest_must_match_prod.md",
        "affected_files": [
            "*backtest*.py", "replay_*.py", "*_replay.py",
            "config.py", "*strategies*.json", "*strategies*.toml",
        ],
    },
    {
        "id": "real_entry_price",
        "title": "Use real up_ask/dn_ask entry -- never $0.50 midpoint (33x inflation)",
        "severity": "critical",
        "domain": "polymarket",
        "triggers": [
            "backtest", "entry", "poly", "pnl", "replay", "ask", "bid",
            "midpoint", "mid",
        ],
        "body": (
            "In ALL Polymarket directional backtests, compute PnL using real up_ask/dn_ask at the\n"
            "actual entry time -- NEVER the $0.50 midpoint fiction. The $0.50 assumption inflates\n"
            "PnL by up to 33x.\n"
            "\n"
            "Why: 1590 5m slots on 2026-04-10. count_late_55 (t=180) at $0.50 entry showed +$706.95\n"
            "at 82.7% WR. Recomputed with real up_ask_180/dn_ask_180: +$21.15 (33x haircut).\n"
            "count_t180_60/40 at 84.5% WR flipped from great to -$0.62. Confidence trap: margin\n"
            "0.10-0.15 = +$0.079/bet (best), margin >=0.20 = -$0.013/bet -- high conviction = price\n"
            "already moved = entry eats the edge.\n"
            "\n"
            "How to apply: (1) Read up_best_ask/dn_best_ask at the decision t-mark. (2) pnl =\n"
            "(1/entry - 1) if win else -1. (3) Reject any strategy that looks great at $0.50 entry\n"
            "but weak/negative at real entry. (4) ALWAYS include an honest-entry column alongside\n"
            "theoretical. (5) Real PnL <30% of theoretical = strategy is lookahead-trapped."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_real_entry_price.md",
        # Day 6: detect assignments that hard-code 0.50 as entry price in backtest code.
        "violation_patterns": [
            r"entry(_price)?\s*=\s*0\.50?\b",
            r"(up_ask|dn_ask|up_mid|dn_mid)\s*=\s*0\.50?\b",
        ],
    },
    {
        "id": "lag_arb_slippage_kill",
        "title": "Binance->Polymarket lag-arb: gross 89% WR but slippage+fills kill it",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "lag-arb", "lag_arb", "binance", "lead-lag", "slippage",
            "fill-rate", "forward",
        ],
        "body": (
            "Binance->Polymarket lag-arb appears to work at peak 89% WR at 1500ms forward delay, but\n"
            "every realistic execution scenario with non-zero slippage and <100% fill rate turns\n"
            "negative. The gross mid-to-mid edge is real (+$0.09-0.20/trade at 1bp BTC moves) --\n"
            "spread and slip eat ALL of it.\n"
            "\n"
            "Why: 8-hour BTC probe showed BTC 1-second move >=1bp -> forward PM WR 46% at 500ms,\n"
            "72% at 1000ms, 89% at 1500ms peak. Decomposition on 5-share trades: gross +$0.25,\n"
            "entry spread -$0.025, exit spread -$0.025. 1c slippage alone = -$30/day; 'bad day'\n"
            "scenario = -$32.89. Dead in every realistic scenario.\n"
            "\n"
            "How to apply: (1) Don't propose naive lag-arb -- the gross edge is well-known and\n"
            "eaten by structure. (2) Any lag-arb variant must include explicit spread + slippage\n"
            "+ fill-rate modeling in the backtest -- theoretical edge is irrelevant. (3) Maker-side\n"
            "lag-arb inherits `adverse_selection_maker` -- combined verdict = dead."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "Palace: execution/LAG_EXECUTION_REALISM.md + data/LAG_ARB_FULL.md",
    },
    {
        "id": "no_live_param_change_without_bt",
        "title": "NEVER change live trading params without 100% WR backtest proof",
        "severity": "critical",
        "domain": "polymarket",
        "triggers": [
            "live", "change", "modify", "param", "threshold", "price_min",
            "gate", "adjust", "loosen", "tighten", "rebuild", "deploy",
        ],
        "body": (
            "NEVER modify live trading parameters, thresholds, or strategy code without first\n"
            "proving 100% WR on backtest replay with the proposed changes. Dry spells (no trades\n"
            "for 1+ hours) are normal market behavior -- the strategy's selectivity IS the edge.\n"
            "\n"
            "Why: On 2026-04-12, agent observed a 1-hour dry spell and immediately attempted to\n"
            "lower price_min from 0.99 to 0.98 for XRP/HYPE and inject debug logging into live\n"
            "strategy code -- all without any backtest validation. User caught it before binary\n"
            "rebuild. Loosening params without proof risks turning 100% WR into losers.\n"
            "\n"
            "How to apply: (1) If no trades for extended period, diagnose via logs but DO NOT\n"
            "change params -- dry spells are expected. (2) Before ANY param or code change to live\n"
            "bot, first run replay_late_lock.py with proposed params and confirm WR=100% at\n"
            "meaningful n. (3) Never rebuild or restart live binary without explicit user approval\n"
            "AND backtest proof."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_no_live_changes_without_backtest.md",
    },
    {
        "id": "boundary_vs_grid_search",
        "title": "Boundary analysis (1-axis monotonic) beats grid search for live tuning",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "tune", "tuning", "sweep", "param", "grid", "search", "optimize",
            "100", "wr", "wilson", "overfit", "bonferroni",
        ],
        "body": (
            "When proposing live trading parameter changes, prefer single-axis BOUNDARY ANALYSIS\n"
            "(monotonic widening of one parameter) over multi-dimensional GRID SEARCH. Grid search\n"
            "creates false 100% WR pockets via multiplicity; boundary analysis with stable WR\n"
            "plateau is structural evidence.\n"
            "\n"
            "Why: 2026-04-12 grid of 240 configs/asset (4 t_min x 3 price_min x 5 gate x 4 depth)\n"
            "found '100% WR pockets' on n=15-30 — but Bonferroni demands alpha/240 ~ 0.0002, and\n"
            "Wilson LB at n=24 is only ~93% vs BE ~98.7%. Same day, single-axis t_max sweep 288 ->\n"
            "295 showed WR=100% holding monotonically at 288/290/292/294/295 with n growing\n"
            "234 -> 271 — clean structural edge. Plus 295 was the original code default.\n"
            "\n"
            "How to apply: (1) Boundary sweep OK to deploy when WR is monotonic across the range\n"
            "AND n grows AND there's a microstructure rationale. (2) Grid search results = OOS\n"
            "candidates only, never direct deploy. (3) Always compute Wilson LB for any 100% WR\n"
            "claim — if LB < BE, it's chance. (4) Suspect any 100% WR on n<50 from a search."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_boundary_vs_grid_search.md",
    },
    {
        "id": "ladder_killed_by_dip_rarity",
        "title": "Late-lock ladder/split DEAD: 10% dip rate, 90% no-dip slots dominate PnL",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "ladder", "grid", "split", "dip", "average", "down", "scaled",
            "limit", "maker", "late_lock", "лестница", "сетка",
        ],
        "body": (
            "DO NOT propose grid/ladder/split-entry strategies for late-lock without first\n"
            "measuring dip frequency in current data. The base IOC at $0.98+ is mathematically\n"
            "OPTIMAL when dips are rare.\n"
            "\n"
            "Why: 2026-04-12 user proposed splitting $45 base into $30 IOC + $15 ladder across\n"
            "0.97/0.96/0.95/0.92. Backtest on 279 entries showed ALL split configs lose vs\n"
            "current single $45: $30+ladder = $14.88/d vs current $20.44/d (-27%). Reason:\n"
            "dips happen in only ~10% of slots after gate=1.0 filter (26/271). In dominant\n"
            "90% no-dip slots, only base fills, and reducing base from $45 to $30 cuts profit\n"
            "per win from $0.92 to $0.61. The $0.30 loss x 245 non-dip wins outweighs the\n"
            "$0.28 gain x 26 dip wins. Dips are rare BECAUSE the filter selects quiet slots --\n"
            "ladder is testing the wrong distribution.\n"
            "\n"
            "How to apply: (1) Lead with dip frequency measurement before any ladder/split\n"
            "discussion. (2) If dips <30% of entries, splitting is mathematically inferior --\n"
            "kill the proposal immediately. (3) Addon model (extra capital on top, not split)\n"
            "shows +$3.52/d in 100% WR backtest but inflates tail risk ~80% in OOS losses --\n"
            "needs loss-distribution data we don't have."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_ladder_killed_by_dip_rarity.md",
    },
    {
        "id": "hope_bias_down_underpriced",
        "title": "Crypto 5m Polymarket: Down systematically underpriced ~5-10pp (Hope Bias)",
        "severity": "high",
        "domain": "polymarket",
        "triggers": [
            "down", "up", "side", "directional", "5m", "ladder", "asymmetric",
            "bias", "favorite", "underpriced", "polymarket",
        ],
        "body": (
            "On crypto 5-min directional Polymarket markets, Down side is systematically\n"
            "underpriced by ~5-10pp vs true probability due to crowd hope-bias (long-only\n"
            "crypto holder audience). Displayed Down at e.g. 50.5% has true probability\n"
            "closer to 58-62%. This creates structural alpha INDEPENDENT of late-lock spread.\n"
            "\n"
            "Why: 2026-04-12 user observation after watching SOL 5:30PM ET slot dip from 0.99\n"
            "to 0.505 mid-flight and resolve Down (won). Audience is overwhelmingly long-biased\n"
            "crypto holders who anchor on multi-year uptrend (recency), hold spot positions\n"
            "(loss aversion), follow bull narratives (mimicry), and treat Down bets as 'betting\n"
            "against my bag'. Live polybot data: 30/30 Down WR + 47/47 Up WR (both 100% at low\n"
            "n, can't yet distinguish, but structural reasoning predicts Down outperforms at\n"
            "large n).\n"
            "\n"
            "How to apply: (1) When deploying directional ladders, allocate MORE capital to\n"
            "Down-side slices than Up-side (asymmetric: $1.50 Down / $0.50 Up, same total).\n"
            "(2) Track per-side WR separately for first 100+ trades to validate. (3) Consider\n"
            "Down-only deep ladders. (4) Adjust BE: at entry P with bias_pp, true BE = P -\n"
            "bias_pp, not just P. (5) Don't apply HBH to tradfi (gold/stocks)."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "feedback_hope_bias_down_underpriced.md",
    },
]


SEED_COST_COMPONENTS: list[dict] = [
    {
        "id": "pm_5m_spread",
        "tripwire_id": "directional_5m_dead",
        "metric": "spread_slip",
        "value": 2.4,
        "unit": "pp",
        "sign": "drag",
    },
    {
        "id": "pm_5m_info_decay",
        "tripwire_id": "information_decay_5m",
        "metric": "info_decay_5min",
        "value": 7.25,
        "unit": "pp",
        "sign": "drag",
    },
    {
        "id": "pm_5m_adverse_sel",
        "tripwire_id": "adverse_selection_maker",
        "metric": "adverse_selection",
        "value": 10.0,
        "unit": "pp",
        "sign": "drag",
    },
]


SEED_SYNTHESIS_RULES: list[dict] = [
    {
        "id": "pm_5m_directional_block",
        "triggers": ["5m", "directional", "poly"],
        "sum_over": [
            "pm_5m_spread",
            "pm_5m_info_decay",
            "pm_5m_adverse_sel",
        ],
        "threshold": 5.0,
        "op": "gte",
        "message": (
            "Sum structural drag = {sum}pp ({n} components) >= {threshold}pp floor. "
            "Any directional 5m Polymarket strategy needs pre-fee edge > {sum}pp "
            "to even be testable. See directional_5m_dead for the full autopsy."
        ),
    },
]


def run_migration(db_path: str = ".cortex/store.db") -> int:
    """Seed the store with tripwires + cost components + synthesis rules.

    Returns the number of tripwires migrated. Idempotent: re-running preserves
    accumulated violation stats, overwrites body/triggers/cost with latest values.
    """
    store = CortexStore(db_path)
    try:
        for tw in SEED_TRIPWIRES:
            store.add_tripwire(**tw)
        for cc in SEED_COST_COMPONENTS:
            store.add_cost_component(**cc)
        for rule in SEED_SYNTHESIS_RULES:
            store.add_synthesis_rule(**rule)
        return len(SEED_TRIPWIRES)
    finally:
        store.close()


if __name__ == "__main__":
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else ".cortex/store.db"
    n = run_migration(db)
    print(f"Migrated {n} tripwires to {db}")
