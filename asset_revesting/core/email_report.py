# asset_revesting/core/email_report.py
"""
Daily Email Report for Asset Revesting Signal Engine.

Generates a thorough end-of-day report and emails it.
Designed so you never need to open the dashboard — just read
your morning email and follow the instructions.

Usage:
    python -m asset_revesting.run report          # Generate + send daily report
    python -m asset_revesting.run test-email      # Send a test email
"""

import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date

from asset_revesting.data.database import get_connection, init_db


# =============================================================================
# EMAIL CONFIGURATION (stored in DB)
# =============================================================================

def get_email_config(db_path=None):
    """Read email config from database."""
    _ensure_email_table(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM email_config WHERE id = 1").fetchone()
    if row:
        return dict(row)
    return None


def save_email_config(config, db_path=None):
    """Save email config to database."""
    _ensure_email_table(db_path)
    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO email_config
            (id, recipient_email, smtp_server, smtp_port, smtp_user, smtp_password,
             reply_to_email, report_hour, report_minute, report_days, enabled)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            config.get("recipient_email", ""),
            config.get("smtp_server", "smtp.gmail.com"),
            config.get("smtp_port", 587),
            config.get("smtp_user", ""),
            config.get("smtp_password", ""),
            config.get("reply_to_email", ""),
            config.get("report_hour", 17),
            config.get("report_minute", 0),
            config.get("report_days", "0,1,2,3,4,5"),
            1 if config.get("enabled", True) else 0,
        ))


