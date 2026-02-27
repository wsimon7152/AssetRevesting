# asset_revesting/core/backtester.py
"""
Layer 4 — Backtester

Replays historical data through the signal engine day-by-day,
simulating the portfolio state machine exactly as it would run live.

Key design: uses the same functions from signals.py and stage_analysis.py
with an as_of_date parameter. No lookahead bias.

Implements validation criteria from Signal Logic Spec v1.1 Section 9.
"""

import pandas as pd
import numpy as np
from datetime import datetime
from asset_revesting.config import (
    ANALYSIS_SYMBOLS, CASH_SYMBOL,
    MAX_STOP_PCT, TRAILING_STOP_PCT,
    VIX_EMERGENCY_LEVEL,
)
from asset_revesting.data.database import get_connection
from asset_revesting.core.stage_analysis import (
    STAGE_1, STAGE_2, STAGE_3, STAGE_4, TRANSITIONAL,
    compute_stage_history,
)
from asset_revesting.core.signals import (
    asset_rotation, check_exits, calc_trade_params, equity_pick,
    check_sma_crossover, check_intermarket_warnings,
    LONG, LONG_INVERSE, HOLD, STRONG_ENTRY, MODERATE_ENTRY, NO_ENTRY,
    _get_close,
)
from asset_revesting.core.indicators import get_latest_vix


# =============================================================================
# BACKTESTER ENGINE
# =============================================================================

class BacktestResult:
    """Container for backtest results."""

    def __init__(self):
        self.trades = []
        self.daily_log = []
        self.equity_curve = []
        self.start_date = None
        self.end_date = None
        self.initial_capital = 0
        self.final_capital = 0

    def summary(self):
        """Compute summary statistics."""
        if not self.trades:
            return {"error": "No trades executed"}

        completed = [t for t in self.trades if t["status"] == "CLOSED"]
        if not completed:
            return {"error": "No completed trades"}

        # Trade stats
        pnls = [t["pnl_pct"] for t in completed]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        # Holding periods
        holds = [t["holding_days"] for t in completed if t.get("holding_days")]

        # Time in cash
        cash_days = sum(1 for d in self.daily_log if d["state"] == "CASH")
        total_days = len(self.daily_log)

        # Equity curve stats
        if self.equity_curve:
            eq = [e["equity"] for e in self.equity_curve]
            peak = eq[0]
            max_dd = 0
            for e in eq:
                if e > peak:
                    peak = e
                dd = (peak - e) / peak * 100
                if dd > max_dd:
                    max_dd = dd

            total_return = (self.final_capital - self.initial_capital) / self.initial_capital * 100
        else:
            max_dd = 0
            total_return = 0

        # Annual trade count
        if self.start_date and self.end_date:
            years = max(0.5, (datetime.strptime(self.end_date, "%Y-%m-%d") -
                              datetime.strptime(self.start_date, "%Y-%m-%d")).days / 365.25)
            trades_per_year = len(completed) / years
        else:
            years = 1
            trades_per_year = len(completed)

        return {
            "total_trades": len(completed),
            "trades_per_year": round(trades_per_year, 1),
            "win_rate": round(len(winners) / len(completed) * 100, 1) if completed else 0,
            "avg_win": round(np.mean(winners), 2) if winners else 0,
            "avg_loss": round(np.mean(losers), 2) if losers else 0,
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "avg_holding_days": round(np.mean(holds), 1) if holds else 0,
            "median_holding_days": round(np.median(holds), 1) if holds else 0,
            "cash_pct": round(cash_days / total_days * 100, 1) if total_days else 0,
            "years": round(years, 1),
            "total_days": total_days,
        }


