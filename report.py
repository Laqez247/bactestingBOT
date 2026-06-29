"""
report.py — Console output, CSV export, and summary text file generation.
Exports: trades_ITER_N.csv, setups_ITER_N.csv, summary_ITER_N.txt
"""

import os
import csv
import pandas as pd
from typing import List, Dict, Any
from trade_simulator import TradeRecord
import config

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def ensure_results_dir() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)


def _trade_to_dict(t: TradeRecord) -> dict:
    return {
        "timestamp":            t.timestamp,
        "symbol":               t.symbol,
        "timeframe":            t.timeframe,
        "iteration":            t.iteration,
        "direction":            t.direction,
        "setup_id":             t.setup_id,
        "range_high":           t.range_high,
        "range_low":            t.range_low,
        "range_quality_score":  t.range_quality_score,
        "range_bars":           t.range_bars,
        "range_touches_high":   t.range_touches_high,
        "range_touches_low":    t.range_touches_low,
        "atr_at_setup":         t.atr_at_setup,
        "range_height_atr":     t.range_height_atr,
        "ssl_bsl_sweep_type":   t.ssl_bsl_sweep_type,
        "sweep_bar":            t.sweep_bar,
        "sweep_wick_size_atr":  t.sweep_wick_size_atr,
        "sweep_wick_size_abs":  t.sweep_wick_size_abs,
        "structure_break_type": t.structure_break_type,
        "breakout_bar":         t.breakout_bar,
        "breakout_direction":   t.breakout_direction,
        "breakout_body_atr":    t.breakout_body_atr,
        "breakout_quality":     t.breakout_quality,
        "htf_trend":            t.htf_trend,
        "retest_zone_type":     t.retest_zone_type,
        "retest_zone_top":      t.retest_zone_top,
        "retest_zone_bottom":   t.retest_zone_bottom,
        "entry_price":          t.entry_price,
        "sl_price":             t.sl_price,
        "tp1_price":            t.tp1_price,
        "tp2_price":            t.tp2_price,
        "tp_score":             t.tp_score,
        "rr_tp1":               t.rr_tp1,
        "rr_tp2":               t.rr_tp2,
        "rejection_reason":     t.rejection_reason,
        "entry_bar":            t.entry_bar,
        "exit_bar":             t.exit_bar,
        "exit_reason":          t.exit_reason,
        "exit_price":           t.exit_price,
        "r_multiple":           t.r_multiple,
        "mae_abs":              t.mae_abs,
        "mfe_abs":              t.mfe_abs,
        "bars_held":            t.bars_held,
        "session":              t.session,
        # Phase 2 adaptive override fields
        "override_type":        getattr(t, "override_type",        "NONE"),
        "compensation_score":   getattr(t, "compensation_score",   0.0),
        "compensation_breakdown": getattr(t, "compensation_breakdown", ""),
        # Complementary strategy fields
        "strategy_type":        getattr(t, "strategy_type",        "EDGE2"),
        "modification_type":    getattr(t, "modification_type",    "NONE"),
        "market_regime":        getattr(t, "market_regime",        ""),
        "cs_setup_context":     getattr(t, "cs_setup_context",     ""),
    }


def export_trades(trades: List[TradeRecord], iteration: int,
                  suffix: str = "") -> str:
    """Export completed trades to CSV. Returns file path."""
    ensure_results_dir()
    fname = f"trades_ITER_{iteration}{suffix}.csv"
    path  = os.path.join(RESULTS_DIR, fname)
    rows  = [_trade_to_dict(t) for t in trades]
    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
    else:
        # Write empty CSV with correct headers
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(_trade_to_dict(TradeRecord("", 0, "", "", "")).keys())
    return path


def export_setups(trades: List[TradeRecord],
                  rejected: List[TradeRecord],
                  iteration: int, suffix: str = "") -> str:
    """Export all candidate setups (traded + rejected) to CSV."""
    ensure_results_dir()
    fname = f"setups_ITER_{iteration}{suffix}.csv"
    path  = os.path.join(RESULTS_DIR, fname)
    all_setups = trades + rejected
    rows = [_trade_to_dict(t) for t in all_setups]
    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
    return path


