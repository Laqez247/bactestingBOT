"""
trade_simulator.py — 10-Gate Validation Guard + Bar-by-Bar Trade Simulation.

PHASE 2 ADAPTIVE OVERRIDE FRAMEWORK:
  Four contextual overrides can unlock ONE soft gate when compensation
  factors meet the required threshold. Hard gates remain inviolable.

Hard Gates (NEVER overridable):
  - SL placement (structural invalidation point)
  - Spread filter (real execution cost)
  - TP1+BE structure (floor protection mechanic)
  - Dynamic TP engine (core edge source)
  - Lookahead bias (zero tolerance)

Soft Gates (contextually overridable — one at a time):
  Override 1 — MOMENTUM_OVERRIDE    : BOS → treated as MSS when displacement exceptional
  Override 2 — CONFLUENCE_OVERRIDE  : stale zone → extended when confluence very high
  Override 3 — SWEEP_MAGNITUDE_OVERRIDE : low range quality → accepted when sweep violent
  Override 4 — CONTEXT_OVERRIDE     : blocked sweep type → accepted with full confluence stack

Every override trade is tagged with override_type and compensation_score.
Override trades are tracked and reported separately from standard trades.
"""

import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import config


# ===========================================================================
# COMPENSATION SCORE ENGINE
# ===========================================================================

def compute_compensation_score(
    sweep_wick_atr:       float,   # sweep wick extension in ATR units
    breakout_body_atr:    float,   # breakout candle body in ATR units
    zone_type:            str,     # "OB" | "FVG" | "BB" | "SR"
    extra_confluence_types: list,  # extra overlapping level types beyond primary zone
    htf_swing_count:      int,     # confirmed swings in exec-TF structure_engine
    round_number_distance: float,  # absolute $ distance to nearest round-number level
    pdh_pdl_overlap:      bool,    # does the zone overlap with prev-day high/low?
    in_london_open:       bool,    # timestamp 07:00–09:30 UTC?
) -> Dict[str, float]:
    """
    Compute per-setup compensation score for override eligibility.
    Uses ONLY data available at bar T — no lookahead.

    Returns a dict with per-factor scores and 'total'.

    Section 3B scoring from prompt:
      Sweep depth        : 0.5x→+10, 0.75x→+20, 1.0x+→+35
      Displacement       : 1.0x→+10, 1.5x→+20, 2.0x+→+35
      Zone confluence    : +15 per extra overlapping level type
      HTF clarity        : 3-swing→+10, 5-swing→+20
      Round number       : within $1.50→+20, within $3.00→+10
      PDH/PDL alignment  : overlap→+15
      Session timing     : London open 07:00–09:30→+15
    """
    s: Dict[str, float] = {}

    # Factor 1: Sweep depth (wick_extension / ATR)
    if sweep_wick_atr >= 1.0:
        s["sweep_depth"] = 35.0
    elif sweep_wick_atr >= 0.75:
        s["sweep_depth"] = 20.0
    elif sweep_wick_atr >= 0.5:
        s["sweep_depth"] = 10.0
    else:
        s["sweep_depth"] = 0.0

    # Factor 2: Breakout displacement (body_size / ATR)
    if breakout_body_atr >= 2.0:
        s["displacement"] = 35.0
    elif breakout_body_atr >= 1.5:
        s["displacement"] = 20.0
    elif breakout_body_atr >= 1.0:
        s["displacement"] = 10.0
    else:
        s["displacement"] = 0.0

    # Factor 3: Zone confluence — extra overlapping level types beyond primary zone
    n_extra = len([t for t in (extra_confluence_types or []) if t])
    s["zone_confluence"] = min(n_extra, 3) * 15.0  # +15 per type, cap at 3 types (45 max)

    # Factor 4: HTF alignment clarity (exec-TF swing count as proxy)
    # ≥10 swings → clearly trending (treat as 5-swing), ≥5 → treat as 3-swing
    if htf_swing_count >= 10:
        s["htf_clarity"] = 20.0
    elif htf_swing_count >= 5:
        s["htf_clarity"] = 10.0
    else:
        s["htf_clarity"] = 0.0

    # Factor 5: Round number proximity ($50 increments for XAUUSD)
    if round_number_distance <= 1.5:
        s["round_number"] = 20.0
    elif round_number_distance <= 3.0:
        s["round_number"] = 10.0
    else:
        s["round_number"] = 0.0

    # Factor 6: PDH/PDL alignment
    s["pdh_pdl"] = 15.0 if pdh_pdl_overlap else 0.0

    # Factor 7: Session timing (London open window)
    s["session_timing"] = 15.0 if in_london_open else 0.0

    s["total"] = sum(v for k, v in s.items() if k != "total")
    return s


