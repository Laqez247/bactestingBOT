# EDGE 2 BREAKOUT BACKTESTING ENGINE — MASTER PROMPT v2
## XAUUSD-Specific | 5m/15m Execution | Self-Improving Iterative Loop

---

## ROLE & OBJECTIVE

You are a senior Python quant engineer and price-structure trading systems architect.

Your task is to build a complete, non-cheating, modular backtesting engine for
**Edge 2 (Breakout)** using XAUUSD OHLC data from the Twelve Data REST API.

This is NOT a generic strategy tester. Every rule, filter, and parameter in this engine
exists because of how XAUUSD specifically behaves on 5m and 15m timeframes during
London and New York sessions. You must internalize that context before writing a
single line of code.

After building the engine, you must run it, read the results, diagnose what is and is
not working, and iteratively improve the code — all sequentially and automatically,
with a written summary after each iteration.

---

## INSTRUMENT CONTEXT — READ THIS FIRST

Before you build anything, understand the instrument you are modelling.

### XAUUSD Characteristics

- XAUUSD (Spot Gold vs US Dollar) is a highly liquid instrument traded 24/5.
- Price is quoted to 2 decimal places. 1 pip = $0.10. 1 full point = $1.00.
- Typical spread on a retail broker (e.g., Exness) in active session: $0.20–$0.50 (0.2–0.5 pts).
- During news or off-hours, spread can spike to $2.00–$5.00. Model this.
- ATR(14) on 5m during London/NY overlap: approximately $0.70–$2.50.
- ATR(14) on 15m during London session: approximately $1.50–$4.00.
- XAUUSD forms clean S/R compression ranges during Asian session and early London,
  then breaks out violently when liquidity is triggered.
- Wicks are frequently exaggerated on XAUUSD. A 30-pip wick that comes back inside is
  a normal liquidity grab, not a breakout. Your engine must account for this.
- XAUUSD is highly reactive to USD news events (CPI, NFP, FOMC).
  These events create breakout patterns but also fake-outs that look identical.
  The engine should log when a setup coincides with a known high-impact window.
- Round numbers ($1900, $1950, $2000, $2100, $2300, $2400, etc.) act as strong
  psychological levels and should be treated as liquidity magnets.
- The instrument has strong directional momentum phases. A structure break on 15m
  often leads to 50–150 pip continuation before the next resistance zone.

### Session Schedule (UTC)

| Session       | UTC Open | UTC Close | Typical Behavior                              |
|---------------|----------|-----------|-----------------------------------------------|
| Asian         | 00:00    | 07:00     | Range formation, consolidation, manipulation  |
| London Open   | 07:00    | 08:30     | Initial sweep of Asian range, structure break |
| London Core   | 08:30    | 12:00     | Main trend development, OB retests            |
| NY Open       | 12:00    | 15:00     | Re-test of London structure, second moves     |
| NY Core       | 15:00    | 17:00     | Continuation or reversal                      |
| Off Hours     | 17:00    | 00:00     | Low liquidity, do not trade                   |

The highest-quality breakouts happen during London Open (07:00–09:30 UTC) and
NY Open (12:30–15:00 UTC). Setups forming outside these windows have lower
historical continuation probability and should be filtered or down-scored.

---

## CORE PRINCIPLES — NON-NEGOTIABLE

### 1. Zero Lookahead Bias
Any swing, zone, level, or structure label used to make a decision at bar T must be
derived exclusively from candles 0 through T. No exceptions. Swing highs and lows
must use delayed pivot confirmation with at least N bars on both the left and right
sides. N is configurable. Never reference a bar's high, low, or close before that bar
has closed.

### 2. No Indicator Soup
Do not use RSI, MACD, Stochastic, Bollinger Bands, or any lagging indicator as
entry or filter logic. ATR is permitted for measurement only (range size, sweep size,
displacement size, SL buffer). This is a pure price-structure engine.

### 3. Realistic Fills, No Fantasy Entries
Entry happens on a confirmed retest reaction candle, never on the breakout candle
itself. If price never returns to the retest zone, the setup expires. Spread and
slippage must reduce your fill price in the direction that hurts the trade.

### 4. Conservative, Structural Stop Losses
No arbitrary pip-based stops. SL must be placed at a level that structurally
invalidates the setup if hit — below the OB low for longs, above the OB high for
shorts. Add a configurable ATR buffer on top.

### 5. Dynamic, Realistic Take Profits
No fixed RR multipliers. TP must come from actual historical reaction zones visible
at entry time. If no credible target zone exists, the trade is skipped.

### 6. Flexibility Over Overfitting
Parameters must have ranges, not single magic numbers. Do not optimize parameters
solely to maximize win rate on historical data. Instead, reason about why each
parameter value makes structural sense for XAUUSD. If changing a parameter
by ±20% collapses performance, that is a sign of overfitting, not a good model.

### 7. Every Condition Is a Hard Gate — ELSE No Trade
The setup validation is a strict sequential IF/THEN/ELSE chain, not a scoring
system. Each condition below must be TRUE before evaluating the next. If any
condition is FALSE, the setup is immediately rejected and the rejection reason
is logged. There is no partial credit, no override, and no workaround.

The full gate chain for a LONG setup:
  IF   HTF trend is Bullish (1H structure: HH + HL sequence)
  AND  A valid compression range exists
  AND  SSL has been swept (sell-side liquidity below range low or equal lows)
  AND  A Bullish MSS or BOS is confirmed after the sweep
  AND  A valid retest zone exists (Bullish OB or FVG, in that priority order)
  AND  Price retests the zone within timeout and shows rejection
  AND  A valid dynamic TP target exists (score >= threshold)
  THEN open LONG trade
  ELSE reject setup, log the first condition that failed, do not trade

Mirror applies for SHORT. This chain is implemented in trade_simulator.py
as a guard function that runs before any position is opened.

---

## STRATEGY LOGIC — FULL WALKTHROUGH

This section explains exactly what Edge 2 is and how every component works.
Read this before building any module.

### What Edge 2 Is

Edge 2 is a breakout-retest strategy. It profits from the sequence of:
1. Market compresses into a defined range
2. Liquidity is taken from one or both sides
3. Price breaks structure in one direction
4. Price retests the broken level or nearby institutional zone
5. Price continues in the breakout direction to a liquidity target

This is NOT a random breakout system. Random breakouts have negative expectancy.
Edge 2 only triggers when the specific sequence above is confirmed in order.
Skipping steps leads to chasing moves that have already completed.

### Step A — Range / Compression Detection

A valid compression range requires:
- A defined ceiling (range high) and floor (range low) that price has respected multiple times
- The range must be TIGHT relative to normal volatility — this is the key signal
- Range height should be between 0.5x ATR(14) and 3.0x ATR(14) on the execution TF
  - Below 0.5x ATR: too tight, likely noise
  - Above 3.0x ATR: too wide, not a compression range
- A minimum of 6 bars inside the range (configurable, default: 6 for 5m, 4 for 15m)
- A maximum of 120 bars inside the range (default: 120 for 5m, 80 for 15m)
  — ranges that last too long lose breakout energy
