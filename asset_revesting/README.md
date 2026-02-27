# Asset Revesting Signal Engine

An end-of-day trading signal system based on Chris Vermeulen's Asset Revesting strategy. It monitors five asset classes across a 4-tier hierarchy, evaluates four "pillars" for entry/exit decisions, and delivers clear daily instructions via email — so you spend minutes, not hours, managing your portfolio.

**Philosophy:** Look at markets once a day, after the close. Act the next morning. Target 5–12 trades per year with 30–40% in cash.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [First Run](#first-run)
5. [Daily Usage](#daily-usage)
6. [Email Reports](#email-reports)
7. [Automatic Scheduling](#automatic-scheduling)
8. [Dashboard Guide](#dashboard-guide)
9. [CLI Reference](#cli-reference)
10. [How the System Works](#how-the-system-works)
11. [Project Structure](#project-structure)
12. [Configuration](#configuration)
13. [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
# 1. Clone or copy to your home directory
mkdir ~/AssetRevesting
cp -R asset_revesting ~/AssetRevesting/

# 2. Install dependencies
pip install yfinance pandas beautifulsoup4 requests fastapi uvicorn

# 3. Initialize (fetches 7 years of data, ~2 minutes)
cd ~/AssetRevesting
python -m asset_revesting.run init --start 2019-01-01

# 4. Launch the dashboard
python -m asset_revesting.run dashboard
# Opens at http://localhost:8000
```

---

## Prerequisites

**Operating System:** macOS (tested on macOS 14+). Linux should work for everything except the automatic scheduler (which uses macOS LaunchAgent). Windows users can use Task Scheduler manually.

**Python:** 3.10 or newer. Tested with Python 3.12 and 3.13.

Check your version:
```bash
python --version
```

If you're using Miniconda/Anaconda, the default `base` environment works fine:
```bash
conda activate base
```

**Internet connection:** Required for fetching market data (Yahoo Finance) and NYSE breadth data (Barchart.com). Not required for viewing the dashboard with cached data.

---

## Installation

### Step 1: Install Python Dependencies

```bash
pip install yfinance pandas beautifulsoup4 requests fastapi uvicorn
```

If you get a permissions error, add `--user` or use `--break-system-packages` (Python 3.12+):
```bash
pip install --break-system-packages yfinance pandas beautifulsoup4 requests fastapi uvicorn
```

**Required packages:**

| Package | Purpose |
|---------|---------|
| `yfinance` | Daily price data for ETFs and VIX |
| `pandas` | Data manipulation and indicator calculations |
| `beautifulsoup4` | Scraping NYSE A/D ratio from Barchart.com |
| `requests` | HTTP requests for Barchart scraper |
| `fastapi` | Dashboard web server |
| `uvicorn` | ASGI server for FastAPI |

**No API keys needed.** All data comes from free, public sources (Yahoo Finance and Barchart.com).

### Step 2: Set Up the Project

Choose a permanent location. We recommend your home directory:

```bash
mkdir ~/AssetRevesting
cd ~/AssetRevesting
```

Copy or extract the `asset_revesting` folder here so the structure looks like:

```
~/AssetRevesting/
├── asset_revesting/        ← the Python package
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   ├── config.py
│   ├── run.py
│   ├── core/
│   ├── data/
│   ├── static/
│   └── tests/
└── asset_revesting.db      ← created on first run
```

### Step 3: Verify Installation

```bash
cd ~/AssetRevesting
python -c "import yfinance, pandas, bs4, requests, fastapi, uvicorn; print('All dependencies OK')"
```

---

## First Run

Initialize the database with historical data. This fetches ~7 years of daily prices, computes all indicators, and runs stage analysis:

```bash
cd ~/AssetRevesting
python -m asset_revesting.run init --start 2019-01-01
```

This takes about 2 minutes and will:

1. Create the SQLite database (`asset_revesting.db`)
2. Download daily prices for SPY, QQQ, TLT, UUP, UDN, BIL, SH, PSQ, XLU, GLD, RSP, and VIX
3. Scrape today's NYSE Advance/Decline ratio from Barchart.com
4. Compute all technical indicators (SMAs, Bollinger Bands, relative strength)
5. Compute VIX indicators (regime, trend, spike detection)
6. Compute NYSE volume ratios (panic/FOMO ratios)
7. Run stage analysis for all assets
8. Backfill historical NYSE breadth data using RSP vs SPY as a proxy

You should see output like:
```
ASSET REVESTING SIGNAL ENGINE — INITIALIZATION
[1/4] Initializing database...
[2/4] Fetching historical data...
  SPY: 1798 rows
  QQQ: 1798 rows
  ...
  NYSE A/D Ratio: 1.900
  RSP breadth proxy: 307 rows
[3/4] Computing indicators...
[4/4] Computing stage history...
INITIALIZATION COMPLETE
```

---

## Daily Usage

The system is designed for end-of-day use. After the market closes (4:00 PM ET / 1:00 PM PT), either:

**Option A: Read your email** (recommended)
If you've set up email reports and automatic scheduling, a report arrives in your inbox at your configured time. It tells you exactly what to do — HOLD, BUY, SELL, or DO NOTHING. Follow the instructions the next morning, waiting 15–30 minutes after market open before placing orders.

**Option B: Open the dashboard**
Double-click `Start_Dashboard.command` on your Desktop (if you created it), or:

```bash
cd ~/AssetRevesting
python -m asset_revesting.run dashboard
```

The dashboard auto-refreshes data on startup. Read the Market Narrative section at the bottom for a plain-English summary.

**Option C: Use the CLI**
```bash
cd ~/AssetRevesting
python -m asset_revesting.run update    # Refresh data
python -m asset_revesting.run signal    # View current signal
python -m asset_revesting.run stages    # View stage analysis
```

### When to Act

- **After market close:** Review your signal/email
- **Next morning:** Wait 15–30 minutes after open, then place any orders
- **Order types:** Use market orders or limit orders near current price. Use **stop orders** (not stop-limit) for stop losses — they guarantee exit even on gaps
- **During the day:** Don't watch. The system uses end-of-day data only

---

## Email Reports

The system sends a daily HTML email with:

- **Clear action items** — the first item is always HOLD, BUY, SELL, or DO NOTHING
- **Market narrative** — plain-English summary of all conditions
- **Position details** — entry price, P&L, stop, target
- **Current signal** — which asset, how many pillars aligned
- **Market conditions** — VIX, NYSE breadth, stage analysis
- **Indicator table** — SMAs, slopes, relative strength

### Setup

**Step 1: Create a Gmail App Password**

You need a Gmail account with 2-Step Verification enabled:

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Click **Security** → **2-Step Verification** (enable it if not already)
3. At the bottom, click **App passwords**
4. Select **Mail** and your device
5. Google generates a 16-character password — copy it

**Step 2: Configure in the Dashboard**

1. Launch the dashboard
2. Click **✉ Email Settings** at the bottom
3. Fill in:
   - **Send reports to:** your email address (where you want to receive reports)
   - **Gmail address (sender):** the Gmail address you created the app password for
   - **App Password:** the 16-character password from step 1
   - **Reply-To:** (optional) if you want replies to go to a different address
4. Click **Save Settings**
5. Click **Send Test Email** to verify

**Alternative: Configure via CLI**
```bash
cd ~/AssetRevesting
python -m asset_revesting.run configure-email
```

### Manual Report

Send a report anytime:
```bash
cd ~/AssetRevesting
python -m asset_revesting.run report
```

This refreshes all data, generates the report, and sends the email.

---

## Automatic Scheduling

The system can run automatically every day — it survives reboots and requires no terminal window.

### Setup via Dashboard (Recommended)

1. Open the dashboard → **✉ Email Settings**
2. Configure your email (see above)
3. Set the **Schedule** section:
   - **Hour/Minute:** When to send (your local time). Default: 17:00 (5 PM)
   - **Days:** Click to toggle which days. Default: Sun–Fri (skips Saturday since no market data)
4. Click **Enable Automatic Reports**

You'll see "✓ Scheduler active" with your schedule confirmed.

### Setup via CLI

```bash
cd ~/AssetRevesting
python -m asset_revesting.run schedule-install
```

### How It Works (macOS)

The scheduler creates a macOS **LaunchAgent** — a plist file at:
```
~/Library/LaunchAgents/com.assetrevesting.dailyreport.plist
```

This is macOS's native job scheduler. It calls Python directly (no shell scripts or cron involved). It:
- Starts automatically at login
- Runs at your configured time
- Survives reboots, sleep/wake cycles, and code updates
- Logs output to `~/Library/Logs/AssetRevesting/report.log`

### Managing the Scheduler

```bash
cd ~/AssetRevesting

# Check status
python -m asset_revesting.run schedule-status

# Update after changing schedule settings
python -m asset_revesting.run schedule-install

# Remove (stop automatic reports)
python -m asset_revesting.run schedule-remove
```

**After extracting a code update**, reinstall the scheduler to pick up any path changes:
```bash
cd ~/AssetRevesting
python -m asset_revesting.run schedule-install
```

### Viewing Logs

```bash
# Main report output
cat ~/Library/Logs/AssetRevesting/report.log

# Errors (if report fails silently)
cat ~/Library/Logs/AssetRevesting/report_err.log
```

---

## Dashboard Guide

The dashboard is a single-page web app at `http://localhost:8000`.

### Sections (top to bottom)

**Header:** Portfolio state (IN CASH / IN POSITION), data date, refresh button.

**Action Banner:** The main signal — either "No Action — Stay in Cash," an entry signal with details, or position management info with P&L.

**4 Pillars:** When an entry signal appears, you'll see which of the 4 pillars are aligned (Stage, Trend, Volatility, Volume) with ✓/✗ indicators.

**Stage Analysis:** Current Weinstein stage (1–4) for each of the 5 tracked assets. Stage 2 = advancing (green), Stage 4 = declining (red).

**Market Conditions:**
- VIX level, regime (low/normal/elevated/high/extreme), and trend
- NYSE A/D ratio from Barchart with panic/FOMO ratios
- Intermarket warnings (defensive rotation, divergences)

**Market Narrative:** Plain-English summary connecting all the data into a coherent story with a bold "bottom line" takeaway.

**Trade History:** Your logged trades with P&L.

**Indicators:** Technical data table (close price, SMA50, SMA150, slope, relative strength) for all assets.

### Position Management

**Entering a trade:** When a signal appears, click "Log Entry" and fill in the price. The system calculates your stop loss, first target, and position size.

**Exiting a trade:** Click "Log Exit." Choose partial (sells 25%, moves stop to breakeven) or full exit.

**Capital:** Click "Set Portfolio Capital" to adjust your starting capital.

---

## CLI Reference

All commands must be run from the project directory:

```bash
cd ~/AssetRevesting
```

| Command | Description |
|---------|-------------|
| `python -m asset_revesting.run init --start 2019-01-01` | First-time setup with full history |
| `python -m asset_revesting.run update` | Fetch latest data and recompute |
| `python -m asset_revesting.run dashboard` | Launch web dashboard on port 8000 |
| `python -m asset_revesting.run signal` | Print current entry/exit signal |
| `python -m asset_revesting.run stages` | Print stage analysis for all assets |
| `python -m asset_revesting.run status` | Print data summary and latest indicators |
| `python -m asset_revesting.run verify` | Spot-check indicator calculations |
| `python -m asset_revesting.run backtest` | Run full backtest over available data |
| `python -m asset_revesting.run backtest 2020-01-01 2023-12-31 -v` | Backtest a specific period (verbose) |
| `python -m asset_revesting.run report` | Refresh data + generate + email report |
| `python -m asset_revesting.run test-email` | Send a test email |
| `python -m asset_revesting.run configure-email` | Interactive email setup |
| `python -m asset_revesting.run schedule-install` | Install automatic daily scheduler |
| `python -m asset_revesting.run schedule-status` | Check if scheduler is running |
| `python -m asset_revesting.run schedule-remove` | Remove automatic scheduler |

---

## How the System Works

### The Strategy

Asset Revesting rotates between five asset classes based on which one has the best risk-adjusted setup:

**4-Tier Hierarchy** (checked in order):
1. **US Equities** — SPY (S&P 500) or QQQ (Nasdaq 100)
2. **Treasury Bonds** — TLT (20+ Year Treasury)
3. **US Dollar** — UUP (Dollar Bull) or UDN (Dollar Bear)
4. **Cash** — BIL (T-Bills) — when nothing else qualifies

The system picks the highest-tier asset that passes the entry criteria. If equities qualify, it buys equities. If only bonds qualify, it buys bonds. If nothing qualifies, it stays in cash.

### The 4 Pillars

Every potential entry is scored against 4 pillars. Need 3+ to enter:

1. **Stage Analysis** — Is the asset in Stage 2 (advancing) or Stage 4 (declining)? Based on Weinstein's method using 150-day moving average position and slope.

2. **Trend** — Are the moving averages aligned? Price > 5-SMA > 20-SMA > 50-SMA, with 150-SMA slope confirming direction.

3. **Volatility** — Is the VIX regime favorable? Is the Bollinger Band position (%B) confirming the move?

4. **Volume** — Is NYSE breadth supporting the move? No FOMO euphoria blocking entries, no extreme panic (unless used as a contrarian buy signal)?

### Exit Rules

- **Initial stop:** 5% below entry (4% for inverse trades)
- **First target:** 2% profit → sell 25%, move stop to breakeven
- **Trailing stop:** 3% trailing on the remaining position
- **VIX emergency:** VIX > 40 and rising → exit everything immediately
- **Stage change:** Asset drops from Stage 2 to Stage 4 → exit

### Data Sources

| Data | Source | Frequency |
|------|--------|-----------|
| ETF prices | Yahoo Finance (yfinance) | Daily close |
| VIX | Yahoo Finance (^VIX) | Daily close |
| NYSE A/D ratio | Barchart.com ($ADRN) | Daily (web scrape) |
| Historical breadth | RSP vs SPY proxy | Computed from prices |

---

## Project Structure

```
~/AssetRevesting/
├── asset_revesting/
│   ├── __init__.py
│   ├── __main__.py           # Entry point
│   ├── app.py                # FastAPI server + API endpoints
│   ├── config.py             # All tunable parameters
│   ├── run.py                # CLI command dispatcher
│   │
│   ├── core/
│   │   ├── backtester.py     # Historical backtest engine
│   │   ├── email_report.py   # Daily email report generation + sending
│   │   ├── indicators.py     # SMA, Bollinger, VIX, volume calculations
│   │   ├── portfolio.py      # Position tracking, dashboard data
│   │   ├── scheduler.py      # macOS LaunchAgent installer
│   │   ├── signals.py        # 4-pillar scoring + asset rotation
│   │   └── stage_analysis.py # Weinstein stage classification
│   │
│   ├── data/
│   │   ├── database.py       # SQLite schema + connections
│   │   └── ingestion.py      # yfinance + Barchart data fetching
│   │
│   ├── static/
│   │   └── index.html        # Dashboard (React SPA)
│   │
│   └── tests/
│       └── test_indicators.py
│
├── asset_revesting.db         # SQLite database (created on init)
└── README.md
```

---

## Configuration

All parameters live in `asset_revesting/config.py`. Key settings:

### Risk Management
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_STOP_PCT` | 5% | Initial stop loss distance |
| `FIRST_TARGET_PCT` | 2% | First profit target |
| `TRAILING_STOP_PCT` | 3% | Trailing stop after partial exit |
| `PARTIAL_EXIT_PCT_STAGE2` | 25% | Percentage to sell at first target |

### VIX Thresholds
| Parameter | Default | Description |
|-----------|---------|-------------|
| `VIX_LOW` | 15 | Below this = complacent |
| `VIX_NORMAL` | 20 | Normal range ceiling |
| `VIX_ELEVATED` | 30 | Caution zone |
| `VIX_EMERGENCY_LEVEL` | 40 | Exit everything |

### Entry Criteria
| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENTRY_STRONG_THRESHOLD` | 4 | Pillars for high-confidence entry |
| `ENTRY_MODERATE_THRESHOLD` | 3 | Minimum pillars for any entry |
| `STAGE_CONFIRMATION_DAYS` | 3 | Days to confirm stage transition |

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'asset_revesting'"

You need to be in the project directory:
```bash
cd ~/AssetRevesting
python -m asset_revesting.run dashboard
```

### "No module named 'yfinance'" (or any dependency)

Install the missing package:
```bash
pip install yfinance pandas beautifulsoup4 requests fastapi uvicorn
```

### Dashboard shows stale data

Click the **Refresh Data** button in the top-right corner of the dashboard. The dashboard reads from the local database — it only fetches new market data when you refresh or when the scheduled report runs.

### NYSE A/D ratio shows "—"

Barchart.com may have changed their page format or temporarily blocked the scrape. The system falls back to the RSP vs SPY proxy for historical data. Today's ratio will be missing until the next successful scrape. This does not affect the system's operation — the volume pillar still works with the proxy data.

### Email not sending

1. Verify your Gmail App Password is correct (16 characters, no spaces)
2. Ensure 2-Step Verification is enabled on your Gmail account
3. Check that "Less secure app access" is NOT what you're using — you need an App Password
4. Test from CLI: `python -m asset_revesting.run test-email`
5. If using a non-Gmail SMTP server, update the server and port in Email Settings

### Scheduler not running after reboot

Check status:
```bash
cd ~/AssetRevesting
python -m asset_revesting.run schedule-status
```

If it shows "not installed," reinstall:
```bash
python -m asset_revesting.run schedule-install
```

If installed but not active, macOS may need permission. Check System Settings → General → Login Items & Extensions and ensure the LaunchAgent isn't being blocked.

### Database corrupted or need to start fresh

Delete the database and re-initialize:
```bash
cd ~/AssetRevesting
rm asset_revesting.db
python -m asset_revesting.run init --start 2019-01-01
```

Your email configuration is stored in the database, so you'll need to reconfigure email after a reset.

### Backtest results

Run a backtest to validate the system:
```bash
cd ~/AssetRevesting
python -m asset_revesting.run backtest 2020-01-01 2026-01-01 -v
```

Expected results: ~96% total return, ~13% max drawdown, ~60% win rate over 2020–2026.

---

## Desktop Shortcut (macOS)

Create a file called `Start_Dashboard.command` on your Desktop with:

```bash
#!/bin/bash
cd ~/AssetRevesting
python -m asset_revesting.run dashboard &
sleep 3
open http://localhost:8000
echo "Dashboard running. Close this window to stop."
wait
```

Then make it executable:
```bash
chmod +x ~/Desktop/Start_Dashboard.command
```

Double-click to launch the dashboard and open your browser.

---

## License

This is a personal trading tool. The strategy concepts are based on Chris Vermeulen's publicly available Asset Revesting methodology. This implementation is original code. Use at your own risk — this is not financial advice.
