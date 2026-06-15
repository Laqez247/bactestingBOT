"""
trade_simulator.py — 10-Gate Validation Guard + Bar-by-Bar Trade Simulation.

The 10-gate validation runs BEFORE every trade open.
First gate failure = immediate rejection with logged reason.
No partial credit. No override.

Bar-by-bar simulation uses realistic fills:
  Entry : limit order — zone boundary ± spread ± slippage
  SL    : market order — SL price + slippage (extra cost)
  TP    : limit order — TP price exactly (no additional slippage)
"""

import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
import config


@dataclass
class TradeRecord:
    setup_id: str
    iteration: int
    symbol: str
    timeframe: str
    direction: str

    # Range info
    range_high: float = 0.0
    range_low: float = 0.0
    range_quality_score: float = 0.0
    range_bars: int = 0
    range_touches_high: int = 0
    range_touches_low: int = 0

    # ATR at setup
    atr_at_setup: float = 0.0
    range_height_atr: float = 0.0

    # Sweep info
    ssl_bsl_sweep_type: str = ""
    sweep_bar: int = -1
    sweep_wick_size_atr: float = 0.0
    sweep_wick_size_abs: float = 0.0

    # Structure break info
    structure_break_type: str = ""
    breakout_bar: int = -1
    breakout_direction: str = ""
    breakout_body_atr: float = 0.0
    breakout_quality: str = ""

    # HTF info
    htf_trend: str = ""

    # Zone info
    retest_zone_type: str = ""
    retest_zone_top: float = 0.0
    retest_zone_bottom: float = 0.0

    # Prices
    entry_price: float = 0.0
    sl_price: float = 0.0          # Current SL (may be moved to breakeven after TP1)
    original_sl_price: float = 0.0 # Original SL at trade open — used for R multiple calc
    tp1_price: float = 0.0
    tp2_price: float = 0.0

    # TP scoring
    tp_score: float = 0.0
    rr_tp1: float = 0.0
    rr_tp2: float = 0.0

    # Validation
    rejection_reason: str = ""  # empty = not rejected

    # Execution
    entry_bar: int = -1
    exit_bar: int = -1
    exit_reason: str = ""     # TP1 | TP2 | SL | TIMEOUT | INVALIDATED
    exit_price: float = 0.0

    # Performance
    r_multiple: float = 0.0
    mae_abs: float = 0.0      # Max Adverse Excursion in $
    mfe_abs: float = 0.0      # Max Favorable Excursion in $
    bars_held: int = 0

    # Context
    session: str = ""
    timestamp: str = ""

    # Dual TP tracking
    tp1_hit: bool = False
    partial_exit_price: float = 0.0


# ------------------------------------------------------------------
# 10-GATE VALIDATION FUNCTION — runs before every trade open
# ------------------------------------------------------------------

