# asset_revesting/data/ingestion.py
"""
Layer 1 — Data Ingestion

Fetches daily price data from yfinance (all ETFs + VIX) and
NYSE advancing/declining volume from Barchart.com (with RSP fallback).

Stores everything in SQLite via the database module.

Timing: All data is end-of-day. Run after market close (4 PM ET).
Chris's method: signal at close, execute at next day's open.
"""

import pandas as pd
from datetime import datetime, timedelta


def _flatten_columns(df):
    """
    Flatten MultiIndex columns from yfinance.
    Newer yfinance may return columns like ('Close', 'SPY') instead of 'Close'.
    This normalizes to simple column names: Close, Open, High, Low, Volume.
    """
    if isinstance(df.columns, pd.MultiIndex):
        # If grouped by ticker, first level is the field name
        # Try to flatten by taking just the first level
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    return df
from asset_revesting.config import (
    YFINANCE_SYMBOLS, VIX_SYMBOL, ALL_SYMBOLS,
    MIN_HISTORY_DAYS
)
from asset_revesting.data.database import get_connection


def fetch_yfinance_prices(symbols=None, start_date=None, end_date=None, db_path=None):
    """
    Fetch daily OHLCV data for all symbols from yfinance and store in SQLite.
    
    Args:
        symbols: List of ticker symbols. Defaults to ALL_SYMBOLS.
        start_date: Start date string 'YYYY-MM-DD'. Defaults to MIN_HISTORY_DAYS ago.
        end_date: End date string 'YYYY-MM-DD'. Defaults to today.
        db_path: Override database path (for testing).
    
    Returns:
        dict: {symbol: number_of_rows_inserted} for each symbol.
    """
    if symbols is None:
        symbols = ALL_SYMBOLS
    
    import yfinance as yf
    
    if end_date is None:
        # yfinance end date is EXCLUSIVE — add 1 day to include today's close
        end_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=int(MIN_HISTORY_DAYS * 1.5))).strftime("%Y-%m-%d")
    
    results = {}
    
    # Download all symbols at once for efficiency
    print(f"Fetching price data for {len(symbols)} symbols from {start_date} to {end_date}...")
    data = yf.download(symbols, start=start_date, end=end_date, group_by="ticker", progress=False)
    
    with get_connection(db_path) as conn:
        for symbol in symbols:
            try:
                if len(symbols) == 1:
                    df = _flatten_columns(data.copy())
                else:
                    df = data[symbol].copy()
                    df = _flatten_columns(df)
                
                # Normalize column names to title case
                df.columns = [c.title() if isinstance(c, str) else c for c in df.columns]
                
                df = df.dropna(subset=["Close"])
                
                if df.empty:
                    print(f"  WARNING: No data returned for {symbol}")
                    results[symbol] = 0
                    continue
                
                rows_inserted = 0
                for date_idx, row in df.iterrows():
                    date_str = date_idx.strftime("%Y-%m-%d")
                    conn.execute("""
                        INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        symbol, date_str,
                        float(row["Open"]) if pd.notna(row["Open"]) else None,
                        float(row["High"]) if pd.notna(row["High"]) else None,
                        float(row["Low"]) if pd.notna(row["Low"]) else None,
                        float(row["Close"]),
                        float(row["Volume"]) if pd.notna(row.get("Volume", None)) else None,
                    ))
                    rows_inserted += 1
                
                results[symbol] = rows_inserted
                print(f"  {symbol}: {rows_inserted} rows")
                
            except Exception as e:
                print(f"  ERROR fetching {symbol}: {e}")
                results[symbol] = 0
    
    return results


def fetch_vix(start_date=None, end_date=None, db_path=None):
    """
    Fetch VIX daily close from yfinance and store in SQLite.
    
    Returns:
        int: Number of rows inserted.
    """
    if end_date is None:
        # yfinance end date is EXCLUSIVE — add 1 day to include today's close
        end_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=int(MIN_HISTORY_DAYS * 1.5))).strftime("%Y-%m-%d")
    
    import yfinance as yf
    
    print(f"Fetching VIX data from {start_date} to {end_date}...")
    data = yf.download(VIX_SYMBOL, start=start_date, end=end_date, progress=False)
    data = _flatten_columns(data)
    data.columns = [c.title() if isinstance(c, str) else c for c in data.columns]
    data = data.dropna(subset=["Close"])
    
    rows_inserted = 0
    with get_connection(db_path) as conn:
        for date_idx, row in data.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d")
            conn.execute("""
                INSERT OR REPLACE INTO vix (date, close)
                VALUES (?, ?)
            """, (date_str, float(row["Close"])))
            rows_inserted += 1
    
    print(f"  VIX: {rows_inserted} rows")
    return rows_inserted




def scrape_barchart_nyse_ratio(db_path=None):
    """
    Scrape current NYSE advance/decline ratio from Barchart.com ($ADRN).
    
    Uses BeautifulSoup to parse the performance page and extract "Last Price".
    The ratio = advancing volume / declining volume:
    - > 1.0 = more advancing (bullish breadth)
    - < 1.0 = more declining (bearish breadth)
    - > 3.0 = extreme buying (FOMO)
    - < 0.33 = extreme selling (panic, inverse > 3.0)
    
    Called once daily after market close (4 PM ET).
    """
    import requests
    
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  beautifulsoup4 not installed — run: pip install beautifulsoup4")
        return None
    
    print("Scraping NYSE A/D ratio from Barchart.com ($ADRN)...")
    
    try:
        url = "https://www.barchart.com/stocks/quotes/$ADRN/performance"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        ratio = None
        for i, line in enumerate(lines):
            if line == "Last Price":
                ratio = float(lines[i + 1])
                break
        
        if ratio is None:
            print("  Could not find 'Last Price' on Barchart page")
            return None
        
        print(f"  NYSE A/D Ratio: {ratio:.3f}")
        
        # Convert ratio to up_volume / down_volume for our DB schema
        today = datetime.now().strftime("%Y-%m-%d")
        base_volume = 1_000_000_000  # normalized base
        
        if ratio >= 1.0:
            up_vol = base_volume * ratio
            down_vol = base_volume
        else:
            up_vol = base_volume
            down_vol = base_volume / ratio if ratio > 0 else base_volume
        
        with get_connection(db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO nyse_volume (date, up_volume, down_volume)
                VALUES (?, ?, ?)
            """, (today, up_vol, down_vol))
        
        return ratio
        
    except Exception as e:
        print(f"  Barchart scrape failed: {e}")
        return None