def _compute_confluence_context(
    zone_type: str,
    zone_top: float,
    zone_bottom: float,
    entry_price: float,
    tp_candidates: list,
    ts,
    cfg: dict = None,
) -> Dict[str, Any]:
    """
    Compute zone confluence context from data visible at entry bar T.
    Returns:
      extra_confluence_types : list of overlapping level-type labels
      pdh_pdl_overlap        : bool
      round_number_distance  : float ($)
      in_london_open         : bool (07:00-09:30 UTC)
    """
    _cfg = cfg or {}
    rn_increment = _cfg.get("ROUND_NUMBER_INCREMENT", getattr(config, "ROUND_NUMBER_INCREMENT", 50))

    zone_mid = (zone_top + zone_bottom) / 2.0
    extra_types = []

    # 1. Round number check — nearest multiple of rn_increment
    nearest_rn = round(zone_mid / rn_increment) * rn_increment
    rn_dist = abs(zone_mid - nearest_rn)

    if rn_dist <= 3.0:
        extra_types.append("ROUND_NUMBER")

    # 2. PDH/PDL overlap — check tp_candidates for PDH / PDL types near zone
    pdh_pdl_overlap = False
    for cand in (tp_candidates or []):
        cand_type = getattr(cand, "target_type", "")
        cand_price = getattr(cand, "price", 0.0)
        if cand_type in ("PDH", "PDL", "PREV_DAY_HIGH", "PREV_DAY_LOW"):
            # Overlap if candidate price is within the zone band ± 10%
            zone_range = max(zone_top - zone_bottom, 0.5)
            if abs(cand_price - zone_mid) <= zone_range * 1.5:
                pdh_pdl_overlap = True
                extra_types.append("PDH_PDL")
                break

    # 3. Multi-zone check — if zone_type itself is already a confluence marker
    #    (FVG + OB overlap at same price level = strong confluence)
    #    This is approximated: if zone_type is FVG, check if an OB candidate exists in tp_candidates
    if zone_type == "FVG":
        extra_types.append("FVG_DISPLACEMENT")

    # 4. Session extreme check
    for cand in (tp_candidates or []):
        cand_type = getattr(cand, "target_type", "")
        cand_price = getattr(cand, "price", 0.0)
        if cand_type in ("SESSION_HIGH", "SESSION_LOW", "EQH", "EQL"):
            zone_range = max(zone_top - zone_bottom, 0.5)
            if abs(cand_price - zone_mid) <= zone_range * 2.0:
                extra_types.append("SESSION_EXTREME")
                break

    # 5. London open check (07:00–09:30 UTC)
    hour = ts.hour if hasattr(ts, "hour") else 8
    minute = ts.minute if hasattr(ts, "minute") else 0
    in_london_open = (hour == 7) or (hour == 8) or (hour == 9 and minute <= 30)

    return {
        "extra_confluence_types": list(set(extra_types)),
        "pdh_pdl_overlap":        pdh_pdl_overlap,
        "round_number_distance":  rn_dist,
        "in_london_open":         in_london_open,
    }


