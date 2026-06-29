# EDGE 2 HYBRID ENGINE — MERGE & ADAPTIVE OPTIMIZATION PROMPT
## Combining High-Frequency Signal Generation with High-Precision Gate Logic

---

## SECTION 0 — YOUR MISSION IN ONE PARAGRAPH

You are merging two versions of the same strategy into one engine that does what
neither can do alone. The new bot is too strict — it finds perfect setups but
misses months of trading. The old bot found trades constantly but its gates were
too loose to maintain elite win rate. Your job is to extract what made the old bot
generate signals, wire it into the new bot's precision framework, and build a
modification layer between them that behaves the way a human trader does: adapting
the entry requirements to match the quality of the setup, rather than either
blindly accepting or blindly rejecting.

The target is not "more trades" — it is "more of the RIGHT trades."

---

## SECTION 1 — THE TWO INPUTS

### Input A — The New Bot (Pull From GitHub)
Repository: https://github.com/Laqez247/bactestingBOT.git

Pull the full codebase. Do not modify anything yet. Inventory every file.
This is your foundation — the gate chain, zone logic, TP engine, and audit
infrastructure stay. Only the signal generation breadth and the gate flexibility
will change.

Current performance (your baseline to beat):
- 32 trades over 25 months (~1.28/month)
- 87.5% Win Rate
- +0.6495R expectancy
- 6.057 profit factor
- 1.030R max drawdown
- Zero dry spells survived: Mar 2025, Jul 2025, Oct–Nov 2025 = zero trades

### Input B — The Old Bot (Uploaded Python Files)
Read every uploaded Python file completely before forming any opinion about it.

This is the source of signal frequency intelligence. You are looking for the
specific logic patterns that allowed it to generate trades during periods when
the new bot found nothing. Do not assess quality yet — just understand what it
was doing differently.

---

## SECTION 2 — THE DIAGNOSTIC PHASE (DO NOT SKIP)

Before writing a single line of new code, produce a written Diagnostic Report.
This is not optional. It is the foundation of the entire merge strategy.

### 2A — Old Bot Deep Analysis

Read through every file of the old bot and answer these questions exactly:

**Signal Generation:**
- What was the sweep detection threshold (wick minimum in ATR or absolute)?
- How was the range defined — same logic as new bot or different?
- What was the range quality threshold (if any)?
- Did it require MSS, BOS, or just any close beyond a level?
- What was the retest timeout in bars?
- Did it allow multiple zone types (OB, FVG, BB, SR) or a subset?

**Session and Filter Logic:**
- Did it trade London only or multiple sessions?
- Was there a volatility filter? What were the ATR bounds?
- Was there a spread filter?
- Were any sweep types blocked?

**TP Logic:**
- Was TP2 structural (scored against reaction zones) or fixed RR?
- How were targets selected?
- Was there a TP1 partial close mechanic?

**Output a comparison table:**

| Parameter              | New Bot Value | Old Bot Value | Difference |
|------------------------|---------------|---------------|------------|
| Sweep min wick (ATR)   | 0.20          | ?             | ?          |
| Range min quality      | 40            | ?             | ?          |
| MSS required           | True          | ?             | ?          |
| Retest timeout (bars)  | 125           | ?             | ?          |
| Session filter         | London only   | ?             | ?          |
| Blocked sweep types    | 6 types       | ?             | ?          |
| TP method              | Dynamic       | ?             | ?          |

### 2B — Dry Spell Root Cause Analysis

For the new bot specifically, load the results/setups log from the last full run.
Find every rejected setup during the known dry months (Mar 2025, Jul 2025, Oct–Nov 2025).

For each rejected setup in those months, record:
- Which gate rejected it (Gate 0 through Gate 10)
- The compensation factors present (sweep depth, displacement size, zone quality)
- Whether the old bot would have taken this setup

Produce a gate-by-gate rejection breakdown for those dry months:

| Gate | Rejections During Dry Months | % of Total Dry-Month Rejections |
|------|-----------------------------|----------------------------------|
| Gate 2 (Range)         | ? | ? |
| Gate 3 (SSL/BSL Sweep) | ? | ? |
| Gate 4 (MSS/BOS)       | ? | ? |
| Gate 5 (HTF Bias)      | ? | ? |
| Gate 6 (Zone)          | ? | ? |