def _ensure_email_table(db_path=None):
    """Create email_config table if it doesn't exist."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_config (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                recipient_email TEXT NOT NULL DEFAULT '',
                smtp_server     TEXT NOT NULL DEFAULT 'smtp.gmail.com',
                smtp_port       INTEGER NOT NULL DEFAULT 587,
                smtp_user       TEXT NOT NULL DEFAULT '',
                smtp_password   TEXT NOT NULL DEFAULT '',
                reply_to_email  TEXT NOT NULL DEFAULT '',
                report_hour     INTEGER NOT NULL DEFAULT 17,
                report_minute   INTEGER NOT NULL DEFAULT 0,
                report_days     TEXT NOT NULL DEFAULT '0,1,2,3,4,5',
                enabled         INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Migrate: add columns if missing (existing DBs)
        for col, defn in [
            ("reply_to_email", "TEXT NOT NULL DEFAULT ''"),
            ("report_hour", "INTEGER NOT NULL DEFAULT 17"),
            ("report_minute", "INTEGER NOT NULL DEFAULT 0"),
            ("report_days", "TEXT NOT NULL DEFAULT '0,1,2,3,4,5'"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM email_config LIMIT 1")
            except Exception:
                try:
                    conn.execute(f"ALTER TABLE email_config ADD COLUMN {col} {defn}")
                except Exception:
                    pass


# =============================================================================
# REPORT GENERATION
# =============================================================================

def generate_report(db_path=None):
    """
    Generate the full daily report as a dict.
    Contains everything needed for the email body.
    """
    from asset_revesting.core.portfolio import (
        get_dashboard_data, get_portfolio_state, get_trade_history,
    )

    data = get_dashboard_data(db_path=db_path)
    state = get_portfolio_state(db_path)

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_date": data["data_date"],
        "portfolio": data["portfolio"],
        "position": data.get("position"),
        "signal": data["signal"],
        "stages": data["stages"],
        "vix": data["vix"],
        "volume": data["volume"],
        "warnings": data.get("warnings", []),
        "indicators": data.get("indicators", {}),
        "trades": get_trade_history(10, db_path),
    }

    # Enrich position with ATR-based stop recommendation
    if report["position"]:
        report["position"] = _enrich_position_with_atr(report["position"], db_path)

    # Build action items
    report["actions"] = _build_action_items(report)

    # Build market narrative
    report["narrative"] = _build_narrative(report)

    return report


def _build_narrative(report):
    """
    Build a plain-English market narrative from all the data.
    Returns a list of paragraph strings.
    """
    stg = report.get("stages", {})
    vix = report.get("vix", {})
    vol = report.get("volume", {})
    sig = report.get("signal", {})
    ind = report.get("indicators", {})
    warnings = report.get("warnings", [])
    position = report.get("position")

    syms = ["SPY", "QQQ", "TLT", "UUP", "UDN"]
    s2 = [s for s in syms if "2" in (stg.get(s, {}).get("stage", ""))]
    s4 = [s for s in syms if "4" in (stg.get(s, {}).get("stage", ""))]
    all_s2 = len(s2) == 5
    equities_s2 = "2" in stg.get("SPY", {}).get("stage", "") and "2" in stg.get("QQQ", {}).get("stage", "")

    vix_close = vix.get("close") or 0
    vix_trend = vix.get("trend", "")
    ad_ratio = vol.get("nyse_ad_ratio")
    fomo = vol.get("fomo_ratio")
    panic = vol.get("panic_ratio")
    vol_fav = vol.get("favorable", False)

    spy_ind = ind.get("SPY", {})
    qqq_ind = ind.get("QQQ", {})
    spy_slope = spy_ind.get("sma_150_slope") or 0
    qqq_slope = qqq_ind.get("sma_150_slope") or 0
    spy_rs = spy_ind.get("relative_strength") or 0
    qqq_rs = qqq_ind.get("relative_strength") or 0

    warn_strs = [w if isinstance(w, str) else w.get("name", "") for w in warnings]
    has_def_rotation = any("DEFENSIVE" in w for w in warn_strs)
    has_divergence = any("DIVERGENCE" in w for w in warn_strs)

    score = sig.get("score") or 0
    sig_asset = sig.get("asset", "")

    parts = []

    # 1. Stage overview
    if all_s2:
        parts.append("All five tracked assets are in Stage 2 (advancing), the strongest possible configuration. Equities, bonds, and dollar pairs are all in confirmed uptrends.")
    elif equities_s2 and len(s2) >= 3:
        others = [{"TLT": "bonds", "UUP": "US dollar (bull)", "UDN": "US dollar (bear)"}.get(s, s) for s in s2 if s not in ("SPY", "QQQ")]
        extra = f" {', '.join(others)} also advancing." if others else ""
        decline = f" {', '.join(s4)} in Stage 4 (declining) — worth monitoring." if s4 else ""
        parts.append(f"SPY and QQQ are both in Stage 2 (advancing), confirming the equity uptrend.{extra}{decline}")
    elif len(s4) >= 3:
        parts.append(f"{len(s4)} of 5 assets are in Stage 4 (declining). This is a broad deterioration — the system favors cash or inverse positions.")
    else:
        parts.append(f"Mixed picture: {len(s2)} asset{'s' if len(s2) != 1 else ''} advancing, {len(s4)} declining. The system is selective about entries.")

    # 2. VIX
    if vix_close < 15:
        parts.append(f"VIX at {vix_close:.1f} signals complacency — very low fear. Good for trends, but extreme calm can precede sudden spikes.")
    elif vix_close < 20:
        trend_note = " and trending higher" if vix_trend == "RISING" else " and trending lower" if vix_trend == "FALLING" else ""
        parts.append(f"VIX at {vix_close:.1f} is in the normal range{trend_note}. Standard conditions — no fear-driven adjustments needed.")
    elif vix_close < 25:
        parts.append(f"VIX at {vix_close:.1f} is at the upper end of normal{' and still rising' if vix_trend == 'RISING' else ''}. Not yet elevated, but the system is paying attention.")
    elif vix_close < 35:
        parts.append(f"VIX at {vix_close:.1f} is elevated — fear above average. The system becomes more cautious with entries and tightens risk management.")
    else:
        parts.append(f"VIX at {vix_close:.1f} is in crisis territory. {'Above 40 triggers emergency exit of all positions.' if vix_close >= 40 else 'Approaching the emergency threshold.'}")

    # 3. Breadth
    if ad_ratio is not None:
        if fomo and fomo >= 3:
            parts.append(f"NYSE breadth is in euphoria (A/D ratio {ad_ratio:.3f}). Extreme one-sided buying often occurs near tops. The volume pillar is blocking new entries.")
        elif panic and panic >= 3:
            parts.append(f"NYSE breadth shows panic selling (A/D ratio {ad_ratio:.3f}). Extreme panic often marks bottoms — the volume pillar treats this as a contrarian buy signal.")
        elif ad_ratio > 1.3:
            parts.append(f"NYSE breadth is healthy at {ad_ratio:.3f} — advancing volume comfortably leads declining. Broad participation confirms the rally isn't driven by just a handful of mega-caps.")
        elif ad_ratio > 0.8:
            parts.append(f"NYSE breadth is neutral at {ad_ratio:.3f} — advancing and declining volume roughly balanced.")
        else:
            parts.append(f"NYSE breadth is weak at {ad_ratio:.3f} — declining volume exceeds advancing. Underlying participation is deteriorating.")

    # 4. Trend quality
    if spy_slope > 0.5 and qqq_slope > 0.5:
        stronger = "SPY showing stronger relative momentum" if spy_rs > qqq_rs else "QQQ leading in relative strength"
        parts.append(f"Both SPY and QQQ have positive 150-day slopes (+{spy_slope:.1f}% and +{qqq_slope:.1f}%), confirming the medium-term uptrend. {stronger}.")
    elif spy_slope > 0 and qqq_slope > 0:
        parts.append(f"SPY and QQQ slopes are positive but modest (+{spy_slope:.1f}% and +{qqq_slope:.1f}%). The trend is up but momentum is not strong.")
    elif spy_slope < 0 or qqq_slope < 0:
        neg = [s for s, sl in [("SPY", spy_slope), ("QQQ", qqq_slope)] if sl < 0]
        parts.append(f"{' and '.join(neg)} {'have' if len(neg) > 1 else 'has a'} negative 150-day slope — the medium-term trend is bending lower.")

    # 5. Warnings
    if has_def_rotation:
        parts.append("Utilities are outperforming the S&P 500, a classic defensive rotation signal. Institutional investors often shift to utilities before broader weakness. This doesn't mean sell immediately, but it's a yellow flag that has historically preceded corrections.")
    if has_divergence:
        parts.append("Stocks and bonds are both falling simultaneously — an unusual divergence pointing to dollar strength or liquidity tightening. This cross-asset stress warrants extra caution.")

    # 6. Bottom line
    if position:
        entry = position.get("entry_price", 0)
        stop = position.get("stop", 0)
        pnl = position.get("unrealized_pnl", 0) or 0
        sym = position.get("symbol", "?")
        working = "The position is working" if pnl >= 0 else "The position is underwater but"
        if equities_s2 and vix_close < 25 and vol_fav:
            outlook = "conditions remain supportive. Hold and let the trade play out."
        elif has_def_rotation or vix_close >= 25:
            outlook = "keep a close eye on your stop — conditions are showing some stress."
        else:
            outlook = "continue to hold."
        parts.append(f"Bottom line: You're in {sym} at ${entry:.2f} with a stop at ${stop:.2f}. {working} — {outlook}")
    elif score >= 3 and sig_asset != "BIL":
        conf = "This is a high-confidence setup." if score == 4 else "The signal is valid but not full strength — size accordingly."
        parts.append(f"Bottom line: The system sees an entry opportunity in {sig_asset} with {score}/4 pillars aligned. {conf}")
    else:
        if all_s2 and vix_close < 25:
            parts.append("Bottom line: No trade right now. Conditions are favorable but full confluence hasn't lined up yet. Patience.")
        else:
            parts.append("Bottom line: No trade right now. The system is waiting for better alignment across all four pillars before committing capital.")

    return parts


def _enrich_position_with_atr(position, db_path=None):
    """
    Add ATR-based stop recommendation to the position dict.
    Compares current stop_price to what the ATR stop would be,
    flagging adjustments when they differ by more than $1.
    """
    from asset_revesting.config import (
        USE_ATR_STOPS, ATR_MULTIPLIER, ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT
    )
    from asset_revesting.core.backtester import _get_atr

    if not USE_ATR_STOPS:
        return position

    entry_price = position.get("entry_price")
    entry_date  = position.get("entry_date")
    current_stop = position.get("stop")
    underlying = position.get("underlying") or position.get("symbol")

    if not all([entry_price, entry_date, current_stop, underlying]):
        return position

    atr = _get_atr(underlying, entry_date, db_path)
    if not atr:
        return position

    raw_dist = ATR_MULTIPLIER * atr
    min_dist = entry_price * ATR_MIN_STOP_PCT
    max_dist = entry_price * ATR_MAX_STOP_PCT
    distance = max(min_dist, min(raw_dist, max_dist))
    atr_stop = round(entry_price - distance, 2)

    position = dict(position)  # don't mutate original
    position["atr_stop_recommended"] = atr_stop
    position["atr_val"] = round(atr, 4)
    position["atr_stop_diff"] = round(atr_stop - current_stop, 2)

    return position


def _build_action_items(report):
    """
    Build clear, prioritized action items based on current state.
    
    RULE: The FIRST action item is always a clear, unambiguous instruction.
    "HOLD", "BUY", "SELL", "DO NOTHING" — never just data.
    """
    actions = []
    position = report["position"]
    signal = report["signal"]
    vix = report["vix"]
    volume = report["volume"]
    warnings = report.get("warnings", [])
    portfolio = report["portfolio"]

    # ── EMERGENCY: VIX crisis ──
    if vix.get("regime") in ("extreme", "high") and vix.get("close") and vix["close"] >= 40:
        actions.append({
            "priority": "🚨 EMERGENCY",
            "action": "EXIT ALL POSITIONS — VIX CRISIS",
            "detail": f"VIX is at {vix['close']:.1f} (extreme fear). "
                      "Chris's rule: exit everything when VIX > 40. "
                      "Place a market sell order at tomorrow's open. Do not wait.",
        })
        return actions

    # ── IN A POSITION ──
    if position:
        sym = position["symbol"]
        entry = position["entry_price"]
        current = position["current_price"]
        stop = position.get("stop")
        target = position.get("target")
        pnl = position.get("unrealized_pnl", 0) or 0
        partial = position.get("partial_exited", False)
        stage = report["stages"].get(sym, {}).get("stage", "")

        # Determine the PRIMARY instruction
        if current and stop and current <= stop:
            # ── STOP HIT ──
            actions.append({
                "priority": "🚨 SELL",
                "action": f"EXIT {sym} — your stop has been hit",
                "detail": f"{sym} closed at ${current:.2f}, which is at or below your stop of ${stop:.2f}. "
                          f"Your broker's stop order should have filled — check your account to confirm. "
                          f"If it didn't fill, place a market sell at tomorrow's open.",
            })

        elif current and target and not partial and current >= target:
            # ── TARGET HIT ──
            actions.append({
                "priority": "⚡ SELL 25%",
                "action": f"PARTIAL EXIT {sym} — first target reached",
                "detail": f"{sym} hit ${current:.2f}, past your ${target:.2f} target. "
                          f"Tomorrow morning, sell 25% of your shares. "
                          f"Then move your stop from ${stop:.2f} up to breakeven at ${entry:.2f}. "
                          f"Hold the remaining 75% with a trailing stop.",
            })

        elif stage in ("STAGE_4", "STAGE_3"):
            # ── STAGE DETERIORATING ──
            actions.append({
                "priority": "⚠️ WATCH CLOSELY",
                "action": f"{sym} trend is weakening — prepare to exit",
                "detail": f"{sym} has moved to {stage}. This often precedes a decline. "
                          f"If it doesn't recover to Stage 2 within 1-2 days, exit the position. "
                          f"Your stop is at ${stop:.2f} — consider tightening it.",
            })

        else:
            # ── ALL CLEAR → HOLD ──
            pnl_desc = f"up {pnl:+.1f}%" if pnl >= 0 else f"down {pnl:.1f}%"
            stop_dist = ((current - stop) / current * 100) if current and stop else 0

            actions.append({
                "priority": "✅ HOLD",
                "action": f"Keep holding {sym} — no action needed today",
                "detail": f"{sym} is at ${current:.2f} ({pnl_desc} from your ${entry:.2f} entry). "
                          f"Stop loss at ${stop:.2f} ({stop_dist:.1f}% below current price). "
                          + (f"First target: ${target:.2f}. " if target and not partial else "")
                          + f"Stage 2 (advancing) confirmed. Everything is on track.",
            })

        # Broker stop order expiry check (Vanguard GTC orders expire after 60 days)
        days_left = position.get("stop_order_days_left")
        if days_left is not None:
            if days_left <= 0:
                actions.append({
                    "priority": "🚨 STOP ORDER EXPIRED",
                    "action": f"Your {sym} stop-loss order has EXPIRED — you are unprotected",
                    "detail": f"Vanguard GTC stop orders expire after 60 days. Your stop order at "
                              f"${stop:.2f} has expired. Log into Vanguard immediately, cancel the old "
                              f"order if it still shows, and place a new GTC stop-loss order at "
                              f"${stop:.2f}. Until you do, you have NO downside protection.",
                })
            elif days_left <= 7:
                actions.append({
                    "priority": "⚠️ RENEW STOP ORDER",
                    "action": f"Stop-loss order expires in {days_left} day{'s' if days_left != 1 else ''} — renew with Vanguard",
                    "detail": f"Your GTC stop order at ${stop:.2f} expires in {days_left} day{'s' if days_left != 1 else ''}. "
                              f"Log into Vanguard, cancel the current stop order, and place a new GTC "
                              f"stop-loss order at ${stop:.2f}. Then click 'Renew Stop Order' on the dashboard "
                              f"to reset the 60-day clock.",
                })

        # ATR stop adjustment check
        atr_stop = position.get("atr_stop_recommended")
        atr_diff = position.get("atr_stop_diff")
        if atr_stop and atr_diff is not None and abs(atr_diff) >= 1.0:
            if atr_diff > 0:
                actions.append({
                    "priority": "📐 ADJUST STOP",
                    "action": f"Move your {sym} stop UP from ${stop:.2f} → ${atr_stop:.2f}",
                    "detail": f"The ATR-based stop (3× 14-day volatility, floored at 4%) is ${atr_stop:.2f}, "
                              f"which is ${atr_diff:.2f} tighter than your current stop of ${stop:.2f}. "
                              f"Update your broker stop order to ${atr_stop:.2f} to lock in better protection.",
                })
            else:
                actions.append({
                    "priority": "📐 CONSIDER WIDENING STOP",
                    "action": f"Volatility has risen — ATR stop suggests ${atr_stop:.2f} vs your ${stop:.2f}",
                    "detail": f"Current 14-day ATR has expanded. The ATR-based stop (3× ATR, 4% floor) is now "
                              f"${atr_stop:.2f}, ${abs(atr_diff):.2f} below your current stop of ${stop:.2f}. "
                              f"If SPY is swinging normally, consider widening to ${atr_stop:.2f} to avoid "
                              f"being stopped out on a routine pullback.",
                })

        # Additional warnings for positioned state
        if vix.get("close") and vix["close"] >= 30:
            actions.append({
                "priority": "⚠️ CAUTION",
                "action": "VIX elevated — consider tightening your stop",
                "detail": f"VIX is at {vix['close']:.1f} (fear rising). "
                          "You may want to tighten your trailing stop to protect gains.",
            })

    # ── IN CASH ──
    else:
        score = signal.get("score")
        asset = signal.get("asset")
        direction = signal.get("direction")

        if score and score >= 3 and asset != "BIL":
            # ── ENTRY SIGNAL ──
            actions.append({
                "priority": "⚡ BUY",
                "action": f"Enter {asset} ({direction}) tomorrow morning",
                "detail": f"{score}/4 pillars aligned. "
                          f"Wait 15-30 minutes after market open, then place a market or limit order. "
                          f"Open the dashboard to see your position size, stop loss, and target prices. "
                          f"Pillar breakdown: {signal.get('details', '')}",
            })

        elif score and score >= 3 and asset == "BIL":
            actions.append({
                "priority": "✅ DO NOTHING",
                "action": "Stay in cash — system says safety first",
                "detail": "All signals point to cash/T-bills (BIL). "
                          "No equities, bonds, or dollar trades meet entry criteria right now. "
                          "This is the system protecting your capital — be patient.",
            })

        else:
            actions.append({
                "priority": "✅ DO NOTHING",
                "action": "Stay in cash — no entry signal",
                "detail": f"Current signal: {asset or 'none'} with only {score or 0}/4 pillars. "
                          "Need 3+ aligned pillars for a valid entry. Nothing to do today.",
            })

        if portfolio.get("vix_cooldown"):
            actions.append({
                "priority": "⏸️ COOLDOWN",
                "action": "VIX cooldown active — entries blocked",
                "detail": "Recent VIX emergency triggered a cooldown. "
                          "Wait for VIX to drop below 25 before any new positions.",
            })

    # ── WARNINGS (context, not instructions) ──
    for w in warnings:
        if isinstance(w, dict):
            name = w.get("name", "Warning")
            detail = w.get("detail", "")
        else:
            name = str(w)
            detail = ""
        actions.append({
            "priority": "ℹ️ MARKET NOTE",
            "action": name,
            "detail": detail or "Monitor this condition — no action required unless it worsens.",
        })

    # Volume warning
    vol_flags = volume.get("flags", [])
    for flag in vol_flags:
        actions.append({
            "priority": "⚠️ BREADTH",
            "action": flag,
            "detail": "NYSE breadth warning — the volume pillar may block new entries.",
        })

    return actions


# =============================================================================
# EMAIL FORMATTING
# =============================================================================

def format_email_html(report):
    """Format the report as a clean HTML email."""
    data_date = report["data_date"]
    generated = report["generated_at"]
    actions = report["actions"]
    portfolio = report["portfolio"]
    signal = report["signal"]
    vix = report["vix"]
    volume = report["volume"]
    stages = report["stages"]
    indicators = report.get("indicators", {})
    position = report.get("position")
    warnings = report.get("warnings", [])
    trades = report.get("trades", [])
    narrative = report.get("narrative", [])

    # Colors
    grn = "#00d4aa"
    red = "#ff6b6b"
    amb = "#f0a500"
    blu = "#5ba0d0"
    txd = "#7a8a9a"
    bg = "#0d1117"
    card = "#161b22"
    bdr = "#30363d"
    tx = "#e6edf3"

    # Build action items HTML
    action_html = ""
    for a in actions:
        prio = a["priority"]
        # Color based on priority
        if "EMERGENCY" in prio or "URGENT" in prio:
            pcolor = red
        elif "ACTION" in prio or "ENTRY" in prio:
            pcolor = grn
        elif "WARNING" in prio or "CAUTION" in prio or "MARKET" in prio or "BREADTH" in prio:
            pcolor = amb
        else:
            pcolor = blu

        action_html += f"""
        <tr>
            <td style="padding:12px 16px;border-bottom:1px solid {bdr};vertical-align:top;">
                <div style="color:{pcolor};font-weight:600;font-size:13px;margin-bottom:4px;">{prio}</div>
                <div style="color:{tx};font-weight:600;font-size:15px;margin-bottom:6px;">{a['action']}</div>
                <div style="color:{txd};font-size:13px;line-height:1.5;">{a['detail']}</div>
            </td>
        </tr>
        """

    # State label
    state = portfolio.get("state", "CASH")
    if state == "CASH":
        state_label = f'<span style="color:{blu};">● IN CASH</span>'
    elif state in ("POSITIONED", "PARTIAL_EXIT"):
        state_label = f'<span style="color:{grn};">● IN POSITION</span>'
    else:
        state_label = state

    # Stage rows
    stage_html = ""
    stage_names = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "TLT": "Treasury Bonds", "UUP": "US Dollar Bull", "UDN": "US Dollar Bear"}
    for sym in ["SPY", "QQQ", "TLT", "UUP", "UDN"]:
        s = stages.get(sym, {})
        stage = s.get("stage", "?")
        confirmed = s.get("confirmed", False)
        if "2" in stage:
            scolor, slabel = grn, "2 Advancing"
        elif "4" in stage:
            scolor, slabel = red, "4 Declining"
        elif "1" in stage:
            scolor, slabel = blu, "1 Basing"
        elif "3" in stage:
            scolor, slabel = amb, "3 Topping"
        else:
            scolor, slabel = txd, stage

        stage_html += f"""
        <tr>
            <td style="padding:6px 12px;color:{tx};font-size:13px;border-bottom:1px solid {bdr};">{sym}</td>
            <td style="padding:6px 12px;color:{txd};font-size:13px;border-bottom:1px solid {bdr};">{stage_names.get(sym, sym)}</td>
            <td style="padding:6px 12px;color:{scolor};font-size:13px;font-weight:600;border-bottom:1px solid {bdr};text-align:right;">{slabel}</td>
        </tr>
        """

    # Indicator rows
    ind_html = ""
    for sym in ["SPY", "QQQ", "TLT", "UUP", "UDN"]:
        ind = indicators.get(sym, {})
        close = ind.get("close")
        sma50 = ind.get("sma_50")
        sma150 = ind.get("sma_150")
        slope = ind.get("sma_150_slope")
        rs = ind.get("relative_strength")

        slope_color = grn if slope and slope > 0 else red if slope else txd
        rs_color = grn if rs and rs > 0 else red if rs else txd

        ind_html += f"""
        <tr>
            <td style="padding:4px 8px;color:{tx};font-size:12px;border-bottom:1px solid {bdr};font-weight:600;">{sym}</td>
            <td style="padding:4px 8px;color:{tx};font-size:12px;border-bottom:1px solid {bdr};">${f'{close:.2f}' if close else '—'}</td>
            <td style="padding:4px 8px;color:{txd};font-size:12px;border-bottom:1px solid {bdr};">{f'{sma50:.1f}' if sma50 else '—'}</td>
            <td style="padding:4px 8px;color:{txd};font-size:12px;border-bottom:1px solid {bdr};">{f'{sma150:.1f}' if sma150 else '—'}</td>
            <td style="padding:4px 8px;color:{slope_color};font-size:12px;border-bottom:1px solid {bdr};">{f'+{slope:.2f}%' if slope and slope > 0 else f'{slope:.2f}%' if slope else '—'}</td>
            <td style="padding:4px 8px;color:{rs_color};font-size:12px;border-bottom:1px solid {bdr};">{f'+{rs:.1f}%' if rs and rs > 0 else f'{rs:.1f}%' if rs else '—'}</td>
        </tr>
        """

    # VIX section
    vix_close = vix.get("close", 0) or 0
    vix_regime = vix.get("regime", "?")
    vix_trend = vix.get("trend", "?")
    vix_color = grn if vix_close < 15 else blu if vix_close < 25 else amb if vix_close < 35 else red

    # Volume section
    ad_ratio = volume.get("nyse_ad_ratio")
    fomo = volume.get("fomo_ratio")
    panic = volume.get("panic_ratio")
    vol_favorable = volume.get("favorable", False)

    # Position section
    pos_html = ""
    if position:
        pnl = position.get("unrealized_pnl", 0) or 0
        pnl_color = grn if pnl >= 0 else red
        pos_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
            <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">CURRENT POSITION</td></tr>
            <tr><td style="padding:16px;">
                <div style="font-size:18px;color:{tx};font-weight:600;margin-bottom:8px;">{position['symbol']} — {position.get('direction', 'Long')}</div>
                <table>
                    <tr><td style="color:{txd};font-size:12px;padding:2px 16px 2px 0;">Entry:</td><td style="color:{tx};font-size:12px;">${position['entry_price']:.2f} on {position.get('entry_date', '?')}</td></tr>
                    <tr><td style="color:{txd};font-size:12px;padding:2px 16px 2px 0;">Current:</td><td style="color:{tx};font-size:12px;">${position['current_price']:.2f}</td></tr>
                    <tr><td style="color:{txd};font-size:12px;padding:2px 16px 2px 0;">P&L:</td><td style="color:{pnl_color};font-size:12px;font-weight:600;">{'+'if pnl>=0 else ''}{pnl:.1f}%</td></tr>
                    <tr><td style="color:{txd};font-size:12px;padding:2px 16px 2px 0;">Stop:</td><td style="color:{red};font-size:12px;">${position.get('stop', 0):.2f}</td></tr>
                    {f'<tr><td style="color:{txd};font-size:12px;padding:2px 16px 2px 0;">Target:</td><td style="color:{grn};font-size:12px;">${position["target"]:.2f}</td></tr>' if position.get("target") and not position.get("partial_exited") else ''}
                </table>
            </td></tr>
        </table>
        """

    # Trade history
    trade_html = ""
    closed_trades = [t for t in trades if t.get("exit_date")]
    if closed_trades:
        trade_rows = ""
        for t in closed_trades[:5]:
            pnl = t.get("pnl_pct", 0) or 0
            tc = grn if pnl >= 0 else red
            trade_rows += f"""
            <tr>
                <td style="padding:4px 8px;color:{tx};font-size:11px;border-bottom:1px solid {bdr};">{t.get('symbol','?')}</td>
                <td style="padding:4px 8px;color:{txd};font-size:11px;border-bottom:1px solid {bdr};">{t.get('entry_date','?')}</td>
                <td style="padding:4px 8px;color:{txd};font-size:11px;border-bottom:1px solid {bdr};">{t.get('exit_date','?')}</td>
                <td style="padding:4px 8px;color:{tc};font-size:11px;font-weight:600;border-bottom:1px solid {bdr};text-align:right;">{'+'if pnl>=0 else ''}{pnl:.1f}%</td>
                <td style="padding:4px 8px;color:{txd};font-size:11px;border-bottom:1px solid {bdr};">{t.get('exit_reason','')}</td>
            </tr>
            """
        trade_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
            <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">RECENT TRADES</td></tr>
            <tr><td style="padding:8px;">
                <table width="100%">
                    <tr>
                        <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">SYMBOL</td>
                        <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">ENTRY</td>
                        <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">EXIT</td>
                        <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;text-align:right;">P&L</td>
                        <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">REASON</td>
                    </tr>
                    {trade_rows}
                </table>
            </td></tr>
        </table>
        """

    # Signal summary
    score = signal.get("score", 0) or 0
    sig_asset = signal.get("asset", "BIL")
    sig_dir = signal.get("direction", "—")
    sig_details = signal.get("details", "")
    sig_color = grn if score >= 3 and sig_asset != "BIL" else amb if score >= 2 else txd

    # Full HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background:{bg};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;padding:20px;">

        <!-- Header -->
        <tr><td style="padding:16px 0;border-bottom:2px solid {bdr};">
            <div style="font-size:20px;font-weight:700;color:{tx};">ASSET <span style="color:{grn};">REVESTING</span></div>
            <div style="font-size:12px;color:{txd};margin-top:4px;">Daily Report — Data through {data_date} — {state_label}</div>
        </td></tr>

        <!-- ACTION ITEMS -->
        <tr><td style="padding:20px 0 8px;">
            <div style="font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;margin-bottom:4px;">📋 WHAT YOU NEED TO DO</div>
            <div style="font-size:12px;color:{txd};margin-bottom:12px;">Review these items before market open. Wait 15-30 min after open before placing orders.</div>
        </td></tr>
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                {action_html}
            </table>
        </td></tr>

        <!-- Narrative -->
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">MARKET NARRATIVE</td></tr>
                <tr><td style="padding:16px;">
                    {''.join(f'<p style="font-size:13px;color:{tx if i<len(narrative)-1 else "#ffffff"};line-height:1.6;margin:0 0 {10 if i<len(narrative)-1 else 0}px 0;{"font-weight:600;" if i==len(narrative)-1 else ""}">{p}</p>' for i,p in enumerate(narrative))}
                </td></tr>
            </table>
        </td></tr>

        <!-- Position (if any) -->
        <tr><td>{pos_html}</td></tr>

        <!-- Signal -->
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">CURRENT SIGNAL</td></tr>
                <tr><td style="padding:16px;">
                    <div style="font-size:16px;color:{sig_color};font-weight:600;margin-bottom:6px;">{sig_asset} — {sig_dir} ({score}/4 pillars)</div>
                    <div style="font-size:12px;color:{txd};">{sig_details}</div>
                </td></tr>
            </table>
        </td></tr>

        <!-- Market Conditions: VIX + Breadth -->
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">MARKET CONDITIONS</td></tr>
                <tr><td style="padding:16px;">
                    <table width="100%">
                        <tr>
                            <td style="vertical-align:top;width:50%;padding-right:12px;">
                                <div style="font-size:10px;color:{txd};font-weight:600;margin-bottom:4px;">VIX</div>
                                <div style="font-size:24px;color:{vix_color};font-weight:600;">{vix_close:.1f}</div>
                                <div style="font-size:11px;color:{txd};">{vix_regime.upper()} — {vix_trend}</div>
                            </td>
                            <td style="vertical-align:top;width:50%;">
                                <div style="font-size:10px;color:{txd};font-weight:600;margin-bottom:4px;">NYSE BREADTH</div>
                                <div style="font-size:14px;color:{tx};font-weight:600;">A/D Ratio: {f'{ad_ratio:.3f}' if ad_ratio else '—'}</div>
                                <div style="font-size:11px;color:{txd};margin-top:2px;">
                                    Panic: {f'{panic:.2f}' if panic else '—'} | 
                                    FOMO: {f'{fomo:.2f}' if fomo else '—'} | 
                                    {'✓ Favorable' if vol_favorable else '✗ Not favorable'}
                                </div>
                            </td>
                        </tr>
                    </table>
                </td></tr>
            </table>
        </td></tr>

        <!-- Stages -->
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">STAGE ANALYSIS</td></tr>
                <tr><td style="padding:8px;">
                    <table width="100%">
                        {stage_html}
                    </table>
                </td></tr>
            </table>
        </td></tr>

        <!-- Indicators -->
        <tr><td>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:{card};border:1px solid {bdr};border-radius:8px;margin-bottom:20px;">
                <tr><td style="padding:12px 16px;border-bottom:1px solid {bdr};font-size:11px;color:{txd};font-weight:600;letter-spacing:1px;">INDICATORS</td></tr>
                <tr><td style="padding:8px;">
                    <table width="100%">
                        <tr>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;"></td>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">CLOSE</td>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">SMA50</td>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">SMA150</td>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">SLOPE</td>
                            <td style="padding:4px 8px;color:{txd};font-size:10px;font-weight:600;">RS</td>
                        </tr>
                        {ind_html}
                    </table>
                </td></tr>
            </table>
        </td></tr>

        <!-- Recent Trades -->
        <tr><td>{trade_html}</td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 0;border-top:1px solid {bdr};">
            <div style="font-size:11px;color:{txd};text-align:center;">
                Asset Revesting Signal Engine — Generated {generated}<br>
                Signals generate at close, execute at next open. Wait 15-30 min after open before placing orders.
            </div>
        </td></tr>

    </table>
    </body>
    </html>
    """

    return html


