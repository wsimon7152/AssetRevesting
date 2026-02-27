# asset_revesting/core/indicators.py
"""
Layer 2 — Indicator Engine

Computes all technical indicators defined in Signal Logic Spec v1.1 Section 2:
- Simple Moving Averages (5, 20, 50, 150, 200)
- SMA Slopes
- Bollinger Bands (20-period, 2 std dev)
- VIX Classification & Trend
- NYSE Up/Down Volume Ratios (Panic & FOMO)
- Relative Strength (distance from 50-SMA)

All calculations use closing prices. Results are stored in SQLite.
"""

import pandas as pd
import numpy as np
from asset_revesting.config import (
    SMA_PERIODS, BB_PERIOD, BB_STD_DEV, SLOPE_LOOKBACK,
    RELATIVE_STRENGTH_SMA,
    VIX_LOW, VIX_NORMAL, VIX_ELEVATED, VIX_HIGH,
    VIX_TREND_FAST, VIX_TREND_SLOW, VIX_SPIKE_THRESHOLD,
    VIX_EMERGENCY_LEVEL,
    VOLUME_RATIO_MA_PERIOD,
    ANALYSIS_SYMBOLS,
    ATR_PERIOD
)
from asset_revesting.data.database import get_connection


# Lazy import for ingestion functions (avoids pulling in yfinance at import time)
def _get_ingestion():
    from asset_revesting.data import ingestion
    return ingestion


# =============================================================================
# PURE CALCULATION FUNCTIONS (no DB dependency — testable, backtestable)
# =============================================================================

def calc_sma(series, period):
    """
    Simple Moving Average.
    
    Args:
        series: pd.Series of closing prices
        period: number of days
    
    Returns:
        pd.Series of SMA values (NaN for insufficient data)
    """
    return series.rolling(window=period, min_periods=period).mean()


def calc_sma_slope(sma_series, lookback):
    """
    Percentage change in SMA over lookback days.
    
    Spec: sma_slope = (current - prior) / prior * 100
    
    Args:
        sma_series: pd.Series of SMA values
        lookback: number of days to measure slope over
    
    Returns:
        pd.Series of slope percentages
    """
    prior = sma_series.shift(lookback)
    return ((sma_series - prior) / prior) * 100


def calc_bollinger_bands(close_series, period=BB_PERIOD, num_std=BB_STD_DEV):
    """
    Bollinger Bands: middle (SMA), upper, lower, bandwidth, %B.
    
    Args:
        close_series: pd.Series of closing prices
        period: SMA period (default 20)
        num_std: number of standard deviations (default 2.0)
    
    Returns:
        dict of pd.Series: {middle, upper, lower, bandwidth, percent_b}
    """
    middle = calc_sma(close_series, period)
    std = close_series.rolling(window=period, min_periods=period).std()
    
    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    bandwidth = ((upper - lower) / middle) * 100
    
    # Avoid division by zero
    band_range = upper - lower
    percent_b = (close_series - lower) / band_range.replace(0, np.nan)
    
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,
        "percent_b": percent_b,
    }


def calc_relative_strength(close_series, sma_period=RELATIVE_STRENGTH_SMA):
    """
    Distance from SMA as percentage of SMA value.
    
    Spec: (close - SMA) / SMA * 100
    
    Args:
        close_series: pd.Series of closing prices
        sma_period: SMA to measure distance from (default 50)
    
    Returns:
        pd.Series of relative strength values
    """
    sma = calc_sma(close_series, sma_period)
    return ((close_series - sma) / sma) * 100