def export_summary(metrics: Dict, iteration: int,
                   param_snapshot: Dict = None, suffix: str = "") -> str:
    """Write human-readable summary_ITER_N.txt."""
    ensure_results_dir()
    fname = f"summary_ITER_{iteration}{suffix}.txt"
    path  = os.path.join(RESULTS_DIR, fname)

    lines = []
    lines.append("=" * 70)
    lines.append(f"EDGE 2 BACKTEST SUMMARY — ITERATION {iteration}{suffix}")
    lines.append("=" * 70)
    lines.append(f"Label: {metrics.get('label', '')}")
    if metrics.get("statistically_insufficient"):
        lines.append("  *** STATISTICALLY INSUFFICIENT (< 30 trades) ***")
    lines.append("")
    lines.append(f"  Total setups found   : {metrics['total_setups_found']}")
    lines.append(f"  Setups rejected      : {metrics['setups_rejected']}")
    lines.append(f"  Trades taken         : {metrics['trades_taken']}")
    lines.append(f"  Win rate             : {metrics['win_rate']:.1%}")
    lines.append(f"  Loss rate            : {metrics['loss_rate']:.1%}")
    lines.append(f"  Expectancy (R)       : {metrics['expectancy_r']:+.4f}")
    lines.append(f"  Profit factor        : {metrics['profit_factor']:.4f}")
    lines.append(f"  Max drawdown (R)     : -{metrics['max_drawdown_r']:.4f}")
    lines.append(f"  Avg R win            : {metrics['avg_r_win']:+.4f}")
    lines.append(f"  Avg R loss           : {metrics['avg_r_loss']:+.4f}")
    lines.append(f"  Longest win streak   : {metrics['longest_win_streak']}")
    lines.append(f"  Longest loss streak  : {metrics['longest_loss_streak']}")
    lines.append(f"  Avg bars held        : {metrics['avg_bars_held']:.1f}")
    lines.append(f"  False breakout rate  : {metrics['false_breakout_rate']:.1%}")
    lines.append(f"  Retest success rate  : {metrics['retest_success_rate']:.1%}")
    lines.append("")

    # Rejection breakdown
    rb = metrics.get("rejection_breakdown", {})
    if rb:
        lines.append("  REJECTION REASONS:")
        for reason, count in rb.items():
            lines.append(f"    {reason:<30} : {count}")
        lines.append("")

    # Session breakdown
    by_sess = metrics.get("by_session", {})
    if by_sess:
        lines.append("  SESSION BREAKDOWN:")
        _format_breakdown_table(lines, by_sess)
        lines.append("")

    # Direction breakdown
    by_dir = metrics.get("by_direction", {})
    if by_dir:
        lines.append("  DIRECTION BREAKDOWN:")
        _format_breakdown_table(lines, by_dir)
        lines.append("")

    # Zone type breakdown
    by_zone = metrics.get("by_zone_type", {})
    if by_zone:
        lines.append("  ZONE TYPE BREAKDOWN:")
        _format_breakdown_table(lines, by_zone)
        lines.append("")

    # Structure type breakdown
    by_struct = metrics.get("by_structure_break_type", {})
    if by_struct:
        lines.append("  STRUCTURE BREAK TYPE (MSS vs BOS):")
        _format_breakdown_table(lines, by_struct)
        lines.append("")

    # SSL/BSL type breakdown
    by_sweep = metrics.get("by_ssl_bsl_type", {})
    if by_sweep:
        lines.append("  SWEEP TYPE BREAKDOWN:")
        _format_breakdown_table(lines, by_sweep)
        lines.append("")

    # Strategy type breakdown (EDGE2 vs CS1–CS4)
    by_strat = metrics.get("by_strategy_type", {})
    if by_strat:
        lines.append("  STRATEGY TYPE BREAKDOWN:")
        _format_breakdown_table(lines, by_strat)
        lines.append("")

    # Parameter snapshot
    if param_snapshot:
        lines.append("  PARAMETER SNAPSHOT:")
        for k, v in param_snapshot.items():
            lines.append(f"    {k:<30} = {v}")
        lines.append("")

    lines.append("=" * 70)
    content = "\n".join(lines)

    with open(path, "w") as f:
        f.write(content)

    return path