- Both the range high and range low must have been touched/approached at least 2 times
- Touches are counted using a proximity threshold (within 0.15x ATR of the level)

Range quality score (0–100):
- +30: both sides touched 2+ times
- +20: range height is 0.75–2.0x ATR (sweet spot)
- +15: range formed during Asian session or off-hours (highest quality)
- +10: bars inside range >= 15 (more compression = more energy)
- +10: range contraction visible (later bars have smaller bodies than early bars)
- +15: clean touches with minimal wick through level
- Score threshold for valid range: 50 (reject below this)
- Score threshold for premium range: 75 (log separately)

Do NOT hardcode these weights — make them configurable with these as defaults.

### Step B — Range Boundary Extraction

Once a range is identified:
- Range High = highest confirmed reaction high within the range period
- Range Low = lowest confirmed reaction low within the range period
- These levels are your primary reference for the breakout direction, liquidity grab,
  and retest zone

Track per level:
- Number of touches
- Average wick size through the level
- Whether touches are clean rejections or messy overlap

### Step C — Liquidity Sweep: SSL for Bullish, BSL for Bearish

**Definitions — understand these before implementing:**

**SSL (Sell-Side Liquidity):** The cluster of resting sell-stop orders that sit
BELOW price — below range lows, below equal lows, below session lows, below
swing lows. These are stop-losses of retail buyers and breakout-shorts waiting
to be triggered. Sweeping SSL means smart money pushes price DOWN through these
levels, collects those orders, then reverses UP. This is the fuel for a bullish move.

**BSL (Buy-Side Liquidity):** The cluster of resting buy-stop orders that sit
ABOVE price — above range highs, above equal highs, above session highs, above
swing highs. Sweeping BSL means smart money pushes price UP through these levels
to collect buy orders, then reverses DOWN. This is the fuel for a bearish move.

**The directional rule is non-negotiable:**
- BULLISH setup requires SSL sweep ONLY. If BSL is swept and no SSL sweep
  precedes the bullish breakout, the setup is INVALID.
- BEARISH setup requires BSL sweep ONLY. If SSL is swept and no BSL sweep
  precedes the bearish breakout, the setup is INVALID.

This is not symmetric coincidence — it is the mechanism. The direction of the
sweep determines the direction of the trade. Do not mix them up.

**SSL Pool Types (for Bullish setup — sweep must hit one of these):**
- Range low (the defined range floor)
- Equal lows (2+ confirmed swing lows within EQUAL_HIGH_LOW_BAND * ATR of each other)
- Previous day low (PDL)
- Previous session low
- Any recent confirmed swing low on the execution TF

**BSL Pool Types (for Bearish setup — sweep must hit one of these):**
- Range high (the defined range ceiling)
- Equal highs (EQH)
- Previous day high (PDH)
- Previous session high
- Any recent confirmed swing high on the execution TF

**Sweep validity rules (apply to both SSL and BSL):**
- The wick must extend beyond the reference level by at least SWEEP_MIN_WICK_ATR * ATR
- The wick extension must also be at least SWEEP_MIN_WICK_ABS in absolute $ terms
  — this kills spread-noise spikes that look like sweeps but aren't
- The candle must CLOSE back on the opposite side of the swept level
  (close above the level for SSL sweep, close below for BSL sweep)
- Tiny random pokes that do not leave a visible wick do not count
- A sweep inside the last 30 minutes before a high-impact news event is flagged
  as UNRELIABLE and logged, but not automatically disqualified — the backtester
  will separately analyse news-adjacent sweeps in the metrics output

**Sweep lookback:**
Allow up to SWEEP_LOOKBACK_BARS bars before the structure break candle to find
the qualifying sweep. If no valid SSL sweep (for bullish) or BSL sweep (for bearish)
is found in that window, the setup fails this gate immediately.

**Sweep types to track and log separately:**
- SSL_RANGE_LOW       (range floor swept)
- SSL_EQUAL_LOWS      (EQL cluster swept)
- SSL_PDL             (previous day low swept)
- SSL_SESSION_LOW     (session low swept)
- SSL_SWING_LOW       (confirmed swing low swept)
- BSL_RANGE_HIGH      (range ceiling swept)
- BSL_EQUAL_HIGHS     (EQH cluster swept)
- BSL_PDH             (previous day high swept)
- BSL_SESSION_HIGH    (session high swept)
- BSL_SWING_HIGH      (confirmed swing high swept)

### Step D — Structure Shift: MSS is Primary, BOS is Secondary

**Definitions — this distinction matters:**

**BOS (Break of Structure):** A candle close beyond any confirmed swing high (bullish)
or swing low (bearish). This confirms that the immediate local structure has been
broken. It can happen within a trend as continuation or at a turning point. On its
own, it is a necessary but not sufficient condition for a valid Edge 2 setup.

**MSS (Market Structure Shift):** A more significant event. An MSS occurs when price
creates the FIRST higher high after a sequence of lower highs (Bullish MSS), or the
FIRST lower low after a sequence of higher lows (Bearish MSS). The MSS signals that
the prevailing short-term direction has genuinely flipped — it is not just a break
of one swing, it is evidence of a change in order flow direction.

In the context of Edge 2:
- After an SSL sweep, the ideal confirmation is a **Bullish MSS** — price not only
  breaks a swing high but does so from a sequence where structure was previously
  bearish or ranging. This is the "shift" — the market was going down (sweeping SSL),
  and now it has structurally turned up.
- A **BOS alone** (price just breaks one recent swing high without a prior sequence of
  lower highs) is a weaker signal. It still qualifies, but is logged as lower quality.
- The difference matters because MSS after SSL = high-conviction institutional reversal.
  BOS after SSL = could still be a dead-cat bounce or weak continuation.

**Implementation rules:**

MSS_BULLISH (strong gate — preferred):
- Track the last 3 confirmed swing highs on the execution TF
- If the sequence was: SH[2] > SH[1] > SH[0] (lower highs), structure was bearish
- An MSS is confirmed when a candle closes above SH[0] (the most recent lower high)
- AFTER a valid SSL sweep has already occurred
- The MSS candle body must be >= BREAKOUT_MIN_BODY_ATR * ATR
- Price must not close back below SH[0] on the very next bar

BOS_BULLISH (secondary gate — valid but lower quality):
- A candle close above ANY confirmed swing high (not requiring a prior LH sequence)
- Still requires the prior SSL sweep
- Still requires minimum body size
- Logged as BOS, not MSS — tracked separately in metrics

MSS_BEARISH / BOS_BEARISH: mirror of the above using swing lows and BSL sweeps.

**False structure break filter:**
If the candle immediately after the MSS/BOS close candle closes BACK below (bullish)
or above (bearish) the broken pivot, the structure break is voided.
This is the single most common failure mode on XAUUSD 5m — fake breaks are frequent.
Do not skip this filter.