# ===========================================================================
# TRADE RECORD
# ===========================================================================

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
    sl_price: float = 0.0
    original_sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0

    # TP scoring
    tp_score: float = 0.0
    rr_tp1: float = 0.0
    rr_tp2: float = 0.0

    # Validation
    rejection_reason: str = ""

    # Execution
    entry_bar: int = -1
    exit_bar: int = -1
    exit_reason: str = ""
    exit_price: float = 0.0

    # Performance
    r_multiple: float = 0.0
    mae_abs: float = 0.0
    mfe_abs: float = 0.0
    bars_held: int = 0

    # Context
    session: str = ""
    timestamp: str = ""

    # Dual TP tracking
    tp1_hit: bool = False
    partial_exit_price: float = 0.0

    # Phase 2 adaptive override fields (legacy — kept for backward compat)
    override_type: str = "NONE"          # NONE | MOMENTUM_OVERRIDE | CONFLUENCE_OVERRIDE |
                                          #        SWEEP_MAGNITUDE_OVERRIDE | CONTEXT_OVERRIDE
    compensation_score: float = 0.0      # total compensation score at setup evaluation
    compensation_breakdown: str = ""     # JSON-encoded per-factor scores (for audit)

    # Hybrid Engine Phase 3 — modification fields
    modification_type: str = "NONE"      # NONE | WEAK_RANGE_STRICT_ENTRY | BOS_MACRO_DISPLACEMENT |
                                          #        STALE_RETEST_CONFLUENCE | BLOCKED_SWEEP_ELEVATED_CONTEXT |
                                          #        LONG_PDL_RECOVERY
    market_regime: str = ""              # TRENDING_STRONG | TRENDING_MODERATE | RANGING | HIGH_VOLATILITY
    regime_confidence: float = 0.0       # 0.0–1.0 regime classifier confidence
    sl_buffer_mod: float = 0.0           # any SL buffer increase applied by a modification

    # Complementary strategy fields
    strategy_type: str = "EDGE2"         # EDGE2 | CS1 | CS2 | CS3 | CS4
    cs_setup_context: str = ""           # human-readable description of CS signal