def _format_breakdown_table(lines: list, breakdown: Dict) -> None:
    header = f"    {'Category':<24} | {'N':>5} | {'WR':>7} | {'Exp(R)':>8} | {'Stat':>12}"
    lines.append(header)
    lines.append("    " + "-" * 64)
    for key, m in sorted(breakdown.items()):
        n   = m.get("n", 0)
        wr  = m.get("win_rate", 0.0)
        exp = m.get("expectancy_r", 0.0)
        ins = "INSUFFICIENT" if m.get("insufficient") else "ok"
        lines.append(f"    {str(key):<24} | {n:>5} | {wr:>7.1%} | {exp:>+8.4f} | {ins:>12}")


def print_iteration_summary(metrics: Dict, iteration: int,
                             param_snapshot: Dict = None) -> None:
    """Print iteration summary to console."""
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  ITERATION {iteration} RESULTS — {metrics.get('label', '')}")
    print(sep)

    if metrics.get("statistically_insufficient"):
        print("  *** STATISTICALLY INSUFFICIENT (< 30 trades) ***")

    print(f"  Setups found:   {metrics['total_setups_found']:>6}")
    print(f"  Rejected:       {metrics['setups_rejected']:>6}")
    print(f"  Trades taken:   {metrics['trades_taken']:>6}")
    print(f"  Win rate:       {metrics['win_rate']:>7.1%}")
    print(f"  Expectancy:     {metrics['expectancy_r']:>+8.4f}R")
    print(f"  Profit factor:  {metrics['profit_factor']:>8.4f}")
    print(f"  Max drawdown:   {metrics['max_drawdown_r']:>8.4f}R")
    print(f"  False BOS rate: {metrics['false_breakout_rate']:>7.1%}")
    print(f"  Retest succ.:   {metrics['retest_success_rate']:>7.1%}")

    # Top 3 rejection reasons
    rb = metrics.get("rejection_breakdown", {})
    if rb:
        print("\n  TOP REJECTION REASONS:")
        for reason, count in list(rb.items())[:5]:
            print(f"    {reason:<32}: {count}")

    # Session table
    by_sess = metrics.get("by_session", {})
    if by_sess:
        print("\n  SESSION PERFORMANCE:")
        for sname, sm in by_sess.items():
            n   = sm.get("n", 0)
            wr  = sm.get("win_rate", 0.0)
            exp = sm.get("expectancy_r", 0.0)
            print(f"    {sname:<20} n={n:>4}  WR={wr:.1%}  Exp={exp:+.4f}R")

    if param_snapshot:
        print("\n  PARAMETER SNAPSHOT:")
        for k, v in param_snapshot.items():
            print(f"    {k:<32} = {v}")

    print(sep)


def export_final(trades: List[TradeRecord], rejected: List[TradeRecord],
                 metrics: Dict, best_config: Dict) -> None:
    """Export final trades, setups, config, and summary."""
    ensure_results_dir()

    # trades_FINAL.csv
    rows = [_trade_to_dict(t) for t in trades]
    if rows:
        pd.DataFrame(rows).to_csv(os.path.join(RESULTS_DIR, "trades_FINAL.csv"), index=False)

    # setups_FINAL.csv
    all_setups = trades + rejected
    rows2 = [_trade_to_dict(t) for t in all_setups]
    if rows2:
        pd.DataFrame(rows2).to_csv(os.path.join(RESULTS_DIR, "setups_FINAL.csv"), index=False)

    # config_FINAL.txt
    config_path = os.path.join(RESULTS_DIR, "config_FINAL.txt")
    with open(config_path, "w") as f:
        f.write("FINAL BEST CONFIGURATION\n")
        f.write("=" * 50 + "\n")
        for k, v in best_config.items():
            f.write(f"{k} = {v}\n")

    # summary_FINAL.txt
    export_summary(metrics, iteration=99, suffix="_FINAL")

    print(f"\nFinal exports written to: {RESULTS_DIR}/")
    print(f"  trades_FINAL.csv, setups_FINAL.csv, config_FINAL.txt, summary_99_FINAL.txt")
