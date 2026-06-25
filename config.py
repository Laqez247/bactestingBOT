# ===========================================================
# EDGE 2 BACKTESTING ENGINE — ALL CONFIGURATION
# Zero hardcoded values exist in any other module.
# ===========================================================

# ===========================================================
# INSTRUMENT
# ===========================================================
SYMBOL = "XAU/USD"
EXECUTION_TF = "5min"          # Primary timeframe
STRUCTURE_TF = "1h"            # Higher-TF bias filter
DATA_SOURCE = "twelvedata"

# ===========================================================
# DATE RANGE
# ===========================================================
BACKTEST_START = "2024-01-01"
BACKTEST_END   = "2026-5-31"
WALK_FORWARD_SPLIT = 0.70      # 70% in-sample, 30% out-of-sample

# ===========================================================
# SESSIONS (UTC)
# ===========================================================
TRADE_SESSIONS = {
    "london": {"open": "07:00", "close": "12:00"},
    # "new_york" is EXCLUDED — at SL_ATR_BUFFER=0.3 NY trades are marginally breakeven (-0.007R)
    # London-only: 32 trades, 87.5% WR, +0.6495R expectancy (vs both sessions: 35 trades, 85.7% WR, +0.4936R)
    # Re-enable NY by uncommenting: "new_york": {"open": "12:00", "close": "17:00"},
}
HIGH_PRIORITY_WINDOWS = [
    {"name": "london_open", "start": "07:00", "end": "09:30"},
    {"name": "ny_open",     "start": "12:30", "end": "15:00"},
]

# ===========================================================
# SPREAD & SLIPPAGE (price units; 1 unit = $1 on XAUUSD)
# ===========================================================
DEFAULT_SPREAD         = 0.35   # $0.35 default spread
NEWS_SPREAD_MULTIPLIER = 4.0    # Spread multiplier during news window
SLIPPAGE               = 0.05   # $0.05 slippage per fill

# ===========================================================
# ATR SETTINGS
# ===========================================================
ATR_PERIOD = 14

# ===========================================================
# SWING DETECTION
# ===========================================================
PIVOT_LEFT  = 3    # Bars required to left of pivot for confirmation
PIVOT_RIGHT = 3    # Bars required to right of pivot for confirmation
# Range: left 2–5, right 2–5 for 5m. For 15m use 2–4.

# ===========================================================
# RANGE DETECTION
# ===========================================================
RANGE_MIN_BARS        = 8      # Minimum bars inside range
RANGE_MAX_BARS        = 150    # Maximum bars before range is stale
RANGE_MIN_TOUCHES     = 1      # Minimum touches per side (1 = at least 1 test each side)
RANGE_TOUCH_PROXIMITY = 0.35   # ATR fraction to count as a touch (was 0.15 — too tight)
RANGE_MIN_HEIGHT_ATR  = 0.25   # Range too narrow if below this (was 0.5)
RANGE_MAX_HEIGHT_ATR  = 3.5    # Range too wide if above this (lowered from 5.0; 2025 XAUUSD was very volatile)
RANGE_MIN_QUALITY     = 40     # Optimized: 40 vs 60 gives 75.6% WR vs 66.7%; sweet spot

# Range quality score weights (0-100 scale) — configurable
RANGE_SCORE_BOTH_SIDES_TOUCHED = 30   # both sides touched 2+ times
RANGE_SCORE_HEIGHT_SWEET_SPOT  = 20   # height 0.75–2.0x ATR
RANGE_SCORE_ASIAN_FORMATION    = 15   # formed during Asian/off-hours
RANGE_SCORE_LONG_COMPRESSION   = 10   # >= 15 bars inside
RANGE_SCORE_CONTRACTION        = 10   # later bars have smaller bodies
RANGE_SCORE_CLEAN_TOUCHES      = 15   # minimal wick through levels
RANGE_PREMIUM_THRESHOLD        = 75   # premium range if score >= this

