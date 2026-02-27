# asset_revesting/core/signals.py
"""
Layer 3b — Signal Generator

Implements the four-pillar confluence check, asset rotation logic,
and all entry/exit signal rules from Signal Logic Spec v1.1 Sections 4-5.
"""

from asset_revesting.config import (
    EQUITY_SYMBOLS, EQUITY_INVERSE_SYMBOLS, BOND_SYMBOLS,
    DOLLAR_SYMBOLS, CASH_SYMBOL,
    ENTRY_STRONG_THRESHOLD, ENTRY_MODERATE_THRESHOLD, TREND_MIN_CONDITIONS,
    VIX_EMERGENCY_LEVEL,
    VOLUME_PANIC_THRESHOLD, VOLUME_FOMO_THRESHOLD,
    MAX_STOP_PCT, MAX_STOP_PCT_INVERSE,
    FIRST_TARGET_PCT, FIRST_TARGET_PCT_STAGE3, FIRST_TARGET_PCT_INVERSE,
    TRAILING_STOP_PCT, TRAILING_STOP_PCT_INVERSE,
    PARTIAL_EXIT_PCT_STAGE2, PARTIAL_EXIT_PCT_STAGE3, PARTIAL_EXIT_PCT_INVERSE,
    SPEED_CHECK_DAYS, SPEED_CHECK_EXTRA_PCT, SPEED_CHECK_MAX_PCT,
    DEFENSIVE_ROTATION_THRESHOLD,
    USE_ATR_STOPS, ATR_MULTIPLIER, ATR_MAX_STOP_PCT, ATR_MIN_STOP_PCT,
)
from asset_revesting.core.stage_analysis import (
    STAGE_1, STAGE_2, STAGE_3, STAGE_4, TRANSITIONAL,
    determine_stage, get_all_stages, print_stage_summary,
)
from asset_revesting.core.indicators import (
    get_latest_indicators, get_latest_vix, get_latest_volume,
)
from asset_revesting.data.database import get_connection


# Signal constants
STRONG_ENTRY = "STRONG_ENTRY"
MODERATE_ENTRY = "MODERATE_ENTRY"
NO_ENTRY = "NO_ENTRY"
LONG = "LONG"
LONG_INVERSE = "LONG_INVERSE"
HOLD = "HOLD"


# =============================================================================
# PILLAR 1: TREND CHECK
# =============================================================================

def trend_check(symbol, direction=LONG, as_of_date=None, db_path=None):
    """Check if SMA relationships support the trade direction."""
    ind = get_latest_indicators(symbol, as_of_date, db_path)
    if ind is None:
        return {"favorable": False, "score": 0, "details": "No data"}

    close = _get_close(symbol, ind["date"], db_path)
    if close is None:
        return {"favorable": False, "score": 0, "details": "No close price"}

    sma_5 = ind.get("sma_5")
    sma_20 = ind.get("sma_20")
    sma_50 = ind.get("sma_50")
    sma_150 = ind.get("sma_150")
    sma_200 = ind.get("sma_200")

    if any(v is None for v in [sma_5, sma_20, sma_50, sma_150, sma_200]):
        return {"favorable": False, "score": 0, "details": "Insufficient SMA data"}

    if direction == LONG:
        conditions = [
            close > sma_5, sma_5 > sma_20, close > sma_50,
            close > sma_150, close > sma_200,
        ]
        labels = [
            f"Close({close:.2f})>SMA5({sma_5:.2f})",
            f"SMA5>SMA20({sma_20:.2f})",
            f"Close>SMA50({sma_50:.2f})",
            f"Close>SMA150({sma_150:.2f})",
            f"Close>SMA200({sma_200:.2f})",
        ]
    else:
        conditions = [
            close < sma_5, sma_5 < sma_20, close < sma_50,
            close < sma_150, close < sma_200,
        ]
        labels = [
            f"Close({close:.2f})<SMA5({sma_5:.2f})",
            f"SMA5<SMA20({sma_20:.2f})",
            f"Close<SMA50({sma_50:.2f})",
            f"Close<SMA150({sma_150:.2f})",
            f"Close<SMA200({sma_200:.2f})",
        ]

    score = sum(conditions)
    details = "; ".join(f"{'Y' if c else 'N'} {l}" for c, l in zip(conditions, labels))
    return {"favorable": score >= TREND_MIN_CONDITIONS, "score": score, "details": details}


