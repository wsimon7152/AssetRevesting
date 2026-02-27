# asset_revesting/core/portfolio.py
"""
Layer 5 — Portfolio Tracker

Manages the live portfolio state machine:
  CASH → ENTERING → POSITIONED → PARTIAL_EXIT → CASH

Tracks current position, logs trades, and provides the daily
runner that coordinates data updates + signal evaluation.
"""

from datetime import datetime, date
from asset_revesting.config import (
    CASH_SYMBOL, VIX_EMERGENCY_LEVEL,
)
from asset_revesting.data.database import get_connection, init_db
from asset_revesting.core.indicators import (
    get_latest_indicators, get_latest_vix, get_latest_volume,
    compute_all_indicators,
)
from asset_revesting.core.stage_analysis import (
    STAGE_1, STAGE_2, STAGE_3, STAGE_4, TRANSITIONAL,
    determine_stage, get_all_stages, compute_stage_history,
)
from asset_revesting.core.signals import (
    asset_rotation, check_exits, calc_trade_params,
    check_intermarket_warnings, check_sma_crossover,
    LONG, LONG_INVERSE, HOLD, STRONG_ENTRY, MODERATE_ENTRY, NO_ENTRY,
    _get_close,
)


# =============================================================================
# STATE CONSTANTS
# =============================================================================

STATE_CASH = "CASH"
STATE_ENTERING = "ENTERING"
STATE_POSITIONED = "POSITIONED"
STATE_PARTIAL = "PARTIAL_EXIT"


# =============================================================================
# PORTFOLIO STATE
# =============================================================================

def get_portfolio_state(db_path=None):
    """
    Get the current portfolio state from the database.
    Returns dict with state, position details, and trade history.
    """
    with get_connection(db_path) as conn:
        state_row = conn.execute("""
            SELECT * FROM portfolio_state ORDER BY date DESC LIMIT 1
        """).fetchone()

    if state_row is None:
        return {
            "state": STATE_CASH,
            "cash": 100000,
            "position": None,
            "vix_cooldown": False,
            "date": None,
        }

    position = None
    if state_row["state"] in (STATE_POSITIONED, STATE_PARTIAL, STATE_ENTERING):
        position = {
            "symbol": state_row["symbol"],
            "direction": state_row["direction"],
            "entry_date": state_row["entry_date"],
            "entry_price": state_row["entry_price"],
            "shares": state_row["shares"],
            "stop": state_row["stop_price"],
            "first_target": state_row["target_price"],
            "trailing_pct": state_row["trailing_pct"],
            "partial_exited": state_row["state"] == STATE_PARTIAL,
            "partial_exit_pct": state_row["partial_exit_pct"],
            "stop_order_date": state_row["stop_order_date"],
        }

    return {
        "state": state_row["state"],
        "cash": state_row["cash"],
        "position": position,
        "vix_cooldown": bool(state_row["vix_cooldown"]),
        "date": state_row["date"],
    }


def save_portfolio_state(state_dict, db_path=None):
    """Save the current portfolio state to the database."""
    pos = state_dict.get("position")

    with get_connection(db_path) as conn:
        conn.execute("""
            UPDATE portfolio_state SET
                date=?, state=?, cash=?, symbol=?, direction=?, entry_date=?,
                entry_price=?, shares=?, stop_price=?, target_price=?,
                trailing_pct=?, partial_exit_pct=?, vix_cooldown=?,
                stop_order_date=?, last_updated=datetime('now')
            WHERE id = 1
        """, (
            state_dict.get("date") or date.today().isoformat(),
            state_dict.get("state", STATE_CASH),
            state_dict.get("cash", 0),
            pos.get("symbol") if pos else None,
            pos.get("direction") if pos else None,
            pos.get("entry_date") if pos else None,
            pos.get("entry_price") if pos else None,
            pos.get("shares") if pos else None,
            pos.get("stop") if pos else None,
            pos.get("first_target") if pos else None,
            pos.get("trailing_pct") if pos else None,
            pos.get("partial_exit_pct") if pos else None,
            1 if state_dict.get("vix_cooldown") else 0,
            pos.get("stop_order_date") if pos else None,
        ))