The gate with the highest rejection count during dry months is the primary
bottleneck. That is where the modification layer needs to focus.

### 2C — Write Your Merge Strategy Before Building

After completing 2A and 2B, write a plain-English paragraph explaining:
- Exactly which logic from the old bot you will integrate and why
- Exactly which gates in the new bot you will add modifications to and why
- What you expect the trade-off to look like (trade count up by X, WR may drop by Y)

Print: "DIAGNOSTIC COMPLETE — MERGE STRATEGY: [your paragraph]"

Do not proceed to Phase 2 until this is printed.

---

## SECTION 3 — THE CORE PHILOSOPHY: MODIFY, DON'T FILTER

This is the fundamental shift you are implementing. Read it carefully.

### The Problem With Binary Gates

The new bot's gate chain works like this:
```
IF mss_bullish == True  → PASS
IF mss_bullish == False → REJECT
```

That is a binary light switch. The market either delivers a textbook setup or
the bot sits on its hands — even when everything else about the setup is
screaming institutional activity.

A human trader looking at the same setup does not think in binary.
They think in weight of evidence:

*"Okay the MSS sequence isn't textbook — it's technically a BOS because there
wasn't a clear prior lower-high sequence. BUT — the sweep was the deepest wick
I've seen in three sessions, the OB is sitting perfectly on the previous week's
high, and the 1H has been making higher highs for six weeks straight. The weight
of evidence says this is institutional. I'm going to take the trade, but I'm
going to be TIGHTER on my entry — I want a full wick rejection and close away
from the zone, not just a touch. The setup quality is lower so my entry
confirmation has to be higher."*

That is what you are building. Not lower standards — ADAPTIVE standards.

### The Three Layers of Adaptation

**Layer 1 — SIGNAL EXPANSION**
Extract the old bot's broader detection logic to generate more candidate setups
in the first place. More raw candidates means more setups that can pass through
the precision gates. This is where signal frequency comes from.

**Layer 2 — REGIME DETECTION**
Before running the gate chain on any setup, classify the current market regime.
The regime determines which modifications are available and what the compensation
thresholds are. In a strongly trending regime, more setups qualify for adaptation.
In a choppy, low-structure regime, the gates stay strict.

**Layer 3 — SETUP MODIFICATION**
When a soft gate fails, instead of immediately rejecting, the engine checks whether
a modification can make the setup valid. A modification is a HIGHER requirement on
entry confirmation in exchange for accepting a lower-quality precondition.

The key insight: a modified setup is not a lower-quality trade — it is a trade
where the entry demand has been raised to compensate for the setup imperfection.

---

## SECTION 4 — BUILD THE REGIME DETECTOR (regime_detector.py)

Create a new module: `regime_detector.py`

This module runs once per bar and outputs a REGIME classification that feeds
into the gate chain. It uses only data available at bar T.

### Regime Classification Logic

**REGIME_TRENDING_STRONG:**
- HTF (1H) shows a clean 5+ swing sequence of HH+HL (bullish) or LH+LL (bearish)
- ATR is between 1.0x and 2.5x the 30-bar ATR average (normal volatility)
- No HTF candle in the last 10 bars has closed more than 1.5x ATR against the trend
- In this regime: the most modifications are available, adaptation thresholds are lower

**REGIME_TRENDING_MODERATE:**
- HTF shows a 3-swing HH+HL or LH+LL sequence (minimum for trend classification)
- ATR is within normal range
- Some counter-trend candles present but trend direction is clear
- In this regime: moderate modifications available, standard thresholds

**REGIME_RANGING:**
- HTF has no clear swing sequence (last 5 swings alternate without direction)
- Price is oscillating between visible support and resistance
- In this regime: minimum modifications, gates stay near their original strictness

**REGIME_HIGH_VOLATILITY:**
- ATR spike: current ATR > 2.5x the 30-bar ATR average
- News-adjacent or momentum-driven price action
- In this regime: NO modifications. Hard gates only. Volatility masks signal quality.
- Log every setup as REGIME_HV_REJECTED without running the full gate chain

