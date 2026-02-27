#!/usr/bin/env python3
# asset_revesting/run.py
"""
Main runner for the Asset Revesting Signal Engine.

Usage:
    python -m asset_revesting.run init       # First-time setup: fetch data + compute indicators
    python -m asset_revesting.run update     # Daily update: fetch latest + recompute
    python -m asset_revesting.run status     # Show current data and indicator status
    python -m asset_revesting.run verify     # Verify indicator calculations with spot checks
"""

import sys
import os

# Add parent directory to path so we can run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_revesting.data.database import init_db, get_connection
from asset_revesting.data.ingestion import fetch_all, get_data_summary
from asset_revesting.core.indicators import (
    compute_all_indicators,
    get_latest_indicators,
    get_latest_vix,
    get_indicator_history,
)
from asset_revesting.config import ANALYSIS_SYMBOLS


def cmd_init():
    """First-time setup: create DB, fetch all historical data, compute indicators, compute stages."""
    print("=" * 60)
    print("ASSET REVESTING SIGNAL ENGINE — INITIALIZATION")
    print("=" * 60)

    # Parse optional --start flag
    start_date = None
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == "--start" and i + 1 < len(args):
            start_date = args[i + 1]
    
    # Step 1: Create database
    print("\n[1/4] Initializing database...")
    init_db()
    print("  Database created.")
    
    # Step 2: Fetch all data
    print("\n[2/4] Fetching historical data...")
    results = fetch_all(start_date=start_date)
    
    # Step 3: Compute indicators
    print("\n[3/4] Computing indicators...")
    indicator_results = compute_all_indicators()
    
    # Step 4: Compute stage history for all dates
    print("\n[4/4] Computing stage history...")
    from asset_revesting.core.stage_analysis import compute_stage_history
    compute_stage_history()
    
    # Summary
    print("\n" + "=" * 60)
    print("INITIALIZATION COMPLETE")
    print("=" * 60)
    get_data_summary()


def cmd_update():
    """Daily update: fetch latest data and recompute indicators."""
    print("=" * 60)
    print("ASSET REVESTING — DAILY UPDATE")
    print("=" * 60)
    
    init_db()  # ensure tables exist
    
    print("\n[1/3] Fetching latest data...")
    from datetime import datetime, timedelta
    # Fetch last 10 days to catch any gaps
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    results = fetch_all(start_date=start)
    
    print("\n[2/3] Recomputing indicators...")
    indicator_results = compute_all_indicators()

    print("\n[3/3] Updating stage analysis...")
    from asset_revesting.core.stage_analysis import compute_stage_history
    compute_stage_history()
    
    print("\nUpdate complete.")


def cmd_status():
    """Show current data and indicator status."""
    init_db()
    get_data_summary()
    
    print("LATEST INDICATORS:")
    for symbol in ANALYSIS_SYMBOLS:
        ind = get_latest_indicators(symbol)
        if ind:
            print(f"\n  {symbol} (as of {ind['date']}):")
            print(f"    Close:    ${ind.get('sma_5', 0):>10.2f} (5-SMA)")
            print(f"    SMAs:     5={_fmt(ind.get('sma_5'))}  20={_fmt(ind.get('sma_20'))}  50={_fmt(ind.get('sma_50'))}  150={_fmt(ind.get('sma_150'))}  200={_fmt(ind.get('sma_200'))}")
            print(f"    150-Slope: {_fmt(ind.get('sma_150_slope'), pct=True)}")
            print(f"    BB:       Width={_fmt(ind.get('bb_bandwidth'), pct=True)}  %B={_fmt(ind.get('bb_percent_b'))}")
            print(f"    Rel Str:  {_fmt(ind.get('relative_strength'), pct=True)}")
        else:
            print(f"\n  {symbol}: No indicator data")
    
    vix = get_latest_vix()
    if vix:
        print(f"\n  VIX (as of {vix['date']}):")
        print(f"    Close:    {_fmt(vix.get('vix_close'))}")
        print(f"    Regime:   {vix.get('vix_regime')}")
        print(f"    Trend:    {vix.get('vix_trend')}")
        print(f"    Spike:    {'YES' if vix.get('vix_spike') else 'No'}")


def cmd_stages():
    """Show current stage analysis for all symbols."""
    init_db()
    from asset_revesting.core.stage_analysis import print_stage_summary
    print_stage_summary()


def cmd_signal():
    """Run the full daily signal evaluation and print the report."""
    init_db()
    from asset_revesting.core.signals import print_daily_report
    print_daily_report()