def format_email_subject(report):
    """Generate a concise, action-oriented subject line."""
    actions = report["actions"]
    data_date = report["data_date"]

    if not actions:
        return f"Asset Revesting [{data_date}]"

    first = actions[0]
    prio = first["priority"]

    if "EMERGENCY" in prio or "SELL" in prio:
        return f"🚨 ACTION REQUIRED — {first['action']}"
    elif "BUY" in prio or "SELL 25%" in prio:
        return f"⚡ ACTION REQUIRED — {first['action']}"
    elif "WATCH" in prio:
        return f"⚠️ {first['action']}"
    elif "HOLD" in prio:
        return f"✅ HOLD — {first['action']}"
    elif "DO NOTHING" in prio:
        return f"✅ No action — {first['action']}"
    else:
        return f"Asset Revesting [{data_date}] — {first['action']}"


# =============================================================================
# EMAIL SENDING
# =============================================================================

def send_email(report, db_path=None):
    """Send the daily report email using configured SMTP settings."""
    config = get_email_config(db_path)
    if not config:
        print("  No email configured. Use the dashboard to set up email.")
        return False

    if not config.get("enabled"):
        print("  Email is disabled in settings.")
        return False

    recipient = config.get("recipient_email", "")
    if not recipient:
        print("  No recipient email configured.")
        return False

    smtp_server = config.get("smtp_server", "smtp.gmail.com")
    smtp_port = config.get("smtp_port", 587)
    smtp_user = config.get("smtp_user", "")
    smtp_password = config.get("smtp_password", "")

    if not smtp_user or not smtp_password:
        print("  SMTP credentials not configured.")
        return False

    subject = format_email_subject(report)
    html_body = format_email_html(report)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Asset Revesting <{smtp_user}>"
    msg["To"] = recipient
    reply_to = config.get("reply_to_email", "")
    if reply_to:
        msg["Reply-To"] = reply_to

    # Plain text fallback
    plain_text = "Asset Revesting Daily Report\n\n"
    for a in report["actions"]:
        plain_text += f"{a['priority']} — {a['action']}\n{a['detail']}\n\n"
    if report.get("narrative"):
        plain_text += "--- MARKET NARRATIVE ---\n\n"
        for p in report["narrative"]:
            plain_text += f"{p}\n\n"

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipient, msg.as_string())
        print(f"  ✓ Report emailed to {recipient}")
        return True
    except Exception as e:
        print(f"  ✗ Email failed: {e}")
        return False


