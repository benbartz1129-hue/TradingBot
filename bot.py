"""
Robinhood AI Trading Bot
- Scans at market open (9:30am ET), midday (12:00pm ET), and close (3:30pm ET)
- Uses Claude to analyze market and recommend trades
- Sends Pushover notifications for approval before executing
- Enforces: no margin, max 20% of account per trade
"""

import os
import json
import time
import requests
import anthropic
from datetime import datetime
import pytz

# ── Config (set these as Railway environment variables) ──────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
PUSHOVER_APP_TOKEN  = os.environ["PUSHOVER_APP_TOKEN"]
PUSHOVER_USER_KEY   = os.environ["PUSHOVER_USER_KEY"]
ROBINHOOD_USERNAME  = os.environ["ROBINHOOD_USERNAME"]
ROBINHOOD_PASSWORD  = os.environ["ROBINHOOD_PASSWORD"]
APPROVAL_TIMEOUT    = int(os.environ.get("APPROVAL_TIMEOUT", "1800"))  # 30 min default

# Robinhood Agentic account number
ACCOUNT_NUMBER      = os.environ["RH_ACCOUNT_NUMBER"]  # 496478850

# ── Robinhood API helpers ────────────────────────────────────────────────────
RH_BASE = "https://api.robinhood.com"

def rh_login():
    """Login to Robinhood and return auth token."""
    r = requests.post(f"{RH_BASE}/api-token-auth/", json={
        "username": ROBINHOOD_USERNAME,
        "password": ROBINHOOD_PASSWORD,
        "grant_type": "password",
        "client_id": "c82SH0WZOsabOXGP2sxqcj34FFK0aYdS",
        "scope": "internal",
        "expires_in": 86400,
        "device_token": os.environ.get("RH_DEVICE_TOKEN", ""),
    })
    r.raise_for_status()
    return r.json().get("access_token")

def rh_headers(token):
    return {"Authorization": f"Bearer {token}"}

def get_portfolio(token):
    """Get account portfolio value and buying power."""
    r = requests.get(
        f"{RH_BASE}/portfolios/{ACCOUNT_NUMBER}/",
        headers=rh_headers(token)
    )
    r.raise_for_status()
    return r.json()

def get_positions(token):
    """Get current open positions."""
    r = requests.get(
        f"{RH_BASE}/positions/?account={ACCOUNT_NUMBER}&nonzero=true",
        headers=rh_headers(token)
    )
    r.raise_for_status()
    return r.json().get("results", [])

def get_quote(token, symbol):
    """Get current quote for a symbol."""
    r = requests.get(
        f"{RH_BASE}/quotes/{symbol}/",
        headers=rh_headers(token)
    )
    r.raise_for_status()
    return r.json()

def place_order(token, symbol, side, quantity, price):
    """Place a market order."""
    payload = {
        "account": f"{RH_BASE}/accounts/{ACCOUNT_NUMBER}/",
        "instrument": f"{RH_BASE}/instruments/?symbol={symbol}",
        "symbol": symbol,
        "side": side,          # "buy" or "sell"
        "quantity": quantity,
        "type": "market",
        "time_in_force": "gfd",  # good for day
        "trigger": "immediate",
        "price": price,
    }
    r = requests.post(
        f"{RH_BASE}/orders/",
        json=payload,
        headers=rh_headers(token)
    )
    r.raise_for_status()
    return r.json()

# ── Pushover notifications ───────────────────────────────────────────────────
def send_notification(title, message, priority=0):
    """Send a push notification via Pushover."""
    requests.post("https://api.pushover.net/1/messages.json", data={
        "token":   PUSHOVER_APP_TOKEN,
        "user":    PUSHOVER_USER_KEY,
        "title":   title,
        "message": message,
        "priority": priority,  # 1 = high priority, requires acknowledgment
    })