def cmd_backtest():
    """Run the backtester over available data."""
    init_db()
    from asset_revesting.core.backtester import run_full_backtest

    # Check for optional args: python -m asset_revesting.run backtest [start] [end] [--verbose]
    start = None
    end = None
    verbose = False

    args = sys.argv[2:]
    for arg in args:
        if arg == "--verbose" or arg == "-v":
            verbose = True
        elif start is None:
            start = arg
        elif end is None:
            end = arg

    run_full_backtest(start_date=start, end_date=end, verbose=verbose)


def cmd_report():
    """Generate and send the daily email report."""
    init_db()
    from asset_revesting.core.email_report import run_daily_report
    run_daily_report()


def cmd_test_email():
    """Send a test email to verify configuration."""
    init_db()
    from asset_revesting.core.email_report import get_email_config, generate_report, send_email

    config = get_email_config()
    if not config or not config.get("recipient_email"):
        print("Email not configured yet.")
        print("Open the dashboard and click 'Email Settings' to configure,")
        print("or configure via CLI:")
        print()
        print("  python -m asset_revesting.run configure-email")
        return

    print(f"Sending test email to {config['recipient_email']}...")
    report = generate_report()
    success = send_email(report)
    if success:
        print("✓ Test email sent successfully!")
    else:
        print("✗ Failed. Check your SMTP settings in the dashboard.")


def cmd_configure_email():
    """Interactive email configuration."""
    init_db()
    from asset_revesting.core.email_report import save_email_config, get_email_config

    print("=" * 60)
    print("EMAIL CONFIGURATION")
    print("=" * 60)
    print()
    print("For Gmail, you need an App Password (not your regular password):")
    print("  1. Go to myaccount.google.com → Security → 2-Step Verification")
    print("  2. At the bottom, click 'App passwords'")
    print("  3. Create one for 'Mail' → copy the 16-character password")
    print()

    existing = get_email_config()

    recipient = input(f"Send reports to [{existing.get('recipient_email', '')}]: ").strip()
    if not recipient and existing:
        recipient = existing["recipient_email"]

    smtp_user = input(f"Gmail address (sender) [{existing.get('smtp_user', '')}]: ").strip()
    if not smtp_user and existing:
        smtp_user = existing["smtp_user"]

    smtp_password = input("App password (16 chars, no spaces): ").strip()
    if not smtp_password and existing and existing.get("smtp_password"):
        smtp_password = existing["smtp_password"]
        print("  (keeping existing password)")

    save_email_config({
        "recipient_email": recipient,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "enabled": True,
    })

    print(f"\n✓ Email configured. Reports will be sent to {recipient}")
    print("  Test with: python -m asset_revesting.run test-email")


def cmd_schedule_install():
    """Install the automatic daily report scheduler."""
    init_db()
    from asset_revesting.core.scheduler import install_schedule
    result = install_schedule()
    if result["status"] == "ok":
        print(f"✓ {result['message']}")
        print(f"  Plist: {result.get('plist_path', '')}")
        print(f"  Log:   {result.get('log_path', '')}")
        print()
        print("  The report will run automatically, even after reboot.")
        print("  To stop: python -m asset_revesting.run schedule-remove")
    else:
        print(f"✗ {result['message']}")


def cmd_schedule_remove():
    """Remove the automatic daily report scheduler."""
    from asset_revesting.core.scheduler import uninstall_schedule
    result = uninstall_schedule()
    print(f"✓ {result['message']}")


def cmd_schedule_status():
    """Check scheduler status."""
    from asset_revesting.core.scheduler import get_schedule_status
    status = get_schedule_status()
    if status["installed"]:
        print(f"✓ Scheduler installed and {'active' if status['active'] else 'inactive'}")
        print(f"  Schedule: {status.get('schedule', '?')}")
        print(f"  Plist:    {status.get('plist_path', '?')}")
        print(f"  Log:      {status.get('log_path', '?')}")
        if status.get("last_run"):
            print(f"  Last run: {status['last_run']}")
    else:
        print("✗ Scheduler not installed")
        print("  Install with: python -m asset_revesting.run schedule-install")


def cmd_dashboard():
    """Launch the web dashboard."""
    init_db()

    # Auto-update data on startup
    print("Updating data before launching dashboard...")
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    try:
        results = fetch_all(start_date=start)
        compute_all_indicators()
        from asset_revesting.core.stage_analysis import compute_stage_history
        compute_stage_history()
        print("Data updated.\n")
    except Exception as e:
        print(f"Warning: data update failed ({e}). Using cached data.\n")

    try:
        import uvicorn
    except ImportError:
        print("Dashboard requires uvicorn and fastapi.")
        print("Install with: pip install fastapi uvicorn")
        sys.exit(1)

    print("Starting Asset Revesting dashboard...")
    print("Open http://localhost:8000 in your browser")
    print("Press Ctrl+C to stop\n")
    uvicorn.run("asset_revesting.app:app", host="0.0.0.0", port=8000, reload=False)