# Asian/off-hours session for range quality bonus (UTC)
ASIAN_SESSION_OPEN  = "00:00"
ASIAN_SESSION_CLOSE = "07:00"
OFF_HOURS_OPEN      = "17:00"

# ===========================================================
# PHASE 2 ADAPTIVE OVERRIDE FRAMEWORK (legacy names kept for compatibility)
# ===========================================================
# Round-number increment for XAUUSD ($50 levels = 2500, 2550, 2600 …)
ROUND_NUMBER_INCREMENT = 50

# Legacy override thresholds (kept for backward-compat; Hybrid Engine uses MOD_* below)
MOMENTUM_OVERRIDE_MIN_SCORE         = 45
MOMENTUM_OVERRIDE_MIN_DISPLACEMENT  = 20
CONFLUENCE_OVERRIDE_MIN_SCORE       = 35
CONFLUENCE_OVERRIDE_MIN_CONF_RN     = 20
SWEEP_MAG_OVERRIDE_MIN_SCORE        = 40
SWEEP_MAG_OVERRIDE_MIN_SWEEP        = 25
SWEEP_MAG_OVERRIDE_MIN_RANGE_QUAL   = 25
CONTEXT_OVERRIDE_MIN_SCORE          = 55
CONTEXT_OVERRIDE_MIN_DISPLACEMENT   = 20
CONTEXT_OVERRIDE_MIN_CONFLUENCE     = 30
CONTEXT_OVERRIDE_MIN_HTF            = 20
CONFLUENCE_OVERRIDE_MAX_BARS        = 175

# ===========================================================
# HYBRID ENGINE — SETUP MODIFICATION LAYER (Phase 3)
# ===========================================================
# Each modification has its own threshold set.
# These thresholds are deliberately lower than the Phase 2 override thresholds
# because the regime detector acts as the outer guard — modifications only fire
# when the regime justifies them.

# MODIFICATION 1 — WEAK_RANGE_STRICT_ENTRY
# Triggers: range_quality 25–39 AND regime TRENDING_STRONG or TRENDING_MODERATE
MOD1_RANGE_QUALITY_MIN      = 25    # absolute floor — never below
MOD1_RANGE_QUALITY_MAX      = 39    # gate threshold is 40; this is the soft window
MOD1_SWEEP_WICK_ATR_MIN     = 0.50  # wick must be > 0.50x ATR
MOD1_REQUIRED_ZONE_TYPES    = ["OB", "FVG"]   # no SR fallback
# Fallback range detection (secondary scan when primary finds nothing)
FALLBACK_RANGE_MIN_QUALITY  = 20    # secondary scan threshold (vs primary 40)
FALLBACK_MOD1_WICK_ATR_MIN  = 0.35  # stricter wick for fallback (vs MOD1 standard 0.30)

# MODIFICATION 2 — BOS_MACRO_DISPLACEMENT
# Triggers: structure_break = BOS (not MSS) AND regime TRENDING_STRONG only
MOD2_BODY_ATR_MIN           = 0.75  # Phase3 Iter3: lowered from 1.0 → unlocks 3 more clean BOS setups (8 vs 5 eligible)
MOD2_HTF_SWING_COUNT_MIN    = 5     # need strong macro trend (≥5 qualifying swings)
MOD2_REQUIRED_ZONE_TYPES    = ["OB", "BB"]   # highest-quality zones only
MOD2_SL_BUFFER_INCREASE     = 0.10  # add 0.10 ATR to SL buffer for BOS trades

# MODIFICATION 3 — STALE_RETEST_CONFLUENCE
# Triggers: retest bars > RETEST_TIMEOUT_BARS AND < MOD3_MAX_BARS AND regime not HV
MOD3_MAX_BARS               = 175   # hard outer limit
MOD3_MIN_CONFLUENCE_TYPES   = 2     # kept at 2; lowering to 1 added a losing MOD4 trade via stale-retest interaction