def send_approval_request(trade, trade_id):
    """
    Send a high-priority Pushover notification asking for trade approval.
    User must reply via the approval endpoint or the web dashboard.
    """
    msg = (
        f"🤖 Trade Recommendation #{trade_id}\n\n"
        f"Action: {trade['side'].upper()} {trade['symbol']}\n"
        f"Quantity: {trade['quantity']} shares\n"
        f"Est. Value: ${trade['estimated_value']:.2f}\n"
        f"Reason: {trade['reason']}\n\n"
        f"Reply APPROVE or DENY via the bot dashboard.\n"
        f"Auto-expires in {APPROVAL_TIMEOUT // 60} minutes."
    )
    send_notification("⚡ Trade Approval Needed", msg, priority=1)

# ── Claude AI analysis ───────────────────────────────────────────────────────
def get_claude_recommendation(portfolio_value, buying_power, positions, scan_type):
    """Ask Claude to analyze current state and recommend trades."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    max_trade_value = portfolio_value * 0.20  # 20% rule

    system_prompt = """You are an aggressive but smart trading assistant managing a small cash account.
Your job is to recommend specific trades based on current market conditions.

STRICT RULES you must never break:
- No margin trading ever
- No single trade > 20% of total account value
- Cash account only
- Always provide a clear reason for each trade

Respond ONLY with a JSON object in this exact format:
{
  "market_summary": "Brief 1-2 sentence market overview",
  "recommendations": [
    {
      "symbol": "TICKER",
      "side": "buy" or "sell",
      "allocation_pct": 10,
      "reason": "Why this trade makes sense right now",
      "asset_type": "stock" or "crypto" or "etf"
    }
  ],
  "hold_current": true or false,
  "notes": "Any other relevant notes"
}

