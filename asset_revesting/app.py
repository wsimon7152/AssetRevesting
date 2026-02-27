# asset_revesting/app.py
"""
FastAPI server for the Asset Revesting dashboard.
Full trading cockpit: signals, position management, data refresh.
"""

import json
import traceback
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os

from asset_revesting.data.database import init_db, get_connection
from asset_revesting.data.ingestion import fetch_all
from asset_revesting.core.indicators import compute_all_indicators, get_latest_vix
from asset_revesting.core.stage_analysis import get_all_stages, compute_stage_history
from asset_revesting.core.portfolio import (
    get_dashboard_data, get_trade_history,
    get_portfolio_state, save_portfolio_state, log_trade,
    STATE_CASH, STATE_POSITIONED, STATE_PARTIAL,
)
from asset_revesting.core.signals import (
    asset_rotation, calc_trade_params, _get_close,
    LONG, LONG_INVERSE,
)

app = FastAPI(title="Asset Revesting Signal Engine")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

init_db()


# =============================================================================
# MODELS
# =============================================================================

class EnterTradeRequest(BaseModel):
    symbol: str
    direction: str = "LONG"
    entry_price: float
    entry_date: Optional[str] = None
    shares: Optional[float] = None
    capital: Optional[float] = None  # if provided, calculates shares

class ExitTradeRequest(BaseModel):
    exit_price: float
    exit_date: Optional[str] = None
    exit_reason: str = "MANUAL"
    partial: bool = False  # True = partial exit

class UpdateCapitalRequest(BaseModel):
    capital: float


# =============================================================================
# DASHBOARD
# =============================================================================

@app.get("/api/dashboard")
def dashboard():
    return get_dashboard_data()


# =============================================================================
# DATA REFRESH
# =============================================================================

@app.post("/api/refresh")
def refresh_data():
    """Fetch latest market data, recompute indicators and stages."""
    try:
        start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        fetch_all(start_date=start)
        compute_all_indicators()
        compute_stage_history()
        # Get updated data date
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(date) as d FROM prices WHERE symbol='SPY'").fetchone()
            data_date = row["d"] if row else "unknown"
        return {"status": "ok", "data_date": data_date}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# POSITION MANAGEMENT
# =============================================================================

@app.post("/api/position/enter")
def enter_position(req: EnterTradeRequest):
    """Log a new position entry."""
    state = get_portfolio_state()

    if state["position"] is not None:
        raise HTTPException(400, "Already in a position. Exit first.")

    entry_date = req.entry_date or datetime.now().strftime("%Y-%m-%d")

    # Calculate shares from capital if needed
    if req.shares:
        shares = req.shares
        capital_used = shares * req.entry_price
    elif req.capital:
        shares = req.capital / req.entry_price
        capital_used = req.capital
    else:
        capital_used = state["cash"]
        shares = capital_used / req.entry_price

    # Get trade parameters from the signal engine
    from asset_revesting.core.stage_analysis import determine_stage
    stage_info = determine_stage(req.symbol)
    params = calc_trade_params(req.entry_price, req.direction, stage_info["stage"])

    new_state = {
        "date": entry_date,
        "state": STATE_POSITIONED,
        "cash": state["cash"] - capital_used,
        "position": {
            "symbol": req.symbol,
            "direction": req.direction,
            "entry_date": entry_date,
            "entry_price": req.entry_price,
            "shares": shares,
            "stop": params["initial_stop"],
            "first_target": params["first_target"],
            "trailing_pct": params["trailing_pct"],
            "partial_exit_pct": params["partial_exit_pct"],
            "partial_exited": False,
            "stop_order_date": entry_date,  # assume stop placed same day as entry
        },
        "vix_cooldown": state.get("vix_cooldown", False),
    }

    save_portfolio_state(new_state)

    return {
        "status": "ok",
        "position": new_state["position"],
        "trade_plan": {
            "stop_loss": round(params["initial_stop"], 2),
            "first_target": round(params["first_target"], 2),
            "partial_exit_pct": round(params["partial_exit_pct"] * 100, 0),
            "trailing_stop_pct": round(params["trailing_pct"] * 100, 2),
            "trade_type": params["trade_type"],
            "instructions": [
                f"Set stop loss at ${params['initial_stop']:.2f}",
                f"Set alert at ${params['first_target']:.2f} (first target)",
                f"When target hit: sell {params['partial_exit_pct']*100:.0f}% of position",
                f"Move stop to breakeven (${req.entry_price:.2f})",
                f"Trailing stop at {params['trailing_pct']*100:.1f}% for remainder",
            ],
        },
    }