# MODIFICATION 4 — BLOCKED_SWEEP_ELEVATED_CONTEXT
# Triggers: sweep type in blocked list (but NOT the zero-WR types) AND regime TRENDING_STRONG
MOD4_SWEEP_WICK_ATR_MIN     = 0.30  # Iter 2: lowered from 0.50 (median wick=0.430, >=0.30 captures ~55%)
MOD4_BODY_ATR_MIN           = 0.50  # Iter 2: lowered from 1.0 (body median=0.613, >=0.50 captures majority)
MOD4_REQUIRED_ZONE_TYPES    = ["OB", "BB"]  # covers 61/62 blocked sweep rejections
MOD4_HTF_SWING_COUNT_MIN    = 3
# Zero-WR sweep types that can NEVER be unlocked by any modification
MOD4_NEVER_UNLOCK_LONG      = ["SSL_SESSION_LOW", "SSL_RANGE_LOW"]
# BSL_PDH for SHORT: 0% WR (-1.025R sole trade) — permanently hard-blocked for MOD4
MOD4_NEVER_UNLOCK_SHORT     = ["BSL_PDH"]

# MODIFICATION 5 — LONG_PDL_RECOVERY
# Triggers: direction=LONG AND sweep=SSL_PDL AND regime TRENDING_STRONG
MOD5_SWEEP_TYPE             = "SSL_PDL"
MOD5_SWEEP_WICK_ATR_MIN     = 0.40
MOD5_REQUIRED_ZONE_TYPES    = ["OB"]
MOD5_ENTRY_MODE             = "MODE_WICK_REJECTION"

# ===========================================================
# LIQUIDITY GRAB (SWEEP)
# ===========================================================
SWEEP_MIN_WICK_ATR  = 0.20   # Minimum wick beyond level (ATR fraction) — tightened from 0.15
SWEEP_MIN_WICK_ABS  = 0.20   # Minimum wick beyond level (absolute $) — tightened from 0.15
SWEEP_LOOKBACK_BARS = 20     # How many bars before breakout to find sweep — was 15
EQUAL_HIGH_LOW_BAND = 0.25   # ATR fraction for EQH/EQL proximity — was 0.20

# News proximity window for sweep flagging (minutes)
NEWS_PROXIMITY_MINUTES = 30

# ===========================================================
# STRUCTURE BREAK
# ===========================================================
BREAKOUT_MIN_BODY_ATR = 0.30  # Minimum breakout candle body (ATR fraction) — was 0.40
FALSE_BREAKOUT_BARS   = 3     # Bars to check for false break — was 1 (too strict on 5m)

# ===========================================================
# RETEST ZONES
# ===========================================================
OB_DISPLACEMENT_ATR = 1.2    # Minimum displacement after OB to qualify — raised from 0.8 (too loose)
FVG_MIN_SIZE_ATR    = 0.10   # Minimum FVG gap size (ATR fraction) — was 0.20
SR_ZONE_BAND_ATR    = 0.20   # Proximity band around broken S/R level — was 0.15
RETEST_TIMEOUT_BARS = 125    # Optimized: 125 bars plateau (100=same quality); longer = better completions
ZONE_MIN_WIDTH_ABS  = 0.20   # Minimum zone width in $ terms

# ===========================================================
# ENTRY
# ===========================================================
ENTRY_MODE = "MODE_WICK_REJECTION"
# Options: MODE_CLOSE_OUTSIDE | MODE_WICK_REJECTION | MODE_IMMEDIATE
# MODE_WICK_REJECTION: requires confirmed rejection candle (wick into zone, close back away)
# This significantly improves win rate vs MODE_CLOSE_OUTSIDE by filtering fake retests

# ===========================================================
# STOP LOSS
# ===========================================================
SL_ATR_BUFFER       = 0.30   # Optimized: 0.3 ATR buffer; 0.2 too tight for NY, 0.5 too loose
SL_MIN_DISTANCE_ABS = 1.00   # Minimum SL distance in $ — was 0.30 (too tight for XAUUSD)
SL_MAX_DISTANCE_ATR = 2.5    # Reject trade if SL exceeds this — was 2.0