If there are no good opportunities, return an empty recommendations array.
Keep allocations to 10-20% per trade maximum."""

    positions_str = json.dumps(positions, indent=2) if positions else "No open positions"

    user_prompt = f"""Current {scan_type} scan - {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d %H:%M ET')}

Portfolio Value: ${portfolio_value:.2f}
Buying Power: ${buying_power:.2f}
Max Per Trade: ${max_trade_value:.2f}

Current Positions:
{positions_str}

Analyze current market conditions and recommend specific trades.
Use web search to check current prices and market sentiment.
Focus on momentum, news catalysts, and technical setups.
Aggressive but risk-managed strategy."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )

    # Extract text content from response
    for block in response.content:
        if block.type == "text":
            try:
                # Strip any markdown code fences
                text = block.text.strip().replace("```json", "").replace("```", "").strip()
                return json.loads(text)
            except json.JSONDecodeError:
                pass

    return {"recommendations": [], "market_summary": "Unable to parse response", "notes": ""}

# ── Approval state (simple file-based for Railway) ──────────────────────────
APPROVALS_FILE = "/tmp/pending_approvals.json"

def save_pending(trade_id, trade):
    try:
        with open(APPROVALS_FILE, "r") as f:
            pending = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pending = {}
    pending[str(trade_id)] = {**trade, "timestamp": time.time()}
    with open(APPROVALS_FILE, "w") as f:
        json.dump(pending, f)

def get_approval_status(trade_id):
    """Check if a trade has been approved or denied. Returns 'pending', 'approved', or 'denied'."""
    try:
        with open(APPROVALS_FILE, "r") as f:
            pending = json.load(f)
        trade = pending.get(str(trade_id), {})
        return trade.get("status", "pending")
    except (FileNotFoundError, json.JSONDecodeError):
        return "pending"

def wait_for_approval(trade_id):
    """Poll for approval status until timeout."""
    start = time.time()
    while time.time() - start < APPROVAL_TIMEOUT:
        status = get_approval_status(trade_id)
        if status == "approved":
            return True
        if status == "denied":
            return False
        time.sleep(30)  # check every 30 seconds
    # Timed out — treat as denied
    send_notification("⏰ Trade Expired", f"Trade #{trade_id} approval timed out and was not executed.")
    return False

# ── Main scan logic ──────────────────────────────────────────────────────────
def run_scan(scan_type="market_open"):
    """Run a full scan cycle: analyze → notify → wait for approval → execute."""
    print(f"\n{'='*50}")
    print(f"Running {scan_type} scan at {datetime.now()}")
    print('='*50)

    try:
        # 1. Login to Robinhood
        token = rh_login()
        print("✅ Robinhood login successful")

        # 2. Get portfolio state
        portfolio = get_portfolio(token)
        portfolio_value = float(portfolio.get("equity", 500))
        buying_power = float(portfolio.get("withdrawable_amount", 0))
        positions = get_positions(token)

        print(f"💰 Portfolio: ${portfolio_value:.2f} | Buying Power: ${buying_power:.2f}")
        print(f"📊 Open positions: {len(positions)}")

        # 3. Ask Claude for recommendations
        send_notification("🔍 Scanning Market", f"{scan_type.replace('_', ' ').title()} scan in progress...")
        recs = get_claude_recommendation(portfolio_value, buying_power, positions, scan_type)

        print(f"🤖 Market summary: {recs.get('market_summary', 'N/A')}")
        print(f"📋 Recommendations: {len(recs.get('recommendations', []))}")

        # 4. No recommendations? Notify and exit
        if not recs.get("recommendations"):
            send_notification(
                "📊 Scan Complete — No Trades",
                f"{scan_type.replace('_', ' ').title()}: {recs.get('market_summary', 'No opportunities found.')}\n\n{recs.get('notes', '')}"
            )
            return

        # 5. Process each recommendation
        for i, rec in enumerate(recs["recommendations"]):
            symbol = rec["symbol"]
            side = rec["side"]
            allocation_pct = rec["allocation_pct"]
            reason = rec["reason"]

            # Calculate quantity
            trade_value = portfolio_value * (allocation_pct / 100)
            trade_value = min(trade_value, portfolio_value * 0.20)  # hard 20% cap

            if side == "buy" and trade_value > buying_power:
                send_notification("⚠️ Skipped Trade", f"{symbol}: Not enough buying power (${buying_power:.2f} available)")
                continue

            # Get current price
            try:
                quote = get_quote(token, symbol)
                price = float(quote.get("last_trade_price") or quote.get("last_extended_hours_trade_price", 0))
            except Exception:
                price = 0

            if price <= 0:
                print(f"⚠️ Could not get price for {symbol}, skipping")
                continue

            quantity = round(trade_value / price, 6)  # fractional shares

            trade = {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "estimated_value": trade_value,
                "reason": reason,
                "asset_type": rec.get("asset_type", "stock"),
                "status": "pending"
            }

            trade_id = f"{int(time.time())}_{i}"
            save_pending(trade_id, trade)

            # 6. Send approval notification
            send_approval_request(trade, trade_id)
            print(f"📱 Approval request sent for {side} {symbol}")

            # 7. Wait for approval
            approved = wait_for_approval(trade_id)

            if approved:
                # 8. Execute the trade
                try:
                    order = place_order(token, symbol, side, quantity, price)
                    send_notification(
                        "✅ Trade Executed",
                        f"{side.upper()} {quantity:.4f} {symbol} @ ~${price:.2f}\nOrder ID: {order.get('id', 'N/A')}"
                    )
                    print(f"✅ Order placed: {side} {symbol}")
                except Exception as e:
                    send_notification("❌ Trade Failed", f"{symbol}: {str(e)}")
                    print(f"❌ Order failed: {e}")
            else:
                send_notification("🚫 Trade Denied", f"{side.upper()} {symbol} was not executed.")
                print(f"🚫 Trade denied or timed out: {symbol}")

    except Exception as e:
        send_notification("🚨 Bot Error", f"Scan failed: {str(e)}", priority=1)
        print(f"❌ Scan error: {e}")
        raise


# ── Entry point (called by scheduler.py) ────────────────────────────────────
if __name__ == "__main__":
    import sys
    scan_type = sys.argv[1] if len(sys.argv) > 1 else "manual"
    run_scan(scan_type)