def calc_atr(high_series, low_series, close_series, period=ATR_PERIOD):
    """
    Average True Range (Wilder's ATR).

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder's smoothed average of TR over `period` days.

    Args:
        high_series:  pd.Series of daily highs
        low_series:   pd.Series of daily lows
        close_series: pd.Series of daily closes
        period:       smoothing period (default ATR_PERIOD=14)

    Returns:
        pd.Series of ATR values (NaN for first `period` rows)
    """
    prev_close = close_series.shift(1)
    tr = pd.concat([
        high_series - low_series,
        (high_series - prev_close).abs(),
        (low_series  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder's smoothing: seed with simple mean, then exponential decay
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def classify_vix(vix_value):
    """
    Classify a single VIX value into a regime.
    
    Returns:
        str: "LOW", "NORMAL", "ELEVATED", "HIGH", or "EXTREME"
    """
    if pd.isna(vix_value):
        return None
    if vix_value < VIX_LOW:
        return "LOW"
    elif vix_value < VIX_NORMAL:
        return "NORMAL"
    elif vix_value < VIX_ELEVATED:
        return "ELEVATED"
    elif vix_value < VIX_HIGH:
        return "HIGH"
    else:
        return "EXTREME"


def calc_vix_indicators(vix_series):
    """
    Compute all VIX-derived indicators.
    
    Args:
        vix_series: pd.Series of VIX closing values (index=date)
    
    Returns:
        pd.DataFrame with columns:
            vix_close, vix_regime, vix_sma_5, vix_sma_20,
            vix_trend, vix_daily_change, vix_spike
    """
    df = pd.DataFrame(index=vix_series.index)
    df["vix_close"] = vix_series
    
    # Regime classification
    df["vix_regime"] = vix_series.apply(classify_vix)
    
    # VIX SMAs for trend
    df["vix_sma_5"] = calc_sma(vix_series, VIX_TREND_FAST)
    df["vix_sma_20"] = calc_sma(vix_series, VIX_TREND_SLOW)
    
    # Trend: RISING if fast SMA > slow SMA
    df["vix_trend"] = np.where(
        df["vix_sma_5"] > df["vix_sma_20"], "RISING", "FALLING"
    )
    # Handle NaN cases
    df.loc[df["vix_sma_5"].isna() | df["vix_sma_20"].isna(), "vix_trend"] = None
    
    # Daily change (%)
    df["vix_daily_change"] = vix_series.pct_change() * 100
    
    # Spike detection
    df["vix_spike"] = (df["vix_daily_change"] > VIX_SPIKE_THRESHOLD).astype(int)
    
    return df


def calc_volume_ratios(up_volume_series, down_volume_series):
    """
    Compute panic ratio, FOMO ratio, and their moving averages.
    
    Spec:
        panic_ratio = down_volume / up_volume  (≥3 = panic)
        fomo_ratio  = up_volume / down_volume  (≥3 = FOMO)
    
    Args:
        up_volume_series: pd.Series of NYSE up volume
        down_volume_series: pd.Series of NYSE down volume
    
    Returns:
        pd.DataFrame with columns:
            panic_ratio, fomo_ratio, panic_ratio_ma, fomo_ratio_ma
    """
    df = pd.DataFrame(index=up_volume_series.index)
    
    # Avoid division by zero
    up_safe = up_volume_series.replace(0, np.nan)
    down_safe = down_volume_series.replace(0, np.nan)
    
    df["panic_ratio"] = down_volume_series / up_safe
    df["fomo_ratio"] = up_volume_series / down_safe
    
    # 20-day moving averages for smoothed trend
    df["panic_ratio_ma"] = calc_sma(df["panic_ratio"], VOLUME_RATIO_MA_PERIOD)
    df["fomo_ratio_ma"] = calc_sma(df["fomo_ratio"], VOLUME_RATIO_MA_PERIOD)
    
    return df


# =============================================================================
# SYMBOL-LEVEL INDICATOR COMPUTATION
# =============================================================================

def compute_symbol_indicators(close_series, high_series=None, low_series=None):
    """
    Compute all indicators for a single symbol's closing prices.

    Args:
        close_series: pd.Series of closing prices (index=date)
        high_series:  pd.Series of daily highs (optional; needed for ATR)
        low_series:   pd.Series of daily lows  (optional; needed for ATR)

    Returns:
        pd.DataFrame with all indicator columns
    """
    df = pd.DataFrame(index=close_series.index)
    df["close"] = close_series

    # SMAs
    for period in SMA_PERIODS:
        df[f"sma_{period}"] = calc_sma(close_series, period)

    # SMA Slopes (for stage analysis)
    df["sma_150_slope"] = calc_sma_slope(df["sma_150"], SLOPE_LOOKBACK)
    df["sma_200_slope"] = calc_sma_slope(df["sma_200"], SLOPE_LOOKBACK)
    df["sma_50_slope"] = calc_sma_slope(df["sma_50"], SLOPE_LOOKBACK)

    # Bollinger Bands
    bb = calc_bollinger_bands(close_series)
    df["bb_upper"] = bb["upper"]
    df["bb_middle"] = bb["middle"]
    df["bb_lower"] = bb["lower"]
    df["bb_bandwidth"] = bb["bandwidth"]
    df["bb_percent_b"] = bb["percent_b"]

    # Relative Strength
    df["relative_strength"] = calc_relative_strength(close_series)

    # ATR (requires high/low; gracefully skipped if not provided)
    if high_series is not None and low_series is not None:
        df["atr_14"] = calc_atr(high_series, low_series, close_series)
    else:
        df["atr_14"] = np.nan

    return df


# =============================================================================
# DATABASE STORAGE
# =============================================================================

def store_symbol_indicators(symbol, indicators_df, db_path=None):
    """
    Store computed indicators for a symbol in SQLite.
    
    Args:
        symbol: ticker symbol
        indicators_df: DataFrame from compute_symbol_indicators()
        db_path: override database path
    """
    with get_connection(db_path) as conn:
        for date_idx, row in indicators_df.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d")
            conn.execute("""
                INSERT OR REPLACE INTO indicators
                (symbol, date, sma_5, sma_20, sma_50, sma_150, sma_200,
                 sma_150_slope, sma_200_slope, sma_50_slope,
                 bb_upper, bb_middle, bb_lower, bb_bandwidth, bb_percent_b,
                 relative_strength, atr_14)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, date_str,
                _safe_float(row.get("sma_5")),
                _safe_float(row.get("sma_20")),
                _safe_float(row.get("sma_50")),
                _safe_float(row.get("sma_150")),
                _safe_float(row.get("sma_200")),
                _safe_float(row.get("sma_150_slope")),
                _safe_float(row.get("sma_200_slope")),
                _safe_float(row.get("sma_50_slope")),
                _safe_float(row.get("bb_upper")),
                _safe_float(row.get("bb_middle")),
                _safe_float(row.get("bb_lower")),
                _safe_float(row.get("bb_bandwidth")),
                _safe_float(row.get("bb_percent_b")),
                _safe_float(row.get("relative_strength")),
                _safe_float(row.get("atr_14")),
            ))


def store_vix_indicators(vix_df, db_path=None):
    """Store VIX indicators in SQLite."""
    with get_connection(db_path) as conn:
        for date_idx, row in vix_df.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d")
            conn.execute("""
                INSERT OR REPLACE INTO vix_indicators
                (date, vix_close, vix_regime, vix_sma_5, vix_sma_20,
                 vix_trend, vix_daily_change, vix_spike)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                _safe_float(row.get("vix_close")),
                row.get("vix_regime"),
                _safe_float(row.get("vix_sma_5")),
                _safe_float(row.get("vix_sma_20")),
                row.get("vix_trend"),
                _safe_float(row.get("vix_daily_change")),
                int(row.get("vix_spike", 0)) if pd.notna(row.get("vix_spike")) else None,
            ))


def store_volume_indicators(volume_df, db_path=None):
    """Store volume ratio indicators in SQLite."""
    with get_connection(db_path) as conn:
        for date_idx, row in volume_df.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d")
            conn.execute("""
                INSERT OR REPLACE INTO volume_indicators
                (date, panic_ratio, fomo_ratio, panic_ratio_ma, fomo_ratio_ma)
                VALUES (?, ?, ?, ?, ?)
            """, (
                date_str,
                _safe_float(row.get("panic_ratio")),
                _safe_float(row.get("fomo_ratio")),
                _safe_float(row.get("panic_ratio_ma")),
                _safe_float(row.get("fomo_ratio_ma")),
            ))


# =============================================================================
# MAIN COMPUTATION PIPELINE
# =============================================================================

def compute_all_indicators(start_date=None, end_date=None, db_path=None):
    """
    Compute all indicators for all analysis symbols and store in SQLite.
    This is the main entry point for Layer 2.
    
    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter
        db_path: Override database path
    
    Returns:
        dict: Summary of computations performed
    """
    results = {}
    ingestion = _get_ingestion()
    
    # --- Symbol indicators (SMAs, Bollinger, Relative Strength) ---
    print("\nComputing symbol indicators...")
    
    # Include warning symbols for intermarket analysis
    from asset_revesting.config import WARNING_SYMBOLS
    all_symbols = ANALYSIS_SYMBOLS + WARNING_SYMBOLS
    
    for symbol in all_symbols:
        try:
            price_df = ingestion.get_price_dataframe(symbol, start_date, end_date, db_path)
            
            if price_df.empty:
                print(f"  {symbol}: No price data available")
                results[symbol] = 0
                continue
            
            indicators = compute_symbol_indicators(
                price_df["close"],
                high_series=price_df.get("high"),
                low_series=price_df.get("low"),
            )
            store_symbol_indicators(symbol, indicators, db_path)
            
            # Count non-NaN rows (valid indicator values)
            valid_rows = indicators["sma_200"].notna().sum()
            results[symbol] = int(valid_rows)
            print(f"  {symbol}: {valid_rows} rows with full indicators (of {len(indicators)} total)")
            
        except Exception as e:
            print(f"  ERROR computing {symbol}: {e}")
            results[symbol] = 0
    
    # --- VIX indicators ---
    print("\nComputing VIX indicators...")
    try:
        vix_df = ingestion.get_vix_dataframe(start_date, end_date, db_path)
        if not vix_df.empty:
            vix_indicators = calc_vix_indicators(vix_df["close"])
            store_vix_indicators(vix_indicators, db_path)
            valid = vix_indicators["vix_sma_20"].notna().sum()
            results["VIX"] = int(valid)
            print(f"  VIX: {valid} rows with full indicators")
        else:
            print("  VIX: No data available")
            results["VIX"] = 0
    except Exception as e:
        print(f"  ERROR computing VIX indicators: {e}")
        results["VIX"] = 0
    
    # --- Volume ratios ---
    print("\nComputing volume ratios...")
    try:
        vol_df = ingestion.get_nyse_volume_dataframe(start_date, end_date, db_path)
        if not vol_df.empty:
            volume_indicators = calc_volume_ratios(vol_df["up_volume"], vol_df["down_volume"])
            store_volume_indicators(volume_indicators, db_path)
            valid = volume_indicators["panic_ratio_ma"].notna().sum()
            results["volume_ratios"] = int(valid)
            print(f"  Volume ratios: {valid} rows with full indicators")
        else:
            print("  Volume ratios: No NYSE volume data (run daily update after market close)")
            results["volume_ratios"] = 0
    except Exception as e:
        print(f"  ERROR computing volume ratios: {e}")
        results["volume_ratios"] = 0
    
    return results


# =============================================================================
# RETRIEVAL FUNCTIONS (for use by Layer 3 — Signal Generator)
# =============================================================================

def get_latest_indicators(symbol, as_of_date=None, db_path=None):
    """
    Get the most recent indicator values for a symbol.
    If as_of_date is provided, get indicators as of that date (for backtesting).
    
    Returns:
        dict or None: All indicator values for the date
    """
    with get_connection(db_path) as conn:
        if as_of_date:
            row = conn.execute("""
                SELECT * FROM indicators
                WHERE symbol = ? AND date <= ?
                ORDER BY date DESC LIMIT 1
            """, (symbol, as_of_date)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM indicators
                WHERE symbol = ?
                ORDER BY date DESC LIMIT 1
            """, (symbol,)).fetchone()
        
        if row:
            return dict(row)
        return None


def get_latest_vix(as_of_date=None, db_path=None):
    """Get the most recent VIX indicators."""
    with get_connection(db_path) as conn:
        if as_of_date:
            row = conn.execute("""
                SELECT * FROM vix_indicators
                WHERE date <= ?
                ORDER BY date DESC LIMIT 1
            """, (as_of_date,)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM vix_indicators
                ORDER BY date DESC LIMIT 1
            """).fetchone()
        
        if row:
            return dict(row)
        return None


def get_latest_volume(as_of_date=None, db_path=None):
    """Get the most recent volume ratio indicators."""
    with get_connection(db_path) as conn:
        if as_of_date:
            row = conn.execute("""
                SELECT * FROM volume_indicators
                WHERE date <= ?
                ORDER BY date DESC LIMIT 1
            """, (as_of_date,)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM volume_indicators
                ORDER BY date DESC LIMIT 1
            """).fetchone()
        
        if row:
            return dict(row)
        return None


def get_indicator_history(symbol, start_date=None, end_date=None, db_path=None):
    """
    Get indicator history for a symbol as a DataFrame.
    Useful for backtesting and charting.
    """
    with get_connection(db_path) as conn:
        query = "SELECT * FROM indicators WHERE symbol = ?"
        params = [symbol]
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY date ASC"
        
        df = pd.read_sql_query(query, conn, params=params, parse_dates=["date"])
        df.set_index("date", inplace=True)
        return df


# =============================================================================
# HELPERS
# =============================================================================

def _safe_float(val):
    """Convert to float, returning None for NaN/None."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
