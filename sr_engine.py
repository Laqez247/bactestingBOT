"""
sr_engine.py — Range / Compression Zone Detection and Quality Scoring.

KEY FIXES v2:
- Dense window scan (every 5 bars from min to max) instead of 5 discrete values
- RANGE_TOUCH_PROXIMITY now 0.35x ATR ($0.84 at avg ATR) vs old 0.15x ($0.36)
- RANGE_MIN_TOUCHES now 1 per side (by definition always met → quality decides)
- RANGE_MIN_QUALITY now 30 (was 50)
- Range detection runs on ALL bars including Asian session
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
import config


@dataclass
class RangeState:
    valid: bool = False
    range_high: float = 0.0
    range_low: float = 0.0
    range_start_bar: int = -1
    range_end_bar: int = -1
    quality_score: float = 0.0
    is_premium: bool = False
    touch_count_high: int = 0
    touch_count_low: int = 0
    formed_in_asian: bool = False
    height_atr: float = 0.0
    is_fallback: bool = False     # True when detected by secondary low-threshold scan


class RangeEngine:
    """
    Detects and scores horizontal compression ranges bar by bar.
    Call update(i, df, atr) for EVERY bar — including Asian session bars.
    Access state via self.state (a RangeState).
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))
        self.state = RangeState()

        # Cache for numpy arrays
        self._last_df = None
        self._highs = None
        self._lows = None
        self._closes = None
        self._opens = None
        self._atrs = None
        self._hours = None
        self._times = None

    def update(self, i: int, df: pd.DataFrame, atr: pd.Series) -> None:
        """
        At each bar i, check if current range is still valid or needs rebuild.
        Runs on ALL bars (Asian, London, NY) — session filtering is for entries only.
        """
        if self._last_df is not df:
            self._last_df = df
            self._highs = df["high"].to_numpy(dtype=np.float64)
            self._lows = df["low"].to_numpy(dtype=np.float64)
            self._closes = df["close"].to_numpy(dtype=np.float64)
            self._opens = df["open"].to_numpy(dtype=np.float64)
            self._atrs = atr.to_numpy(dtype=np.float64)
            self._hours = df.index.hour.to_numpy(dtype=np.int32)
            self._times = df.index

        atr_val = self._atrs[i]
        if pd.isna(atr_val):
            atr_val = 1.0

        if self.state.valid:
            if self._is_range_broken(i, atr_val):
                self.state = RangeState()
            else:
                self.state.range_end_bar = i
                return

        if i < self._p("RANGE_MIN_BARS", 8):
            return

        new_state = self._scan_for_range(i)
        if new_state is not None:
            self.state = new_state

    def _is_range_broken(self, i: int, atr_val: float) -> bool:
        """
        Range expires when:
        - A candle CLOSES more than 1.2x ATR beyond either boundary
        - More than RANGE_MAX_BARS have elapsed since start
        """
        max_bars = self._p("RANGE_MAX_BARS", 150)
        if i - self.state.range_start_bar > max_bars:
            return True
        close_i = self._closes[i]
        breakout_buffer = 1.2 * atr_val
        if close_i > self.state.range_high + breakout_buffer:
            return True
        if close_i < self.state.range_low - breakout_buffer:
            return True
        return False

    def _scan_for_range(self, current_bar: int,
                        min_quality_override: Optional[float] = None) -> Optional[RangeState]:
        """
        Dense window scan — every 5 bars from RANGE_MIN_BARS to RANGE_MAX_BARS.
        Finds the highest-quality range ending at or near current_bar.
        Pass min_quality_override to run the fallback low-threshold scan.
        """
        max_bars   = self._p("RANGE_MAX_BARS", 150)
        min_bars   = self._p("RANGE_MIN_BARS", 8)
        min_touches = self._p("RANGE_MIN_TOUCHES", 1)
        proximity  = self._p("RANGE_TOUCH_PROXIMITY", 0.35)
        min_h_atr  = self._p("RANGE_MIN_HEIGHT_ATR", 0.25)
        max_h_atr  = self._p("RANGE_MAX_HEIGHT_ATR", 5.0)
        min_quality = min_quality_override if min_quality_override is not None else self._p("RANGE_MIN_QUALITY", 30)

        atr_val = self._atrs[current_bar]
        if pd.isna(atr_val):
            atr_val = 1.0
        touch_prox = proximity * atr_val

        best_state: Optional[RangeState] = None
        best_score = -1.0

        # Dense scan: try every window_step bars from min_bars to max_bars
        window_step = 5
        windows = list(range(min_bars, max_bars + 1, window_step))
        if max_bars not in windows:
            windows.append(max_bars)

        for window in windows:
            start_bar = max(0, current_bar - window)
            actual_window = current_bar - start_bar
            if actual_window < min_bars:
                continue

            # NumPy slices (fast view)
            highs_slice = self._highs[start_bar: current_bar + 1]
            lows_slice  = self._lows[start_bar: current_bar + 1]
            closes_slice = self._closes[start_bar: current_bar + 1]
            opens_slice  = self._opens[start_bar: current_bar + 1]
            hours_slice  = self._hours[start_bar: current_bar + 1]

            rh = float(np.max(highs_slice))
            rl = float(np.min(lows_slice))
            height = rh - rl

            if atr_val <= 0:
                continue

            h_atr = height / atr_val
            if h_atr < min_h_atr or h_atr > max_h_atr:
                continue

            high_touches = int(np.sum(np.abs(highs_slice - rh) <= touch_prox))
            low_touches  = int(np.sum(np.abs(lows_slice - rl) <= touch_prox))

            if high_touches < min_touches or low_touches < min_touches:
                continue

            score, is_premium, asian_flag = self._score_range_np(
                rh, rl, h_atr, high_touches, low_touches, touch_prox,
                highs_slice, lows_slice, closes_slice, opens_slice, hours_slice
            )

            if score < min_quality:
                continue

            if score > best_score:
                best_score = score
                best_state = RangeState(
                    valid=True,
                    range_high=rh,
                    range_low=rl,
                    range_start_bar=start_bar,
                    range_end_bar=current_bar,
                    quality_score=score,
                    is_premium=is_premium,
                    touch_count_high=high_touches,
                    touch_count_low=low_touches,
                    formed_in_asian=asian_flag,
                    height_atr=round(h_atr, 3),
                )

        return best_state

    def _score_range_np(self, rh, rl, h_atr,
                        high_touches, low_touches, touch_prox,
                        highs_slice, lows_slice, closes_slice, opens_slice, hours_slice) -> tuple:
        """
        Score 0–100. Returns (score, is_premium, formed_in_asian).
        """
        score = 0.0

        # +30: both sides tested 2+ times
        if high_touches >= 2 and low_touches >= 2:
            score += 30

        # +20: height in sweet spot 0.75–2.5x ATR
        if 0.75 <= h_atr <= 2.5:
            score += 20
        elif 0.25 <= h_atr < 0.75 or 2.5 < h_atr <= 4.0:
            score += 10  # partial credit for near-sweet-spot ranges

        # +15: Asian / off-hours formation
        asian_flag = self._is_asian_or_offhours_np(hours_slice)
        if asian_flag:
            score += 15

        # +10: long compression (>= 15 bars)
        n = len(highs_slice)
        if n >= 15:
            score += 10

        # +10: contraction (later bars tighter than earlier)
        if self._has_contraction_np(closes_slice, opens_slice):
            score += 10

        # +15: clean touches (wicks don't pierce much through boundary)
        if self._has_clean_touches_np(highs_slice, lows_slice, rh, rl, touch_prox):
            score += 15

        is_premium = score >= self._p("RANGE_PREMIUM_THRESHOLD", 75)
        return float(score), is_premium, asian_flag

    def _is_asian_or_offhours_np(self, hours_slice: np.ndarray) -> bool:
        try:
            asian_open  = int(self._p("ASIAN_SESSION_OPEN",  "00:00").split(":")[0])
            asian_close = int(self._p("ASIAN_SESSION_CLOSE", "07:00").split(":")[0])
            off_hours   = int(self._p("OFF_HOURS_OPEN",      "17:00").split(":")[0])
        except Exception:
            return False

        count_in = np.sum((hours_slice >= asian_open) & (hours_slice < asian_close) | (hours_slice >= off_hours))
        return count_in > len(hours_slice) * 0.4  # 40% threshold (was 50%)

    def _has_contraction_np(self, closes_slice: np.ndarray, opens_slice: np.ndarray) -> bool:
        n = len(closes_slice)
        if n < 6:
            return False
        bodies = np.abs(closes_slice - opens_slice)
        half = n // 2
        first_half  = np.mean(bodies[:half])
        second_half = np.mean(bodies[half:])
        if first_half == 0:
            return False
        return second_half < first_half * 0.85  # 15% smaller (was 20%)

    def _has_clean_touches_np(self, highs_slice: np.ndarray, lows_slice: np.ndarray,
                              rh: float, rl: float, touch_prox: float) -> bool:
        near_high = np.abs(highs_slice - rh) <= touch_prox
        near_low  = np.abs(lows_slice - rl) <= touch_prox
        total_touches = np.sum(near_high) + np.sum(near_low)
        if total_touches == 0:
            return True
        clean_high = np.sum((highs_slice[near_high] - rh) < touch_prox * 0.6)
        clean_low  = np.sum((rl - lows_slice[near_low]) < touch_prox * 0.6)
        return (clean_high + clean_low) / total_touches >= 0.5
