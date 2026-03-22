"""
BIST Haber Analiz Sistemi - Web Dashboard v3.0
===============================================
Canlı BIST durumu, hisse arama, kompakt sinyal kartları,
gerçek zamanlı fiyat takibi.
"""

import threading
import time
import json
import os
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request

from config import FETCH_INTERVAL_MINUTES, BIST_TICKERS, TICKER_NAMES, COMPANY_NAMES
from database import init_db, get_connection, get_unprocessed_news, update_news_sentiment
from news_fetcher import fetch_all_feeds, fetch_kap_notifications
from price_fetcher import fetch_latest_prices, fetch_all_historical_prices
from nlp_engine import process_news_item
from signal_generator import (
    init_signals_table, generate_signal, generate_macro_signal,
    get_active_signals, get_completed_signals, get_signal_success_rate,
    check_signal_results, save_signal, add_business_days
)
from proactive_analyzer import run_proactive_scan

app = Flask(__name__)

worker_status = {
    "running": False,
    "last_check": None,
    "cycle": 0,
    "total_signals": 0,
    "errors": [],
}


# ─── BIST Piyasa Durumu ──────────────────────────────────────

def get_bist_status():
    """BIST piyasasının açık mı kapalı mı olduğunu döndürür."""
    now = datetime.now()
    weekday = now.weekday()  # 0=Pazartesi, 6=Pazar
    hour = now.hour
    minute = now.minute
    current_minutes = hour * 60 + minute

    # BIST saatleri: 10:00 - 18:10 (Pazartesi-Cuma)
    bist_open = 10 * 60       # 10:00
    bist_close = 18 * 60 + 10 # 18:10

    if weekday >= 5:
        return {"open": False, "status": "KAPALI", "reason": "Hafta sonu", "emoji": "🔴"}

    if current_minutes < bist_open:
        mins_left = bist_open - current_minutes
        return {"open": False, "status": "KAPALI", "reason": f"Açılışa {mins_left} dk", "emoji": "🟡"}
    elif current_minutes <= bist_close:
        return {"open": True, "status": "AÇIK", "reason": "İşlem saatleri", "emoji": "🟢"}
    else:
        return {"open": False, "status": "KAPALI", "reason": "Seans bitti", "emoji": "🔴"}


