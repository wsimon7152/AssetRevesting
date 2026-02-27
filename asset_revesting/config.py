# asset_revesting/config.py
"""
Central configuration for the Asset Revesting Signal Engine.
All tunable parameters from Signal Logic Spec v1.1 live here.
Change these values for backtesting parameter sweeps — never hardcode them in logic modules.
"""

# =============================================================================
# UNIVERSE OF INSTRUMENTS
# =============================================================================

# Tier 1: US Equities
EQUITY_SYMBOLS = ["SPY", "QQQ"]
EQUITY_INVERSE_SYMBOLS = {"SPY": "SH", "QQQ": "PSQ"}

# Tier 2: US Treasury Bonds
BOND_SYMBOLS = ["TLT"]

# Tier 3: US Dollar
DOLLAR_SYMBOLS = ["UUP", "UDN"]

# Tier 4: Cash Equivalent
CASH_SYMBOL = "BIL"

# VIX (not traded, used for classification)
VIX_SYMBOL = "^VIX"

# All symbols that need daily price data
ALL_SYMBOLS = ["SPY", "QQQ", "TLT", "UUP", "UDN", "BIL", "SH", "PSQ"]

# Symbols that need full indicator calculation (stage analysis, SMAs, etc.)
ANALYSIS_SYMBOLS = ["SPY", "QQQ", "TLT", "UUP", "UDN"]

# Intermarket warning symbols (not traded, used for flags)
WARNING_SYMBOLS = ["XLU", "GLD", "RSP"]

# =============================================================================
# DATA INGESTION
# =============================================================================

# Minimum trading days of history needed (200-SMA + buffer)
MIN_HISTORY_DAYS = 300

# Data sources
YFINANCE_SYMBOLS = ALL_SYMBOLS + [VIX_SYMBOL] + WARNING_SYMBOLS
# NYSE breadth data scraped from Barchart.com ($AVVN/$DVVN) after close
# RSP (equal-weight S&P) used as fallback for historical backtests

# =============================================================================
# INDICATOR PARAMETERS
# =============================================================================

# Simple Moving Averages
SMA_PERIODS = [5, 20, 50, 150, 200]

# Bollinger Bands
BB_PERIOD = 20
BB_STD_DEV = 2.0

# SMA Slope calculation
SLOPE_LOOKBACK = 20  # days to measure slope over

# Relative Strength (for SPY vs QQQ selection)
RELATIVE_STRENGTH_SMA = 50  # distance from 50-SMA

# =============================================================================
# VIX CLASSIFICATION
# =============================================================================

VIX_LOW = 15
VIX_NORMAL = 20
VIX_ELEVATED = 30
VIX_HIGH = 40
# Above VIX_HIGH = EXTREME

# VIX Trend
VIX_TREND_FAST = 5   # fast SMA for VIX trend
VIX_TREND_SLOW = 20  # slow SMA for VIX trend

# VIX Spike Detection
VIX_SPIKE_THRESHOLD = 20  # % daily change to flag a spike

# VIX Emergency Exit
VIX_EMERGENCY_LEVEL = 40  # VIX above this AND rising = emergency exit

# =============================================================================
# VOLUME RATIOS
# =============================================================================

VOLUME_PANIC_THRESHOLD = 3.0      # down/up ratio >= this = panic signal
VOLUME_PANIC_EXTREME = 8.0        # extreme panic capitulation
VOLUME_FOMO_THRESHOLD = 3.0       # up/down ratio >= this = FOMO signal
VOLUME_FOMO_EXTREME = 8.0         # extreme euphoria
VOLUME_RATIO_MA_PERIOD = 20       # smoothing period for volume ratio MA

# =============================================================================
# STAGE ANALYSIS
# =============================================================================

# Slope threshold for 150-SMA to distinguish flat vs trending
# 🔬 Backtest range: ±0.3% to ±1.0%
STAGE_SLOPE_THRESHOLD = 0.5  # percentage change over SLOPE_LOOKBACK days

