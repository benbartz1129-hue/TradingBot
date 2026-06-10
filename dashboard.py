"""
Lightweight approval dashboard.
Accessible via Railway's public URL.
Lets you approve or deny pending trades from your iPhone browser.
"""

from flask import Flask, jsonify, request, render_template_string
import json
import os
import time

app = Flask(__name__)
import redis
redis_client = redis.from_url(os.environ["REDIS_URL"])
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")

def load_pending():
    trades = {}
    for key in redis_client.scan_iter("trade:*"):
        trade_id = key.decode().replace("trade:", "")
        data = redis_client.get(key)
        if data:
            trades[trade_id] = json.loads(data)
    return trades

def save_pending(trade_id, trade):
    redis_client.setex(f"trade:{trade_id}", 86400, json.dumps(trade))

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Bot</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px; background: #f2f2f7; }
    h1 { font-size: 22px; color: #1c1c1e; }
    .card { background: white; border-radius: 12px; padding: 16px; margin: 12px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .symbol { font-size: 24px; font-weight: bold; color: #1c1c1e; }
    .side-buy { color: #34c759; font-weight: bold; }
    .side-sell { color: #ff3b30; font-weight: bold; }
    .detail { color: #8e8e93; font-size: 14px; margin: 4px 0; }
    .reason { color: #3c3c43; font-size: 14px; margin: 10px 0; padding: 10px; background: #f2f2f7; border-radius: 8px; }
    .buttons { display: flex; gap: 10px; margin-top: 14px; }
    button { flex: 1; padding: 14px; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; }
    .approve { background: #34c759; color: white; }
    .deny { background: #ff3b30; color: white; }
    .status-approved { color: #34c759; font-weight: bold; }
    .status-denied { color: #ff3b30; font-weight: bold; }
    .empty { text-align: center; color: #8e8e93; padding: 40px 0; }
    .expired { color: #ff9500; font-size: 12px; }
    input { width: 100%; padding: 12px; border: 1px solid #c6c6c8; border-radius: 10px; font-size: 16px; box-sizing: border-box; margin: 8px 0; }
    .login-card { background: white; border-radius: 12px; padding: 24px; margin-top: 40px; }
  </style>
</head>
<body>
  <h1>🤖 Trading Bot</h1>

  {% if not authenticated %}
  <div class="login-card">
    <p>Enter your dashboard password:</p>
    <form method="POST" action="/login">
      <input type="password" name="password" placeholder="Password" autofocus>
      <button class="approve" type="submit" style="width:100%; margin-top:8px;">Login</button>
    </form>
  </div>

  {% else %}
  {% if trades %}
    {% for trade_id, trade in trades.items() %}
    <div class="card">
      <div class="symbol">{{ trade.symbol }}</div>
      <div>
        <span class="side-{{ trade.side }}">{{ trade.side.upper() }}</span>
        &nbsp;{{ "%.4f"|format(trade.quantity) }} shares @ ~${{ "%.2f"|format(trade.price) }}
      </div>
      <div class="detail">Est. Value: ${{ "%.2f"|format(trade.estimated_value) }}</div>
      <div class="detail">Type: {{ trade.asset_type }}</div>
      <div class="reason">{{ trade.reason }}</div>

      {% if trade.status == "pending" %}
        {% set age = (now - trade.timestamp) / 60 %}
        {% if age > 30 %}
          <div class="expired">⏰ Expired ({{ "%.0f"|format(age) }} min ago)</div>
        {% else %}
          <div class="buttons">
            <button class="approve" onclick="decide('{{ trade_id }}', 'approved')">✅ Approve</button>
            <button class="deny" onclick="decide('{{ trade_id }}', 'denied')">❌ Deny</button>
          </div>
        {% endif %}
      {% elif trade.status == "approved" %}
        <div class="status-approved">✅ Approved & Executed</div>
      {% elif trade.status == "denied" %}
        <div class="status-denied">❌ Denied</div>
      {% endif %}
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">
      <p>📭 No pending trades</p>
      <p>The bot will notify you when opportunities are found.</p>
    </div>
  {% endif %}

  <div class="card" style="text-align:center;">
    <div class="detail">Next scans: 9:30am · 12:00pm · 3:30pm ET</div>
    <div class="detail">Account: ••••8850</div>
  </div>
  {% endif %}

  <script>
  function decide(tradeId, decision) {
    fetch('/decide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({trade_id: tradeId, decision: decision})
    }).then(() => location.reload());
  }
  // Auto-refresh every 30 seconds
  setTimeout(() => location.reload(), 30000);
  </script>
</body>
</html>
"""

# Simple session via cookie
authenticated_ips = set()

@app.route("/login", methods=["POST"])
def login():
    from flask import redirect, make_response
    password = request.form.get("password", "")
    if password == DASHBOARD_PASSWORD:
        resp = make_response(redirect("/"))
        resp.set_cookie("auth", DASHBOARD_PASSWORD, max_age=86400 * 7)
        return resp
    return redirect("/")

@app.route("/")
def dashboard():
    auth_cookie = request.cookies.get("auth", "")
    authenticated = (auth_cookie == DASHBOARD_PASSWORD)
    trades = load_pending() if authenticated else {}
    return render_template_string(
        DASHBOARD_HTML,
        trades=trades,
        authenticated=authenticated,
        now=time.time()
    )

@app.route("/decide", methods=["POST"])
def decide():
    auth_cookie = request.cookies.get("auth", "")
    if auth_cookie != DASHBOARD_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    trade_id = data.get("trade_id")
    decision = data.get("decision")  # "approved" or "denied"

    pending = load_pending()
    if trade_id in pending:
        trade = json.loads(redis_client.get(f"trade:{trade_id}"))
        trade["status"] = decision
        redis_client.setex(f"trade:{trade_id}", 86400, json.dumps(trade))
        return jsonify({"ok": True})
    return jsonify({"error": "trade not found"}), 404

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
