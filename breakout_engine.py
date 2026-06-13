"""
breakout_engine.py — Qualifies structure break events after liquidity sweeps.

Key constraint enforced here (and again in Gate 3):
  SSL sweep → BULLISH breakout ONLY
  BSL sweep → BEARISH breakout ONLY

Quality classification:
  STRONG       — MSS + body > 1.0x ATR
  MODERATE     — MSS + body 0.40–1.0x ATR
  MODERATE_BOS — BOS only (any direction), body >= threshold
"""

import pandas as pd
from dataclasses import dataclass
from typing import Optional
import config


@dataclass
class BreakoutEvent:
    direction: str          # "LONG" or "SHORT"
    structure_type: str     # "MSS_BULLISH" | "MSS_BEARISH" | "BOS_BULLISH" | "BOS_BEARISH"
    sweep_type: str         # The SSL_* or BSL_* that preceded this
    breakout_bar: int
    breakout_price: float   # close price of breakout candle
    breakout_body_atr: float
    quality: str            # "STRONG" | "MODERATE" | "MODERATE_BOS"
    voided: bool = False    # True if false-breakout filter invalidates it


class BreakoutEngine:
    """
    Monitors sweep events + structure flags and emits BreakoutEvent objects.

    Call update(i, df, atr, liquidity_engine, structure_engine) each bar.
    Access self.current_breakout for the latest valid event.
    Call consume() after a setup is processed (opened or rejected at zone stage).
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))

        self.current_breakout: Optional[BreakoutEvent] = None
        self._prev_close: Optional[float] = None
        self._pending_breakout: Optional[BreakoutEvent] = None  # waiting for false-break check

    def consume(self) -> None:
        """Mark current breakout as consumed — prevents double-processing."""
        self.current_breakout = None
        self._pending_breakout = None

    def update(self, i: int, df: pd.DataFrame, atr: pd.Series,
               liquidity_engine, structure_engine) -> None:
        """
        Process bar i:
        1. If there is a pending breakout from bar i-1, apply false-break filter
        2. Check for new breakout events at bar i
        """
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        close_i = float(df["close"].iloc[i])

        # Step 1: Expire stale breakouts (max age = RETEST_TIMEOUT_BARS + 20)
        max_bo_age = self._p("RETEST_TIMEOUT_BARS", 50) + 20
        if self.current_breakout is not None:
            age = i - self.current_breakout.breakout_bar
            if age > max_bo_age:
                self.current_breakout = None   # expired — allow fresh detection

        # Step 2: Validate any pending breakout (false-break filter)
        if self._pending_breakout is not None:
            if self._check_false_break(i, df, self._pending_breakout):
                self._pending_breakout.voided = True
                self._pending_breakout = None
            else:
                # Breakout confirmed — promote only if slot is free (prevents crowding)
                if self.current_breakout is None:
                    self.current_breakout = self._pending_breakout
                self._pending_breakout = None

        # Step 3: Check for new breakout events
        # Guard: only detect when slot is free (caller must call consume() to free it)
        min_body_atr = self._p("BREAKOUT_MIN_BODY_ATR", 0.30)
        body_size    = abs(float(df["close"].iloc[i]) - float(df["open"].iloc[i]))
        body_atr     = body_size / atr_val if atr_val > 0 else 0

        if body_atr < min_body_atr:
            self._prev_close = close_i
            return

        # --- BULLISH breakout: requires prior SSL sweep + free slot ---
        if liquidity_engine.ssl_swept and self.current_breakout is None:
            mss = structure_engine.mss_bullish
            bos = structure_engine.bos_bullish
            if mss or bos:
                struct_type = "MSS_BULLISH" if mss else "BOS_BULLISH"
                quality     = self._classify_quality(body_atr, is_mss=mss)
                ev = BreakoutEvent(
                    direction="LONG",
                    structure_type=struct_type,
                    sweep_type=liquidity_engine.ssl_sweep_type,
                    breakout_bar=i,
                    breakout_price=close_i,
                    breakout_body_atr=body_atr,
                    quality=quality,
                )
                self._pending_breakout = ev
                structure_engine.mss_bullish = False
                structure_engine.bos_bullish = False

        # --- BEARISH breakout: requires prior BSL sweep + free slot ---
        if liquidity_engine.bsl_swept and self.current_breakout is None:
            mss = structure_engine.mss_bearish
            bos = structure_engine.bos_bearish
            if mss or bos:
                struct_type = "MSS_BEARISH" if mss else "BOS_BEARISH"
                quality     = self._classify_quality(body_atr, is_mss=mss)
                ev = BreakoutEvent(
                    direction="SHORT",
                    structure_type=struct_type,
                    sweep_type=liquidity_engine.bsl_sweep_type,
                    breakout_bar=i,
                    breakout_price=close_i,
                    breakout_body_atr=body_atr,
                    quality=quality,
                )
                self._pending_breakout = ev
                structure_engine.mss_bearish = False
                structure_engine.bos_bearish = False

        self._prev_close = close_i

    def _check_false_break(self, current_bar: int, df: pd.DataFrame,
                           breakout: BreakoutEvent) -> bool:
        """
        False breakout filter (loosened in v2):
        Void the breakout ONLY if price closes more than 0.5x ATR BACK through
        the breakout price within FALSE_BREAKOUT_BARS bars.

        RATIONALE: Fakeouts that quickly reverse by only 1-2 ticks are common
        noise on 5m XAUUSD and can still be monetized via the zone retest.
        We only void the breakout if price strongly reverses (≥ 0.5x ATR).
        FALSE_BREAKOUT_BARS = 3 gives 15 min breathing room.
        """
        false_bar_count = self._p("FALSE_BREAKOUT_BARS", 3)
        bars_since = current_bar - breakout.breakout_bar
        if bars_since > false_bar_count:
            return False  # beyond the check window — not a false break

        # Compute ATR at the breakout bar (approximate from close prices)
        atr_val = self._atr_at_bar(current_bar, df)
        reversal_threshold = 0.5 * atr_val  # must reverse by 0.5x ATR to void

        close_curr = float(df["close"].iloc[current_bar])

        if breakout.direction == "LONG":
            # Void only if close is more than 0.5x ATR BELOW breakout price
            return close_curr < breakout.breakout_price - reversal_threshold
        else:  # SHORT
            # Void only if close is more than 0.5x ATR ABOVE breakout price
            return close_curr > breakout.breakout_price + reversal_threshold

    def _atr_at_bar(self, i: int, df: pd.DataFrame, period: int = 5) -> float:
        """Fast approximate ATR using last `period` bars."""
        start = max(0, i - period)
        sub = df.iloc[start:i + 1]
        if len(sub) < 2:
            return 1.0
        trs = (sub["high"] - sub["low"]).values
        return float(trs.mean()) if len(trs) > 0 else 1.0

    def _classify_quality(self, body_atr: float, is_mss: bool) -> str:
        """
        STRONG:       MSS + body > 1.0x ATR
        MODERATE:     MSS + body 0.40–1.0x ATR
        MODERATE_BOS: BOS only (body >= threshold)
        """
        if is_mss:
            return "STRONG" if body_atr > 1.0 else "MODERATE"
        else:
            return "MODERATE_BOS"