# Number of consecutive days required to confirm a stage transition
# 🔬 Backtest range: 2 to 5
STAGE_CONFIRMATION_DAYS = 3

# =============================================================================
# ENTRY RULES
# =============================================================================

# Minimum pillar score for entry (out of 4)
ENTRY_STRONG_THRESHOLD = 4
ENTRY_MODERATE_THRESHOLD = 3

# =============================================================================
# ATR-BASED DYNAMIC STOPS
# =============================================================================

# Use ATR-based stops instead of fixed percentage stops
USE_ATR_STOPS = True

# ATR calculation period
# 🔬 Backtest range: 10 to 20
ATR_PERIOD = 14

# Stop distance = ATR_MULTIPLIER × ATR below entry
# 🔬 Backtest range: 1.5 to 3.0
ATR_MULTIPLIER = 3.0

# Hard cap: ATR stop can never be wider than this (protects against extreme vol)
# 🔬 Backtest range: 8% to 15%
ATR_MAX_STOP_PCT = 0.10  # 10%

# Floor: ATR stop can never be tighter than this (avoids getting shaken by noise)
# 🔬 Backtest range: 2% to 5% — 4% prevents over-trading in calm markets
ATR_MIN_STOP_PCT = 0.04  # 4%

# Trend check: minimum conditions met (out of 5 SMA conditions)
TREND_MIN_CONDITIONS = 4

# =============================================================================
# EXIT RULES — STANDARD (Stage 2 Long)
# =============================================================================

# Initial stop loss
# 🔬 Backtest range: 4% to 7%, also test ATR-based (2x 14-day ATR)
MAX_STOP_PCT = 0.05  # 5%

# First profit target
# 🔬 Backtest range: 1% to 3%
FIRST_TARGET_PCT = 0.02  # 2%

# Trailing stop after partial exit
# 🔬 Backtest range: 2% to 5%
TRAILING_STOP_PCT = 0.03  # 3%

# Partial exit percentage
PARTIAL_EXIT_PCT_STAGE2 = 0.25  # 25%
PARTIAL_EXIT_PCT_STAGE3 = 0.50  # 50%

# Speed check: if target hit within this many days, take extra off
# 🔬 Backtest range: 1 to 3
SPEED_CHECK_DAYS = 2
SPEED_CHECK_EXTRA_PCT = 0.25  # additional 25% to sell
SPEED_CHECK_MAX_PCT = 0.75    # cap at 75% total

# =============================================================================
# EXIT RULES — STAGE 4 INVERSE
# =============================================================================

# 🔬 Backtest: validate against 2008, 2020, 2022 bear markets
MAX_STOP_PCT_INVERSE = 0.04       # 4% (tighter)
FIRST_TARGET_PCT_INVERSE = 0.015  # 1.5%
TRAILING_STOP_PCT_INVERSE = 0.02  # 2% (tighter)
PARTIAL_EXIT_PCT_INVERSE = 0.25   # 25%

# Stage 3 first target (more defensive)
FIRST_TARGET_PCT_STAGE3 = 0.015  # 1.5%

# =============================================================================
# LEVERAGE OVERLAY (OPTIONAL)
# =============================================================================

LEVERAGE_TIERS = {
    # VIX range: leverage multiplier
    (0, 15): 3,
    (15, 20): 2,
    (20, 30): 1,
    (30, float("inf")): 0,  # 0 = cash
}

LEVERAGED_ETFS = {
    "SPY": {"2x": "SSO", "3x": "UPRO", "-2x": "SDS", "-3x": "SPXU"},
    "QQQ": {"2x": "QLD", "3x": "TQQQ", "-2x": "QID", "-3x": "SQQQ"},
}

# =============================================================================
# INTERMARKET WARNINGS
# =============================================================================

# Defensive rotation: XLU outperforming SPY by this much (%)
DEFENSIVE_ROTATION_THRESHOLD = 5.0

# =============================================================================
# DATABASE
# =============================================================================

import os as _os
DB_PATH = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "asset_revesting.db")
