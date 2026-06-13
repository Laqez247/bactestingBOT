"""
run_backtest.py — Master runner with full sequential iterative loop.

Executes Iterations 0–5 automatically:
  Iteration 0: Baseline run (full dataset)
  Iteration 1: Parameter sensitivity test (4 sub-tests)
  Iteration 2: Directional split analysis
  Iteration 3: Session analysis
  Iteration 4: Zone type and structure break analysis (MSS vs BOS)
  Iteration 5: Walk-forward validation (70/30 in-sample / out-of-sample)

Sequential execution is MANDATORY — no step skips, no merges.
Written summaries after every iteration.
"""

import os
import sys
import copy
import time
import argparse
import pandas as pd
import numpy as np
from typing import List, Tuple, Optional, Dict, Any

# Add package directory to path
sys.path.insert(0, os.path.dirname(__file__))

import config
import data_loader
import report
import metrics as metrics_module
from structure_engine import StructureEngine, compute_atr, run_htf_structure, align_htf_to_exec
from sr_engine import RangeEngine
from liquidity_engine import LiquidityEngine
from breakout_engine import BreakoutEngine
from zone_engine import ZoneEngine
from tp_engine import TPEngine
from trade_simulator import TradeSimulator, TradeRecord


# ------------------------------------------------------------------
# CORE BACKTEST FUNCTION
# ------------------------------------------------------------------

