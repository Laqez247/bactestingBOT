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

    def update(self, i: int, df: pd.DataFrame, atr: pd.Series) -> None:
        """
        At each bar i, check if current range is still valid or needs rebuild.
        Runs on ALL bars (Asian, London, NY) — session filtering is for entries only.
        """
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0

        if self.state.valid:
            if self._is_range_broken(i, df, atr_val):
                self.state = RangeState()
            else:
                self.state.range_end_bar = i
                return

        if i < self._p("RANGE_MIN_BARS", 8):
            return

        new_state = self._scan_for_range(i, df, atr)
        if new_state is not None:
            self.state = new_state

    def _is_range_broken(self, i: int, df: pd.DataFrame, atr_val: float) -> bool:
        """
        Range expires when:
        - A candle CLOSES more than 1.2x ATR beyond either boundary
        - More than RANGE_MAX_BARS have elapsed since start
        """
        max_bars = self._p("RANGE_MAX_BARS", 150)
        if i - self.state.range_start_bar > max_bars:
            return True
        close_i = df["close"].iloc[i]
        breakout_buffer = 1.2 * atr_val
        if close_i > self.state.range_high + breakout_buffer:
            return True
        if close_i < self.state.range_low - breakout_buffer:
            return True
        return False

    def _scan_for_range(self, current_bar: int, df: pd.DataFrame,
                        atr: pd.Series) -> Optional[RangeState]:
        """
        Dense window scan — every 5 bars from RANGE_MIN_BARS to RANGE_MAX_BARS.
        Finds the highest-quality range ending at or near current_bar.
        """
        max_bars   = self._p("RANGE_MAX_BARS", 150)
        min_bars   = self._p("RANGE_MIN_BARS", 8)
        min_touches = self._p("RANGE_MIN_TOUCHES", 1)
        proximity  = self._p("RANGE_TOUCH_PROXIMITY", 0.35)
        min_h_atr  = self._p("RANGE_MIN_HEIGHT_ATR", 0.25)
        max_h_atr  = self._p("RANGE_MAX_HEIGHT_ATR", 5.0)
        min_quality = self._p("RANGE_MIN_QUALITY", 30)

        atr_val = float(atr.iloc[current_bar]) if not pd.isna(atr.iloc[current_bar]) else 1.0
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

            sub = df.iloc[start_bar: current_bar + 1]
            rh = float(sub["high"].max())
            rl = float(sub["low"].min())
            height = rh - rl

            if atr_val <= 0:
                continue

            h_atr = height / atr_val
            if h_atr < min_h_atr or h_atr > max_h_atr:
                continue

            high_touches = self._count_touches(sub, rh, touch_prox, "high")
            low_touches  = self._count_touches(sub, rl, touch_prox, "low")

            if high_touches < min_touches or low_touches < min_touches:
                continue

            score, is_premium, asian_flag = self._score_range(
                sub, rh, rl, h_atr, high_touches, low_touches, touch_prox
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

    def _count_touches(self, window_df: pd.DataFrame, level: float,
                       proximity: float, side: str) -> int:
        """
        Count how many bars came within `proximity` of `level`.
        side='high' → use bar highs, side='low' → use bar lows.
        At least 1 is always true (the bar that IS the max/min).
        """
        col = "high" if side == "high" else "low"
        prices = window_df[col].values
        return int(np.sum(np.abs(prices - level) <= proximity))

    def _score_range(self, window_df, rh, rl, h_atr,
                     high_touches, low_touches, touch_prox) -> tuple:
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
        asian_flag = self._is_asian_or_offhours(window_df)
        if asian_flag:
            score += 15

        # +10: long compression (>= 15 bars)
        if len(window_df) >= 15:
            score += 10

        # +10: contraction (later bars tighter than earlier)
        if self._has_contraction(window_df):
            score += 10

        # +15: clean touches (wicks don't pierce much through boundary)
        if self._has_clean_touches(window_df, rh, rl, touch_prox):
            score += 15

        is_premium = score >= self._p("RANGE_PREMIUM_THRESHOLD", 75)
        return float(score), is_premium, asian_flag

    def _is_asian_or_offhours(self, window_df: pd.DataFrame) -> bool:
        try:
            asian_open  = int(self._p("ASIAN_SESSION_OPEN",  "00:00").split(":")[0])
            asian_close = int(self._p("ASIAN_SESSION_CLOSE", "07:00").split(":")[0])
            off_hours   = int(self._p("OFF_HOURS_OPEN",      "17:00").split(":")[0])
        except Exception:
            return False

        count_in = 0
        for ts in window_df.index:
            h = ts.hour
            if asian_open <= h < asian_close or h >= off_hours:
                count_in += 1
        return count_in > len(window_df) * 0.4  # 40% threshold (was 50%)

    def _has_contraction(self, window_df: pd.DataFrame) -> bool:
        n = len(window_df)
        if n < 6:
            return False
        bodies = (window_df["close"] - window_df["open"]).abs()
        first_half  = bodies.iloc[:n // 2].mean()
        second_half = bodies.iloc[n // 2:].mean()
        if first_half == 0:
            return False
        return second_half < first_half * 0.85  # 15% smaller (was 20%)

    def _has_clean_touches(self, window_df: pd.DataFrame, rh: float, rl: float,
                           touch_prox: float) -> bool:
        clean_count = 0
        total_touches = 0
        for _, row in window_df.iterrows():
            near_high = abs(row["high"] - rh) <= touch_prox
            near_low  = abs(row["low"]  - rl) <= touch_prox
            if near_high:
                total_touches += 1
                if (row["high"] - rh) < touch_prox * 0.6:
                    clean_count += 1
            if near_low:
                total_touches += 1
                if (rl - row["low"]) < touch_prox * 0.6:
                    clean_count += 1
        if total_touches == 0:
            return True  # no touch data = default pass
        return (clean_count / total_touches) >= 0.5  # was 0.6
