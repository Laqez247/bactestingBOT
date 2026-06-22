"""
metrics.py — All performance metrics computed from completed trades.
Breakdowns by session, direction, zone type, structure type, sweep type, HTF alignment.
Anti-overfitting: marks results STATISTICALLY INSUFFICIENT if < 30 trades.
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
from trade_simulator import TradeRecord
import config

STAT_INSUFFICIENT_THRESHOLD = 30


def compute_metrics(trades: List[TradeRecord],
                    rejected: List[TradeRecord],
                    label: str = "") -> Dict[str, Any]:
    """
    Full metrics computation from a list of completed trades and rejected setups.
    Returns dict with all metric fields.
    """
    n_trades   = len(trades)
    n_rejected = len(rejected)
    n_setups   = n_trades + n_rejected

    insufficient = n_trades < STAT_INSUFFICIENT_THRESHOLD

    if n_trades == 0:
        return _empty_metrics(n_setups, n_rejected, label, insufficient)

    r_series = [t.r_multiple for t in trades]
    wins     = [r for r in r_series if r > 0]
    losses   = [r for r in r_series if r < 0]
    be       = [r for r in r_series if r == 0]

    win_rate  = len(wins)  / n_trades
    loss_rate = len(losses) / n_trades
    be_rate   = len(be)    / n_trades

    avg_r_win  = np.mean(wins)  if wins   else 0.0
    avg_r_loss = np.mean(losses) if losses else 0.0

    expectancy_r = (win_rate * avg_r_win) + (loss_rate * avg_r_loss)

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown in R
    cumulative = np.cumsum(r_series)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd_r = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    # Streaks
    longest_win_streak, longest_loss_streak = _compute_streaks(r_series)

    avg_bars_held = np.mean([t.bars_held for t in trades])

    # Rejection breakdown
    rejection_breakdown = _count_by_field(rejected, "rejection_reason")

    # Phase 2: Override type breakdown (completed trades only)
    by_override_type = _breakdown_by_field(trades, "override_type")
    override_trades = [t for t in trades if getattr(t, "override_type", "NONE") not in ("NONE", "", None)]
    n_override = len(override_trades)
    override_win_rate = (
        len([t for t in override_trades if t.r_multiple > 0]) / n_override
        if n_override > 0 else 0.0
    )
    override_expectancy = (
        sum(t.r_multiple for t in override_trades) / n_override
        if n_override > 0 else 0.0
    )

    # Session breakdown
    by_session = _breakdown_by_field(trades, "session")

    # Direction breakdown
    by_direction = _breakdown_by_field(trades, "direction")

    # Zone type breakdown
    by_zone_type = _breakdown_by_field(trades, "retest_zone_type")

    # Breakout quality
    by_breakout_quality = _breakdown_by_field(trades, "breakout_quality")

    # Range quality tier (premium vs standard)
    premium_trades  = [t for t in trades if t.range_quality_score >= 75]
    standard_trades = [t for t in trades if 50 <= t.range_quality_score < 75]
    by_range_quality = {
        "premium":  _simple_metrics(premium_trades),
        "standard": _simple_metrics(standard_trades),
    }

    # HTF alignment
    by_htf = _breakdown_by_field(trades, "htf_trend")

    # Structure break type
    by_structure_type = _breakdown_by_field(trades, "structure_break_type")
    _flag_mss_bos_anomaly(by_structure_type)

    # SSL/BSL sweep type
    by_sweep_type = _breakdown_by_field(trades, "ssl_bsl_sweep_type")

    # Retest success rate (retests / valid breakouts)
    n_retested = sum(1 for t in trades if t.rejection_reason == "" and t.entry_bar >= 0)
    n_breakout_setups = n_setups  # approximate
    retest_success_rate = n_retested / n_breakout_setups if n_breakout_setups > 0 else 0.0

    # False breakout rate — trades that hit SL quickly (< 5 bars)
    false_bos_trades = [t for t in trades if t.exit_reason == "SL" and t.bars_held < 5]
    false_breakout_rate = len(false_bos_trades) / n_trades if n_trades > 0 else 0.0

    return {
        "label":                label,
        "statistically_insufficient": insufficient,
        "total_setups_found":   n_setups,
        "setups_rejected":      n_rejected,
        "trades_taken":         n_trades,
        "win_rate":             round(win_rate,  4),
        "loss_rate":            round(loss_rate, 4),
        "breakeven_rate":       round(be_rate,   4),
        "avg_r_win":            round(avg_r_win,  4),
        "avg_r_loss":           round(avg_r_loss, 4),
        "expectancy_r":         round(expectancy_r, 4),
        "profit_factor":        round(profit_factor, 4),
        "max_drawdown_r":       round(max_dd_r, 4),
        "longest_win_streak":   longest_win_streak,
        "longest_loss_streak":  longest_loss_streak,
        "avg_bars_held":        round(avg_bars_held, 1),
        "rejection_breakdown":  rejection_breakdown,
        "by_session":           by_session,
        "by_direction":         by_direction,
        "by_zone_type":         by_zone_type,
        "by_breakout_quality":  by_breakout_quality,
        "by_range_quality":     by_range_quality,
        "by_htf_alignment":     by_htf,
        "by_structure_break_type": by_structure_type,
        "by_ssl_bsl_type":      by_sweep_type,
        "retest_success_rate":  round(retest_success_rate, 4),
        "false_breakout_rate":  round(false_breakout_rate, 4),
        # Phase 2 override metrics
        "by_override_type":     by_override_type,
        "n_override_trades":    n_override,
        "override_win_rate":    round(override_win_rate, 4),
        "override_expectancy":  round(override_expectancy, 4),
    }


def _empty_metrics(n_setups, n_rejected, label, insufficient) -> Dict:
    return {
        "label": label,
        "statistically_insufficient": True,
        "total_setups_found": n_setups,
        "setups_rejected": n_rejected,
        "trades_taken": 0,
        "win_rate": 0.0, "loss_rate": 0.0, "breakeven_rate": 0.0,
        "avg_r_win": 0.0, "avg_r_loss": 0.0,
        "expectancy_r": 0.0, "profit_factor": 0.0, "max_drawdown_r": 0.0,
        "longest_win_streak": 0, "longest_loss_streak": 0, "avg_bars_held": 0.0,
        "rejection_breakdown": {}, "by_session": {}, "by_direction": {},
        "by_zone_type": {}, "by_breakout_quality": {}, "by_range_quality": {},
        "by_htf_alignment": {}, "by_structure_break_type": {}, "by_ssl_bsl_type": {},
        "retest_success_rate": 0.0, "false_breakout_rate": 0.0,
        "by_override_type": {}, "n_override_trades": 0,
        "override_win_rate": 0.0, "override_expectancy": 0.0,
    }


def _simple_metrics(trades: List[TradeRecord]) -> Dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "expectancy_r": 0.0,
                "insufficient": True}
    r_series = [t.r_multiple for t in trades]
    wins     = [r for r in r_series if r > 0]
    losses   = [r for r in r_series if r < 0]
    wr       = len(wins) / n
    avg_w    = np.mean(wins)   if wins   else 0.0
    avg_l    = np.mean(losses) if losses else 0.0
    exp      = (wr * avg_w) + ((1 - wr) * avg_l)
    return {
        "n": n,
        "win_rate": round(wr, 4),
        "expectancy_r": round(exp, 4),
        "insufficient": n < STAT_INSUFFICIENT_THRESHOLD,
    }


def _breakdown_by_field(trades: List[TradeRecord], field: str) -> Dict:
    groups: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        key = getattr(t, field, "unknown") or "unknown"
        groups.setdefault(key, []).append(t)
    return {k: _simple_metrics(v) for k, v in groups.items()}


def _count_by_field(records: List[TradeRecord], field: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in records:
        key = getattr(r, field, "unknown") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


def _compute_streaks(r_series: List[float]):
    longest_win  = 0
    longest_loss = 0
    cur_win  = 0
    cur_loss = 0
    for r in r_series:
        if r > 0:
            cur_win += 1
            cur_loss = 0
        elif r < 0:
            cur_loss += 1
            cur_win  = 0
        else:
            cur_win  = 0
            cur_loss = 0
        longest_win  = max(longest_win,  cur_win)
        longest_loss = max(longest_loss, cur_loss)
    return longest_win, longest_loss


def _flag_mss_bos_anomaly(by_structure: Dict) -> None:
    """
    Log anomaly if MSS trades do NOT outperform BOS trades.
    Per spec: MSS setups should outperform BOS-only setups.
    """
    mss_long  = by_structure.get("MSS_BULLISH",  {})
    mss_short = by_structure.get("MSS_BEARISH",  {})
    bos_long  = by_structure.get("BOS_BULLISH",  {})
    bos_short = by_structure.get("BOS_BEARISH",  {})

    mss_exp = np.mean([
        mss_long.get("expectancy_r", 0.0),
        mss_short.get("expectancy_r", 0.0)
    ])
    bos_exp = np.mean([
        bos_long.get("expectancy_r", 0.0),
        bos_short.get("expectancy_r", 0.0)
    ])

    if bos_exp > mss_exp and (bos_long.get("n", 0) + bos_short.get("n", 0)) >= 10:
        for d in [mss_long, mss_short, bos_long, bos_short]:
            d["anomaly_flag"] = "BOS_OUTPERFORMS_MSS"


def diagnose_baseline(m: Dict) -> str:
    """
    Generate diagnostic paragraph for Iteration 0 analysis.
    Identifies flags per spec criteria.
    """
    flags = []
    n = m["trades_taken"]
    wr = m["win_rate"]
    exp = m["expectancy_r"]
    pf = m["profit_factor"]
    fbr = m["false_breakout_rate"]
    rsr = m["retest_success_rate"]

    if n < 30:
        flags.append(f"TRADE_COUNT_LOW ({n} < 30): Parameters may be too strict — flag for loosening.")
    if n > 500:
        flags.append(f"TRADE_COUNT_HIGH ({n} > 500): Parameters may be too loose — flag for tightening.")
    if wr < 0.35:
        flags.append(f"LOW_WIN_RATE ({wr:.1%}): Entry or zone logic likely has issues.")
    if wr > 0.70:
        flags.append(f"HIGH_WIN_RATE ({wr:.1%}): Possible overfitting in zone or TP scoring.")
    if exp < 0:
        flags.append(f"NEGATIVE_EXPECTANCY ({exp:.3f}R): Strategy is net negative — must identify root cause.")
    if pf < 1.0:
        flags.append(f"PROFIT_FACTOR_BELOW_1 ({pf:.3f}): Same as negative expectancy.")
    if fbr > 0.50:
        flags.append(f"HIGH_FALSE_BREAKOUT_RATE ({fbr:.1%}): Breakout filter too weak.")
    if rsr < 0.20:
        flags.append(f"LOW_RETEST_SUCCESS_RATE ({rsr:.1%}): Timeout may be too short or zones misaligned.")

    if not flags:
        diagnosis = "BASELINE ANALYSIS: Results look structurally sound. "
        diagnosis += f"{n} trades, {wr:.1%} win rate, {exp:.3f}R expectancy, {pf:.3f} profit factor. "
        diagnosis += "Proceeding to sensitivity testing."
    else:
        diagnosis = "BASELINE ANALYSIS: The following issues were detected:\n"
        for f in flags:
            diagnosis += f"  → {f}\n"
        diagnosis += f"Core metrics: {n} trades | {wr:.1%} WR | {exp:.3f}R expectancy | {pf:.3f} PF"

    return diagnosis
