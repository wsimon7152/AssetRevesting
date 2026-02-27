# asset_revesting/core/stage_analysis.py
"""
Layer 3a — Stage Analysis

Determines the current market stage (1-4) for each asset based on
SMA relationships, slope thresholds, and moving average ordering.

Implements Signal Logic Spec v1.1 Section 3.
"""

import pandas as pd
from asset_revesting.config import (
    STAGE_SLOPE_THRESHOLD, STAGE_CONFIRMATION_DAYS, SLOPE_LOOKBACK,
    ANALYSIS_SYMBOLS
)
from asset_revesting.data.database import get_connection
from asset_revesting.core.indicators import get_latest_indicators, get_indicator_history


# Stage constants
STAGE_1 = "STAGE_1"
STAGE_2 = "STAGE_2"
STAGE_3 = "STAGE_3"
STAGE_4 = "STAGE_4"
TRANSITIONAL = "TRANSITIONAL"


def classify_stage(ind):
    """
    Determine the raw stage for a single date's indicator values.
    Does NOT apply confirmation logic.
    
    Args:
        ind: dict with keys: close, sma_50, sma_150, sma_200,
             sma_150_slope, sma_200_slope, sma_50_slope
    
    Returns:
        str: STAGE_1, STAGE_2, STAGE_3, STAGE_4, or TRANSITIONAL
    """
    close = ind.get("close")
    sma_50 = ind.get("sma_50")
    sma_150 = ind.get("sma_150")
    sma_200 = ind.get("sma_200")
    slope_150 = ind.get("sma_150_slope")
    slope_200 = ind.get("sma_200_slope")
    slope_50 = ind.get("sma_50_slope")

    if any(v is None for v in [close, sma_50, sma_150, sma_200, slope_150, slope_200]):
        return TRANSITIONAL

    threshold = STAGE_SLOPE_THRESHOLD

    # --- STAGE 2: Advancing ---
    s2 = [
        close > sma_150,
        close > sma_200,
        slope_150 > threshold,
        sma_50 > sma_150 and sma_150 > sma_200,
        slope_200 > -threshold,
    ]
    if sum(s2) >= 4:
        return STAGE_2

    # --- STAGE 4: Declining ---
    s4 = [
        close < sma_150,
        close < sma_200,
        slope_150 < -threshold,
        sma_50 < sma_150 and sma_150 < sma_200,
        slope_200 < threshold,
    ]
    if sum(s4) >= 4:
        return STAGE_4

    # --- STAGE 3: Distribution ---
    s3 = [
        abs(slope_150) <= threshold * 2,
        slope_50 is not None and slope_50 < threshold,
        sma_50 < sma_150,
        slope_200 is not None and slope_200 > -threshold,
    ]
    if sum(s3) >= 3:
        return STAGE_3

    # --- STAGE 1: Accumulation ---
    s1 = [
        abs(slope_150) <= threshold,
        slope_200 is not None and abs(slope_200) <= threshold * 1.5,
        sma_150 > 0 and abs(close - sma_150) / sma_150 * 100 < 3.0,
    ]
    if sum(s1) >= 2:
        return STAGE_1

    return TRANSITIONAL


def get_close_for_date(symbol, date_str, db_path=None):
    """Get closing price for a symbol on a specific date."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT close FROM prices WHERE symbol = ? AND date = ?",
            (symbol, date_str)
        ).fetchone()
        return row["close"] if row else None


def determine_stage(symbol, as_of_date=None, db_path=None):
    """
    Determine the current confirmed stage for a symbol.
    Applies 3-day confirmation rule.
    
    Returns:
        dict: {stage, raw_stage, consecutive_days, confirmed, date}
    """
    ind = get_latest_indicators(symbol, as_of_date, db_path)
    if ind is None:
        return {"stage": TRANSITIONAL, "raw_stage": TRANSITIONAL,
                "consecutive_days": 0, "confirmed": False, "date": as_of_date}

    today = ind["date"]

    # Check if we already computed this date — return cached result
    with get_connection(db_path) as conn:
        existing = conn.execute("""
            SELECT stage, confirmed, consecutive_days FROM stages
            WHERE symbol = ? AND date = ?
        """, (symbol, today)).fetchone()

    if existing:
        # Already computed — look up the confirmed stage
        with get_connection(db_path) as conn:
            last_conf = conn.execute("""
                SELECT stage FROM stages
                WHERE symbol = ? AND date <= ? AND confirmed = 1
                ORDER BY date DESC LIMIT 1
            """, (symbol, today)).fetchone()

        confirmed_stage = last_conf["stage"] if last_conf else TRANSITIONAL
        return {
            "stage": confirmed_stage,
            "raw_stage": existing["stage"],
            "consecutive_days": existing["consecutive_days"],
            "confirmed": bool(existing["confirmed"]),
            "date": today,
        }

    close = get_close_for_date(symbol, today, db_path)
    if close is not None:
        ind["close"] = close

    raw_stage = classify_stage(ind)

    # Look up last confirmed stage (from BEFORE today)
    with get_connection(db_path) as conn:
        last_entry = conn.execute("""
            SELECT stage, confirmed, consecutive_days FROM stages
            WHERE symbol = ? AND date < ? AND confirmed = 1
            ORDER BY date DESC LIMIT 1
        """, (symbol, today)).fetchone()

    last_confirmed = last_entry["stage"] if last_entry else TRANSITIONAL

    # Count consecutive days from PREVIOUS day (not today)
    with get_connection(db_path) as conn:
        recent = conn.execute("""
            SELECT stage, consecutive_days FROM stages
            WHERE symbol = ? AND date < ?
            ORDER BY date DESC LIMIT 1
        """, (symbol, today)).fetchone()

    if recent and recent["stage"] == raw_stage:
        consecutive = recent["consecutive_days"] + 1
    elif raw_stage == TRANSITIONAL:
        consecutive = 0
    else:
        consecutive = 1

    # Apply confirmation
    if raw_stage == TRANSITIONAL:
        confirmed_stage = last_confirmed
        confirmed = False
    elif raw_stage == last_confirmed:
        confirmed_stage = last_confirmed
        confirmed = True
    elif consecutive >= STAGE_CONFIRMATION_DAYS:
        confirmed_stage = raw_stage
        confirmed = True
    else:
        confirmed_stage = last_confirmed
        confirmed = False

    # Store
    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO stages (symbol, date, stage, confirmed, consecutive_days)
            VALUES (?, ?, ?, ?, ?)
        """, (symbol, ind["date"], raw_stage, 1 if confirmed else 0, consecutive))

    return {
        "stage": confirmed_stage,
        "raw_stage": raw_stage,
        "consecutive_days": consecutive,
        "confirmed": confirmed,
        "date": ind["date"],
    }