**REGIME_LOW_STRUCTURE:**
- ATR below 0.70 (dead market, no institutional activity)
- Gate 0 volatility filter handles this — no trades

Output from regime_detector.py at each bar:
```python
{
  "regime": "TRENDING_STRONG" | "TRENDING_MODERATE" | "RANGING" | "HIGH_VOLATILITY",
  "htf_swing_count": int,      # how many qualifying HTF swings
  "atr_ratio": float,          # current ATR / 30-bar ATR mean
  "regime_confidence": float   # 0.0–1.0 score
}
```

Log the regime for every setup in the setup log column `market_regime`.

---

## SECTION 5 — BUILD THE SETUP MODIFICATION LAYER

### Architecture Overview

Refactor `trade_simulator.py` to use this structure:

```python
def validate_setup(setup, context):
    # STEP 1: Run hard gates — no modification possible
    if not hard_gates_pass(setup, context):
        return REJECT(hard_gate_reason)

    # STEP 2: Check regime
    regime = detect_regime(context)
    if regime == "HIGH_VOLATILITY":
        return REJECT("HIGH_VOLATILITY_REGIME")

    # STEP 3: Run soft gates with modification fallback
    gate_results = {}
    modifications_used = []

    for gate in SOFT_GATES:
        result = gate.evaluate(setup, context)
        if result.passed:
            gate_results[gate.name] = "PASS"
        else:
            # Attempt modification instead of immediate rejection
            mod = attempt_modification(gate, setup, context, regime)
            if mod.approved:
                gate_results[gate.name] = "MODIFIED"
                modifications_used.append(mod)
                # Apply the modification — change entry requirements
                setup = mod.apply_to(setup)
            else:
                return REJECT(gate.name + "_NO_VALID_MODIFICATION")

    # STEP 4: Maximum one modification per setup
    if len(modifications_used) > 1:
        return REJECT("MULTIPLE_MODIFICATIONS_EXCEEDED")

    return ACCEPT(setup, modifications_used)
```

### The Modification Rules — Implement All Five

**MODIFICATION 1 — WEAK_RANGE → STRICT_ENTRY**
```
Triggers when:
  Gate 2 fails because range_quality_score is between 25 and 40 (below threshold of 40)
  AND regime is TRENDING_STRONG or TRENDING_MODERATE

Modification applied:
  - Require sweep wick > 0.50x ATR (deeper sweep compensates for noisy range)
  - Override entry mode to MODE_WICK_REJECTION regardless of config setting
    (stricter entry confirmation compensates for weaker range quality)
  - Require zone to be OB or FVG only (no SR fallback — zone quality must be higher)

Logic: The range was messy but the sweep was violent and the entry will require a
full wick rejection rather than just a close outside the zone. The trade can proceed
because the entry demand is higher — we are not accepting a worse trade, we are
requiring more proof before entering it.

Log as: modification_type = "WEAK_RANGE_STRICT_ENTRY"
```

**MODIFICATION 2 — BOS_NOT_MSS → MACRO_DISPLACEMENT**
```
Triggers when:
  Gate 4 fails because structure_break_type = BOS (not MSS)
  AND regime is TRENDING_STRONG (minimum requirement — moderate not enough for this one)

Modification applied:
  - Require breakout candle body > 1.5x ATR (vs normal threshold of 0.30x)
    — the displacement must be massive to compensate for missing the MSS sequence
  - Require HTF swing count >= 5 (strong macro trend, not borderline)
  - Require the zone type to be OB or BB only (highest-quality zones only)
  - SL_ATR_BUFFER increases by 0.10 above normal (additional cushion for BOS trades)

Logic: The market did not produce a perfect MSS sequence, but the breakout candle
was so powerful that it signals institutional aggression regardless. The macro trend
must be unambiguously strong to allow this. The entry is also harder: only the
highest-quality zones qualify when MSS is absent.

Log as: modification_type = "BOS_MACRO_DISPLACEMENT"
```