**Structure break quality classification:**
- MSS + body > 1.0x ATR: STRONG (highest conviction)
- MSS + body 0.40–1.0x ATR: MODERATE
- BOS + body > 0.40x ATR: MODERATE_BOS
- Anything below threshold: reject

### Step E — Retest Zone Construction

After a valid breakout, the engine constructs candidate retest zones.
The retest is where the trade entry will happen.

Retest zone priority order (highest to lowest quality):

1. ORDER BLOCK (OB)
   - Bullish OB: the last BEARISH candle before a strong bullish displacement move
     that led to the breakout. This candle represents institutional selling that was
     absorbed, and the zone is where buyers are likely to defend again on retest.
   - Bearish OB: the last BULLISH candle before a strong bearish displacement.
   - OB boundaries: [candle open, candle close] (body only, not wick)
   - For XAUUSD the OB body is the key zone; the full wick can extend SL further
   - Displacement qualifier: the move following the OB must be at least 1.5x ATR
     to count as a genuine OB-originating move
   - An OB is INVALIDATED if price closes through its body before the retest

2. FAIR VALUE GAP (FVG)
   - A 3-candle imbalance pattern:
     - Bullish FVG: Candle[N-2].high < Candle[N].low (gap between candle N-2 top and candle N bottom)
     - Bearish FVG: Candle[N-2].low > Candle[N].high (gap between candle N-2 bottom and candle N top)
   - The FVG must be created DURING the breakout displacement move
   - FVG validity: if price fully closes through the FVG before retest, it is voided
   - FVG minimum size: at least 0.20x ATR to filter microscopic gaps

3. BREAKER BLOCK (BB)
   - A prior bullish OB that failed (price broke through it) and now acts as resistance
     (bearish context), or a prior bearish OB that failed and now acts as support (bullish)
   - Only count as a BB when: the original OB has been broken with a candle close through
     its body, AND price is now returning to test that zone from the other side
   - Breaker blocks are the highest-conviction retest zones when they exist

4. BROKEN S/R LEVEL (RANGE BOUNDARY)
   - The broken range high (for bullish breakout) or broken range low (for bearish breakout)
     now acts as support/resistance
   - This is the most common retest zone and the fallback if OB/FVG/BB are not available
   - Use a proximity band: price must come within 0.15x ATR of the broken level

Zone thickness rules:
- OB zone: from OB candle open to OB candle close
- FVG zone: from FVG bottom to FVG top
- BB zone: same as original OB zone
- SR zone: from broken level minus 0.10x ATR to broken level plus 0.10x ATR
- If the zone is less than $0.20 wide after ATR scaling, expand to $0.20 minimum

### Step F — Entry Trigger

The engine waits for price to enter the retest zone.

Once price enters the zone:
- A reaction must be confirmed — do not enter on zone touch alone
- Reaction options (configurable):
  - MODE_CLOSE_OUTSIDE: enter when a candle closes back above (bullish) or below
    (bearish) the zone boundary — more conservative, fewer trades
  - MODE_WICK_REJECTION: enter when a candle wicks into the zone but closes
    away from it — more aggressive, more trades
  - MODE_IMMEDIATE: enter at zone touch with no additional confirmation — least
    conservative, only use with very tight SL and high zone quality
- Default mode: MODE_CLOSE_OUTSIDE

Retest timeout:
- If price does not retest within N bars after the structure break, the setup expires
- Default N: 30 bars on 5m (150 minutes), 20 bars on 15m (300 minutes)
- This prevents stale setups from triggering after market context has changed

Entry price:
- Bullish: entry = zone top boundary + spread (buying above the zone to confirm rejection)
- Bearish: entry = zone bottom boundary - spread (selling below the zone)
- Apply slippage: add configurable slippage (default $0.05 above/below entry side)

### Setup Validation Gate — The Full IF/THEN/ELSE Check

Before any trade opens, the engine runs this validation function in order.
Each check is a hard binary pass/fail. First failure = immediate rejection.
The rejection_reason field logs exactly which gate failed.

```
FUNCTION validate_setup(direction, bar_index, state):

  # Gate 1: HTF trend alignment
  IF HIGHER_TF_FILTER_ON:
    IF direction == LONG  AND htf_trend != UPTREND:   REJECT("HTF_NOT_BULLISH")
    IF direction == SHORT AND htf_trend != DOWNTREND: REJECT("HTF_NOT_BEARISH")

  # Gate 2: Valid compression range exists
  IF NOT state.range_valid:                           REJECT("NO_VALID_RANGE")

  # Gate 3: Correct liquidity sweep type for direction
  IF direction == LONG:
    IF NOT state.ssl_swept:                           REJECT("NO_SSL_SWEEP")
  IF direction == SHORT:
    IF NOT state.bsl_swept:                           REJECT("NO_BSL_SWEEP")

  # Gate 4: MSS or BOS confirmed after the sweep
  IF direction == LONG:
    IF NOT (state.mss_bullish OR state.bos_bullish):  REJECT("NO_BULLISH_MSS_BOS")
  IF direction == SHORT:
    IF NOT (state.mss_bearish OR state.bos_bearish):  REJECT("NO_BEARISH_MSS_BOS")

  # Gate 5: Valid retest zone exists (OB or FVG preferred, SR as fallback)
  IF NOT state.retest_zone_valid:                     REJECT("NO_RETEST_ZONE")

  # Gate 6: Price has reached and reacted from the zone
  IF NOT state.zone_reaction_confirmed:               REJECT("NO_ZONE_REACTION")

  # Gate 7: Valid dynamic TP target exists
  IF state.best_tp_score < TP_MIN_SCORE:             REJECT("NO_VALID_TARGET")

  # Gate 8: RR is acceptable
  IF state.best_rr < TP_MIN_RR:                      REJECT("RR_TOO_LOW")

  # Gate 9: SL distance within limits
  IF state.sl_distance > SL_MAX_DISTANCE_ATR * atr:  REJECT("SL_TOO_WIDE")

  # Gate 10: Spread filter
  IF current_spread > SPREAD_FILTER_MAX:             REJECT("SPREAD_TOO_HIGH")

  # All gates passed
  RETURN VALID
```

If RETURN is VALID: open the trade.
If REJECT is called at any gate: log the rejection and move to the next setup candidate.
Do not re-evaluate a rejected setup.



Bullish trade:
- SL below the retest zone invalidation low
- Specifically: below the lowest low of the retest zone candle minus ATR buffer
- Default ATR buffer: 0.20x ATR(14)
- SL must be below the breakout OB's full wick, not just the body

Bearish trade:
- SL above the retest zone invalidation high plus ATR buffer

Minimum SL distance: $0.30 (30 pips) after spread and buffer
Maximum SL distance: 2.0x ATR (if structural SL exceeds this, reject the setup as
the position size would be too large to be practical)

### Take Profit — Dynamic Target Engine

This is the most critical component. A fixed RR system will fail on XAUUSD because
the instrument has uneven leg structures. TP must be placed at a level the market
has actually respected historically.

Candidate target generation (all checked in order):