def cmd_verify():
    """
    Verify indicator calculations with spot checks.
    Fetches raw data and manually calculates a few indicators to compare.
    """
    import pandas as pd
    from asset_revesting.data.ingestion import get_price_dataframe
    from asset_revesting.core.indicators import calc_sma, calc_bollinger_bands
    
    print("=" * 60)
    print("INDICATOR VERIFICATION")
    print("=" * 60)
    
    init_db()
    
    symbol = "SPY"
    print(f"\nVerifying {symbol} indicators...\n")
    
    # Get raw prices
    prices = get_price_dataframe(symbol)
    if prices.empty:
        print("No price data available. Run 'init' first.")
        return
    
    close = prices["close"]
    
    # Manual SMA calculation
    manual_sma_5 = close.rolling(5).mean()
    manual_sma_20 = close.rolling(20).mean()
    manual_sma_200 = close.rolling(200).mean()
    
    # Get stored indicators
    stored = get_latest_indicators(symbol)
    if not stored:
        print("No stored indicators. Run 'init' first.")
        return
    
    latest_date = stored["date"]
    latest_close = close.iloc[-1]
    
    print(f"Latest date: {latest_date}")
    print(f"Latest close: ${latest_close:.2f}")
    print()
    
    # Compare SMAs
    checks = [
        ("SMA-5", manual_sma_5.iloc[-1], stored.get("sma_5")),
        ("SMA-20", manual_sma_20.iloc[-1], stored.get("sma_20")),
        ("SMA-200", manual_sma_200.iloc[-1], stored.get("sma_200")),
    ]
    
    all_pass = True
    for name, manual, stored_val in checks:
        if stored_val is None:
            print(f"  {name}: SKIP (no stored value)")
            continue
        
        diff = abs(manual - stored_val)
        ok = diff < 0.01  # tolerance
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {name}: Manual={manual:.4f}  Stored={stored_val:.4f}  Diff={diff:.6f}  {status}")
        if not ok:
            all_pass = False
    
    # Verify Bollinger Bands
    bb = calc_bollinger_bands(close)
    bb_checks = [
        ("BB-Upper", bb["upper"].iloc[-1], stored.get("bb_upper")),
        ("BB-Lower", bb["lower"].iloc[-1], stored.get("bb_lower")),
        ("BB-%B", bb["percent_b"].iloc[-1], stored.get("bb_percent_b")),
    ]
    
    print()
    for name, manual, stored_val in bb_checks:
        if stored_val is None:
            print(f"  {name}: SKIP (no stored value)")
            continue
        
        diff = abs(manual - stored_val)
        ok = diff < 0.01
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {name}: Manual={manual:.4f}  Stored={stored_val:.4f}  Diff={diff:.6f}  {status}")
        if not ok:
            all_pass = False
    
    print()
    if all_pass:
        print("ALL CHECKS PASSED ✓")
    else:
        print("SOME CHECKS FAILED ✗ — investigate discrepancies")


def _fmt(val, pct=False):
    """Format a value for display."""
    if val is None:
        return "N/A"
    if pct:
        return f"{val:.2f}%"
    return f"{val:.2f}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m asset_revesting.run [init|update|status|verify]")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == "init":
        cmd_init()
    elif command == "update":
        cmd_update()
    elif command == "status":
        cmd_status()
    elif command == "verify":
        cmd_verify()
    elif command == "stages":
        cmd_stages()
    elif command == "signal":
        cmd_signal()
    elif command == "backtest":
        cmd_backtest()
    elif command == "report":
        cmd_report()
    elif command == "test-email":
        cmd_test_email()
    elif command == "configure-email":
        cmd_configure_email()
    elif command == "schedule-install":
        cmd_schedule_install()
    elif command == "schedule-remove":
        cmd_schedule_remove()
    elif command == "schedule-status":
        cmd_schedule_status()
    elif command == "dashboard":
        cmd_dashboard()
    else:
        print(f"Unknown command: {command}")
        print("Available: init, update, status, verify, stages, signal, backtest, dashboard, report, test-email, configure-email, schedule-install, schedule-remove, schedule-status")
        sys.exit(1)


if __name__ == "__main__":
    main()