**MODIFICATION 3 — STALE_RETEST → ZONE_CONFLUENCE**
```
Triggers when:
  Gate 6 fails because retest occurred after RETEST_TIMEOUT_BARS
  BUT before 175 bars (hard outer limit — no modification beyond 175 bars)
  AND regime is TRENDING_STRONG or TRENDING_MODERATE

Modification applied:
  - Zone must overlap with at least TWO of these level types simultaneously:
    (round number within $2.00, PDH/PDL within $1.50, session high/low within $1.50,
     EQH/EQL within the zone band)
  - If the zone does not have 2+ overlapping confluence types, reject immediately
  - Entry mode stays as configured — confluence is the compensation, not entry change

Logic: The setup has taken longer to develop than expected. The only reason a zone
remains valid after the timeout is if multiple institutional reference levels are
converging there. The confluence is evidence that the zone is a genuine magnet, not
just a stale artifact.

Log as: modification_type = "STALE_RETEST_CONFLUENCE"
```

**MODIFICATION 4 — BLOCKED_SWEEP_RECOVERY → ELEVATED_CONTEXT**
```
Triggers when:
  Gate 3 fails because ssl_bsl_sweep_type is in BLOCKED_SWEEP_TYPES
  AND the specific blocked type is NOT SSL_SESSION_LOW or SSL_RANGE_LOW for LONG
    (those two have 0% historical WR — no modification available, always reject)
  AND regime is TRENDING_STRONG

Modification applied:
  - Sweep wick must be > 0.75x ATR (top third of sweep quality)
  - Displacement (breakout body) must be > 1.2x ATR
  - Zone must be OB only (highest conviction zone)
  - HTF swing count must be >= 5

Logic: The sweep type has underperformed historically. But in an exceptionally
strong trending regime with elite displacement and the highest quality zone, the
specific sweep type becomes less important than the overall institutional context.
This is only allowed for the least-problematic blocked types, never for the
historically zero-WR types.

Log as: modification_type = "BLOCKED_SWEEP_ELEVATED_CONTEXT"
```

**MODIFICATION 5 — LONG_DIRECTION_RECOVERY (SSL_PDL)**
```
Triggers when:
  Direction = LONG
  AND ssl_bsl_sweep_type = SSL_PDL (previous day low swept)
  Note: SSL_PDL was NOT explicitly blocked — this modification unblocks a specific
  LONG sweep type that was never blocked but underperformed marginally

Modification applied:
  - Regime must be TRENDING_STRONG (1H clearly bullish)
  - Sweep wick must be > 0.40x ATR
  - Zone must be OB (no FVG, BB, or SR for this modification)
  - Entry mode must be MODE_WICK_REJECTION

Logic: LONG trades were systematically underrepresented (only 6 of 32 trades = 18.75%)
because the blocked sweeps eliminated most long opportunities. SSL_PDL was not blocked
but showed moderate performance. In a clearly bullish HTF regime with a clean OB and
strict entry, a PDL sweep for LONG is a high-quality reversal setup.

Log as: modification_type = "LONG_PDL_RECOVERY"
```

### Modification Guardrails

1. **Only one modification per trade.** If two soft gates fail, reject the setup.
   No stacking modifications.

2. **Modifications are regime-gated.** HIGH_VOLATILITY regime = zero modifications.
   RANGING regime = Modification 3 only (stale retest with confluence).

3. **SL placement is not modified.** Regardless of what modification is applied,
   the structural SL always goes below the zone's invalidation level plus ATR buffer.
   Modifications may INCREASE the buffer (as in Modification 2) but never reduce it.

4. **Dynamic TP is not modified.** The TP engine runs identically on standard and
   modified trades. No fixed RR is added under any modification rule.

5. **Modified trades are fully logged.** The `modification_type` field must exist in
   every trade log entry. Non-modified trades = "NONE".

---

## SECTION 6 — DATA UPDATE

Fetch and cache all XAUUSD 5m and 1H bars through Friday, June 19, 2026.

Check the current cache for the last cached timestamp on both timeframes.
Fetch only the gap. Append to existing cache. Do not re-fetch what exists.
Confirm total bar count and date range before running any backtest.

---

## SECTION 7 — THE AI-DRIVEN ITERATIVE LOOP

**You are the optimizer. Not a Python script.**

Do not write a parameter sweep. Do not write a grid search. Do not write any
automated loop script that tests configurations without you reasoning about them.