1. Opposite side of the original range
   - Bullish: range high + (range height * 1.0) — the projected target above the range
   - Bearish: range low - (range height * 1.0)
   - Score bonus: +20 if range is high quality (score > 75)

2. Previous swing highs/lows (from structure_engine)
   - Nearest unbroken swing high above entry (bullish target)
   - Nearest unbroken swing low below entry (bearish target)
   - Only use swings from the last 50 bars

3. Previous day high/low
   - If the PDH is above entry (bullish), it acts as a natural target
   - Score bonus: +15

4. Previous session high/low
   - London high/low, NY high/low — strong liquidity magnets
   - Score bonus: +10

5. Equal highs/equal lows in the vicinity
   - EQH above price (bullish target): clusters of prior swing highs within 0.20x ATR
   - EQL below price (bearish target)
   - Score bonus: +15 (equal levels have high probability of being revisited)

6. Psychological round numbers
   - For XAUUSD: every $50 increment ($2300, $2350, $2400, etc.)
   - Within $5 of the level: score bonus +10

7. Nearby OB / FVG zones in the direction of trade
   - A bullish OB above price (bearish OB acts as resistance for bullish TP)
   - Score bonus: +10 if overlapping with another target

Target scoring formula:
- Base score: 0
- For each factor present, add the bonus listed above
- Deductions:
  - -20 if the target is less than 1.0x RR from entry
  - -15 if the level has not been touched in the last 100 bars (stale)
  - -10 if the target is more than 4.0x ATR from entry
    (this rarely gets hit in a single move on 5m/15m)

Target selection rules:
- Minimum target score: 30
- If no candidate scores >= 30, skip the trade
- Select the highest-scoring candidate as the primary TP
- If two candidates are within 10 score points of each other, prefer the nearer one
- Optional dual TP:
  - TP1: nearest qualifying target (minimum 1.0 RR)
  - TP2: second qualifying target (minimum 1.8 RR)
  - For simulation: close 50% at TP1, trail SL to breakeven, close remainder at TP2

Minimum RR requirement: 1.0 (if best TP gives less than 1.0 RR after spread, skip)
Practical RR range for valid setups: 1.0–3.5 (anything above 3.5 is unlikely and
statistically suspicious — log it but treat with low confidence)

---

## MODULE ARCHITECTURE

Build in clean Python modules. Each module has a single responsibility.

```
edge2_backtest/
├── config.py
├── data_loader.py
├── structure_engine.py
├── sr_engine.py
├── liquidity_engine.py
├── breakout_engine.py
├── zone_engine.py
├── tp_engine.py
├── trade_simulator.py
├── metrics.py
├── report.py
├── run_backtest.py       ← master runner with iterative loop
└── results/
    ├── trades_ITER_N.csv
    ├── setups_ITER_N.csv
    └── summary_ITER_N.txt
```

### config.py — All Parameters in One Place

```python
# ===========================================================
# INSTRUMENT
# ===========================================================
SYMBOL = "XAU/USD"
EXECUTION_TF = "5min"          # Primary timeframe
STRUCTURE_TF = "1h"            # Optional higher-TF bias filter
DATA_SOURCE = "twelvedata"

# ===========================================================
# DATE RANGE
# ===========================================================
BACKTEST_START = "2023-01-01"
BACKTEST_END   = "2024-12-31"
WALK_FORWARD_SPLIT = 0.70      # 70% in-sample, 30% out-of-sample

# ===========================================================
# SESSIONS (UTC)
# ===========================================================
TRADE_SESSIONS = {
    "london": {"open": "07:00", "close": "12:00"},
    "new_york": {"open": "12:00", "close": "17:00"},
}
HIGH_PRIORITY_WINDOWS = [
    {"name": "london_open", "start": "07:00", "end": "09:30"},
    {"name": "ny_open", "start": "12:30", "end": "15:00"},
]

# ===========================================================
# SPREAD & SLIPPAGE (in price units, 1 unit = $1 on XAUUSD)
# ===========================================================
DEFAULT_SPREAD        = 0.35   # $0.35 default spread
NEWS_SPREAD_MULTIPLIER = 4.0   # Spread multiplier during news
SLIPPAGE              = 0.05   # $0.05 slippage per fill

# ===========================================================
# ATR SETTINGS
# ===========================================================
ATR_PERIOD = 14

# ===========================================================
# SWING DETECTION
# ===========================================================
PIVOT_LEFT  = 3    # Bars required to left of pivot for confirmation
PIVOT_RIGHT = 3    # Bars required to right of pivot for confirmation
# Higher = more conservative, fewer but higher-quality pivots
# Range: left 2–5, right 2–5 for 5m. For 15m use 2–4.

# ===========================================================
# RANGE DETECTION
# ===========================================================
RANGE_MIN_BARS         = 6     # Minimum bars inside range
RANGE_MAX_BARS         = 120   # Maximum bars before range is stale
RANGE_MIN_TOUCHES      = 2     # Minimum touches per side
RANGE_TOUCH_PROXIMITY  = 0.15  # ATR fraction to count as a touch
RANGE_MIN_HEIGHT_ATR   = 0.5   # Range too narrow if below this
RANGE_MAX_HEIGHT_ATR   = 3.0   # Range too wide if above this
RANGE_MIN_QUALITY      = 50    # Reject ranges scoring below this

# ===========================================================
# LIQUIDITY GRAB (SWEEP)
# ===========================================================
SWEEP_MIN_WICK_ATR     = 0.25  # Minimum wick beyond level (ATR fraction)
SWEEP_MIN_WICK_ABS     = 0.30  # Minimum wick beyond level (absolute $)
SWEEP_LOOKBACK_BARS    = 15    # How many bars before breakout to find sweep
EQUAL_HIGH_LOW_BAND    = 0.20  # ATR fraction for EQH/EQL proximity

# ===========================================================
# STRUCTURE BREAK
# ===========================================================
BREAKOUT_MIN_BODY_ATR  = 0.40  # Minimum breakout candle body (ATR fraction)
FALSE_BREAKOUT_BARS    = 1     # If price re-enters range within N bars = false break

# ===========================================================
# RETEST ZONES
# ===========================================================
OB_DISPLACEMENT_ATR    = 1.5   # Minimum displacement after OB to qualify
FVG_MIN_SIZE_ATR       = 0.20  # Minimum FVG gap size (ATR fraction)
SR_ZONE_BAND_ATR       = 0.15  # Proximity band around broken S/R level
RETEST_TIMEOUT_BARS    = 30    # Setup expires after N bars with no retest
ZONE_MIN_WIDTH_ABS     = 0.20  # Minimum zone width in $ terms

# ===========================================================
# ENTRY
# ===========================================================
ENTRY_MODE = "MODE_CLOSE_OUTSIDE"
# Options: MODE_CLOSE_OUTSIDE | MODE_WICK_REJECTION | MODE_IMMEDIATE

# ===========================================================
# STOP LOSS
# ===========================================================
SL_ATR_BUFFER          = 0.20  # ATR buffer added to structural SL
SL_MIN_DISTANCE_ABS    = 0.30  # Minimum SL distance in $
SL_MAX_DISTANCE_ATR    = 2.0   # Reject trade if SL exceeds this

# ===========================================================
# TAKE PROFIT (DYNAMIC)
# ===========================================================
TP_MIN_SCORE           = 30    # Skip trade if no target scores this high
TP_MIN_RR              = 1.0   # Minimum RR to consider a target valid
TP_MAX_RR              = 3.5   # Flag suspiciously far targets
TP_STALE_LEVEL_BARS    = 100   # Level is stale if not touched in N bars
TP_MAX_DIST_ATR        = 4.0   # Target deduction if farther than this

# ===========================================================
# TRADE MANAGEMENT
# ===========================================================
TIMEOUT_BARS           = 72    # Close trade after N bars regardless
SAME_BAR_RESOLUTION    = "SL"  # If SL and TP hit same bar: conservative = SL wins
DUAL_TP_ENABLED        = False
DUAL_TP_RATIO_1        = 0.50  # Close 50% at TP1
BREAKEVEN_AFTER_TP1    = True  # Move SL to breakeven after TP1

# ===========================================================
# FILTERS
# ===========================================================
SESSION_FILTER_ON      = True
VOLATILITY_FILTER_ON   = True
VOLATILITY_MIN_ATR     = 0.50  # Minimum ATR to trade (avoid dead markets)
VOLATILITY_MAX_ATR     = 5.00  # Skip if ATR too high (news spike / chaos)
SPREAD_FILTER_MAX      = 1.00  # Skip if spread exceeds this
NO_TRADE_BEFORE_NEWS   = True  # Skip setups within 30 min of known news

# ===========================================================
# HTF BIAS FILTER (HIGHER TF) — HARD GATE
# ===========================================================
HIGHER_TF_FILTER_ON    = True  # 1H structure must align with trade direction
# LONG only when 1H = UPTREND, SHORT only when 1H = DOWNTREND
# When 1H = RANGING: both directions allowed but logged as HTF_RANGING
# Disable ONLY for isolated testing; should be ON for production runs

# ===========================================================
# MSS vs BOS QUALITY SETTINGS
# ===========================================================
MSS_REQUIRED           = False # True = only accept MSS, reject BOS-only setups
# Default False = both MSS and BOS qualify, but MSS logged as higher quality
# Toggle to True in Iteration 4 to test MSS-only performance
MSS_PRIOR_LH_COUNT     = 2    # Min number of lower highs before MSS is valid (bullish)
# Prevents calling a random break an MSS — requires at least 2 prior lower highs

# ===========================================================
# SSL / BSL SWEEP SETTINGS
# ===========================================================
SWEEP_MIN_WICK_ATR     = 0.25  # Minimum wick beyond level (ATR fraction)
SWEEP_MIN_WICK_ABS     = 0.30  # Minimum wick beyond level (absolute $)
SWEEP_LOOKBACK_BARS    = 15    # How many bars before structure break to find sweep
EQUAL_HIGH_LOW_BAND    = 0.20  # ATR fraction for EQH/EQL proximity

# ===========================================================
# TWELVE DATA API
# ===========================================================
TWELVE_DATA_API_KEY = "YOUR_API_KEY_HERE"
CACHE_DIR           = "./cache"
CACHE_FORMAT        = "csv"    # "csv" or "parquet"
MAX_RETRIES         = 5
RETRY_DELAY_SECONDS = 2.0
```