@app.post("/api/position/exit")
def exit_position(req: ExitTradeRequest):
    """Log a position exit (full or partial)."""
    state = get_portfolio_state()

    if state["position"] is None:
        raise HTTPException(400, "No open position to exit.")

    pos = state["position"]
    exit_date = req.exit_date or datetime.now().strftime("%Y-%m-%d")

    if req.partial:
        # Partial exit
        sell_pct = pos.get("partial_exit_pct", 0.25)
        shares_sold = pos["shares"] * sell_pct
        proceeds = shares_sold * req.exit_price

        new_state = {
            "date": exit_date,
            "state": STATE_PARTIAL,
            "cash": state["cash"] + proceeds,
            "position": {
                **pos,
                "shares": pos["shares"] - shares_sold,
                "stop": pos["entry_price"],  # move to breakeven
                "partial_exited": True,
            },
            "vix_cooldown": state.get("vix_cooldown", False),
        }
        save_portfolio_state(new_state)

        return {
            "status": "ok",
            "action": "PARTIAL_EXIT",
            "shares_sold": round(shares_sold, 4),
            "proceeds": round(proceeds, 2),
            "remaining_shares": round(pos["shares"] - shares_sold, 4),
            "new_stop": pos["entry_price"],
            "message": f"Sold {sell_pct*100:.0f}% ({shares_sold:.2f} shares) at ${req.exit_price:.2f}. Stop moved to breakeven ${pos['entry_price']:.2f}.",
        }

    else:
        # Full exit
        proceeds = pos["shares"] * req.exit_price
        pnl_pct = (req.exit_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_dollar = proceeds - (pos["shares"] * pos["entry_price"])

        # Log the trade
        log_trade({
            "symbol": pos["symbol"],
            "direction": pos["direction"],
            "entry_date": pos["entry_date"],
            "entry_price": pos["entry_price"],
            "exit_date": exit_date,
            "exit_price": req.exit_price,
            "exit_reason": req.exit_reason,
            "shares": pos["shares"],
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
        })

        new_state = {
            "date": exit_date,
            "state": STATE_CASH,
            "cash": state["cash"] + proceeds,
            "position": None,
            "vix_cooldown": req.exit_reason == "VIX_EMERGENCY",
        }
        save_portfolio_state(new_state)

        return {
            "status": "ok",
            "action": "FULL_EXIT",
            "proceeds": round(proceeds, 2),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "total_cash": round(state["cash"] + proceeds, 2),
            "message": f"Exited {pos['symbol']} at ${req.exit_price:.2f}. P&L: {'+'if pnl_pct>=0 else ''}{pnl_pct:.1f}% (${pnl_dollar:+,.2f})",
        }


@app.post("/api/position/update-stop")
def update_stop(new_stop: float):
    """Manually update the stop price. Also resets the 60-day broker order clock."""
    state = get_portfolio_state()
    if state["position"] is None:
        raise HTTPException(400, "No open position.")

    today = datetime.now().strftime("%Y-%m-%d")
    state["position"]["stop"] = new_stop
    state["position"]["stop_order_date"] = today  # placing a new stop resets the clock
    save_portfolio_state(state)
    return {"status": "ok", "new_stop": new_stop, "stop_order_date": today}


@app.post("/api/position/renew-stop")
def renew_stop_order():
    """Record that the broker stop order was renewed (same price, new 60-day clock)."""
    state = get_portfolio_state()
    if state["position"] is None:
        raise HTTPException(400, "No open position.")

    today = datetime.now().strftime("%Y-%m-%d")
    state["position"]["stop_order_date"] = today
    save_portfolio_state(state)
    return {
        "status": "ok",
        "stop_order_date": today,
        "message": f"Stop order renewal recorded. Next expiry in 60 days ({today}).",
    }


@app.post("/api/capital")
def set_capital(req: UpdateCapitalRequest):
    """Set portfolio starting capital."""
    state = get_portfolio_state()
    state["cash"] = req.capital
    save_portfolio_state(state)
    return {"status": "ok", "cash": req.capital}


# =============================================================================
# EMAIL CONFIGURATION
# =============================================================================

class EmailConfigRequest(BaseModel):
    recipient_email: str
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    reply_to_email: str = ""
    report_hour: int = 17
    report_minute: int = 0
    report_days: str = "0,1,2,3,4,5"
    enabled: bool = True

@app.get("/api/email-config")
def get_email_settings():
    from asset_revesting.core.email_report import get_email_config
    config = get_email_config()
    if config:
        # Mask the password for display
        masked = dict(config)
        if masked.get("smtp_password"):
            masked["smtp_password"] = "••••••••"
        return masked
    return {"configured": False}

@app.post("/api/email-config")
def save_email_settings(req: EmailConfigRequest):
    from asset_revesting.core.email_report import save_email_config, get_email_config
    # If password is masked, keep the existing one
    if req.smtp_password == "••••••••":
        existing = get_email_config()
        if existing:
            req.smtp_password = existing["smtp_password"]
    save_email_config(req.dict())
    return {"status": "ok"}

@app.post("/api/test-email")
def test_email():
    from asset_revesting.core.email_report import generate_report, send_email, get_email_config
    config = get_email_config()
    if not config or not config.get("recipient_email"):
        raise HTTPException(400, "Email not configured. Save email settings first.")
    report = generate_report()
    success = send_email(report)
    if success:
        return {"status": "ok", "message": f"Test email sent to {config['recipient_email']}"}
    else:
        raise HTTPException(500, "Failed to send email. Check SMTP credentials.")

@app.post("/api/send-report")
def send_report():
    from asset_revesting.core.email_report import generate_report, send_email
    report = generate_report()
    success = send_email(report)
    if success:
        return {"status": "ok"}
    else:
        raise HTTPException(500, "Failed to send report.")

@app.post("/api/scheduler/install")
def install_scheduler():
    from asset_revesting.core.scheduler import install_schedule
    result = install_schedule()
    return result

@app.post("/api/scheduler/uninstall")
def uninstall_scheduler():
    from asset_revesting.core.scheduler import uninstall_schedule
    result = uninstall_schedule()
    return result

@app.get("/api/scheduler/status")
def scheduler_status():
    from asset_revesting.core.scheduler import get_schedule_status
    return get_schedule_status()


# =============================================================================
# READ ENDPOINTS
# =============================================================================

@app.get("/api/stages")
def stages():
    return get_all_stages()

@app.get("/api/signal")
def signal():
    return asset_rotation()

@app.get("/api/trades")
def trades(limit: int = 50):
    return get_trade_history(limit)

@app.get("/api/vix")
def vix():
    return get_latest_vix()

@app.get("/api/performance")
def performance():
    """Calculate performance stats from trade history."""
    trades = get_trade_history(200)
    if not trades:
        return {"trades": 0}

    closed = [t for t in trades if t.get("exit_date")]
    if not closed:
        return {"trades": 0}

    pnls = [t["pnl_pct"] for t in closed if t.get("pnl_pct") is not None]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    return {
        "total_trades": len(closed),
        "win_rate": round(len(winners) / len(closed) * 100, 1) if closed else 0,
        "avg_win": round(sum(winners) / len(winners), 2) if winners else 0,
        "avg_loss": round(sum(losers) / len(losers), 2) if losers else 0,
        "total_pnl_dollar": round(sum(t.get("pnl_dollar", 0) or 0 for t in closed), 2),
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
    }


# =============================================================================
# STATIC FILES
# =============================================================================

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def serve_dashboard():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
