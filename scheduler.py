"""
Scheduler for the trading bot.
Runs bot.py at:
  - 9:30am ET  (market open)
  - 12:00pm ET (midday)
  - 3:30pm ET  (market close)
Skips weekends and US market holidays.
"""

import schedule
import time
import subprocess
import sys
from datetime import date
import pytz
from zoneinfo import ZoneInfo

# US Market holidays 2026 (NYSE)
MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 11, 27), # Black Friday (early close — skip for safety)
    date(2026, 12, 25), # Christmas
}

def is_market_open():
    """Return True if today is a trading day."""
    today = date.today()
    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        print(f"⏭️  Skipping — weekend ({today.strftime('%A')})")
        return False
    if today in MARKET_HOLIDAYS_2026:
        print(f"⏭️  Skipping — market holiday ({today})")
        return False
    return True

def run_scan(scan_type):
    """Execute the bot for the given scan type."""
    if not is_market_open():
        return
    print(f"\n🚀 Launching {scan_type} scan...")
    result = subprocess.run(
        [sys.executable, "bot.py", scan_type],
        capture_output=False
    )
    if result.returncode != 0:
        print(f"❌ Bot exited with code {result.returncode}")

def market_open():
    run_scan("market_open")

def midday():
    run_scan("midday")

def market_close():
    run_scan("market_close")

# ── Schedule all three scans (ET timezone) ───────────────────────────────────
# Railway runs in UTC; these times are ET converted to UTC
# ET = UTC-4 (EDT, summer) / UTC-5 (EST, winter)
# Using 13:30 UTC = 9:30am EDT | 16:00 UTC = 12:00pm EDT | 19:30 UTC = 3:30pm EDT

schedule.every().day.at("13:30").do(market_open)   # 9:30am ET
schedule.every().day.at("16:00").do(midday)         # 12:00pm ET
schedule.every().day.at("19:30").do(market_close)   # 3:30pm ET

print("📅 Scheduler started. Waiting for market hours...")
print("   9:30am ET  → market open scan")
print("  12:00pm ET  → midday scan")
print("   3:30pm ET  → market close scan")
print()

while True:
    schedule.run_pending()
    time.sleep(60)  # check every minute