---

## DATA LOADER

The data loader must:
- Call `https://api.twelvedata.com/time_series` with proper pagination
- Handle rate limits (Twelve Data free tier: 8 calls/minute, 800/day)
- Cache responses by symbol + timeframe + date range
- On resume: load cached data, detect the last cached timestamp, only fetch what's missing
- Normalize all timestamps to UTC
- Remove duplicate bars (keep first)
- Sort ascending by timestamp
- Detect and log missing candles (flag gaps > 2x normal interval, e.g., > 10 minutes for 5m)
  — do NOT interpolate missing bars, just log and skip them
- Validate: open/high/low/close must be positive, high >= open/close >= low

The loader should support fetching TWO timeframes simultaneously for the same date range
(execution TF and structure TF) and return both as aligned DataFrames.

---

## STRUCTURE ENGINE

The structure engine identifies meaningful price pivots and labels market structure.

### Pivot Detection — Strict No-Lookahead Implementation

A swing high at bar T is only confirmed at bar T + PIVOT_RIGHT.
At bar T+PIVOT_RIGHT, check:
- bars T-PIVOT_LEFT to T-1: all have lower highs than T
- bars T+1 to T+PIVOT_RIGHT: all have lower highs than T
Only then label bar T as a confirmed swing high. The same applies to swing lows.

This introduces a PIVOT_RIGHT bar delay in detection. That is correct and required.
Never label a pivot at bar T using bar T+1 or later in the decision logic.

### Market Structure States

Track the last 3 confirmed swing highs (SH) and 3 swing lows (SL) at all times.

Base structure classification:
- UPTREND: SH[0] > SH[1] > SH[2] AND SL[0] > SL[1] > SL[2] (HH + HL sequence)
- DOWNTREND: SH[0] < SH[1] < SH[2] AND SL[0] < SL[1] < SL[2] (LH + LL sequence)
- RANGING: neither condition fully met

### MSS and BOS Detection — Implement Both, Label Separately

These are the two structure break labels used directly in the setup validation gate.

**BOS_BULLISH:**
- A candle closes above the most recent confirmed swing high (SH[0])
- Regardless of whether SH[0] > SH[1] or SH[0] < SH[1]
- This is a basic structural break — needed but not sufficient on its own

**MSS_BULLISH:**
- The sequence of the last 3 swing highs was BEARISH (SH[0] < SH[1] ← lower high)
  meaning the short-term structure was making lower highs
- A candle now closes ABOVE SH[0] — the most recent lower high
- This is the SHIFT: price was respecting lower highs (bearish pressure), and has
  now broken that sequence for the first time, confirming a change in direction
- Requires the prior SSL sweep to have occurred (checked in validation gate)
- MSS_BULLISH is higher quality than BOS_BULLISH and should be logged separately

**BOS_BEARISH:**
- A candle closes below the most recent confirmed swing low (SL[0])

**MSS_BEARISH:**
- The sequence of the last 3 swing lows was BULLISH (SL[0] > SL[1] ← higher low)
- A candle closes BELOW SL[0] — breaking the higher-low sequence for the first time
- Requires prior BSL sweep

**Implementation note:**
At each bar, after updating confirmed pivots, set four boolean flags on the state:
- state.mss_bullish = True if MSS_BULLISH was just confirmed (reset after setup consumed)
- state.bos_bullish = True if BOS_BULLISH was just confirmed
- state.mss_bearish = True if MSS_BEARISH was just confirmed
- state.bos_bearish = True if BOS_BEARISH was just confirmed

These flags feed directly into Gate 4 of the setup validation function.
Once a setup is opened or rejected, reset the flags to avoid double-triggering.

