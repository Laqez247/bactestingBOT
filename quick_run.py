#!/usr/bin/env python3
"""
quick_run.py — Runs a SINGLE full backtest (Iteration 0 only) and prints
a condensed results block for fast iterative analysis.

Usage:
    cd backtest && python quick_run.py --cache-only
    cd backtest && python quick_run.py --cache-only --cfg RANGE_MIN_QUALITY=40,MSS_REQUIRED=True
"""
import os, sys, argparse, json
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import config
import data_loader
import metrics as metrics_module
import report
from structure_engine import StructureEngine, compute_atr, run_htf_structure, align_htf_to_exec
from sr_engine import RangeEngine
from liquidity_engine import LiquidityEngine
from breakout_engine import BreakoutEngine
from zone_engine import ZoneEngine
from tp_engine import TPEngine
from trade_simulator import TradeSimulator, TradeRecord
from run_backtest import run_single_backtest, _default_param_snapshot

BEST_CFG_FILE = "./results/best_config.json"


def load_best_cfg():
    if os.path.exists(BEST_CFG_FILE):
        with open(BEST_CFG_FILE) as f:
            return json.load(f)
    return {}


def save_best_cfg(cfg: dict, metrics: dict):
    os.makedirs("./results", exist_ok=True)
    payload = {"cfg": cfg, "metrics": {
        "trades_taken": metrics.get("trades_taken"),
        "win_rate": round(metrics.get("win_rate", 0), 4),
        "expectancy_r": round(metrics.get("expectancy_r", 0), 4),
        "profit_factor": round(metrics.get("profit_factor", 0), 4),
        "max_drawdown_r": round(metrics.get("max_drawdown_r", 0), 4),
    }}
    with open(BEST_CFG_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  [SAVED] Best config written to {BEST_CFG_FILE}")


def is_better(new_m: dict, old_m: dict) -> bool:
    """Return True if new metrics are better overall."""
    n_new = new_m.get("trades_taken", 0)
    n_old = old_m.get("trades_taken", 0)
    if n_new < 30:
        return False  # not statistically valid
    exp_new = new_m.get("expectancy_r", 0)
    exp_old = old_m.get("expectancy_r", 0)
    wr_new  = new_m.get("win_rate", 0)
    # Score: weight expectancy heavily + win rate bonus
    score_new = exp_new * 2 + wr_new
    score_old = exp_old * 2 + old_m.get("win_rate", 0)
    return score_new > score_old


def parse_cfg_overrides(s: str) -> dict:
    """Parse 'KEY=VAL,KEY2=VAL2' into a dict with correct Python types."""
    if not s:
        return {}
    result = {}
    for part in s.split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        v = v.strip()
        if v.lower() == "true":
            result[k] = True
        elif v.lower() == "false":
            result[k] = False
        else:
            try:
                result[k] = int(v)
            except ValueError:
                try:
                    result[k] = float(v)
                except ValueError:
                    result[k] = v
    return result


def print_results(m: dict, cfg: dict, label: str = "RUN"):
    n   = m["trades_taken"]
    wr  = m["win_rate"]
    exp = m["expectancy_r"]
    pf  = m["profit_factor"]
    mdd = m["max_drawdown_r"]
    fbr = m["false_breakout_rate"]
    rsr = m["retest_success_rate"]
    insuf = "⚠ INSUFFICIENT" if m["statistically_insufficient"] else "✓ VALID"

    print(f"\n{'='*60}")
    print(f"  QUICK RUN — {label}")
    print(f"{'='*60}")
    print(f"  Sample    : {n} trades  {insuf}")
    print(f"  Win Rate  : {wr:.1%}")
    print(f"  Expectancy: {exp:+.4f}R")
    print(f"  Profit PF : {pf:.3f}")
    print(f"  Max DD    : -{mdd:.3f}R")
    print(f"  FBR       : {fbr:.1%}  (SL within 5 bars)")
    print(f"  Retest%   : {rsr:.1%}")

    print(f"\n  --- REJECTION BREAKDOWN ---")
    for reason, cnt in list(m["rejection_breakdown"].items())[:8]:
        print(f"    {reason:<45}: {cnt}")

    print(f"\n  --- ZONE TYPE ---")
    for zt, zm in sorted(m["by_zone_type"].items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True):
        print(f"    {zt:<8} n={zm['n']:>4}  WR={zm['win_rate']:.1%}  Exp={zm['expectancy_r']:+.4f}R")

    print(f"\n  --- SWEEP TYPE ---")
    for st, sm in sorted(m["by_ssl_bsl_type"].items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True):
        print(f"    {st:<25} n={sm['n']:>4}  WR={sm['win_rate']:.1%}  Exp={sm['expectancy_r']:+.4f}R")

    print(f"\n  --- STRUCTURE TYPE ---")
    for stype, sm in sorted(m["by_structure_break_type"].items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True):
        print(f"    {stype:<20} n={sm['n']:>4}  WR={sm['win_rate']:.1%}  Exp={sm['expectancy_r']:+.4f}R")

    print(f"\n  --- SESSION ---")
    for sess, sm in sorted(m["by_session"].items(), key=lambda x: x[1].get("expectancy_r", 0), reverse=True):
        print(f"    {sess:<15} n={sm['n']:>4}  WR={sm['win_rate']:.1%}  Exp={sm['expectancy_r']:+.4f}R")

    print(f"\n  --- DIRECTION ---")
    for d, dm in sorted(m["by_direction"].items()):
        print(f"    {d:<10} n={dm['n']:>4}  WR={dm['win_rate']:.1%}  Exp={dm['expectancy_r']:+.4f}R")

    # Phase 2: Override breakdown
    n_ov = m.get("n_override_trades", 0)
    if n_ov > 0:
        print(f"\n  --- PHASE 2 OVERRIDE TRADES (n={n_ov}) ---")
        ov_wr  = m.get("override_win_rate", 0)
        ov_exp = m.get("override_expectancy", 0)
        print(f"    Override WR       : {ov_wr:.1%}")
        print(f"    Override Expectancy: {ov_exp:+.4f}R")
        for ov_type, ov_m in m.get("by_override_type", {}).items():
            if ov_type not in ("NONE", ""):
                print(f"    {ov_type:<35} n={ov_m['n']:>3}  WR={ov_m['win_rate']:.1%}  Exp={ov_m['expectancy_r']:+.4f}R")
    else:
        print(f"\n  --- PHASE 2 OVERRIDES ---")
        print(f"    No override trades triggered this run")

    print(f"\n  --- KEY PARAMS ACTIVE ---")
    for k, v in cfg.items():
        if k in ("RANGE_MIN_QUALITY","MSS_REQUIRED","BREAKOUT_MIN_BODY_ATR",
                 "RETEST_TIMEOUT_BARS","SWEEP_MIN_WICK_ATR","SHORT_BLOCKED_SWEEPS",
                 "DISABLE_BB_ZONE","DISABLE_SR_ZONE","PIVOT_LEFT","PIVOT_RIGHT",
                 "SL_ATR_BUFFER","OB_DISPLACEMENT_ATR","FVG_MIN_SIZE_ATR",
                 "ENTRY_MODE","VOLATILITY_MIN_ATR","HIGHER_TF_FILTER_ON"):
            print(f"    {k:<35}: {v}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--cfg", default="", help="Overrides: KEY=VAL,KEY2=VAL2")
    parser.add_argument("--label", default="", help="Label for this run")
    parser.add_argument("--save-if-best", action="store_true", help="Save config if best so far")
    parser.add_argument("--london-only", action="store_true", help="Restrict entries to London session only")
    parser.add_argument("--start", default="", help="Override BACKTEST_START (YYYY-MM-DD)")
    parser.add_argument("--end", default="", help="Override BACKTEST_END (YYYY-MM-DD)")
    parser.add_argument(
        "--strategy-only",
        default="",
        metavar="STRATEGY",
        help=(
            "Run only one strategy in isolation. "
            "Options: edge2, cs1, cs2, cs3, cs4. "
            "Example: --strategy-only edge2"
        ),
    )
    args = parser.parse_args()

    cfg_overrides = parse_cfg_overrides(args.cfg)
    if args.london_only:
        cfg_overrides["TRADE_SESSIONS"] = {"london": config.TRADE_SESSIONS["london"]}

    # --strategy-only mode: disable all other strategies
    if args.strategy_only:
        strat = args.strategy_only.lower().strip()
        valid = {"edge2", "cs1", "cs2", "cs3", "cs4"}
        if strat not in valid:
            print(f"ERROR: --strategy-only must be one of {sorted(valid)}")
            sys.exit(1)
        # Disable all CS strategies, then re-enable only the requested one
        cfg_overrides["CS1_ENABLED"] = False
        cfg_overrides["CS2_ENABLED"] = False
        cfg_overrides["CS3_ENABLED"] = False
        cfg_overrides["CS4_ENABLED"] = False
        cfg_overrides["STRATEGY_ONLY_EDGE2"] = False
        if strat == "edge2":
            cfg_overrides["STRATEGY_ONLY_EDGE2"] = True  # disables CS runner entirely
        elif strat == "cs1":
            cfg_overrides["CS1_ENABLED"] = True
        elif strat == "cs2":
            cfg_overrides["CS2_ENABLED"] = True
        elif strat == "cs3":
            cfg_overrides["CS3_ENABLED"] = True
        elif strat == "cs4":
            cfg_overrides["CS4_ENABLED"] = True
        print(f"[STRATEGY-ONLY MODE] Running: {strat.upper()}")

    start_date = args.start or None
    end_date   = args.end   or None

    print(f"Loading data...")
    df_exec, df_htf = data_loader.load_data(
        start=config.BACKTEST_START,
        end=config.BACKTEST_END,
        use_cache=True,
        cache_only=args.cache_only
    )

    # Slice to user-specified date window (after loading from full cache)
    if start_date:
        df_exec = df_exec[df_exec.index >= pd.Timestamp(start_date, tz="UTC")]
        df_htf  = df_htf[df_htf.index  >= pd.Timestamp(start_date, tz="UTC")]
    if end_date:
        df_exec = df_exec[df_exec.index <= pd.Timestamp(end_date + " 23:59", tz="UTC")]
        df_htf  = df_htf[df_htf.index  <= pd.Timestamp(end_date + " 23:59", tz="UTC")]

    print(f"  {len(df_exec)} exec bars, {len(df_htf)} htf bars")

    label = args.label or ("CUSTOM" if cfg_overrides else "DEFAULT")
    print(f"Running backtest: {label}  overrides={cfg_overrides or 'none'}")

    m, trades, rejected = run_single_backtest(
        df_exec, df_htf,
        cfg_overrides=cfg_overrides,
        iteration=99,
        label=label
    )

    # Merge config + overrides for display
    full_cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    full_cfg.update(cfg_overrides)

    print_results(m, full_cfg, label)

    # Optionally save trades CSV
    os.makedirs("./results", exist_ok=True)
    safe_label = label.replace(" ", "_")
    report.export_trades(trades, 99, suffix=f"_{safe_label}")
    report.export_setups(trades, rejected, 99, suffix=f"_{safe_label}")

    if args.save_if_best:
        best = load_best_cfg()
        old_m = best.get("metrics", {"trades_taken": 0, "expectancy_r": -999, "win_rate": 0})
        if is_better(m, old_m):
            save_best_cfg(full_cfg, m)
            print(f"  ✓ New best! (was Exp={old_m['expectancy_r']:+.4f}R → now {m['expectancy_r']:+.4f}R)")
        else:
            print(f"  → Not better than best (Exp={old_m['expectancy_r']:+.4f}R with {old_m['trades_taken']} trades)")

    return m


if __name__ == "__main__":
    main()
