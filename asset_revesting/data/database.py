# asset_revesting/data/database.py
"""
SQLite database for storing price data, computed indicators, and trade history.
"""

import sqlite3
import os
from contextlib import contextmanager
from asset_revesting.config import DB_PATH


def get_db_path(db_path=None):
    """Get the database path, allowing override for testing."""
    return db_path or DB_PATH


@contextmanager
def get_connection(db_path=None):
    """Context manager for database connections."""
    conn = sqlite3.connect(get_db_path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    """Create all tables if they don't exist."""
    with get_connection(db_path) as conn:
        conn.executescript("""
            -- Raw daily price data from yfinance
            CREATE TABLE IF NOT EXISTS prices (
                symbol      TEXT NOT NULL,
                date        TEXT NOT NULL,
                open        REAL,
                high        REAL,
                low         REAL,
                close       REAL,
                volume      REAL,
                PRIMARY KEY (symbol, date)
            );

            -- VIX daily close (separate because it only has close)
            CREATE TABLE IF NOT EXISTS vix (
                date        TEXT NOT NULL PRIMARY KEY,
                close       REAL NOT NULL
            );

            -- NYSE Up/Down Volume (from Barchart + RSP proxy)
            CREATE TABLE IF NOT EXISTS nyse_volume (
                date        TEXT NOT NULL PRIMARY KEY,
                up_volume   REAL,
                down_volume REAL
            );

            -- Computed indicators (one row per symbol per date)
            CREATE TABLE IF NOT EXISTS indicators (
                symbol      TEXT NOT NULL,
                date        TEXT NOT NULL,
                sma_5       REAL,
                sma_20      REAL,
                sma_50      REAL,
                sma_150     REAL,
                sma_200     REAL,
                sma_150_slope REAL,
                sma_200_slope REAL,
                sma_50_slope  REAL,
                bb_upper    REAL,
                bb_middle   REAL,
                bb_lower    REAL,
                bb_bandwidth REAL,
                bb_percent_b REAL,
                relative_strength REAL,
                atr_14      REAL,
                PRIMARY KEY (symbol, date)
            );

            -- VIX indicators
            CREATE TABLE IF NOT EXISTS vix_indicators (
                date        TEXT NOT NULL PRIMARY KEY,
                vix_close   REAL,
                vix_regime  TEXT,
                vix_sma_5   REAL,
                vix_sma_20  REAL,
                vix_trend   TEXT,
                vix_daily_change REAL,
                vix_spike   INTEGER  -- 0 or 1
            );

            -- Volume ratio indicators
            CREATE TABLE IF NOT EXISTS volume_indicators (
                date            TEXT NOT NULL PRIMARY KEY,
                panic_ratio     REAL,
                fomo_ratio      REAL,
                panic_ratio_ma  REAL,
                fomo_ratio_ma   REAL
            );

            -- Stage analysis results
            CREATE TABLE IF NOT EXISTS stages (
                symbol      TEXT NOT NULL,
                date        TEXT NOT NULL,
                stage       TEXT NOT NULL,  -- STAGE_1, STAGE_2, STAGE_3, STAGE_4, TRANSITIONAL
                confirmed   INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
                consecutive_days INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, date)
            );

            -- Trade history
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                entry_date      TEXT,
                entry_price     REAL,
                exit_date       TEXT,
                exit_price      REAL,
                exit_reason     TEXT,
                shares          REAL,
                pnl_pct         REAL,
                pnl_dollar      REAL
            );

            -- Portfolio state (single row, updated each day)
            CREATE TABLE IF NOT EXISTS portfolio_state (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                date            TEXT,
                state           TEXT NOT NULL DEFAULT 'CASH',
                cash            REAL NOT NULL DEFAULT 100000,
                symbol          TEXT,
                direction       TEXT,
                entry_date      TEXT,
                entry_price     REAL,
                shares          REAL,
                stop_price      REAL,
                target_price    REAL,
                trailing_pct    REAL,
                partial_exit_pct REAL,
                vix_cooldown    INTEGER NOT NULL DEFAULT 0,
                last_updated    TEXT
            );

            -- Daily log (for equity curve)
            CREATE TABLE IF NOT EXISTS daily_log (
                date            TEXT NOT NULL PRIMARY KEY,
                state           TEXT,
                equity          REAL,
                symbol          TEXT,
                vix_regime      TEXT,
                signals         TEXT,
                warnings        TEXT
            );

            -- Create indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices(symbol, date);
            CREATE INDEX IF NOT EXISTS idx_indicators_symbol_date ON indicators(symbol, date);
            CREATE INDEX IF NOT EXISTS idx_stages_symbol_date ON stages(symbol, date);
            CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_date);
        """)

        # Initialize portfolio state if not exists
        conn.execute("""
            INSERT OR IGNORE INTO portfolio_state (id, state, cash, vix_cooldown, last_updated)
            VALUES (1, 'CASH', 100000, 0, datetime('now'))
        """)


def reset_db(db_path=None):
    """Drop and recreate all tables. Use for testing only."""
    path = get_db_path(db_path)
    if os.path.exists(path):
        os.remove(path)
    init_db(db_path)