**HTF structure classification (for HIGHER_TF_FILTER_ON):**
Run the same pivot detection on the 1H timeframe.
The htf_trend state (UPTREND / DOWNTREND / RANGING) feeds Gate 1.
If the 1H is RANGING, treat it as no directional bias — Gate 1 passes for both
directions, but log the setup as HTF_RANGING for separate analysis.

---

## RANGE ENGINE (sr_engine.py)

The range engine identifies horizontal compression zones.

Algorithm:
1. Scan backwards from the current bar up to RANGE_MAX_BARS
2. Identify the most recent period where:
   - Price oscillated between a consistent ceiling and floor
   - Both the ceiling and floor were touched RANGE_MIN_TOUCHES times
   - ATR-normalized height is within [RANGE_MIN_HEIGHT_ATR, RANGE_MAX_HEIGHT_ATR]
3. Score the range using the quality scoring formula above
4. Return: range_high, range_low, range_start_bar, range_end_bar, quality_score, touch_count

A range is EXPIRED and must be rebuilt if:
- A candle closes more than 1.0x ATR beyond either boundary (definitive break)
- More than RANGE_MAX_BARS have passed since the last touch

Do not detect overlapping ranges. Track only the most recent valid range.

---

## LIQUIDITY ENGINE (liquidity_engine.py)

Detects liquidity pools (SSL and BSL) and records sweep events.

### SSL Pool Construction (sell-side liquidity — below price)

SSL pools are clusters of resting sell-stop orders below reference levels.
For XAUUSD, they form reliably at:
- The range low (most common)
- Clusters of equal lows: confirmed swing lows within EQUAL_HIGH_LOW_BAND * ATR
- Previous day low (PDL) — resets each UTC day
- Previous session low — resets each session
- Any isolated confirmed swing low on the execution TF

Maintain a live list of active SSL pools at each bar.
A pool is consumed/removed when price sweeps it (see sweep detection below).

### BSL Pool Construction (buy-side liquidity — above price)

BSL pools are clusters of resting buy-stop orders above reference levels:
- The range high
- Equal highs (EQH): confirmed swing highs within EQUAL_HIGH_LOW_BAND * ATR
- Previous day high (PDH)
- Previous session high
- Any isolated confirmed swing high

### Sweep Detection

After each candle closes, scan all active pools:

For SSL sweep (downward wick through a pool level):
- Check: candle.low < pool.level - (SWEEP_MIN_WICK_ATR * atr)
- AND: candle.low < pool.level - SWEEP_MIN_WICK_ABS
- AND: candle.close > pool.level  (closed back above — liquidity was taken and reversed)
- If all conditions met:
  - Record sweep_event(type=SSL_*, bar=T, level=pool.level, wick_extension, atr_multiple)
  - Set state.ssl_swept = True
  - Set state.ssl_sweep_type = (specific pool type from SSL_* list)
  - Set state.ssl_sweep_bar = T
  - Remove or flag the pool as consumed

For BSL sweep (upward wick through a pool level):
- Check: candle.high > pool.level + (SWEEP_MIN_WICK_ATR * atr)
- AND: candle.high > pool.level + SWEEP_MIN_WICK_ABS
- AND: candle.close < pool.level  (closed back below)
- If all conditions met:
  - Record sweep_event(type=BSL_*, bar=T, level=pool.level, wick_extension, atr_multiple)
  - Set state.bsl_swept = True
  - Set state.bsl_sweep_type = (specific pool type from BSL_* list)
  - Set state.bsl_sweep_bar = T

**State reset rules:**
- After a valid LONG trade is opened: reset state.ssl_swept = False
- After a valid SHORT trade is opened: reset state.bsl_swept = False
- If SWEEP_LOOKBACK_BARS have passed since the sweep with no MSS/BOS:
  reset the sweep flag — the sweep is stale

---

## BREAKOUT ENGINE (breakout_engine.py)

Qualifies the structure break event after a liquidity sweep.
This module reads sweep events from the liquidity engine and structure labels
from the structure engine. It does not re-derive them independently.

For each SSL sweep event:
1. Watch forward up to SWEEP_LOOKBACK_BARS bars for a MSS_BULLISH or BOS_BULLISH label
   from the structure engine (confirmed on that bar)
2. Verify the MSS/BOS candle body >= BREAKOUT_MIN_BODY_ATR * ATR
3. Apply false breakout filter: the candle immediately after the MSS/BOS close must
   NOT close back below the broken swing high (for bullish) — if it does, void the event
4. If all conditions met: emit a breakout_event with:
   - direction: LONG
   - structure_type: MSS_BULLISH or BOS_BULLISH (logged separately)
   - sweep_type: (the SSL_* type that preceded this)
   - breakout_bar, breakout_price, breakout_candle_size
   - breakout_quality: STRONG (MSS + body > 1.0x ATR) | MODERATE (MSS, smaller body) |
                       MODERATE_BOS (BOS only)

For each BSL sweep event:
- Mirror logic using MSS_BEARISH or BOS_BEARISH, emitting a SHORT breakout_event

**Key constraint:** The breakout engine only considers a breakout valid if the
preceding sweep matches the required direction:
- SSL sweep → only BULLISH breakout events
- BSL sweep → only BEARISH breakout events
If the sweep direction and breakout direction do not match, discard the event.

---

## ZONE ENGINE (zone_engine.py)

Builds the retest zones from pre-breakout candle history.

OB Detection:
- Scan backwards from the breakout displacement move's start
- The last candle in the OPPOSITE direction before the displacement is the OB
- Example: for a bullish breakout, the last bearish candle before the strong up-move is the OB
- Validate displacement: the bullish move following the OB candidate must be >= OB_DISPLACEMENT_ATR * ATR
- OB zone = [min(open, close), max(open, close)] of that candle

FVG Detection:
- Scan the breakout displacement sequence (the 3-candle window during the breakout run)
- For each set of 3 consecutive candles in the displacement:
  - Bullish FVG: candle[0].high < candle[2].low AND gap size >= FVG_MIN_SIZE_ATR * ATR
  - Bearish FVG: candle[0].low > candle[2].high AND gap size >= FVG_MIN_SIZE_ATR * ATR
- Take the nearest (most recent) valid FVG — it has the freshest institutional interest
- FVG zone = [candle[0].high, candle[2].low] for bullish, reversed for bearish

Breaker Block Detection:
- Scan prior confirmed OBs in the trade direction history
- An OB becomes a BB if price has since closed through its body
- The BB zone is the same coordinates as the original OB

Zone Ranking:
- Return all valid zones ordered: BB > OB > FVG > Broken SR
- The primary retest zone is the first valid zone in this ranking that has not been invalidated

Zone Invalidation:
- A zone is invalidated the moment a candle closes through its body (not just wicks)

---

## TP ENGINE (tp_engine.py)

Generates and scores candidate take-profit targets.

Target generation:
- All candidates must be visible at entry bar time — no future data
- Generate from: projected range extension, recent swing highs/lows (last 50 bars),
  PDH/PDL, session extremes, EQH/EQL clusters, round numbers, and nearby OB/FVG zones

