"""
BIST Haber Analiz Sistemi - Web Dashboard
==========================================
Flask tabanlı web arayüzü. Arka planda sürekli çalışan worker thread ile
haberleri çeker, analiz eder ve sinyal üretir.

Çalıştırma: python web_app.py
Tarayıcıdan: http://localhost:5000
"""

import threading
import time
import json
from datetime import datetime
from flask import Flask, render_template_string, jsonify

from config import FETCH_INTERVAL_MINUTES, BIST_TICKERS
from database import init_db, get_connection, get_unprocessed_news, update_news_sentiment
from news_fetcher import fetch_all_feeds, fetch_kap_notifications
from price_fetcher import fetch_latest_prices, fetch_all_historical_prices
from nlp_engine import process_news_item
from signal_generator import (
    init_signals_table, generate_signal, generate_macro_signal,
    get_active_signals, get_completed_signals, get_signal_success_rate,
    check_signal_results, save_signal
)
from proactive_analyzer import run_proactive_scan

app = Flask(__name__)

# Arka plan worker durumu
worker_status = {
    "running": False,
    "last_check": None,
    "cycle": 0,
    "total_signals": 0,
    "errors": [],
}


# ─── Arka Plan Worker ────────────────────────────────────────

def background_worker():
    """Sürekli çalışan arka plan işçisi."""
    global worker_status
    worker_status["running"] = True

    while worker_status["running"]:
        try:
            worker_status["cycle"] += 1
            worker_status["last_check"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            print(f"\n[WORKER] Döngü #{worker_status['cycle']} - {worker_status['last_check']}")

            # 1. Fiyatları güncelle
            fetch_latest_prices()

            # 2. Eski sinyalleri kontrol et
            check_signal_results()

            # 3. Haberleri çek
            fetch_kap_notifications()
            fetch_all_feeds()

            # 4. Haberleri analiz et ve sinyal üret
            unprocessed = get_unprocessed_news()
            for news in unprocessed:
                result = process_news_item(news)
                tickers_json = json.dumps(result["tickers"])
                macro_json = json.dumps(result["macro_keywords"]) if result["macro_keywords"] else None

                update_news_sentiment(
                    news_id=result["news_id"],
                    sentiment_score=result["sentiment_score"],
                    sentiment_label=result["sentiment_label"],
                    related_tickers=tickers_json,
                    is_macro=result["is_macro"],
                    macro_keywords=macro_json
                )

                if result["sentiment_label"] == "neutral" and not result["is_macro"]:
                    continue

                targets = result["tickers"]
                if result["is_macro"] and not targets:
                    sigs = generate_macro_signal(result["sentiment_score"], result["sentiment_label"], news["title"])
                    worker_status["total_signals"] += len(sigs)
                else:
                    for tc in targets:
                        sig = generate_signal(tc, result["sentiment_score"], result["sentiment_label"], news["title"])
                        if sig:
                            worker_status["total_signals"] += 1

            # 5. Proaktif teknik analiz (her 6 döngüde bir)
            if worker_status["cycle"] % 6 == 1:
                tech_signals = run_proactive_scan()
                for sig in tech_signals[:5]:
                    save_signal(sig)
                    worker_status["total_signals"] += 1

            print(f"[WORKER] Döngü tamamlandı. Toplam sinyal: {worker_status['total_signals']}")

        except Exception as e:
            error_msg = f"{datetime.now().strftime('%H:%M:%S')} - {str(e)}"
            worker_status["errors"].append(error_msg)
            if len(worker_status["errors"]) > 20:
                worker_status["errors"] = worker_status["errors"][-20:]
            print(f"[WORKER HATA] {e}")

        time.sleep(FETCH_INTERVAL_MINUTES * 60)


# ─── HTML Template ────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>BIST Sinyal Paneli</title>
    <meta name="description" content="Borsa İstanbul haber analizi ve sinyal üretim sistemi">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg-primary: #0a0e17;
            --bg-secondary: #111827;
            --bg-card: #1a2235;
            --bg-card-hover: #1f2a40;
            --text-primary: #e2e8f0;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --green: #10b981;
            --green-glow: rgba(16, 185, 129, 0.3);
            --red: #ef4444;
            --red-glow: rgba(239, 68, 68, 0.3);
            --blue: #3b82f6;
            --purple: #8b5cf6;
            --yellow: #f59e0b;
            --border: #1e293b;
            --border-light: #334155;
        }

        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }

        .header {
            background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #0f172a 100%);
            border-bottom: 1px solid var(--border);
            padding: 20px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header h1 {
            font-size: 24px;
            font-weight: 800;
            background: linear-gradient(135deg, #60a5fa, #a78bfa, #f472b6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .header .status {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 13px;
            color: var(--text-secondary);
        }

        .header .status .dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 10px var(--green-glow);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .stats-bar {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            padding: 24px 40px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
        }

        .stat-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px 20px;
            transition: all 0.3s ease;
        }

        .stat-card:hover {
            border-color: var(--border-light);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(0,0,0,0.3);
        }

        .stat-card .label {
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }

        .stat-card .value {
            font-size: 28px;
            font-weight: 800;
        }

        .stat-card .value.green { color: var(--green); }
        .stat-card .value.red { color: var(--red); }
        .stat-card .value.blue { color: var(--blue); }
        .stat-card .value.purple { color: var(--purple); }

        .container { padding: 24px 40px; }

        .section-title {
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .signals-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 16px;
            margin-bottom: 40px;
        }

        .signal-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 24px;
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }

        .signal-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
        }

        .signal-card.up::before { background: linear-gradient(90deg, var(--green), #34d399); }
        .signal-card.down::before { background: linear-gradient(90deg, var(--red), #f87171); }

        .signal-card:hover {
            border-color: var(--border-light);
            transform: translateY(-3px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.4);
        }

        .signal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .signal-ticker {
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.5px;
        }

        .signal-direction {
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }

        .signal-direction.up {
            background: rgba(16, 185, 129, 0.15);
            color: var(--green);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .signal-direction.down {
            background: rgba(239, 68, 68, 0.15);
            color: var(--red);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }

        .signal-body { display: flex; flex-direction: column; gap: 10px; }

        .signal-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
        }

        .signal-row .label { color: var(--text-muted); }
        .signal-row .value { font-weight: 600; }
        .signal-row .value.green { color: var(--green); }
        .signal-row .value.red { color: var(--red); }

        .signal-change {
            font-size: 32px;
            font-weight: 900;
            text-align: center;
            margin: 12px 0;
            letter-spacing: -1px;
        }

        .signal-change.up { color: var(--green); text-shadow: 0 0 20px var(--green-glow); }
        .signal-change.down { color: var(--red); text-shadow: 0 0 20px var(--red-glow); }

        .signal-news {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid var(--border);
            line-height: 1.5;
        }

        .confidence-bar {
            width: 100%;
            height: 4px;
            background: var(--border);
            border-radius: 2px;
            margin-top: 8px;
            overflow: hidden;
        }

        .confidence-fill {
            height: 100%;
            border-radius: 2px;
            transition: width 0.5s ease;
        }

        .confidence-fill.high { background: var(--green); }
        .confidence-fill.mid { background: var(--yellow); }
        .confidence-fill.low { background: var(--red); }

        .history-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin-bottom: 40px;
        }

        .history-table th {
            background: var(--bg-card);
            padding: 12px 16px;
            text-align: left;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
        }

        .history-table td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            font-size: 14px;
        }

        .history-table tr:hover td {
            background: var(--bg-card);
        }

        .badge {
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }

        .badge.success { background: rgba(16, 185, 129, 0.15); color: var(--green); }
        .badge.fail { background: rgba(239, 68, 68, 0.15); color: var(--red); }
        .badge.active { background: rgba(59, 130, 246, 0.15); color: var(--blue); }

        .footer {
            text-align: center;
            padding: 20px;
            color: var(--text-muted);
            font-size: 12px;
            border-top: 1px solid var(--border);
        }

        @media (max-width: 768px) {
            .header, .stats-bar, .container { padding: 16px; }
            .signals-grid { grid-template-columns: 1fr; }
            .stats-bar { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📡 BIST Sinyal Paneli</h1>
        <div class="status">
            <div class="dot"></div>
            <span>Canlı İzleme Aktif</span>
            <span>|</span>
            <span>Son kontrol: {{ last_check or 'Henüz yok' }}</span>
            <span>|</span>
            <span>Döngü #{{ cycle }}</span>
        </div>
    </div>

    <div class="stats-bar">
        <div class="stat-card">
            <div class="label">Aktif Sinyaller</div>
            <div class="value blue">{{ active_count }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Toplam Üretilen</div>
            <div class="value purple">{{ total_signals }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Başarı Oranı</div>
            <div class="value {{ 'green' if success_rate >= 55 else 'red' }}">%{{ success_rate }}</div>
        </div>
        <div class="stat-card">
            <div class="label">Başarılı / Toplam</div>
            <div class="value green">{{ success_count }} / {{ completed_count }}</div>
        </div>
    </div>

    <div class="container">
        <div class="section-title">📡 Aktif Sinyaller</div>
        <div class="signals-grid">
            {% for sig in active_signals %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <div class="signal-card {{ 'up' if is_up else 'down' }}">
                <div class="signal-header">
                    <div class="signal-ticker">{{ sig.ticker }}</div>
                    <div class="signal-direction {{ 'up' if is_up else 'down' }}">
                        {{ '▲ YÜKSELİŞ' if is_up else '▼ DÜŞÜŞ' }}
                    </div>
                </div>

                <div class="signal-change {{ 'up' if is_up else 'down' }}">
                    %{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}
                </div>

                <div class="signal-body">
                    <div class="signal-row">
                        <span class="label">📅 Tarih</span>
                        <span class="value">{{ sig.start_date }} → {{ sig.end_date }}</span>
                    </div>
                    {% if sig.price_at_signal %}
                    <div class="signal-row">
                        <span class="label">💰 Sinyal Fiyatı</span>
                        <span class="value">{{ '%.2f'|format(sig.price_at_signal) }} TL</span>
                    </div>
                    <div class="signal-row">
                        <span class="label">🎯 Hedef Fiyat</span>
                        <span class="value {{ 'green' if is_up else 'red' }}">
                            {{ '%.2f'|format(sig.price_at_signal * (1 + (sig.expected_change_pct or 0)/100)) }} TL
                        </span>
                    </div>
                    {% endif %}
                    <div class="signal-row">
                        <span class="label">🛡️ Güvenilirlik</span>
                        <span class="value">{{ sig.confidence or '?' }}</span>
                    </div>
                    <div class="signal-row">
                        <span class="label">🔒 Stop-Loss</span>
                        <span class="value">%{{ '%.1f'|format((sig.expected_change_pct or 3)|abs * 0.5) }}</span>
                    </div>
                </div>

                {% set conf = sig.confidence_score or 0.3 %}
                <div class="confidence-bar">
                    <div class="confidence-fill {{ 'high' if conf > 0.65 else 'mid' if conf > 0.4 else 'low' }}"
                         style="width: {{ (conf * 100)|int }}%"></div>
                </div>

                {% if sig.trigger_news %}
                <div class="signal-news">📰 {{ sig.trigger_news[:120] }}...</div>
                {% endif %}
            </div>
            {% endfor %}

            {% if not active_signals %}
            <div class="signal-card" style="text-align:center; padding:40px;">
                <p style="color: var(--text-muted); font-size: 16px;">
                    ℹ️ Henüz aktif sinyal yok. Sistem haberleri analiz ediyor...
                </p>
            </div>
            {% endif %}
        </div>

        {% if completed_signals %}
        <div class="section-title">📜 Sinyal Geçmişi</div>
        <table class="history-table">
            <thead>
                <tr>
                    <th>Hisse</th>
                    <th>Yön</th>
                    <th>Beklenen</th>
                    <th>Gerçekleşen</th>
                    <th>Tarih</th>
                    <th>Sonuç</th>
                </tr>
            </thead>
            <tbody>
                {% for sig in completed_signals %}
                {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
                <tr>
                    <td style="font-weight:700;">{{ sig.ticker }}</td>
                    <td>
                        <span class="signal-direction {{ 'up' if is_up else 'down' }}" style="font-size:11px;padding:3px 8px;">
                            {{ '▲' if is_up else '▼' }}
                        </span>
                    </td>
                    <td class="{{ 'green' if (sig.expected_change_pct or 0) > 0 else 'red' }}" style="font-weight:600;">
                        %{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}
                    </td>
                    <td class="{{ 'green' if (sig.actual_change_pct or 0) > 0 else 'red' }}" style="font-weight:600;">
                        %{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}
                    </td>
                    <td style="color:var(--text-muted);">{{ sig.start_date }} → {{ sig.end_date }}</td>
                    <td>
                        {% if 'BAŞARILI' in (sig.result or '') and 'BAŞARISIZ' not in (sig.result or '') %}
                        <span class="badge success">✅ Başarılı</span>
                        {% else %}
                        <span class="badge fail">❌ Başarısız</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% endif %}
    </div>

    <div class="footer">
        BIST Sinyal Paneli v2.0 — Sayfa her 60 saniyede otomatik yenilenir — 
        ⚠️ Bu sistem yatırım tavsiyesi değildir, sadece analiz amaçlıdır.
    </div>
</body>
</html>
"""


# ─── Flask Route'lar ──────────────────────────────────────────

@app.route("/")
def dashboard():
    """Ana dashboard sayfası."""
    active = get_active_signals()
    completed = get_completed_signals(limit=20)
    stats = get_signal_success_rate()

    return render_template_string(
        DASHBOARD_HTML,
        active_signals=active,
        completed_signals=completed,
        active_count=len(active),
        total_signals=worker_status["total_signals"],
        success_rate=stats.get("rate", 0),
        success_count=stats.get("success", 0),
        completed_count=stats.get("total", 0),
        last_check=worker_status["last_check"],
        cycle=worker_status["cycle"],
    )


@app.route("/api/signals")
def api_signals():
    """API: Aktif sinyalleri JSON olarak döndürür."""
    return jsonify(get_active_signals())


@app.route("/api/history")
def api_history():
    """API: Sinyal geçmişini JSON olarak döndürür."""
    return jsonify(get_completed_signals())


@app.route("/api/status")
def api_status():
    """API: Worker durumunu döndürür."""
    return jsonify(worker_status)


# ─── Ana Giriş ────────────────────────────────────────────────

import os

def _initialize():
    """Uygulama başlatıldığında veritabanı ve ilk verileri hazırlar."""
    print("  ⚙️  Veritabanı hazırlanıyor...")
    init_db()
    init_signals_table()

    print("  [1/3] Fiyat verileri çekiliyor...")
    fetch_all_historical_prices()
    print("  [2/3] Haberler toplanıyor...")
    fetch_kap_notifications()
    fetch_all_feeds()
    print("  [3/3] Proaktif tarama yapılıyor...")
    tech_signals = run_proactive_scan()
    for sig in tech_signals[:10]:
        save_signal(sig)
        worker_status["total_signals"] += 1

    # Haberleri işle
    unprocessed = get_unprocessed_news()
    for news in unprocessed:
        result = process_news_item(news)
        tickers_json = json.dumps(result["tickers"])
        macro_json = json.dumps(result["macro_keywords"]) if result["macro_keywords"] else None
        update_news_sentiment(
            news_id=result["news_id"],
            sentiment_score=result["sentiment_score"],
            sentiment_label=result["sentiment_label"],
            related_tickers=tickers_json,
            is_macro=result["is_macro"],
            macro_keywords=macro_json
        )
        if result["sentiment_label"] != "neutral" or result["is_macro"]:
            for tc in result["tickers"]:
                sig = generate_signal(tc, result["sentiment_score"], result["sentiment_label"], news["title"])
                if sig:
                    worker_status["total_signals"] += 1
            if result["is_macro"] and not result["tickers"]:
                sigs = generate_macro_signal(result["sentiment_score"], result["sentiment_label"], news["title"])
                worker_status["total_signals"] += len(sigs)

    worker_status["last_check"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    # Arka plan worker'ı başlat
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    print("  ✅ Worker başlatıldı!")


# Gunicorn ile çalışırken otomatik başlat
_initialized = False
if not _initialized:
    _initialized = True
    _initialize()


def start_web_app(host="0.0.0.0", port=None):
    """Web uygulamasını başlatır."""
    if port is None:
        port = int(os.environ.get("PORT", 5000))

    print(f"\n  🌐 BIST Sinyal Web Paneli")
    print(f"  📡 http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    start_web_app()
