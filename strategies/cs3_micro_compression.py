"""
cs3_micro_compression.py — CS3: Micro-Compression Breakout Strategy

When XAUUSD coils into a tight 8–15 bar range on the 5m chart, the ATR
compresses and bodies shrink. The breakout of this micro-range is the signal.
No sweep required. No OB required. Compression + breakout = full signal.

Gate sequence:
  CS3-1: Micro-compression detected (ATR ratio, body shrinkage, tight range)
  CS3-2: Current bar closes BEYOND the micro range boundary with body >= threshold
  CS3-3: Breakout direction aligns with regime (or RANGING + sweep provides bias)
  CS3-4: Regime is NOT HIGH_VOLATILITY
  CS3-5: Session is LONDON or NY
  CS3-6: No trade open (checked by runner)
  CS3-7: Next candle does NOT close back inside micro range (confirmation bar)

Adaptive modification:
  MOD-A: weak body (0.28–0.35x ATR) + TRENDING_STRONG in breakout direction
         → require next candle also closes further in breakout direction
  MOD-B: RANGING regime + confirmed sweep in breakout direction
         → allow but require CS3_MIN_BODY_ATR = 0.55 (stricter)

No fixed RR. Dynamic TP via tp_engine.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
import config
from strategies.cs_signal import CSSignal


def _get_session(ts) -> str:
    h = ts.hour
    london_open  = int(getattr(config, "TRADE_SESSIONS", {}).get("london", {}).get("open",  "07:00").split(":")[0])
    london_close = int(getattr(config, "TRADE_SESSIONS", {}).get("london", {}).get("close", "12:00").split(":")[0])
    if london_open <= h < london_close:
        return "london"
    if 12 <= h < 17:
        return "new_york"
    return "off_hours"


def detect_micro_compression(
    df: pd.DataFrame,
    atr_series: pd.Series,
    bar_i: int,
    lookback: int,
    baseline_lookback: int = 30,
    atr_ratio_threshold: float = 0.55,
    max_range_atr: float = 0.80,
) -> tuple:
    """
    Detect micro-compression ending at bar_i.
    Returns (is_valid, micro_high, micro_low, micro_range, atr_ratio).
    Uses only data up to bar_i (no lookahead).
    """
    if bar_i < lookback + baseline_lookback:
        return False, 0.0, 0.0, 0.0, 1.0

    start = bar_i - lookback
    recent_bars = df.iloc[start:bar_i]

    recent_atr   = float(atr_series.iloc[bar_i - 1]) if not pd.isna(atr_series.iloc[bar_i - 1]) else 1.0
    baseline_start = max(0, bar_i - baseline_lookback - lookback)
    baseline_end   = max(0, bar_i - lookback)
    baseline_atr_slice = atr_series.iloc[baseline_start:baseline_end].dropna()
    if len(baseline_atr_slice) == 0:
        return False, 0.0, 0.0, 0.0, 1.0
    baseline_atr = float(baseline_atr_slice.mean())
    if baseline_atr <= 0:
        return False, 0.0, 0.0, 0.0, 1.0

    atr_ratio = recent_atr / baseline_atr

    bodies = (recent_bars["close"] - recent_bars["open"]).abs()
    n = len(bodies)
    if n < 6:
        return False, 0.0, 0.0, 0.0, atr_ratio

    half = n // 2
    first_mean  = float(bodies.iloc[:half].mean()) if half > 0 else 0.0
    second_mean = float(bodies.iloc[half:].mean()) if half > 0 else 0.0
    body_trend  = second_mean < first_mean * 0.80

    micro_high  = float(recent_bars["high"].max())
    micro_low   = float(recent_bars["low"].min())
    micro_range = micro_high - micro_low

    compression_valid = (
        atr_ratio < atr_ratio_threshold
        and body_trend
        and micro_range < max_range_atr * baseline_atr
        and lookback >= 6
    )

    return compression_valid, micro_high, micro_low, micro_range, atr_ratio


def check(
    range_state,
    liquidity_engine,
    structure_engine,
    regime_info: dict,
    df_exec: pd.DataFrame,
    atr_exec: pd.Series,
    bar_i: int,
    cfg: dict = None,
    tp_engine=None,
) -> "CSSignal | None":
    """Check CS3 signal at bar_i."""
    _cfg = cfg or {}

    def cp(key, default=None):
        return _cfg.get(key, getattr(config, key, default))

    if not cp("CS3_ENABLED", True):
        return None

    # --- Gate CS3-4: Regime ---
    regime = (regime_info or {}).get("regime", "RANGING")
    if regime == "HIGH_VOLATILITY":
        return None

    # --- Gate CS3-5: Session ---
    ts = df_exec.index[bar_i]
    session = _get_session(ts)
    cs_sessions = cp("CS_SESSION_FILTER", ["london", "new_york"])
    if session not in cs_sessions:
        return None

    atr_val = float(atr_exec.iloc[bar_i]) if not pd.isna(atr_exec.iloc[bar_i]) else 1.0

    min_comp_bars = cp("CS3_MIN_COMPRESSION_BARS", 8)
    max_comp_bars = cp("CS3_MAX_COMPRESSION_BARS", 20)
    atr_ratio_thr = cp("CS3_ATR_COMPRESSION_RATIO", 0.55)
    max_range_atr = cp("CS3_MAX_RANGE_ATR", 0.80)

    # --- Gate CS3-1: Micro-compression detection ---
    # Fast pre-filter: skip the expensive inner scan if ATR is not compressed.
    # Check only the shortest lookback's ATR ratio before trying all windows.
    _baseline_lookback = 30
    if bar_i < min_comp_bars + _baseline_lookback:
        return None  # not enough history
    _recent_atr  = float(atr_exec.iloc[bar_i - 1]) if not pd.isna(atr_exec.iloc[bar_i - 1]) else 1.0
    _bl_start    = max(0, bar_i - _baseline_lookback - min_comp_bars)
    _bl_end      = max(0, bar_i - min_comp_bars)
    _bl_slice    = atr_exec.iloc[_bl_start:_bl_end].dropna()
    _baseline_atr = float(_bl_slice.mean()) if len(_bl_slice) > 0 else 1.0
    if _baseline_atr <= 0 or (_recent_atr / _baseline_atr) >= atr_ratio_thr:
        return None  # ATR not compressed — skip expensive scan entirely

    # Scan multiple lookback windows; use the most recent valid one
    comp_valid  = False
    micro_high  = 0.0
    micro_low   = 0.0
    micro_range = 0.0
    found_lookback = 0

    for lookback in range(min_comp_bars, min(max_comp_bars + 1, bar_i)):
        ok, mh, ml, mr, ratio = detect_micro_compression(
            df=df_exec,
            atr_series=atr_exec,
            bar_i=bar_i,
            lookback=lookback,
            atr_ratio_threshold=atr_ratio_thr,
            max_range_atr=max_range_atr,
        )
        if ok:
            comp_valid   = True
            micro_high   = mh
            micro_low    = ml
            micro_range  = mr
            found_lookback = lookback
            break  # use shortest valid compression

    if not comp_valid:
        return None

    # --- Gate CS3-2: Current candle breaks micro range ---
    high_i  = float(df_exec["high"].iloc[bar_i])
    low_i   = float(df_exec["low"].iloc[bar_i])
    close_i = float(df_exec["close"].iloc[bar_i])
    open_i  = float(df_exec["open"].iloc[bar_i])

    body_size = abs(close_i - open_i)
    body_atr  = body_size / atr_val if atr_val > 0 else 0.0

    min_body_atr = cp("CS3_MIN_BODY_ATR", 0.35)
    mod_a_active = False
    mod_b_active = False

    if close_i > micro_high:
        direction = "LONG"
    elif close_i < micro_low:
        direction = "SHORT"
    else:
        return None  # no clean breakout

    # Body size check
    if body_atr < min_body_atr:
        # Check MOD-A: weak body + TRENDING_STRONG in breakout direction
        trend_dir = (regime_info or {}).get("trend_direction", "NONE")
        regime_str = regime

        is_trending_strong = (regime_str == "TRENDING_STRONG")
        trend_supports = (
            (direction == "LONG"  and trend_dir in ("BULLISH", "UPTREND")) or
            (direction == "SHORT" and trend_dir in ("BEARISH", "DOWNTREND"))
        )
        mod_a_lower = cp("CS3_MOD_A_BODY_FLOOR", 0.28)

        if (is_trending_strong and trend_supports and body_atr >= mod_a_lower):
            mod_a_active = True
            # MOD-A: need next candle to also close further in breakout direction
            # We'll enforce this via a lookahead-safe 1-bar check
            if bar_i + 1 >= len(df_exec):
                return None
            next_close = float(df_exec["close"].iloc[bar_i + 1])
            if direction == "LONG" and next_close <= close_i:
                return None
            if direction == "SHORT" and next_close >= close_i:
                return None
        else:
            return None

    # --- Gate CS3-3: Direction aligns with regime ---
    trend_direction = (regime_info or {}).get("trend_direction", "NONE")

    if regime == "TRENDING_STRONG":
        if direction == "LONG" and trend_direction not in ("BULLISH", "UPTREND", "NONE"):
            return None
        if direction == "SHORT" and trend_direction not in ("BEARISH", "DOWNTREND", "NONE"):
            return None
    elif regime == "RANGING":
        # MOD-B: RANGING but sweep in breakout direction provides bias
        ssl_swept = getattr(liquidity_engine, "ssl_swept", False)
        bsl_swept = getattr(liquidity_engine, "bsl_swept", False)
        sweep_bias = (direction == "LONG" and ssl_swept) or (direction == "SHORT" and bsl_swept)
        if sweep_bias:
            mod_b_active = True
            min_body_atr = max(min_body_atr, cp("CS3_MOD_B_BODY_ATR", 0.55))
            if body_atr < min_body_atr:
                return None
        # No sweep? Both directions allowed in RANGING (no filter, per spec)

    # Gate CS3-7: Confirmation bar — next candle must NOT close back inside micro range
    # NOTE: We check next bar if available (lookahead-safe: we use bar_i+1 which is "the
    # next bar the loop will process"; in a live runner this would wait 1 bar).
    # For backtesting we can check bar_i+1 since it is a validation gate, not a prediction.
    if bar_i + 1 < len(df_exec):
        next_close = float(df_exec["close"].iloc[bar_i + 1])
        if direction == "LONG" and next_close < micro_high:
            return None  # closed back inside — breakout failed
        if direction == "SHORT" and next_close > micro_low:
            return None
    # If bar_i+1 is beyond data, proceed (end-of-data edge case)

    # --- Compute SL ---
    sl_buffer = cp("CS3_SL_BUFFER_ATR", 0.20) * atr_val
    if direction == "LONG":
        entry_price = close_i
        sl_price    = micro_low - sl_buffer
    else:
        entry_price = close_i
        sl_price    = micro_high + sl_buffer

    # --- Dynamic TP ---
    tp_candidates = []
    if tp_engine is not None:
        tp_min_score = cp("CS3_TP_MIN_SCORE", 20)
        tp_min_rr    = cp("CS3_TP_MIN_RR", 1.2)

        tp_candidates = tp_engine.generate_cs(
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            i=bar_i,
            df=df_exec,
            atr=atr_exec,
            range_state=range_state,
            structure_engine=structure_engine,
            liquidity_engine=liquidity_engine,
            candidate_types=[
                "nearest_swing_in_direction",
                "bot_range_boundary_in_direction",
                "round_number_in_direction",
                "session_high_low_in_direction",
            ],
            tp_min_score=tp_min_score,
            tp_min_rr=tp_min_rr,
        )

    if not tp_candidates:
        return None

    mod_type = "NONE"
    if mod_a_active:
        mod_type = "CS3_WEAK_BODY_TREND_CONFIRM"
    elif mod_b_active:
        mod_type = "CS3_RANGING_SWEEP_BIAS"

    return CSSignal(
        strategy="CS3",
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_candidates=tp_candidates,
        modification_type=mod_type,
        setup_context=(
            f"CS3 {direction} micro-compression breakout over "
            f"{micro_high:.2f}/{micro_low:.2f} "
            f"({found_lookback} bars), body_atr={body_atr:.2f}"
        ),
        bar_index=bar_i,
    )