def compute_stage_history(db_path=None):
    """
    Compute stages for every historical date that has indicators.
    Must be called AFTER indicators are computed.
    Processes dates sequentially so confirmation logic works correctly.
    """
    for symbol in ANALYSIS_SYMBOLS:
        # Get all dates with full indicators for this symbol
        with get_connection(db_path) as conn:
            dates = conn.execute("""
                SELECT DISTINCT i.date FROM indicators i
                JOIN prices p ON p.symbol = i.symbol AND p.date = i.date
                WHERE i.symbol = ? AND i.sma_200 IS NOT NULL
                ORDER BY i.date ASC
            """, (symbol,)).fetchall()

        if not dates:
            print(f"  {symbol}: No indicator data")
            continue

        last_confirmed = TRANSITIONAL
        consecutive = 0
        last_raw = None

        for row in dates:
            date_str = row["date"]

            # Get indicators for this date
            ind = get_latest_indicators(symbol, date_str, db_path)
            if ind is None:
                continue

            close = get_close_for_date(symbol, date_str, db_path)
            if close is not None:
                ind["close"] = close

            raw_stage = classify_stage(ind)

            # Count consecutive days
            if raw_stage == last_raw and raw_stage != TRANSITIONAL:
                consecutive += 1
            elif raw_stage == TRANSITIONAL:
                consecutive = 0
            else:
                consecutive = 1
            last_raw = raw_stage

            # Apply confirmation
            if raw_stage == TRANSITIONAL:
                confirmed_stage = last_confirmed
                confirmed = False
            elif raw_stage == last_confirmed:
                confirmed_stage = last_confirmed
                confirmed = True
            elif consecutive >= STAGE_CONFIRMATION_DAYS:
                confirmed_stage = raw_stage
                confirmed = True
                last_confirmed = raw_stage
            else:
                confirmed_stage = last_confirmed
                confirmed = False

            # Store
            with get_connection(db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO stages (symbol, date, stage, confirmed, consecutive_days)
                    VALUES (?, ?, ?, ?, ?)
                """, (symbol, date_str, raw_stage, 1 if confirmed else 0, consecutive))

        print(f"  {symbol}: {len(dates)} dates processed, current: {last_confirmed}")


def get_all_stages(as_of_date=None, db_path=None):
    """Get current stage for all analysis symbols."""
    stages = {}
    for symbol in ANALYSIS_SYMBOLS:
        stages[symbol] = determine_stage(symbol, as_of_date, db_path)
    return stages


def print_stage_summary(as_of_date=None, db_path=None):
    """Print formatted summary of all stages."""
    stages = get_all_stages(as_of_date, db_path)

    stage_labels = {
        STAGE_1: "Stage 1 (Accumulation)",
        STAGE_2: "Stage 2 (Advancing)",
        STAGE_3: "Stage 3 (Distribution)",
        STAGE_4: "Stage 4 (Declining)",
        TRANSITIONAL: "Transitional",
    }

    print("\n=== STAGE ANALYSIS ===\n")

    for symbol, info in stages.items():
        label = stage_labels.get(info["stage"], info["stage"])
        raw_label = stage_labels.get(info["raw_stage"], info["raw_stage"])
        conf = "confirmed" if info["confirmed"] else f"{info['consecutive_days']}/{STAGE_CONFIRMATION_DAYS} days"

        line = f"  {symbol:6s}: {label:30s} ({conf})"
        if info["raw_stage"] != info["stage"]:
            line += f"  [raw: {raw_label}]"
        print(line)

    print()