# ===========================================================================
# GATE VALIDATION — with Phase 2 override support
# ===========================================================================

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
    cfg: dict = None,
    comp_scores: Dict[str, float] = None,
    bars_since_breakout: int = 0,
    # Hybrid Engine Phase 3 — regime info
    regime_info: Dict[str, Any] = None,
    sweep_wick_atr: float = 0.0,
    breakout_body_atr: float = 0.0,
    htf_swing_count: int = 0,
) -> tuple:
    """
    Hybrid Engine gate validation with 5-modification layer.

    Returns (rejection_reason: str, override_type: str, modification_type: str, sl_buffer_mod: float).
      rejection_reason  = "" → all gates passed.
      override_type     = legacy Phase 2 name (kept for metrics compatibility).
      modification_type = Phase 3 modification name (NONE if clean pass).
      sl_buffer_mod     = extra SL buffer ATR fraction (MOD2 adds 0.10).

    Hard gates: ALWAYS enforced regardless of regime or modification.
    Soft gates: ONE modification allowed per setup (two failures = reject).
    HIGH_VOLATILITY regime: zero modifications — hard gates only.
    """
    _p = lambda k, default=None: (cfg or {}).get(k, getattr(config, k, default))

    regime      = (regime_info or {}).get("regime", "RANGING")
    mod_allowed = _get_allowed_mods(regime)

    modification_used = False
    modification_type = "NONE"
    override_type     = "NONE"   # legacy compat
    sl_buffer_mod     = 0.0

    comp            = comp_scores or {}

    # -----------------------------------------------------------------------
    # GATE 1: HTF trend alignment  [HARD — no modification]
    # -----------------------------------------------------------------------
    if _p("HIGHER_TF_FILTER_ON", True):
        if direction == "LONG" and htf_trend == "DOWNTREND":
            return "HTF_NOT_BULLISH", "NONE", "NONE", 0.0
        if direction == "SHORT" and htf_trend == "UPTREND":
            return "HTF_NOT_BEARISH", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 2: Valid compression range  [SOFT — MODIFICATION_1]
    # -----------------------------------------------------------------------
    if not range_state.valid:
        return "NO_VALID_RANGE", "NONE", "NONE", 0.0

    range_quality = getattr(range_state, "quality_score", 0.0)
    rmq_threshold = _p("RANGE_MIN_QUALITY", 40)

    if range_quality < rmq_threshold:
        mod1_min = _p("MOD1_RANGE_QUALITY_MIN", 25)
        mod1_wick = _p("MOD1_SWEEP_WICK_ATR_MIN", 0.50)
        mod1_zones = _p("MOD1_REQUIRED_ZONE_TYPES", ["OB", "FVG"])
        zone_type  = getattr(zone_engine.primary_zone, "zone_type", "") if zone_engine.primary_zone else ""
        touches_ok = (getattr(range_state, "touch_count_high", 0) >= 1 and
                      getattr(range_state, "touch_count_low",  0) >= 1)

        can_mod = (
            not modification_used
            and "MODIFICATION_1" in mod_allowed
            and range_quality >= mod1_min
            and sweep_wick_atr >= mod1_wick
            and zone_type in mod1_zones
            and touches_ok
        )
        if can_mod:
            modification_used = True
            modification_type = "WEAK_RANGE_STRICT_ENTRY"
            override_type     = "SWEEP_MAGNITUDE_OVERRIDE"   # legacy compat
        else:
            return "RANGE_QUALITY_TOO_LOW", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 3: Correct liquidity sweep direction  [HARD direction check]
    # -----------------------------------------------------------------------
    if direction == "LONG":
        if breakout_event is None or not str(getattr(breakout_event, "sweep_type", "")).startswith("SSL_"):
            return "NO_SSL_SWEEP", "NONE", "NONE", 0.0
    else:
        if breakout_event is None or not str(getattr(breakout_event, "sweep_type", "")).startswith("BSL_"):
            return "NO_BSL_SWEEP", "NONE", "NONE", 0.0

    sweep_type = getattr(breakout_event, "sweep_type", "") if breakout_event else ""

    # Gate 3b/3c: Blocked sweep types  [SOFT — MODIFICATION_4 for SHORT; MODIFICATION_4/5 for LONG]
    if direction == "SHORT":
        blocked = _p("SHORT_BLOCKED_SWEEPS", [])
        never_unlock_short = _p("MOD4_NEVER_UNLOCK_SHORT", [])
        if blocked and sweep_type in blocked:
            # Zero-WR types: never unlockable (e.g. BSL_PDH: 0% WR, -1.025R)
            if sweep_type in never_unlock_short:
                return f"SHORT_BLOCKED_SWEEP_{sweep_type}", "NONE", "NONE", 0.0
            can_mod = _check_mod4(
                modification_used, mod_allowed, direction, sweep_type,
                sweep_wick_atr, breakout_body_atr, htf_swing_count,
                zone_engine, _p
            )
            if can_mod:
                modification_used = True
                modification_type = "BLOCKED_SWEEP_ELEVATED_CONTEXT"
                override_type     = "CONTEXT_OVERRIDE"
            else:
                return f"SHORT_BLOCKED_SWEEP_{sweep_type}", "NONE", "NONE", 0.0

    if direction == "LONG":
        blocked = _p("LONG_BLOCKED_SWEEPS", [])
        never_unlock = _p("MOD4_NEVER_UNLOCK_LONG", ["SSL_SESSION_LOW", "SSL_RANGE_LOW"])

        if blocked and sweep_type in blocked:
            # Zero-WR types: never unlockable
            if sweep_type in never_unlock:
                return f"LONG_BLOCKED_SWEEP_{sweep_type}", "NONE", "NONE", 0.0

            can_mod = _check_mod4(
                modification_used, mod_allowed, direction, sweep_type,
                sweep_wick_atr, breakout_body_atr, htf_swing_count,
                zone_engine, _p
            )
            if can_mod:
                modification_used = True
                modification_type = "BLOCKED_SWEEP_ELEVATED_CONTEXT"
                override_type     = "CONTEXT_OVERRIDE"
            else:
                return f"LONG_BLOCKED_SWEEP_{sweep_type}", "NONE", "NONE", 0.0

        # Gate 3d: MODIFICATION_5 — LONG_PDL_RECOVERY (SSL_PDL in trending strong)
        elif (not modification_used
              and sweep_type == _p("MOD5_SWEEP_TYPE", "SSL_PDL")
              and "MODIFICATION_5" in mod_allowed):
            mod5_wick  = _p("MOD5_SWEEP_WICK_ATR_MIN", 0.40)
            mod5_zones = _p("MOD5_REQUIRED_ZONE_TYPES", ["OB"])
            zone_type  = getattr(zone_engine.primary_zone, "zone_type", "") if zone_engine.primary_zone else ""
            entry_mode = _p("ENTRY_MODE", "MODE_CLOSE_OUTSIDE")

            if (sweep_wick_atr >= mod5_wick
                    and zone_type in mod5_zones
                    and entry_mode == _p("MOD5_ENTRY_MODE", "MODE_WICK_REJECTION")):
                modification_used = True
                modification_type = "LONG_PDL_RECOVERY"
                override_type     = "CONTEXT_OVERRIDE"

    # -----------------------------------------------------------------------
    # GATE 4: Structure break confirmed  [HARD direction check]
    # -----------------------------------------------------------------------
    if breakout_event is None:
        return ("NO_BULLISH_MSS_BOS" if direction == "LONG" else "NO_BEARISH_MSS_BOS"), "NONE", "NONE", 0.0
    if direction == "LONG" and breakout_event.direction != "LONG":
        return "NO_BULLISH_MSS_BOS", "NONE", "NONE", 0.0
    if direction == "SHORT" and breakout_event.direction != "SHORT":
        return "NO_BEARISH_MSS_BOS", "NONE", "NONE", 0.0

    # Gate 4b: MSS requirement  [SOFT — MODIFICATION_2]
    if direction == "LONG":
        mss_needed = _p("MSS_REQUIRED_LONG", _p("MSS_REQUIRED", False))
    else:
        mss_needed = _p("MSS_REQUIRED_SHORT", _p("MSS_REQUIRED", False))

    if mss_needed:
        needed = "MSS_BULLISH" if direction == "LONG" else "MSS_BEARISH"
        if getattr(breakout_event, "structure_type", "") != needed:
            mod2_body  = _p("MOD2_BODY_ATR_MIN", 1.5)
            mod2_swings= _p("MOD2_HTF_SWING_COUNT_MIN", 5)
            mod2_zones = _p("MOD2_REQUIRED_ZONE_TYPES", ["OB", "BB"])
            zone_type  = getattr(zone_engine.primary_zone, "zone_type", "") if zone_engine.primary_zone else ""

            can_mod = (
                not modification_used
                and "MODIFICATION_2" in mod_allowed
                and breakout_body_atr >= mod2_body
                and htf_swing_count >= mod2_swings
                and zone_type in mod2_zones
            )
            if can_mod:
                modification_used = True
                modification_type = "BOS_MACRO_DISPLACEMENT"
                override_type     = "MOMENTUM_OVERRIDE"
                sl_buffer_mod     = _p("MOD2_SL_BUFFER_INCREASE", 0.10)
            else:
                return "MSS_REQUIRED_NOT_MET", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 5: Valid retest zone  [SOFT — MODIFICATION_3 for stale retests]
    # -----------------------------------------------------------------------
    if not zone_engine.has_valid_zone:
        return "NO_RETEST_ZONE", "NONE", "NONE", 0.0

    base_timeout   = _p("RETEST_TIMEOUT_BARS", 125)
    mod3_max_bars  = _p("MOD3_MAX_BARS", 175)

    if bars_since_breakout > base_timeout:
        if bars_since_breakout <= mod3_max_bars:
            # MODIFICATION_3: stale retest allowed with ≥2 confluence types
            min_conf = _p("MOD3_MIN_CONFLUENCE_TYPES", 2)
            conf_types = comp.get("_extra_confluence_count", 0)
            # Fall back to zone_confluence pts (15 per type) if count not passed
            if conf_types == 0:
                conf_types = int(comp.get("zone_confluence", 0.0) / 15.0)

            can_mod = (
                not modification_used
                and "MODIFICATION_3" in mod_allowed
                and conf_types >= min_conf
            )
            if can_mod:
                modification_used = True
                modification_type = "STALE_RETEST_CONFLUENCE"
                override_type     = "CONFLUENCE_OVERRIDE"
            else:
                return "RETEST_TIMEOUT_EXCEEDED", "NONE", "NONE", 0.0
        else:
            return "RETEST_TIMEOUT_EXCEEDED", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 6: Zone reaction confirmed  [HARD — no modification]
    # -----------------------------------------------------------------------
    if not zone_reaction:
        return "NO_ZONE_REACTION", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 7: Valid dynamic TP target  [HARD]
    # -----------------------------------------------------------------------
    tp_min_score = _p("TP_MIN_SCORE", 20)
    if best_tp_score < tp_min_score:
        return "NO_VALID_TARGET", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 8: RR check  [HARD]
    # -----------------------------------------------------------------------
    if best_rr < _p("TP_MIN_RR", 1.0):
        return "RR_TOO_LOW", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 9: SL distance  [HARD]
    # -----------------------------------------------------------------------
    sl_dist = abs(entry_price - sl_price)
    if sl_dist > _p("SL_MAX_DISTANCE_ATR", 2.5) * atr_val:
        return "SL_TOO_WIDE", "NONE", "NONE", 0.0

    # -----------------------------------------------------------------------
    # GATE 10: Spread filter  [HARD]
    # -----------------------------------------------------------------------
    if current_spread > _p("SPREAD_FILTER_MAX", 2.00):
        return "SPREAD_TOO_HIGH", "NONE", "NONE", 0.0

    return "", override_type, modification_type, sl_buffer_mod