# ===========================================================
# TAKE PROFIT (DYNAMIC)
# ===========================================================
TP_MIN_SCORE       = 20     # Skip trade if no target scores this high — was 30
TP_MIN_RR          = 1.0    # Minimum RR to consider a target valid
TP_MAX_RR          = 4.0    # Flag suspiciously far targets — was 3.5
TP_STALE_LEVEL_BARS = 200   # Level is stale if not touched in N bars — was 100
TP_MAX_DIST_ATR    = 6.0    # Target deduction if farther than this — was 4.0

# TP scoring bonuses (configurable)
TP_SCORE_RANGE_PROJECTION   = 20  # bonus if range quality > 75
TP_SCORE_PDH_PDL            = 15  # previous day high/low
TP_SCORE_SESSION_EXTREME    = 10  # session high/low
TP_SCORE_EQUAL_LEVEL        = 15  # equal highs / equal lows cluster
TP_SCORE_ROUND_NUMBER       = 10  # psychological round number ($50 increments)
TP_SCORE_OB_FVG_OVERLAP     = 10  # overlapping OB or FVG in direction
TP_DEDUCT_LOW_RR            = 15  # target is less than 1.0x RR from entry — was 20
TP_DEDUCT_STALE             =  5  # level not touched in last TP_STALE_LEVEL_BARS — was 15
TP_DEDUCT_FAR               =  5  # target more than TP_MAX_DIST_ATR from entry — was 10

# Round number increment for XAUUSD ($)
ROUND_NUMBER_INCREMENT = 50
ROUND_NUMBER_PROXIMITY = 5     # Within $5 of the level counts

# Swing lookback for TP candidates (bars)
TP_SWING_LOOKBACK_BARS = 50

# Dual TP — enabled to improve win rate via partial close + breakeven
DUAL_TP_ENABLED   = True
DUAL_TP_RATIO_1   = 0.50   # Close 50% at TP1
TP2_MIN_RR        = 1.8    # Minimum RR for TP2
BREAKEVEN_AFTER_TP1 = True  # Move SL to breakeven after TP1 hit

# Fixed TP1 at a set R multiple — overrides dynamic TP1 when > 0
# e.g. TP1_FIXED_RR = 1.0 places TP1 at exactly 1R from entry (high hit-rate)
# Dynamic scoring target then becomes TP2. Set 0 to use only dynamic targets.
TP1_FIXED_RR = 1.0

# SHORT sweep quality filter — block low-win-rate sweep types for SHORT entries
# BSL_RANGE_HIGH and BSL_PDH historically show ~25% WR; SWING and EQUAL are better
SHORT_BLOCKED_SWEEPS = ["BSL_EQUAL_HIGHS", "BSL_PDH", "BSL_RANGE_HIGH", "BSL_SESSION_HIGH"]
# BSL_EQUAL_HIGHS: historically 23% WR
# BSL_PDH: low WR
# BSL_RANGE_HIGH: added per audit — net loser at -0.162R avg
# BSL_SESSION_HIGH: consistently 0-50% WR across multiple test configs, avg -0.6R

LONG_BLOCKED_SWEEPS = ["SSL_RANGE_LOW", "SSL_SESSION_LOW"]
# SSL_RANGE_LOW: 33% WR, -0.511R avg — range low sweeps for LONG are structural losers
# SSL_SESSION_LOW: 0% WR, -1.027R avg — session low sweeps for LONG consistently fail

# Zone type filter
DISABLE_SR_ZONE = True   # SR zones historically show 33% WR — disabled for quality
DISABLE_BB_ZONE = False  # BB zones show 57.1% WR vs OB 70.2%; set True to block entirely

# ===========================================================
# TRADE MANAGEMENT
# ===========================================================
TIMEOUT_BARS          = 72    # Close trade after N bars regardless
SAME_BAR_RESOLUTION   = "SL"  # If SL and TP hit same bar: conservative = SL wins

