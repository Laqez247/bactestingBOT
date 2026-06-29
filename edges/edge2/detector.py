# edges/edge2/detector.py
import logging
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from analytics.logger import log_zone_event
from config import settings
from indicators.core import atr, rolling_range, body_ratio, wick_ratio, swing_point
from state.models import CompressionZone, Edge2Signal, BreakoutClass, RegimeState

logger = logging.getLogger(__name__)


def detect_compression_zones(df_m15: pd.DataFrame) -> List[CompressionZone]:
    atr_series = atr(df_m15, 14)
    range_series = rolling_range(df_m15, 20)
    zones: List[CompressionZone] = []

    for end_bar in range(19, len(df_m15)):
        atr_at_detection = atr_series.iloc[end_bar]
        range_value = range_series.iloc[end_bar]
        if pd.isna(atr_at_detection) or pd.isna(range_value):
            continue

        if range_value / atr_at_detection > settings.E2_COMPRESSION_MULT:
            continue

        window = df_m15.iloc[end_bar - 19:end_bar + 1]
        range_low = window['low'].min()
        range_high = window['high'].max()
        range_height = range_high - range_low

        if range_height < settings.E2_RANGE_MIN_ATR_MULT * atr_at_detection:
            continue
        if range_height > settings.E2_RANGE_MAX_ATR_MULT * atr_at_detection:
            continue

        zone = CompressionZone(
            start_bar=end_bar - 19,
            end_bar=end_bar,
            range_high=range_high,
            range_low=range_low,
            range_height=range_height,
            atr_at_detection=atr_at_detection
        )
        zones.append(zone)

    return zones


def classify_breakout(row: pd.Series, compression_zone: CompressionZone, direction: str) -> BreakoutClass:
    total_range = row['high'] - row['low']
    if total_range == 0:
        return BreakoutClass.FAKEOUT

    if body_ratio(row) < settings.E2_FAKEOUT_BODY_MIN or wick_ratio(row, direction) > settings.E2_FAKEOUT_WICK_RATIO:
        return BreakoutClass.FAKEOUT

    if direction == 'LONG':
        if (row['close'] - row['low']) / total_range >= settings.E2_CLASS_A_CLOSE_MULT:
            return BreakoutClass.A
    else:
        if (row['high'] - row['close']) / total_range >= settings.E2_CLASS_A_CLOSE_MULT:
            return BreakoutClass.A

    return BreakoutClass.B