def _compute_rsp_breadth(db_path=None):
    """
    Compute historical breadth from RSP vs SPY divergence.
    
    RSP (equal-weight S&P 500) vs SPY (cap-weight):
    - RSP outperforms → broad participation (healthy)
    - SPY outperforms → narrow mega-cap rally (warning)
    
    Used to backfill historical data for backtesting.
    """
    import yfinance as yf
    
    print("  Computing RSP vs SPY breadth proxy for historical data...")
    
    try:
        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(MIN_HISTORY_DAYS * 1.5))).strftime("%Y-%m-%d")
        
        rsp_data = yf.download("RSP", start=start, end=end, progress=False)
        if rsp_data.empty:
            print("  RSP unavailable")
            return 0
        
        if hasattr(rsp_data.columns, 'levels') and len(rsp_data.columns.levels) > 1:
            rsp_data.columns = [c[0] if isinstance(c, tuple) else c for c in rsp_data.columns]
        rsp_data.columns = [c.title() if isinstance(c, str) else c for c in rsp_data.columns]
        
        with get_connection(db_path) as conn:
            spy_rows = conn.execute(
                "SELECT date, close, volume FROM prices WHERE symbol='SPY' ORDER BY date"
            ).fetchall()
        
        if not spy_rows:
            return 0
        
        spy_data = {r["date"]: {"close": r["close"], "volume": r["volume"]} for r in spy_rows}
        rows_inserted = 0
        prev_spy = None
        prev_rsp = None
        
        with get_connection(db_path) as conn:
            for idx, row in rsp_data.iterrows():
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, 'strftime') else str(idx)[:10]
                rsp_close = float(row.get("Close", 0))
                
                if date_str not in spy_data or rsp_close <= 0:
                    prev_rsp = rsp_close if rsp_close > 0 else prev_rsp
                    continue
                
                spy_close = spy_data[date_str]["close"]
                spy_volume = spy_data[date_str]["volume"]
                
                if prev_spy and prev_rsp and spy_close > 0:
                    spy_ret = (spy_close - prev_spy) / prev_spy
                    rsp_ret = (rsp_close - prev_rsp) / prev_rsp
                    breadth_diff = rsp_ret - spy_ret
                    
                    if breadth_diff > 0:
                        up_vol = spy_volume * (1.0 + min(breadth_diff * 100, 2.0))
                        down_vol = spy_volume * max(0.3, 1.0 - min(breadth_diff * 100, 0.7))
                    else:
                        up_vol = spy_volume * max(0.3, 1.0 + max(breadth_diff * 100, -0.7))
                        down_vol = spy_volume * (1.0 + min(abs(breadth_diff) * 100, 2.0))
                    
                    if spy_ret > 0.01:
                        up_vol *= 1.3
                    elif spy_ret < -0.01:
                        down_vol *= 1.3
                    
                    conn.execute("""
                        INSERT OR IGNORE INTO nyse_volume (date, up_volume, down_volume)
                        VALUES (?, ?, ?)
                    """, (date_str, up_vol, down_vol))
                    rows_inserted += 1
                
                prev_spy = spy_close
                prev_rsp = rsp_close
        
        print(f"  RSP breadth proxy: {rows_inserted} rows")
        return rows_inserted
        
    except Exception as e:
        print(f"  RSP breadth failed: {e}")
        return 0