# =============================================================================
# PILLAR 2: VOLATILITY CHECK
# =============================================================================

def volatility_check(direction=LONG, symbol=None, as_of_date=None, db_path=None):
    """Check if VIX regime and Bollinger Bands support the trade."""
    vix = get_latest_vix(as_of_date, db_path)
    if vix is None:
        return {"favorable": False, "vix_regime": None, "vix_trend": None, "details": "No VIX data"}

    regime = vix.get("vix_regime")
    trend = vix.get("vix_trend")

    bb_pct_b = None
    if symbol:
        ind = get_latest_indicators(symbol, as_of_date, db_path)
        if ind:
            bb_pct_b = ind.get("bb_percent_b")

    if direction == LONG:
        vix_ok = regime in ("LOW", "NORMAL") or (regime == "ELEVATED" and trend == "FALLING")
        bb_ok = bb_pct_b is None or (0 <= bb_pct_b <= 1)
        favorable = vix_ok and bb_ok
    else:
        favorable = regime in ("HIGH", "EXTREME") and trend == "RISING"

    details = f"VIX:{regime}/{trend}"
    if bb_pct_b is not None:
        details += f" BB%B:{bb_pct_b:.2f}"
    return {"favorable": favorable, "vix_regime": regime, "vix_trend": trend, "details": details}


# =============================================================================
# PILLAR 3: VOLUME CHECK
# =============================================================================

def volume_check(direction=LONG, as_of_date=None, db_path=None):
    """Check if NYSE volume ratios support the trade direction."""
    vol = get_latest_volume(as_of_date, db_path)
    flags = []

    if vol is None:
        return {"favorable": True, "panic_ratio": None, "fomo_ratio": None,
                "flags": ["No NYSE volume data — volume pillar neutral"], "details": "No data"}

    pr = vol.get("panic_ratio")
    fr = vol.get("fomo_ratio")
    fr_ma = vol.get("fomo_ratio_ma")

    if direction == LONG:
        panic_signal = pr is not None and pr >= VOLUME_PANIC_THRESHOLD
        no_euphoria = fr_ma is not None and fr_ma < 2.0
        favorable = panic_signal or no_euphoria
        if fr is not None and fr >= VOLUME_FOMO_THRESHOLD:
            flags.append(f"FOMO WARNING: ratio={fr:.1f}")
        if pr is not None and pr >= 8.0:
            flags.append(f"EXTREME PANIC: ratio={pr:.1f}")
    else:
        favorable = fr is not None and fr >= VOLUME_FOMO_THRESHOLD

    details = f"Panic={pr:.2f} FOMO={fr:.2f}" if pr and fr else "Partial data"
    return {"favorable": favorable, "panic_ratio": pr, "fomo_ratio": fr, "flags": flags, "details": details}


# =============================================================================
# SMA CROSSOVER CHECK
# =============================================================================

def check_sma_crossover(symbol, as_of_date=None, db_path=None):
    """Check if 5-SMA crossed above/below 20-SMA."""
    with get_connection(db_path) as conn:
        if as_of_date:
            rows = conn.execute("""
                SELECT date, sma_5, sma_20 FROM indicators
                WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 2
            """, (symbol, as_of_date)).fetchall()
        else:
            rows = conn.execute("""
                SELECT date, sma_5, sma_20 FROM indicators
                WHERE symbol = ? ORDER BY date DESC LIMIT 2
            """, (symbol,)).fetchall()

    if len(rows) < 2:
        return {"bullish_cross": False, "bearish_cross": False, "details": "Insufficient data"}

    today, yesterday = rows[0], rows[1]
    if any(v is None for v in [today["sma_5"], today["sma_20"], yesterday["sma_5"], yesterday["sma_20"]]):
        return {"bullish_cross": False, "bearish_cross": False, "details": "Missing SMA data"}

    bullish = today["sma_5"] > today["sma_20"] and yesterday["sma_5"] <= yesterday["sma_20"]
    bearish = today["sma_5"] < today["sma_20"] and yesterday["sma_5"] >= yesterday["sma_20"]
    details = f"SMA5: {yesterday['sma_5']:.2f}->{today['sma_5']:.2f}, SMA20: {today['sma_20']:.2f}"
    return {"bullish_cross": bullish, "bearish_cross": bearish, "details": details}