def run_daily_report(db_path=None):
    """
    Full daily workflow: update data → generate report → send email.
    Call this after market close (4:30 PM ET or later).
    """
    from asset_revesting.data.ingestion import fetch_all
    from asset_revesting.core.indicators import compute_all_indicators
    from asset_revesting.core.stage_analysis import compute_stage_history

    print("=" * 60)
    print("ASSET REVESTING — DAILY REPORT")
    print("=" * 60)

    # Step 1: Update data
    print("\n[1/3] Updating market data...")
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    try:
        fetch_all(start_date=start, db_path=db_path)
        compute_all_indicators(db_path=db_path)
        compute_stage_history(db_path=db_path)
        print("  Data updated.")
    except Exception as e:
        print(f"  Warning: update failed ({e}). Using cached data.")

    # Step 2: Generate report
    print("\n[2/3] Generating report...")
    report = generate_report(db_path=db_path)

    # Print summary to console
    print(f"\n  Data date: {report['data_date']}")
    print(f"  Portfolio: {report['portfolio']['state']} (${report['portfolio']['total_equity']:,.2f})")
    print(f"  Signal: {report['signal'].get('asset', '?')} {report['signal'].get('score', 0)}/4 pillars")
    print(f"\n  Actions:")
    for a in report["actions"]:
        print(f"    {a['priority']} {a['action']}")

    # Step 3: Send email
    print("\n[3/3] Sending email...")
    success = send_email(report, db_path=db_path)

    if not success:
        print("\n  To configure email, open the dashboard and click 'Email Settings'.")
        print("  Or run: python -m asset_revesting.run test-email")

    print("\n" + "=" * 60)
    return report
