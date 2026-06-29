"""
zone_engine.py — Retest Zone Construction (OB, FVG, BB, Broken S/R).

Priority order: BB > OB > FVG > Broken SR
A zone is invalidated if a candle body closes through it before the retest.
All lookups use only bars up to current bar (no lookahead).
"""

import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional
import config


@dataclass
class RetestZone:
    zone_type: str      # "OB" | "FVG" | "BB" | "SR"
    direction: str      # "LONG" or "SHORT"
    top: float          # upper boundary
    bottom: float       # lower boundary
    formed_bar: int
    invalidated: bool = False
    priority: int = 99  # lower = higher priority (BB=1, OB=2, FVG=3, SR=4)


class ZoneEngine:
    """
    Builds and manages retest zones after a breakout event.

    Call build_zones(breakout, i, df, atr, range_state) after a valid breakout.
    Call update_zones(i, df) each bar to check for zone invalidation.
    The primary_zone property returns the highest-priority non-invalidated zone.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))
        self.zones: List[RetestZone] = []

    def clear(self) -> None:
        self.zones = []

    def build_zones(self, breakout, i: int, df: pd.DataFrame,
                    atr: pd.Series, range_state) -> None:
        """
        Build all candidate retest zones after a confirmed breakout.
        Called once per breakout event, using data up to bar i.
        """
        self.zones = []
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        direction = breakout.direction

        # 1. Order Block (OB) — highest structural priority
        ob = self._find_ob(breakout, i, df, atr_val)
        if ob:
            self.zones.append(ob)

        # 2. Fair Value Gap (FVG) — formed during displacement
        fvg = self._find_fvg(breakout, i, df, atr_val)
        if fvg:
            self.zones.append(fvg)

        # 3. Breaker Block (BB) — prior failed OB, now acting from other side
        # Audit: BB zones show 57.1% WR vs OB 70.2% — deprioritized below OB/FVG
        if not self._p("DISABLE_BB_ZONE", False):
            bb = self._find_bb(breakout, i, df, atr_val)
            if bb:
                self.zones.append(bb)

        # 4. Broken S/R (range boundary now acting as support/resistance)
        # Disabled when DISABLE_SR_ZONE=True (historically low win rate ~33%)
        if not self._p("DISABLE_SR_ZONE", False):
            sr = self._find_sr(breakout, range_state, atr_val)
            if sr:
                self.zones.append(sr)

        # Sort by priority, then apply minimum width
        self.zones = [self._enforce_min_width(z, atr_val) for z in self.zones]
        self.zones.sort(key=lambda z: z.priority)

    def update_zones(self, i: int, df: pd.DataFrame) -> None:
        """
        Invalidate zones where price body has closed through them.
        Called each bar after zone construction.
        """
        close_i = float(df["close"].iloc[i])
        open_i  = float(df["open"].iloc[i])
        body_top    = max(close_i, open_i)
        body_bottom = min(close_i, open_i)

        for zone in self.zones:
            if zone.invalidated:
                continue
            # A zone is invalidated when a candle body closes THROUGH it
            if zone.direction == "LONG":
                # Bearish close through the zone (body top < zone bottom)
                if body_top < zone.bottom:
                    zone.invalidated = True
            else:  # SHORT
                # Bullish close through the zone (body bottom > zone top)
                if body_bottom > zone.top:
                    zone.invalidated = True

    @property
    def primary_zone(self) -> Optional[RetestZone]:
        """Return highest-priority non-invalidated zone."""
        active = [z for z in self.zones if not z.invalidated]
        return active[0] if active else None

    @property
    def has_valid_zone(self) -> bool:
        return self.primary_zone is not None

    def _find_ob(self, breakout, i: int, df: pd.DataFrame,
                 atr_val: float) -> Optional[RetestZone]:
        """
        Bullish OB: last BEARISH candle before the strong bullish displacement.
        Bearish OB: last BULLISH candle before the strong bearish displacement.
        Displacement must be >= OB_DISPLACEMENT_ATR * ATR.
        OB zone = [min(open,close), max(open,close)] of the OB candle body.
        """
        ob_disp_atr = self._p("OB_DISPLACEMENT_ATR", 1.5)
        direction   = breakout.direction
        bo_bar      = breakout.breakout_bar

        # Scan backward from breakout bar
        scan_start = max(0, bo_bar - 20)  # look back up to 20 bars

        for j in range(bo_bar - 1, scan_start - 1, -1):
            open_j  = float(df["open"].iloc[j])
            close_j = float(df["close"].iloc[j])
            high_j  = float(df["high"].iloc[j])
            low_j   = float(df["low"].iloc[j])

            if direction == "LONG":
                # Looking for last bearish candle (close < open)
                if close_j < open_j:
                    # Verify displacement: the move from j+1 to bo_bar >= OB_DISPLACEMENT_ATR * ATR
                    if j + 1 > bo_bar:
                        continue
                    displacement = float(df["high"].iloc[bo_bar]) - float(df["low"].iloc[j + 1])
                    if displacement >= ob_disp_atr * atr_val:
                        ob_bottom = min(open_j, close_j)
                        ob_top    = max(open_j, close_j)
                        return RetestZone(
                            zone_type="OB",
                            direction=direction,
                            top=ob_top,
                            bottom=ob_bottom,
                            formed_bar=j,
                            priority=1,   # Raised to 1 — OB is highest structural priority
                        )
            else:  # SHORT
                # Looking for last bullish candle (close > open)
                if close_j > open_j:
                    if j + 1 > bo_bar:
                        continue
                    displacement = float(df["high"].iloc[j + 1]) - float(df["low"].iloc[bo_bar])
                    if displacement >= ob_disp_atr * atr_val:
                        ob_bottom = min(open_j, close_j)
                        ob_top    = max(open_j, close_j)
                        return RetestZone(
                            zone_type="OB",
                            direction=direction,
                            top=ob_top,
                            bottom=ob_bottom,
                            formed_bar=j,
                            priority=1,   # Raised to 1 — OB is highest structural priority
                        )
        return None

    def _find_fvg(self, breakout, i: int, df: pd.DataFrame,
                  atr_val: float) -> Optional[RetestZone]:
        """
        FVG: 3-candle imbalance pattern created DURING the breakout displacement.
        Bullish FVG: candle[N-2].high < candle[N].low
        Bearish FVG: candle[N-2].low > candle[N].high
        Minimum size: FVG_MIN_SIZE_ATR * ATR
        """
        fvg_min = self._p("FVG_MIN_SIZE_ATR", 0.20) * atr_val
        direction = breakout.direction
        bo_bar = breakout.breakout_bar

        # Scan the 3-candle windows in the displacement area (bo_bar - 5 to bo_bar)
        scan_start = max(2, bo_bar - 5)
        best_fvg = None

        for n in range(scan_start, bo_bar + 1):
            if n < 2 or n >= len(df):
                continue
            c0_high = float(df["high"].iloc[n - 2])
            c0_low  = float(df["low"].iloc[n - 2])
            c2_high = float(df["high"].iloc[n])
            c2_low  = float(df["low"].iloc[n])

            if direction == "LONG":
                gap = c2_low - c0_high
                if gap >= fvg_min:
                    best_fvg = RetestZone(
                        zone_type="FVG",
                        direction=direction,
                        top=c2_low,
                        bottom=c0_high,
                        formed_bar=n,
                        priority=2,   # FVG raised above BB — audit shows FVG 100% WR
                    )
            else:  # SHORT
                gap = c0_low - c2_high
                if gap >= fvg_min:
                    best_fvg = RetestZone(
                        zone_type="FVG",
                        direction=direction,
                        top=c0_low,
                        bottom=c2_high,
                        formed_bar=n,
                        priority=2,   # FVG raised above BB — audit shows FVG 100% WR
                    )

        return best_fvg

    def _find_bb(self, breakout, i: int, df: pd.DataFrame,
                 atr_val: float) -> Optional[RetestZone]:
        """
        Breaker Block: a prior OB that failed (price broke through its body)
        and now acts as support (bullish) or resistance (bearish).
        Returns the highest-priority BB if found.
        """
        direction = breakout.direction
        bo_bar = breakout.breakout_bar
        scan_start = max(0, bo_bar - 50)

        for j in range(scan_start, bo_bar):
            open_j  = float(df["open"].iloc[j])
            close_j = float(df["close"].iloc[j])
            ob_top    = max(open_j, close_j)
            ob_bottom = min(open_j, close_j)

            if direction == "LONG":
                # Look for prior bearish OB that has been broken (price closed above top)
                if close_j >= open_j:
                    continue  # not bearish
                # Check if price closed through the OB body after formation
                broken = False
                for k in range(j + 1, bo_bar + 1):
                    if float(df["close"].iloc[k]) > ob_top:
                        broken = True
                        break
                if broken:
                    return RetestZone(
                        zone_type="BB",
                        direction=direction,
                        top=ob_top,
                        bottom=ob_bottom,
                        formed_bar=j,
                        priority=3,  # Lowered from 1 — below OB(1) and FVG(2) per audit
                    )
            else:  # SHORT
                if close_j <= open_j:
                    continue  # not bullish
                broken = False
                for k in range(j + 1, bo_bar + 1):
                    if float(df["close"].iloc[k]) < ob_bottom:
                        broken = True
                        break
                if broken:
                    return RetestZone(
                        zone_type="BB",
                        direction=direction,
                        top=ob_top,
                        bottom=ob_bottom,
                        formed_bar=j,
                        priority=3,  # Lowered from 1 — below OB(1) and FVG(2) per audit
                    )
        return None

    def _find_sr(self, breakout, range_state, atr_val: float) -> Optional[RetestZone]:
        """
        Broken S/R: the broken range boundary now acts as support (bullish) or resistance (bearish).
        Zone extends SR_ZONE_BAND_ATR * ATR on each side of the level.
        """
        if not range_state.valid:
            return None

        sr_band = self._p("SR_ZONE_BAND_ATR", 0.15) * atr_val
        direction = breakout.direction

        if direction == "LONG":
            level = range_state.range_high  # broken high now acts as support
        else:
            level = range_state.range_low   # broken low now acts as resistance

        return RetestZone(
            zone_type="SR",
            direction=direction,
            top=level + sr_band,
            bottom=level - sr_band,
            formed_bar=range_state.range_end_bar,
            priority=4,  # lowest priority
        )

    def _enforce_min_width(self, zone: RetestZone, atr_val: float) -> RetestZone:
        """Ensure zone width is at least ZONE_MIN_WIDTH_ABS."""
        min_width = self._p("ZONE_MIN_WIDTH_ABS", 0.20)
        width = zone.top - zone.bottom
        if width < min_width:
            gap = (min_width - width) / 2
            zone.top    += gap
            zone.bottom -= gap
        return zone

    def check_retest(self, i: int, df: pd.DataFrame) -> bool:
        """Check if price has entered the primary retest zone at bar i."""
        zone = self.primary_zone
        if zone is None:
            return False
        low_i  = float(df["low"].iloc[i])
        high_i = float(df["high"].iloc[i])
        return low_i <= zone.top and high_i >= zone.bottom

    def check_reaction(self, i: int, df: pd.DataFrame, entry_mode: str = None) -> bool:
        """
        Check if price shows a confirmed reaction from the zone at bar i.
        MODE_CLOSE_OUTSIDE: candle closes back outside zone boundary
        MODE_WICK_REJECTION: wick into zone but close away from it
        MODE_IMMEDIATE: any zone touch counts
        """
        zone = self.primary_zone
        if zone is None:
            return False

        if entry_mode is None:
            entry_mode = self._p("ENTRY_MODE", "MODE_CLOSE_OUTSIDE")

        close_i = float(df["close"].iloc[i])
        low_i   = float(df["low"].iloc[i])
        high_i  = float(df["high"].iloc[i])
        open_i  = float(df["open"].iloc[i])

        if entry_mode == "MODE_IMMEDIATE":
            return True

        if zone.direction == "LONG":
            entered_zone = low_i <= zone.top
            if entry_mode == "MODE_CLOSE_OUTSIDE":
                return entered_zone and close_i > zone.top
            elif entry_mode == "MODE_WICK_REJECTION":
                return entered_zone and close_i > zone.bottom and close_i > open_i
        else:  # SHORT
            entered_zone = high_i >= zone.bottom
            if entry_mode == "MODE_CLOSE_OUTSIDE":
                return entered_zone and close_i < zone.bottom
            elif entry_mode == "MODE_WICK_REJECTION":
                return entered_zone and close_i < zone.top and close_i < open_i

        return False