# =============================================================================
# EQUITY PICK
# =============================================================================

def equity_pick(as_of_date=None, db_path=None):
    """Choose SPY or QQQ based on distance from 50-SMA."""
    spy = get_latest_indicators("SPY", as_of_date, db_path)
    qqq = get_latest_indicators("QQQ", as_of_date, db_path)
    spy_rs = spy.get("relative_strength") if spy else None
    qqq_rs = qqq.get("relative_strength") if qqq else None

    if qqq_rs is not None and spy_rs is not None and qqq_rs > spy_rs:
        return "QQQ"
    return "SPY"


# =============================================================================
# FOUR-PILLAR ENTRY SIGNAL
# =============================================================================

def entry_signal(symbol, direction=LONG, stage_info=None, as_of_date=None, db_path=None):
    """Evaluate four-pillar confluence for entry."""
    if stage_info is None:
        stage_info = determine_stage(symbol, as_of_date, db_path)

    stage = stage_info["stage"]
    stage_ok = (stage == STAGE_2) if direction == LONG else (stage == STAGE_4)

    trend = trend_check(symbol, direction, as_of_date, db_path)
    vol = volatility_check(direction, symbol, as_of_date, db_path)
    volume = volume_check(direction, as_of_date, db_path)

    score = sum([stage_ok, trend["favorable"], vol["favorable"], volume["favorable"]])

    if score >= ENTRY_STRONG_THRESHOLD:
        signal = STRONG_ENTRY
    elif score >= ENTRY_MODERATE_THRESHOLD:
        signal = MODERATE_ENTRY
    else:
        signal = NO_ENTRY

    details = (f"Score={score}/4: Stage={'Y' if stage_ok else 'N'}({stage}) "
               f"Trend={'Y' if trend['favorable'] else 'N'} "
               f"Vol={'Y' if vol['favorable'] else 'N'} "
               f"Volume={'Y' if volume['favorable'] else 'N'}")

    return {
        "signal": signal, "score": score,
        "pillar_results": {"stage": stage_ok, "trend": trend, "volatility": vol, "volume": volume},
        "flags": volume.get("flags", []),
        "details": details,
    }


# =============================================================================
# ASSET ROTATION
# =============================================================================