def get_live_prices(search=None, limit=20):
    """Veritabanından en güncel hisse fiyatlarını döndürür."""
    conn = get_connection()

    prices = []
    tickers = BIST_TICKERS
    if search:
        search_lower = search.lower()
        tickers = [t for t in BIST_TICKERS
                   if search_lower in t.lower()
                   or any(search_lower in name for names in COMPANY_NAMES.values()
                          for name in names if t.replace(".IS","") in COMPANY_NAMES)]

    for ticker_yf in tickers[:limit]:
        rows = conn.execute(
            "SELECT close, open, high, low, volume, date FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 2",
            (ticker_yf,)
        ).fetchall()

        if not rows:
            continue

        current = rows[0]
        prev_close = rows[1]["close"] if len(rows) > 1 else current["close"]
        change = current["close"] - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        code = ticker_yf.replace(".IS", "")
        prices.append({
            "ticker": code,
            "price": round(current["close"], 2),
            "open": round(current["open"], 2),
            "high": round(current["high"], 2),
            "low": round(current["low"], 2),
            "volume": current["volume"],
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "date": current["date"],
        })

    conn.close()
    prices.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return prices


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

            fetch_latest_prices()
            check_signal_results()
            fetch_kap_notifications()
            fetch_all_feeds()

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
    <meta name="description" content="BIST haber analizi ve canlı sinyal üretim sistemi">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        :root{
            --bg:#06080f;--bg2:#0d1117;--bg3:#161b22;--bg4:#1c2333;
            --t1:#f0f6fc;--t2:#8b949e;--t3:#484f58;
            --g:#2ea043;--gg:rgba(46,160,67,.15);
            --r:#f85149;--rg:rgba(248,81,73,.15);
            --b:#58a6ff;--p:#bc8cff;--y:#d29922;
            --br:#21262d;--br2:#30363d;
            --radius:10px;
        }
        body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--t1);min-height:100vh}

        /* ── HEADER ── */
        .hdr{background:linear-gradient(135deg,#0d1117,#161b22);border-bottom:1px solid var(--br);padding:14px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
        .hdr h1{font-size:18px;font-weight:800;background:linear-gradient(90deg,#58a6ff,#bc8cff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
        .hdr-right{display:flex;align-items:center;gap:16px;font-size:12px;color:var(--t2)}
        .bist-badge{padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.5px}
        .bist-badge.open{background:var(--gg);color:var(--g);border:1px solid rgba(46,160,67,.3)}
        .bist-badge.closed{background:var(--rg);color:var(--r);border:1px solid rgba(248,81,73,.2)}
        .dot{width:7px;height:7px;border-radius:50%;background:var(--g);animation:blink 2s infinite}
        @keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

        /* ── STATS ── */
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;padding:14px 24px;background:var(--bg2);border-bottom:1px solid var(--br)}
        .st{background:var(--bg3);border:1px solid var(--br);border-radius:var(--radius);padding:12px 14px;text-align:center}
        .st .lb{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
        .st .vl{font-size:22px;font-weight:800}
        .st .vl.g{color:var(--g)}.st .vl.r{color:var(--r)}.st .vl.b{color:var(--b)}.st .vl.p{color:var(--p)}

        /* ── TICKER BAR ── */
        .ticker-section{padding:14px 24px;background:var(--bg2);border-bottom:1px solid var(--br)}
        .ticker-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
        .ticker-header h2{font-size:13px;font-weight:700;color:var(--t2)}
        .search-box{background:var(--bg3);border:1px solid var(--br);border-radius:8px;padding:6px 12px;color:var(--t1);font-size:12px;width:200px;outline:none;font-family:inherit}
        .search-box:focus{border-color:var(--b)}
        .ticker-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px}
        .tk{background:var(--bg3);border:1px solid var(--br);border-radius:8px;padding:10px 12px;cursor:default;transition:all .2s}
        .tk:hover{border-color:var(--br2);transform:translateY(-1px)}
        .tk-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
        .tk-code{font-size:13px;font-weight:800}
        .tk-change{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px}
        .tk-change.up{background:var(--gg);color:var(--g)}
        .tk-change.dn{background:var(--rg);color:var(--r)}
        .tk-price{font-size:16px;font-weight:700}
        .tk-sub{font-size:10px;color:var(--t3)}

        /* ── SIGNALS ── */
        .container{padding:14px 24px}
        .sec-title{font-size:14px;font-weight:700;margin-bottom:10px;color:var(--t2);text-transform:uppercase;letter-spacing:1px}
        .sig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:24px}
        .sig{background:var(--bg3);border:1px solid var(--br);border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden;transition:all .2s}
        .sig::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
        .sig.up::before{background:linear-gradient(90deg,var(--g),#3fb950)}
        .sig.dn::before{background:linear-gradient(90deg,var(--r),#ff7b72)}
        .sig:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.4)}
        .sig-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
        .sig-ticker{font-size:16px;font-weight:800}
        .sig-dir{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;letter-spacing:.5px}
        .sig-dir.up{background:var(--gg);color:var(--g)}
        .sig-dir.dn{background:var(--rg);color:var(--r)}
        .sig-pct{font-size:26px;font-weight:900;text-align:center;margin:6px 0;letter-spacing:-1px}
        .sig-pct.up{color:var(--g)}.sig-pct.dn{color:var(--r)}
        .sig-rows{display:flex;flex-direction:column;gap:4px}
        .sig-row{display:flex;justify-content:space-between;font-size:11px}
        .sig-row .l{color:var(--t3)}.sig-row .v{font-weight:600;color:var(--t2)}
        .sig-row .v.g{color:var(--g)}.sig-row .v.r{color:var(--r)}
        .sig-news{font-size:10px;color:var(--t3);margin-top:8px;padding-top:8px;border-top:1px solid var(--br);line-height:1.4}
        .conf-bar{width:100%;height:3px;background:var(--br);border-radius:2px;margin-top:6px;overflow:hidden}
        .conf-fill{height:100%;border-radius:2px}
        .conf-fill.h{background:var(--g)}.conf-fill.m{background:var(--y)}.conf-fill.l{background:var(--r)}

        /* ── TABLE ── */
        .htable{width:100%;border-collapse:collapse;margin-bottom:24px;font-size:12px}
        .htable th{background:var(--bg3);padding:8px 12px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--t3);border-bottom:1px solid var(--br)}
        .htable td{padding:8px 12px;border-bottom:1px solid var(--br)}
        .htable tr:hover td{background:var(--bg4)}
        .badge{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
        .badge.ok{background:var(--gg);color:var(--g)}.badge.no{background:var(--rg);color:var(--r)}

        .empty{text-align:center;padding:30px;color:var(--t3);font-size:13px}
        .ft{text-align:center;padding:14px;color:var(--t3);font-size:10px;border-top:1px solid var(--br)}

        @media(max-width:768px){
            .hdr,.stats,.ticker-section,.container{padding:10px 14px}
            .sig-grid{grid-template-columns:1fr}
            .ticker-grid{grid-template-columns:repeat(2,1fr)}
            .stats{grid-template-columns:repeat(2,1fr)}
        }
    </style>
</head>
<body>
    <!-- HEADER -->
    <div class="hdr">
        <h1>📡 BIST Sinyal Paneli</h1>
        <div class="hdr-right">
            <span class="bist-badge {{ 'open' if bist.open else 'closed' }}">
                {{ bist.emoji }} BIST {{ bist.status }}
            </span>
            <span style="color:var(--t3)">{{ bist.reason }}</span>
            <span style="color:var(--t3)">|</span>
            <div class="dot"></div>
            <span>Son: {{ last_check or '-' }}</span>
            <span>#{{ cycle }}</span>
        </div>
    </div>

    <!-- STATS -->
    <div class="stats">
        <div class="st"><div class="lb">Aktif Sinyal</div><div class="vl b">{{ active_count }}</div></div>
        <div class="st"><div class="lb">Toplam</div><div class="vl p">{{ total_signals }}</div></div>
        <div class="st"><div class="lb">Başarı</div><div class="vl {{ 'g' if success_rate >= 55 else 'r' }}">%{{ success_rate }}</div></div>
        <div class="st"><div class="lb">Başarılı</div><div class="vl g">{{ success_count }}/{{ completed_count }}</div></div>
        <div class="st"><div class="lb">Takip Edilen</div><div class="vl b">{{ price_count }} Hisse</div></div>
    </div>

    <!-- LIVE TICKER BAR -->
    <div class="ticker-section">
        <div class="ticker-header">
            <h2>📈 Canlı Fiyatlar (Son Kapanış)</h2>
            <input type="text" class="search-box" placeholder="🔍 Hisse ara... (THYAO)" id="searchInput" onkeyup="filterTickers()">
        </div>
        <div class="ticker-grid" id="tickerGrid">
            {% for p in prices %}
            <div class="tk" data-ticker="{{ p.ticker }}">
                <div class="tk-top">
                    <span class="tk-code">{{ p.ticker }}</span>
                    <span class="tk-change {{ 'up' if p.change_pct >= 0 else 'dn' }}">
                        {{ '▲' if p.change_pct >= 0 else '▼' }} %{{ '{:.2f}'.format(p.change_pct|abs) }}
                    </span>
                </div>
                <div class="tk-price">{{ '%.2f'|format(p.price) }} ₺</div>
                <div class="tk-sub">A: {{ '%.2f'|format(p.open) }} | Y: {{ '%.2f'|format(p.high) }} | D: {{ '%.2f'|format(p.low) }}</div>
            </div>
            {% endfor %}
            {% if not prices %}
            <div class="empty">Fiyat verisi yükleniyor...</div>
            {% endif %}
        </div>
    </div>

    <!-- ACTIVE SIGNALS -->
    <div class="container">
        <div class="sec-title">📡 Aktif Sinyaller</div>
        <div class="sig-grid">
            {% for sig in active_signals %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <div class="sig {{ 'up' if is_up else 'dn' }}">
                <div class="sig-top">
                    <span class="sig-ticker">{{ sig.ticker }}</span>
                    <span class="sig-dir {{ 'up' if is_up else 'dn' }}">{{ '▲ YÜKSELİŞ' if is_up else '▼ DÜŞÜŞ' }}</span>
                </div>
                <div class="sig-pct {{ 'up' if is_up else 'dn' }}">%{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}</div>
                <div class="sig-rows">
                    <div class="sig-row"><span class="l">📅 Süre</span><span class="v">{{ sig.start_date }} → {{ sig.end_date }}</span></div>
                    {% if sig.price_at_signal %}
                    <div class="sig-row"><span class="l">💰 Fiyat</span><span class="v">{{ '%.2f'|format(sig.price_at_signal) }} ₺</span></div>
                    <div class="sig-row"><span class="l">🎯 Hedef</span><span class="v {{ 'g' if is_up else 'r' }}">{{ '%.2f'|format(sig.price_at_signal * (1 + (sig.expected_change_pct or 0)/100)) }} ₺</span></div>
                    {% endif %}
                    <div class="sig-row"><span class="l">🛡 Güven</span><span class="v">{{ sig.confidence or '?' }}</span></div>
                    <div class="sig-row"><span class="l">🔒 Stop</span><span class="v">%{{ '%.1f'|format((sig.expected_change_pct or 3)|abs * 0.5) }}</span></div>
                </div>
                {% set conf = sig.confidence_score or 0.3 %}
                <div class="conf-bar"><div class="conf-fill {{ 'h' if conf > 0.65 else 'm' if conf > 0.4 else 'l' }}" style="width:{{ (conf*100)|int }}%"></div></div>
                {% if sig.trigger_news %}
                <div class="sig-news">📰 {{ sig.trigger_news[:100] }}...</div>
                {% endif %}
            </div>
            {% endfor %}
            {% if not active_signals %}
            <div class="empty" style="grid-column:1/-1">ℹ️ Aktif sinyal yok. Sistem analiz ediyor...</div>
            {% endif %}
        </div>

        <!-- HISTORY -->
        {% if completed_signals %}
        <div class="sec-title">📜 Geçmiş</div>
        <table class="htable">
            <thead><tr><th>Hisse</th><th>Yön</th><th>Beklenen</th><th>Gerçek</th><th>Tarih</th><th>Sonuç</th></tr></thead>
            <tbody>
            {% for sig in completed_signals %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <tr>
                <td style="font-weight:700">{{ sig.ticker }}</td>
                <td><span class="sig-dir {{ 'up' if is_up else 'dn' }}" style="font-size:9px;padding:2px 5px">{{ '▲' if is_up else '▼' }}</span></td>
                <td style="color:{{ 'var(--g)' if (sig.expected_change_pct or 0) > 0 else 'var(--r)' }};font-weight:600">%{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}</td>
                <td style="color:{{ 'var(--g)' if (sig.actual_change_pct or 0) > 0 else 'var(--r)' }};font-weight:600">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</td>
                <td style="color:var(--t3)">{{ sig.start_date }} → {{ sig.end_date }}</td>
                <td>{% if 'BAŞARILI' in (sig.result or '') and 'BAŞARISIZ' not in (sig.result or '') %}<span class="badge ok">✅</span>{% else %}<span class="badge no">❌</span>{% endif %}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% endif %}
    </div>

    <div class="ft">BIST Sinyal Paneli v3.0 — Otomatik yenileme: 60sn — ⚠️ Yatırım tavsiyesi değildir</div>

    <script>
    function filterTickers(){
        const q=document.getElementById('searchInput').value.toUpperCase();
        document.querySelectorAll('.tk').forEach(el=>{
            el.style.display=el.dataset.ticker.includes(q)?'':'none';
        });
    }
    </script>
</body>
</html>
"""


# ─── Flask Routes ─────────────────────────────────────────────

@app.route("/")
def dashboard():
    active = get_active_signals()
    completed = get_completed_signals(limit=20)
    stats = get_signal_success_rate()
    prices = get_live_prices(limit=50)
    bist = get_bist_status()

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
        prices=prices,
        price_count=len(BIST_TICKERS),
        bist=bist,
    )

@app.route("/api/signals")
def api_signals():
    return jsonify(get_active_signals())

@app.route("/api/prices")
def api_prices():
    search = request.args.get("q", None)
    return jsonify(get_live_prices(search=search, limit=50))

@app.route("/api/history")
def api_history():
    return jsonify(get_completed_signals())

@app.route("/api/status")
def api_status():
    bist = get_bist_status()
    return jsonify({**worker_status, "bist": bist})


# ─── Başlatma ─────────────────────────────────────────────────

def _initialize():
    print("  ⚙️  Veritabanı hazırlanıyor...")
    init_db()
    init_signals_table()

    # Eski spam sinyalleri temizle
    conn = get_connection()
    conn.execute("DELETE FROM signals")
    conn.commit()
    conn.close()
    print("  🧹 Eski sinyaller temizlendi.")

    print("  [1/3] Fiyat verileri çekiliyor...")
    fetch_all_historical_prices()
    print("  [2/3] Haberler toplanıyor...")
    fetch_kap_notifications()
    fetch_all_feeds()
    print("  [3/3] Proaktif tarama yapılıyor...")
    tech_signals = run_proactive_scan()
    for sig in tech_signals[:5]:
        save_signal(sig)
        worker_status["total_signals"] += 1

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

    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    print("  ✅ Worker başlatıldı!")


_initialized = False
if not _initialized:
    _initialized = True
    _initialize()


def start_web_app(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    print(f"\n  🌐 http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    start_web_app()