def log_trade(trade_dict, db_path=None):
    """Log a completed trade to the trades table."""
    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO trades
            (symbol, direction, entry_date, entry_price, exit_date, exit_price,
             exit_reason, shares, pnl_pct, pnl_dollar)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_dict["symbol"],
            trade_dict["direction"],
            trade_dict["entry_date"],
            trade_dict["entry_price"],
            trade_dict.get("exit_date"),
            trade_dict.get("exit_price"),
            trade_dict.get("exit_reason"),
            trade_dict.get("shares"),
            trade_dict.get("pnl_pct"),
            trade_dict.get("pnl_dollar"),
        ))


def get_trade_history(limit=50, db_path=None):
    """Get recent trade history."""
    with get_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM trades ORDER BY entry_date DESC LIMIT ?
        """, (limit,)).fetchall()

    return [dict(r) for r in rows]


# =============================================================================
# DASHBOARD DATA
# =============================================================================

def get_dashboard_data(db_path=None):
    """
    Get all data needed for the dashboard in a single call.
    Returns a dict with everything the frontend needs.
    """
    today = date.today().isoformat()

    # Find the latest date we actually have data for
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT MAX(date) as latest FROM prices WHERE symbol = 'SPY'").fetchone()
        data_date = row["latest"] if row else "unknown"

    # Portfolio state
    portfolio = get_portfolio_state(db_path)

    # Current position value
    position_value = 0
    total_equity = portfolio["cash"]
    if portfolio["position"]:
        pos = portfolio["position"]
        current_price = _get_close(pos["symbol"], today, db_path)
        if current_price is None:
            # Try getting the latest price regardless of date
            with get_connection(db_path) as conn:
                row = conn.execute("""
                    SELECT close FROM prices WHERE symbol = ?
                    ORDER BY date DESC LIMIT 1
                """, (pos["symbol"],)).fetchone()
                current_price = row["close"] if row else pos["entry_price"]

        position_value = pos["shares"] * current_price
        total_equity = portfolio["cash"] + position_value
        unrealized_pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
    else:
        current_price = None
        unrealized_pnl = None

    # Stages
    stages = {}
    for symbol in ["SPY", "QQQ", "TLT", "UUP", "UDN"]:
        stage_info = determine_stage(symbol, db_path=db_path)
        stages[symbol] = {
            "stage": stage_info["stage"],
            "raw_stage": stage_info["raw_stage"],
            "confirmed": stage_info["confirmed"],
            "date": stage_info["date"],
        }

    # VIX
    vix = get_latest_vix(db_path=db_path)
    vix_info = {
        "close": vix.get("vix_close") if vix else None,
        "regime": vix.get("vix_regime") if vix else None,
        "trend": vix.get("vix_trend") if vix else None,
        "spike": vix.get("vix_spike") if vix else None,
    }

    # Latest indicators for all symbols
    indicators = {}
    for symbol in ["SPY", "QQQ", "TLT", "UUP", "UDN"]:
        ind = get_latest_indicators(symbol, db_path=db_path)
        if ind:
            close = _get_close(symbol, ind["date"], db_path)
            indicators[symbol] = {
                "date": ind["date"],
                "close": close,
                "sma_5": ind.get("sma_5"),
                "sma_20": ind.get("sma_20"),
                "sma_50": ind.get("sma_50"),
                "sma_150": ind.get("sma_150"),
                "sma_200": ind.get("sma_200"),
                "sma_150_slope": ind.get("sma_150_slope"),
                "bb_bandwidth": ind.get("bb_bandwidth"),
                "bb_percent_b": ind.get("bb_percent_b"),
                "relative_strength": ind.get("relative_strength"),
            }

    # Asset rotation signal
    rotation = asset_rotation(db_path=db_path)

    # Volume breadth
    from asset_revesting.core.signals import volume_check
    vol_data = get_latest_volume(db_path=db_path)
    vol_check = volume_check(db_path=db_path)
    # Get raw NYSE A/D ratio from latest nyse_volume row
    nyse_ad_ratio = None
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT up_volume, down_volume FROM nyse_volume ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row and row["down_volume"] and row["down_volume"] > 0:
                nyse_ad_ratio = round(row["up_volume"] / row["down_volume"], 3)
    except Exception:
        pass

    volume_info = {
        "panic_ratio": vol_data.get("panic_ratio") if vol_data else None,
        "fomo_ratio": vol_data.get("fomo_ratio") if vol_data else None,
        "panic_ratio_ma": vol_data.get("panic_ratio_ma") if vol_data else None,
        "fomo_ratio_ma": vol_data.get("fomo_ratio_ma") if vol_data else None,
        "date": vol_data.get("date") if vol_data else None,
        "nyse_ad_ratio": nyse_ad_ratio,
        "favorable": vol_check.get("favorable", False),
        "flags": vol_check.get("flags", []),
        "available": vol_data is not None,
    }

    # Intermarket warnings
    warnings = check_intermarket_warnings(db_path=db_path)

    # Trade history
    trades = get_trade_history(20, db_path)

    # Recent equity curve from daily_log
    with get_connection(db_path) as conn:
        equity_rows = conn.execute("""
            SELECT date, equity FROM daily_log
            ORDER BY date DESC LIMIT 120
        """).fetchall()

    equity_curve = [{"date": r["date"], "equity": r["equity"]} for r in reversed(equity_rows)]

    # Build position response dict (enriched with ATR stop recommendation if applicable)
    position_response = None
    if portfolio["position"]:
        pos = portfolio["position"]
        position_response = {
            "symbol": pos["symbol"],
            "direction": pos["direction"],
            "entry_date": pos["entry_date"],
            "entry_price": pos["entry_price"],
            "current_price": current_price,
            "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            "stop": pos["stop"],
            "target": pos["first_target"],
            "partial_exited": pos["partial_exited"],
        }
        # Broker stop order expiry tracking
        try:
            from asset_revesting.config import BROKER_STOP_ORDER_DAYS, BROKER_STOP_WARN_DAYS
            sod = pos.get("stop_order_date")
            if sod:
                from datetime import date as _date
                placed = _date.fromisoformat(sod)
                days_since = (date.today() - placed).days
                days_left = BROKER_STOP_ORDER_DAYS - days_since
                position_response["stop_order_date"] = sod
                position_response["stop_order_days_left"] = days_left
                position_response["stop_order_warning"] = days_left <= BROKER_STOP_WARN_DAYS
            else:
                position_response["stop_order_date"] = None
                position_response["stop_order_days_left"] = None
                position_response["stop_order_warning"] = False
        except Exception:
            pass

        # ATR-based stop recommendation — compare stored stop to what ATR system would set
        try:
            from asset_revesting.config import (
                USE_ATR_STOPS, ATR_MULTIPLIER, ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT
            )
            from asset_revesting.core.backtester import _get_atr
            if USE_ATR_STOPS and pos["entry_price"] and pos["stop"]:
                atr = _get_atr(pos["symbol"], pos["entry_date"], db_path)
                if atr:
                    raw_dist = ATR_MULTIPLIER * atr
                    min_dist = pos["entry_price"] * ATR_MIN_STOP_PCT
                    max_dist = pos["entry_price"] * ATR_MAX_STOP_PCT
                    dist = max(min_dist, min(raw_dist, max_dist))
                    atr_stop = round(pos["entry_price"] - dist, 2)
                    atr_diff = round(atr_stop - pos["stop"], 2)
                    position_response["atr_stop_recommended"] = atr_stop
                    position_response["atr_stop_diff"] = atr_diff
        except Exception:
            pass  # ATR enrichment is best-effort; never break the dashboard

    return {
        "date": today,
        "data_date": data_date,
        "portfolio": {
            "state": portfolio["state"],
            "cash": round(portfolio["cash"], 2),
            "position_value": round(position_value, 2),
            "total_equity": round(total_equity, 2),
            "vix_cooldown": portfolio["vix_cooldown"],
        },
        "position": position_response,
        "signal": {
            "asset": rotation["asset"],
            "direction": rotation["direction"],
            "tier": rotation["tier"],
            "strength": rotation["signal_strength"],
            "reason": rotation["reason"],
            "score": rotation["entry_details"]["score"] if rotation["entry_details"] else None,
            "details": rotation["entry_details"]["details"] if rotation["entry_details"] else None,
        },
        "stages": stages,
        "vix": vix_info,
        "volume": volume_info,
        "indicators": indicators,
        "warnings": warnings,
        "trades": trades,
        "equity_curve": equity_curve,
    }
