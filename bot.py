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
import redis
from datetime import datetime
import pytz

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
PUSHOVER_APP_TOKEN = os.environ["PUSHOVER_APP_TOKEN"]
PUSHOVER_USER_KEY  = os.environ["PUSHOVER_USER_KEY"]
ROBINHOOD_USERNAME = os.environ["ROBINHOOD_USERNAME"]
ROBINHOOD_PASSWORD = os.environ["ROBINHOOD_PASSWORD"]
APPROVAL_TIMEOUT   = int(os.environ.get("APPROVAL_TIMEOUT", "1800"))
ACCOUNT_NUMBER     = os.environ["RH_ACCOUNT_NUMBER"]
redis_client       = redis.from_url(os.environ["REDIS_URL"])

# ── Robinhood login ───────────────────────────────────────────────────────────
def rh_login():
    login = rh.login(
        username=ROBINHOOD_USERNAME,
        password=ROBINHOOD_PASSWORD,
        expiresIn=86400 * 7,
        store_session=True,
        mfa_code=None
    )
    return login

# ── Robinhood data ────────────────────────────────────────────────────────────
def get_portfolio():
    profile   = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER)
    portfolio = rh.profiles.load_portfolio_profile(account_number=ACCOUNT_NUMBER)
    buying_power = float(profile.get("buying_power", 0))
    equity       = float(portfolio.get("equity", 0)) or buying_power
    return equity, buying_power

def get_positions():
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
    quote = rh.get_latest_price(symbol)
    if quote and len(quote) > 0:
        return float(quote[0])
    return 0.0

# ── Order execution ───────────────────────────────────────────────────────────
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

def place_option_order(symbol, option_type, strike, expiry, contracts, side):
    print(f"📤 Placing option order: {side} {contracts}x {symbol} {strike}{option_type[0].upper()} {expiry}")
    options = rh.find_options_by_expiration_and_strike(
        inputSymbols=symbol,
        expirationDate=expiry,
        strikePrice=strike,
        optionType=option_type,
        info=None
    )
    if not options or len(options) == 0:
        raise Exception(f"No options found for {symbol} {strike} {option_type} {expiry}")
    option = options[0]
    print(f"📋 Option found: {option.get('chain_symbol')} strike={option.get('strike_price')} exp={option.get('expiration_date')}")
    if side == "buy":
        order = rh.order_buy_option_limit(
            positionEffect="open",
            creditOrDebit="debit",
            price=float(option.get("ask_price", 1.00)),
            symbol=symbol,
            quantity=contracts,
            expirationDate=expiry,
            strike=strike,
            optionType=option_type,
            timeInForce="gfd",
            account_number=ACCOUNT_NUMBER
        )
    else:
        order = rh.order_sell_option_limit(
            positionEffect="close",
            creditOrDebit="credit",
            price=float(option.get("bid_price", 1.00)),
            symbol=symbol,
            quantity=contracts,
            expirationDate=expiry,
            strike=strike,
            optionType=option_type,
            timeInForce="gfd",
            account_number=ACCOUNT_NUMBER
        )
    print(f"📥 Option order response: {json.dumps(order, indent=2)}")
    if not order or not order.get("id"):
        raise Exception(f"Option order rejected: {order}")
    return order

# ── Pushover notifications ────────────────────────────────────────────────────
def send_notification(title, message, priority=0):
    requests.post("https://api.pushover.net/1/messages.json", data={
        "token":    PUSHOVER_APP_TOKEN,
        "user":     PUSHOVER_USER_KEY,
        "title":    title,
        "message":  message,
        "priority": priority,
    })

def send_approval_request(trade, trade_id):
    option_data = trade.get("option")
    if option_data:
        trade_detail = (
            f"Action: {trade['side'].upper()} OPTION on {trade['symbol']}\n"
            f"Type: {option_data.get('type', 'call').upper()}\n"
            f"Strike: ${option_data.get('strike')}\n"
            f"Expiry: {option_data.get('expiry')}\n"
            f"Contracts: {option_data.get('contracts', 1)}\n"
            f"Est. Cost: ${trade['estimated_value']:.2f}\n"
        )
    else:
        trade_detail = (
            f"Action: {trade['side'].upper()} {trade['symbol']}\n"
            f"Quantity: {trade['quantity']} shares\n"
            f"Est. Value: ${trade['estimated_value']:.2f}\n"
        )
    msg = (
        f"🤖 Trade Recommendation #{trade_id}\n\n"
        f"{trade_detail}"
        f"Reason: {trade['reason']}\n\n"
        f"Open your dashboard to Approve or Deny.\n"
        f"Auto-expires in {APPROVAL_TIMEOUT // 60} minutes."
    )
    send_notification("⚡ Trade Approval Needed", msg, priority=1)