def asset_rotation(as_of_date=None, db_path=None):
    """Determine which asset to enter. Single asset, check tiers in order."""
    # VIX emergency gate — no entries when VIX is in emergency territory
    vix = get_latest_vix(as_of_date, db_path)
    if vix:
        vix_close = vix.get("vix_close", 0)
        vix_trend = vix.get("vix_trend", "")
        if vix_close > VIX_EMERGENCY_LEVEL and vix_trend == "RISING":
            return {"asset": CASH_SYMBOL, "direction": HOLD, "tier": 4,
                    "signal_strength": NO_ENTRY, "entry_details": None,
                    "crossover": None,
                    "reason": f"VIX emergency ({vix_close:.1f} > {VIX_EMERGENCY_LEVEL}) — cash only"}

    stages = get_all_stages(as_of_date, db_path)

    spy_stage = stages.get("SPY", {}).get("stage", TRANSITIONAL)
    qqq_stage = stages.get("QQQ", {}).get("stage", TRANSITIONAL)

    # Tier 1: Equities long
    if spy_stage == STAGE_2 or qqq_stage == STAGE_2:
        equity = equity_pick(as_of_date, db_path)
        sig = entry_signal(equity, LONG, stages.get(equity), as_of_date, db_path)
        if sig["signal"] in (STRONG_ENTRY, MODERATE_ENTRY):
            return {"asset": equity, "direction": LONG, "tier": 1,
                    "signal_strength": sig["signal"], "entry_details": sig,
                    "crossover": check_sma_crossover(equity, as_of_date, db_path),
                    "reason": f"{equity} Stage 2, score {sig['score']}/4"}

    # Tier 1: Equities inverse
    if spy_stage == STAGE_4:
        sig = entry_signal("SPY", LONG_INVERSE, stages.get("SPY"), as_of_date, db_path)
        if sig["signal"] in (STRONG_ENTRY, MODERATE_ENTRY):
            inv = EQUITY_INVERSE_SYMBOLS.get("SPY", "SH")
            return {"asset": inv, "direction": LONG_INVERSE, "tier": 1,
                    "signal_strength": sig["signal"], "entry_details": sig,
                    "crossover": check_sma_crossover("SPY", as_of_date, db_path),
                    "reason": f"SPY Stage 4, inverse via {inv}, score {sig['score']}/4"}

    # Tier 2: Bonds
    if stages.get("TLT", {}).get("stage") == STAGE_2:
        sig = entry_signal("TLT", LONG, stages.get("TLT"), as_of_date, db_path)
        if sig["signal"] in (STRONG_ENTRY, MODERATE_ENTRY):
            return {"asset": "TLT", "direction": LONG, "tier": 2,
                    "signal_strength": sig["signal"], "entry_details": sig,
                    "crossover": check_sma_crossover("TLT", as_of_date, db_path),
                    "reason": f"TLT Stage 2, score {sig['score']}/4"}

    # Tier 3: Dollar
    uup_stage = stages.get("UUP", {}).get("stage", TRANSITIONAL)
    if uup_stage == STAGE_2:
        sig = entry_signal("UUP", LONG, stages.get("UUP"), as_of_date, db_path)
        if sig["signal"] in (STRONG_ENTRY, MODERATE_ENTRY):
            return {"asset": "UUP", "direction": LONG, "tier": 3,
                    "signal_strength": sig["signal"], "entry_details": sig,
                    "crossover": check_sma_crossover("UUP", as_of_date, db_path),
                    "reason": f"UUP Stage 2, score {sig['score']}/4"}

    if uup_stage == STAGE_4 and stages.get("UDN", {}).get("stage") == STAGE_2:
        sig = entry_signal("UDN", LONG, stages.get("UDN"), as_of_date, db_path)
        if sig["signal"] in (STRONG_ENTRY, MODERATE_ENTRY):
            return {"asset": "UDN", "direction": LONG, "tier": 3,
                    "signal_strength": sig["signal"], "entry_details": sig,
                    "crossover": check_sma_crossover("UDN", as_of_date, db_path),
                    "reason": f"UDN Stage 2, score {sig['score']}/4"}

    # Tier 4: Cash
    return {"asset": CASH_SYMBOL, "direction": HOLD, "tier": 4,
            "signal_strength": NO_ENTRY, "entry_details": None,
            "crossover": None, "reason": "No favorable entry — holding cash"}


# =============================================================================
# TRADE PARAMETERS
# =============================================================================

def calc_trade_params(entry_price, direction, stage, atr=None):
    """
    Calculate stop, target, trail for a new trade.

    If USE_ATR_STOPS is True and a valid ATR value is supplied, the initial
    stop is set at entry_price - (ATR_MULTIPLIER × ATR), clamped between
    ATR_MIN_STOP_PCT and ATR_MAX_STOP_PCT of entry price.
    Falls back to fixed-percentage stops when ATR is unavailable.
    """
    def _atr_stop(fixed_pct, inverse=False):
        """Return stop price using ATR if available, else fixed pct."""
        if USE_ATR_STOPS and atr and atr > 0:
            raw_distance = ATR_MULTIPLIER * atr
            max_distance = entry_price * (ATR_MAX_STOP_PCT if not inverse else MAX_STOP_PCT_INVERSE * 1.5)
            min_distance = entry_price * ATR_MIN_STOP_PCT
            distance = max(min_distance, min(raw_distance, max_distance))
            return entry_price - distance
        return entry_price * (1 - fixed_pct)

    if direction == LONG_INVERSE:
        return {"initial_stop": _atr_stop(MAX_STOP_PCT_INVERSE, inverse=True),
                "first_target": entry_price * (1 + FIRST_TARGET_PCT_INVERSE),
                "trailing_pct": TRAILING_STOP_PCT_INVERSE,
                "partial_exit_pct": PARTIAL_EXIT_PCT_INVERSE, "trade_type": "INVERSE"}
    elif stage == STAGE_3:
        return {"initial_stop": _atr_stop(MAX_STOP_PCT),
                "first_target": entry_price * (1 + FIRST_TARGET_PCT_STAGE3),
                "trailing_pct": TRAILING_STOP_PCT,
                "partial_exit_pct": PARTIAL_EXIT_PCT_STAGE3, "trade_type": "STAGE3"}
    else:
        return {"initial_stop": _atr_stop(MAX_STOP_PCT),
                "first_target": entry_price * (1 + FIRST_TARGET_PCT),
                "trailing_pct": TRAILING_STOP_PCT,
                "partial_exit_pct": PARTIAL_EXIT_PCT_STAGE2, "trade_type": "STANDARD"}