def fetch_nyse_volume(api_key=None, db_path=None):
    """
    Fetch NYSE breadth data.
    
    1. Scrape today's real A/D ratio from Barchart ($ADRN)
    2. Backfill historical with RSP vs SPY proxy for backtests
    """
    # Try Barchart for today's live ratio
    ratio = scrape_barchart_nyse_ratio(db_path)
    
    # Backfill historical with RSP proxy
    hist_rows = _compute_rsp_breadth(db_path)
    
    if ratio is not None:
        print(f"  Today's NYSE A/D ratio: {ratio:.3f} (live from Barchart)")
    
    return hist_rows


def fetch_all(start_date=None, end_date=None, db_path=None):
    """
    Fetch all data sources. Main entry point for data ingestion.
    
    Returns:
        dict: Summary of all fetches.
    """
    results = {}
    
    # Fetch all ETF prices
    price_results = fetch_yfinance_prices(
        symbols=ALL_SYMBOLS,
        start_date=start_date,
        end_date=end_date,
        db_path=db_path
    )
    results["prices"] = price_results
    
    # Fetch warning symbols (XLU, GLD for intermarket analysis)
    from asset_revesting.config import WARNING_SYMBOLS
    warning_results = fetch_yfinance_prices(
        symbols=WARNING_SYMBOLS,
        start_date=start_date,
        end_date=end_date,
        db_path=db_path
    )
    results["warning_symbols"] = warning_results
    
    # Fetch VIX
    vix_rows = fetch_vix(start_date=start_date, end_date=end_date, db_path=db_path)
    results["vix"] = vix_rows
    
    # Fetch NYSE breadth from Barchart (with RSP fallback)
    nyse_rows = fetch_nyse_volume(db_path=db_path)
    results["nyse_volume"] = nyse_rows
    
    return results


def get_price_dataframe(symbol, start_date=None, end_date=None, db_path=None):
    """
    Retrieve price data from SQLite as a pandas DataFrame.
    Useful for indicator calculations.
    
    Returns:
        pd.DataFrame with columns: open, high, low, close, volume
        Index: datetime
    """
    with get_connection(db_path) as conn:
        query = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ?"
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


def get_vix_dataframe(start_date=None, end_date=None, db_path=None):
    """
    Retrieve VIX data from SQLite as a pandas DataFrame.
    
    Returns:
        pd.DataFrame with column: close
        Index: datetime
    """
    with get_connection(db_path) as conn:
        query = "SELECT date, close FROM vix WHERE 1=1"
        params = []
        
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


def get_nyse_volume_dataframe(start_date=None, end_date=None, db_path=None):
    """
    Retrieve NYSE volume data from SQLite as a pandas DataFrame.
    
    Returns:
        pd.DataFrame with columns: up_volume, down_volume
        Index: datetime
    """
    with get_connection(db_path) as conn:
        query = "SELECT date, up_volume, down_volume FROM nyse_volume WHERE 1=1"
        params = []
        
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


def get_data_summary(db_path=None):
    """
    Print a summary of what data is available in the database.
    """
    with get_connection(db_path) as conn:
        print("\n=== DATA SUMMARY ===\n")
        
        # Prices
        rows = conn.execute("""
            SELECT symbol, COUNT(*) as rows, MIN(date) as first, MAX(date) as last
            FROM prices GROUP BY symbol ORDER BY symbol
        """).fetchall()
        
        print("PRICE DATA:")
        for r in rows:
            print(f"  {r['symbol']:6s}: {r['rows']:5d} rows  ({r['first']} to {r['last']})")
        
        # VIX
        row = conn.execute("SELECT COUNT(*) as rows, MIN(date) as first, MAX(date) as last FROM vix").fetchone()
        print(f"\nVIX DATA:")
        print(f"  ^VIX  : {row['rows']:5d} rows  ({row['first']} to {row['last']})")
        
        # NYSE Volume
        row = conn.execute("SELECT COUNT(*) as rows, MIN(date) as first, MAX(date) as last FROM nyse_volume").fetchone()
        print(f"\nNYSE VOLUME:")
        if row["rows"] > 0:
            print(f"  NYSE  : {row['rows']:5d} rows  ({row['first']} to {row['last']})")
        else:
            print("  No data (Alpha Vantage API key needed)")
        
        print()