Scoring:
- Implement the scoring formula defined above
- Return: list of (price_level, score, target_type) sorted by score descending

Target selection:
- Filter: score >= TP_MIN_SCORE
- Filter: RR >= TP_MIN_RR (calculated using entry price and SL)
- Filter: target is in the correct direction (above entry for bullish, below for bearish)
- Select highest-scoring qualifying target
- If DUAL_TP_ENABLED: select top 2

---

## TRADE SIMULATOR (trade_simulator.py)

Bar-by-bar simulation. No vectorized assumptions.

For each bar after entry:
1. Check if low (bullish) or high (bearish) touches SL
2. Check if high (bullish) or low (bearish) touches TP
3. If both in same bar: apply SAME_BAR_RESOLUTION rule
4. If TIMEOUT_BARS reached: close at close price
5. If DUAL_TP_ENABLED: handle partial close at TP1 and trail SL to breakeven

Fill prices:
- Entry: limit order simulation — fill at zone boundary ± spread ± slippage
- SL: market order — fill at SL price + slippage (costs extra)
- TP: limit order — fill at TP price exactly (no additional slippage)

Track per trade:
- entry_price, entry_bar
- exit_price, exit_bar
- exit_reason: TP1 | TP2 | SL | TIMEOUT | INVALIDATED
- R_multiple: (exit_price - entry_price) / (entry_price - SL_price) for longs
- MAE: worst adverse excursion in $ during trade
- MFE: best favorable excursion in $ during trade
- bars_held

---

## METRICS (metrics.py)

Calculate after all trades:
- total_setups_found
- setups_rejected (with breakdown by rejection reason)
- trades_taken
- win_rate, loss_rate, breakeven_rate
- avg_R_win, avg_R_loss
- expectancy_R = (win_rate * avg_R_win) - (loss_rate * abs(avg_R_loss))
- profit_factor = gross_profits / gross_losses
- max_drawdown_R
- longest_win_streak, longest_loss_streak
- avg_bars_held
- by_session: repeat metrics for London / NY only
- by_direction: LONG vs SHORT
- by_zone_type: OB / FVG / BB / SR
- by_breakout_quality: STRONG vs MODERATE
- by_range_quality: premium (>75) vs standard (50–75)
- by_htf_alignment: aligned (HTF trend matches direction) / counter / ranging
- by_structure_break_type: MSS_BULLISH / MSS_BEARISH / BOS_BULLISH / BOS_BEARISH
  — compare win rate and expectancy: MSS setups should outperform BOS-only setups
  — if they don't, log the finding as an anomaly requiring further inspection
- by_ssl_bsl_type: breakdown by sweep type (SSL_RANGE_LOW / SSL_EQUAL_LOWS / etc.)
  — identifies which pool types produce the highest-quality sweeps
- retest_success_rate: % of valid breakouts that retested within timeout
- false_breakout_rate: % of breakout events that reversed inside

---

## REPORT (report.py)

Exports after each iteration:
- `trades_ITER_N.csv`: full trade journal with every tracked field
- `setups_ITER_N.csv`: all candidate setups including rejected ones with rejection reason
- `summary_ITER_N.txt`: human-readable summary of metrics

Print to console after each run:
- Total setups found / rejected / traded
- Win rate and expectancy
- Profit factor
- Max drawdown
- Session breakdown table
- Parameter snapshot that produced these results
- Iteration number

---

## SEQUENTIAL EXECUTION PROTOCOL (run_backtest.py)

This is the most critical section. Follow it exactly and in order.

The engine must execute the following sequence automatically, without human intervention
between steps. After all steps complete, print a final recommendation.

### PRE-RUN CHECKLIST
Before the first iteration begins:
1. Validate API key is set and test with a 10-bar data fetch
2. Confirm cache directory exists and is writable
3. Load or fetch full data for both timeframes for the configured date range
4. Print data summary: total bars, date range covered, missing candle count
5. Compute baseline ATR stats: mean, min, max, 25th/75th percentile for the dataset
   — this confirms your ATR-based parameters are calibrated to actual data

### ITERATION 0 — BASELINE RUN
- Load the default config exactly as specified above
- Run the full backtest on the ENTIRE dataset (in-sample + out-of-sample together)
- Print SUMMARY_0.txt with full metrics
- Print: "BASELINE COMPLETE. Analyzing..."

Analyze BASELINE results. Specifically:
- If total_trades < 30: Parameters may be too strict. Flag for loosening.
- If total_trades > 500: Parameters may be too loose. Flag for tightening.
- If win_rate < 0.35: Entry or zone logic likely has issues. Flag for review.
- If win_rate > 0.70: Possible overfitting in zone or TP scoring. Flag for review.
- If expectancy_R < 0: Strategy is net negative. Must identify root cause.
- If profit_factor < 1.0: Same as above.
- If false_breakout_rate > 0.50: Breakout filter too weak. Flag.
- If retest_success_rate < 0.20: Timeout may be too short or zones misaligned. Flag.

After analysis, print a diagnostic paragraph:
"BASELINE ANALYSIS: [describe what the metrics show, what is strong, what needs work]"

### ITERATION 1 — PARAMETER SENSITIVITY TEST
- Run 4 sub-tests, each changing ONE parameter from the baseline:
  A. Increase PIVOT_LEFT/RIGHT to 4 (from 3) — tighter swings
  B. Decrease BREAKOUT_MIN_BODY_ATR to 0.30 (from 0.40) — more breakouts qualify
  C. Increase RETEST_TIMEOUT_BARS to 50 (from 30) — allow more time to retest
  D. Decrease RANGE_MIN_QUALITY to 40 (from 50) — allow more ranges
- For each sub-test, run the full backtest and record expectancy_R and trade count
- Print a comparison table of all 5 runs (baseline + 4 variants)
- Identify which single change produced the highest improvement in expectancy_R
  WITHOUT significantly increasing false_breakout_rate
- Print: "ITER 1 WINNER: [describe what worked and why it makes structural sense]"

### ITERATION 2 — DIRECTIONAL SPLIT ANALYSIS
- Run the baseline with best ITER 1 parameter applied
- Split results into LONG-only and SHORT-only performance
- Print metrics for each direction separately
- If one direction has expectancy_R < 0: consider disabling that direction
- Print: "DIRECTIONAL ANALYSIS: [describe findings]"
- Decision: ENABLE_LONG, ENABLE_SHORT, or BOTH based on findings

### ITERATION 3 — SESSION ANALYSIS
- Using best config from Iterations 1–2:
- Run London session only
- Run NY session only
- Run both
- Print metrics table comparing sessions
- Print: "SESSION ANALYSIS: [which session performs better and why — consider XAUUSD typical behavior]"
- Disable low-performing sessions if expectancy_R for that session is below -0.10R