# =============================================================================
# EXIT CHECKS
# =============================================================================

def check_exits(position, current_close, current_date, symbol, as_of_date=None, db_path=None):
    """Check all exit conditions. Returns highest-priority exit or None."""

    # Priority 1: VIX Emergency
    vix = get_latest_vix(as_of_date, db_path)
    if vix:
        if vix.get("vix_close", 0) > VIX_EMERGENCY_LEVEL and vix.get("vix_trend") == "RISING":
            return {"action": "FULL_EXIT", "reason": "VIX_EMERGENCY", "exit_pct": 1.0,
                    "details": f"VIX={vix['vix_close']:.1f} >{VIX_EMERGENCY_LEVEL} and RISING"}

    # Priority 2: Stop Hit
    if current_close <= position["stop"]:
        return {"action": "FULL_EXIT", "reason": "STOP_HIT", "exit_pct": 1.0,
                "details": f"Close {current_close:.2f} <= Stop {position['stop']:.2f}"}

    # Priority 3: Stage Change
    stage_info = determine_stage(symbol, as_of_date, db_path)
    stage = stage_info["stage"]
    if position["direction"] == LONG and stage in (STAGE_3, STAGE_4):
        return {"action": "FULL_EXIT", "reason": "STAGE_CHANGE", "exit_pct": 1.0,
                "details": f"Stage changed to {stage}"}
    if position["direction"] == LONG_INVERSE and stage in (STAGE_1, STAGE_2):
        return {"action": "FULL_EXIT", "reason": "STAGE_CHANGE", "exit_pct": 1.0,
                "details": f"Stage changed to {stage}"}

    # Priority 4: First Target Hit
    if not position.get("partial_exited", False):
        if current_close >= position["first_target"]:
            exit_pct = position.get("partial_exit_pct", PARTIAL_EXIT_PCT_STAGE2)
            if position.get("entry_date"):
                days = _business_days_between(position["entry_date"], current_date)
                if days <= SPEED_CHECK_DAYS:
                    exit_pct = min(exit_pct + SPEED_CHECK_EXTRA_PCT, SPEED_CHECK_MAX_PCT)
            return {"action": "PARTIAL_EXIT", "reason": "TARGET_HIT", "exit_pct": exit_pct,
                    "details": f"Close {current_close:.2f} >= Target {position['first_target']:.2f} (exit {exit_pct*100:.0f}%)"}

    # Priority 5: Update Trailing Stop
    if position.get("partial_exited", False):
        trail_pct = position.get("trailing_pct", TRAILING_STOP_PCT)
        new_trail = current_close * (1 - trail_pct)
        if new_trail > position["stop"]:
            return {"action": "UPDATE_STOP", "reason": "TRAILING_STOP", "new_stop": new_trail,
                    "details": f"Trail: {position['stop']:.2f} -> {new_trail:.2f}"}

    return None


# =============================================================================
# INTERMARKET WARNINGS
# =============================================================================

