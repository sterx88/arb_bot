#!/usr/bin/env python3
"""
Polymarket Arbitrage Monitor — исправленная и улучшенная версия
"""

import json
import time
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests
from flask import Flask, render_template_string, jsonify

# ──────────────────────────────────────────────
# Конфиг
# ──────────────────────────────────────────────
TOTAL_FEE         = 0.009   # 0.9% комиссия
PROFIT_THRESHOLD  = 0.2     # минимальная прибыль для арбитража (%)
MARKETS_LIMIT     = 50      # сколько рынков тянуть
SCAN_INTERVAL     = 20      # секунд между сканами
REQUEST_TIMEOUT   = 8       # секунд на один запрос
MAX_HISTORY       = 100     # записей в истории

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Общие данные (защита через lock)
_lock                = threading.Lock()
current_opportunities: list = []
arbitrage_history:    list = []

# ──────────────────────────────────────────────
# HTTP-сессия с retry
# ──────────────────────────────────────────────
session = requests.Session()
session.headers.update({"User-Agent": "PolymarketMonitor/2.0"})

def _get(url: str, params: Optional[Dict] = None, retries: int = 3) -> Optional[Any]:
    """GET с retry и таймаутом."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.warning("HTTP %s для %s (попытка %d/%d)", e.response.status_code, url, attempt, retries)
        except requests.exceptions.RequestException as e:
            log.warning("Ошибка запроса %s: %s (попытка %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(1.5 * attempt)
    return None

# ──────────────────────────────────────────────
# Получение цены токена
# ──────────────────────────────────────────────
def get_token_price(token_id: str) -> float:
    """
    Пробует midpoint, затем лучший ask из книги заявок.
    Возвращает 0.0 при неудаче.
    """
    # 1) midpoint
    data = _get("https://clob.polymarket.com/midpoint", {"token_id": token_id})
    if data and "mid" in data:
        try:
            price = float(data["mid"])
            if 0 < price < 1:
                return price
        except (ValueError, TypeError):
            pass

    # 2) лучший ask из книги
    data = _get("https://clob.polymarket.com/book", {"token_id": token_id})
    if data:
        asks = data.get("asks") or []
        if asks:
            try:
                price = float(asks[0]["price"])
                if 0 < price < 1:
                    return price
            except (ValueError, TypeError, KeyError):
                pass

    return 0.0

# ──────────────────────────────────────────────
# Получение рынков
# ──────────────────────────────────────────────
def fetch_markets() -> List[Dict]:
    """Загружает бинарные рынки с Gamma API."""
    log.info("Загрузка списка рынков...")
    data = _get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": MARKETS_LIMIT, "active": "true", "closed": "false"},
    )
    if not data:
        log.error("Не удалось получить список рынков")
        return []

    markets = data if isinstance(data, list) else data.get("markets", [])
    log.info("Получено %d рынков от API", len(markets))
    return markets

def parse_token_ids(market: Dict) -> List[str]:
    """Извлекает clob_token_ids независимо от формата (список или JSON-строка)."""
    raw = market.get("clob_token_ids") or market.get("clobTokenIds")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw) if isinstance(raw, (list, tuple)) else []

def parse_outcomes(market: dict) -> tuple[str, str]:
    """Возвращает имена двух исходов."""
    outcomes = market.get("outcomes") or []
    
    # outcomes может быть JSON-строкой
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = []

    def name(o) -> str:
        if isinstance(o, dict):
            return o.get("name") or o.get("title") or "?"
        return str(o)

    o1 = name(outcomes[0]) if len(outcomes) > 0 else "YES"
    o2 = name(outcomes[1]) if len(outcomes) > 1 else "NO"
    return o1, o2

def enrich_market(market: Dict) -> Optional[Dict]:
    """
    Добавляет цены к одному рынку.
    Возвращает None, если рынок не бинарный или цены недоступны.
    """
    token_ids = parse_token_ids(market)
    if len(token_ids) != 2:
        return None

    price1 = get_token_price(token_ids[0])
    price2 = get_token_price(token_ids[1])

    if price1 <= 0 or price2 <= 0:
        return None

    o1, o2 = parse_outcomes(market)
    name = market.get("question") or market.get("title") or market.get("slug") or "Unknown"

    return {
        "name":     name,
        "outcome1": o1,
        "outcome2": o2,
        "price1":   price1,
        "price2":   price2,
    }

# ──────────────────────────────────────────────
# Анализ арбитража
# ──────────────────────────────────────────────
def analyze(market: dict) -> dict:
    p1, p2 = market["price1"], market["price2"]
    total_with_fee = p1 + p2 + TOTAL_FEE
    profit_pct     = (1 - total_with_fee) * 100
    is_arb         = profit_pct > PROFIT_THRESHOLD

    return {
        "market_name":        market["name"],
        "outcome1_name":      market["outcome1"],
        "outcome2_name":      market["outcome2"],
        "outcome1_price":     p1,
        "outcome2_price":     p2,
        "real_profit_percent": profit_pct,
        "is_arbitrage":       is_arb,
    }

# ──────────────────────────────────────────────
# Фоновый мониторинг
# ──────────────────────────────────────────────
def monitor_loop():
    global current_opportunities, arbitrage_history

    log.info("Мониторинг запущен")

    while True:
        log.info("=" * 55)
        log.info("Сканирование %s", datetime.now().strftime("%H:%M:%S"))
        log.info("=" * 55)

        markets_raw = fetch_markets()
        if not markets_raw:
            log.warning("Нет данных, пауза %d сек", SCAN_INTERVAL)
            time.sleep(SCAN_INTERVAL)
            continue

        enriched: List[Dict] = []
        for market in markets_raw:
            result = enrich_market(market)
            if result:
                enriched.append(result)

        log.info("Рынков с ценами: %d / %d", len(enriched), len(markets_raw))

        opportunities = [analyze(m) for m in enriched]
        new_history   = []

        for opp in opportunities:
            if not opp["is_arbitrage"]:
                continue

            # Уникальность: не дублируем в историю
            with _lock:
                already = any(h["market_name"] == opp["market_name"] for h in arbitrage_history[:10])

            if not already:
                entry = {
                    "timestamp":          datetime.now().strftime("%H:%M:%S"),
                    "market_name":        opp["market_name"],
                    "outcome1_name":      opp["outcome1_name"],
                    "outcome2_name":      opp["outcome2_name"],
                    "outcome1_price":     opp["outcome1_price"],
                    "outcome2_price":     opp["outcome2_price"],
                    "real_profit_percent": opp["real_profit_percent"],
                }
                new_history.append(entry)
                log.info(
                    "🔔 АРБИТРАЖ: %s | +%.2f%%",
                    opp["market_name"][:60],
                    opp["real_profit_percent"],
                )

        with _lock:
            current_opportunities = opportunities
            arbitrage_history     = (new_history + arbitrage_history)[:MAX_HISTORY]

        arb_count = sum(1 for o in opportunities if o["is_arbitrage"])
        log.info("Арбитражей найдено: %d", arb_count)
        log.info("Пауза %d сек...", SCAN_INTERVAL)
        time.sleep(SCAN_INTERVAL)

# ──────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Arbitrage Monitor</title>
    <meta http-equiv="refresh" content="20">
    <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0d0f14;
            --surface: #151821;
            --border: #1e2330;
            --accent: #00e5ff;
            --green: #00e676;
            --red: #ff1744;
            --gold: #ffd740;
            --muted: #4a5568;
            --text: #e2e8f0;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: 'Inter', sans-serif;
            font-size: 14px;
            padding: 24px;
        }
        .mono { font-family: 'Space Mono', monospace; }

        /* Header */
        .header {
            display: flex;
            align-items: center;
            gap: 16px;
            margin-bottom: 28px;
        }
        .header h1 {
            font-family: 'Space Mono', monospace;
            font-size: 22px;
            color: var(--accent);
            letter-spacing: -0.5px;
        }
        .pulse {
            width: 10px; height: 10px;
            background: var(--green);
            border-radius: 50%;
            animation: pulse 1.5s infinite;
            flex-shrink: 0;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50%       { opacity: 0.4; transform: scale(1.4); }
        }

        /* Stats */
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 28px;
        }
        .stat {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px 20px;
        }
        .stat-value {
            font-family: 'Space Mono', monospace;
            font-size: 28px;
            font-weight: 700;
            color: var(--accent);
            line-height: 1;
        }
        .stat-value.green { color: var(--green); }
        .stat-label { color: var(--muted); font-size: 12px; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.8px; }

        /* Table */
        .section-title {
            font-family: 'Space Mono', monospace;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: var(--muted);
            margin-bottom: 12px;
        }
        .table-wrap {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
            margin-bottom: 28px;
        }
        .scroll-x { overflow-x: auto; max-height: 540px; overflow-y: auto; }
        table { width: 100%; border-collapse: collapse; }
        th {
            background: #1a1d27;
            padding: 10px 14px;
            text-align: left;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--muted);
            position: sticky;
            top: 0;
            white-space: nowrap;
        }
        td {
            padding: 10px 14px;
            border-top: 1px solid var(--border);
            white-space: nowrap;
        }
        tr.arb { background: rgba(0,230,118,0.04); }
        tr:hover { background: rgba(255,255,255,0.02); }

        .price {
            font-family: 'Space Mono', monospace;
            font-size: 13px;
        }
        .total-normal { color: var(--muted); }
        .total-arb    { color: var(--green); font-weight: 700; }

        .profit-pos { color: var(--green); font-family: 'Space Mono', monospace; }
        .profit-neg { color: var(--muted);  font-family: 'Space Mono', monospace; }

        .badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 20px;
            font-size: 11px;
            font-family: 'Space Mono', monospace;
            letter-spacing: 0.5px;
        }
        .badge-arb  { background: rgba(0,230,118,0.15); color: var(--green); border: 1px solid var(--green); }
        .badge-norm { background: transparent; color: var(--muted); border: 1px solid var(--muted); }

        .market-name {
            max-width: 360px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: var(--text);
        }

        /* History */
        .history-list { display: flex; flex-direction: column; gap: 8px; }
        .history-item {
            background: var(--surface);
            border: 1px solid var(--border);
            border-left: 3px solid var(--green);
            border-radius: 8px;
            padding: 12px 16px;
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }
        .hist-time { font-family: 'Space Mono', monospace; color: var(--accent); font-size: 12px; flex-shrink: 0; }
        .hist-name { color: var(--text); flex: 1; min-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .hist-profit { font-family: 'Space Mono', monospace; color: var(--green); font-weight: 700; flex-shrink: 0; }

        .empty { color: var(--muted); text-align: center; padding: 32px; font-size: 13px; }
        .footer { color: var(--muted); font-size: 11px; text-align: center; margin-top: 20px; letter-spacing: 0.5px; }
    </style>
</head>
<body>
    <div class="header">
        <div class="pulse"></div>
        <h1>Polymarket / Arbitrage Monitor</h1>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-value mono">{{ active_markets }}</div>
            <div class="stat-label">Рынков загружено</div>
        </div>
        <div class="stat">
            <div class="stat-value mono green">{{ current_arbitrage_count }}</div>
            <div class="stat-label">Арбитражей сейчас</div>
        </div>
        <div class="stat">
            <div class="stat-value mono">{{ daily_arbitrage_count }}</div>
            <div class="stat-label">Найдено всего</div>
        </div>
        <div class="stat">
            <div class="stat-value mono" style="font-size:18px;">{{ last_scan }}</div>
            <div class="stat-label">Последнее сканирование</div>
        </div>
    </div>

    <p class="section-title">Текущие рынки</p>
    <div class="table-wrap">
        <div class="scroll-x">
        {% if opportunities %}
        <table>
            <thead>
                <tr>
                    <th>Событие</th>
                    <th>Исход 1</th>
                    <th>Цена 1</th>
                    <th>Исход 2</th>
                    <th>Цена 2</th>
                    <th>Сумма (с комиссией)</th>
                    <th>Прибыль</th>
                    <th>Статус</th>
                </tr>
            </thead>
            <tbody>
            {% for opp in opportunities %}
            <tr class="{% if opp.is_arbitrage %}arb{% endif %}">
                <td><div class="market-name" title="{{ opp.market_name }}">{{ opp.market_name[:80] }}</div></td>
                <td style="color:var(--muted);">{{ opp.outcome1_name[:20] }}</td>
                <td><span class="price">{{ "%.2f"|format(opp.outcome1_price * 100) }}¢</span></td>
                <td style="color:var(--muted);">{{ opp.outcome2_name[:20] }}</td>
                <td><span class="price">{{ "%.2f"|format(opp.outcome2_price * 100) }}¢</span></td>
                <td>
                    {% set total_fee = (opp.outcome1_price + opp.outcome2_price + 0.009) * 100 %}
                    <span class="price {% if opp.is_arbitrage %}total-arb{% else %}total-normal{% endif %}">
                        {{ "%.2f"|format(total_fee) }}¢
                    </span>
                </td>
                <td>
                    <span class="{% if opp.real_profit_percent > 0 %}profit-pos{% else %}profit-neg{% endif %}">
                        {% if opp.real_profit_percent > 0 %}+{% endif %}{{ "%.2f"|format(opp.real_profit_percent) }}%
                    </span>
                </td>
                <td>
                    {% if opp.is_arbitrage %}
                        <span class="badge badge-arb">▲ АРБИТРАЖ</span>
                    {% else %}
                        <span class="badge badge-norm">обычный</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty">Ожидание первого сканирования...</div>
        {% endif %}
        </div>
    </div>

    {% if history %}
    <p class="section-title">История арбитражей</p>
    <div class="history-list">
        {% for item in history %}
        <div class="history-item">
            <span class="hist-time">{{ item.timestamp }}</span>
            <span class="hist-name" title="{{ item.market_name }}">{{ item.market_name[:90] }}</span>
            <span style="color:var(--muted); font-size:12px;">
                {{ item.outcome1_name }}: {{ "%.0f"|format(item.outcome1_price * 100) }}¢ &nbsp;
                {{ item.outcome2_name }}: {{ "%.0f"|format(item.outcome2_price * 100) }}¢
            </span>
            <span class="hist-profit">+{{ "%.2f"|format(item.real_profit_percent) }}%</span>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    <div class="footer">
        КОМИССИЯ {{ (0.009 * 100)|round(1) }}% &nbsp;|&nbsp; ПОРОГ +{{ "%.1f"|format(0.2) }}% &nbsp;|&nbsp; ОБНОВЛЕНИЕ {{ 20 }}с
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    with _lock:
        opps    = list(current_opportunities)
        history = list(arbitrage_history)

    return render_template_string(
        HTML_TEMPLATE,
        opportunities          = opps,
        history                = history,
        active_markets         = len(opps),
        current_arbitrage_count= sum(1 for o in opps if o["is_arbitrage"]),
        daily_arbitrage_count  = len(history),
        last_scan              = datetime.now().strftime("%H:%M:%S"),
    )

@app.route("/api/opportunities")
def api_opportunities():
    with _lock:
        return jsonify(current_opportunities)

@app.route("/api/history")
def api_history():
    with _lock:
        return jsonify(arbitrage_history)

@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify({
            "markets":    len(current_opportunities),
            "arbitrages": sum(1 for o in current_opportunities if o["is_arbitrage"]),
            "history":    len(arbitrage_history),
        })

# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("""
╔═══════════════════════════════════════════════════╗
║   Polymarket Arbitrage Monitor  v2.0              ║
╠═══════════════════════════════════════════════════╣
║  🌐  http://localhost:5001                        ║
║  🔄  Обновление каждые 20 секунд                  ║
║  📊  API: /api/opportunities  /api/history        ║
╚═══════════════════════════════════════════════════╝
""")
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    import os
port = int(os.environ.get("PORT", 5001))
app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
