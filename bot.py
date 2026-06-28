"""
Robinhood AI Trading Bot
- Scans at market open (9:30am ET), midday (12:00pm ET), and close (3:30pm ET)
- Uses Claude to analyze market and recommend trades
- Sends Pushover notifications for approval before executing
- Enforces: no margin, max 20% of account per trade (stocks AND options)
- Position monitor every 15 mins with tiered alerts (no spam)
- Weekly P&L summary every Friday at close
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

def get_option_quote(symbol, option_type, strike, expiry):
    """
    Fetch the real ask price for a specific option contract.
    Returns (ask_price, option_data) or (0, None) if not found.
    """
    try:
        options = rh.find_options_by_expiration_and_strike(
            inputSymbols=symbol,
            expirationDate=expiry,
            strikePrice=strike,
            optionType=option_type,
            info=None
        )
        if not options or len(options) == 0:
            print(f"⚠️ No option contract found for {symbol} {strike}{option_type[0].upper()} {expiry}")
            return 0, None

        option = options[0]
        ask_price = float(option.get("ask_price") or 0)

        if ask_price <= 0:
            print(f"⚠️ No valid ask price for {symbol} {strike}{option_type[0].upper()} {expiry}")
            return 0, None

        return ask_price, option

    except Exception as e:
        print(f"❌ Error fetching option quote: {e}")
        return 0, None

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

def place_option_order(symbol, option_type, strike, expiry, contracts, side, max_cost=None):
    """Place an options order, re-validating price hasn't moved beyond the approved cap."""
    print(f"📤 Placing option order: {side} {contracts}x {symbol} {strike}{option_type[0].upper()} {expiry}")

    ask_price, option = get_option_quote(symbol, option_type, strike, expiry)

    if option is None:
        raise Exception(f"No options found for {symbol} {strike} {option_type} {expiry}")

    print(f"📋 Option found: {option.get('chain_symbol')} strike={option.get('strike_price')} exp={option.get('expiration_date')}")

    if side == "buy":
        current_cost = contracts * ask_price * 100
        # Re-check the cap at execution time too — price may have moved since approval
        if max_cost and current_cost > max_cost * 1.15:  # allow 15% price drift, reject beyond that
            raise Exception(
                f"Price moved too much since approval: now ${current_cost:.2f} "
                f"(was capped at ${max_cost:.2f}). Order not placed for safety."
            )

        order = rh.order_buy_option_limit(
            positionEffect="open",
            creditOrDebit="debit",
            price=ask_price,
            symbol=symbol,
            quantity=contracts,
            expirationDate=expiry,
            strike=strike,
            optionType=option_type,
            timeInForce="gfd",
            account_number=ACCOUNT_NUMBER
        )
    else:
        bid_price = float(option.get("bid_price") or 0)
        if bid_price <= 0:
            raise Exception(f"No valid bid price to sell {symbol} option")

        order = rh.order_sell_option_limit(
            positionEffect="close",
            creditOrDebit="credit",
            price=bid_price,
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
- NEVER include "hold" as a side value — only return "buy" or "sell". If you recommend holding a position, omit that symbol entirely from the recommendations array.

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

# ── Position monitor (tiered alerts, no spam) ────────────────────────────────
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

            if pct_change <= -20:
                tier = "down_20"
            elif pct_change <= -10:
                tier = "down_10"
            elif pct_change >= 25:
                tier = "up_25"
            elif pct_change >= 15:
                tier = "up_15"
            elif pct_change >= 7:
                tier = "up_7"
            else:
                tier = "neutral"

            last_tier_key = f"alert_tier:{symbol}"
            last_tier = redis_client.get(last_tier_key)
            last_tier = last_tier.decode() if last_tier else None

            if tier != "neutral" and tier != last_tier:
                if tier == "down_20":
                    send_notification(
                        f"🔴🔴 MAJOR LOSS — {symbol}",
                        f"{symbol} is down {abs(pct_change):.1f}% from your buy price\n"
                        f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                        f"P&L: ${profit_loss:+.2f}\n\nStrongly consider selling.",
                        priority=1
                    )
                elif tier == "down_10":
                    send_notification(
                        f"🔴 STOP LOSS ALERT — {symbol}",
                        f"{symbol} is down {abs(pct_change):.1f}% from your buy price\n"
                        f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                        f"P&L: ${profit_loss:+.2f}\n\nOpen dashboard to consider selling.",
                        priority=1
                    )
                elif tier == "up_25":
                    send_notification(
                        f"🟢🟢 BIG WIN — {symbol}",
                        f"{symbol} is up {pct_change:.1f}% from your buy price!\n"
                        f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                        f"P&L: ${profit_loss:+.2f}\n\nConsider locking in gains.",
                        priority=1
                    )
                elif tier == "up_15":
                    send_notification(
                        f"🟢 TAKE PROFIT ALERT — {symbol}",
                        f"{symbol} is up {pct_change:.1f}% from your buy price\n"
                        f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                        f"P&L: ${profit_loss:+.2f}\n\nOpen dashboard to consider taking profits.",
                        priority=1
                    )
                elif tier == "up_7":
                    send_notification(
                        f"📈 {symbol} up {pct_change:.1f}%",
                        f"Bought @ ${avg_buy_price:.2f} | Now @ ${current_price:.2f}\n"
                        f"P&L: ${profit_loss:+.2f}",
                        priority=0
                    )

                redis_client.set(last_tier_key, tier, ex=86400 * 2)

            elif tier == "neutral" and last_tier is not None:
                redis_client.delete(last_tier_key)

    except Exception as e:
        print(f"❌ Monitor error: {e}")

# ── Weekly P&L summary ────────────────────────────────────────────────────────
def send_weekly_summary():
    """Compile and send a weekly P&L summary every Friday at close."""
    try:
        rh_login()
        portfolio_value, buying_power = get_portfolio()

        cutoff = time.time() - (7 * 86400)
        history = []
        for key in redis_client.scan_iter("history:*"):
            data = redis_client.get(key)
            if data:
                entry = json.loads(data)
                if entry.get("timestamp", 0) >= cutoff:
                    history.append(entry)

        if not history:
            send_notification(
                "📊 Weekly Summary",
                f"No trades this week.\nCurrent balance: ${portfolio_value:.2f}"
            )
            return

        executed = [h for h in history if h.get("outcome") == "executed"]
        denied   = [h for h in history if h.get("outcome") in ["denied", "expired"]]
        failed   = [h for h in history if h.get("outcome") == "failed"]

        buys  = [h for h in executed if h.get("side") == "buy"]
        sells = [h for h in executed if h.get("side") == "sell"]

        total_bought = sum(h.get("estimated_value", 0) for h in buys)
        total_sold   = sum(h.get("estimated_value", 0) for h in sells)

        starting_balance_raw = redis_client.get("weekly_starting_balance")
        starting_balance = float(starting_balance_raw) if starting_balance_raw else portfolio_value

        pct_change = ((portfolio_value - starting_balance) / starting_balance * 100) if starting_balance > 0 else 0

        symbols_traded = sorted(set(h.get("symbol") for h in executed))

        msg = (
            f"📊 WEEKLY SUMMARY\n\n"
            f"Balance: ${starting_balance:.2f} → ${portfolio_value:.2f} ({pct_change:+.1f}%)\n\n"
            f"Trades: {len(executed)} executed, {len(denied)} denied, {len(failed)} failed\n"
            f"Bought: ${total_bought:.2f} | Sold: ${total_sold:.2f}\n"
            f"Symbols: {', '.join(symbols_traded) if symbols_traded else 'none'}\n\n"
            f"Have a good weekend! 📈"
        )

        send_notification("📊 Weekly Trading Summary", msg, priority=0)
        print(f"📊 Weekly summary sent: {msg}")

        redis_client.set("weekly_starting_balance", str(portfolio_value), ex=86400 * 10)

    except Exception as e:
        print(f"❌ Weekly summary error: {e}")

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

            asset_type  = rec.get("asset_type", "stock")
            option_data = rec.get("option")

            if asset_type == "option" and option_data:
                # ── Real options pricing & sizing ──────────────────────
                option_type = option_data.get("type", "call")
                strike      = option_data.get("strike")
                expiry      = option_data.get("expiry")
                contracts   = option_data.get("contracts", 1)

                ask_price, option_info = get_option_quote(symbol, option_type, strike, expiry)

                if ask_price <= 0:
                    send_notification(
                        "⚠️ Skipped Option",
                        f"{symbol} {strike}{option_type[0].upper()} {expiry}: Could not get a valid quote"
                    )
                    continue

                true_cost = contracts * ask_price * 100
                max_allowed = portfolio_value * 0.20

                if true_cost > max_allowed:
                    max_contracts = int(max_allowed // (ask_price * 100))
                    if max_contracts < 1:
                        send_notification(
                            "⚠️ Skipped Option",
                            f"{symbol} {strike}{option_type[0].upper()} {expiry}: "
                            f"Even 1 contract (${ask_price * 100:.2f}) exceeds 20% cap (${max_allowed:.2f})"
                        )
                        continue
                    print(f"⚠️ Resizing {symbol} option from {contracts} to {max_contracts} contracts to fit 20% cap")
                    contracts = max_contracts
                    true_cost = contracts * ask_price * 100

                if side == "buy" and true_cost > buying_power:
                    send_notification("⚠️ Skipped Option", f"{symbol}: Not enough buying power for ${true_cost:.2f}")
                    continue

                option_data["contracts"] = contracts

                trade = {
                    "symbol":          symbol,
                    "side":            side,
                    "quantity":        contracts,
                    "price":           ask_price,
                    "estimated_value": true_cost,
                    "reason":          reason,
                    "asset_type":      "option",
                    "option":          option_data,
                    "status":          "pending"
                }

            else:
                # ── Stock/ETF/crypto sizing ─────────────────────────────
                price = get_quote(symbol)
                if price <= 0:
                    print(f"⚠️ Could not get price for {symbol}, skipping")
                    continue

                if side == "buy" and trade_value > buying_power:
                    send_notification("⚠️ Skipped Trade", f"{symbol}: Not enough buying power")
                    continue

                quantity = round(trade_value / price, 6)

                trade = {
                    "symbol":          symbol,
                    "side":            side,
                    "quantity":        quantity,
                    "price":           price,
                    "estimated_value": trade_value,
                    "reason":          reason,
                    "asset_type":      asset_type,
                    "option":          None,
                    "status":          "pending"
                }

            trade_id = f"{int(time.time())}_{i}"
            save_pending(trade_id, trade)
            send_approval_request(trade, trade_id)
            print(f"📱 Approval request sent for {side} {symbol}")

            approved = wait_for_approval(trade_id)

            if approved:
                try:
                    if asset_type == "option" and option_data:
                        order = place_option_order(
                            symbol=symbol,
                            option_type=option_data.get("type", "call"),
                            strike=option_data.get("strike"),
                            expiry=option_data.get("expiry"),
                            contracts=option_data.get("contracts", 1),
                            side=side,
                            max_cost=trade.get("estimated_value")
                        )
                        send_notification(
                            "✅ Option Executed",
                            f"{side.upper()} {option_data.get('contracts', 1)}x {symbol} "
                            f"{option_data.get('strike')}{option_data.get('type', 'call')[0].upper()} "
                            f"exp {option_data.get('expiry')}\n"
                            f"Order ID: {order.get('id', 'N/A')}"
                        )
                    else:
                        order = place_order(symbol, side, trade["quantity"], trade["price"])
                        send_notification(
                            "✅ Trade Executed",
                            f"{side.upper()} {trade['quantity']:.4f} {symbol} @ ~${trade['price']:.2f}\n"
                            f"Order ID: {order.get('id', 'N/A')}"
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
    elif scan_type == "weekly_summary":
        send_weekly_summary()
    else:
        run_scan(scan_type)