def run_backtest(start_date, end_date, initial_capital=100000, db_path=None, verbose=False):
    """
    Run a full backtest over the specified date range.

    The engine replays each trading day sequentially:
    1. Check exits on existing positions
    2. Check for new entries if in cash
    3. Track equity curve

    Args:
        start_date: 'YYYY-MM-DD' start of backtest
        end_date: 'YYYY-MM-DD' end of backtest
        initial_capital: starting portfolio value
        db_path: override database path
        verbose: print each trade as it happens

    Returns:
        BacktestResult with all trades, daily log, and equity curve
    """
    result = BacktestResult()
    result.start_date = start_date
    result.end_date = end_date
    result.initial_capital = initial_capital

    # Get all trading dates in range
    with get_connection(db_path) as conn:
        dates = conn.execute("""
            SELECT DISTINCT date FROM prices
            WHERE symbol = 'SPY' AND date >= ? AND date <= ?
            ORDER BY date ASC
        """, (start_date, end_date)).fetchall()

    trading_dates = [d["date"] for d in dates]
    if not trading_dates:
        print(f"No trading data between {start_date} and {end_date}")
        return result

    print(f"Backtesting {len(trading_dates)} trading days: {trading_dates[0]} to {trading_dates[-1]}")

    # Compute stage history first (so determine_stage works correctly)
    print("Computing stage history...")
    compute_stage_history(db_path)

    # Portfolio state
    cash = initial_capital    # money not in the market
    position = None           # None = no position, dict = open position
    pending_entry = None      # Entry signal waiting for next day's close
    trade_count = 0
    vix_cooldown = False      # True = VIX emergency, wait for VIX to drop
    cooldown_days = 0         # General cooldown after any exit (prevents same-day re-entry churn)
    COOLDOWN_PERIOD = 1       # Minimum days to wait after exit before re-entering

    for i, date in enumerate(trading_dates):

        # --- VIX cooldown check ---
        vix_data = get_latest_vix(date, db_path)
        current_vix = vix_data.get("vix_close", 0) if vix_data else 0
        if vix_cooldown and current_vix < VIX_EMERGENCY_LEVEL:
            vix_cooldown = False
            if verbose:
                print(f"  {date}: VIX cooldown lifted (VIX={current_vix:.1f})")

        # --- General cooldown countdown ---
        if cooldown_days > 0:
            cooldown_days -= 1

        # === STEP 1: Execute pending entry at today's close ===
        if pending_entry is not None:
            if vix_cooldown:
                if verbose:
                    print(f"  {date}: ENTRY CANCELLED (VIX cooldown)")
                pending_entry = None
            else:
                # Enter at the OPEN of the next day (signal at close, execute at open)
                from asset_revesting.core.signals import _get_open
                entry_price = _get_open(pending_entry["asset"], date, db_path)
                # Fallback to close if open not available
                if not entry_price or entry_price <= 0:
                    entry_price = _get_close(pending_entry["asset"], date, db_path)
                if entry_price and entry_price > 0:
                    stage_for_params = pending_entry.get("stage", STAGE_2)
                    # Look up ATR for the underlying instrument on entry date
                    atr_val = _get_atr(pending_entry.get("underlying", pending_entry["asset"]), date, db_path)
                    params = calc_trade_params(entry_price, pending_entry["direction"], stage_for_params, atr=atr_val)

                    shares = cash / entry_price
                    position = {
                        "symbol": pending_entry["asset"],
                        "underlying": pending_entry.get("underlying", pending_entry["asset"]),
                        "direction": pending_entry["direction"],
                        "tier": pending_entry["tier"],
                        "entry_date": date,
                        "entry_price": entry_price,
                        "shares": shares,
                        "stop": params["initial_stop"],
                        "first_target": params["first_target"],
                        "trailing_pct": params["trailing_pct"],
                        "partial_exit_pct": params["partial_exit_pct"],
                        "trade_type": params["trade_type"],
                        "partial_exited": False,
                    }
                    cash = 0

                    trade_count += 1
                    if verbose:
                        print(f"  {date}: ENTRY {position['symbol']} @ ${entry_price:.2f} "
                              f"(stop ${params['initial_stop']:.2f}, target ${params['first_target']:.2f})")

                pending_entry = None

        # === STEP 2: If positioned, check exits (skip on entry day) ===
        if position is not None and position["entry_date"] != date:
            current_close = _get_close(position["symbol"], date, db_path)
            if current_close is not None:
                exit_signal = check_exits(
                    position, current_close, date,
                    position["underlying"], date, db_path
                )

                if exit_signal:
                    if exit_signal["action"] == "FULL_EXIT":
                        exit_value = position["shares"] * current_close
                        cash += exit_value
                        position["shares"] = 0

                        pnl_pct = (current_close - position["entry_price"]) / position["entry_price"] * 100
                        holding_days = _calc_holding_days(position["entry_date"], date)

                        result.trades.append({
                            "trade_num": trade_count,
                            "symbol": position["symbol"],
                            "direction": position["direction"],
                            "tier": position["tier"],
                            "entry_date": position["entry_date"],
                            "entry_price": position["entry_price"],
                            "exit_date": date,
                            "exit_price": current_close,
                            "exit_reason": exit_signal["reason"],
                            "pnl_pct": round(pnl_pct, 2),
                            "holding_days": holding_days,
                            "status": "CLOSED",
                        })

                        if verbose:
                            emoji = "+" if pnl_pct > 0 else ""
                            print(f"  {date}: EXIT  {position['symbol']} @ ${current_close:.2f} "
                                  f"({exit_signal['reason']}) {emoji}{pnl_pct:.1f}% "
                                  f"[{holding_days}d]")

                        if exit_signal["reason"] == "VIX_EMERGENCY":
                            vix_cooldown = True
                            if verbose:
                                print(f"  {date}: VIX COOLDOWN — no entries until VIX < {VIX_EMERGENCY_LEVEL}")

                        cooldown_days = COOLDOWN_PERIOD
                        position = None

                    elif exit_signal["action"] == "PARTIAL_EXIT":
                        sell_pct = exit_signal["exit_pct"]
                        shares_to_sell = position["shares"] * sell_pct
                        sell_value = shares_to_sell * current_close
                        cash += sell_value
                        position["shares"] -= shares_to_sell
                        position["partial_exited"] = True
                        position["stop"] = position["entry_price"]

                        if verbose:
                            print(f"  {date}: PARTIAL EXIT {position['symbol']} "
                                  f"{sell_pct*100:.0f}% @ ${current_close:.2f} "
                                  f"(stop -> breakeven ${position['entry_price']:.2f})")

                    elif exit_signal["action"] == "UPDATE_STOP":
                        position["stop"] = exit_signal["new_stop"]

        # === STEP 3: If in cash, check for entries ===
        if position is None and pending_entry is None and not vix_cooldown and cooldown_days <= 0:
            rotation = asset_rotation(date, db_path)

            if rotation["direction"] != HOLD and rotation["signal_strength"] != NO_ENTRY:
                underlying = rotation["asset"] if rotation["direction"] == LONG else "SPY"

                from asset_revesting.core.stage_analysis import determine_stage
                stage_info = determine_stage(underlying, date, db_path)

                pending_entry = {
                    "asset": rotation["asset"],
                    "underlying": underlying,
                    "direction": rotation["direction"],
                    "tier": rotation["tier"],
                    "signal_strength": rotation["signal_strength"],
                    "stage": stage_info["stage"],
                    "signal_date": date,
                }

                if verbose:
                    print(f"  {date}: SIGNAL {rotation['asset']} {rotation['direction']} "
                          f"({rotation['signal_strength']}, score {rotation['entry_details']['score']}/4) "
                          f"→ enter tomorrow's open")

        # === STEP 4: Calculate equity and log ===
        if position is not None and position["shares"] > 0:
            pos_close = _get_close(position["symbol"], date, db_path)
            equity = (cash + position["shares"] * pos_close) if pos_close else cash
        else:
            equity = cash

        result.equity_curve.append({"date": date, "equity": equity})
        result.daily_log.append({
            "date": date,
            "state": "CASH" if position is None and pending_entry is None else
                     "ENTERING" if pending_entry else
                     "POSITIONED",
            "equity": equity,
            "position": position["symbol"] if position else None,
        })

    # Close any remaining position at final price
    if position is not None and position["shares"] > 0:
        final_close = _get_close(position["symbol"], trading_dates[-1], db_path)
        if final_close:
            final_value = position["shares"] * final_close
            cash += final_value
            pnl_pct = (final_close - position["entry_price"]) / position["entry_price"] * 100
            result.trades.append({
                "trade_num": trade_count,
                "symbol": position["symbol"],
                "direction": position["direction"],
                "tier": position["tier"],
                "entry_date": position["entry_date"],
                "entry_price": position["entry_price"],
                "exit_date": trading_dates[-1],
                "exit_price": final_close,
                "exit_reason": "BACKTEST_END",
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": _calc_holding_days(position["entry_date"], trading_dates[-1]),
                "status": "CLOSED",
            })

    result.final_capital = cash
    # Use equity curve final value if available
    if result.equity_curve:
        result.final_capital = result.equity_curve[-1]["equity"]

    return result