def detect_edge2(df_m15: pd.DataFrame,
                 current_bar: int,
                 regime: RegimeState,
                 bot_state,
                 compression_zones: List[CompressionZone]) -> Optional[Edge2Signal]:
    bot_state.e2_reject_reason = ''
    bot_state.e2_last_zone_event = None
    current_row = df_m15.iloc[current_bar]
    current_close = float(current_row['close'])
    atr_value = atr(df_m15, 14).iloc[current_bar]
    ts = datetime.now(timezone.utc)

    # Use stable (start_bar, end_bar) tuple keys — id() is ephemeral across cycles
    if not hasattr(bot_state, 'e2_used_zone_keys') or bot_state.e2_used_zone_keys is None:
        bot_state.e2_used_zone_keys = set()

    valid_zones = [
        z for z in compression_zones
        if z.end_bar < current_bar
        and (z.start_bar, z.end_bar) not in bot_state.e2_used_zone_keys
    ]
    if not valid_zones:
        bot_state.e2_reject_reason = 'No valid compression zone found'
        bot_state.e2_watch_zone = None
        # Log NO_ZONE event so the absence of a zone is also auditable
        bot_state.e2_last_zone_event = log_zone_event(
            zone_event_type='NO_ZONE',
            zone_event_reason='No unused compression zone found in current M15 history',
            zone=None,
            zone_age_bars=0,
            current_price=current_close,
            timestamp=ts,
        )
        logger.info('E2 rejected — no valid compression zone found')
        return None

    zone = max(valid_zones, key=lambda z: z.end_bar)

    # ── Zone expiry — expire zones older than E2_TIMEOUT_BARS ──────────────
    # E2_TIMEOUT_BARS (72) borrowed from trade timeout until research specifies
    # a separate zone timeout parameter.
    zone_age = current_bar - zone.end_bar
    if zone_age >= settings.E2_TIMEOUT_BARS:
        bot_state.e2_used_zone_keys.add((zone.start_bar, zone.end_bar))
        reason = (
            f'Zone aged out: {zone_age} bars since detection end_bar={zone.end_bar}. '
            f'Timeout={settings.E2_TIMEOUT_BARS} bars. '
            f'Zone boundaries: high={zone.range_high:.2f} low={zone.range_low:.2f}.'
        )
        bot_state.e2_reject_reason = f'Zone expired: age={zone_age} bars >= {settings.E2_TIMEOUT_BARS}'
        bot_state.e2_watch_zone = None
        bot_state.e2_last_zone_event = log_zone_event(
            zone_event_type='EXPIRED',
            zone_event_reason=reason,
            zone=zone,
            zone_age_bars=zone_age,
            current_price=current_close,
            timestamp=ts,
        )
        logger.info('E2 rejected — zone expired: age=%s bars >= timeout=%s', zone_age, settings.E2_TIMEOUT_BARS)
        return None

    long_break = current_close > zone.range_high
    short_break = current_close < zone.range_low

    # ── Price still inside zone → maintain WATCH ───────────────────────────
    if not long_break and not short_break:
        bot_state.e2_watch_zone = zone
        bot_state.e2_reject_reason = 'Compression zone active — awaiting breakout'
        reason = (
            f'Price {current_close:.2f} inside zone '
            f'[{zone.range_low:.2f} – {zone.range_high:.2f}]. '
            f'Age: {zone_age} bars. Awaiting breakout.'
        )
        bot_state.e2_last_zone_event = log_zone_event(
            zone_event_type='WATCH_CONTINUE',
            zone_event_reason=reason,
            zone=zone,
            zone_age_bars=zone_age,
            current_price=current_close,
            timestamp=ts,
        )
        logger.info(
            'E2 WATCH — price inside zone: close=%s zone_high=%s zone_low=%s age=%s bars',
            current_close, zone.range_high, zone.range_low, zone_age
        )
        return None

    # ── Price has broken the zone boundary — zone is consumed ──────────────
    bot_state.e2_watch_zone = None
    direction = 'LONG' if long_break else 'SHORT'
    breakout_class = classify_breakout(current_row, zone, direction)

    # ── FAKEOUT — mark zone used, log event, return None ───────────────────
    if breakout_class == BreakoutClass.FAKEOUT:
        bot_state.e2_used_zone_keys.add((zone.start_bar, zone.end_bar))
        breach_side = 'above' if long_break else 'below'
        body = body_ratio(current_row)
        wick = wick_ratio(current_row, direction)
        reason = (
            f'Price {current_close:.2f} closed {breach_side} zone boundary '
            f'({zone.range_high:.2f} high / {zone.range_low:.2f} low) '
            f'but candle failed quality filter. '
            f'body_ratio={body:.3f} (min={settings.E2_FAKEOUT_BODY_MIN}), '
            f'wick_ratio={wick:.3f} (max={settings.E2_FAKEOUT_WICK_RATIO}). '
            f'Zone age: {zone_age} bars.'
        )
        bot_state.e2_reject_reason = f'Fakeout detected: direction={direction}'
        bot_state.e2_last_zone_event = log_zone_event(
            zone_event_type='FAKEOUT',
            zone_event_reason=reason,
            zone=zone,
            zone_age_bars=zone_age,
            current_price=current_close,
            direction=direction,
            breakout_class='FAKEOUT',
            timestamp=ts,
        )
        logger.info('E2 rejected — fakeout detected: direction=%s body=%.3f wick=%.3f', direction, body, wick)
        return None

    if current_bar < settings.E2_SL_SWING_LOOKBACK - 1:
        bot_state.e2_reject_reason = 'Insufficient history for swing point'
        logger.info('E2 rejected — insufficient history for swing point')
        return None

    swing = swing_point(
        df_m15.iloc[current_bar - settings.E2_SL_SWING_LOOKBACK + 1: current_bar + 1],
        settings.E2_SL_SWING_LOOKBACK,
        'long' if direction == 'LONG' else 'short'
    ).iloc[-1]

    if direction == 'LONG':
        stop_loss = swing - (settings.E2_SL_ATR_BUFFER * atr_value)
    else:
        stop_loss = swing + (settings.E2_SL_ATR_BUFFER * atr_value)

    stop_distance = abs(current_close - stop_loss)
    take_profit = current_close + (
        settings.E2_RR * stop_distance if direction == 'LONG'
        else -settings.E2_RR * stop_distance
    )
    timeout_bar = current_bar + settings.E2_TIMEOUT_BARS
    dollar_risk = stop_distance * settings.USD_PER_POINT

    # Mark zone as used with stable key
    bot_state.e2_used_zone_keys.add((zone.start_bar, zone.end_bar))

    # ── Valid breakout — log BREAKOUT_TRADE event ───────────────────────────
    breach_side = 'above' if long_break else 'below'
    reason = (
        f'Price {current_close:.2f} closed {breach_side} zone boundary '
        f'({zone.range_high:.2f} high / {zone.range_low:.2f} low). '
        f'Class {breakout_class.value} breakout confirmed. '
        f'Direction: {direction}. '
        f'Entry={current_close:.2f} SL={float(stop_loss):.2f} TP={float(take_profit):.2f}. '
        f'Zone age: {zone_age} bars.'
    )
    bot_state.e2_last_zone_event = log_zone_event(
        zone_event_type='BREAKOUT_TRADE',
        zone_event_reason=reason,
        zone=zone,
        zone_age_bars=zone_age,
        current_price=current_close,
        direction=direction,
        breakout_class=breakout_class.value,
        timestamp=ts,
    )
    logger.info(
        'E2 BREAKOUT — class=%s direction=%s entry=%s sl=%s tp=%s age=%s bars',
        breakout_class.value, direction, current_close, float(stop_loss), float(take_profit), zone_age
    )

    return Edge2Signal(
        timestamp=current_row['timestamp'],
        direction=direction,
        breakout_class=breakout_class,
        entry_price=current_close,
        stop_loss=stop_loss,
        take_profit=take_profit,
        stop_distance=stop_distance,
        dollar_risk=dollar_risk,
        timeout_bar=timeout_bar,
        compression_high=zone.range_high,
        compression_low=zone.range_low,
        atr=atr_value,
        session=regime.session.value,
        sizing_factor=1.0,
        adjusted_risk=0.0,
        overlap_active=False,
        e2_short_suppressed=False
    )