### ITERATION 4 — ZONE TYPE AND STRUCTURE TYPE ANALYSIS
- Using best config:
- Print win rate and expectancy by zone type: OB / FVG / BB / SR
- Print win rate and expectancy by structure break type: MSS vs BOS
  — If MSS_BULLISH/MSS_BEARISH trades have materially higher expectancy than
    BOS-only trades (by at least 0.15R), run a sub-test with MSS_REQUIRED = True
    and log whether restricting to MSS improves or worsens overall expectancy
    (note: fewer trades may improve expectancy but reduce opportunity frequency)
  — Print: "MSS vs BOS analysis: [findings and recommendation on MSS_REQUIRED]"
- Print: "ZONE ANALYSIS: [which zone types produce the best results]"
- If a zone type has fewer than 5 trades: insufficient data, do not disable it yet
- If a zone type has >10 trades and negative expectancy: flag for optional disabling

### ITERATION 5 — WALK-FORWARD VALIDATION
- Split data: first 70% = in-sample, last 30% = out-of-sample
- Run the final best config on the IN-SAMPLE period only → record metrics
- Run the exact same config on the OUT-OF-SAMPLE period → record metrics
- Print: "WALK-FORWARD: In-Sample vs Out-of-Sample comparison"
- Healthy result: out-of-sample expectancy is within 30% of in-sample
- Warning result: out-of-sample expectancy is 30–60% below in-sample
- Fail result: out-of-sample expectancy is >60% below OR negative when in-sample is positive
- Print verdict: PASS / WARNING / FAIL with explanation

### FINAL REPORT
After all iterations:
- Print FINAL CONFIGURATION (the best-performing config from the iterative process)
- Print FINAL METRICS from the out-of-sample walk-forward window
- Export final trades to `results/trades_FINAL.csv`
- Export final setups to `results/setups_FINAL.csv`
- Export final config to `results/config_FINAL.txt`
- Print a plain-English summary of whether this strategy is viable, what conditions
  it performs best in, and what risks remain

---

## ANTI-OVERFITTING SAFEGUARDS

You must enforce these rules throughout all iterations:

1. Never optimize a parameter just because it improves win rate in isolation.
   Every parameter change must have a structural explanation.
   Example acceptable: "Increasing PIVOT_RIGHT to 4 reduces false pivot detection
   on 5m XAUUSD because the instrument has frequent single-bar spikes."
   Example unacceptable: "Setting RANGE_MIN_QUALITY to 73.5 maximizes sharpe ratio."

2. Use a minimum of 30 trades before drawing any conclusions from a configuration.
   Fewer than 30 trades: report the metrics but mark results as STATISTICALLY INSUFFICIENT.

3. If a parameter change improves in-sample results but worsens out-of-sample results,
   REJECT that change and revert to the prior value.

4. Round all parameters to sensible increments (e.g., ATR multiples in 0.05 steps,
   bar counts in steps of 5). Do not optimize to arbitrary decimal places.

5. Check for overfitting signature: if STRONG breakouts have much higher win rate than
   MODERATE breakouts, that may be fine (genuine quality filter). But if premium-quality
   ranges have extremely high win rate while standard ranges have negative expectancy,
   the scoring rubric may need rebalancing rather than just filtering one category out.

---

## LOGGING REQUIREMENTS

Every candidate setup (including rejected ones) must produce a log entry with:

```
timestamp, symbol, timeframe, iteration, direction, setup_id,
range_high, range_low, range_quality_score, range_bars, range_touches_high, range_touches_low,
atr_at_setup, range_height_atr,
ssl_bsl_sweep_type,         # e.g. SSL_RANGE_LOW / BSL_EQUAL_HIGHS / etc.
sweep_bar, sweep_wick_size_atr, sweep_wick_size_abs,
structure_break_type,       # MSS_BULLISH / MSS_BEARISH / BOS_BULLISH / BOS_BEARISH
breakout_bar, breakout_direction, breakout_body_atr, breakout_quality,
htf_trend,                  # UPTREND / DOWNTREND / RANGING at time of setup
retest_zone_type, retest_zone_top, retest_zone_bottom,
entry_price, sl_price, tp1_price, tp2_price,
tp_score, rr_tp1, rr_tp2,
rejection_reason,           # empty string if not rejected; gate name if rejected
entry_bar, exit_bar,
exit_reason, exit_price,
r_multiple, mae_abs, mfe_abs,
bars_held, session
```

---

## FINAL DELIVERABLES CHECKLIST

Before considering the engine complete, verify:
- [ ] All modules are separate Python files with clear imports
- [ ] config.py contains ALL configurable parameters (zero hardcoded values in other files)
- [ ] No lookahead bias (testable by verifying that all zone/swing lookups pass only index <= T)
- [ ] Twelve Data API is the only external data source
- [ ] Cache works: second run of same date range makes zero API calls
- [ ] SSL pools and BSL pools are tracked separately — LONG setup requires SSL sweep, SHORT requires BSL
- [ ] MSS and BOS are labeled separately — structure_engine emits distinct flags for each
- [ ] The 10-gate validation function is implemented and runs before every trade open
- [ ] HTF trend filter is ON by default and acts as Gate 1 (first check, first to reject)
- [ ] MSS_REQUIRED flag works: when True, BOS-only setups are rejected at Gate 4
- [ ] At least 5 iterative runs complete with printed summaries
- [ ] Walk-forward split is implemented and reported
- [ ] Trade journal CSV exported for every iteration with ssl_bsl_sweep_type and structure_break_type columns
- [ ] Anti-overfitting rules are enforced in code (not just described in comments)
- [ ] Engine correctly handles: no valid range found, SSL/BSL swept but no MSS/BOS,
      MSS/BOS confirmed but no valid retest zone, valid zone but no TP candidate,
      TP and SL hit same bar, timeout before TP/SL

---

## SAMPLE USAGE

```bash
# Install dependencies
pip install requests pandas numpy pyarrow tqdm

# Set your API key in config.py

# Run full backtest with iterative loop
python run_backtest.py

# Run single iteration with custom config overrides
python run_backtest.py --start 2024-01-01 --end 2024-06-30 --tf 15min

# Re-run from cached data only (no API calls)
python run_backtest.py --cache-only
```

---

## FINAL NOTE TO THE AI BUILDER

You are not building a demo. You are building a tool that a live trader will use to
make decisions about real money on XAUUSD.

This means:
- A bug in lookahead logic produces fake backtest results that lead to real losses.
- A TP engine that places targets at unreachable levels inflates win rate.
- A sweep filter that is too loose counts noise as signals and destroys edge.
- A sweep filter that is too tight means the engine misses the entire strategy.

The iterative loop is not optional polish — it is how you find the real parameter
ranges that work versus the ones that looked good in your head. Run every iteration.
Read the output. Explain what you see. Adjust one thing at a time.

When you are done, a serious trader reading your final summary should be able to
answer these questions from your output:
1. How many valid setups does this strategy find per week on XAUUSD 5m/15m?
2. What is the realistic expectancy after spread and slippage?
3. Which session performs better and by how much?
4. What is the most common reason setups are rejected?
5. What is the out-of-sample performance compared to in-sample?

If your output cannot answer all five questions, the engine is incomplete.
