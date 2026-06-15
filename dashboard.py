from flask import Flask, jsonify, request, render_template_string
import json
import os
import time
import redis

app = Flask(__name__)
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

def load_history():
    history = []
    for key in redis_client.scan_iter("history:*"):
        data = redis_client.get(key)
        if data:
            history.append(json.loads(data))
    return sorted(history, key=lambda x: x.get("timestamp", 0), reverse=True)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Bot</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, sans-serif; background: #f2f2f7; padding: 16px; }
    h1 { font-size: 22px; color: #1c1c1e; margin-bottom: 16px; }
    .tabs { display: flex; background: #e5e5ea; border-radius: 10px; padding: 2px; margin-bottom: 16px; }
    .tab { flex: 1; text-align: center; padding: 8px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; color: #8e8e93; }
    .tab.active { background: white; color: #1c1c1e; }
    .card { background: white; border-radius: 12px; padding: 16px; margin: 10px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .symbol { font-size: 22px; font-weight: bold; color: #1c1c1e; }
    .side-buy { color: #34c759; font-weight: bold; }
    .side-sell { color: #ff3b30; font-weight: bold; }
    .detail { color: #8e8e93; font-size: 13px; margin: 3px 0; }
    .reason { color: #3c3c43; font-size: 13px; margin: 10px 0; padding: 10px; background: #f2f2f7; border-radius: 8px; }
    .buttons { display: flex; gap: 10px; margin-top: 14px; }
    button { flex: 1; padding: 14px; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; }
    .approve { background: #34c759; color: white; }
    .deny { background: #ff3b30; color: white; }
    .badge { display: inline-block; padding: 3px 8px; border-radius: 20px; font-size: 12px; font-weight: 600; }
    .badge-executed { background: #d4edda; color: #155724; }
    .badge-denied { background: #f8d7da; color: #721c24; }
    .badge-expired { background: #fff3cd; color: #856404; }
    .badge-failed { background: #f8d7da; color: #721c24; }
    .badge-pending { background: #cce5ff; color: #004085; }
    .empty { text-align: center; color: #8e8e93; padding: 40px 0; }
    .status-approved { color: #34c759; font-weight: bold; }
    .status-denied { color: #ff3b30; font-weight: bold; }
    .stats { display: flex; gap: 10px; margin-bottom: 16px; }
    .stat { flex: 1; background: white; border-radius: 12px; padding: 12px; text-align: center; }
    .stat-value { font-size: 20px; font-weight: bold; color: #1c1c1e; }
    .stat-label { font-size: 11px; color: #8e8e93; margin-top: 2px; }
    .win { color: #34c759; }
    .loss { color: #ff3b30; }
    input { width: 100%; padding: 12px; border: 1px solid #c6c6c8; border-radius: 10px; font-size: 16px; margin: 8px 0; }
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

  <div class="tabs">
    <div class="tab {% if tab == 'pending' %}active{% endif %}" onclick="switchTab('pending')">
      Pending {% if pending_count > 0 %}({{ pending_count }}){% endif %}
    </div>
    <div class="tab {% if tab == 'history' %}active{% endif %}" onclick="switchTab('history')">
      History
    </div>
  </div>

  {% if tab == 'pending' %}
    {% if trades %}
      {% for trade_id, trade in trades.items() %}
      {% if trade.status == 'pending' %}
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div class="symbol">{{ trade.symbol }}</div>
          <span class="badge badge-pending">PENDING</span>
        </div>
        <div style="margin-top:6px;">
          <span class="side-{{ trade.side }}">{{ trade.side.upper() }}</span>
          &nbsp;{{ "%.4f"|format(trade.quantity) }} shares @ ~${{ "%.2f"|format(trade.price) }}
        </div>
        <div class="detail">Est. Value: ${{ "%.2f"|format(trade.estimated_value) }}</div>
        <div class="detail">Type: {{ trade.asset_type }}</div>
        <div class="reason">{{ trade.reason }}</div>
        <div class="buttons">
          <button class="approve" onclick="decide('{{ trade_id }}', 'approved')">✅ Approve</button>
          <button class="deny" onclick="decide('{{ trade_id }}', 'denied')">❌ Deny</button>
        </div>
      </div>
      {% elif trade.status == 'approved' %}
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div class="symbol">{{ trade.symbol }}</div>
          <span class="badge badge-executed">APPROVED</span>
        </div>
        <div class="detail">{{ trade.side.upper() }} {{ "%.4f"|format(trade.quantity) }} shares</div>
        <div class="status-approved" style="margin-top:8px;">✅ Executing...</div>
      </div>
      {% elif trade.status == 'denied' %}
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div class="symbol">{{ trade.symbol }}</div>
          <span class="badge badge-denied">DENIED</span>
        </div>
        <div class="detail">{{ trade.side.upper() }} {{ "%.4f"|format(trade.quantity) }} shares</div>
      </div>
      {% endif %}
      {% endfor %}
    {% else %}
      <div class="empty">
        <p>📭 No pending trades</p>
        <p style="margin-top:8px; font-size:13px;">Next scans: 8:30am · 11:00am · 2:30pm CT</p>
      </div>
    {% endif %}

  {% elif tab == 'history' %}
    {% if history %}
      <div class="stats">
        <div class="stat">
          <div class="stat-value">{{ history|length }}</div>
          <div class="stat-label">Total Trades</div>
        </div>
        <div class="stat">
          <div class="stat-value win">{{ executed_count }}</div>
          <div class="stat-label">Executed</div>
        </div>
        <div class="stat">
          <div class="stat-value loss">{{ denied_count }}</div>
          <div class="stat-label">Denied</div>
        </div>
      </div>

      {% for entry in history %}
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div class="symbol">{{ entry.symbol }}</div>
          <span class="badge badge-{{ entry.outcome }}">{{ entry.outcome.upper() }}</span>
        </div>
        <div style="margin-top:6px;">
          <span class="side-{{ entry.side }}">{{ entry.side.upper() }}</span>
          {% if entry.quantity %}
          &nbsp;{{ "%.4f"|format(entry.quantity) }} shares @ ~${{ "%.2f"|format(entry.price) }}
          {% endif %}
        </div>
        <div class="detail">Est. Value: ${{ "%.2f"|format(entry.estimated_value) }}</div>
        <div class="detail">{{ entry.date }}</div>
        <div class="reason">{{ entry.reason }}</div>
        {% if entry.order_id %}
        <div class="detail" style="margin-top:6px;">Order ID: {{ entry.order_id }}</div>
        {% endif %}
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">
        <p>📋 No trade history yet</p>
        <p style="margin-top:8px; font-size:13px;">Completed trades will appear here</p>
      </div>
    {% endif %}
  {% endif %}

  <div class="card" style="text-align:center; margin-top:20px;">
    <div class="detail">Account: ••••8850 | $500 starting balance</div>
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
  function switchTab(tab) {
    window.location.href = '/?tab=' + tab;
  }
  setTimeout(() => location.reload(), 30000);
  </script>
</body>
</html>
"""

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
    tab = request.args.get("tab", "pending")

    trades = {}
    history = []
    pending_count = 0
    executed_count = 0
    denied_count = 0

    if authenticated:
        trades = load_pending()
        history = load_history()
        pending_count = sum(1 for t in trades.values() if t.get("status") == "pending")
        executed_count = sum(1 for h in history if h.get("outcome") == "executed")
        denied_count = sum(1 for h in history if h.get("outcome") in ["denied", "expired"])

    return render_template_string(
        DASHBOARD_HTML,
        trades=trades,
        history=history,
        authenticated=authenticated,
        tab=tab,
        now=time.time(),
        pending_count=pending_count,
        executed_count=executed_count,
        denied_count=denied_count
    )

@app.route("/decide", methods=["POST"])
def decide():
    auth_cookie = request.cookies.get("auth", "")
    if auth_cookie != DASHBOARD_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    trade_id = data.get("trade_id")
    decision = data.get("decision")

    raw = redis_client.get(f"trade:{trade_id}")
    if raw:
        trade = json.loads(raw)
        trade["status"] = decision
        redis_client.set(f"trade:{trade_id}", json.dumps(trade), ex=86400)
        return jsonify({"ok": True})
    return jsonify({"error": "trade not found"}), 404

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
