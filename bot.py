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
import re
import requests
import anthropic
import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

# ── Config (set these as Railway environment variables) ──────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
PUSHOVER_APP_TOKEN  = os.environ["PUSHOVER_APP_TOKEN"]
PUSHOVER_USER_KEY   = os.environ["PUSHOVER_USER_KEY"]
ROBINHOOD_USERNAME  = os.environ["ROBINHOOD_USERNAME"]
ROBINHOOD_PASSWORD  = os.environ["ROBINHOOD_PASSWORD"]
APPROVAL_TIMEOUT    = int(os.environ.get("APPROVAL_TIMEOUT", "1800"))  # 30 min default
ACCOUNT_NUMBER      = os.environ["RH_ACCOUNT_NUMBER"]

# ── Robinhood helpers (via robin_stocks) ─────────────────────────────────────
def rh_login():
    """Login to Robinhood, reusing stored session if available."""
    login = rh.login(
        username=ROBINHOOD_USERNAME,
        password=ROBINHOOD_PASSWORD,
        expiresIn=86400 * 7,
        store_session=True,
        mfa_code=None
    )
    return login

def get_portfolio():
    """Get account portfolio value and buying power."""
    profile = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER)
    portfolio = rh.profiles.load_portfolio_profile(account_number=ACCOUNT_NUMBER)
    buying_power = float(profile.get("buying_power", 0))
    equity = float(portfolio.get("equity", 0)) or buying_power
    return equity, buying_power

def get_positions():
    """Get current open positions."""
    positions = rh.get_open_stock_positions(account_number=ACCOUNT_NUMBER)
    result = []
    for p in (positions or []):
        try:
            symbol = rh.get_symbol_by_url(p.get("instrument"))
            result.append({
                "symbol": symbol,
                "quantity": p.get("quantity"),
                "average_buy_price": p.get("average_buy_price")
            })
        except Exception:
            pass
    return result

def get_quote(symbol):
    """Get current quote for a symbol."""
    quote = rh.get_latest_price(symbol)
    if quote and len(quote) > 0:
        return float(quote[0])
    return 0.0

def place_order(symbol, side, quantity, price):
    print(f"📤 Placing order: {side} {quantity} {symbol}")
    if side == "buy":
        order = rh.order_buy_market(
            symbol=symbol,
            quantity=quantity,
            account_number=ACCOUNT_NUMBER,
            timeInForce="gfd"
        )
    else:
        order = rh.order_sell_market(
            symbol=symbol,
            quantity=quantity,
            account_number=ACCOUNT_NUMBER,
            timeInForce="gfd"
        )
    print(f"📥 Order response: {json.dumps(order, indent=2)}")
    if not order or order.get("detail") or not order.get("id"):
        raise Exception(f"Order rejected: {order}")
    return order
    
# ── Pushover notifications ───────────────────────────────────────────────────
def send_notification(title, message, priority=0):
    """Send a push notification via Pushover."""
    requests.post("https://api.pushover.net/1/messages.json", data={
        "token":    PUSHOVER_APP_TOKEN,
        "user":     PUSHOVER_USER_KEY,
        "title":    title,
        "message":  message,
        "priority": priority,
    })

def send_approval_request(trade, trade_id):
    msg = (
        f"🤖 Trade Recommendation #{trade_id}\n\n"
        f"Action: {trade['side'].upper()} {trade['symbol']}\n"
        f"Quantity: {trade['quantity']} shares\n"
        f"Est. Value: ${trade['estimated_value']:.2f}\n"
        f"Reason: {trade['reason']}\n\n"
        f"Open your dashboard to Approve or Deny.\n"
        f"Auto-expires in {APPROVAL_TIMEOUT // 60} minutes."
    )
    send_notification("⚡ Trade Approval Needed", msg, priority=1)

# ── Approval state ───────────────────────────────────────────────────────────
import redis
redis_client = redis.from_url(os.environ["REDIS_URL"])

def save_pending(trade_id, trade):
    data = {**trade, "timestamp": time.time()}
    redis_client.setex(f"trade:{trade_id}", 86400, json.dumps(data))

def get_approval_status(trade_id):
    data = redis_client.get(f"trade:{trade_id}")
    if data:
        return json.loads(data).get("status", "pending")
    return "pending"

def wait_for_approval(trade_id):
    start = time.time()
    while time.time() - start < APPROVAL_TIMEOUT:
        status = get_approval_status(trade_id)
        if status == "approved":
            return True
        if status == "denied":
            return False
        time.sleep(30)
    send_notification("⏰ Trade Expired", f"Trade #{trade_id} approval timed out and was not executed.")
    return False