You will run the backtest, read the actual results files, think about what those
numbers mean at a trading-logic level, form a specific hypothesis, implement
exactly that change, run again, and evaluate whether your hypothesis was correct.

### Convergence Target

Keep iterating until you hit this target or exhaust 10 iterations:

| Metric          | Minimum Acceptable | Ideal Target     |
|-----------------|--------------------|------------------|
| Trades/month    | 2.5                | 3.5+             |
| Win Rate        | 75%                | 80%+             |
| Expectancy      | +0.45R             | +0.55R+          |
| Max Drawdown    | < 2.5R             | < 2.0R           |
| Dry spells      | Max 6 weeks        | Max 4 weeks      |

If you hit these numbers before 10 iterations, stop. The plateau is reached.

### The Iteration Cycle

Each iteration follows this exact sequence. Do not skip steps.

**STEP A — RUN**
```bash
python quick_run.py --cache-only --label "ITER_N_[description]"
```
Wait for completion. Read the output files before doing anything else.

**STEP B — SPLIT THE TRADE LOG**
Open trades_ITER_N.csv and split by modification_type:
- How many standard trades (modification_type = "NONE")?
- How many modified trades and what types?
- What is the WR and expectancy for each group separately?

This is the most important analysis step. If modified trades are underperforming
(WR < 65% or expectancy < 0), the modification threshold needs raising.
If modified trades are outperforming standard trades (WR > 85%), the threshold
may be too conservative — more setups could qualify.

**STEP C — READ THE REJECTION LOG**
Open setups_ITER_N.csv, filter for rejected setups.
Find the rejection_reason with the highest count.
Is that rejection reason:
a) Correct — those were genuinely bad setups that should be filtered?
b) A false negative — those were valid setups the modification layer didn't catch?

If (b): identify which modification rule should have caught it, or whether a new
Modification 6 is needed.

**STEP D — WRITE YOUR DIAGNOSIS**
Write one paragraph answering:
- What is working (which modifications are adding clean trades)?
- What is failing (which modifications are adding noisy trades)?
- What is still being left on the table (valid setups still being rejected)?
- What is the single highest-leverage change for the next iteration?

**STEP E — IMPLEMENT ONE CHANGE**
Based on the diagnosis, make one change. Before/after diff must be shown.
The change should have a trading-logic justification, not just a numerical one.

WRONG: "I will lower MODIFICATION_1 sweep threshold from 0.50 to 0.40"
RIGHT: "MODIFICATION_1 is rejecting valid weak-range setups where the sweep was
        0.40–0.50x ATR. On XAUUSD during the London open, a 0.40x ATR sweep of
        a noisy range is still a measurable institutional grab — the subsequent
        OB retest with wick rejection is sufficient entry confirmation. Lowering
        the sweep requirement to 0.40x ATR for MODIFICATION_1 specifically during
        London open 07:00–09:30 UTC, where the edge is historically strongest."

**STEP F — EVALUATE AND DECIDE**

Compare against the current champion using this exact scorecard:

| Metric           | Champion | Last Iter | This Iter | Delta vs Champion |
|------------------|----------|-----------|-----------|-------------------|
| Trades total     | 32       | ?         | ?         | ?                 |
| Trades/month     | 1.28     | ?         | ?         | ?                 |
| Win Rate         | 87.5%    | ?         | ?         | ?                 |
| Expectancy       | +0.6495R | ?         | ?         | ?                 |
| Profit Factor    | 6.057    | ?         | ?         | ?                 |
| Max Drawdown     | 1.030R   | ?         | ?         | ?                 |
| Modified trades  | 0%       | ?         | ?         | ?                 |
| Dry spell (max)  | ~8 weeks | ?         | ?         | ?                 |

Define "better" as: trades/month increases by at least 0.5 AND
expectancy does not drop below 0.45R AND max drawdown stays below 2.5R.

If better: declare new champion, save to best_config.json, continue loop.
If worse: revert completely, log why, form a different hypothesis.

---

## SECTION 8 — DYNAMIC TP ENGINE PROTECTION

This section is non-negotiable and must be verified at every iteration.