# ── Redis state ───────────────────────────────────────────────────────────────
def save_pending(trade_id, trade):
    data = {**trade, "timestamp": time.time()}
    redis_client.set(f"trade:{trade_id}", json.dumps(data), ex=86400)

def save_trade_history(trade, trade_id, outcome, order_id=None):
    history_entry = {
        "trade_id":        trade_id,
        "symbol":          trade["symbol"],
        "side":            trade["side"],
        "quantity":        trade.get("quantity", 0),
        "price":           trade.get("price", 0),
        "estimated_value": trade.get("estimated_value", 0),
        "reason":          trade.get("reason", ""),
        "asset_type":      trade.get("asset_type", "stock"),
        "outcome":         outcome,
        "order_id":        order_id,
        "timestamp":       time.time(),
        "date":            datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d %H:%M ET')
    }
    redis_client.set(f"history:{trade_id}", json.dumps(history_entry), ex=86400 * 30)

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

# ── Claude AI analysis ────────────────────────────────────────────────────────
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
- Options are available — use them for high conviction directional plays
- Buy CALLS for strong bullish momentum plays (1-2 week expiry, slightly OTM or ATM)
- Buy PUTS for hedging existing positions or strong bearish plays
- Keep options to max 10% allocation per trade — they're higher risk
- Prefer weekly or bi-weekly expiries for short term plays
- Include strike price and expiry in recommendations

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
      "allocation_pct": 10,
      "reason": "string",
      "asset_type": "stock",
      "option": {
        "type": "call",
        "strike": 150.00,
        "expiry": "2026-06-27",
        "contracts": 1
      }
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
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        full_text = ""
        for block in response.content:
            if block.type == "text":
                full_text += block.text

        print(f"🔍 Response length: {len(full_text)} chars")

        # Try 1: direct parse
        try:
            clean = full_text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Try 2: first { to last }
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

# ── Position monitor ──────────────────────────────────────────────────────────
def monitor_positions():
    try:
        rh_login()
        positions = get_positions()

        if not positions:
            print("📊 No open positions to monitor")
            return

        for position in positions:
            symbol        = position["symbol"]
            avg_buy_price = float(position["average_buy_price"])
            quantity      = float(position["quantity"])
            current_price = get_quote(symbol)

            if current_price <= 0 or avg_buy_price <= 0:
                continue

            pct_change  = ((current_price - avg_buy_price) / avg_buy_price) * 100
            profit_loss = (current_price - avg_buy_price) * quantity

            print(f"📈 {symbol}: ${avg_buy_price:.2f} → ${current_price:.2f} ({pct_change:+.1f}%) P&L: ${profit_loss:+.2f}")

            if pct_change <= -10:
                send_notification(
                    f"🔴 STOP LOSS ALERT — {symbol}",
                    f"{symbol} is down {abs(pct_change):.1f}% from your buy price\n"
                    f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                    f"P&L: ${profit_loss:+.2f}\n\nOpen dashboard to consider selling.",
                    priority=1
                )
            elif pct_change >= 15:
                send_notification(
                    f"🟢 TAKE PROFIT ALERT — {symbol}",
                    f"{symbol} is up {pct_change:.1f}% from your buy price\n"
                    f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                    f"P&L: ${profit_loss:+.2f}\n\nOpen dashboard to consider taking profits.",
                    priority=1
                )
            elif pct_change >= 7:
                send_notification(
                    f"📈 {symbol} up {pct_change:.1f}%",
                    f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                    f"P&L: ${profit_loss:+.2f}",
                    priority=0
                )

    except Exception as e:
        print(f"❌ Monitor error: {e}")