# ── Claude AI analysis ───────────────────────────────────────────────────────
def get_claude_recommendation(portfolio_value, buying_power, positions, scan_type):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    max_trade_value = portfolio_value * 0.20

    system_prompt = """You are an aggressive short-term trading assistant managing a small cash account. Your goal is maximum short-term gains.

STRATEGY:
- Focus on HIGH MOMENTUM plays — stocks moving hard today with volume
- Prioritize TECH, AI, semiconductors, cybersecurity, quantum computing, biotech, and emerging growth sectors
- Look for catalysts: earnings beats, product launches, analyst upgrades, sector momentum, news events
- Prefer stocks with strong pre-market or intraday momentum
- Short term holds — hours to a few days, not weeks
- Be aggressive — if there's a strong setup, recommend it
- Crypto is fair game for high volatility plays (BTC, ETH, SOL, emerging altcoins)
- ETFs for sector plays (ARKK, SOXL, TECL, QQQ options proxies)
- Always have recommendations if market is open — no opportunities is rarely the right answer
- Look for the day's biggest movers and identify if momentum will continue

STRICT RULES (never break these):
- No margin trading ever
- No single trade > 20% of total account value  
- Cash account only
- Always provide a clear reason for each trade

You MUST respond with ONLY a valid JSON object. No text before it. No text after it. No markdown. Just raw JSON starting with { and ending with }.

JSON format:
{
  "market_summary": "string",
  "recommendations": [
    {
      "symbol": "TICKER",
      "side": "buy",
      "allocation_pct": 15,
      "reason": "string",
      "asset_type": "stock"
    }
  ],
  "hold_current": true,
  "notes": "string"
}"""

    positions_str = json.dumps(positions, indent=2) if positions else "No open positions"

    user_prompt = f"""Scan type: {scan_type}
Time: {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d %H:%M ET')}
Portfolio: ${portfolio_value:.2f}
Buying Power: ${buying_power:.2f}
Max Per Trade: ${max_trade_value:.2f}
Positions: {positions_str}

Search current market conditions and return ONLY the JSON object."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        # Combine all text blocks
        full_text = ""
        for block in response.content:
            if block.type == "text":
                full_text += block.text

        print(f"🔍 Response length: {len(full_text)} chars")

        # Try 1: direct parse after stripping markdown
        try:
            clean = full_text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Try 2: find first { to last }
        try:
            start = full_text.index("{")
            end = full_text.rindex("}") + 1
            return json.loads(full_text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        # Try 3: regex
        try:
            match = re.search(r'\{.*\}', full_text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except json.JSONDecodeError:
            pass

        print(f"❌ All parse attempts failed. Preview: {full_text[:300]}")
        return {"recommendations": [], "market_summary": "Unable to parse response", "notes": ""}

    except Exception as e:
        print(f"❌ Claude API error: {e}")
        return {"recommendations": [], "market_summary": f"API error: {str(e)}", "notes": ""}
# ── Main scan logic ──────────────────────────────────────────────────────────
def run_scan(scan_type="manual"):
    print(f"\n{'='*50}")
    print(f"Running {scan_type} scan at {datetime.now()}")
    print('='*50)

    try:
        # 1. Login
        rh_login()
        print("✅ Robinhood login successful")

        # 2. Get portfolio state
        portfolio_value, buying_power = get_portfolio()
        positions = get_positions()

        print(f"💰 Portfolio: ${portfolio_value:.2f} | Buying Power: ${buying_power:.2f}")
        print(f"📊 Open positions: {len(positions)}")

        # 3. Ask Claude
        send_notification("🔍 Scanning Market", f"{scan_type.replace('_', ' ').title()} scan in progress...")
        recs = get_claude_recommendation(portfolio_value, buying_power, positions, scan_type)
        recs = get_claude_recommendation(portfolio_value, buying_power, positions, scan_type)
        if recs is None:
            recs = {"recommendations": [], "market_summary": "No response", "notes": ""}
        
        print(f"🤖 Market summary: {recs.get('market_summary', 'N/A')}")
        print(f"📋 Recommendations: {len(recs.get('recommendations', []))}")

        # 4. No recommendations
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

            trade_value = portfolio_value * (allocation_pct / 100)
            trade_value = min(trade_value, portfolio_value * 0.20)  # hard 20% cap

            if side == "buy" and trade_value > buying_power:
                send_notification("⚠️ Skipped Trade", f"{symbol}: Not enough buying power")
                continue

            price = get_quote(symbol)
            if price <= 0:
                print(f"⚠️ Could not get price for {symbol}, skipping")
                continue

            quantity = round(trade_value / price, 6)

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
            send_approval_request(trade, trade_id)
            print(f"📱 Approval request sent for {side} {symbol}")

            approved = wait_for_approval(trade_id)

            if approved:
                try:
                    order = place_order(symbol, side, quantity, price)
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

# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    scan_type = sys.argv[1] if len(sys.argv) > 1 else "manual"
    run_scan(scan_type)