# ===========================================================
# FILTERS
# ===========================================================
SESSION_FILTER_ON    = True
VOLATILITY_FILTER_ON = True
VOLATILITY_MIN_ATR   = 0.80   # Minimum ATR to trade (raised from 0.50; avoid low-vol noise)
VOLATILITY_MAX_ATR   = 5.00   # Skip if ATR too high (news spike / chaos)
SPREAD_FILTER_MAX    = 2.00   # Skip if spread exceeds this — was 1.00 (too tight)
NO_TRADE_BEFORE_NEWS = True   # Skip setups within 30 min of known news

# ===========================================================
# HTF BIAS FILTER (HIGHER TF) — HARD GATE
# ===========================================================
HIGHER_TF_FILTER_ON = True    # 1H structure must align with trade direction
# LONG only when 1H = UPTREND, SHORT only when 1H = DOWNTREND
# When 1H = RANGING: both directions allowed but logged as HTF_RANGING

# ===========================================================
# MSS vs BOS QUALITY SETTINGS
# ===========================================================
MSS_REQUIRED       = True   # Global MSS requirement (overridden by directional settings below)
MSS_REQUIRED_LONG  = True   # LONG: require MSS — BOS_BULLISH too weak (55% WR in testing)
MSS_REQUIRED_SHORT = True   # SHORT: require MSS — BOS_BEARISH adds MaxDD without compensating
MSS_PRIOR_LH_COUNT = 2      # Min number of lower highs before MSS is valid (bullish)

# ===========================================================
# DIRECTION FILTERS (set by iterative loop)
# ===========================================================
ENABLE_LONG  = True
ENABLE_SHORT = True

# ===========================================================
# PERFORMANCE / ACCELERATION
# ===========================================================
# Optional packages:
# - polars: faster lazy CSV/cache reads when installed
# - numba: JIT-friendly numeric loops for future deeper refactors
# - joblib: parallel independent parameter sweeps when installed
USE_POLARS_IO = True
PARALLEL_ITERATION_TESTS = True
PARALLEL_N_JOBS = -1

# ===========================================================
# KNOWN HIGH-IMPACT NEWS WINDOWS (UTC times, recurring weekly)
# These are approximate recurring windows; production would use a live feed.
# Format: list of (weekday, hour, minute) for 30-min before/after window
# ===========================================================
HIGH_IMPACT_NEWS_WINDOWS = [
    # NFP: First Friday of month 12:30 UTC
    {"name": "NFP",  "weekday": 4, "hour": 12, "minute": 30},
    # CPI: Usually Wednesday or Thursday 12:30 UTC
    {"name": "CPI",  "weekday": 2, "hour": 12, "minute": 30},
    # FOMC: Wednesday 18:00 UTC (8 times/year, approximate)
    {"name": "FOMC", "weekday": 2, "hour": 18, "minute": 0},
]

# ===========================================================
# TWELVE DATA API
# ===========================================================
TWELVE_DATA_API_KEY = "b676e2c075b649c587fe3d2b4d5958ef"
CACHE_DIR           = "./cache"
CACHE_FORMAT        = "csv"    # "csv" or "parquet"
MAX_RETRIES         = 5
RETRY_DELAY_SECONDS = 2.0
API_BASE_URL        = "https://api.twelvedata.com/time_series"
API_OUTPUT_SIZE     = 5000     # max bars per request

# Multiple API keys for parallel data fetching
TWELVE_DATA_API_KEYS = [
    "b676e2c075b649c587fe3d2b4d5958ef",  # API 2
    "8d59bf1c84a74979a87045fd5cd08459",  # API 3
    "9273a4397b8a47fdb41de884274489ba",  # API 4
    "c90500ddd9e9439db7ead6bbc0f382df",  # API 5
    "0e6ee0a1b9414198aac018b8a096519b",  # API 6
    "c8256d1382744c9ab2f3b5f04275b025",  # API 7
    "e723f25bd4df48408b7e6239ee76bdd9",  # API 8
]

# Rate limit: free tier 8 calls/min per key, 800/day
API_CALLS_PER_MINUTE = 8
