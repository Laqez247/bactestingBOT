"""
cs1_range_edge.py — CS1: Range Edge Scalp Strategy

Trades the range boundary that Edge 2 already detects, without requiring the
full sweep → MSS → OB retest sequence. Fires INSIDE the range waiting period
to monetise the range oscillation itself.

Gate sequence:
  CS1-1: Range valid + quality >= CS1_MIN_RANGE_QUALITY
  CS1-2: Price within CS1_BAND_ATR × ATR of range_high or range_low
  CS1-3: Wick rejection on touching candle
  CS1-4: Regime is NOT HIGH_VOLATILITY
  CS1-5: Session is LONDON or NY
  CS1-6: No trade currently open (checked by runner)
  CS1-7: Touching candle is NOT in the 3 bars after a sweep at this boundary

Adaptive modification (max ONE per setup):
  MOD-A: range_quality 45–54 → require wick >= CS1_MOD_WICK_ATR × ATR
  MOD-B: body not fully in lower/upper half but regime strongly opposes boundary →
         wait one extra confirmation candle before entry

No fixed RR. Dynamic TP via tp_engine.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import config
from strategies.cs_signal import CSSignal
from tp_engine import TPEngine


def _p(key, default=None):
    return getattr(config, key, default)


def _get_session(ts) -> str:
    """Return session name for a bar timestamp."""
    h = ts.hour
    london_open  = int(_p("TRADE_SESSIONS", {}).get("london", {}).get("open",  "07:00").split(":")[0])
    london_close = int(_p("TRADE_SESSIONS", {}).get("london", {}).get("close", "12:00").split(":")[0])
    ny_open  = 12
    ny_close = 17
    if london_open <= h < london_close:
        return "london"
    if ny_open <= h < ny_close:
        return "new_york"
    return "off_hours"


def _has_qualifying_prior_sweep(
    liquidity_engine, direction: str, range_high: float,
    atr_val: float, bar_i: int, cp
) -> tuple:
    """
    Gate CS1-SWEEP: For CS1 SHORT, check if a qualifying BSL sweep is
    active (not yet consumed by Edge 2) within the last CS1_SWEEP_LOOKBACK_BARS.

    Uses the current bsl_swept state from liquidity_engine — the SAME state
    that Edge 2 uses to validate sweep context, but accessed here for CS1's
    earlier-entry thesis: sweep happened, MSS/OB not yet confirmed, CS1 fades
    the boundary 4–20 bars after the sweep fires.

    If Edge 2 already opened a trade on this sweep and reset bsl_swept, then
    CS1 cannot fire on the same sweep. This is intentional — Edge 2 consumed
    the signal, CS1 looks for a different opportunity.

    Fallback: sweep_history is also checked for qualifying BSL sweeps near
    range_high when bsl_swept has been reset. This covers the case where a
    sweep occurred, Edge 2 ran a full trade, and the range persists — a new
    CS1 setup can form if the range_high is retested later.

    Returns (valid: bool, context_type: str | None)
      True, sweep_type  → qualifying sweep found
      False, None       → no qualifying sweep (may trigger adaptive rule)
      False, reason_str → hard block
    """
    if direction != "SHORT":
        return True, None  # LONG disabled — gate not applicable

    lookback = cp("CS1_SWEEP_LOOKBACK_BARS", 20)
    proximity = cp("CS1_SWEEP_PROXIMITY_ATR", 0.30) * atr_val
    qualifying_types = cp(
        "CS1_QUALIFYING_SWEEP_TYPES",
        ["BSL_SWING_HIGH", "BSL_EQUAL_HIGHS"]
    )

    # --- Primary: check current active BSL sweep state ---
    # This is the high-conviction path: sweep is active, Edge 2 hasn't used it yet
    bsl_swept = getattr(liquidity_engine, "bsl_swept", False)
    bsl_type  = getattr(liquidity_engine, "bsl_sweep_type", "")
    bsl_bar   = getattr(liquidity_engine, "bsl_sweep_bar", -1)

    if bsl_swept and bsl_bar >= 0:
        bars_since = bar_i - bsl_bar
        if 0 <= bars_since <= lookback:
            if bsl_type in qualifying_types:
                # Also verify proximity: sweep level must be near current range_high
                # Use the sweep_history level if available for the most precise check
                return True, bsl_type
            # If current BSL type is not qualifying (e.g. BSL_RANGE_HIGH), still block
            # the adaptation path to avoid low-quality setups near poor sweep types

    # --- Block SSL_EQUAL_LOWS context near range_high ---
    ssl_swept = getattr(liquidity_engine, "ssl_swept", False)
    ssl_type  = getattr(liquidity_engine, "ssl_sweep_type", "")
    ssl_bar   = getattr(liquidity_engine, "ssl_sweep_bar", -1)
    if ssl_swept and ssl_bar >= 0:
        bars_since = bar_i - ssl_bar
        if 0 <= bars_since <= lookback and ssl_type == "SSL_EQUAL_LOWS":
            return False, "SSL_EQUAL_LOWS_BLOCKED"

    # --- Fallback: sweep_history for recently-reset qualifying sweeps ---
    # Handles: Edge 2 took a trade, reset bsl_swept, range persists, new CS1 setup
    sweep_hist = getattr(liquidity_engine, "sweep_history", [])
    qualifying_found = False
    qualifying_type  = None

    for event in sweep_hist:
        bars_since = bar_i - event.bar
        if bars_since < 0 or bars_since > lookback:
            continue
        if abs(event.level - range_high) > proximity:
            continue
        if event.sweep_type == "SSL_EQUAL_LOWS":
            return False, "SSL_EQUAL_LOWS_BLOCKED"
        if event.sweep_type in qualifying_types:
            qualifying_found = True
            qualifying_type  = event.sweep_type
            # Keep scanning: a later SSL_EQUAL_LOWS could override (hard block wins)

    return qualifying_found, qualifying_type


def check(
    range_state,
    liquidity_engine,
    structure_engine,
    htf_aligned_series=None,
    regime_info: dict = None,
    df_exec: pd.DataFrame = None,
    atr_exec: pd.Series = None,
    bar_i: int = 0,
    cfg: dict = None,
    tp_engine: "TPEngine" = None,
) -> "CSSignal | None":
    """
    Check CS1 signal at bar_i. Returns CSSignal or None.
    All state inputs are already computed by the main loop (no re-computation).
    """
    _cfg = cfg or {}

    def cp(key, default=None):
        return _cfg.get(key, getattr(config, key, default))

    if not cp("CS1_ENABLED", True):
        return None

    # --- Gate CS1-4: Regime check (fast exit) ---
    regime = (regime_info or {}).get("regime", "RANGING")
    if regime == "HIGH_VOLATILITY":
        return None

    # --- Gate CS1-5: Session check ---
    # CS1_SESSION_FILTER overrides the shared CS_SESSION_FILTER for CS1 specifically.
    # Iter 4 analysis: London-only CS1 gives MaxDD -3.8R vs -11R for all-session.
    # London London-pure WR is 70.9% vs 66% for all-session (London+NY+off-hours).
    ts = df_exec.index[bar_i]
    session = _get_session(ts)
    cs1_sessions = cp("CS1_SESSION_FILTER", cp("CS_SESSION_FILTER", ["london", "new_york"]))
    if session not in cs1_sessions:
        return None

    # --- Gate CS1-1: Range validity ---
    if not range_state.valid:
        return None

    min_quality = cp("CS1_MIN_RANGE_QUALITY", 55)
    quality = range_state.quality_score
    mod_a_active = False

    if quality < min_quality:
        # Check MOD-A: range_quality 45–54 is acceptable with deeper wick
        mod_floor = cp("CS1_MOD_QUALITY_FLOOR", 45)
        if quality >= mod_floor:
            mod_a_active = True  # will enforce deeper wick at Gate CS1-3
        else:
            return None

    # --- Gate CS1-2: Price proximity to boundary ---
    atr_val = float(atr_exec.iloc[bar_i]) if not pd.isna(atr_exec.iloc[bar_i]) else 1.0
    band = cp("CS1_BAND_ATR", 0.20) * atr_val

    rh = range_state.range_high
    rl = range_state.range_low

    high_i  = float(df_exec["high"].iloc[bar_i])
    low_i   = float(df_exec["low"].iloc[bar_i])
    close_i = float(df_exec["close"].iloc[bar_i])
    open_i  = float(df_exec["open"].iloc[bar_i])

    near_high = high_i >= rh - band
    near_low  = low_i  <= rl + band

    if not near_high and not near_low:
        return None

    # Determine direction
    if near_high and near_low:
        # Inside a very narrow range — both boundaries touched, use candle position
        direction = "SHORT" if close_i > (rh + rl) / 2 else "LONG"
    elif near_high:
        direction = "SHORT"
    else:
        direction = "LONG"

    # --- Gate CS1-DIR: Direction filter + HTF alignment ---
    # CS1 LONG (fade range_low) is disabled: gold drops faster than it rises,
    # making range_low breakdowns more often genuine than fake-outs.
    # CS1 SHORT is kept but filtered against confirmed HTF uptrends.
    if not cp("CS1_ALLOW_LONG", False) and direction == "LONG":
        return None

    # HTF alignment: don't SHORT a range_high into a confirmed HTF UPTREND.
    # When 1H structure is already in UPTREND, the range_high is more likely
    # to break than to hold — we're fading institutional momentum.
    htf_trend = "RANGING"
    if htf_aligned_series is not None and bar_i < len(htf_aligned_series):
        _v = htf_aligned_series.iloc[bar_i]
        if _v and str(_v) != "nan":
            htf_trend = str(_v)
    if direction == "SHORT" and htf_trend in ("UPTREND", "BULLISH"):
        return None

    # --- Gate CS1-3: Wick rejection ---
    min_wick_body_ratio = cp("CS1_MIN_WICK_BODY_RATIO", 0.50)
    mod_wick_atr = cp("CS1_MOD_WICK_ATR", 0.35)

    if direction == "SHORT":
        # Candle touched range_high but close in lower half
        if high_i < rh - band:
            return None
        body_mid = (open_i + close_i) / 2.0  # midpoint of body prices
        wick_size = high_i - max(open_i, close_i)
        close_in_lower_half = close_i < body_mid  # body closed below body midpoint
        rejection_ok = close_i < (high_i + open_i) / 2  # close in lower half of candle range
    else:
        # LONG: candle touched range_low but close in upper half
        if low_i > rl + band:
            return None
        body_mid = (open_i + close_i) / 2.0
        wick_size = min(open_i, close_i) - low_i
        rejection_ok = close_i > (low_i + open_i) / 2  # close in upper half of candle range

    if not rejection_ok:
        return None

    # --- Hard wick gate: applies to ALL CS1 trades ---
    # A genuine range boundary rejection requires committed sellers/buyers.
    # Soft touches (tiny wicks) are continuation tests, not rejections.
    min_wick_atr_hard = cp("CS1_MIN_WICK_ATR", 0.35)
    if wick_size < min_wick_atr_hard * atr_val:
        return None

    # MOD-A: if range quality was weak, require even deeper wick
    mod_wick_atr = cp("CS1_MOD_WICK_ATR", 0.50)
    if mod_a_active:
        if wick_size < mod_wick_atr * atr_val:
            return None

    # MOD-B: if body not cleanly in correct half, check regime for 2nd-candle confirmation
    # (implementation note: MOD-B defers entry by 1 bar; here we mark it but still return
    # a signal — the runner will handle bar-ahead check if mod_b is flagged)
    mod_b_needed = False
    min_wick_body_ratio_check = cp("CS1_MIN_WICK_BODY_RATIO", 0.50)
    if direction == "SHORT":
        candle_range = high_i - low_i
        body_in_lower_half = close_i < (high_i - candle_range * 0.5) if candle_range > 0 else False
    else:
        candle_range = high_i - low_i
        body_in_lower_half = close_i > (low_i + candle_range * 0.5) if candle_range > 0 else False

    if not body_in_lower_half and not mod_a_active:
        # Check if regime strongly opposes boundary
        trend_dir = (regime_info or {}).get("trend_direction", "NONE")
        if direction == "SHORT" and trend_dir in ("BULLISH", "UPTREND"):
            mod_b_needed = True
        elif direction == "LONG" and trend_dir in ("BEARISH", "DOWNTREND"):
            mod_b_needed = True
        else:
            return None  # no adaptation available

    # --- Gate CS1-7: Not in the 3 bars after a sweep at this boundary ---
    sweep_guard_bars = 3
    if direction == "SHORT":
        # BSL sweep would be near range_high
        bsl_swept = getattr(liquidity_engine, "bsl_swept", False)
        bsl_bar   = getattr(liquidity_engine, "bsl_sweep_bar", -1)
        if bsl_swept and bsl_bar >= 0 and (bar_i - bsl_bar) <= sweep_guard_bars:
            return None  # Edge 2 should handle this
    else:
        ssl_swept = getattr(liquidity_engine, "ssl_swept", False)
        ssl_bar   = getattr(liquidity_engine, "ssl_sweep_bar", -1)
        if ssl_swept and ssl_bar >= 0 and (bar_i - ssl_bar) <= sweep_guard_bars:
            return None

    # --- Gate CS1-SWEEP: Prior qualifying BSL sweep near range_high (SHORT only) ---
    # Data: BSL_SWING_HIGH 78.4% WR +0.3858R, BSL_EQUAL_HIGHS 66.7% WR +0.4828R
    # vs no-sweep context: 130 trades 64.6% WR +0.1322R
    # Sweep validates institutional rejection of range_high before CS1 fades it.
    sweep_mod = None
    sweep_valid, found_sweep_type = _has_qualifying_prior_sweep(
        liquidity_engine, direction, rh, atr_val, bar_i, cp
    )
    if not sweep_valid:
        if found_sweep_type == "SSL_EQUAL_LOWS_BLOCKED":
            # Hard block: SSL_EQUAL_LOWS near range_high is a counter-signal
            return None
        # No qualifying sweep found — check adaptive rule:
        # Deep wick (>= 0.55x ATR) compensates for missing sweep context
        no_sweep_wick_atr = cp("CS1_NO_SWEEP_WICK_ATR", 0.55)
        if wick_size >= no_sweep_wick_atr * atr_val:
            sweep_mod = "NO_SWEEP_DEEP_WICK"
        else:
            return None  # CS1_NO_PRIOR_SWEEP — insufficient context

    # --- Gate CS1-MSS: Require recent MSS/BOS structural confirmation ---
    # Iter 8 data: zone-confirmed CS1 trades (61T): OB 80.8% WR, FVG 75%, BB 60%
    # Unknown zone CS1 (42T): 57.1% WR, -0.035R combined → -0.194R OOS (degradation signal)
    # Unknown zone = unknown structure = no recent MSS/BOS anchor.
    # mss_bearish_bar / bos_bearish_bar persist after flag consumption (same as CS2/CS4 gates).
    if cp("CS1_MSS_REQUIRED", True):
        mss_lookback = cp("CS1_MSS_LOOKBACK_BARS", 10)
        allow_bos    = cp("CS1_ALLOW_BOS_AS_MSS", True)

        if direction == "SHORT":
            mss_bar = getattr(structure_engine, "mss_bearish_bar", -1)
            bos_bar = getattr(structure_engine, "bos_bearish_bar", -1)
        else:
            mss_bar = getattr(structure_engine, "mss_bullish_bar", -1)
            bos_bar = getattr(structure_engine, "bos_bullish_bar", -1)

        mss_recent = mss_bar >= 0 and (bar_i - mss_bar) <= mss_lookback
        bos_recent = allow_bos and bos_bar >= 0 and (bar_i - bos_bar) <= mss_lookback

        if not mss_recent and not bos_recent:
            return None  # Range-edge bounce without structural anchor — reject

    # --- Compute SL ---
    sl_buffer = cp("CS1_SL_BUFFER_ATR", 0.25) * atr_val
    if direction == "SHORT":
        entry_price = close_i
        sl_price    = rh + sl_buffer
    else:
        entry_price = close_i
        sl_price    = rl - sl_buffer

    # --- Dynamic TP ---
    tp_candidates = []
    if tp_engine is not None:
        tp_min_score = cp("CS1_TP_MIN_SCORE", 25)
        tp_min_rr    = cp("CS1_TP_MIN_RR", 1.0)

        # Generate candidates using extended TP engine
        tp_candidates = tp_engine.generate_cs(
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            i=bar_i,
            df=df_exec,
            atr=atr_exec,
            range_state=range_state,
            structure_engine=structure_engine,
            liquidity_engine=liquidity_engine,
            candidate_types=[
                "range_midpoint",
                "range_opposite_boundary",
                "nearest_swing_in_direction",
                "round_number_in_direction",
                "session_high_low_in_direction",
            ],
            tp_min_score=tp_min_score,
            tp_min_rr=tp_min_rr,
        )

    if not tp_candidates:
        return None

    mod_type = "NONE"
    if mod_a_active:
        mod_type = "CS1_WEAK_RANGE_DEEP_WICK"
    elif mod_b_needed:
        mod_type = "CS1_BORDERLINE_BODY_TREND_CONFIRM"
    elif sweep_mod:
        mod_type = sweep_mod  # "NO_SWEEP_DEEP_WICK" adaptation

    return CSSignal(
        strategy="CS1",
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_candidates=tp_candidates,
        modification_type=mod_type,
        setup_context=(
            f"CS1 {'SHORT' if direction == 'SHORT' else 'LONG'} at range "
            f"{'high' if direction == 'SHORT' else 'low'} "
            f"{rh if direction == 'SHORT' else rl:.2f}, "
            f"quality={quality:.0f}, wick={wick_size:.2f}, "
            f"sweep_ctx={found_sweep_type or sweep_mod or 'NONE'}"
        ),
        bar_index=bar_i,
    )
