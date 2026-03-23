"""
BIST Haber Analiz Sistemi - Web Dashboard v4.0
===============================================
Canlı BIST durumu, hisse arama, kompakt sinyal kartları,
gerçek zamanlı fiyat takibi, stop/kazanılan işlem takibi,
backtest öğrenme paneli.
"""

import threading
import time
import json
import os
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, jsonify, request

from config import FETCH_INTERVAL_MINUTES, BIST_TICKERS, TICKER_NAMES, COMPANY_NAMES
from database import init_db, get_connection, get_unprocessed_news, update_news_sentiment
from news_fetcher import fetch_all_feeds, fetch_kap_notifications
from price_fetcher import fetch_latest_prices, fetch_all_historical_prices
from nlp_engine import process_news_item
from signal_generator import (
    init_signals_table, generate_signal, generate_macro_signal,
    get_active_signals, get_completed_signals, get_signal_success_rate,
    check_signal_results, save_signal, add_business_days,
    get_stopped_signals, get_won_signals, get_backtest_summary,
    run_backtest_learning
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

# Türkiye saat dilimi (UTC+3)
TZ_TURKEY = timezone(timedelta(hours=3))

# ─── BIST Piyasa Durumu ──────────────────────────────────────

def get_bist_status():
    """BIST piyasasının açık mı kapalı mı olduğunu döndürür."""
    # Türkiye saatini kullan (sunucu UTC'de olsa bile)
    now = datetime.now(TZ_TURKEY)
    weekday = now.weekday()  # 0=Pazartesi, 6=Pazar
    hour = now.hour
    minute = now.minute
    current_minutes = hour * 60 + minute

    # BIST saatleri:
    # Sürekli İşlem: 09:40 - 18:10 (tek seans)
    bist_open = 9 * 60 + 40     # 09:40
    bist_close = 18 * 60 + 10   # 18:10

    if weekday >= 5:
        if weekday == 5:  # Cumartesi
            next_open = 2  # Pazartesi
        else:  # Pazar
            next_open = 1
        return {"open": False, "status": "KAPALI", "reason": "Hafta sonu", "emoji": "🔴", "next_open_days": next_open}

    if current_minutes < bist_open:
        mins_left = bist_open - current_minutes
        hrs = mins_left // 60
        mins = mins_left % 60
        if hrs > 0:
            reason = f"Açılışa {hrs} saat {mins} dk"
        else:
            reason = f"Açılışa {mins} dk"
        return {"open": False, "status": "KAPALI", "reason": reason, "emoji": "🟡"}
    elif current_minutes <= bist_close:
        mins_left = bist_close - current_minutes
        hrs = mins_left // 60
        mins = mins_left % 60
        return {"open": True, "status": "AÇIK", "reason": f"Kapanışa {hrs}s {mins}dk", "emoji": "🟢"}
    else:
        return {"open": False, "status": "KAPALI", "reason": "Seans bitti", "emoji": "🔴"}


def get_live_prices(search=None, limit=50):
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
            worker_status["last_check"] = datetime.now(TZ_TURKEY).strftime("%d.%m.%Y %H:%M:%S")
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
            error_msg = f"{datetime.now(TZ_TURKEY).strftime('%H:%M:%S')} - {str(e)}"
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
    <meta http-equiv="refresh" content="30">
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
            --b:#58a6ff;--p:#bc8cff;--y:#d29922;--o:#f0883e;
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
        .server-time{font-size:11px;color:var(--t3);font-weight:500}

        /* ── NAV TABS ── */
        .nav-tabs{display:flex;gap:4px;padding:8px 24px;background:var(--bg2);border-bottom:1px solid var(--br);overflow-x:auto}
        .nav-tab{padding:8px 16px;border-radius:8px;font-size:12px;font-weight:600;color:var(--t2);text-decoration:none;transition:all .2s;white-space:nowrap;border:1px solid transparent}
        .nav-tab:hover{background:var(--bg3);color:var(--t1)}
        .nav-tab.active{background:var(--bg4);color:var(--b);border-color:var(--b)}
        .nav-tab .count{background:var(--bg4);padding:1px 6px;border-radius:10px;font-size:10px;margin-left:4px}
        .nav-tab.stop-tab .count{background:var(--rg);color:var(--r)}
        .nav-tab.won-tab .count{background:var(--gg);color:var(--g)}

        /* ── STATS ── */
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;padding:14px 24px;background:var(--bg2);border-bottom:1px solid var(--br)}
        .st{background:var(--bg3);border:1px solid var(--br);border-radius:var(--radius);padding:12px 14px;text-align:center}
        .st .lb{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
        .st .vl{font-size:22px;font-weight:800}
        .st .vl.g{color:var(--g)}.st .vl.r{color:var(--r)}.st .vl.b{color:var(--b)}.st .vl.p{color:var(--p)}.st .vl.o{color:var(--o)}.st .vl.y{color:var(--y)}

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
        .tk-date{font-size:9px;color:var(--t3);margin-top:2px}

        /* ── SIGNALS ── */
        .container{padding:14px 24px}
        .sec-title{font-size:14px;font-weight:700;margin-bottom:10px;color:var(--t2);text-transform:uppercase;letter-spacing:1px}
        .sig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:24px}
        .sig{background:var(--bg3);border:1px solid var(--br);border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden;transition:all .2s}
        .sig::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
        .sig.up::before{background:linear-gradient(90deg,var(--g),#3fb950)}
        .sig.dn::before{background:linear-gradient(90deg,var(--r),#ff7b72)}
        .sig.stop::before{background:linear-gradient(90deg,var(--o),#f0883e)}
        .sig:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.4)}
        .sig-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
        .sig-ticker{font-size:16px;font-weight:800}
        .sig-dir{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;letter-spacing:.5px}
        .sig-dir.up{background:var(--gg);color:var(--g)}
        .sig-dir.dn{background:var(--rg);color:var(--r)}
        .sig-dir.stop{background:rgba(240,136,62,.15);color:var(--o)}
        .sig-pct{font-size:26px;font-weight:900;text-align:center;margin:6px 0;letter-spacing:-1px}
        .sig-pct.up{color:var(--g)}.sig-pct.dn{color:var(--r)}
        .sig-rows{display:flex;flex-direction:column;gap:4px}
        .sig-row{display:flex;justify-content:space-between;font-size:11px}
        .sig-row .l{color:var(--t3)}.sig-row .v{font-weight:600;color:var(--t2)}
        .sig-row .v.g{color:var(--g)}.sig-row .v.r{color:var(--r)}.sig-row .v.o{color:var(--o)}
        .sig-news{font-size:10px;color:var(--t3);margin-top:8px;padding-top:8px;border-top:1px solid var(--br);line-height:1.4}
        .conf-bar{width:100%;height:3px;background:var(--br);border-radius:2px;margin-top:6px;overflow:hidden}
        .conf-fill{height:100%;border-radius:2px}
        .conf-fill.h{background:var(--g)}.conf-fill.m{background:var(--y)}.conf-fill.l{background:var(--r)}

        /* ── STATUS BADGES ── */
        .status-badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:700}
        .status-badge.won{background:var(--gg);color:var(--g)}
        .status-badge.stop{background:rgba(240,136,62,.15);color:var(--o)}
        .status-badge.fail{background:var(--rg);color:var(--r)}

        /* ── TABLE ── */
        .htable{width:100%;border-collapse:collapse;margin-bottom:24px;font-size:12px}
        .htable th{background:var(--bg3);padding:8px 12px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--t3);border-bottom:1px solid var(--br)}
        .htable td{padding:8px 12px;border-bottom:1px solid var(--br)}
        .htable tr:hover td{background:var(--bg4)}
        .badge{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
        .badge.ok{background:var(--gg);color:var(--g)}.badge.no{background:var(--rg);color:var(--r)}.badge.st{background:rgba(240,136,62,.15);color:var(--o)}

        /* ── BACKTEST PANEL ── */
        .bt-panel{background:linear-gradient(135deg,var(--bg3),var(--bg4));border:1px solid var(--br);border-radius:var(--radius);padding:20px;margin-bottom:24px}
        .bt-title{font-size:16px;font-weight:800;margin-bottom:12px;color:var(--p)}
        .bt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
        .bt-item{background:var(--bg2);border:1px solid var(--br);border-radius:8px;padding:12px;text-align:center}
        .bt-item .bt-label{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
        .bt-item .bt-value{font-size:18px;font-weight:800}
        .bt-notes{margin-top:12px;font-size:11px;color:var(--t3);padding:10px;background:var(--bg2);border-radius:8px;line-height:1.6}

        .empty{text-align:center;padding:30px;color:var(--t3);font-size:13px}
        .ft{text-align:center;padding:14px;color:var(--t3);font-size:10px;border-top:1px solid var(--br)}

        @media(max-width:768px){
            .hdr,.stats,.ticker-section,.container,.nav-tabs{padding:10px 14px}
            .sig-grid{grid-template-columns:1fr}
            .ticker-grid{grid-template-columns:repeat(2,1fr)}
            .stats{grid-template-columns:repeat(2,1fr)}
        }
    </style>
</head>
<body>
    <!-- HEADER -->
    <div class="hdr">
        <h1>📡 BIST Sinyal Paneli v4.0</h1>
        <div class="hdr-right">
            <span class="bist-badge {{ 'open' if bist.open else 'closed' }}">
                {{ bist.emoji }} BIST {{ bist.status }}
            </span>
            <span style="color:var(--t2)">{{ bist.reason }}</span>
            <span style="color:var(--t3)">|</span>
            <span class="server-time">🕐 {{ turkey_time }}</span>
            <span style="color:var(--t3)">|</span>
            <div class="dot"></div>
            <span>Son: {{ last_check or '-' }}</span>
            <span>#{{ cycle }}</span>
        </div>
    </div>

    <!-- NAV TABS -->
    <div class="nav-tabs">
        <a href="/" class="nav-tab {{ 'active' if page == 'main' else '' }}">📊 Ana Panel</a>
        <a href="/stopped" class="nav-tab stop-tab {{ 'active' if page == 'stopped' else '' }}">🛑 Stop Olan<span class="count">{{ stopped_count }}</span></a>
        <a href="/won" class="nav-tab won-tab {{ 'active' if page == 'won' else '' }}">✅ Kazanılan<span class="count">{{ won_count }}</span></a>
        <a href="/backtest" class="nav-tab {{ 'active' if page == 'backtest' else '' }}">🧠 Backtest</a>
        <a href="/history" class="nav-tab {{ 'active' if page == 'history' else '' }}">📜 Geçmiş</a>
    </div>

    <!-- STATS -->
    <div class="stats">
        <div class="st"><div class="lb">Aktif Sinyal</div><div class="vl b">{{ active_count }}</div></div>
        <div class="st"><div class="lb">Kazanılan</div><div class="vl g">{{ won_count }}</div></div>
        <div class="st"><div class="lb">Stop Olan</div><div class="vl o">{{ stopped_count }}</div></div>
        <div class="st"><div class="lb">Başarı</div><div class="vl {{ 'g' if success_rate >= 55 else 'r' }}">%{{ success_rate }}</div></div>
        <div class="st"><div class="lb">Toplam</div><div class="vl p">{{ total_signals }}</div></div>
        <div class="st"><div class="lb">Takip Edilen</div><div class="vl b">{{ price_count }} Hisse</div></div>
    </div>

    {% if page == 'main' %}
    <!-- LIVE TICKER BAR -->
    <div class="ticker-section">
        <div class="ticker-header">
            <h2>📈 Canlı Fiyatlar {% if bist.open %}(Canlı){% else %}(Son Kapanış){% endif %}</h2>
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
                <div class="tk-date">{{ p.date }}</div>
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
                    <div class="sig-row"><span class="l">🛑 Stop</span><span class="v o">{{ '%.2f'|format(sig.stop_price or 0) }} ₺ (%{{ '%.1f'|format(sig.stop_loss_pct or 0) }})</span></div>
                    {% endif %}
                    <div class="sig-row"><span class="l">🛡 Güven</span><span class="v">{{ sig.confidence or '?' }}</span></div>
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
    </div>

    {% elif page == 'stopped' %}
    <!-- STOPPED SIGNALS PAGE -->
    <div class="container">
        <div class="sec-title">🛑 Stop Olan İşlemler (Zarar Kesilen)</div>
        {% if stopped_signals %}
        <div class="sig-grid">
            {% for sig in stopped_signals %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <div class="sig stop">
                <div class="sig-top">
                    <span class="sig-ticker">{{ sig.ticker }}</span>
                    <span class="status-badge stop">🛑 STOP OLDU</span>
                </div>
                <div class="sig-pct dn">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</div>
                <div class="sig-rows">
                    <div class="sig-row"><span class="l">📅 Tarih</span><span class="v">{{ sig.start_date }} → {{ sig.end_date }}</span></div>
                    <div class="sig-row"><span class="l">📊 Beklenen</span><span class="v {{ 'g' if (sig.expected_change_pct or 0) > 0 else 'r' }}">%{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}</span></div>
                    <div class="sig-row"><span class="l">📉 Gerçekleşen</span><span class="v r">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</span></div>
                    {% if sig.price_at_signal %}
                    <div class="sig-row"><span class="l">💰 Giriş Fiyat</span><span class="v">{{ '%.2f'|format(sig.price_at_signal) }} ₺</span></div>
                    {% endif %}
                    {% if sig.price_at_end %}
                    <div class="sig-row"><span class="l">💸 Çıkış Fiyat</span><span class="v r">{{ '%.2f'|format(sig.price_at_end) }} ₺</span></div>
                    {% endif %}
                    <div class="sig-row"><span class="l">🛑 Stop Level</span><span class="v o">%{{ '%.1f'|format(sig.stop_loss_pct or 0) }}</span></div>
                    <div class="sig-row"><span class="l">📐 Yön</span><span class="v">{{ '▲ YÜKSELİŞ' if is_up else '▼ DÜŞÜŞ' }}</span></div>
                </div>
                {% if sig.trigger_news %}
                <div class="sig-news">📰 {{ sig.trigger_news[:120] }}...</div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty">✨ Henüz stop olan işlem yok. Çok iyi!</div>
        {% endif %}
    </div>

    {% elif page == 'won' %}
    <!-- WON SIGNALS PAGE -->
    <div class="container">
        <div class="sec-title">✅ Kazanılan İşlemler</div>
        {% if won_signals %}
        <div class="sig-grid">
            {% for sig in won_signals %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <div class="sig up">
                <div class="sig-top">
                    <span class="sig-ticker">{{ sig.ticker }}</span>
                    <span class="status-badge won">✅ KAZANDI</span>
                </div>
                <div class="sig-pct up">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</div>
                <div class="sig-rows">
                    <div class="sig-row"><span class="l">📅 Tarih</span><span class="v">{{ sig.start_date }} → {{ sig.end_date }}</span></div>
                    <div class="sig-row"><span class="l">📊 Beklenen</span><span class="v {{ 'g' if (sig.expected_change_pct or 0) > 0 else 'r' }}">%{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}</span></div>
                    <div class="sig-row"><span class="l">📈 Gerçekleşen</span><span class="v g">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</span></div>
                    {% if sig.price_at_signal %}
                    <div class="sig-row"><span class="l">💰 Giriş Fiyat</span><span class="v">{{ '%.2f'|format(sig.price_at_signal) }} ₺</span></div>
                    {% endif %}
                    {% if sig.price_at_end %}
                    <div class="sig-row"><span class="l">💵 Çıkış Fiyat</span><span class="v g">{{ '%.2f'|format(sig.price_at_end) }} ₺</span></div>
                    {% endif %}
                    <div class="sig-row"><span class="l">📐 Yön</span><span class="v">{{ '▲ YÜKSELİŞ' if is_up else '▼ DÜŞÜŞ' }}</span></div>
                    <div class="sig-row"><span class="l">🛡 Güven</span><span class="v">{{ sig.confidence or '?' }}</span></div>
                </div>
                {% if sig.trigger_news %}
                <div class="sig-news">📰 {{ sig.trigger_news[:120] }}...</div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty">⏳ Henüz kazanılan işlem yok. Sinyaller bekleniyor...</div>
        {% endif %}
    </div>

    {% elif page == 'backtest' %}
    <!-- BACKTEST PANEL -->
    <div class="container">
        <div class="bt-panel">
            <div class="bt-title">🧠 Backtest Öğrenme Sistemi</div>
            <p style="font-size:12px;color:var(--t2);margin-bottom:16px">
                Algoritma, geçmiş sinyal sonuçlarına göre kendini otomatik olarak ayarlar.
                Stop olan ve başarısız sinyallerden öğrenir, gelecek sinyalleri optimize eder.
            </p>
            <div class="bt-grid">
                <div class="bt-item">
                    <div class="bt-label">Toplam Analiz</div>
                    <div class="bt-value" style="color:var(--b)">{{ backtest.total_analyzed }}</div>
                </div>
                <div class="bt-item">
                    <div class="bt-label">Win Rate</div>
                    <div class="bt-value" style="color:{{ 'var(--g)' if backtest.win_rate >= 55 else 'var(--r)' }}">%{{ backtest.win_rate }}</div>
                </div>
                <div class="bt-item">
                    <div class="bt-label">Güven Çarpanı</div>
                    <div class="bt-value" style="color:var(--p)">x{{ backtest.confidence_multiplier }}</div>
                </div>
                <div class="bt-item">
                    <div class="bt-label">Stop-Loss Çarpanı</div>
                    <div class="bt-value" style="color:var(--o)">x{{ backtest.stop_loss_multiplier }}</div>
                </div>
                <div class="bt-item">
                    <div class="bt-label">Beklenti Çarpanı</div>
                    <div class="bt-value" style="color:var(--y)">x{{ backtest.expected_change_multiplier }}</div>
                </div>
                <div class="bt-item">
                    <div class="bt-label">Min. Eşik</div>
                    <div class="bt-value" style="color:var(--t1)">%{{ backtest.min_score_threshold }}</div>
                </div>
            </div>
            <div class="bt-notes">
                <strong>📋 Öğrenme Notları:</strong><br>
                {% if backtest.avoid_tickers %}
                🚫 <strong>Kaçınılan Hisseler:</strong> {{ backtest.avoid_tickers|join(', ') }} (sürekli kaybettiren)<br>
                {% endif %}
                {% if backtest.favor_tickers %}
                ⭐ <strong>Favori Hisseler:</strong> {{ backtest.favor_tickers|join(', ') }} (sürekli kazandıran)<br>
                {% endif %}
                📅 <strong>Son Backtest:</strong> {{ backtest.last_backtest or 'Henüz çalışmadı' }}<br>
                💡 <strong>Strateji:</strong>
                {% if backtest.win_rate >= 70 %}
                Yüksek başarı oranı → Daha agresif sinyal üretimi, sıkı stop-loss
                {% elif backtest.win_rate >= 55 %}
                Dengeli başarı → Normal parametreler
                {% elif backtest.win_rate >= 40 %}
                Düşük başarı → Daha seçici sinyal üretimi, geniş stop-loss
                {% else %}
                Çok düşük başarı → Sadece çok güçlü sinyaller, geniş stop-loss
                {% endif %}
            </div>
        </div>

        <!-- BACKTEST GEÇMİŞİ -->
        {% if backtest_history %}
        <div class="sec-title">📊 Backtest Geçmişi</div>
        <table class="htable">
            <thead><tr><th>Tarih</th><th>Toplam</th><th>Kazanılan</th><th>Stop</th><th>Win Rate</th><th>Ort. Kazanç</th><th>Ort. Kayıp</th><th>En İyi</th><th>En Kötü</th></tr></thead>
            <tbody>
            {% for bt in backtest_history %}
            <tr>
                <td style="color:var(--t3)">{{ bt.created_at }}</td>
                <td style="font-weight:700">{{ bt.total_signals }}</td>
                <td style="color:var(--g);font-weight:600">{{ bt.won_signals }}</td>
                <td style="color:var(--o);font-weight:600">{{ bt.stopped_signals }}</td>
                <td style="color:{{ 'var(--g)' if (bt.win_rate or 0) >= 55 else 'var(--r)' }};font-weight:700">%{{ '%.1f'|format(bt.win_rate or 0) }}</td>
                <td style="color:var(--g)">%{{ '%.2f'|format(bt.avg_win_pct or 0) }}</td>
                <td style="color:var(--r)">%{{ '%.2f'|format(bt.avg_loss_pct or 0) }}</td>
                <td style="color:var(--g);font-weight:600">{{ bt.best_ticker or '-' }}</td>
                <td style="color:var(--r);font-weight:600">{{ bt.worst_ticker or '-' }}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% endif %}
    </div>

    {% elif page == 'history' %}
    <!-- FULL HISTORY -->
    <div class="container">
        <div class="sec-title">📜 Tüm Geçmiş İşlemler</div>
        {% if all_history %}
        <table class="htable">
            <thead><tr><th>Hisse</th><th>Yön</th><th>Beklenen</th><th>Gerçek</th><th>Tarih</th><th>Stop</th><th>Sonuç</th></tr></thead>
            <tbody>
            {% for sig in all_history %}
            {% set is_up = 'YÜKSELİŞ' in (sig.direction or '') %}
            <tr>
                <td style="font-weight:700">{{ sig.ticker }}</td>
                <td><span class="sig-dir {{ 'up' if is_up else 'dn' }}" style="font-size:9px;padding:2px 5px">{{ '▲' if is_up else '▼' }}</span></td>
                <td style="color:{{ 'var(--g)' if (sig.expected_change_pct or 0) > 0 else 'var(--r)' }};font-weight:600">%{{ '{:+.2f}'.format(sig.expected_change_pct or 0) }}</td>
                <td style="color:{{ 'var(--g)' if (sig.actual_change_pct or 0) > 0 else 'var(--r)' }};font-weight:600">%{{ '{:+.2f}'.format(sig.actual_change_pct or 0) }}</td>
                <td style="color:var(--t3)">{{ sig.start_date }} → {{ sig.end_date }}</td>
                <td style="color:var(--o)">%{{ '%.1f'|format(sig.stop_loss_pct or 0) }}</td>
                <td>
                    {% if sig.status == 'KAZANDI' %}
                    <span class="badge ok">✅ Kazandı</span>
                    {% elif sig.status == 'STOP' %}
                    <span class="badge st">🛑 Stop</span>
                    {% else %}
                    <span class="badge no">❌ Başarısız</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty">📭 Henüz tamamlanmış işlem bulunmuyor.</div>
        {% endif %}
    </div>
    {% endif %}

    <div class="ft">BIST Sinyal Paneli v4.0 — Otomatik yenileme: 30sn — 🧠 Backtest aktif — ⚠️ Yatırım tavsiyesi değildir</div>

    <script>
    function filterTickers(){
        const q=document.getElementById('searchInput').value.toUpperCase();
        document.querySelectorAll('.tk').forEach(el=>{
            el.style.display=el.dataset.ticker.includes(q)?'':'none';
        });
    }

    // Auto refresh prices with AJAX (live)
    {% if bist.open %}
    setInterval(function(){
        fetch('/api/prices')
            .then(r=>r.json())
            .then(data=>{
                data.forEach(p=>{
                    const el=document.querySelector(`.tk[data-ticker="${p.ticker}"]`);
                    if(el){
                        el.querySelector('.tk-price').textContent=p.price.toFixed(2)+' ₺';
                        const ch=el.querySelector('.tk-change');
                        ch.textContent=(p.change_pct>=0?'▲':'▼')+' %'+Math.abs(p.change_pct).toFixed(2);
                        ch.className='tk-change '+(p.change_pct>=0?'up':'dn');
                    }
                });
            }).catch(()=>{});
    }, 15000);
    {% endif %}
    </script>
</body>
</html>
"""


# ─── Flask Routes ─────────────────────────────────────────────

def _get_common_context():
    """Tüm sayfalar için ortak context verileri."""
    stats = get_signal_success_rate()
    bist = get_bist_status()
    stopped = get_stopped_signals()
    won = get_won_signals()
    backtest = get_backtest_summary()
    turkey_time = datetime.now(TZ_TURKEY).strftime("%H:%M:%S")

    return {
        "active_count": len(get_active_signals()),
        "total_signals": worker_status["total_signals"],
        "success_rate": stats.get("rate", 0),
        "won_count": len(won),
        "stopped_count": len(stopped),
        "last_check": worker_status["last_check"],
        "cycle": worker_status["cycle"],
        "price_count": len(BIST_TICKERS),
        "bist": bist,
        "backtest": backtest,
        "turkey_time": turkey_time,
    }


@app.route("/")
def dashboard():
    ctx = _get_common_context()
    active = get_active_signals()
    prices = get_live_prices(limit=50)

    return render_template_string(
        DASHBOARD_HTML,
        page="main",
        active_signals=active,
        prices=prices,
        **ctx,
    )


@app.route("/stopped")
def stopped_page():
    ctx = _get_common_context()
    stopped = get_stopped_signals(limit=50)

    return render_template_string(
        DASHBOARD_HTML,
        page="stopped",
        stopped_signals=stopped,
        **ctx,
    )


@app.route("/won")
def won_page():
    ctx = _get_common_context()
    won = get_won_signals(limit=50)

    return render_template_string(
        DASHBOARD_HTML,
        page="won",
        won_signals=won,
        **ctx,
    )


@app.route("/backtest")
def backtest_page():
    ctx = _get_common_context()

    # Backtest geçmişi
    conn = get_connection()
    bt_rows = conn.execute("SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    backtest_history = [dict(r) for r in bt_rows] if bt_rows else []

    return render_template_string(
        DASHBOARD_HTML,
        page="backtest",
        backtest_history=backtest_history,
        **ctx,
    )


@app.route("/history")
def history_page():
    ctx = _get_common_context()

    # Tüm tamamlanan sinyaller
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status IN ('TAMAMLANDI', 'KAZANDI', 'STOP')
        ORDER BY created_at DESC LIMIT 100
    """).fetchall()
    conn.close()
    all_history = [dict(r) for r in rows]

    return render_template_string(
        DASHBOARD_HTML,
        page="history",
        all_history=all_history,
        **ctx,
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

@app.route("/api/stopped")
def api_stopped():
    return jsonify(get_stopped_signals())

@app.route("/api/won")
def api_won():
    return jsonify(get_won_signals())

@app.route("/api/backtest")
def api_backtest():
    return jsonify(get_backtest_summary())

@app.route("/api/status")
def api_status():
    bist = get_bist_status()
    return jsonify({**worker_status, "bist": bist, "backtest": get_backtest_summary()})


# ─── Başlatma ─────────────────────────────────────────────────

def _heavy_init():
    """Ağır işlemleri arka planda yapar (fiyat çekme, haber toplama)."""
    try:
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

        worker_status["last_check"] = datetime.now(TZ_TURKEY).strftime("%d.%m.%Y %H:%M:%S")
        print("  ✅ Veri çekme tamamlandı!")
    except Exception as e:
        print(f"  [HATA] Init hatası: {e}")

    # Sonra sürekli çalışan worker'a geç
    background_worker()


# Sadece veritabanını oluştur (hızlı), ağır işleri arka plana at
print("  ⚙️  Veritabanı hazırlanıyor...")
init_db()
init_signals_table()

# Eski sinyalleri temizle
conn = get_connection()
conn.execute("DELETE FROM signals")
conn.commit()
conn.close()
print("  🧹 Sinyaller temizlendi.")

# Ağır işleri arka plan thread'de başlat (gunicorn'u BLOKE ETMEZ)
init_thread = threading.Thread(target=_heavy_init, daemon=True)
init_thread.start()
print("  🚀 Arka plan worker başlatıldı, web sunucu hazır!")


def start_web_app(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    print(f"\n  🌐 http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    start_web_app()