def validate_setup(
    direction: str,
    i: int,
    df: pd.DataFrame,
    atr_val: float,
    htf_trend: str,
    range_state,
    liquidity_engine,
    breakout_event,
    zone_engine,
    zone_reaction: bool,
    best_tp_score: float,
    best_rr: float,
    sl_price: float,
    entry_price: float,
    current_spread: float,
    cfg: dict = None
) -> str:
    """
    Sequential 10-gate guard function.
    Returns "" if all gates pass, or the rejection reason string on first failure.
    """
    _p = lambda k, default=None: (cfg or {}).get(k, getattr(config, k, default))

    # Gate 1: HTF trend alignment
    if _p("HIGHER_TF_FILTER_ON", True):
        if direction == "LONG" and htf_trend == "DOWNTREND":
            return "HTF_NOT_BULLISH"
        if direction == "SHORT" and htf_trend == "UPTREND":
            return "HTF_NOT_BEARISH"

    # Gate 2: Valid compression range
    if not range_state.valid:
        return "NO_VALID_RANGE"

    # Gate 3: Correct liquidity sweep for direction
    if direction == "LONG":
        if breakout_event is None or not str(breakout_event.sweep_type).startswith("SSL_"):
            return "NO_SSL_SWEEP"
    else:
        if breakout_event is None or not str(breakout_event.sweep_type).startswith("BSL_"):
            return "NO_BSL_SWEEP"

    # Gate 3b: SHORT sweep quality filter — block low-WR sweep types for SHORT
    # Uses liquidity_engine directly (same source as TradeRecord.ssl_bsl_sweep_type)
    if direction == "SHORT":
        blocked = _p("SHORT_BLOCKED_SWEEPS", [])
        if blocked:
            frozen_sweep = getattr(breakout_event, "sweep_type", "")
            if frozen_sweep in blocked:
                return f"SHORT_BLOCKED_SWEEP_{frozen_sweep}"

    # Gate 4: MSS or BOS confirmed (already consumed from structure_engine
    #          by breakout_engine, so we check via breakout_event presence)
    if breakout_event is None:
        return "NO_BULLISH_MSS_BOS" if direction == "LONG" else "NO_BEARISH_MSS_BOS"
    if direction == "LONG" and breakout_event.direction != "LONG":
        return "NO_BULLISH_MSS_BOS"
    if direction == "SHORT" and breakout_event.direction != "SHORT":
        return "NO_BEARISH_MSS_BOS"

    # Gate 4b: MSS_REQUIRED filter
    if _p("MSS_REQUIRED", False):
        needed = "MSS_BULLISH" if direction == "LONG" else "MSS_BEARISH"
        if breakout_event.structure_type != needed:
            return "MSS_REQUIRED_NOT_MET"

    # Gate 5: Valid retest zone exists
    if not zone_engine.has_valid_zone:
        return "NO_RETEST_ZONE"

    # Gate 6: Zone reaction confirmed
    if not zone_reaction:
        return "NO_ZONE_REACTION"

    # Gate 7: Valid dynamic TP target exists
    tp_min_score = _p("TP_MIN_SCORE", 30)
    if best_tp_score < tp_min_score:
        return "NO_VALID_TARGET"

    # Gate 8: RR is acceptable
    tp_min_rr = _p("TP_MIN_RR", 1.0)
    if best_rr < tp_min_rr:
        return "RR_TOO_LOW"

    # Gate 9: SL distance within limits
    sl_max_dist_atr = _p("SL_MAX_DISTANCE_ATR", 2.0)
    sl_min_dist_abs = _p("SL_MIN_DISTANCE_ABS", 0.30)
    sl_dist = abs(entry_price - sl_price)
    if sl_dist > sl_max_dist_atr * atr_val:
        return "SL_TOO_WIDE"
    if sl_dist < sl_min_dist_abs:
        sl_price = entry_price - sl_min_dist_abs if direction == "LONG" else entry_price + sl_min_dist_abs

    # Gate 10: Spread filter
    spread_max = _p("SPREAD_FILTER_MAX", 1.00)
    if current_spread > spread_max:
        return "SPREAD_TOO_HIGH"

    # All gates passed
    return ""


# ------------------------------------------------------------------
# TRADE SIMULATOR
# ------------------------------------------------------------------

