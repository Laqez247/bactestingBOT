"""
data_loader.py — Fetches and caches XAUUSD OHLC data from Twelve Data API.
Supports parallel fetching with multiple API keys across date-range segments.
Zero hardcoded values — all params come from config.py.
"""

import os
import time
import logging
import requests
import threading
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Tuple
import config

try:
    import polars as pl
except ImportError:  # Optional acceleration dependency.
    pl = None

logger = logging.getLogger(__name__)


def _cache_path(symbol: str, interval: str, start: str, end: str) -> str:
    safe_sym = symbol.replace("/", "_")
    return os.path.join(config.CACHE_DIR, f"{safe_sym}_{interval}_{start}_{end}.csv")


def _save_cache(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    df.to_csv(path, index=True)


def _load_cache(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None

    if getattr(config, "USE_POLARS_IO", True) and pl is not None:
        df_pl = pl.scan_csv(path, try_parse_dates=True).collect()
        ts_col = df_pl.columns[0]
        df = df_pl.to_pandas()
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
        df = df.set_index(ts_col)
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)

    df = df.sort_index(ascending=True)
    return df


def _validate_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate OHLC integrity: positive prices, high >= open/close >= low.
    Drops invalid rows and logs them.
    """
    before = len(df)
    mask = (
        (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0) &
        (df["high"] >= df["open"]) & (df["high"] >= df["close"]) &
        (df["low"] <= df["open"]) & (df["low"] <= df["close"])
    )
    df = df[mask].copy()
    dropped = before - len(df)
    if dropped > 0:
        logger.warning(f"Dropped {dropped} invalid OHLC rows")
    return df


def _detect_gaps(df: pd.DataFrame, interval_minutes: int) -> int:
    """
    Detect and log missing candles (gaps > 2x normal interval).
    Does NOT interpolate — just logs and counts.
    """
    if len(df) < 2:
        return 0
    diffs = df.index.to_series().diff().dropna()
    threshold = timedelta(minutes=interval_minutes * 2)
    gaps = diffs[diffs > threshold]
    gap_count = len(gaps)
    if gap_count > 0:
        logger.info(f"Detected {gap_count} candle gaps (>{interval_minutes*2}min)")
    return gap_count


def _interval_to_minutes(interval: str) -> int:
    mapping = {
        "1min": 1, "5min": 5, "15min": 15, "30min": 30,
        "1h": 60, "4h": 240, "1day": 1440
    }
    return mapping.get(interval, 5)


def _fetch_segment(
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
    api_key: str,
    max_retries: int = None,
    retry_delay: float = None
) -> Optional[pd.DataFrame]:
    """
    Fetch one date-range segment from Twelve Data API.
    Returns DataFrame or None on failure.
    """
    if max_retries is None:
        max_retries = config.MAX_RETRIES
    if retry_delay is None:
        retry_delay = config.RETRY_DELAY_SECONDS

    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str   = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    params = {
        "symbol":     symbol,
        "interval":   interval,
        "start_date": start_str,
        "end_date":   end_str,
        "outputsize": config.API_OUTPUT_SIZE,
        "timezone":   "UTC",
        "format":     "JSON",
        "apikey":     api_key,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.get(config.API_BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "error":
                msg = data.get("message", "unknown error")
                logger.error(f"API error: {msg}")
                if "rate limit" in msg.lower() or "too many" in msg.lower():
                    time.sleep(retry_delay * (attempt + 2))
                    continue
                return None

            values = data.get("values", [])
            if not values:
                logger.warning(f"No data returned for {symbol} {interval} {start_str}–{end_str}")
                return pd.DataFrame()

            df = pd.DataFrame(values)
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
            df = df.set_index("datetime")
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if "volume" in df.columns:
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

            df = df.sort_index(ascending=True)
            df = df[~df.index.duplicated(keep="first")]
            return df

        except requests.exceptions.RequestException as e:
            logger.warning(f"Request error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))

    logger.error(f"Failed to fetch {symbol} {interval} after {max_retries} attempts")
    return None


def _split_date_range(start: datetime, end: datetime, n_segments: int):
    """
    Split [start, end] into n_segments equal date-range chunks.
    Returns list of (seg_start, seg_end) tuples.
    """
    total_seconds = (end - start).total_seconds()
    seg_seconds = total_seconds / n_segments
    segments = []
    for i in range(n_segments):
        s = start + timedelta(seconds=i * seg_seconds)
        e = start + timedelta(seconds=(i + 1) * seg_seconds)
        if i == n_segments - 1:
            e = end
        segments.append((s, e))
    return segments


def fetch_ohlc(
    symbol: str,
    interval: str,
    start: str,
    end: str,
    use_cache: bool = True,
    cache_only: bool = False
) -> pd.DataFrame:
    """
    Fetch OHLC data for symbol+interval over [start, end].
    Uses multi-key parallel fetching if needed.
    Caches results to disk. Returns sorted, validated DataFrame.
    """
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    cache_path = _cache_path(symbol, interval, start, end)

    if use_cache:
        cached = _load_cache(cache_path)
        if cached is not None and len(cached) > 0:
            logger.info(f"Cache hit: {cache_path} ({len(cached)} bars)")
            return cached

    if cache_only:
        raise RuntimeError(f"Cache miss and --cache-only is set: {cache_path}")

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=None)
    end_dt   = datetime.strptime(end,   "%Y-%m-%d").replace(tzinfo=None)
    end_dt   = end_dt.replace(hour=23, minute=59, second=59)

    api_keys = config.TWELVE_DATA_API_KEYS
    n_keys   = len(api_keys)
    interval_minutes = _interval_to_minutes(interval)

    # Estimate bars needed
    total_minutes = (end_dt - start_dt).total_seconds() / 60
    # Forex is ~5 days/week, ~16 trading hours/day roughly
    trading_fraction = (5 / 7) * (17 / 24)
    est_bars = int((total_minutes / interval_minutes) * trading_fraction)
    n_segments = max(1, min(n_keys, (est_bars // config.API_OUTPUT_SIZE) + 1))

    logger.info(f"Fetching {symbol} {interval} {start}→{end} | "
                f"~{est_bars} bars | {n_segments} segments across {n_keys} keys")

    segments = _split_date_range(start_dt, end_dt, n_segments)
    results = [None] * n_segments

    def fetch_worker(idx, seg_start, seg_end, api_key):
        logger.info(f"  Segment {idx}: {seg_start.date()} → {seg_end.date()} [key ...{api_key[-6:]}]")
        # Rate-limit: stagger start by 0.5s per segment to avoid burst
        time.sleep(idx * 0.5)
        df = _fetch_segment(symbol, interval, seg_start, seg_end, api_key)
        results[idx] = df

    threads = []
    for i, (seg_start, seg_end) in enumerate(segments):
        key = api_keys[i % n_keys]
        t = threading.Thread(target=fetch_worker, args=(i, seg_start, seg_end, key))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Concatenate all segments
    valid_dfs = [r for r in results if r is not None and len(r) > 0]
    if not valid_dfs:
        raise RuntimeError(f"No data returned for {symbol} {interval}")

    df = pd.concat(valid_dfs)
    df = df.sort_index(ascending=True)
    df = df[~df.index.duplicated(keep="first")]
    df = _validate_ohlc(df)

    gap_count = _detect_gaps(df, interval_minutes)

    logger.info(f"Fetched {len(df)} bars | {gap_count} gaps detected")
    _save_cache(df, cache_path)

    return df


def load_data(
    start: str = None,
    end: str = None,
    use_cache: bool = True,
    cache_only: bool = False
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load both EXECUTION_TF and STRUCTURE_TF DataFrames.
    Returns (df_exec, df_htf).
    Prints data summary to stdout.
    """
    start = start or config.BACKTEST_START
    end   = end   or config.BACKTEST_END

    symbol = config.SYMBOL

    print(f"\n{'='*60}")
    print(f"DATA LOADER — {symbol}")
    print(f"Range: {start} → {end}")
    print(f"Execution TF: {config.EXECUTION_TF} | Structure TF: {config.STRUCTURE_TF}")
    print(f"{'='*60}")

    df_exec = fetch_ohlc(symbol, config.EXECUTION_TF, start, end, use_cache, cache_only)
    df_htf  = fetch_ohlc(symbol, config.STRUCTURE_TF,  start, end, use_cache, cache_only)

    # Print data summary
    print(f"\nEXECUTION TF ({config.EXECUTION_TF}):")
    print(f"  Total bars   : {len(df_exec)}")
    print(f"  Date range   : {df_exec.index[0]} → {df_exec.index[-1]}")
    missing_exec = _detect_gaps(df_exec, _interval_to_minutes(config.EXECUTION_TF))
    print(f"  Missing gaps : {missing_exec}")

    print(f"\nSTRUCTURE TF ({config.STRUCTURE_TF}):")
    print(f"  Total bars   : {len(df_htf)}")
    print(f"  Date range   : {df_htf.index[0]} → {df_htf.index[-1]}")
    missing_htf = _detect_gaps(df_htf, _interval_to_minutes(config.STRUCTURE_TF))
    print(f"  Missing gaps : {missing_htf}")

    return df_exec, df_htf


def test_api_connection() -> bool:
    """
    Test API connectivity with a 10-bar data fetch.
    Returns True if successful.
    """
    api_key = config.TWELVE_DATA_API_KEYS[0]
    params = {
        "symbol":     config.SYMBOL,
        "interval":   config.EXECUTION_TF,
        "outputsize": 10,
        "timezone":   "UTC",
        "format":     "JSON",
        "apikey":     api_key,
    }
    try:
        resp = requests.get(config.API_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            print(f"API ERROR: {data.get('message')}")
            return False
        values = data.get("values", [])
        if len(values) == 0:
            print("API WARNING: Returned 0 bars on test fetch")
            return False
        print(f"API OK: Received {len(values)} test bars. Latest: {values[0].get('datetime')}")
        return True
    except Exception as e:
        print(f"API CONNECTION FAILED: {e}")
        return False


def compute_baseline_atr_stats(df: pd.DataFrame, atr_period: int = None) -> dict:
    """
    Compute ATR stats across the full dataset for parameter calibration.
    Returns dict with mean, min, max, p25, p75.
    """
    if atr_period is None:
        atr_period = config.ATR_PERIOD
    from structure_engine import compute_atr
    atr = compute_atr(df, atr_period)
    atr_clean = atr.dropna()
    stats = {
        "mean":  round(float(atr_clean.mean()), 4),
        "min":   round(float(atr_clean.min()),  4),
        "max":   round(float(atr_clean.max()),  4),
        "p25":   round(float(atr_clean.quantile(0.25)), 4),
        "p75":   round(float(atr_clean.quantile(0.75)), 4),
        "count": int(len(atr_clean)),
    }
    return stats
