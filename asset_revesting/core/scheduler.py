# asset_revesting/core/scheduler.py
"""
Automatic report scheduling using macOS LaunchAgent.

This creates a persistent background job that:
- Survives reboots
- Runs at the configured time
- Sends the daily email report
- Logs output for troubleshooting

No cron, no manual setup. Just click "Enable" in the dashboard.
"""

import os
import sys
import subprocess
import plistlib
from pathlib import Path
from datetime import datetime

from asset_revesting.data.database import get_connection

PLIST_NAME = "com.assetrevesting.dailyreport"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_NAME}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "AssetRevesting"


def _get_python_path():
    """Get the full path to the current Python interpreter."""
    return sys.executable


def _get_project_dir():
    """Get the asset_revesting project directory."""
    # Go up from core/scheduler.py → core → asset_revesting → parent
    return str(Path(__file__).parent.parent.parent)


def _get_schedule_config(db_path=None):
    """Read schedule settings from email_config."""
    from asset_revesting.core.email_report import get_email_config
    config = get_email_config(db_path)
    if not config:
        return {"hour": 17, "minute": 0, "days": [0, 1, 2, 3, 4, 5]}

    hour = config.get("report_hour", 17)
    minute = config.get("report_minute", 0)
    days_str = config.get("report_days", "0,1,2,3,4,5")
    days = [int(d.strip()) for d in days_str.split(",") if d.strip().isdigit()]

    return {"hour": hour, "minute": minute, "days": days}


def install_schedule(db_path=None):
    """
    Install macOS LaunchAgent for daily report.
    Returns status dict.
    """
    sched = _get_schedule_config(db_path)
    python_path = _get_python_path()
    project_dir = _get_project_dir()

    # Create log directory
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Create the wrapper script that cd's to the right directory
    script_path = Path(project_dir) / "asset_revesting" / "run_report.sh"
    script_content = f"""#!/bin/bash
# Asset Revesting Daily Report Runner
# Auto-generated — do not edit manually

cd "{project_dir}"
"{python_path}" -m asset_revesting.run report >> "{LOG_DIR}/report.log" 2>&1

# Keep only last 1000 lines of log
tail -1000 "{LOG_DIR}/report.log" > "{LOG_DIR}/report.log.tmp" 2>/dev/null
mv "{LOG_DIR}/report.log.tmp" "{LOG_DIR}/report.log" 2>/dev/null
"""
    script_path.write_text(script_content)
    os.chmod(script_path, 0o755)

    # Build calendar intervals for each scheduled day
    # macOS LaunchAgent uses: Weekday (0=Sun, 1=Mon, ... 6=Sat)
    calendar_intervals = []
    for day in sched["days"]:
        calendar_intervals.append({
            "Weekday": day,
            "Hour": sched["hour"],
            "Minute": sched["minute"],
        })

    # Build the plist
    plist = {
        "Label": PLIST_NAME,
        "ProgramArguments": [str(script_path)],
        "StartCalendarInterval": calendar_intervals,
        "StandardOutPath": str(LOG_DIR / "launchd_out.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd_err.log"),
        "WorkingDirectory": project_dir,
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/miniconda3/bin",
        },
    }

    # Ensure LaunchAgents directory exists
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Unload existing if present
    if PLIST_PATH.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", str(PLIST_PATH)],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

    # Write plist
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    # Load it
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True, text=True, timeout=10
    )

    if result.returncode != 0:
        return {
            "status": "error",
            "message": f"launchctl load failed: {result.stderr.strip()}",
            "plist_path": str(PLIST_PATH),
        }

    # Format schedule for display
    day_names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
    days_display = ", ".join(day_names.get(d, str(d)) for d in sorted(sched["days"]))
    time_display = f"{sched['hour']:02d}:{sched['minute']:02d}"

    return {
        "status": "ok",
        "message": f"Scheduled at {time_display} on {days_display}",
        "schedule": f"{time_display} on {days_display}",
        "plist_path": str(PLIST_PATH),
        "log_path": str(LOG_DIR / "report.log"),
        "installed": True,
    }


def uninstall_schedule():
    """Remove the LaunchAgent."""
    if PLIST_PATH.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", str(PLIST_PATH)],
                capture_output=True, timeout=10
            )
        except Exception:
            pass
        PLIST_PATH.unlink(missing_ok=True)

    # Also remove the wrapper script
    project_dir = _get_project_dir()
    script_path = Path(project_dir) / "asset_revesting" / "run_report.sh"
    script_path.unlink(missing_ok=True)

    return {
        "status": "ok",
        "message": "Scheduler removed. No more automatic reports.",
        "installed": False,
    }


def get_schedule_status():
    """Check if the scheduler is currently installed and active."""
    installed = PLIST_PATH.exists()

    # Check if actually loaded
    active = False
    if installed:
        try:
            result = subprocess.run(
                ["launchctl", "list", PLIST_NAME],
                capture_output=True, text=True, timeout=5
            )
            active = result.returncode == 0
        except Exception:
            pass

    # Get schedule info
    schedule_info = None
    if installed:
        try:
            sched = _get_schedule_config()
            day_names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
            days_display = ", ".join(day_names.get(d, str(d)) for d in sorted(sched["days"]))
            time_display = f"{sched['hour']:02d}:{sched['minute']:02d}"
            schedule_info = f"{time_display} on {days_display}"
        except Exception:
            schedule_info = "Unknown"

    # Check last run from log
    last_run = None
    log_file = LOG_DIR / "report.log"
    if log_file.exists():
        try:
            text = log_file.read_text()
            # Look for our report header timestamps
            for line in reversed(text.splitlines()):
                if "DAILY REPORT" in line or "Report emailed" in line or "report" in line.lower():
                    last_run = line.strip()[:80]
                    break
        except Exception:
            pass

    return {
        "installed": installed,
        "active": active,
        "schedule": schedule_info,
        "plist_path": str(PLIST_PATH) if installed else None,
        "log_path": str(LOG_DIR / "report.log"),
        "last_run": last_run,
    }