class TradeSimulator:
    """
    Bar-by-bar simulation engine.
    Manages active trade state and records outcomes.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._p = lambda k, default=None: self.cfg.get(k, getattr(config, k, default))

        self._active_trade: Optional[TradeRecord] = None
        self.completed_trades: List[TradeRecord] = []
        self.rejected_setups:  List[TradeRecord] = []
        self._setup_counter = 0

    def has_active_trade(self) -> bool:
        return self._active_trade is not None

    def try_open_trade(
        self,
        direction: str,
        i: int,
        df: pd.DataFrame,
        atr: pd.Series,
        htf_trend: str,
        range_state,
        liquidity_engine,
        breakout_event,
        zone_engine,
        zone_reaction: bool,
        tp_candidates: list,
        tp_engine_obj,
        current_spread: float,
        iteration: int = 0
    ) -> Optional[TradeRecord]:
        """
        Attempt to open a trade. Runs 10-gate validation first.
        Returns the TradeRecord on success, or a rejected record on failure.
        """
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        zone    = zone_engine.primary_zone

        if zone is None:
            return None

        # Compute entry, SL prices
        half_spread = current_spread / 2.0   # limit orders cost half-spread
        slippage    = self._p("SLIPPAGE", 0.05)
        sl_buffer   = self._p("SL_ATR_BUFFER", 0.50) * atr_val
        sl_min_abs  = self._p("SL_MIN_DISTANCE_ABS", 1.00)

        if direction == "LONG":
            # Limit buy at zone top (pullback into zone).
            # Cost = half bid-ask spread + slippage
            entry_price = zone.top + half_spread + slippage
            # SL: below zone bottom by an ATR buffer, minimum 1.0 × ATR_BUFFER
            sl_price    = zone.bottom - sl_buffer
            # Enforce minimum SL distance so noise can't stop us instantly
            actual_dist = entry_price - sl_price
            if actual_dist < sl_min_abs:
                sl_price = entry_price - sl_min_abs
        else:
            # Limit sell at zone bottom (pullback into zone from below)
            entry_price = zone.bottom - half_spread - slippage
            sl_price    = zone.top + sl_buffer
            actual_dist = sl_price - entry_price
            if actual_dist < sl_min_abs:
                sl_price = entry_price + sl_min_abs

        sl_dist = abs(entry_price - sl_price)

        # TP candidates
        tp1_dynamic, tp2_dynamic = tp_engine_obj.select_tp1_tp2(tp_candidates)

        # TP1_FIXED_RR: when > 0, place TP1 at a fixed R multiple from entry
        # and use the best dynamic candidate as TP2 (high hit-rate partial close strategy)
        tp1_fixed_rr = self._p("TP1_FIXED_RR", 0)
        if self._p("DUAL_TP_ENABLED", False) and tp1_fixed_rr and tp1_fixed_rr > 0 and tp1_dynamic:
            from dataclasses import replace
            # Build a synthetic fixed TP1 at exactly TP1_FIXED_RR × SL distance from entry
            if direction == "LONG":
                fixed_tp1_price = entry_price + tp1_fixed_rr * sl_dist
            else:
                fixed_tp1_price = entry_price - tp1_fixed_rr * sl_dist
            # Create fixed TP1 candidate (same score as best dynamic, but fixed price)
            from tp_engine import TPCandidate
            tp1_obj = TPCandidate(
                price=round(fixed_tp1_price, 2),
                score=tp1_dynamic.score,
                rr=tp1_fixed_rr,
                target_type=f"FIXED_{tp1_fixed_rr}R",
            )
            # Use original best dynamic target as TP2
            tp1  = tp1_obj
            tp2  = tp1_dynamic   # dynamic level becomes TP2
        else:
            tp1, tp2 = tp1_dynamic, tp2_dynamic

        best_tp_score = tp1.score if tp1 else 0.0
        best_rr       = tp1.rr   if tp1 else 0.0

        # 10-gate validation
        rejection = validate_setup(
            direction=direction,
            i=i,
            df=df,
            atr_val=atr_val,
            htf_trend=htf_trend,
            range_state=range_state,
            liquidity_engine=liquidity_engine,
            breakout_event=breakout_event,
            zone_engine=zone_engine,
            zone_reaction=zone_reaction,
            best_tp_score=best_tp_score,
            best_rr=best_rr,
            sl_price=sl_price,
            entry_price=entry_price,
            current_spread=current_spread,
            cfg=self.cfg
        )

        self._setup_counter += 1
        setup_id = f"SETUP_{self._setup_counter:05d}"
        ts = df.index[i]
        session = self._get_session(ts)

        # Freeze sweep info from the breakout event, not mutable live liquidity state.
        sweep_type = breakout_event.sweep_type if breakout_event else ""
        sweep_bar = getattr(breakout_event, "sweep_bar", -1) if breakout_event else -1
        sweep_wick_atr = 0.0
        sweep_wick_abs = 0.0
        for ev in reversed(liquidity_engine.sweep_history):
            if ev.sweep_type == sweep_type and ev.bar == sweep_bar:
                sweep_wick_atr = ev.wick_extension_atr
                sweep_wick_abs = ev.wick_extension_abs
                break

        rec = TradeRecord(
            setup_id=setup_id,
            iteration=iteration,
            symbol=self._p("SYMBOL", "XAU/USD"),
            timeframe=self._p("EXECUTION_TF", "5min"),
            direction=direction,

            range_high=range_state.range_high,
            range_low=range_state.range_low,
            range_quality_score=range_state.quality_score,
            range_bars=range_state.range_end_bar - range_state.range_start_bar,
            range_touches_high=range_state.touch_count_high,
            range_touches_low=range_state.touch_count_low,

            atr_at_setup=round(atr_val, 4),
            range_height_atr=round(range_state.height_atr, 3),

            ssl_bsl_sweep_type=sweep_type,
            sweep_bar=sweep_bar,
            sweep_wick_size_atr=round(sweep_wick_atr, 3),
            sweep_wick_size_abs=round(sweep_wick_abs, 3),

            structure_break_type=breakout_event.structure_type if breakout_event else "",
            breakout_bar=breakout_event.breakout_bar if breakout_event else -1,
            breakout_direction=breakout_event.direction if breakout_event else "",
            breakout_body_atr=round(breakout_event.breakout_body_atr, 3) if breakout_event else 0.0,
            breakout_quality=breakout_event.quality if breakout_event else "",

            htf_trend=htf_trend,

            retest_zone_type=zone.zone_type if zone else "",
            retest_zone_top=round(zone.top, 2) if zone else 0.0,
            retest_zone_bottom=round(zone.bottom, 2) if zone else 0.0,

            entry_price=round(entry_price, 2),
            sl_price=round(sl_price, 2),
            original_sl_price=round(sl_price, 2),  # frozen at open for R-multiple calc
            tp1_price=round(tp1.price, 2) if tp1 else 0.0,
            tp2_price=round(tp2.price, 2) if tp2 else 0.0,

            tp_score=best_tp_score,
            rr_tp1=best_rr,
            rr_tp2=tp2.rr if tp2 else 0.0,

            rejection_reason=rejection,
            entry_bar=i if not rejection else -1,
            session=session,
            timestamp=str(ts),
        )

        if rejection:
            self.rejected_setups.append(rec)
            return None

        self._active_trade = rec
        return rec

    def update_active_trade(self, i: int, df: pd.DataFrame) -> Optional[TradeRecord]:
        """
        Bar-by-bar simulation for the active trade.
        Returns completed TradeRecord when trade closes, else None.
        """
        if self._active_trade is None:
            return None

        rec  = self._active_trade
        row  = df.iloc[i]
        high = float(row["high"])
        low  = float(row["low"])
        close_price = float(row["close"])

        slippage  = self._p("SLIPPAGE", 0.05)
        timeout   = self._p("TIMEOUT_BARS", 72)
        same_bar  = self._p("SAME_BAR_RESOLUTION", "SL")
        dual_tp   = self._p("DUAL_TP_ENABLED", False)
        be_after_tp1 = self._p("BREAKEVEN_AFTER_TP1", True)

        entry = rec.entry_price
        sl    = rec.sl_price
        tp1   = rec.tp1_price
        tp2   = rec.tp2_price if rec.tp2_price > 0 else None

        bars_held = i - rec.entry_bar

        # MAE / MFE tracking
        if rec.direction == "LONG":
            adverse   = entry - low
            favorable = high - entry
        else:
            adverse   = high - entry
            favorable = entry - low

        rec.mae_abs = max(rec.mae_abs, adverse)
        rec.mfe_abs = max(rec.mfe_abs, favorable)

        # --- TP1 partial close handling ---
        if dual_tp and not rec.tp1_hit and tp1 > 0:
            tp1_hit_this_bar = (rec.direction == "LONG" and high >= tp1) or \
                               (rec.direction == "SHORT" and low <= tp1)
            if tp1_hit_this_bar:
                rec.tp1_hit = True
                rec.partial_exit_price = tp1
                if be_after_tp1:
                    # Move SL to breakeven
                    rec.sl_price = entry
                    sl = entry

        # --- Check SL and TP hits ---
        sl_hit = (rec.direction == "LONG"  and low  <= sl) or \
                 (rec.direction == "SHORT" and high >= sl)
        tp_price = tp2 if (dual_tp and rec.tp1_hit and tp2) else tp1
        tp_hit = (tp_price > 0) and (
            (rec.direction == "LONG"  and high >= tp_price) or
            (rec.direction == "SHORT" and low  <= tp_price)
        )

        # --- Same-bar resolution ---
        if sl_hit and tp_hit:
            if same_bar == "SL":
                sl_hit, tp_hit = True, False
            else:
                sl_hit, tp_hit = False, True

        # --- Close trade ---
        if sl_hit:
            exit_price  = sl - slippage if rec.direction == "LONG" else sl + slippage
            exit_reason = "SL"
        elif tp_hit:
            exit_price  = tp_price  # limit order, no extra slippage
            exit_reason = "TP2" if (dual_tp and rec.tp1_hit) else "TP1"
        elif bars_held >= timeout:
            exit_price  = close_price
            exit_reason = "TIMEOUT"
        else:
            return None  # trade still open

        rec.exit_bar   = i
        rec.exit_price = round(exit_price, 2)
        rec.exit_reason = exit_reason
        rec.bars_held   = bars_held

        # Always use original_sl_price for R calculation — sl_price may have moved to BE
        orig_sl   = rec.original_sl_price
        orig_dist = abs(entry - orig_sl)

        if orig_dist > 0:
            if rec.direction == "LONG":
                r_raw = (exit_price - entry) / orig_dist
            else:
                r_raw = (entry - exit_price) / orig_dist
        else:
            r_raw = 0.0

        # Dual TP: blend partial-close at TP1 + remainder at TP2 (or BE exit)
        if dual_tp and rec.tp1_hit and orig_dist > 0:
            ratio1 = self._p("DUAL_TP_RATIO_1", 0.50)
            if exit_reason == "SL":
                # Hit SL from breakeven → remainder closed at entry (0R on remainder)
                if rec.direction == "LONG":
                    r_tp1 = (tp1 - entry) / orig_dist * ratio1
                else:
                    r_tp1 = (entry - tp1) / orig_dist * ratio1
                r_raw = r_tp1 + 0.0 * (1 - ratio1)  # remainder at 0R (breakeven SL)
            elif exit_reason in ("TP2", "TP1"):
                if rec.direction == "LONG":
                    r_tp1 = (tp1 - entry) / orig_dist * ratio1
                    r_tp2 = (exit_price - entry) / orig_dist * (1 - ratio1)
                else:
                    r_tp1 = (entry - tp1) / orig_dist * ratio1
                    r_tp2 = (entry - exit_price) / orig_dist * (1 - ratio1)
                r_raw = r_tp1 + r_tp2
            elif exit_reason == "TIMEOUT":
                if rec.direction == "LONG":
                    r_tp1 = (tp1 - entry) / orig_dist * ratio1
                    r_rem = (exit_price - entry) / orig_dist * (1 - ratio1)
                else:
                    r_tp1 = (entry - tp1) / orig_dist * ratio1
                    r_rem = (entry - exit_price) / orig_dist * (1 - ratio1)
                r_raw = r_tp1 + r_rem

        rec.r_multiple = round(r_raw, 3)
        rec.mae_abs    = round(rec.mae_abs, 2)
        rec.mfe_abs    = round(rec.mfe_abs, 2)

        self.completed_trades.append(rec)
        self._active_trade = None
        return rec

    def _get_session(self, ts) -> str:
        hour = ts.hour
        sessions = self._p("TRADE_SESSIONS", {})
        for name, window in sessions.items():
            open_h  = int(window["open"].split(":")[0])
            close_h = int(window["close"].split(":")[0])
            if open_h <= hour < close_h:
                return name
        return "off_hours"