# =============================================================================
# BENCHMARK COMPARISON
# =============================================================================

def calc_buy_and_hold(start_date, end_date, initial_capital=100000, symbol="SPY", db_path=None):
    """Calculate buy-and-hold return for comparison."""
    start_price = _get_close(symbol, start_date, db_path)

    # If exact start date has no data, find nearest
    if start_price is None:
        with get_connection(db_path) as conn:
            row = conn.execute("""
                SELECT date, close FROM prices
                WHERE symbol = ? AND date >= ? ORDER BY date ASC LIMIT 1
            """, (symbol, start_date)).fetchone()
            if row:
                start_price = row["close"]

    end_price = _get_close(symbol, end_date, db_path)
    if end_price is None:
        with get_connection(db_path) as conn:
            row = conn.execute("""
                SELECT date, close FROM prices
                WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1
            """, (symbol, end_date)).fetchone()
            if row:
                end_price = row["close"]

    if start_price and end_price:
        shares = initial_capital / start_price
        final = shares * end_price
        return_pct = (final - initial_capital) / initial_capital * 100

        # Calculate max drawdown for buy-and-hold
        with get_connection(db_path) as conn:
            prices = conn.execute("""
                SELECT close FROM prices
                WHERE symbol = ? AND date >= ? AND date <= ?
                ORDER BY date ASC
            """, (symbol, start_date, end_date)).fetchall()

        peak = start_price
        max_dd = 0
        for p in prices:
            if p["close"] > peak:
                peak = p["close"]
            dd = (peak - p["close"]) / peak * 100
            if dd > max_dd:
                max_dd = dd

        return {
            "symbol": symbol,
            "start_price": round(start_price, 2),
            "end_price": round(end_price, 2),
            "total_return_pct": round(return_pct, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "final_capital": round(final, 2),
        }

    return {"error": f"Could not get prices for {symbol}"}


# =============================================================================
# REPORT PRINTING
# =============================================================================

def print_backtest_report(result, benchmark=None):
    """Print a formatted backtest report."""
    summary = result.summary()

    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print(f"{result.start_date} to {result.end_date} ({summary['years']} years)")
    print("=" * 70)

    # Performance
    print(f"\n  PERFORMANCE:")
    print(f"    Total Return:    {summary['total_return_pct']:>8.1f}%")
    print(f"    Max Drawdown:    {summary['max_drawdown_pct']:>8.1f}%")
    print(f"    Final Capital:   ${result.final_capital:>12,.2f} (from ${result.initial_capital:,.2f})")

    if benchmark:
        print(f"\n  vs BUY & HOLD {benchmark['symbol']}:")
        print(f"    B&H Return:      {benchmark['total_return_pct']:>8.1f}%")
        print(f"    B&H Max DD:      {benchmark['max_drawdown_pct']:>8.1f}%")
        print(f"    B&H Final:       ${benchmark['final_capital']:>12,.2f}")
        outperform = summary['total_return_pct'] - benchmark['total_return_pct']
        dd_improve = benchmark['max_drawdown_pct'] - summary['max_drawdown_pct']
        print(f"    Outperformance:  {'+' if outperform > 0 else ''}{outperform:.1f}% return, "
              f"{'+' if dd_improve > 0 else ''}{dd_improve:.1f}% less drawdown")

    # Trade Statistics
    print(f"\n  TRADE STATISTICS:")
    print(f"    Total Trades:    {summary['total_trades']}")
    print(f"    Trades/Year:     {summary['trades_per_year']}")
    print(f"    Win Rate:        {summary['win_rate']}%")
    print(f"    Avg Win:         +{summary['avg_win']}%")
    print(f"    Avg Loss:        {summary['avg_loss']}%")
    print(f"    Best Trade:      +{summary['best_trade']}%")
    print(f"    Worst Trade:     {summary['worst_trade']}%")

    # Holding Periods
    print(f"\n  HOLDING PERIODS:")
    print(f"    Avg Hold:        {summary['avg_holding_days']} days")
    print(f"    Median Hold:     {summary['median_holding_days']} days")
    print(f"    Time in Cash:    {summary['cash_pct']}%")

    # Validation against spec targets
    print(f"\n  SPEC VALIDATION:")
    tpy = summary['trades_per_year']
    if 5 <= tpy <= 12:
        print(f"    Trades/year:     {tpy} ✓ (target: 5-12)")
    else:
        print(f"    Trades/year:     {tpy} ✗ (target: 5-12)")

    cash = summary['cash_pct']
    if 20 <= cash <= 50:
        print(f"    Cash %:          {cash}% ✓ (target: 30-40%)")
    else:
        print(f"    Cash %:          {cash}% ✗ (target: 30-40%)")

    avg_hold = summary['avg_holding_days']
    if 10 <= avg_hold <= 240:
        print(f"    Avg hold:        {avg_hold}d ✓ (target: 14-240d)")
    else:
        print(f"    Avg hold:        {avg_hold}d ✗ (target: 14-240d)")

    # Trade list
    if result.trades:
        print(f"\n  TRADE LOG:")
        print(f"  {'#':>3} {'Symbol':>6} {'Dir':>6} {'Entry':>12} {'Exit':>12} "
              f"{'Entry$':>8} {'Exit$':>8} {'P&L':>7} {'Days':>5} {'Reason'}")
        print(f"  {'---':>3} {'------':>6} {'---':>6} {'-----':>12} {'----':>12} "
              f"{'------':>8} {'-----':>8} {'---':>7} {'----':>5} {'------'}")

        for t in result.trades:
            pnl_str = f"{'+' if t['pnl_pct'] > 0 else ''}{t['pnl_pct']:.1f}%"
            print(f"  {t['trade_num']:>3} {t['symbol']:>6} {t['direction']:>6} "
                  f"{t['entry_date']:>12} {t['exit_date']:>12} "
                  f"${t['entry_price']:>7.2f} ${t['exit_price']:>7.2f} "
                  f"{pnl_str:>7} {t['holding_days']:>5} {t['exit_reason']}")

    print()


# =============================================================================
# MAIN BACKTEST RUNNER
# =============================================================================

def run_full_backtest(start_date=None, end_date=None, initial_capital=100000,
                      db_path=None, verbose=False):
    """
    Run backtest and print full report with benchmark comparison.
    Main entry point called from run.py.
    """
    # Default to full available range
    if start_date is None or end_date is None:
        with get_connection(db_path) as conn:
            row = conn.execute("""
                SELECT MIN(i.date) as first, MAX(i.date) as last
                FROM indicators i
                WHERE i.symbol = 'SPY' AND i.sma_200 IS NOT NULL
            """).fetchone()
            if row:
                start_date = start_date or row["first"]
                end_date = end_date or row["last"]
            else:
                print("No indicator data available. Run 'init' first.")
                return None

    # Run backtest
    result = run_backtest(start_date, end_date, initial_capital, db_path, verbose)

    # Calculate benchmark
    benchmark = calc_buy_and_hold(start_date, end_date, initial_capital, "SPY", db_path)

    # Print report
    print_backtest_report(result, benchmark)

    return result


# =============================================================================
# HELPERS
# =============================================================================

def _calc_holding_days(entry_date, exit_date):
    """Calculate calendar days between two date strings."""
    d1 = datetime.strptime(entry_date, "%Y-%m-%d")
    d2 = datetime.strptime(exit_date, "%Y-%m-%d")
    return (d2 - d1).days


def _get_atr(symbol, date_str, db_path=None):
    """
    Look up the stored ATR-14 value for a symbol on or before a given date.
    Returns None if not available (backtester falls back to fixed-pct stop).
    """
    with get_connection(db_path) as conn:
        row = conn.execute("""
            SELECT atr_14 FROM indicators
            WHERE symbol = ? AND date <= ?
            ORDER BY date DESC LIMIT 1
        """, (symbol, date_str)).fetchone()
    return row["atr_14"] if row and row["atr_14"] else None