# ── Main scan ─────────────────────────────────────────────────────────────────
def run_scan(scan_type="manual"):
    print(f"\n{'='*50}")
    print(f"Running {scan_type} scan at {datetime.now()}")
    print('='*50)

    try:
        rh_login()
        print("✅ Robinhood login successful")

        portfolio_value, buying_power = get_portfolio()
        positions = get_positions()

        print(f"💰 Portfolio: ${portfolio_value:.2f} | Buying Power: ${buying_power:.2f}")
        print(f"📊 Open positions: {len(positions)}")

        send_notification("🔍 Scanning Market", f"{scan_type.replace('_', ' ').title()} scan in progress...")
        recs = get_claude_recommendation(portfolio_value, buying_power, positions, scan_type)
        if recs is None:
            recs = {"recommendations": [], "market_summary": "No response", "notes": ""}

        print(f"🤖 Market summary: {recs.get('market_summary', 'N/A')}")
        print(f"📋 Recommendations: {len(recs.get('recommendations', []))}")

        if not recs.get("recommendations"):
            send_notification(
                "📊 Scan Complete — No Trades",
                f"{scan_type.replace('_', ' ').title()}: {recs.get('market_summary', 'No opportunities found.')}\n\n{recs.get('notes', '')}"
            )
            return

        for i, rec in enumerate(recs["recommendations"]):
            symbol         = rec["symbol"]
            side           = rec["side"]
            allocation_pct = rec["allocation_pct"]
            reason         = rec["reason"]

            # Skip hold recommendations — no action needed
            if side.lower() not in ["buy", "sell"]:
                print(f"⏭️  Skipping {side} recommendation for {symbol} — no action needed")
                continue
            
            trade_value = portfolio_value * (allocation_pct / 100)
            trade_value = min(trade_value, portfolio_value * 0.20)

            if side == "buy" and trade_value > buying_power:
                send_notification("⚠️ Skipped Trade", f"{symbol}: Not enough buying power")
                continue

            price = get_quote(symbol)
            if price <= 0:
                print(f"⚠️ Could not get price for {symbol}, skipping")
                continue

            quantity = round(trade_value / price, 6)

            trade = {
                "symbol":          symbol,
                "side":            side,
                "quantity":        quantity,
                "price":           price,
                "estimated_value": trade_value,
                "reason":          reason,
                "asset_type":      rec.get("asset_type", "stock"),
                "option":          rec.get("option"),
                "status":          "pending"
            }

            trade_id = f"{int(time.time())}_{i}"
            save_pending(trade_id, trade)
            send_approval_request(trade, trade_id)
            print(f"📱 Approval request sent for {side} {symbol}")

            approved = wait_for_approval(trade_id)

            if approved:
                try:
                    asset_type  = rec.get("asset_type", "stock")
                    option_data = rec.get("option")

                    if asset_type == "option" and option_data:
                        order = place_option_order(
                            symbol=symbol,
                            option_type=option_data.get("type", "call"),
                            strike=option_data.get("strike"),
                            expiry=option_data.get("expiry"),
                            contracts=option_data.get("contracts", 1),
                            side=side
                        )
                        send_notification(
                            "✅ Option Executed",
                            f"{side.upper()} {option_data.get('contracts', 1)}x {symbol} "
                            f"{option_data.get('strike')}{option_data.get('type', 'call')[0].upper()} "
                            f"exp {option_data.get('expiry')}\n"
                            f"Order ID: {order.get('id', 'N/A')}"
                        )
                    else:
                        order = place_order(symbol, side, quantity, price)
                        send_notification(
                            "✅ Trade Executed",
                            f"{side.upper()} {quantity:.4f} {symbol} @ ~${price:.2f}\nOrder ID: {order.get('id', 'N/A')}"
                        )
                    save_trade_history(trade, trade_id, "executed", order.get("id"))
                    print(f"✅ Order placed: {side} {symbol}")
                except Exception as e:
                    send_notification("❌ Trade Failed", f"{symbol}: {str(e)}")
                    save_trade_history(trade, trade_id, "failed")
                    print(f"❌ Order failed: {e}")
            else:
                status  = get_approval_status(trade_id)
                outcome = "denied" if status == "denied" else "expired"
                send_notification("🚫 Trade Denied", f"{side.upper()} {symbol} was not executed.")
                save_trade_history(trade, trade_id, outcome)
                print(f"🚫 Trade denied or timed out: {symbol}")

    except Exception as e:
        send_notification("🚨 Bot Error", f"Scan failed: {str(e)}", priority=1)
        print(f"❌ Scan error: {e}")
        raise

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    scan_type = sys.argv[1] if len(sys.argv) > 1 else "manual"
    if scan_type == "monitor":
        monitor_positions()
    else:
        run_scan(scan_type)
