"""
cs4_bounce.py — CS4: Oversold/Overbought Structural Bounce Strategy

When HTF (1H) RSI reaches an extreme (< CS4_RSI_OVERSOLD or > CS4_RSI_OVERBOUGHT)
AND the bot has detected a valid range with a sweep at the boundary, the bounce
toward the opposite side is high-conviction mean-reversion.

RSI extreme = context. Sweep at range boundary = precision trigger.

Gate sequence:
  CS4-1: HTF RSI in extreme territory (oversold for LONG, overbought for SHORT)
  CS4-2: Bot's range is valid (range_quality >= 45)
  CS4-3: Price within CS4_PROXIMITY_ATR × ATR of appropriate range boundary
  CS4-4: Sweep at that boundary in last CS4_SWEEP_LOOKBACK_BARS bars
  CS4-5: Sweep candle shows wick rejection
  CS4-6: Regime is NOT HIGH_VOLATILITY
  CS4-7: Session is LONDON or NY
  CS4-8: No trade open (checked by runner)
  CS4-9: HTF trend counter-trend check (stricter RSI threshold required)

Adaptive modification:
  MOD-A: RSI 32–36 (above LONG threshold) but wick > 0.60x ATR below range_low
         → deep sweep compensates for RSI not quite extreme yet
  MOD-B: Sweep 9–15 bars ago but RSI moved DEEPER into extreme since the sweep
         → extend lookback for fresh RSI exhaustion

No fixed RR. Dynamic TP via tp_engine.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
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


def check(
    range_state,
    liquidity_engine,
    structure_engine,
    htf_aligned_series,
    htf_rsi_series,          # pd.Series of HTF RSI(14) aligned to exec TF index
    regime_info: dict,
    df_exec: pd.DataFrame,
    atr_exec: pd.Series,
    bar_i: int,
    cfg: dict = None,
    tp_engine=None,
) -> "CSSignal | None":
    """Check CS4 signal at bar_i."""
    _cfg = cfg or {}

    def cp(key, default=None):
        return _cfg.get(key, getattr(config, key, default))

    if not cp("CS4_ENABLED", True):
        return None

    # --- Gate CS4-6: Regime ---
    regime = (regime_info or {}).get("regime", "RANGING")
    if regime == "HIGH_VOLATILITY":
        return None

    # --- Gate CS4-7: Session ---
    # CS4 uses its own session filter first; falls back to global CS_SESSION_FILTER.
    # Default: London-only — NY RSI bounces fail because US equity correlation
    # creates momentum continuation rather than the mean-reversion CS4 expects.
    ts = df_exec.index[bar_i]
    session = _get_session(ts)
    cs_sessions = cp("CS4_SESSION_FILTER", cp("CS_SESSION_FILTER", ["london"]))
    if session not in cs_sessions:
        return None

    # --- Gate CS4-1: HTF RSI extreme ---
    rsi_oversold   = cp("CS4_RSI_OVERSOLD", 32)
    rsi_overbought = cp("CS4_RSI_OVERBOUGHT", 68)
    rsi_extreme_override = cp("CS4_RSI_EXTREME_OVERRIDE", 28)

    htf_rsi = None
    if htf_rsi_series is not None and bar_i < len(htf_rsi_series):
        rsi_val = htf_rsi_series.iloc[bar_i]
        if not pd.isna(rsi_val):
            htf_rsi = float(rsi_val)

    if htf_rsi is None:
        return None

    is_oversold   = htf_rsi < rsi_oversold
    is_overbought = htf_rsi > rsi_overbought
    mod_a_active  = False
    mod_b_active  = False

    if not is_oversold and not is_overbought:
        # MOD-A: RSI slightly above threshold (32–36 for LONG, 64–68 for SHORT)
        mod_a_oversold_floor  = cp("CS4_MOD_A_RSI_FLOOR", 36)
        mod_a_overbought_ceil = cp("CS4_MOD_A_RSI_CEIL", 64)
        if rsi_oversold <= htf_rsi <= mod_a_oversold_floor:
            mod_a_active = True
            is_oversold  = True   # tentative — requires deep wick validation later
        elif mod_a_overbought_ceil <= htf_rsi <= rsi_overbought:
            mod_a_active  = True
            is_overbought = True
        else:
            return None

    direction = "LONG" if is_oversold else "SHORT"

    # --- Gate CS4-2: Range valid ---
    if not range_state.valid:
        return None
    if range_state.quality_score < cp("CS4_MIN_RANGE_QUALITY", 45):
        return None

    rh = range_state.range_high
    rl = range_state.range_low

    atr_val = float(atr_exec.iloc[bar_i]) if not pd.isna(atr_exec.iloc[bar_i]) else 1.0
    proximity_band = cp("CS4_PROXIMITY_ATR", 0.25) * atr_val

    high_i  = float(df_exec["high"].iloc[bar_i])
    low_i   = float(df_exec["low"].iloc[bar_i])
    close_i = float(df_exec["close"].iloc[bar_i])
    open_i  = float(df_exec["open"].iloc[bar_i])

    # --- Gate CS4-3: Price at appropriate boundary ---
    if direction == "LONG":
        near_boundary = low_i <= rl + proximity_band
    else:
        near_boundary = high_i >= rh - proximity_band

    if not near_boundary:
        return None

    # --- Gate CS4-4: Sweep at boundary in recent bars ---
    lookback_bars = cp("CS4_SWEEP_LOOKBACK_BARS", 8)
    mod_lookback  = cp("CS4_MOD_SWEEP_LOOKBACK", 15)

    ssl_swept = getattr(liquidity_engine, "ssl_swept", False)
    bsl_swept = getattr(liquidity_engine, "bsl_swept", False)
    ssl_bar   = getattr(liquidity_engine, "ssl_sweep_bar", -1)
    bsl_bar   = getattr(liquidity_engine, "bsl_sweep_bar", -1)

    sweep_found = False
    sweep_bars_ago = 0

    if direction == "LONG" and ssl_swept and ssl_bar >= 0:
        bars_since = bar_i - ssl_bar
        if bars_since <= lookback_bars:
            sweep_found    = True
            sweep_bars_ago = bars_since
        elif bars_since <= mod_lookback:
            # MOD-B: older sweep — check if RSI moved deeper since then
            sweep_bar_rsi = None
            if htf_rsi_series is not None and ssl_bar < len(htf_rsi_series):
                v = htf_rsi_series.iloc[ssl_bar]
                if not pd.isna(v):
                    sweep_bar_rsi = float(v)
            if sweep_bar_rsi is not None and htf_rsi < sweep_bar_rsi:
                # RSI moved deeper into oversold since the sweep
                mod_b_active   = True
                sweep_found    = True
                sweep_bars_ago = bars_since

    elif direction == "SHORT" and bsl_swept and bsl_bar >= 0:
        bars_since = bar_i - bsl_bar
        if bars_since <= lookback_bars:
            sweep_found    = True
            sweep_bars_ago = bars_since
        elif bars_since <= mod_lookback:
            sweep_bar_rsi = None
            if htf_rsi_series is not None and bsl_bar < len(htf_rsi_series):
                v = htf_rsi_series.iloc[bsl_bar]
                if not pd.isna(v):
                    sweep_bar_rsi = float(v)
            if sweep_bar_rsi is not None and htf_rsi > sweep_bar_rsi:
                mod_b_active   = True
                sweep_found    = True
                sweep_bars_ago = bars_since

    if not sweep_found:
        return None

    # --- Gate CS4-5: Sweep candle shows wick rejection ---
    if direction == "LONG":
        # Low swept below range_low AND close above range_low
        sweep_wick = rl - low_i
        rejection_ok = low_i < rl and close_i > rl
    else:
        sweep_wick   = high_i - rh
        rejection_ok = high_i > rh and close_i < rh

    if not rejection_ok:
        return None

    # MOD-A validation: requires deep wick > 0.60x ATR
    if mod_a_active:
        mod_wick_threshold = cp("CS4_MOD_WICK_THRESHOLD", 0.60) * atr_val
        if sweep_wick < mod_wick_threshold:
            return None

    # --- Gate CS4-9: HTF counter-trend check ---
    htf_trend = "RANGING"
    if htf_aligned_series is not None and bar_i < len(htf_aligned_series):
        htf_trend = str(htf_aligned_series.iloc[bar_i])

    if direction == "LONG" and htf_trend == "DOWNTREND":
        # Counter-trend bounce in downtrend: require stricter RSI
        if htf_rsi >= rsi_extreme_override:
            return None  # RSI not extreme enough for counter-trend
    elif direction == "SHORT" and htf_trend == "UPTREND":
        if htf_rsi <= (100 - rsi_extreme_override):
            return None

    # --- Compute SL ---
    sl_buffer_atr = cp("CS4_SL_BUFFER_ATR", 0.25)
    if mod_a_active:
        sl_buffer_atr += 0.10  # spec: MOD-A adds 0.10 to SL buffer

    if direction == "LONG":
        # SL at sweep_low (absolute low of the sweep bar)
        sweep_low = low_i
        entry_price = close_i
        sl_price    = sweep_low - sl_buffer_atr * atr_val
    else:
        sweep_high  = high_i
        entry_price = close_i
        sl_price    = sweep_high + sl_buffer_atr * atr_val

    # --- Dynamic TP ---
    tp_candidates = []
    if tp_engine is not None:
        tp_min_score = cp("CS4_TP_MIN_SCORE", 30)
        tp_min_rr    = cp("CS4_TP_MIN_RR", 1.5)

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
                "range_midpoint",
                "range_opposite_boundary",
                "nearest_swing_in_direction",
                "round_number_in_direction",
            ],
            tp_min_score=tp_min_score,
            tp_min_rr=tp_min_rr,
        )

    if not tp_candidates:
        return None

    mod_type = "NONE"
    if mod_a_active and mod_b_active:
        mod_type = "CS4_DEEP_WICK_EXTENDED_LOOKBACK"
    elif mod_a_active:
        mod_type = "CS4_DEEP_WICK_WEAK_RSI"
    elif mod_b_active:
        mod_type = "CS4_EXTENDED_SWEEP_LOOKBACK"

    return CSSignal(
        strategy="CS4",
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_candidates=tp_candidates,
        modification_type=mod_type,
        setup_context=(
            f"CS4 {direction} bounce: HTF RSI={htf_rsi:.1f}, "
            f"range={'low' if direction == 'LONG' else 'high'} "
            f"{rl if direction == 'LONG' else rh:.2f}, "
            f"sweep {sweep_bars_ago} bars ago, wick={sweep_wick:.2f}"
        ),
        bar_index=bar_i,
    )