# ---------------------------------------------------------------------------
# MODIFICATION HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def _get_allowed_mods(regime: str) -> list:
    """Return list of allowed modification ids for the given regime."""
    from regime_detector import regime_allows_modifications
    return regime_allows_modifications(regime)


def _check_mod4(modification_used, mod_allowed, direction, sweep_type,
                sweep_wick_atr, breakout_body_atr, htf_swing_count,
                zone_engine, _p) -> bool:
    """
    Check whether MODIFICATION_4 (BLOCKED_SWEEP_ELEVATED_CONTEXT) can fire.
    Returns True if the modification is approved.
    """
    if modification_used:
        return False
    if "MODIFICATION_4" not in mod_allowed:
        return False

    mod4_wick   = _p("MOD4_SWEEP_WICK_ATR_MIN", 0.75)
    mod4_body   = _p("MOD4_BODY_ATR_MIN", 1.2)
    mod4_zones  = _p("MOD4_REQUIRED_ZONE_TYPES", ["OB"])
    mod4_swings = _p("MOD4_HTF_SWING_COUNT_MIN", 5)
    zone_type   = getattr(zone_engine.primary_zone, "zone_type", "") if zone_engine.primary_zone else ""

    return (
        sweep_wick_atr   >= mod4_wick
        and breakout_body_atr >= mod4_body
        and zone_type        in mod4_zones
        and htf_swing_count  >= mod4_swings
    )


