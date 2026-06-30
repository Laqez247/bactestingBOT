"""
cs2_round_number.py — CS2: Round Number Fade Strategy

Fades wick rejections at XAUUSD psychological $50/$100 levels.
Fires independently of Edge 2 breakout sequence.

Gate sequence:
  CS2-1: Candle wick touches within CS2_ROUND_BAND_ABS of a $50 level
  CS2-2: Wick rejection confirmed — close is CS2_MIN_CLOSE_DISTANCE from level
  CS2-3: Wick size >= CS2_MIN_WICK_ABS AND >= CS2_MIN_WICK_ATR × ATR
  CS2-4: Regime is NOT HIGH_VOLATILITY
  CS2-5: Session is LONDON or NY
  CS2-6: No trade open (checked by runner)
  CS2-7: Round number NOT in direction of confirmed MSS on exec TF
  CS2-8: HTF trend must not strongly oppose the fade

Adaptive modification:
  MOD-A: wick 80–100% of threshold → require $100 level AND strong HTF alignment
  (Gate CS2-8 failure on $100 level is NEVER overridden)

No fixed RR. Dynamic TP via tp_engine.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import config
from strategies.cs_signal import CSSignal


def get_nearest_round_number(price: float, increment: float = 50.0) -> float:
    return round(price / increment) * increment


def is_near_round_number(price: float, band_abs: float = 2.50, increment: float = 50.0):
    nearest = get_nearest_round_number(price, increment)
    distance = abs(price - nearest)
    return distance <= band_abs, nearest


def round_number_weight(level: float, major_increment: float = 100.0) -> float:
    """$100 levels return 1.5, $50 levels return 1.0."""
    return 1.5 if (level % major_increment == 0) else 1.0


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
    regime_info: dict,
    df_exec: pd.DataFrame,
    atr_exec: pd.Series,
    bar_i: int,
    cfg: dict = None,
    tp_engine=None,
) -> "CSSignal | None":
    """Check CS2 signal at bar_i."""
    _cfg = cfg or {}

    def cp(key, default=None):
        return _cfg.get(key, getattr(config, key, default))

    if not cp("CS2_ENABLED", True):
        return None

    # --- Gate CS2-4: Regime ---
    regime = (regime_info or {}).get("regime", "RANGING")
    if regime == "HIGH_VOLATILITY":
        return None

    # --- Gate CS2-5: Session ---
    # CS2_SESSION_FILTER overrides shared CS_SESSION_FILTER for CS2 specifically.
    # Iter 5 data: NY = 7 trades at 28.6% WR, -0.5780R — structurally weak.
    # London = 43T at 81.4% WR; off_hours = 19T at 73.7% WR — both viable.
    ts = df_exec.index[bar_i]
    session = _get_session(ts)
    cs2_sessions = cp("CS2_SESSION_FILTER", cp("CS_SESSION_FILTER", ["london", "new_york"]))
    if session not in cs2_sessions:
        return None

    atr_val = float(atr_exec.iloc[bar_i]) if not pd.isna(atr_exec.iloc[bar_i]) else 1.0

    high_i  = float(df_exec["high"].iloc[bar_i])
    low_i   = float(df_exec["low"].iloc[bar_i])
    close_i = float(df_exec["close"].iloc[bar_i])
    open_i  = float(df_exec["open"].iloc[bar_i])

    increment       = cp("CS2_INCREMENT", 50.0)
    major_increment = cp("CS2_MAJOR_INCREMENT", 100.0)
    band_abs        = cp("CS2_ROUND_BAND_ABS", 2.50)
    min_close_dist  = cp("CS2_MIN_CLOSE_DISTANCE", 1.00)
    min_wick_abs    = cp("CS2_MIN_WICK_ABS", 1.50)
    min_wick_atr    = cp("CS2_MIN_WICK_ATR", 0.20)

    # --- Gate CS2-1: Wick proximity to round level ---
    # Check SHORT: candle.high touches round number
    near_short, rn_short = is_near_round_number(high_i, band_abs, increment)
    # Check LONG: candle.low touches round number
    near_long,  rn_long  = is_near_round_number(low_i, band_abs, increment)

    if not near_short and not near_long:
        return None

    # Prefer the direction where the wick is more significant
    if near_short and near_long:
        # Both wicks near round numbers — pick the one with larger wick
        wick_short = high_i - max(open_i, close_i)
        wick_long  = min(open_i, close_i) - low_i
        direction  = "SHORT" if wick_short >= wick_long else "LONG"
        rn_level   = rn_short if direction == "SHORT" else rn_long
    elif near_short:
        direction  = "SHORT"
        rn_level   = rn_short
    else:
        direction  = "LONG"
        rn_level   = rn_long

    is_major_level = (rn_level % major_increment == 0)

    # --- Gate CS2-2: Wick rejection ---
    if direction == "SHORT":
        wick_size  = high_i - max(open_i, close_i)
        # Close must be at least min_close_dist BELOW the round level
        close_ok   = close_i <= rn_level - min_close_dist
    else:
        wick_size  = min(open_i, close_i) - low_i
        # Close must be at least min_close_dist ABOVE the round level
        close_ok   = close_i >= rn_level + min_close_dist

    if not close_ok:
        return None

    # --- Gate CS2-3: Wick size ---
    wick_ok      = wick_size >= min_wick_abs and wick_size >= min_wick_atr * atr_val
    mod_a_active = False

    if not wick_ok:
        # MOD-A: wick 80–100% of ATR threshold → require $100 level AND HTF alignment
        wick_threshold_atr = min_wick_atr * atr_val
        wick_lower_bound   = wick_threshold_atr * 0.80
        if wick_size >= wick_lower_bound and is_major_level:
            mod_a_active = True
            # HTF alignment check will happen at Gate CS2-8 — deferred below
        else:
            return None

    # --- Gate CS2-7: NOT in direction of confirmed MSS ---
    # If a MSS happened recently in the same direction as fade, let Edge 2 handle it
    mss_bullish = getattr(structure_engine, "mss_bullish", False)
    mss_bearish = getattr(structure_engine, "mss_bearish", False)
    if direction == "SHORT" and mss_bullish:
        return None  # don't fade a fresh bullish breakout
    if direction == "LONG" and mss_bearish:
        return None

    # --- Gate CS2-MSS: Require recent MSS/BOS in trade direction ---
    # Data: MSS_BEARISH 37T 81.1% WR +0.4878R, BOS_MACRO 9T 66.7% WR +0.2052R
    # vs no-structure 38T 44.7% WR -0.2219R → gate removes net-negative cohort entirely
    # mss_bearish_bar / bos_bearish_bar persist after flag consumption — use bar tracking.
    if cp("CS2_MSS_REQUIRED", True):
        mss_lookback = cp("CS2_MSS_LOOKBACK_BARS", 10)
        allow_bos    = cp("CS2_ALLOW_BOS_AS_MSS", True)

        if direction == "SHORT":
            mss_bar = getattr(structure_engine, "mss_bearish_bar", -1)
            bos_bar = getattr(structure_engine, "bos_bearish_bar", -1)
        else:
            mss_bar = getattr(structure_engine, "mss_bullish_bar", -1)
            bos_bar = getattr(structure_engine, "bos_bullish_bar", -1)

        mss_recent = mss_bar >= 0 and (bar_i - mss_bar) <= mss_lookback
        bos_recent = allow_bos and bos_bar >= 0 and (bar_i - bos_bar) <= mss_lookback

        if not mss_recent and not bos_recent:
            return None  # No recent structural confirmation — fade is unanchored

    # --- Gate CS2-8: HTF trend alignment ---
    htf_trend = "RANGING"
    if htf_aligned_series is not None and bar_i < len(htf_aligned_series):
        htf_trend = str(htf_aligned_series.iloc[bar_i])

    htf_opposes = False
    if direction == "SHORT" and htf_trend == "UPTREND":
        htf_opposes = True
    elif direction == "LONG" and htf_trend == "DOWNTREND":
        htf_opposes = True

    if htf_opposes:
        # $100 level is NEVER overridden on HTF conflict (spec non-negotiable)
        return None

    # If MOD-A is active, require strong HTF alignment (not RANGING)
    if mod_a_active:
        htf_aligned_direction = (
            (direction == "LONG" and htf_trend == "UPTREND") or
            (direction == "SHORT" and htf_trend == "DOWNTREND")
        )
        if not htf_aligned_direction:
            return None

    # --- Compute SL ---
    sl_buffer_abs = cp("CS2_SL_BUFFER_ABS", 3.00)
    if direction == "SHORT":
        entry_price = close_i
        sl_price    = rn_level + sl_buffer_abs
    else:
        entry_price = close_i
        sl_price    = rn_level - sl_buffer_abs

    # --- Dynamic TP ---
    tp_candidates = []
    if tp_engine is not None:
        tp_min_score = cp("CS2_TP_MIN_SCORE", 20)
        tp_min_rr    = cp("CS2_TP_MIN_RR", 1.0)

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
                "bot_range_boundary_in_direction",
                "nearest_swing_in_direction",
                "nearest_other_round_number",
                "session_high_low_in_direction",
                "pdh_pdl_in_direction",
            ],
            tp_min_score=tp_min_score,
            tp_min_rr=tp_min_rr,
        )

    if not tp_candidates:
        return None

    mod_type = "CS2_WEAK_WICK_MAJOR_LEVEL_HTF" if mod_a_active else "NONE"

    return CSSignal(
        strategy="CS2",
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_candidates=tp_candidates,
        modification_type=mod_type,
        setup_context=(
            f"CS2 {'SHORT' if direction == 'SHORT' else 'LONG'} at "
            f"{'$100' if is_major_level else '$50'} level {rn_level:.0f}, "
            f"wick={wick_size:.2f}, HTF={htf_trend}"
        ),
        bar_index=bar_i,
    )
