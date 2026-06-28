"""
Scheduler for the trading bot.
- 9:30am ET  → full market scan
- 12:00pm ET → full market scan
- 3:30pm ET  → full market scan
- Every 15 mins during market hours → position monitor (no AI, no tokens)
- Fri 3:35pm ET → weekly P&L summary
Skips weekends and US market holidays.
"""

import schedule
import time
import subprocess
import sys
from datetime import date, datetime
import pytz

MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 11, 27),
    date(2026, 12, 25),
}

def is_market_open():
    today = date.today()
    if today.weekday() >= 5:
        print(f"⏭️  Skipping — weekend ({today.strftime('%A')})")
        return False
    if today in MARKET_HOLIDAYS_2026:
        print(f"⏭️  Skipping — market holiday ({today})")
        return False
    return True

def is_during_market_hours():
    """Check if current time is during market hours (9:30am-4pm ET)."""
    et = pytz.timezone('US/Eastern')
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    if now.date() in MARKET_HOLIDAYS_2026:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def run_scan(scan_type):
    if not is_market_open():
        return
    print(f"\n🚀 Launching {scan_type} scan...")
    subprocess.run([sys.executable, "bot.py", scan_type])

def run_monitor():
    if not is_during_market_hours():
        return
    print(f"\n👁️  Running position monitor...")
    subprocess.run([sys.executable, "bot.py", "monitor"])

def run_weekly_summary():
    if not is_market_open():
        return
    print(f"\n📊 Running weekly summary...")
    subprocess.run([sys.executable, "bot.py", "weekly_summary"])

def market_open():
    run_scan("market_open")

def midday():
    run_scan("midday")

def market_close():
    run_scan("market_close")

# Full scans 3x per day (UTC times for ET)
schedule.every().day.at("13:30").do(market_open)   # 9:30am ET
schedule.every().day.at("16:00").do(midday)         # 12:00pm ET
schedule.every().day.at("19:30").do(market_close)   # 3:30pm ET

# Position monitor every 15 mins
schedule.every(15).minutes.do(run_monitor)

# Weekly summary every Friday, 5 min after close
schedule.every().friday.at("19:35").do(run_weekly_summary)

print("📅 Scheduler started.")
print("   9:30am ET  → market open scan")
print("  12:00pm ET  → midday scan")
print("   3:30pm ET  → market close scan")
print("   Every 15m  → position monitor (market hours only)")
print("   Fri 3:35pm ET → weekly P&L summary")

while True:
    schedule.run_pending()
    time.sleep(60)