# ===========================================================================
# TRADE SIMULATOR
# ===========================================================================

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
        iteration: int = 0,
        # Phase 2: additional context for compensation scoring
        htf_swing_count: int = 0,
        bars_since_breakout: int = 0,
        # Hybrid Engine Phase 3: regime info from regime_detector
        regime_info: Dict[str, Any] = None,
    ) -> Optional[TradeRecord]:
        """
        Attempt to open a trade. Runs 10-gate validation + override checks.
        Returns the TradeRecord on success, or None on rejection.
        """
        atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 1.0
        zone    = zone_engine.primary_zone

        if zone is None:
            return None

        half_spread = current_spread / 2.0
        slippage    = self._p("SLIPPAGE", 0.05)
        sl_buffer   = self._p("SL_ATR_BUFFER", 0.50) * atr_val
        sl_min_abs  = self._p("SL_MIN_DISTANCE_ABS", 1.00)

        if direction == "LONG":
            entry_price = zone.top + half_spread + slippage
            sl_price    = zone.bottom - sl_buffer
            actual_dist = entry_price - sl_price
            if actual_dist < sl_min_abs:
                sl_price = entry_price - sl_min_abs
        else:
            entry_price = zone.bottom - half_spread - slippage
            sl_price    = zone.top + sl_buffer
            actual_dist = sl_price - entry_price
            if actual_dist < sl_min_abs:
                sl_price = entry_price + sl_min_abs

        sl_dist = abs(entry_price - sl_price)

        tp1_dynamic, tp2_dynamic = tp_engine_obj.select_tp1_tp2(tp_candidates)

        tp1_fixed_rr = self._p("TP1_FIXED_RR", 0)
        if self._p("DUAL_TP_ENABLED", False) and tp1_fixed_rr and tp1_fixed_rr > 0 and tp1_dynamic:
            if direction == "LONG":
                fixed_tp1_price = entry_price + tp1_fixed_rr * sl_dist
            else:
                fixed_tp1_price = entry_price - tp1_fixed_rr * sl_dist
            from tp_engine import TPCandidate
            tp1_obj = TPCandidate(
                price=round(fixed_tp1_price, 2),
                score=tp1_dynamic.score,
                rr=tp1_fixed_rr,
                target_type=f"FIXED_{tp1_fixed_rr}R",
            )
            tp1 = tp1_obj
            tp2 = tp1_dynamic
        else:
            tp1, tp2 = tp1_dynamic, tp2_dynamic

        best_tp_score = tp1.score if tp1 else 0.0
        best_rr       = tp1.rr   if tp1 else 0.0

        # ----------------------------------------------------------------
        # Phase 2: Compute compensation context (bar-T only, no lookahead)
        # ----------------------------------------------------------------
        ts = df.index[i]
        sweep_wick_atr = 0.0
        sweep_wick_abs = 0.0
        sweep_type_raw = getattr(breakout_event, "sweep_type", "") if breakout_event else ""
        sweep_bar_raw  = getattr(breakout_event, "sweep_bar", -1)  if breakout_event else -1
        for ev in reversed(liquidity_engine.sweep_history):
            if ev.sweep_type == sweep_type_raw and ev.bar == sweep_bar_raw:
                sweep_wick_atr = ev.wick_extension_atr
                sweep_wick_abs = ev.wick_extension_abs
                break

        breakout_body_atr = getattr(breakout_event, "breakout_body_atr", 0.0) if breakout_event else 0.0

        conf_ctx = _compute_confluence_context(
            zone_type=zone.zone_type if zone else "",
            zone_top=zone.top if zone else entry_price,
            zone_bottom=zone.bottom if zone else entry_price,
            entry_price=entry_price,
            tp_candidates=tp_candidates,
            ts=ts,
            cfg=self.cfg,
        )

        comp_scores = compute_compensation_score(
            sweep_wick_atr=sweep_wick_atr,
            breakout_body_atr=breakout_body_atr,
            zone_type=zone.zone_type if zone else "",
            extra_confluence_types=conf_ctx["extra_confluence_types"],
            htf_swing_count=htf_swing_count,
            round_number_distance=conf_ctx["round_number_distance"],
            pdh_pdl_overlap=conf_ctx["pdh_pdl_overlap"],
            in_london_open=conf_ctx["in_london_open"],
        )

        # ----------------------------------------------------------------
        # Run gate validation with Hybrid Engine modification layer
        # ----------------------------------------------------------------
        rejection, override_type, modification_type, sl_buffer_mod = validate_setup(
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
            cfg=self.cfg,
            comp_scores=comp_scores,
            bars_since_breakout=bars_since_breakout,
            regime_info=regime_info,
            sweep_wick_atr=sweep_wick_atr,
            breakout_body_atr=breakout_body_atr,
            htf_swing_count=htf_swing_count,
        )

        # Apply MOD2 SL buffer increase if applicable
        if sl_buffer_mod > 0 and not rejection:
            extra = sl_buffer_mod * atr_val
            if direction == "LONG":
                sl_price -= extra
            else:
                sl_price += extra

        self._setup_counter += 1
        setup_id = f"SETUP_{self._setup_counter:05d}"
        session = self._get_session(ts)

        # Encode compensation breakdown for audit (compact)
        import json
        comp_detail = {k: round(v, 1) for k, v in comp_scores.items()}
        comp_str = json.dumps(comp_detail, separators=(",", ":"))

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

            ssl_bsl_sweep_type=sweep_type_raw,
            sweep_bar=sweep_bar_raw,
            sweep_wick_size_atr=round(sweep_wick_atr, 3),
            sweep_wick_size_abs=round(sweep_wick_abs, 3),

            structure_break_type=(
                "BOS_MACRO_DISPLACEMENT" if modification_type == "BOS_MACRO_DISPLACEMENT"
                else ("BOS_MOMENTUM_OVERRIDE" if override_type == "MOMENTUM_OVERRIDE"
                      else (breakout_event.structure_type if breakout_event else ""))
            ),
            breakout_bar=breakout_event.breakout_bar if breakout_event else -1,
            breakout_direction=breakout_event.direction if breakout_event else "",
            breakout_body_atr=round(breakout_body_atr, 3),
            breakout_quality=breakout_event.quality if breakout_event else "",

            htf_trend=htf_trend,

            retest_zone_type=(
                "ZONE_STALE_RETEST" if modification_type == "STALE_RETEST_CONFLUENCE"
                else ("ZONE_CONFLUENCE_EXTENDED" if override_type == "CONFLUENCE_OVERRIDE"
                      else (zone.zone_type if zone else ""))
            ),
            retest_zone_top=round(zone.top, 2) if zone else 0.0,
            retest_zone_bottom=round(zone.bottom, 2) if zone else 0.0,

            entry_price=round(entry_price, 2),
            sl_price=round(sl_price, 2),
            original_sl_price=round(sl_price, 2),
            tp1_price=round(tp1.price, 2) if tp1 else 0.0,
            tp2_price=round(tp2.price, 2) if tp2 else 0.0,

            tp_score=best_tp_score,
            rr_tp1=best_rr,
            rr_tp2=tp2.rr if tp2 else 0.0,

            rejection_reason=rejection,
            entry_bar=i if not rejection else -1,
            session=session,
            timestamp=str(ts),

            override_type=override_type,
            compensation_score=round(comp_scores.get("total", 0.0), 1),
            compensation_breakdown=comp_str,

            # Hybrid Engine Phase 3 fields
            modification_type=modification_type,
            market_regime=(regime_info or {}).get("regime", ""),
            regime_confidence=round((regime_info or {}).get("regime_confidence", 0.0), 3),
            sl_buffer_mod=round(sl_buffer_mod, 4),
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

        if rec.direction == "LONG":
            adverse   = entry - low
            favorable = high - entry
        else:
            adverse   = high - entry
            favorable = entry - low

        rec.mae_abs = max(rec.mae_abs, adverse)
        rec.mfe_abs = max(rec.mfe_abs, favorable)

        orig_sl = rec.original_sl_price
        orig_sl_hit_this_bar = (rec.direction == "LONG"  and low  <= orig_sl) or \
                               (rec.direction == "SHORT" and high >= orig_sl)

        if dual_tp and not rec.tp1_hit and tp1 > 0:
            tp1_hit_this_bar = (rec.direction == "LONG" and high >= tp1) or \
                               (rec.direction == "SHORT" and low <= tp1)
            if tp1_hit_this_bar and not orig_sl_hit_this_bar:
                rec.tp1_hit = True
                rec.partial_exit_price = tp1
                if be_after_tp1:
                    rec.sl_price = entry
                    sl = entry

        sl_hit = (rec.direction == "LONG"  and low  <= sl) or \
                 (rec.direction == "SHORT" and high >= sl)
        tp_price = tp2 if (dual_tp and rec.tp1_hit and tp2) else tp1
        tp_hit = (tp_price > 0) and (
            (rec.direction == "LONG"  and high >= tp_price) or
            (rec.direction == "SHORT" and low  <= tp_price)
        )

        if sl_hit and tp_hit:
            if same_bar == "SL":
                sl_hit, tp_hit = True, False
            else:
                sl_hit, tp_hit = False, True

        if sl_hit:
            exit_price  = sl - slippage if rec.direction == "LONG" else sl + slippage
            exit_reason = "SL"
        elif tp_hit:
            exit_price  = tp_price
            exit_reason = "TP2" if (dual_tp and rec.tp1_hit) else "TP1"
        elif bars_held >= timeout:
            exit_price  = close_price
            exit_reason = "TIMEOUT"
        else:
            return None

        rec.exit_bar   = i
        rec.exit_price = round(exit_price, 2)
        rec.exit_reason = exit_reason
        rec.bars_held   = bars_held

        orig_sl   = rec.original_sl_price
        orig_dist = abs(entry - orig_sl)

        if orig_dist > 0:
            if rec.direction == "LONG":
                r_raw = (exit_price - entry) / orig_dist
            else:
                r_raw = (entry - exit_price) / orig_dist
        else:
            r_raw = 0.0

        if dual_tp and rec.tp1_hit and orig_dist > 0:
            ratio1 = self._p("DUAL_TP_RATIO_1", 0.50)
            if exit_reason == "SL":
                if rec.direction == "LONG":
                    r_tp1 = (tp1 - entry) / orig_dist * ratio1
                else:
                    r_tp1 = (entry - tp1) / orig_dist * ratio1
                r_raw = r_tp1 + 0.0 * (1 - ratio1)
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
