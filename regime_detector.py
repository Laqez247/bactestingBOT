"""
regime_detector.py — HTF Market Regime Classification for Hybrid Engine.

Runs once per bar (or per setup attempt) using only data available at bar T.
Outputs a REGIME classification that gates which modifications are available
in the setup modification layer (trade_simulator.py).

Called from run_backtest.py before validate_setup / try_open_trade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Dict, Optional

# Regime constants
TRENDING_STRONG   = "TRENDING_STRONG"
TRENDING_MODERATE = "TRENDING_MODERATE"
RANGING           = "RANGING"
HIGH_VOLATILITY   = "HIGH_VOLATILITY"
LOW_STRUCTURE     = "LOW_STRUCTURE"

# ATR ratio thresholds
_HV_ATR_RATIO     = 2.5   # > this → HIGH_VOLATILITY
_LS_ATR_RATIO     = 0.70  # < this → LOW_STRUCTURE (Gate 0 handles, return LS)
_HV_CANDLE_MULT   = 1.5   # HTF candle closing > this x ATR against trend → disqualify strong


def _find_pivots(df_slice: pd.DataFrame, left: int = 2, right: int = 2):
    """
    Detect swing highs and lows in df_slice.
    A pivot high at bar T is confirmed when bar T+right has closed, i.e.,
    we look at bars [left .. len-right-1] in the slice.
    Returns (swing_highs, swing_lows) as lists of (index_pos, price).
    Uses only data available at the final bar of the slice (no lookahead).
    """
    if len(df_slice) < left + right + 1:
        return [], []

    highs = df_slice["high"].to_numpy(dtype=np.float64)
    lows  = df_slice["low"].to_numpy(dtype=np.float64)
    n     = len(df_slice)

    swing_highs = []
    swing_lows  = []

    for i in range(left, n - right):
        h = highs[i]
        if (np.all(highs[i - left:i] < h) and np.all(highs[i + 1:i + right + 1] < h)):
            swing_highs.append((i, h))

        l = lows[i]
        if (np.all(lows[i - left:i] > l) and np.all(lows[i + 1:i + right + 1] > l)):
            swing_lows.append((i, l))

    return swing_highs, swing_lows


def _count_trending_swings(swing_highs, swing_lows):
    """
    Count qualifying HH+HL sequences (bullish) or LH+LL sequences (bearish).

    Returns:
        (bullish_swing_count, bearish_swing_count, direction)
        direction: "BULLISH" | "BEARISH" | "NONE"
    """
    # Need at least 2 highs and 2 lows to form a sequence
    if len(swing_highs) < 2 and len(swing_lows) < 2:
        return 0, 0, "NONE"

    # Sort by position
    sh = sorted(swing_highs, key=lambda x: x[0])
    sl = sorted(swing_lows,  key=lambda x: x[0])

    # Count consecutive HH (higher highs)
    hh_count = 0
    for i in range(1, len(sh)):
        if sh[i][1] > sh[i - 1][1]:
            hh_count += 1

    # Count consecutive HL (higher lows)
    hl_count = 0
    for i in range(1, len(sl)):
        if sl[i][1] > sl[i - 1][1]:
            hl_count += 1

    # Count consecutive LH (lower highs)
    lh_count = 0
    for i in range(1, len(sh)):
        if sh[i][1] < sh[i - 1][1]:
            lh_count += 1

    # Count consecutive LL (lower lows)
    ll_count = 0
    for i in range(1, len(sl)):
        if sl[i][1] < sl[i - 1][1]:
            ll_count += 1

    bullish = min(hh_count, hl_count) * 2  # each HH+HL pair = 2 qualifying swings
    bearish = min(lh_count, ll_count) * 2

    if bullish > bearish:
        return bullish, bearish, "BULLISH"
    elif bearish > bullish:
        return bullish, bearish, "BEARISH"
    else:
        return bullish, bearish, "NONE"


def _check_counter_trend_candles(df_slice: pd.DataFrame, direction: str, atr_val: float,
                                  lookback: int = 10, max_mult: float = 1.5) -> bool:
    """
    Returns True if no HTF candle in the last `lookback` bars has closed
    more than max_mult * ATR against the trend direction.

    Used to disqualify TRENDING_STRONG when there is violent counter-trend
    candle activity.
    """
    if atr_val <= 0 or len(df_slice) < lookback:
        return True  # insufficient data — don't disqualify

    recent = df_slice.iloc[-lookback:]
    closes = recent["close"].to_numpy(dtype=np.float64)
    opens  = recent["open"].to_numpy(dtype=np.float64)

    for c, o in zip(closes, opens):
        if direction == "BULLISH":
            # Counter-trend: close < open (bearish candle) and body > max_mult * ATR
            if (c < o) and (o - c) > max_mult * atr_val:
                return False
        else:
            # Counter-trend: close > open (bullish candle) and body > max_mult * ATR
            if (c > o) and (c - o) > max_mult * atr_val:
                return False

    return True


def detect_regime(
    df_htf: pd.DataFrame,
    current_htf_idx: int,
    atr_htf: Optional[pd.Series] = None,
    lookback_bars: int = 60,
    atr_period: int = 14,
    atr_avg_period: int = 30,
) -> Dict:
    """
    Classify the current market regime using HTF (1H) data up to current_htf_idx.

    Parameters
    ----------
    df_htf          : Full 1H DataFrame (open, high, low, close, index=DatetimeIndex)
    current_htf_idx : The current bar index in df_htf (inclusive, no lookahead)
    atr_htf         : Pre-computed ATR series for df_htf (optional; computed if None)
    lookback_bars   : How many HTF bars to use for swing detection
    atr_period      : ATR period for HTF ATR computation
    atr_avg_period  : Rolling window for ATR mean (used to compute atr_ratio)

    Returns
    -------
    dict with keys:
        regime            : str  TRENDING_STRONG | TRENDING_MODERATE | RANGING | HIGH_VOLATILITY | LOW_STRUCTURE
        htf_swing_count   : int  qualifying swing count used for classification
        atr_ratio         : float current ATR / 30-bar ATR mean
        regime_confidence : float 0.0–1.0
        trend_direction   : str  BULLISH | BEARISH | NONE
    """
    # Clip to available bars
    end_idx = min(current_htf_idx + 1, len(df_htf))
    start_idx = max(0, end_idx - lookback_bars)
    df_slice = df_htf.iloc[start_idx:end_idx]

    if len(df_slice) < 5:
        return {
            "regime":            RANGING,
            "htf_swing_count":   0,
            "atr_ratio":         1.0,
            "regime_confidence": 0.0,
            "trend_direction":   "NONE",
        }

    # Compute ATR for the slice
    if atr_htf is not None:
        atr_series = atr_htf.iloc[start_idx:end_idx]
        current_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 1.0
        atr_mean_window = atr_series.iloc[-atr_avg_period:].dropna()
        atr_mean = float(atr_mean_window.mean()) if len(atr_mean_window) > 0 else current_atr
    else:
        # Compute ATR inline (Wilder's EMA method)
        high  = df_slice["high"].to_numpy(dtype=np.float64)
        low   = df_slice["low"].to_numpy(dtype=np.float64)
        close = df_slice["close"].to_numpy(dtype=np.float64)
        n     = len(close)
        tr_arr = np.empty(n)
        tr_arr[0] = high[0] - low[0]
        for k in range(1, n):
            tr_arr[k] = max(high[k] - low[k],
                            abs(high[k] - close[k - 1]),
                            abs(low[k]  - close[k - 1]))
        alpha = 1.0 / atr_period
        atr_arr = np.empty(n)
        atr_arr[0] = tr_arr[0]
        for k in range(1, n):
            atr_arr[k] = alpha * tr_arr[k] + (1.0 - alpha) * atr_arr[k - 1]

        current_atr = float(atr_arr[-1])
        mean_window = atr_arr[-atr_avg_period:] if n >= atr_avg_period else atr_arr
        atr_mean    = float(mean_window.mean()) if len(mean_window) > 0 else current_atr

    atr_ratio = current_atr / atr_mean if atr_mean > 0 else 1.0

    # Gate 0: LOW_STRUCTURE — ATR too low (below absolute 0.70)
    if current_atr < 0.70:
        return {
            "regime":            LOW_STRUCTURE,
            "htf_swing_count":   0,
            "atr_ratio":         atr_ratio,
            "regime_confidence": 1.0,
            "trend_direction":   "NONE",
        }

    # HIGH_VOLATILITY: ATR > 2.5x 30-bar mean
    if atr_ratio > _HV_ATR_RATIO:
        return {
            "regime":            HIGH_VOLATILITY,
            "htf_swing_count":   0,
            "atr_ratio":         atr_ratio,
            "regime_confidence": 1.0,
            "trend_direction":   "NONE",
        }

    # Detect swings
    swing_highs, swing_lows = _find_pivots(df_slice, left=2, right=2)
    bullish_swings, bearish_swings, direction = _count_trending_swings(swing_highs, swing_lows)
    htf_swing_count = max(bullish_swings, bearish_swings)

    # TRENDING_STRONG: 5+ qualifying swings + no violent counter-trend candles
    if htf_swing_count >= 5:
        no_counter_trend = _check_counter_trend_candles(
            df_slice, direction, current_atr, lookback=10, max_mult=_HV_CANDLE_MULT
        )
        if no_counter_trend and 1.0 <= atr_ratio <= 2.5:
            confidence = min(1.0, htf_swing_count / 10.0)
            return {
                "regime":            TRENDING_STRONG,
                "htf_swing_count":   htf_swing_count,
                "atr_ratio":         atr_ratio,
                "regime_confidence": confidence,
                "trend_direction":   direction,
            }

    # TRENDING_MODERATE: 3+ qualifying swings, ATR normal
    if htf_swing_count >= 3 and 0.7 <= atr_ratio <= 2.5:
        confidence = min(0.8, htf_swing_count / 8.0)
        return {
            "regime":            TRENDING_MODERATE,
            "htf_swing_count":   htf_swing_count,
            "atr_ratio":         atr_ratio,
            "regime_confidence": confidence,
            "trend_direction":   direction,
        }

    # RANGING: fewer than 3 clear trending swings
    confidence = max(0.1, 1.0 - htf_swing_count / 5.0)
    return {
        "regime":            RANGING,
        "htf_swing_count":   htf_swing_count,
        "atr_ratio":         atr_ratio,
        "regime_confidence": confidence,
        "trend_direction":   "NONE",
    }


def regime_allows_modifications(regime: str) -> list:
    """
    Return the list of modification names permitted under the given regime.
    HIGH_VOLATILITY → no modifications at all.
    RANGING          → only MODIFICATION_3 (stale retest with confluence).
    TRENDING_MODERATE → mods 1, 3, 5.
    TRENDING_STRONG   → all mods 1–5.
    """
    if regime == HIGH_VOLATILITY:
        return []
    if regime == RANGING:
        return ["MODIFICATION_3"]
    if regime == TRENDING_MODERATE:
        # Iter 1: added MODIFICATION_4; Iter 3 MOD2 added but reverted (degraded performance)
        return ["MODIFICATION_1", "MODIFICATION_3", "MODIFICATION_4", "MODIFICATION_5"]
    if regime == TRENDING_STRONG:
        return ["MODIFICATION_1", "MODIFICATION_2", "MODIFICATION_3",
                "MODIFICATION_4", "MODIFICATION_5"]
    # LOW_STRUCTURE: Gate 0 handles; no mods
    return []
