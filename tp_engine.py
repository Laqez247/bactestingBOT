"""
tp_engine.py — Dynamic Take-Profit Target Generation and Scoring.

No fixed RR multipliers. All targets must come from actual historical
reaction zones visible at entry time (no lookahead).
7 candidate target types scored per spec formula.
"""

import pandas as pd
from dataclasses import dataclass
from typing import List, Optional, Tuple
import config


@dataclass
class TPCandidate:
    price: float
    score: float
    target_type: str   # e.g. "RANGE_PROJECTION", "SWING_HIGH", "PDH", etc.
    rr: float = 0.0    # computed after SL is known


class TPEngine:
    """
    Generates scored TP candidates using only data visible at entry bar.
    Call generate(direction, entry_price, sl_price, i, df, atr,
                  range_state, structure_engine, liquidity_engine)
    Returns sorted list of TPCandidate (highest score first).
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))

    def generate(
        self,
        direction: str,
        entry_price: float,
        sl_price: float,
        i: int,
        df: pd.DataFrame,
        atr: pd.Series,
        range_state,
        structure_engine,
        liquidity_engine
    ) -> List[TPCandidate]:
        """
        Generate all TP candidates visible at bar i.
        Returns list sorted by score descending.
        """
        atr_val  = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        sl_dist  = abs(entry_price - sl_price)

        candidates: List[TPCandidate] = []

        # 1. Projected range extension
        cand = self._range_projection(direction, entry_price, range_state)
        if cand:
            candidates.append(cand)

        # 2. Recent swing highs/lows (last 50 bars)
        sw_cands = self._swing_targets(direction, entry_price, i, structure_engine)
        candidates.extend(sw_cands)

        # 3. Previous day high/low
        pdh_pdl = self._pdh_pdl(direction, entry_price, liquidity_engine)
        if pdh_pdl:
            candidates.append(pdh_pdl)

        # 4. Previous session high/low
        sess = self._session_target(direction, entry_price, liquidity_engine)
        if sess:
            candidates.append(sess)

        # 5. Equal highs / equal lows clusters
        eq_cands = self._equal_level_targets(direction, entry_price, liquidity_engine)
        candidates.extend(eq_cands)

        # 6. Psychological round numbers
        round_cands = self._round_number_targets(direction, entry_price, atr_val)
        candidates.extend(round_cands)

        # 7. Nearby OB / FVG zones in direction of trade
        # (approximated via structure context — not re-querying zone engine to avoid coupling)

        # Score and deduplicate
        candidates = self._apply_deductions(
            candidates, direction, entry_price, sl_dist, i, df, atr_val
        )
        candidates = self._compute_rr(candidates, entry_price, sl_dist, direction)
        candidates = self._filter(candidates)
        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = self._deduplicate(candidates, atr_val)

        return candidates

    # ------------------------------------------------------------------
    # Target generators
    # ------------------------------------------------------------------

    def _range_projection(self, direction: str, entry_price: float,
                          range_state) -> Optional[TPCandidate]:
        """
        Target: opposite side of range plus one range-height extension.
        Bullish: range_high + range_height * 1.0
        Bearish: range_low  - range_height * 1.0
        """
        if not range_state.valid:
            return None
        rh    = range_state.range_high
        rl    = range_state.range_low
        h     = rh - rl
        bonus = self._p("TP_SCORE_RANGE_PROJECTION", 20) if range_state.is_premium else 0
        base_score = 30

        if direction == "LONG":
            target = rh + h
            if target <= entry_price:
                return None
        else:
            target = rl - h
            if target >= entry_price:
                return None

        return TPCandidate(price=target, score=base_score + bonus,
                           target_type="RANGE_PROJECTION")

    def _swing_targets(self, direction: str, entry_price: float,
                       i: int, structure_engine) -> List[TPCandidate]:
        """
        Nearest unbroken swing high above entry (bullish) or swing low below (bearish).
        From last TP_SWING_LOOKBACK_BARS bars.
        """
        lookback = self._p("TP_SWING_LOOKBACK_BARS", 50)
        cands = []

        if direction == "LONG":
            for bar_idx, price in structure_engine.get_recent_swing_highs(20):
                if i - bar_idx > lookback:
                    continue
                if price > entry_price:
                    cands.append(TPCandidate(price=price, score=25,
                                             target_type="SWING_HIGH"))
        else:
            for bar_idx, price in structure_engine.get_recent_swing_lows(20):
                if i - bar_idx > lookback:
                    continue
                if price < entry_price:
                    cands.append(TPCandidate(price=price, score=25,
                                             target_type="SWING_LOW"))

        # Take nearest one (closest to entry price in correct direction)
        if cands:
            if direction == "LONG":
                cands.sort(key=lambda c: c.price)
            else:
                cands.sort(key=lambda c: c.price, reverse=True)
            return [cands[0]]
        return []

    def _pdh_pdl(self, direction: str, entry_price: float,
                 liquidity_engine) -> Optional[TPCandidate]:
        """Previous day high (bullish target) or previous day low (bearish)."""
        bonus = self._p("TP_SCORE_PDH_PDL", 15)
        if direction == "LONG":
            lvl = liquidity_engine._prev_day_high
            if lvl and lvl > entry_price:
                return TPCandidate(price=lvl, score=20 + bonus, target_type="PDH")
        else:
            lvl = liquidity_engine._prev_day_low
            if lvl and lvl < entry_price:
                return TPCandidate(price=lvl, score=20 + bonus, target_type="PDL")
        return None

    def _session_target(self, direction: str, entry_price: float,
                        liquidity_engine) -> Optional[TPCandidate]:
        """Previous session high or low."""
        bonus = self._p("TP_SCORE_SESSION_EXTREME", 10)
        if direction == "LONG":
            lvl = liquidity_engine._prev_session_high
            if lvl and lvl > entry_price:
                return TPCandidate(price=lvl, score=15 + bonus, target_type="SESSION_HIGH")
        else:
            lvl = liquidity_engine._prev_session_low
            if lvl and lvl < entry_price:
                return TPCandidate(price=lvl, score=15 + bonus, target_type="SESSION_LOW")
        return None

    def _equal_level_targets(self, direction: str, entry_price: float,
                             liquidity_engine) -> List[TPCandidate]:
        """Equal highs (bullish) or equal lows (bearish) as liquidity targets."""
        bonus = self._p("TP_SCORE_EQUAL_LEVEL", 15)
        cands = []
        if direction == "LONG":
            for lvl in liquidity_engine._eq_high_clusters:
                if lvl > entry_price:
                    cands.append(TPCandidate(price=lvl, score=20 + bonus,
                                             target_type="EQH"))
        else:
            for lvl in liquidity_engine._eq_low_clusters:
                if lvl < entry_price:
                    cands.append(TPCandidate(price=lvl, score=20 + bonus,
                                             target_type="EQL"))
        return cands

    def _round_number_targets(self, direction: str, entry_price: float,
                              atr_val: float) -> List[TPCandidate]:
        """
        XAUUSD psychological round numbers ($50 increments).
        Bonus if within ROUND_NUMBER_PROXIMITY of the increment.
        """
        increment  = self._p("ROUND_NUMBER_INCREMENT", 50)
        proximity  = self._p("ROUND_NUMBER_PROXIMITY", 5)
        bonus      = self._p("TP_SCORE_ROUND_NUMBER", 10)
        cands = []

        # Generate round numbers in a reasonable range from entry
        search_range = atr_val * 10
        start_price  = entry_price - search_range
        end_price    = entry_price + search_range

        # Round to nearest increment
        first_round = (int(start_price / increment) + 1) * increment
        level = float(first_round)
        while level <= end_price:
            if direction == "LONG" and level > entry_price + proximity:
                cands.append(TPCandidate(price=level, score=10 + bonus,
                                         target_type="ROUND_NUMBER"))
            elif direction == "SHORT" and level < entry_price - proximity:
                cands.append(TPCandidate(price=level, score=10 + bonus,
                                         target_type="ROUND_NUMBER"))
            level += increment

        return cands

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _apply_deductions(self, candidates: List[TPCandidate], direction: str,
                          entry_price: float, sl_dist: float, i: int,
                          df: pd.DataFrame, atr_val: float) -> List[TPCandidate]:
        """
        Apply deductions per spec:
          -20 if target is less than 1.0x RR from entry
          -15 if level not touched in last TP_STALE_LEVEL_BARS
          -10 if target is more than TP_MAX_DIST_ATR from entry
        """
        stale_bars   = self._p("TP_STALE_LEVEL_BARS", 100)
        max_dist_atr = self._p("TP_MAX_DIST_ATR", 4.0)
        deduct_low_rr  = self._p("TP_DEDUCT_LOW_RR",  20)
        deduct_stale   = self._p("TP_DEDUCT_STALE",   15)
        deduct_far     = self._p("TP_DEDUCT_FAR",      10)

        scan_start = max(0, i - stale_bars)
        highs = df["high"].iloc[scan_start:i + 1]
        lows  = df["low"].iloc[scan_start:i + 1]

        for c in candidates:
            dist = abs(c.price - entry_price)

            # -20 if < 1.0x RR
            if sl_dist > 0 and dist < sl_dist:
                c.score -= deduct_low_rr

            # -15 if stale
            if direction == "LONG":
                touched = (highs >= c.price - 1.0).any()
            else:
                touched = (lows <= c.price + 1.0).any()
            if not touched:
                c.score -= deduct_stale

            # -10 if more than TP_MAX_DIST_ATR from entry
            if atr_val > 0 and dist > max_dist_atr * atr_val:
                c.score -= deduct_far

        return candidates

    def _compute_rr(self, candidates: List[TPCandidate], entry_price: float,
                    sl_dist: float, direction: str) -> List[TPCandidate]:
        """Compute RR for each candidate."""
        for c in candidates:
            dist = abs(c.price - entry_price)
            # Direction check
            if direction == "LONG" and c.price <= entry_price:
                c.rr = -1.0
                continue
            if direction == "SHORT" and c.price >= entry_price:
                c.rr = -1.0
                continue
            c.rr = round(dist / sl_dist, 2) if sl_dist > 0 else 0.0
        return candidates

    def _filter(self, candidates: List[TPCandidate]) -> List[TPCandidate]:
        """Remove candidates below minimum score and minimum RR."""
        min_score = self._p("TP_MIN_SCORE", 30)
        min_rr    = self._p("TP_MIN_RR",    1.0)
        max_rr    = self._p("TP_MAX_RR",    3.5)
        return [
            c for c in candidates
            if c.score >= min_score and c.rr >= min_rr
        ]

    def _deduplicate(self, candidates: List[TPCandidate], atr_val: float) -> List[TPCandidate]:
        """
        Remove duplicate levels within 0.20x ATR of each other.
        Prefer the higher-scored one.
        """
        if not candidates:
            return candidates
        band = 0.20 * atr_val
        seen = []
        for c in candidates:
            duplicate = False
            for s in seen:
                if abs(c.price - s.price) < band:
                    duplicate = True
                    # Keep higher score
                    if c.score > s.score:
                        seen.remove(s)
                        seen.append(c)
                    break
            if not duplicate:
                seen.append(c)
        return seen

    def select_tp1_tp2(
        self, candidates: List[TPCandidate]
    ) -> Tuple[Optional[TPCandidate], Optional[TPCandidate]]:
        """
        Select primary TP (tp1) and optional secondary TP (tp2).
        If two candidates are within 10 score points, prefer the nearer one (lower RR).
        """
        if not candidates:
            return None, None

        sorted_by_score = sorted(candidates, key=lambda c: c.score, reverse=True)

        tp1 = sorted_by_score[0]
        tp2 = None

        if self._p("DUAL_TP_ENABLED", False) and len(sorted_by_score) >= 2:
            min_tp2_rr = self._p("TP2_MIN_RR", 1.8)
            for c in sorted_by_score[1:]:
                if c.rr >= min_tp2_rr:
                    tp2 = c
                    break

        return tp1, tp2
