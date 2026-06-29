"""
liquidity_engine.py — SSL and BSL pool construction and sweep detection.

DIRECTIONAL RULE (non-negotiable, per spec):
  SSL sweep → BULLISH trade ONLY
  BSL sweep → BEARISH trade ONLY
Cross-contamination causes immediate setup rejection at Gate 3.
"""

import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional
import config


@dataclass
class LiquidityPool:
    pool_type: str      # e.g. "SSL_RANGE_LOW", "BSL_EQUAL_HIGHS"
    level: float
    formed_bar: int
    consumed: bool = False


@dataclass
class SweepEvent:
    sweep_type: str     # SSL_* or BSL_*
    bar: int
    level: float
    wick_extension_abs: float   # $ amount beyond level
    wick_extension_atr: float   # multiples of ATR
    near_news: bool = False     # flagged but not disqualified


class LiquidityEngine:
    """
    Maintains live SSL and BSL pool lists and detects sweep events.

    Call update(i, df, atr, range_state, structure_engine) each bar.
    After a sweep, state.ssl_swept or state.bsl_swept is set True.
    After a trade opens, call reset_ssl() or reset_bsl().
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))

        # Active pools
        self.ssl_pools: List[LiquidityPool] = []
        self.bsl_pools: List[LiquidityPool] = []

        # Sweep state
        self.ssl_swept: bool = False
        self.bsl_swept: bool = False
        self.ssl_sweep_type: str = ""
        self.bsl_sweep_type: str = ""
        self.ssl_sweep_bar: int = -1
        self.bsl_sweep_bar: int = -1

        # Full sweep history for metrics
        self.sweep_history: List[SweepEvent] = []

        # Day tracking for PDH/PDL
        self._current_day: Optional[str] = None
        self._prev_day_high: Optional[float] = None
        self._prev_day_low:  Optional[float] = None
        self._day_high:  float = -1e9
        self._day_low:   float = 1e9

        # Session tracking
        self._current_session:  Optional[str] = None
        self._session_high: float = -1e9
        self._session_low:  float = 1e9
        self._prev_session_high: Optional[float] = None
        self._prev_session_low:  Optional[float] = None

        # Equal highs / equal lows clusters
        self._eq_high_clusters: List[float] = []  # clustered price levels
        self._eq_low_clusters:  List[float] = []

    def reset_ssl(self) -> None:
        """Call after a LONG trade opens."""
        self.ssl_swept     = False
        self.ssl_sweep_type = ""
        self.ssl_sweep_bar  = -1

    def reset_bsl(self) -> None:
        """Call after a SHORT trade opens."""
        self.bsl_swept     = False
        self.bsl_sweep_type = ""
        self.bsl_sweep_bar  = -1

    def reset_stale(self, current_bar: int) -> None:
        """
        Reset sweep flags if SWEEP_LOOKBACK_BARS have passed since the sweep
        with no MSS/BOS — the sweep is stale.
        """
        lookback = self._p("SWEEP_LOOKBACK_BARS", 15)
        if self.ssl_swept and self.ssl_sweep_bar >= 0:
            if current_bar - self.ssl_sweep_bar > lookback:
                self.ssl_swept     = False
                self.ssl_sweep_type = ""
                self.ssl_sweep_bar  = -1
        if self.bsl_swept and self.bsl_sweep_bar >= 0:
            if current_bar - self.bsl_sweep_bar > lookback:
                self.bsl_swept     = False
                self.bsl_sweep_type = ""
                self.bsl_sweep_bar  = -1

    def update(self, i: int, df: pd.DataFrame, atr: pd.Series,
               range_state, structure_engine) -> None:
        """
        Process bar i:
        1. Update day/session tracking
        2. Rebuild SSL/BSL pools
        3. Detect sweeps
        4. Reset stale sweeps
        """
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        ts = df.index[i]

        self._update_day(i, df, ts)
        self._update_session(i, df, ts)
        self._update_eq_clusters(structure_engine, atr_val)
        self._rebuild_pools(i, df, range_state, structure_engine, atr_val)
        self._detect_sweeps(i, df, atr_val, ts)
        self.reset_stale(i)

    def _update_day(self, i: int, df: pd.DataFrame, ts) -> None:
        day_str = str(ts.date())
        if day_str != self._current_day:
            # Day rolled — archive previous day extremes
            if self._current_day is not None:
                self._prev_day_high = self._day_high
                self._prev_day_low  = self._day_low
            self._current_day = day_str
            self._day_high = float(df["high"].iloc[i])
            self._day_low  = float(df["low"].iloc[i])
        else:
            self._day_high = max(self._day_high, float(df["high"].iloc[i]))
            self._day_low  = min(self._day_low,  float(df["low"].iloc[i]))

    def _update_session(self, i: int, df: pd.DataFrame, ts) -> None:
        hour = ts.hour
        sessions = self._p("TRADE_SESSIONS", {})
        current = None
        for name, window in sessions.items():
            open_h  = int(window["open"].split(":")[0])
            close_h = int(window["close"].split(":")[0])
            if open_h <= hour < close_h:
                current = name
                break

        if current != self._current_session:
            if self._current_session is not None:
                self._prev_session_high = self._session_high
                self._prev_session_low  = self._session_low
            self._current_session = current
            self._session_high = float(df["high"].iloc[i])
            self._session_low  = float(df["low"].iloc[i])
        else:
            self._session_high = max(self._session_high, float(df["high"].iloc[i]))
            self._session_low  = min(self._session_low,  float(df["low"].iloc[i]))

    def _update_eq_clusters(self, structure_engine, atr_val: float) -> None:
        """Build equal-high and equal-low clusters from confirmed pivots."""
        band = self._p("EQUAL_HIGH_LOW_BAND", 0.20) * atr_val

        # Equal highs: swing highs within BAND of each other
        sh_list = structure_engine.get_recent_swing_highs(10)
        self._eq_high_clusters = self._cluster_levels(
            [p for _, p in sh_list], band
        )

        sl_list = structure_engine.get_recent_swing_lows(10)
        self._eq_low_clusters = self._cluster_levels(
            [p for _, p in sl_list], band
        )

    def _cluster_levels(self, prices: list, band: float) -> List[float]:
        """Return average of each cluster of prices within `band` of each other."""
        if not prices:
            return []
        sorted_p = sorted(prices)
        clusters = []
        cluster = [sorted_p[0]]
        for p in sorted_p[1:]:
            if p - cluster[-1] <= band:
                cluster.append(p)
            else:
                if len(cluster) >= 2:
                    clusters.append(sum(cluster) / len(cluster))
                cluster = [p]
        if len(cluster) >= 2:
            clusters.append(sum(cluster) / len(cluster))
        return clusters

    def _rebuild_pools(self, i: int, df: pd.DataFrame, range_state,
                       structure_engine, atr_val: float) -> None:
        """
        Rebuild the active SSL and BSL pool lists.
        Do not clear already-consumed pools — just regenerate unconsumed ones.
        """
        ssl_pools: List[LiquidityPool] = []
        bsl_pools: List[LiquidityPool] = []

        # Range boundaries
        if range_state.valid:
            ssl_pools.append(LiquidityPool("SSL_RANGE_LOW",  range_state.range_low,  i))
            bsl_pools.append(LiquidityPool("BSL_RANGE_HIGH", range_state.range_high, i))

        # Equal lows / equal highs
        for lvl in self._eq_low_clusters:
            ssl_pools.append(LiquidityPool("SSL_EQUAL_LOWS", lvl, i))
        for lvl in self._eq_high_clusters:
            bsl_pools.append(LiquidityPool("BSL_EQUAL_HIGHS", lvl, i))

        # Previous day levels
        if self._prev_day_low is not None:
            ssl_pools.append(LiquidityPool("SSL_PDL", self._prev_day_low, i))
        if self._prev_day_high is not None:
            bsl_pools.append(LiquidityPool("BSL_PDH", self._prev_day_high, i))

        # Previous session levels
        if self._prev_session_low is not None:
            ssl_pools.append(LiquidityPool("SSL_SESSION_LOW",  self._prev_session_low,  i))
        if self._prev_session_high is not None:
            bsl_pools.append(LiquidityPool("BSL_SESSION_HIGH", self._prev_session_high, i))

        # Recent confirmed swing lows/highs (execution TF)
        for bar_idx, price in structure_engine.get_recent_swing_lows(5):
            if bar_idx < i:
                ssl_pools.append(LiquidityPool("SSL_SWING_LOW",  price, bar_idx))
        for bar_idx, price in structure_engine.get_recent_swing_highs(5):
            if bar_idx < i:
                bsl_pools.append(LiquidityPool("BSL_SWING_HIGH", price, bar_idx))

        self.ssl_pools = ssl_pools
        self.bsl_pools = bsl_pools

    def _detect_sweeps(self, i: int, df: pd.DataFrame, atr_val: float, ts) -> None:
        """
        Check if bar i sweeps any SSL or BSL pool.

        SSL sweep (for BULLISH direction):
          - candle.low < pool.level - (SWEEP_MIN_WICK_ATR * atr)
          - AND candle.low < pool.level - SWEEP_MIN_WICK_ABS
          - AND candle.close > pool.level (closed back above)

        BSL sweep (for BEARISH direction):
          - candle.high > pool.level + (SWEEP_MIN_WICK_ATR * atr)
          - AND candle.high > pool.level + SWEEP_MIN_WICK_ABS
          - AND candle.close < pool.level (closed back below)
        """
        min_wick_atr = self._p("SWEEP_MIN_WICK_ATR", 0.25)
        min_wick_abs = self._p("SWEEP_MIN_WICK_ABS", 0.30)
        near_news_min = self._p("NEWS_PROXIMITY_MINUTES", 30)

        row = df.iloc[i]
        low_i   = float(row["low"])
        high_i  = float(row["high"])
        close_i = float(row["close"])

        near_news = self._is_near_news(ts)

        # --- SSL sweeps ---
        for pool in self.ssl_pools:
            if pool.consumed:
                continue
            level = pool.level
            wick_needed_atr = min_wick_atr * atr_val
            wick_needed_abs = min_wick_abs

            if (low_i < level - wick_needed_atr and
                    low_i < level - wick_needed_abs and
                    close_i > level):
                wick_ext_abs = level - low_i
                wick_ext_atr = wick_ext_abs / atr_val if atr_val > 0 else 0
                ev = SweepEvent(
                    sweep_type=pool.pool_type,
                    bar=i,
                    level=level,
                    wick_extension_abs=wick_ext_abs,
                    wick_extension_atr=wick_ext_atr,
                    near_news=near_news,
                )
                self.sweep_history.append(ev)

                # Only update ssl_swept if we haven't swept yet (or this is newer)
                if not self.ssl_swept or i > self.ssl_sweep_bar:
                    self.ssl_swept      = True
                    self.ssl_sweep_type = pool.pool_type
                    self.ssl_sweep_bar  = i

                pool.consumed = True

        # --- BSL sweeps ---
        for pool in self.bsl_pools:
            if pool.consumed:
                continue
            level = pool.level
            wick_needed_atr = min_wick_atr * atr_val
            wick_needed_abs = min_wick_abs

            if (high_i > level + wick_needed_atr and
                    high_i > level + wick_needed_abs and
                    close_i < level):
                wick_ext_abs = high_i - level
                wick_ext_atr = wick_ext_abs / atr_val if atr_val > 0 else 0
                ev = SweepEvent(
                    sweep_type=pool.pool_type,
                    bar=i,
                    level=level,
                    wick_extension_abs=wick_ext_abs,
                    wick_extension_atr=wick_ext_atr,
                    near_news=near_news,
                )
                self.sweep_history.append(ev)

                if not self.bsl_swept or i > self.bsl_sweep_bar:
                    self.bsl_swept      = True
                    self.bsl_sweep_type = pool.pool_type
                    self.bsl_sweep_bar  = i

                pool.consumed = True

    def _is_near_news(self, ts) -> bool:
        """
        Check if timestamp falls within NEWS_PROXIMITY_MINUTES of a known
        high-impact news window. Approximate check using weekday + hour + minute.
        """
        news_windows = self._p("HIGH_IMPACT_NEWS_WINDOWS", [])
        near_min = self._p("NEWS_PROXIMITY_MINUTES", 30)
        for w in news_windows:
            if ts.weekday() == w.get("weekday", -1):
                news_minutes = w["hour"] * 60 + w["minute"]
                bar_minutes  = ts.hour * 60 + ts.minute
                if abs(bar_minutes - news_minutes) <= near_min:
                    return True
        return False

    def get_current_spread(self, ts) -> float:
        """
        Return current spread estimate.
        Uses NEWS_SPREAD_MULTIPLIER during high-impact windows.
        """
        base = self._p("DEFAULT_SPREAD", 0.35)
        if self._is_near_news(ts):
            return base * self._p("NEWS_SPREAD_MULTIPLIER", 4.0)
        return base
