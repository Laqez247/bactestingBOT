# indicators/core.py
import pandas as pd
from typing import Dict

from state.models import VolatilityState, SessionName


def ema(series: pd.Series, period: int) -> pd.Series:
    """
    Standard EMA using pandas ewm.
    span=period, adjust=False.
    This is the ONLY EMA implementation in the system.
    """
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder's smoothing (alpha = 1/period).
    df must have columns: high, low, close
    This is the ONLY ATR implementation in the system.
    """
    high = df['high']
    low = df['low']
    prev = df['close'].shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev).abs(),
        (low - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def swing_low(df: pd.DataFrame, lookback: int) -> pd.Series:
    """Rolling minimum of 'low' over lookback bars."""
    return df['low'].rolling(lookback).min()


def swing_point(df: pd.DataFrame, lookback: int, direction: str) -> pd.Series:
    """
    direction='long':  rolling min of 'low'  over lookback
    direction='short': rolling max of 'high' over lookback
    """
    if direction == 'long':
        return df['low'].rolling(lookback).min()
    elif direction == 'short':
        return df['high'].rolling(lookback).max()
    else:
        raise ValueError(f"Invalid direction: {direction}")


def rolling_range(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Rolling (high_max - low_min) over period bars.
    Used by Edge 2 compression detection.
    """
    return df['high'].rolling(period).max() - df['low'].rolling(period).min()


def body_ratio(row: Dict) -> float:
    """
    Absolute body size / total candle range.
    Used by Edge 2 fakeout filter.
    """
    total_range = row['high'] - row['low']
    if total_range == 0:
        return 0.0
    return abs(row['close'] - row['open']) / total_range


def wick_ratio(row: Dict, direction: str) -> float:
    """
    Rejection wick / total candle range.
    direction='long'  or 'LONG':  upper wick (high - max(open, close))
    direction='short' or 'SHORT': lower wick (min(open, close) - low)
    Used by Edge 2 fakeout filter.

    E2 BUG FIX (2025-05): caller was passing uppercase 'LONG'/'SHORT' but
    the original function only matched lowercase. For LONG breakouts this
    silently computed the lower wick instead of the upper wick, causing
    fakeout-detection to miss upper-wick rejections entirely.
    Fix: normalise direction to lowercase before comparison.
    """
    total_range = row['high'] - row['low']
    if total_range == 0:
        return 0.0
    direction_lower = direction.lower()
    if direction_lower == 'long':
        wick = row['high'] - max(row['open'], row['close'])
    else:
        wick = min(row['open'], row['close']) - row['low']
    return wick / total_range


def classify_volatility(atr_current: float, atr_mean_20: float) -> VolatilityState:
    """
    LOW:    atr_current < 0.7 * atr_mean_20
    HIGH:   atr_current > 1.5 * atr_mean_20
    NORMAL: everything else
    """
    ratio = atr_current / atr_mean_20 if atr_mean_20 > 0 else 1.0
    if ratio < 0.7:
        return VolatilityState.LOW
    elif ratio > 1.5:
        return VolatilityState.HIGH
    else:
        return VolatilityState.NORMAL


def classify_session(hour_utc: int, minute: int = 0) -> SessionName:
    """
    Asian:       00:00 – 06:59 UTC
    London_Open: 07:00 – 08:59 UTC
    London_Main: 09:00 – 12:59 UTC
    NY_Main:     13:00 – 17:30 UTC
    Off:         17:31 – 23:59 UTC
    """
    if 0 <= hour_utc <= 6:
        return SessionName.ASIAN
    elif 7 <= hour_utc <= 8:
        return SessionName.LONDON_OPEN
    elif 9 <= hour_utc <= 12:
        return SessionName.LONDON_MAIN
    elif 13 <= hour_utc <= 16:
        return SessionName.NY_MAIN
    elif hour_utc == 17 and minute <= 30:
        return SessionName.NY_MAIN
    else:
        return SessionName.OFF