The TP engine must generate targets by scoring actual historical price reaction
zones — the places where XAUUSD candles have repeatedly touched, stalled, or
reversed. It does not use fixed ratios. It does not multiply SL distance by 2
or 3. It does not use generic pip targets.

At the end of every iteration, print the TP2 target type breakdown:

| TP2 Target Type          | Count | % of TP2 Trades | Avg R when Hit |
|--------------------------|-------|-----------------|----------------|
| Previous swing high/low  | ?     | ?               | ?              |
| PDH / PDL                | ?     | ?               | ?              |
| Session high/low         | ?     | ?               | ?              |
| Equal highs/lows (EQH/EQL)| ?    | ?               | ?              |
| Round number ($50 level) | ?     | ?               | ?              |
| Range extension          | ?     | ?               | ?              |
| OB / FVG zone            | ?     | ?               | ?              |

If any row shows "Fixed RR" or any target type that is a mathematical multiple
of SL distance: STOP. Remove it immediately. That is not a structural target.

If the old bot used fixed RR for TP2, that logic must NOT be carried across.
Extract its signal generation. Leave its TP logic behind.

---

## SECTION 9 — NON-NEGOTIABLES

1. **Zero lookahead bias.** After any modification to trade_simulator.py,
   zone_engine.py, or structure_engine.py: run audit_check.py.
   If the audit finds a future-bar reference, revert immediately.

2. **Maximum one modification per setup.** Two soft gate failures = rejection.
   No exceptions, no stacking.

3. **HIGH_VOLATILITY regime = no modifications.** Hard gates only.

4. **MaxDD ceiling: 2.5R.** Auto-reject any configuration exceeding this.

5. **Minimum 30 trades for champion declaration.** Fewer = STATISTICALLY
   INSUFFICIENT. Can still compare metrics but cannot declare it champion.

6. **TP1+BE safety net intact.** TP1 at 1.0R triggers 50% close and SL move
   to breakeven. This applies to both standard and modified trades.

7. **No hallucinated results.** Every number in your analysis comes from an
   actual output file from an actual run. Not estimated. Not approximated.

---

## SECTION 10 — FINAL DELIVERABLE

When the loop concludes, produce the Hybrid Engine Final Report:

**1. MERGE SUMMARY**
- Exactly what was extracted from the old bot and integrated
- Exactly what was kept intact from the new bot
- Which modifications were implemented and at what thresholds

**2. PERFORMANCE TABLE**
| Metric          | Old Bot | New Bot Champion | Hybrid Engine |
|-----------------|---------|-----------------|---------------|
| Trades/month    | ?       | 1.28            | ?             |
| Win Rate        | ?       | 87.5%           | ?             |
| Expectancy      | ?       | +0.6495R        | ?             |
| Max Drawdown    | ?       | 1.030R          | ?             |

**3. MODIFICATION BREAKDOWN**
For each modification type that fired:
- How many trades?
- Win rate on those trades?
- Expectancy on those trades?
- Verdict: keep / raise threshold / lower threshold / remove

**4. WALK-FORWARD CHECK**
Run in-sample (Apr 2024–Dec 2025) vs out-of-sample (Jan–Jun 2026) on the hybrid engine.
OOS expectancy must be within 30% of IS expectancy.
If it fails: identify which modification type degraded out-of-sample.

**5. DYNAMIC TP VERIFICATION**
Print the TP2 target type breakdown table.
Confirm: zero fixed RR targets present.

**6. PUSH TO GITHUB**
```bash
python git_push.py --message "Hybrid Engine Phase 3: [summary of what changed]"
```

---

## QUICK REFERENCE

```bash
# Pull new bot
git clone https://github.com/Laqez247/bactestingBOT.git

# Run baseline after data update
python quick_run.py --cache-only --label "HYBRID_BASELINE"

# Run with single config test
python quick_run.py --cache-only --cfg "MODIFICATION_1_SWEEP_ATR=0.40" --label "TEST_M1"

# In-sample
python quick_run.py --cache-only --start "2024-04-09" --end "2025-12-31" --label "IS"

# Out-of-sample
python quick_run.py --cache-only --start "2026-01-01" --end "2026-06-19" --label "OOS"

# Audit after any code change
python audit_check.py

# Push when done
python git_push.py --message "Hybrid Engine Final"
```
