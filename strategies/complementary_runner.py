"""
complementary_runner.py — Orchestrates all four CS strategies each bar.

Called ONCE per bar AFTER state is computed (range, sweep, regime, HTF)
but BEFORE the Edge 2 gate chain runs.

Priority order (spec-mandated): CS4 > CS1 > CS3 > CS2
If multiple signals fire on the same bar: return the highest-priority one.
If no signal: return None → Edge 2 gate chain runs as normal.

HIGH_VOLATILITY regime blocks ALL CS strategies (checked inside each strategy,
but also guarded here as an outer fast-exit).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
from strategies.cs_signal import CSSignal

from strategies import cs1_range_edge
from strategies import cs2_round_number
from strategies import cs3_micro_compression
from strategies import cs4_bounce


def run_complementary_checks(
    range_state,
    liquidity_engine,
    structure_engine,
    htf_aligned_series,
    htf_rsi_series,
    regime_info: dict,
    df_exec,
    atr_exec,
    bar_i: int,
    cfg: dict = None,
    tp_engine=None,
) -> "CSSignal | None":
    """
    Run all CS strategy checks for bar_i.
    Returns the highest-priority CSSignal that fires, or None.

    Parameters
    ----------
    range_state        : RangeState from range_engine (already computed this bar)
    liquidity_engine   : LiquidityEngine instance (sweep/PDH/PDL/session state)
    structure_engine   : StructureEngine instance (swing highs/lows, MSS/BOS flags)
    htf_aligned_series : pd.Series of HTF structure labels aligned to exec TF index
    htf_rsi_series     : pd.Series of HTF RSI(14) aligned to exec TF index
    regime_info        : dict with 'regime', 'trend_direction', etc.
    df_exec            : execution TF OHLCV DataFrame
    atr_exec           : ATR series for execution TF
    bar_i              : current bar index (integer)
    cfg                : optional config override dict
    tp_engine          : TPEngine instance (shared with Edge 2)
    """
    _cfg = cfg or {}

    def cp(key, default=None):
        return _cfg.get(key, getattr(config, key, default))

    # Outer guard: HIGH_VOLATILITY blocks all CS strategies
    regime = (regime_info or {}).get("regime", "RANGING")
    if regime == "HIGH_VOLATILITY":
        return None

    signals = []

    # Collect signals from enabled strategies
    if cp("CS1_ENABLED", True):
        try:
            sig = cs1_range_edge.check(
                range_state=range_state,
                liquidity_engine=liquidity_engine,
                structure_engine=structure_engine,
                htf_aligned_series=htf_aligned_series,
                regime_info=regime_info,
                df_exec=df_exec,
                atr_exec=atr_exec,
                bar_i=bar_i,
                cfg=_cfg,
                tp_engine=tp_engine,
            )
            if sig is not None:
                signals.append(sig)
        except Exception:
            pass  # strategy errors must never crash the main loop

    if cp("CS2_ENABLED", True):
        try:
            sig = cs2_round_number.check(
                range_state=range_state,
                liquidity_engine=liquidity_engine,
                structure_engine=structure_engine,
                htf_aligned_series=htf_aligned_series,
                regime_info=regime_info,
                df_exec=df_exec,
                atr_exec=atr_exec,
                bar_i=bar_i,
                cfg=_cfg,
                tp_engine=tp_engine,
            )
            if sig is not None:
                signals.append(sig)
        except Exception:
            pass

    if cp("CS3_ENABLED", True):
        try:
            sig = cs3_micro_compression.check(
                range_state=range_state,
                liquidity_engine=liquidity_engine,
                structure_engine=structure_engine,
                regime_info=regime_info,
                df_exec=df_exec,
                atr_exec=atr_exec,
                bar_i=bar_i,
                cfg=_cfg,
                tp_engine=tp_engine,
            )
            if sig is not None:
                signals.append(sig)
        except Exception:
            pass

    if cp("CS4_ENABLED", True):
        try:
            sig = cs4_bounce.check(
                range_state=range_state,
                liquidity_engine=liquidity_engine,
                structure_engine=structure_engine,
                htf_aligned_series=htf_aligned_series,
                htf_rsi_series=htf_rsi_series,
                regime_info=regime_info,
                df_exec=df_exec,
                atr_exec=atr_exec,
                bar_i=bar_i,
                cfg=_cfg,
                tp_engine=tp_engine,
            )
            if sig is not None:
                signals.append(sig)
        except Exception:
            pass

    if not signals:
        return None

    # Apply priority order: CS4 > CS1 > CS3 > CS2
    priority_order = ["CS4", "CS1", "CS3", "CS2"]
    for strategy_name in priority_order:
        match = next((s for s in signals if s.strategy == strategy_name), None)
        if match is not None:
            return match

    return signals[0]
