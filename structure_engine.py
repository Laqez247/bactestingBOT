"""
structure_engine.py — Pivot detection, market structure labeling, MSS/BOS flags.
STRICT NO-LOOKAHEAD: a swing high at bar T is only confirmed at bar T+PIVOT_RIGHT.
All decisions at bar T use only candles 0..T.
"""

import numpy as np
import pandas as pd
from typing import Optional
import config


def compute_atr(df: pd.DataFrame, period: int = None) -> pd.Series:
    """
    Compute ATR(period) using True Range. Returns Series aligned to df.index.
    Uses only past data — no future reference.
    """
    if period is None:
        period = config.ATR_PERIOD
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    return atr


class StructureEngine:
    """
    Processes bars sequentially (bar by bar) and maintains:
      - confirmed swing highs (SH) and swing lows (SL) lists
      - market structure state: UPTREND / DOWNTREND / RANGING
      - MSS and BOS flags (reset after consumption)

    Call update(i, df, atr) for each bar i in order.
    """

    def __init__(self, pivot_left=None, pivot_right=None, mss_prior_lh_count=None):
        self.pivot_left  = pivot_left  if pivot_left  is not None else config.PIVOT_LEFT
        self.pivot_right = pivot_right if pivot_right is not None else config.PIVOT_RIGHT
        self.mss_prior_lh_count = (
            mss_prior_lh_count if mss_prior_lh_count is not None
            else config.MSS_PRIOR_LH_COUNT
        )

        # Confirmed pivots: list of (bar_index, price)
        self.swing_highs = []   # most recent last
        self.swing_lows  = []   # most recent last

        # Market structure state
        self.structure = "RANGING"  # UPTREND | DOWNTREND | RANGING

        # MSS / BOS flags — set True when event occurs, consumed by backtest loop
        self.mss_bullish = False
        self.bos_bullish = False
        self.mss_bearish = False
        self.bos_bearish = False

        # The specific bar on which each flag was set
        self.mss_bullish_bar = -1
        self.bos_bullish_bar = -1
        self.mss_bearish_bar = -1
        self.bos_bearish_bar = -1

        # Last confirmed pivot bars (to avoid re-processing)
        self._last_sh_checked = -1
        self._last_sl_checked = -1

    def reset_flags(self):
        """Call after a setup is consumed (opened or rejected)."""
        self.mss_bullish = False
        self.bos_bullish = False
        self.mss_bearish = False
        self.bos_bearish = False

    def update(self, i: int, df: pd.DataFrame, atr: pd.Series) -> None:
        """
        Process bar i. Updates internal state.
        A pivot at bar T is only confirmed when bar T+PIVOT_RIGHT has closed,
        i.e., when i >= T + PIVOT_RIGHT.
        """
        # The candidate pivot bar is i - PIVOT_RIGHT
        candidate = i - self.pivot_right
        if candidate < self.pivot_left:
            return

        self._check_swing_high(candidate, i, df)
        self._check_swing_low(candidate, i, df)
        self._update_structure()
        self._check_bos_mss(i, df)

    def _check_swing_high(self, cand: int, current: int, df: pd.DataFrame) -> None:
        """
        Confirm whether bar `cand` is a valid swing high.
        Requires all bars in [cand-pivot_left .. cand-1] and [cand+1 .. current]
        to have lower highs than cand.
        """
        if cand <= self._last_sh_checked:
            return
        self._last_sh_checked = cand

        cand_high = df["high"].iloc[cand]
        left_start = max(0, cand - self.pivot_left)

        # Left side: all bars < cand_high
        left_ok = all(df["high"].iloc[j] < cand_high for j in range(left_start, cand))
        if not left_ok:
            return

        # Right side: bars cand+1 .. cand+pivot_right (all within df)
        right_end = min(len(df), cand + self.pivot_right + 1)
        right_ok = all(df["high"].iloc[j] < cand_high for j in range(cand + 1, right_end))
        if not right_ok:
            return

        # Confirmed swing high
        self.swing_highs.append((cand, cand_high))

    def _check_swing_low(self, cand: int, current: int, df: pd.DataFrame) -> None:
        """
        Confirm whether bar `cand` is a valid swing low.
        """
        if cand <= self._last_sl_checked:
            return
        self._last_sl_checked = cand

        cand_low = df["low"].iloc[cand]
        left_start = max(0, cand - self.pivot_left)

        left_ok = all(df["low"].iloc[j] > cand_low for j in range(left_start, cand))
        if not left_ok:
            return

        right_end = min(len(df), cand + self.pivot_right + 1)
        right_ok = all(df["low"].iloc[j] > cand_low for j in range(cand + 1, right_end))
        if not right_ok:
            return

        self.swing_lows.append((cand, cand_low))

    def _update_structure(self) -> None:
        """
        Classify market structure using the last 3 confirmed SH and SL.
        UPTREND:   SH[0] > SH[1] > SH[2] AND SL[0] > SL[1] > SL[2] (HH + HL)
        DOWNTREND: SH[0] < SH[1] < SH[2] AND SL[0] < SL[1] < SL[2] (LH + LL)
        RANGING:   neither
        """
        sh = self.swing_highs[-3:] if len(self.swing_highs) >= 3 else None
        sl = self.swing_lows[-3:]  if len(self.swing_lows)  >= 3 else None

        if sh and sl:
            sh_prices = [x[1] for x in sh]  # oldest to newest
            sl_prices = [x[1] for x in sl]

            hh = sh_prices[2] > sh_prices[1] > sh_prices[0]
            hl = sl_prices[2] > sl_prices[1] > sl_prices[0]
            lh = sh_prices[2] < sh_prices[1] < sh_prices[0]
            ll = sl_prices[2] < sl_prices[1] < sl_prices[0]

            if hh and hl:
                self.structure = "UPTREND"
            elif lh and ll:
                self.structure = "DOWNTREND"
            else:
                self.structure = "RANGING"
        else:
            self.structure = "RANGING"

    def _check_bos_mss(self, i: int, df: pd.DataFrame) -> None:
        """
        After updating pivots, check for BOS or MSS events at bar i.
        MSS_BULLISH: short-term structure was bearish (LH sequence), candle at i
                     closes above the most recent confirmed swing high (SH[0]).
        BOS_BULLISH: candle closes above any confirmed swing high (weaker signal).
        Mirror for bearish.
        """
        if not self.swing_highs or not self.swing_lows:
            return

        close_i = df["close"].iloc[i]
        high_i  = df["high"].iloc[i]
        low_i   = df["low"].iloc[i]

        # --- BULLISH checks ---
        sh_recent = self.swing_highs[-1]  # (bar_idx, price)
        sh0_price = sh_recent[1]

        if close_i > sh0_price:
            # Check MSS: requires prior LH sequence
            is_mss = self._has_lower_high_sequence(count=self.mss_prior_lh_count)
            if is_mss and not self.mss_bullish:
                self.mss_bullish     = True
                self.mss_bullish_bar = i
            elif not self.bos_bullish:
                self.bos_bullish     = True
                self.bos_bullish_bar = i

        # --- BEARISH checks ---
        sl_recent = self.swing_lows[-1]
        sl0_price = sl_recent[1]

        if close_i < sl0_price:
            is_mss = self._has_higher_low_sequence(count=self.mss_prior_lh_count)
            if is_mss and not self.mss_bearish:
                self.mss_bearish     = True
                self.mss_bearish_bar = i
            elif not self.bos_bearish:
                self.bos_bearish     = True
                self.bos_bearish_bar = i

    def _has_lower_high_sequence(self, count: int) -> bool:
        """
        Returns True if the last `count` swing highs form a lower-high sequence
        (each newer SH is lower than the one before it).
        This confirms the short-term structure was bearish before the potential MSS.
        """
        if len(self.swing_highs) < count + 1:
            return False
        relevant = self.swing_highs[-(count + 1):]  # oldest to newest
        prices = [x[1] for x in relevant]
        # lower highs: each newer price < prior
        return all(prices[j+1] < prices[j] for j in range(len(prices)-1))

    def _has_higher_low_sequence(self, count: int) -> bool:
        """
        Returns True if the last `count` swing lows form a higher-low sequence.
        Confirms short-term structure was bullish before potential bearish MSS.
        """
        if len(self.swing_lows) < count + 1:
            return False
        relevant = self.swing_lows[-(count + 1):]
        prices = [x[1] for x in relevant]
        return all(prices[j+1] > prices[j] for j in range(len(prices)-1))

    def get_recent_swing_highs(self, n: int = 3):
        """Return last n confirmed swing highs as list of (bar, price), newest last."""
        return self.swing_highs[-n:] if self.swing_highs else []

    def get_recent_swing_lows(self, n: int = 3):
        """Return last n confirmed swing lows as list of (bar, price), newest last."""
        return self.swing_lows[-n:] if self.swing_lows else []

    def latest_swing_high(self):
        """Return (bar, price) of most recent confirmed swing high, or None."""
        return self.swing_highs[-1] if self.swing_highs else None

    def latest_swing_low(self):
        """Return (bar, price) of most recent confirmed swing low, or None."""
        return self.swing_lows[-1] if self.swing_lows else None


def run_htf_structure(df_htf: pd.DataFrame) -> pd.Series:
    """
    Run structure classification on the HTF (1H) DataFrame.
    Returns a Series indexed by datetime with values UPTREND/DOWNTREND/RANGING.
    Used by Gate 1 of the setup validation.
    """
    eng = StructureEngine()
    atr = compute_atr(df_htf)
    states = []

    for i in range(len(df_htf)):
        eng.update(i, df_htf, atr)
        states.append(eng.structure)

    return pd.Series(states, index=df_htf.index, name="htf_structure")


def align_htf_to_exec(htf_series: pd.Series, exec_index: pd.DatetimeIndex) -> pd.Series:
    """
    Forward-fill HTF structure state onto execution TF index.
    At each exec bar, we see the most recent completed HTF bar's state.
    This is the correct no-lookahead approach.
    """
    aligned = htf_series.reindex(exec_index, method="ffill")
    return aligned