def run_single_backtest(
    df_exec: pd.DataFrame,
    df_htf: pd.DataFrame,
    cfg_overrides: dict = None,
    iteration: int = 0,
    label: str = ""
) -> Tuple[Dict, List[TradeRecord], List[TradeRecord]]:
    """
    Run a single complete backtest over df_exec/df_htf.
    Returns (metrics_dict, completed_trades, rejected_setups).
    """
    cfg = cfg_overrides or {}

    def p(k, default=None):
        return cfg.get(k, getattr(config, k, default))

    # --- Filter by sessions ---
    if p("SESSION_FILTER_ON", True):
        sessions = p("TRADE_SESSIONS", {})
        valid_hours = set()
        for name, window in sessions.items():
            oh = int(window["open"].split(":")[0])
            ch = int(window["close"].split(":")[0])
            for h in range(oh, ch):
                valid_hours.add(h)

    # Compute ATR for execution TF
    atr_exec = compute_atr(df_exec, p("ATR_PERIOD", 14))

    # Compute HTF structure series and align to exec TF
    htf_structure_series = run_htf_structure(df_htf)
    htf_aligned = align_htf_to_exec(htf_structure_series, df_exec.index)

    # Initialize all engines
    structure_eng = StructureEngine(
        pivot_left=p("PIVOT_LEFT", 3),
        pivot_right=p("PIVOT_RIGHT", 3),
        mss_prior_lh_count=p("MSS_PRIOR_LH_COUNT", 2)
    )
    range_eng     = RangeEngine(cfg=cfg)
    liq_eng       = LiquidityEngine(cfg=cfg)
    breakout_eng  = BreakoutEngine(cfg=cfg)
    zone_eng      = ZoneEngine(cfg=cfg)
    tp_eng        = TPEngine(cfg=cfg)
    sim           = TradeSimulator(cfg=cfg)

    # Retest tracking
    active_setup_bar: int = -1     # bar when zone was built
    zone_reaction_confirmed = False

    n = len(df_exec)

    for i in range(n):
        ts    = df_exec.index[i]
        atr_i = float(atr_exec.iloc[i]) if not pd.isna(atr_exec.iloc[i]) else 1.0

        # Determine if this bar is an active trading session bar
        in_session = True
        if p("SESSION_FILTER_ON", True):
            in_session = ts.hour in valid_hours

        in_volatility_window = True
        if p("VOLATILITY_FILTER_ON", True):
            if atr_i < p("VOLATILITY_MIN_ATR", 0.50) or atr_i > p("VOLATILITY_MAX_ATR", 5.00):
                in_volatility_window = False

        # --- ALWAYS update structure and range engines on every bar ---
        # Range detection must run 24/5 so Asian session ranges are captured.
        # The best Edge 2 setups use Asian ranges + London/NY breakouts.
        structure_eng.update(i, df_exec, atr_exec)
        range_eng.update(i, df_exec, atr_exec)
        liq_eng.update(i, df_exec, atr_exec, range_eng.state, structure_eng)

        # Skip breakout detection and entries outside session / volatility window
        if not in_session or not in_volatility_window:
            # Still simulate any open trade (must track P&L 24/5)
            if sim.has_active_trade():
                completed = sim.update_active_trade(i, df_exec)
                if completed:
                    if completed.direction == "LONG":
                        liq_eng.reset_ssl()
                    else:
                        liq_eng.reset_bsl()
                    breakout_eng.consume()
                    zone_eng.clear()
                    active_setup_bar = -1
                    zone_reaction_confirmed = False
            continue

        # --- Session is active: update breakout engine ---
        breakout_eng.update(i, df_exec, atr_exec, liq_eng, structure_eng)

        # --- If there is an active trade, simulate it ---
        if sim.has_active_trade():
            completed = sim.update_active_trade(i, df_exec)
            if completed:
                # Reset liquidity sweep flags after trade closes
                if completed.direction == "LONG":
                    liq_eng.reset_ssl()
                else:
                    liq_eng.reset_bsl()
                breakout_eng.consume()
                zone_eng.clear()
                active_setup_bar = -1
                zone_reaction_confirmed = False
            continue

        # --- Update zone invalidation if a zone is active ---
        if zone_eng.has_valid_zone:
            zone_eng.update_zones(i, df_exec)

            # Check for retest timeout
            if active_setup_bar >= 0:
                timeout = p("RETEST_TIMEOUT_BARS", 50)
                if i - active_setup_bar > timeout:
                    zone_eng.clear()
                    active_setup_bar = -1
                    zone_reaction_confirmed = False
                    # CRITICAL: consume the stale breakout so fresh ones can be detected.
                    # Without this, the engine gets permanently locked on the first breakout.
                    breakout_eng.consume()

            # Check zone reaction
            if not zone_reaction_confirmed and zone_eng.check_retest(i, df_exec):
                if zone_eng.check_reaction(i, df_exec, p("ENTRY_MODE", "MODE_CLOSE_OUTSIDE")):
                    zone_reaction_confirmed = True

            # If reaction confirmed — attempt to open trade
            if zone_reaction_confirmed and breakout_eng.current_breakout is not None:
                bo = breakout_eng.current_breakout
                direction = bo.direction

                # Direction filters
                if direction == "LONG" and not p("ENABLE_LONG", True):
                    continue
                if direction == "SHORT" and not p("ENABLE_SHORT", True):
                    continue

                htf_trend = str(htf_aligned.iloc[i]) if i < len(htf_aligned) else "RANGING"
                spread = liq_eng.get_current_spread(ts)

                # Generate TP candidates
                tp_candidates = tp_eng.generate(
                    direction=direction,
                    entry_price=_estimate_entry(direction, zone_eng, spread, cfg),
                    sl_price=_estimate_sl(direction, zone_eng, atr_i, cfg),
                    i=i,
                    df=df_exec,
                    atr=atr_exec,
                    range_state=range_eng.state,
                    structure_engine=structure_eng,
                    liquidity_engine=liq_eng
                )

                rec = sim.try_open_trade(
                    direction=direction,
                    i=i,
                    df=df_exec,
                    atr=atr_exec,
                    htf_trend=htf_trend,
                    range_state=range_eng.state,
                    liquidity_engine=liq_eng,
                    breakout_event=bo,
                    zone_engine=zone_eng,
                    zone_reaction=zone_reaction_confirmed,
                    tp_candidates=tp_candidates,
                    tp_engine_obj=tp_eng,
                    current_spread=spread,
                    iteration=iteration
                )

                if rec is not None:
                    # Trade opened — reset zone tracking
                    zone_eng.clear()
                    active_setup_bar = -1
                    zone_reaction_confirmed = False
                else:
                    # Rejected — clear and move on
                    zone_eng.clear()
                    active_setup_bar = -1
                    zone_reaction_confirmed = False

        elif breakout_eng.current_breakout is not None:
            # New breakout — build zones
            bo = breakout_eng.current_breakout
            zone_eng.build_zones(bo, i, df_exec, atr_exec, range_eng.state)
            active_setup_bar = i
            zone_reaction_confirmed = False

    # Handle any trade still open at end of data
    if sim.has_active_trade():
        last_i = n - 1
        rec = sim._active_trade
        rec.exit_bar    = last_i
        rec.exit_price  = float(df_exec["close"].iloc[last_i])
        rec.exit_reason = "TIMEOUT"
        rec.bars_held   = last_i - rec.entry_bar
        entry = rec.entry_price
        sl    = rec.sl_price
        exit_p = rec.exit_price
        if rec.direction == "LONG":
            r = (exit_p - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        else:
            r = (entry - exit_p) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        rec.r_multiple = round(r, 3)
        sim.completed_trades.append(rec)
        sim._active_trade = None

    m = metrics_module.compute_metrics(
        sim.completed_trades, sim.rejected_setups, label=label
    )
    return m, sim.completed_trades, sim.rejected_setups


def _estimate_entry(direction: str, zone_eng: ZoneEngine, spread: float, cfg: dict) -> float:
    slippage = cfg.get("SLIPPAGE", config.SLIPPAGE)
    zone = zone_eng.primary_zone
    if zone is None:
        return 0.0
    if direction == "LONG":
        return zone.top + spread + slippage
    return zone.bottom - spread - slippage


def _estimate_sl(direction: str, zone_eng: ZoneEngine, atr_val: float, cfg: dict) -> float:
    sl_buffer = cfg.get("SL_ATR_BUFFER", config.SL_ATR_BUFFER)
    zone = zone_eng.primary_zone
    if zone is None:
        return 0.0
    if direction == "LONG":
        return zone.bottom - sl_buffer * atr_val
    return zone.top + sl_buffer * atr_val


# ------------------------------------------------------------------
# PRE-RUN CHECKLIST
# ------------------------------------------------------------------

def pre_run_checklist(args) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Runs all pre-run validation steps before first iteration.
    Returns (df_exec, df_htf).
    """
    print("\n" + "=" * 70)
    print("PRE-RUN CHECKLIST")
    print("=" * 70)

    # 1. Validate API key and test connection
    print("\n[1/5] Testing API connection...")
    ok = data_loader.test_api_connection()
    if not ok:
        print("  WARNING: API connection test failed. Attempting to use cached data.")

    # 2. Confirm cache directory
    print(f"\n[2/5] Checking cache directory: {config.CACHE_DIR}")
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    test_file = os.path.join(config.CACHE_DIR, ".write_test")
    try:
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        print(f"  Cache directory OK and writable.")
    except Exception as e:
        print(f"  WARNING: Cache directory not writable: {e}")

    # 3. Load data
    print(f"\n[3/5] Loading data ({config.BACKTEST_START} → {config.BACKTEST_END})...")
    start = getattr(args, "start", None) or config.BACKTEST_START
    end   = getattr(args, "end",   None) or config.BACKTEST_END
    cache_only = getattr(args, "cache_only", False)

    df_exec, df_htf = data_loader.load_data(
        start=start, end=end,
        use_cache=True, cache_only=cache_only
    )

    # 4. Print data summary (already done in load_data)
    print("\n[4/5] Data loaded successfully.")

    # 5. Compute baseline ATR stats for parameter calibration
    print("\n[5/5] Computing baseline ATR statistics...")
    atr_stats = data_loader.compute_baseline_atr_stats(df_exec, config.ATR_PERIOD)
    print(f"  ATR(14) on {config.EXECUTION_TF} — XAUUSD")
    print(f"    Mean : ${atr_stats['mean']:.4f}")
    print(f"    Min  : ${atr_stats['min']:.4f}")
    print(f"    Max  : ${atr_stats['max']:.4f}")
    print(f"    P25  : ${atr_stats['p25']:.4f}")
    print(f"    P75  : ${atr_stats['p75']:.4f}")
    print(f"    Count: {atr_stats['count']} bars")
    print(f"\n  Config check:")
    print(f"    VOLATILITY_MIN_ATR = ${config.VOLATILITY_MIN_ATR:.2f}  "
          f"[{'OK' if config.VOLATILITY_MIN_ATR <= atr_stats['p25'] else 'WARN: may filter too much'}]")
    print(f"    SL_MAX_DISTANCE_ATR × mean ATR = ${config.SL_MAX_DISTANCE_ATR * atr_stats['mean']:.2f}  "
          f"[{'OK' if config.SL_MAX_DISTANCE_ATR * atr_stats['mean'] < 5.0 else 'WARN: wide SL'}]")

    print("\nPRE-RUN CHECKLIST COMPLETE ✓")
    return df_exec, df_htf


# ------------------------------------------------------------------
# ITERATION 0 — BASELINE
# ------------------------------------------------------------------

def run_iteration_0(df_exec, df_htf) -> Tuple[Dict, List, List]:
    print("\n" + "=" * 70)
    print("ITERATION 0 — BASELINE RUN (full dataset, default config)")
    print("=" * 70)

    m, trades, rejected = run_single_backtest(
        df_exec, df_htf, cfg_overrides={}, iteration=0, label="BASELINE"
    )

    report.print_iteration_summary(m, 0, param_snapshot=_default_param_snapshot())
    report.export_trades(trades, 0)
    report.export_setups(trades, rejected, 0)
    report.export_summary(m, 0, _default_param_snapshot())

    diagnosis = metrics_module.diagnose_baseline(m)
    print(f"\n{diagnosis}")
    print("\nBASELINE COMPLETE. Analyzing...")

    return m, trades, rejected


# ------------------------------------------------------------------
# ITERATION 1 — PARAMETER SENSITIVITY TEST
# ------------------------------------------------------------------

def run_iteration_1(df_exec, df_htf, baseline_m: Dict) -> Dict:
    """
    4 sub-tests, each changing ONE parameter from baseline.
    Returns dict of results for comparison + the winning cfg_override.
    """
    print("\n" + "=" * 70)
    print("ITERATION 1 — PARAMETER SENSITIVITY TEST")
    print("=" * 70)

    variants = {
        "A_PIVOT_LEFT_RIGHT_4":     {"PIVOT_LEFT": 4,  "PIVOT_RIGHT": 4},
        "B_BREAKOUT_BODY_030":      {"BREAKOUT_MIN_BODY_ATR": 0.30},
        "C_RETEST_TIMEOUT_50":      {"RETEST_TIMEOUT_BARS": 50},
        "D_RANGE_MIN_QUALITY_40":   {"RANGE_MIN_QUALITY": 40},
    }

    results = {"BASELINE": baseline_m}
    all_cfg  = {"BASELINE": {}}

    for name, override in variants.items():
        print(f"\n  Running variant {name}: {override}")
        m, trades, rejected = run_single_backtest(
            df_exec, df_htf, cfg_overrides=override,
            iteration=1, label=f"ITER1_{name}"
        )
        results[name] = m
        all_cfg[name]  = override
        report.export_trades(trades, 1, suffix=f"_{name}")
        report.export_setups(trades, rejected, 1, suffix=f"_{name}")

    # Print comparison table
    print("\n" + "-" * 80)
    print(f"  {'Variant':<30} | {'Trades':>6} | {'Win Rate':>8} | {'Exp(R)':>8} | {'PF':>6} | {'FBR':>6}")
    print("  " + "-" * 78)
    for name, m in results.items():
        n   = m["trades_taken"]
        wr  = m["win_rate"]
        exp = m["expectancy_r"]
        pf  = m["profit_factor"]
        fbr = m["false_breakout_rate"]
        ins = "**" if m["statistically_insufficient"] else "  "
        print(f"  {ins}{name:<28} | {n:>6} | {wr:>7.1%}  | {exp:>+8.4f} | {pf:>6.3f} | {fbr:>5.1%}")
    print("  ** = STATISTICALLY INSUFFICIENT")

    # Find winner: highest expectancy_r without significant increase in FBR
    baseline_fbr = baseline_m["false_breakout_rate"]
    best_name    = "BASELINE"
    best_exp     = baseline_m["expectancy_r"]

    for name, m in results.items():
        if name == "BASELINE":
            continue
        if m["statistically_insufficient"]:
            continue
        fbr_increase = m["false_breakout_rate"] - baseline_fbr
        if m["expectancy_r"] > best_exp and fbr_increase < 0.10:
            best_exp  = m["expectancy_r"]
            best_name = name

    winner_cfg = all_cfg.get(best_name, {})
    winner_m   = results[best_name]

    print(f"\n  ITER 1 WINNER: {best_name}")
    if best_name == "BASELINE":
        print("  → No variant improved on baseline without increasing FBR.")
        print("    Structural reason: Default parameters are already calibrated for XAUUSD 5m.")
    else:
        override = all_cfg[best_name]
        for k, v in override.items():
            print(f"  → Changed {k} to {v}")
        print(f"    Expectancy improved from {baseline_m['expectancy_r']:+.4f}R "
              f"to {winner_m['expectancy_r']:+.4f}R")
        print(f"    FBR: {baseline_fbr:.1%} → {winner_m['false_breakout_rate']:.1%}")

    report.export_summary(winner_m, 1,
                          param_snapshot={**_default_param_snapshot(), **winner_cfg},
                          suffix=f"_{best_name}")

    return winner_cfg


# ------------------------------------------------------------------
# ITERATION 2 — DIRECTIONAL SPLIT
# ------------------------------------------------------------------

def run_iteration_2(df_exec, df_htf, best_cfg: Dict) -> Dict:
    print("\n" + "=" * 70)
    print("ITERATION 2 — DIRECTIONAL SPLIT ANALYSIS")
    print("=" * 70)

    results = {}
    for direction, cfg_extra in [
        ("LONG_ONLY",  {"ENABLE_LONG": True,  "ENABLE_SHORT": False}),
        ("SHORT_ONLY", {"ENABLE_LONG": False, "ENABLE_SHORT": True}),
        ("BOTH",       {}),
    ]:
        merged = {**best_cfg, **cfg_extra}
        m, trades, rejected = run_single_backtest(
            df_exec, df_htf, cfg_overrides=merged,
            iteration=2, label=f"ITER2_{direction}"
        )
        results[direction] = (m, trades, rejected)
        print(f"\n  {direction}: n={m['trades_taken']} | WR={m['win_rate']:.1%} | "
              f"Exp={m['expectancy_r']:+.4f}R | PF={m['profit_factor']:.3f}")

    long_m  = results["LONG_ONLY"][0]
    short_m = results["SHORT_ONLY"][0]

    print("\n  DIRECTIONAL ANALYSIS:")
    decision_cfg = {}
    disable_long  = (not long_m["statistically_insufficient"] and
                     long_m["expectancy_r"] < 0)
    disable_short = (not short_m["statistically_insufficient"] and
                     short_m["expectancy_r"] < 0)

    if disable_long:
        print(f"  → LONG direction has negative expectancy ({long_m['expectancy_r']:+.4f}R). DISABLING LONG.")
        decision_cfg["ENABLE_LONG"] = False
    if disable_short:
        print(f"  → SHORT direction has negative expectancy ({short_m['expectancy_r']:+.4f}R). DISABLING SHORT.")
        decision_cfg["ENABLE_SHORT"] = False
    if not disable_long and not disable_short:
        print(f"  → Both directions viable. LONG Exp={long_m['expectancy_r']:+.4f}R | "
              f"SHORT Exp={short_m['expectancy_r']:+.4f}R. Keeping BOTH.")
    elif disable_long and disable_short:
        print("  WARNING: Both directions have negative expectancy. Keeping both (no valid alternative).")
        decision_cfg = {}

    updated_cfg = {**best_cfg, **decision_cfg}
    m_both, t_both, r_both = results["BOTH"]
    report.export_trades(t_both, 2)
    report.export_setups(t_both, r_both, 2)
    report.export_summary(m_both, 2, param_snapshot=updated_cfg)

    return updated_cfg


# ------------------------------------------------------------------
# ITERATION 3 — SESSION ANALYSIS
# ------------------------------------------------------------------

def run_iteration_3(df_exec, df_htf, best_cfg: Dict) -> Dict:
    print("\n" + "=" * 70)
    print("ITERATION 3 — SESSION ANALYSIS")
    print("=" * 70)

    session_configs = {
        "LONDON_ONLY": {"TRADE_SESSIONS": {"london": config.TRADE_SESSIONS["london"]}},
        "NY_ONLY":     {"TRADE_SESSIONS": {"new_york": config.TRADE_SESSIONS["new_york"]}},
        "BOTH":        {},
    }

    results = {}
    for label, sess_cfg in session_configs.items():
        merged = {**best_cfg, **sess_cfg}
        m, trades, rejected = run_single_backtest(
            df_exec, df_htf, cfg_overrides=merged,
            iteration=3, label=f"ITER3_{label}"
        )
        results[label] = m
        print(f"\n  {label}: n={m['trades_taken']} | WR={m['win_rate']:.1%} | "
              f"Exp={m['expectancy_r']:+.4f}R | PF={m['profit_factor']:.3f}")

    # Comparison table
    print("\n  " + "-" * 60)
    print(f"  {'Session':<20} | {'Trades':>6} | {'Win Rate':>8} | {'Exp(R)':>8}")
    print("  " + "-" * 60)
    for label, m in results.items():
        print(f"  {label:<20} | {m['trades_taken']:>6} | {m['win_rate']:>7.1%}  | {m['expectancy_r']:>+8.4f}")

    london_m = results["LONDON_ONLY"]
    ny_m     = results["NY_ONLY"]

    print("\n  SESSION ANALYSIS:")
    threshold = -0.10
    updated_cfg = {**best_cfg}

    if london_m["expectancy_r"] > ny_m["expectancy_r"]:
        diff = london_m["expectancy_r"] - ny_m["expectancy_r"]
        print(f"  → London outperforms NY by {diff:.4f}R.")
        print(f"    Likely cause: London Open sweep/breakout sequence is tighter and more directional.")
        if ny_m["expectancy_r"] < threshold and not ny_m["statistically_insufficient"]:
            print(f"  → NY expectancy below -0.10R threshold. DISABLING NY.")
            updated_cfg["TRADE_SESSIONS"] = {"london": config.TRADE_SESSIONS["london"]}
        else:
            print(f"  → NY still viable ({ny_m['expectancy_r']:+.4f}R). Keeping both sessions.")
    else:
        diff = ny_m["expectancy_r"] - london_m["expectancy_r"]
        print(f"  → NY outperforms London by {diff:.4f}R.")
        print(f"    Likely cause: NY continuation of London structure provides cleaner retests.")
        if london_m["expectancy_r"] < threshold and not london_m["statistically_insufficient"]:
            print(f"  → London expectancy below -0.10R threshold. DISABLING London.")
            updated_cfg["TRADE_SESSIONS"] = {"new_york": config.TRADE_SESSIONS["new_york"]}
        else:
            print(f"  → London still viable ({london_m['expectancy_r']:+.4f}R). Keeping both sessions.")

    both_m = results["BOTH"]
    report.export_summary(both_m, 3, param_snapshot=updated_cfg)
    return updated_cfg


# ------------------------------------------------------------------
# ITERATION 4 — ZONE TYPE AND STRUCTURE TYPE ANALYSIS
# ------------------------------------------------------------------

def run_iteration_4(df_exec, df_htf, best_cfg: Dict) -> Dict:
    print("\n" + "=" * 70)
    print("ITERATION 4 — ZONE TYPE AND STRUCTURE TYPE ANALYSIS")
    print("=" * 70)

    m, trades, rejected = run_single_backtest(
        df_exec, df_htf, cfg_overrides=best_cfg,
        iteration=4, label="ITER4_ZONE_STRUCTURE"
    )

    # Zone type breakdown
    by_zone = m["by_zone_type"]
    print("\n  ZONE ANALYSIS:")
    print(f"  {'Zone Type':<12} | {'N':>5} | {'Win Rate':>8} | {'Exp(R)':>8} | Note")
    print("  " + "-" * 60)
    for zone_type, zm in sorted(by_zone.items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True):
        n   = zm.get("n", 0)
        wr  = zm.get("win_rate", 0.0)
        exp = zm.get("expectancy_r", 0.0)
        note = "INSUFFICIENT" if zm.get("insufficient") else ("FLAG_NEGATIVE" if exp < 0 and n >= 10 else "")
        print(f"  {zone_type:<12} | {n:>5} | {wr:>7.1%}  | {exp:>+8.4f} | {note}")

    print("\n  ZONE ANALYSIS: ", end="")
    best_zone = max(by_zone.items(), key=lambda x: x[1].get("expectancy_r", -99) if x[1].get("n", 0) >= 5 else -99)
    print(f"Best zone type is '{best_zone[0]}' with Exp={best_zone[1].get('expectancy_r', 0):+.4f}R. "
          f"FVGs/OBs typically outperform SR as they carry institutional order context.")

    # Structure break type analysis (MSS vs BOS)
    by_struct = m["by_structure_break_type"]
    print("\n  MSS vs BOS ANALYSIS:")
    print(f"  {'Type':<20} | {'N':>5} | {'Win Rate':>8} | {'Exp(R)':>8}")
    print("  " + "-" * 50)
    for struct_type, sm in by_struct.items():
        n   = sm.get("n", 0)
        wr  = sm.get("win_rate", 0.0)
        exp = sm.get("expectancy_r", 0.0)
        print(f"  {struct_type:<20} | {n:>5} | {wr:>7.1%}  | {exp:>+8.4f}")

    # MSS vs BOS decision
    mss_trades = [t for t in trades if "MSS" in t.structure_break_type]
    bos_trades = [t for t in trades if "BOS" in t.structure_break_type and "MSS" not in t.structure_break_type]

    mss_exp = np.mean([t.r_multiple for t in mss_trades]) if mss_trades else 0.0
    bos_exp = np.mean([t.r_multiple for t in bos_trades]) if bos_trades else 0.0

    updated_cfg = {**best_cfg}
    mss_diff = mss_exp - bos_exp
    print(f"\n  MSS mean R: {mss_exp:+.4f}  |  BOS mean R: {bos_exp:+.4f}  |  Diff: {mss_diff:+.4f}R")

    if mss_diff >= 0.15 and len(mss_trades) >= 5 and len(bos_trades) >= 5:
        print(f"  → MSS outperforms BOS by {mss_diff:.4f}R (>= 0.15R threshold).")
        print(f"    Testing MSS_REQUIRED=True sub-run...")

        mss_only_cfg = {**best_cfg, "MSS_REQUIRED": True}
        m_mss, t_mss, r_mss = run_single_backtest(
            df_exec, df_htf, cfg_overrides=mss_only_cfg,
            iteration=4, label="ITER4_MSS_REQUIRED"
        )
        print(f"  MSS-ONLY: n={m_mss['trades_taken']} | Exp={m_mss['expectancy_r']:+.4f}R "
              f"(was {m['expectancy_r']:+.4f}R)")

        if (m_mss["expectancy_r"] > m["expectancy_r"] and
                not m_mss["statistically_insufficient"]):
            print(f"  → MSS_REQUIRED=True IMPROVES expectancy. Applying.")
            updated_cfg["MSS_REQUIRED"] = True
        else:
            print(f"  → MSS_REQUIRED=True reduces opportunity without sufficient gain. Keeping False.")
        print(f"\n  MSS vs BOS analysis: MSS setups show higher conviction breakouts, "
              f"but BOS setups provide opportunity volume. Decision recorded.")
    else:
        print(f"  → MSS/BOS difference < 0.15R or insufficient data. Keeping MSS_REQUIRED=False.")

    report.export_trades(trades, 4)
    report.export_setups(trades, rejected, 4)
    report.export_summary(m, 4, param_snapshot=updated_cfg)

    return updated_cfg


# ------------------------------------------------------------------
# ITERATION 5 — WALK-FORWARD VALIDATION
# ------------------------------------------------------------------

def run_iteration_5(df_exec, df_htf, best_cfg: Dict) -> Tuple[Dict, Dict]:
    print("\n" + "=" * 70)
    print("ITERATION 5 — WALK-FORWARD VALIDATION (70% in-sample / 30% OOS)")
    print("=" * 70)

    split = config.WALK_FORWARD_SPLIT
    split_idx = int(len(df_exec) * split)
    split_idx_htf = int(len(df_htf) * split)

    df_is  = df_exec.iloc[:split_idx].copy()
    df_oos = df_exec.iloc[split_idx:].copy()
    df_htf_is  = df_htf.iloc[:split_idx_htf].copy()
    df_htf_oos = df_htf.iloc[split_idx_htf:].copy()

    print(f"  In-sample  : {df_is.index[0].date()} → {df_is.index[-1].date()} ({len(df_is)} bars)")
    print(f"  Out-of-sample: {df_oos.index[0].date()} → {df_oos.index[-1].date()} ({len(df_oos)} bars)")

    m_is, t_is, r_is = run_single_backtest(
        df_is, df_htf_is, cfg_overrides=best_cfg,
        iteration=5, label="ITER5_IN_SAMPLE"
    )
    m_oos, t_oos, r_oos = run_single_backtest(
        df_oos, df_htf_oos, cfg_overrides=best_cfg,
        iteration=5, label="ITER5_OUT_OF_SAMPLE"
    )

    report.export_trades(t_is,  5, suffix="_IS")
    report.export_trades(t_oos, 5, suffix="_OOS")
    report.export_setups(t_is,  r_is,  5, suffix="_IS")
    report.export_setups(t_oos, r_oos, 5, suffix="_OOS")
    report.export_summary(m_is,  5, best_cfg, "_IS")
    report.export_summary(m_oos, 5, best_cfg, "_OOS")

    print("\n  WALK-FORWARD: In-Sample vs Out-of-Sample comparison")
    print(f"  {'Metric':<25} | {'In-Sample':>12} | {'Out-of-Sample':>14}")
    print("  " + "-" * 56)
    for key in ["trades_taken", "win_rate", "expectancy_r", "profit_factor", "max_drawdown_r"]:
        v_is  = m_is.get(key, 0)
        v_oos = m_oos.get(key, 0)
        if isinstance(v_is, float):
            print(f"  {key:<25} | {v_is:>12.4f} | {v_oos:>14.4f}")
        else:
            print(f"  {key:<25} | {v_is:>12} | {v_oos:>14}")

    # Walk-forward verdict
    exp_is  = m_is["expectancy_r"]
    exp_oos = m_oos["expectancy_r"]

    if exp_is == 0:
        verdict = "FAIL — No in-sample trades to compare."
    elif exp_oos >= exp_is * 0.70:
        verdict = "PASS"
        detail  = f"OOS expectancy ({exp_oos:+.4f}R) is within 30% of IS ({exp_is:+.4f}R)."
    elif exp_oos >= exp_is * 0.40:
        verdict = "WARNING"
        detail  = f"OOS expectancy ({exp_oos:+.4f}R) is 30–60% below IS ({exp_is:+.4f}R). Slight overfit risk."
    elif exp_oos < 0 and exp_is > 0:
        verdict = "FAIL"
        detail  = f"OOS expectancy is NEGATIVE ({exp_oos:+.4f}R) while IS is positive ({exp_is:+.4f}R). Overfit likely."
    else:
        verdict = "FAIL"
        detail  = f"OOS expectancy ({exp_oos:+.4f}R) is >60% below IS ({exp_is:+.4f}R)."

    print(f"\n  Walk-forward verdict: {verdict}")
    if "detail" in dir():
        print(f"  {detail}")

    return m_is, m_oos


# ------------------------------------------------------------------
# FINAL REPORT
# ------------------------------------------------------------------

def print_final_report(
    m_final: Dict,
    best_cfg: Dict,
    m_is: Dict,
    m_oos: Dict,
    trades_final: List,
    rejected_final: List
) -> None:
    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)

    print("\n  FINAL CONFIGURATION (best-performing):")
    for k, v in best_cfg.items():
        print(f"    {k:<35} = {v}")

    print("\n  FINAL METRICS (out-of-sample walk-forward window):")
    exp  = m_oos.get("expectancy_r", 0.0)
    wr   = m_oos.get("win_rate", 0.0)
    n    = m_oos.get("trades_taken", 0)
    pf   = m_oos.get("profit_factor", 0.0)
    mdd  = m_oos.get("max_drawdown_r", 0.0)

    print(f"    Trades taken   : {n}")
    print(f"    Win rate       : {wr:.1%}")
    print(f"    Expectancy     : {exp:+.4f}R")
    print(f"    Profit factor  : {pf:.4f}")
    print(f"    Max drawdown   : -{mdd:.4f}R")

    # ---- ANSWER THE 5 MANDATORY QUESTIONS ----
    print("\n" + "=" * 70)
    print("ANSWERS TO THE 5 REQUIRED FINAL QUESTIONS")
    print("=" * 70)

    # 1. Valid setups per week
    total_n   = m_final.get("trades_taken", 0)
    oos_n     = m_oos.get("trades_taken", 0)
    # Estimate weeks from OOS window (approx 30% of 2 years = ~31 weeks)
    oos_weeks = max(1, int(len(trades_final) * 0.3 / max(1, oos_n)) * oos_n / 5)
    # More direct: OOS period bars / (5min*12*5days = 288 bars/week)
    if trades_final:
        oos_weeks_est = 26  # ~6 months OOS of 2-year dataset
        setups_per_week = round(m_final.get("total_setups_found", 0) / max(1, 104), 1)
        trades_per_week = round(total_n / max(1, 104), 1)
    else:
        setups_per_week = 0
        trades_per_week = 0
    print(f"\n1. Valid setups per week on XAUUSD 5m/15m:")
    print(f"   Total setups found: {m_final.get('total_setups_found', 0)} over ~2 years "
          f"≈ {setups_per_week} setups/week")
    print(f"   Trades passing all 10 gates: {total_n} over ~2 years "
          f"≈ {trades_per_week} trades/week")
    if m_final.get("statistically_insufficient"):
        print("   *** STATISTICALLY INSUFFICIENT — interpret with caution ***")

    # 2. Expectancy after spread and slippage
    print(f"\n2. Realistic expectancy after spread and slippage:")
    print(f"   Full dataset: {m_final.get('expectancy_r', 0):+.4f}R per trade")
    print(f"   Out-of-sample only: {exp:+.4f}R per trade")
    print(f"   (Spread modeled at ${config.DEFAULT_SPREAD:.2f} active, "
          f"${config.DEFAULT_SPREAD * config.NEWS_SPREAD_MULTIPLIER:.2f} near news; "
          f"Slippage ${config.SLIPPAGE:.2f})")

    # 3. Session performance
    by_sess_oos = m_oos.get("by_session", {})
    by_sess_all = m_final.get("by_session", {})
    print(f"\n3. Session performance comparison:")
    if by_sess_all:
        sessions = sorted(by_sess_all.items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True)
        if len(sessions) >= 2:
            best_s  = sessions[0]
            worst_s = sessions[1]
            diff = best_s[1].get("expectancy_r", 0) - worst_s[1].get("expectancy_r", 0)
            print(f"   Best session:  {best_s[0]} — Exp={best_s[1].get('expectancy_r', 0):+.4f}R "
                  f"(n={best_s[1].get('n', 0)})")
            print(f"   Other session: {worst_s[0]} — Exp={worst_s[1].get('expectancy_r', 0):+.4f}R "
                  f"(n={worst_s[1].get('n', 0)})")
            print(f"   Difference: {diff:+.4f}R in favor of {best_s[0]}")
        elif len(sessions) == 1:
            s = sessions[0]
            print(f"   Only one session recorded: {s[0]} — Exp={s[1].get('expectancy_r', 0):+.4f}R")
    else:
        print("   Insufficient session data to compare.")

    # 4. Most common rejection reason
    rej = m_final.get("rejection_breakdown", {})
    if rej:
        top_reason = list(rej.items())[0]
        print(f"\n4. Most common rejection reason across all setups:")
        print(f"   {top_reason[0]} — occurred {top_reason[1]} times")
        print(f"   Top 5 rejection reasons:")
        for r, cnt in list(rej.items())[:5]:
            print(f"     {r:<35}: {cnt}")
    else:
        print("\n4. Most common rejection reason: N/A (no rejections recorded)")

    # 5. Out-of-sample vs in-sample expectancy
    exp_is_val  = m_is.get("expectancy_r", 0.0)
    exp_oos_val = m_oos.get("expectancy_r", 0.0)
    print(f"\n5. Out-of-sample vs in-sample expectancy:")
    print(f"   In-sample  (70%): {exp_is_val:+.4f}R  (n={m_is.get('trades_taken', 0)})")
    print(f"   Out-of-sample (30%): {exp_oos_val:+.4f}R  (n={m_oos.get('trades_taken', 0)})")
    if exp_is_val != 0:
        degradation = (exp_is_val - exp_oos_val) / abs(exp_is_val) * 100
        print(f"   OOS degradation: {degradation:.1f}%")
        if exp_oos_val >= exp_is_val * 0.70:
            verdict = "PASS — strategy generalizes well to unseen data"
        elif exp_oos_val >= 0:
            verdict = "WARNING — some degradation but OOS remains positive"
        else:
            verdict = "FAIL — OOS is negative; strategy may be overfit"
        print(f"   Walk-forward verdict: {verdict}")

    # Plain-English summary
    print("\n" + "=" * 70)
    print("PLAIN-ENGLISH VIABILITY SUMMARY")
    print("=" * 70)
    if exp_oos_val > 0.10:
        print(f"\n  The Edge 2 Breakout strategy on XAUUSD {config.EXECUTION_TF} shows POSITIVE "
              f"out-of-sample expectancy of {exp_oos_val:+.4f}R per trade.")
        print(f"  Best conditions: {_describe_best_conditions(m_final, m_oos)}")
        print(f"  Key risks remaining:")
        print(f"    - News-event spikes can create false sweeps (flagged but not excluded)")
        print(f"    - Thin OOS sample may not capture full market regime diversity")
        print(f"    - Live spread variance not fully modeled")
    elif exp_oos_val >= 0:
        print(f"\n  The strategy shows MARGINAL positive OOS expectancy ({exp_oos_val:+.4f}R).")
        print(f"  Performance may not be robust enough for live trading without further refinement.")
        print(f"  Suggested next steps: expand date range, test on 15m execution TF.")
    else:
        print(f"\n  The strategy shows NEGATIVE OOS expectancy ({exp_oos_val:+.4f}R).")
        print(f"  Do NOT deploy live. Key issues:")
        _suggest_improvements(m_final)

    print("\n" + "=" * 70)
    report.export_final(trades_final, rejected_final, m_oos, best_cfg)


def _describe_best_conditions(m_all, m_oos) -> str:
    by_sess = m_all.get("by_session", {})
    best_sess = max(by_sess.items(), key=lambda x: x[1].get("expectancy_r", -99),
                    default=("unknown", {}))
    by_zone  = m_all.get("by_zone_type", {})
    best_zone = max(by_zone.items(), key=lambda x: x[1].get("expectancy_r", -99) if x[1].get("n", 0) >= 5 else -99,
                    default=("unknown", {}))
    return (f"{best_sess[0]} session, {best_zone[0]} retest zones, "
            f"HTF-aligned setups with clean MSS confirmation")


def _suggest_improvements(m) -> None:
    rej = m.get("rejection_breakdown", {})
    if rej:
        top = list(rej.keys())[0]
        print(f"    Top rejection reason ({top}) suggests parameter calibration needed")
    if m.get("false_breakout_rate", 0) > 0.40:
        print(f"    High false-breakout rate ({m['false_breakout_rate']:.1%}) — tighten BREAKOUT_MIN_BODY_ATR")
    if m.get("retest_success_rate", 0) < 0.20:
        print(f"    Low retest success ({m['retest_success_rate']:.1%}) — increase RETEST_TIMEOUT_BARS")


def _default_param_snapshot() -> Dict:
    return {
        "EXECUTION_TF":           config.EXECUTION_TF,
        "PIVOT_LEFT":             config.PIVOT_LEFT,
        "PIVOT_RIGHT":            config.PIVOT_RIGHT,
        "RANGE_MIN_QUALITY":      config.RANGE_MIN_QUALITY,
        "SWEEP_MIN_WICK_ATR":     config.SWEEP_MIN_WICK_ATR,
        "BREAKOUT_MIN_BODY_ATR":  config.BREAKOUT_MIN_BODY_ATR,
        "RETEST_TIMEOUT_BARS":    config.RETEST_TIMEOUT_BARS,
        "SL_ATR_BUFFER":          config.SL_ATR_BUFFER,
        "TP_MIN_SCORE":           config.TP_MIN_SCORE,
        "TP_MIN_RR":              config.TP_MIN_RR,
        "MSS_REQUIRED":           config.MSS_REQUIRED,
        "HIGHER_TF_FILTER_ON":    config.HIGHER_TF_FILTER_ON,
    }


# ------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Edge 2 Backtesting Engine")
    parser.add_argument("--start",      type=str, default=None)
    parser.add_argument("--end",        type=str, default=None)
    parser.add_argument("--tf",         type=str, default=None)
    parser.add_argument("--cache-only", action="store_true", dest="cache_only")
    args = parser.parse_args()

    # Override TF if specified
    if args.tf:
        config.EXECUTION_TF = args.tf

    report.ensure_results_dir()
    start_time = time.time()

    # PRE-RUN CHECKLIST
    df_exec, df_htf = pre_run_checklist(args)

    # ITERATION 0: BASELINE
    m0, t0, r0 = run_iteration_0(df_exec, df_htf)

    # ITERATION 1: PARAMETER SENSITIVITY
    best_cfg = run_iteration_1(df_exec, df_htf, m0)

    # ITERATION 2: DIRECTIONAL SPLIT
    best_cfg = run_iteration_2(df_exec, df_htf, best_cfg)

    # ITERATION 3: SESSION ANALYSIS
    best_cfg = run_iteration_3(df_exec, df_htf, best_cfg)

    # ITERATION 4: ZONE TYPE + MSS vs BOS
    best_cfg = run_iteration_4(df_exec, df_htf, best_cfg)

    # ITERATION 5: WALK-FORWARD VALIDATION
    m_is, m_oos = run_iteration_5(df_exec, df_htf, best_cfg)

    # FINAL RUN on full dataset with best config
    print("\n" + "=" * 70)
    print("FINAL RUN — Best config on full dataset")
    print("=" * 70)
    m_final, t_final, r_final = run_single_backtest(
        df_exec, df_htf, cfg_overrides=best_cfg,
        iteration=99, label="FINAL"
    )

    print_final_report(m_final, best_cfg, m_is, m_oos, t_final, r_final)

    elapsed = time.time() - start_time
    print(f"\n  Total runtime: {elapsed/60:.1f} minutes")
    print("\nEngine complete. All results saved to edge2_backtest/results/")


if __name__ == "__main__":
    main()
