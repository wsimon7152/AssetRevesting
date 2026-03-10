#!/bin/bash
# Run the daily report, then restart the dashboard server so it always
# runs the latest code. Called by the LaunchAgent at 5 PM on weekdays.

set -e

cd /Users/waltersimon/AssetRevesting

# 1. Run the daily report
/opt/miniconda3/bin/python -m asset_revesting.run report

# 2. Restart the dashboard server (picks up any code changes from the day)
launchctl kickstart -k "gui/$(id -u)/com.assetrevesting.dashboard" 2>/dev/null || true

echo "$(date): Report sent and dashboard restarted." >> /Users/waltersimon/Library/Logs/AssetRevesting/report.log