def check_intermarket_warnings(as_of_date=None, db_path=None):
    """Check intermarket conditions. Returns list of warning strings."""
    warnings = []

    xlu = get_latest_indicators("XLU", as_of_date, db_path)
    spy = get_latest_indicators("SPY", as_of_date, db_path)
    if xlu and spy:
        xlu_rs = xlu.get("relative_strength")
        spy_rs = spy.get("relative_strength")
        if xlu_rs is not None and spy_rs is not None:
            diff = xlu_rs - spy_rs
            if diff > DEFENSIVE_ROTATION_THRESHOLD:
                warnings.append(f"DEFENSIVE ROTATION: Utilities outperforming SPY by {diff:.1f}%")

    gld = get_latest_indicators("GLD", as_of_date, db_path)
    if gld:
        gld_stage = determine_stage("GLD", as_of_date, db_path)
        spy_stage = determine_stage("SPY", as_of_date, db_path)
        if gld_stage["stage"] == STAGE_2 and spy_stage["stage"] == STAGE_3:
            warnings.append("COMMODITY CYCLE: Gold rising while equities in distribution")

    spy_stage = determine_stage("SPY", as_of_date, db_path)
    tlt_stage = determine_stage("TLT", as_of_date, db_path)
    if spy_stage["stage"] == STAGE_4 and tlt_stage["stage"] == STAGE_4:
        warnings.append("DIVERGENCE: Both stocks and bonds declining — check UUP/dollar")

    return warnings


# =============================================================================
# DAILY REPORT
# =============================================================================

def print_daily_report(as_of_date=None, db_path=None):
    """Print the full daily signal report."""
    print("=" * 60)
    print(f"DAILY SIGNAL REPORT")
    print("=" * 60)

    # Stages
    print_stage_summary(as_of_date, db_path)

    # VIX
    vix = get_latest_vix(as_of_date, db_path)
    if vix:
        print(f"  VIX: {vix.get('vix_close', 0):.1f}  "
              f"Regime: {vix.get('vix_regime')}  "
              f"Trend: {vix.get('vix_trend')}")
        print()

    # Asset Rotation
    rotation = asset_rotation(as_of_date, db_path)
    print("=== ASSET ROTATION ===\n")
    print(f"  Recommendation: {rotation['asset']} ({rotation['direction']})")
    print(f"  Tier: {rotation['tier']}")
    print(f"  Signal: {rotation['signal_strength']}")
    print(f"  Reason: {rotation['reason']}")

    if rotation["entry_details"]:
        print(f"\n  Pillar Details:")
        print(f"    {rotation['entry_details']['details']}")
        cross = rotation.get("crossover")
        if cross:
            if cross["bullish_cross"]:
                print(f"    BULLISH CROSSOVER: {cross['details']}")
            elif cross["bearish_cross"]:
                print(f"    BEARISH CROSSOVER: {cross['details']}")

    # Trade params if entry
    if rotation["direction"] != HOLD and rotation["entry_details"]:
        stages = get_all_stages(as_of_date, db_path)
        asset = rotation["asset"]
        # Get price for the traded asset
        ind = get_latest_indicators(asset, as_of_date, db_path)
        if ind:
            close = _get_close(asset, ind["date"], db_path)
            if close:
                stage = stages.get("SPY", {}).get("stage", STAGE_2)
                params = calc_trade_params(close, rotation["direction"], stage)
                print(f"\n  Trade Parameters (entry ~${close:.2f}):")
                print(f"    Stop Loss:    ${params['initial_stop']:.2f}")
                print(f"    First Target: ${params['first_target']:.2f}")
                print(f"    Partial Exit: {params['partial_exit_pct']*100:.0f}% at target")
                print(f"    Trail Stop:   {params['trailing_pct']*100:.0f}% after partial")

    # Warnings
    warnings = check_intermarket_warnings(as_of_date, db_path)
    if warnings:
        print(f"\n=== WARNINGS ===\n")
        for w in warnings:
            print(f"  {w}")
    else:
        print(f"\n  No intermarket warnings.")

    print()


# =============================================================================
# HELPERS
# =============================================================================

def _get_close(symbol, date_str, db_path=None):
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT close FROM prices WHERE symbol = ? AND date = ?",
            (symbol, date_str)
        ).fetchone()
        return row["close"] if row else None


def _get_open(symbol, date_str, db_path=None):
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT open FROM prices WHERE symbol = ? AND date = ?",
            (symbol, date_str)
        ).fetchone()
        return row["open"] if row else None


def _business_days_between(start_date, end_date):
    from datetime import datetime
    d1 = datetime.strptime(start_date, "%Y-%m-%d") if isinstance(start_date, str) else start_date
    d2 = datetime.strptime(end_date, "%Y-%m-%d") if isinstance(end_date, str) else end_date
    return max(1, int((d2 - d1).days * 5 / 7))
