#!/usr/bin/env python3
"""
Test Layer 1 + Layer 2 with synthetic data.
Verifies all indicator calculations are mathematically correct.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Override DB path for testing
os.environ["ASSET_REVESTING_TEST"] = "1"
TEST_DB = "/home/claude/test_asset_revesting.db"

from asset_revesting.data.database import init_db, reset_db, get_connection
from asset_revesting.core.indicators import (
    calc_sma, calc_sma_slope, calc_bollinger_bands,
    calc_relative_strength, classify_vix, calc_vix_indicators,
    calc_volume_ratios, compute_symbol_indicators,
    store_symbol_indicators, store_vix_indicators, store_volume_indicators,
    get_latest_indicators, get_latest_vix,
)
from asset_revesting.config import (
    SMA_PERIODS, BB_PERIOD, BB_STD_DEV, SLOPE_LOOKBACK,
    VIX_LOW, VIX_NORMAL, VIX_ELEVATED, VIX_HIGH,
)


def generate_synthetic_prices(n_days=300, start_price=450.0, trend=0.0005, volatility=0.015):
    """Generate realistic synthetic daily price data."""
    dates = pd.date_range(end=datetime.now(), periods=n_days, freq="B")  # business days
    
    np.random.seed(42)  # reproducible
    returns = np.random.normal(trend, volatility, n_days)
    prices = start_price * np.cumprod(1 + returns)
    
    # Generate OHLV from close
    close = pd.Series(prices, index=dates)
    high = close * (1 + np.abs(np.random.normal(0, 0.005, n_days)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, n_days)))
    open_price = close.shift(1).fillna(start_price) * (1 + np.random.normal(0, 0.002, n_days))
    volume = np.random.uniform(50e6, 150e6, n_days)
    
    return pd.DataFrame({
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)


def test_sma():
    """Test SMA calculation against manual computation."""
    print("TEST: Simple Moving Average...")
    
    data = pd.Series([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20.0])
    
    sma_5 = calc_sma(data, 5)
    
    # Manual: last 5 values = [16, 17, 18, 19, 20], mean = 18.0
    assert abs(sma_5.iloc[-1] - 18.0) < 0.001, f"SMA-5 expected 18.0, got {sma_5.iloc[-1]}"
    
    # First 4 values should be NaN (not enough data for 5-period SMA)
    assert pd.isna(sma_5.iloc[0]), "SMA should be NaN for insufficient data"
    assert pd.isna(sma_5.iloc[3]), "SMA should be NaN for insufficient data"
    assert pd.notna(sma_5.iloc[4]), "SMA should have value at index 4"
    
    # SMA at index 4 = mean([10,11,12,13,14]) = 12.0
    assert abs(sma_5.iloc[4] - 12.0) < 0.001, f"SMA-5[4] expected 12.0, got {sma_5.iloc[4]}"
    
    print("  ✓ PASSED")


def test_sma_slope():
    """Test SMA slope calculation."""
    print("TEST: SMA Slope...")
    
    # Create a series where SMA is linearly increasing
    n = 50
    data = pd.Series(np.linspace(100, 150, n))
    sma = calc_sma(data, 5)
    slope = calc_sma_slope(sma, lookback=5)
    
    # Slope should be positive for an uptrending SMA
    last_slope = slope.iloc[-1]
    assert last_slope > 0, f"Slope should be positive for uptrend, got {last_slope}"
    
    # Create flat series
    flat_data = pd.Series([100.0] * 50)
    flat_sma = calc_sma(flat_data, 5)
    flat_slope = calc_sma_slope(flat_sma, lookback=5)
    last_flat = flat_slope.iloc[-1]
    assert abs(last_flat) < 0.001, f"Slope should be ~0 for flat data, got {last_flat}"
    
    print("  ✓ PASSED")


def test_bollinger_bands():
    """Test Bollinger Band calculation."""
    print("TEST: Bollinger Bands...")
    
    np.random.seed(42)
    n = 50
    prices = pd.Series(100 + np.random.normal(0, 2, n))
    
    bb = calc_bollinger_bands(prices, period=20, num_std=2)
    
    # Basic sanity checks
    last = n - 1
    assert bb["upper"].iloc[last] > bb["middle"].iloc[last], "Upper band should be above middle"
    assert bb["lower"].iloc[last] < bb["middle"].iloc[last], "Lower band should be below middle"
    assert bb["bandwidth"].iloc[last] > 0, "Bandwidth should be positive"
    
    # Middle band should equal 20-SMA
    manual_sma = calc_sma(prices, 20)
    assert abs(bb["middle"].iloc[last] - manual_sma.iloc[last]) < 0.001, "Middle band should equal SMA"
    
    # %B should be between -0.5 and 1.5 for reasonable data (not strict bounds)
    pct_b = bb["percent_b"].iloc[last]
    assert -1 < pct_b < 2, f"%B seems unreasonable: {pct_b}"
    
    # Verify upper = middle + 2*std
    std_20 = prices.rolling(20).std().iloc[last]
    expected_upper = manual_sma.iloc[last] + 2 * std_20
    assert abs(bb["upper"].iloc[last] - expected_upper) < 0.001, "Upper band formula wrong"
    
    print("  ✓ PASSED")


def test_relative_strength():
    """Test relative strength calculation."""
    print("TEST: Relative Strength...")
    
    n = 60
    # Price consistently above SMA → positive relative strength
    prices = pd.Series(np.linspace(100, 130, n))
    rs = calc_relative_strength(prices, sma_period=50)
    
    # Last value should be positive (price above SMA-50)
    assert rs.iloc[-1] > 0, f"RS should be positive for uptrending price, got {rs.iloc[-1]}"
    
    # Price consistently below SMA → negative relative strength
    prices_down = pd.Series(np.linspace(130, 100, n))
    rs_down = calc_relative_strength(prices_down, sma_period=50)
    assert rs_down.iloc[-1] < 0, f"RS should be negative for downtrending price, got {rs_down.iloc[-1]}"
    
    print("  ✓ PASSED")


def test_vix_classification():
    """Test VIX regime classification."""
    print("TEST: VIX Classification...")
    
    assert classify_vix(12) == "LOW", "VIX 12 should be LOW"
    assert classify_vix(17) == "NORMAL", "VIX 17 should be NORMAL"
    assert classify_vix(25) == "ELEVATED", "VIX 25 should be ELEVATED"
    assert classify_vix(35) == "HIGH", "VIX 35 should be HIGH"
    assert classify_vix(50) == "EXTREME", "VIX 50 should be EXTREME"
    
    # Boundary cases
    assert classify_vix(15) == "NORMAL", "VIX 15 should be NORMAL (>= threshold)"
    assert classify_vix(14.99) == "LOW", "VIX 14.99 should be LOW"
    assert classify_vix(20) == "ELEVATED", "VIX 20 should be ELEVATED"
    assert classify_vix(30) == "HIGH", "VIX 30 should be HIGH"
    assert classify_vix(40) == "EXTREME", "VIX 40 should be EXTREME"
    
    assert classify_vix(None) is None, "None should return None"
    
    print("  ✓ PASSED")


def test_vix_indicators():
    """Test full VIX indicator pipeline."""
    print("TEST: VIX Indicators Pipeline...")
    
    dates = pd.date_range(end=datetime.now(), periods=50, freq="B")
    
    # Create VIX that starts low, spikes, then comes down
    vix_values = np.concatenate([
        np.full(20, 14),     # LOW regime
        np.linspace(14, 45, 10),  # spike to EXTREME
        np.linspace(45, 22, 20),  # decline to ELEVATED
    ])
    vix_series = pd.Series(vix_values, index=dates)
    
    result = calc_vix_indicators(vix_series)
    
    assert result["vix_regime"].iloc[0] == "LOW", "First regime should be LOW"
    assert "EXTREME" in result["vix_regime"].values, "Should have EXTREME regime during spike"
    assert result["vix_regime"].iloc[-1] == "ELEVATED", f"Last regime should be ELEVATED, got {result['vix_regime'].iloc[-1]}"
    
    # Check that spike detection works
    assert result["vix_spike"].sum() > 0, "Should detect at least one spike"
    
    print("  ✓ PASSED")


def test_volume_ratios():
    """Test volume ratio calculation."""
    print("TEST: Volume Ratios...")
    
    dates = pd.date_range(end=datetime.now(), periods=30, freq="B")
    
    # Scenario: panic day (3:1 down:up)
    up_vol = pd.Series([1e9] * 30, index=dates)
    down_vol = pd.Series([1e9] * 30, index=dates)
    down_vol.iloc[-1] = 3e9  # Last day: 3:1 panic
    
    result = calc_volume_ratios(up_vol, down_vol)
    
    # Last day panic ratio should be 3.0
    assert abs(result["panic_ratio"].iloc[-1] - 3.0) < 0.001, \
        f"Panic ratio should be 3.0, got {result['panic_ratio'].iloc[-1]}"
    
    # Last day FOMO ratio should be 1/3
    assert abs(result["fomo_ratio"].iloc[-1] - (1/3)) < 0.001, \
        f"FOMO ratio should be 0.333, got {result['fomo_ratio'].iloc[-1]}"
    
    # Normal days should be ~1.0
    assert abs(result["panic_ratio"].iloc[0] - 1.0) < 0.001, "Normal day panic ratio should be 1.0"
    
    print("  ✓ PASSED")


def test_full_symbol_pipeline():
    """Test the complete indicator computation pipeline for a symbol."""
    print("TEST: Full Symbol Pipeline...")
    
    prices = generate_synthetic_prices(n_days=250, start_price=450, trend=0.0003)
    indicators = compute_symbol_indicators(prices["close"])
    
    # Should have all expected columns
    expected_cols = [
        "close", "sma_5", "sma_20", "sma_50", "sma_150", "sma_200",
        "sma_150_slope", "sma_200_slope", "sma_50_slope",
        "bb_upper", "bb_middle", "bb_lower", "bb_bandwidth", "bb_percent_b",
        "relative_strength",
    ]
    for col in expected_cols:
        assert col in indicators.columns, f"Missing column: {col}"
    
    # Should have 250 rows
    assert len(indicators) == 250, f"Expected 250 rows, got {len(indicators)}"
    
    # Last row should have values for all indicators (200-SMA needs 200 days)
    last = indicators.iloc[-1]
    for col in expected_cols:
        assert pd.notna(last[col]), f"Last row {col} should not be NaN"
    
    # SMA ordering for uptrending data: sma_5 > sma_20 > sma_50 (roughly)
    # Not guaranteed but likely with positive trend
    assert last["sma_5"] > last["sma_200"], \
        "For uptrending data, short SMA should be above long SMA"
    
    print("  ✓ PASSED")


def test_database_storage():
    """Test storing and retrieving indicators from SQLite."""
    print("TEST: Database Storage & Retrieval...")
    
    reset_db(TEST_DB)
    
    # Generate and compute indicators
    prices = generate_synthetic_prices(n_days=250)
    indicators = compute_symbol_indicators(prices["close"])
    
    # Store in DB
    store_symbol_indicators("SPY", indicators, TEST_DB)
    
    # Retrieve latest
    latest = get_latest_indicators("SPY", db_path=TEST_DB)
    assert latest is not None, "Should retrieve stored indicators"
    assert latest["symbol"] == "SPY", f"Symbol should be SPY, got {latest['symbol']}"
    assert latest["sma_5"] is not None, "SMA-5 should not be None"
    
    # Verify values match
    last_computed = indicators.iloc[-1]
    assert abs(latest["sma_5"] - last_computed["sma_5"]) < 0.01, "Stored SMA-5 should match computed"
    assert abs(latest["bb_upper"] - last_computed["bb_upper"]) < 0.01, "Stored BB-upper should match computed"
    
    # Test as_of_date retrieval (for backtesting)
    mid_date = indicators.index[125].strftime("%Y-%m-%d")
    historical = get_latest_indicators("SPY", as_of_date=mid_date, db_path=TEST_DB)
    assert historical is not None, "Should retrieve historical indicators"
    assert historical["date"] <= mid_date, "Historical date should be <= requested date"
    
    # Store VIX indicators
    dates = pd.date_range(end=datetime.now(), periods=50, freq="B")
    vix_series = pd.Series(np.random.uniform(12, 30, 50), index=dates)
    vix_indicators = calc_vix_indicators(vix_series)
    store_vix_indicators(vix_indicators, TEST_DB)
    
    latest_vix = get_latest_vix(db_path=TEST_DB)
    assert latest_vix is not None, "Should retrieve VIX indicators"
    assert latest_vix["vix_regime"] is not None, "VIX regime should not be None"
    
    # Cleanup
    os.remove(TEST_DB)
    
    print("  ✓ PASSED")


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("LAYER 1 + LAYER 2 VERIFICATION TESTS")
    print("=" * 60)
    print()
    
    tests = [
        test_sma,
        test_sma_slope,
        test_bollinger_bands,
        test_relative_strength,
        test_vix_classification,
        test_vix_indicators,
        test_volume_ratios,
        test_full_symbol_pipeline,
        test_database_storage,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
    
    print()
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
