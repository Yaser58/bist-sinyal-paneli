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

from config import FETCH_INTERVAL_MINUTES, BIST_TICKERS, CRYPTO_TICKERS, TICKER_NAMES, COMPANY_NAMES, TZ_TURKEY
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


def get_live_prices(asset_type="bist", search=None, limit=50):
    """Veritabanından en güncel hisse/coin fiyatlarını döndürür."""
    conn = get_connection()
    prices = []
    base_tickers = BIST_TICKERS if asset_type == "bist" else CRYPTO_TICKERS
    
    for ticker_yf in base_tickers[:limit]:
        try:
            row = conn.execute(
                "SELECT close, open, high, low, volume, date FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (ticker_yf,)
            ).fetchone()
            
            is_crypto = asset_type == "crypto"
            p_data = {
                "ticker": ticker_yf.replace(".IS", "").replace("-USD", ""),
                "ticker_yf": ticker_yf,
                "ticker_tv": ticker_yf.replace("-USD", "USD").replace(".IS", ""),
                "price": round(row["close"], 8 if is_crypto else 4) if row and row["close"] else 0,
                "open": round(row["open"], 8 if is_crypto else 4) if row and row["open"] else 0,
                "high": round(row["high"], 8 if is_crypto else 4) if row and row["high"] else 0,
                "low": round(row["low"], 8 if is_crypto else 4) if row and row["low"] else 0,
                "date": row["date"] if row and row["date"] else "-",
                "change_pct": 0,
                "is_crypto": is_crypto
            }
            
            # Değişim hesapla (Öncelik: Veritabanı Geçmişi, Fallback: 24s Açılış)
            if row and row["date"]:
                # Önce veritabanındaki dünkü fiyata bak
                prev = conn.execute(
                    "SELECT close FROM price_data WHERE ticker=? AND date < ? ORDER BY date DESC LIMIT 1",
                    (ticker_yf, row["date"])
                ).fetchone()
                
                if prev and prev["close"]:
                    p_data["change_pct"] = round(((row["close"] - prev["close"]) / prev["close"]) * 100, 2)
                elif is_crypto and row["open"] and row["open"] > 0:
                    # Dünkünü bulamazsak (yeni eklenmişse), 24s önceki açılış fiyatını kullan
                    p_data["change_pct"] = round(((row["close"] - row["open"]) / row["open"]) * 100, 2)
            
            prices.append(p_data)
        except Exception as e:
            print(f"  [HATA] get_live_prices loop: {e}")
            
    conn.close()
    # Değişime göre sırala (en hareketliler üstte)
    prices.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return prices


# ─── Arka Plan Worker ────────────────────────────────────────

def price_worker():
    """Sadece fiyatları çok hızlı güncelleyen işçi (Kripto için 2sn, BIST için 10sn)."""
    while worker_status["running"]:
        try:
            fetch_latest_prices()
            time.sleep(2) # Kripto piyasası için hızlı döngü
        except Exception as e:
            print(f"[PRICE WORKER HATA] {e}")
            time.sleep(5)

def background_worker():
    """Ağır işleri (Sinyal, Haber, Teknik Tarama) yapan işçi."""
    global worker_status
    from config import TZ_TURKEY
    
    worker_status["running"] = True

    while worker_status["running"]:
        try:
            worker_status["cycle"] += 1
            now_dt = datetime.now(TZ_TURKEY)
            worker_status["last_check"] = now_dt.strftime("%d.%m.%Y %H:%M:%S")
            
            # 1. SİNYAL SONUÇLARI (Stop / Kâr)
            try:
                check_signal_results()
            except Exception as e: pass

            # 2. HABER TOPLAMA VE İŞLEME (5 dakikada bir yeterli)
            try:
                fetch_kap_notifications()
                fetch_all_feeds()
                
                unprocessed = get_unprocessed_news()
                for news in unprocessed:
                    try:
                        result = process_news_item(news)
                        update_news_sentiment(
                            news_id=result["news_id"],
                            sentiment_score=result["sentiment_score"],
                            sentiment_label=result["sentiment_label"],
                            related_tickers=json.dumps(result["tickers"]),
                            is_macro=result["is_macro"],
                            macro_keywords=json.dumps(result["macro_keywords"])
                        )
                        if result["sentiment_label"] != "neutral" or result["is_macro"]:
                            targets = result["tickers"]
                            if result["is_macro"] and not targets:
                                generate_macro_signal(result["sentiment_score"], result["sentiment_label"], news["title"])
                            else:
                                for tc in targets:
                                    generate_signal(tc, result["sentiment_score"], result["sentiment_label"], news["title"])
                    except Exception: pass
            except Exception: pass

            # 3. TEKNİK TARAMA (Proaktif - Her döngüde)
            try:
                print(f"[WORKER] Teknik tarama başladı... {now_dt.strftime('%H:%M:%S')}")
                tech_signals = run_proactive_scan()
                # UI'da göstermek için son durumu güncelle
                scan_msg = f"{now_dt.strftime('%H:%M:%S')}: {len(tech_signals)} fırsat tarandı."
                worker_status["last_check"] = scan_msg
                
                for sig in tech_signals:
                    save_signal(sig)
            except Exception as e:
                print(f"[WORKER HATA] Teknik tarama: {e}")

            time.sleep(60) # Haber ve sinyal kontrolü 1 dk bekleyebilir
            
        except Exception as e:
            print(f"[WORKER HATA] {e}")
            time.sleep(10)


# ─── HTML Template ────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BIST/Kripto Sinyal Paneli</title>
    <meta name="description" content="BIST ve Kripto haber analizi, canlı sinyal üretim sistemi">
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
        .server-time{font-size:14px;color:var(--p);font-weight:800;letter-spacing:1px;text-shadow:0 0 10px rgba(188,140,255,0.4);border:1px solid rgba(188,140,255,0.3);padding:4px 10px;border-radius:6px;background:rgba(188,140,255,0.1)}

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

        /* ── TRADINGVIEW CHART ── */
        .chart-section{padding:14px 24px;background:var(--bg2);border-bottom:1px solid var(--br)}
        .chart-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
        .chart-header h2{font-size:13px;font-weight:700;color:var(--t2)}
        .chart-ticker-name{font-size:14px;font-weight:800;color:var(--b)}
        .chart-container{width:100%;height:450px;border-radius:var(--radius);overflow:hidden;border:1px solid var(--br);background:var(--bg3)}
        .chart-container iframe{width:100%;height:100%;border:none}
        .chart-hint{font-size:11px;color:var(--t3);margin-top:6px;text-align:center}
        .tk{cursor:pointer}
        .tk.active{border-color:var(--b);background:var(--bg4);box-shadow:0 0 12px rgba(88,166,255,.15)}
        .chart-tabs{display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap}
        .chart-tab{padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;color:var(--t3);cursor:pointer;border:1px solid var(--br);background:var(--bg3);transition:all .2s}
        .chart-tab:hover{color:var(--t1);border-color:var(--br2)}
        .chart-tab.active{background:var(--bg4);color:var(--b);border-color:var(--b)}

        .empty{text-align:center;padding:30px;color:var(--t3);font-size:13px}
        .ft{text-align:center;padding:14px;color:var(--t3);font-size:10px;border-top:1px solid var(--br)}

        /* TV Overlay */
        #tv-overlay {display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:9999; justify-content:center; align-items:center; flex-direction:column}
        #tv-close {position:absolute; top:20px; right:30px; color:white; font-size:30px; cursor:pointer;}
        #tv-container {width:90%; height:80%; max-width:1200px; background:#161b22; border-radius:10px; overflow:hidden;}
        
        @media(max-width:768px){
            .hdr,.stats,.ticker-section,.container,.nav-tabs,.chart-section{padding:10px 14px}
            .sig-grid{grid-template-columns:1fr}
            .ticker-grid{grid-template-columns:repeat(2,1fr)}
            .stats{grid-template-columns:repeat(2,1fr)}
            .chart-container{height:300px}
        }
    </style>
</head>
<body>
    <!-- HEADER -->
    <div class="hdr">
        <h1>📡 Kapar Yazılım Ltd.Şti</h1>
        <div class="hdr-right">
            <span class="bist-badge {{ 'open' if bist.open else 'closed' }}">
                {{ bist.emoji }} BIST {{ bist.status }}
            </span>
            <span style="color:var(--t2)">{{ bist.reason }}</span>
            <span style="color:var(--t3)">|</span>
            <span class="server-time" id="js-clock">🕐 {{ turkey_time }}</span>
            <span style="color:var(--t3)">|</span>
            <div class="dot"></div>
            <span>Son: {{ last_check or '-' }}</span>
            <span>#{{ cycle }}</span>
        </div>
    </div>

    <!-- NAV TABS -->
    <div class="nav-tabs">
        <a href="/" class="nav-tab {{ 'active' if page == 'main' else '' }}">📊 Ana Panel (BIST)</a>
        <a href="/crypto" class="nav-tab {{ 'active' if page == 'crypto' else '' }}">🚀 Ana Panel 2 (Kripto)</a>
        <a href="/stopped" class="nav-tab stop-tab {{ 'active' if page == 'stopped' else '' }}">🛑 Stop Olan<span class="count">{{ stopped_count }}</span></a>
        <a href="/won" class="nav-tab won-tab {{ 'active' if page == 'won' else '' }}">✅ Kazanılan<span class="count">{{ won_count }}</span></a>
        <a href="/news" class="nav-tab {{ 'active' if page == 'news' else '' }}">📰 Haberler<span class="count">{{ news_count }}</span></a>
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

    {% if page in ['main', 'crypto'] %}
    <!-- LIVE TICKER BAR -->
    <div class="ticker-section">
        <div class="ticker-header">
            <h2>📈 {{ 'VİOP' if page == 'main' else 'Kripto Futures' }} Canlı Fiyatlar {% if bist.open or page == 'crypto' %}(Canlı){% else %}(Son Kapanış){% endif %}</h2>
            <input type="text" class="search-box" placeholder="🔍 Hisse ara... ({{ 'THYAO' if page == 'main' else 'BTC' }})" id="searchInput" onkeyup="filterTickers()">
        </div>
        <div class="ticker-grid" id="tickerGrid">
            {% for p in prices %}
            <div class="tk" data-ticker="{{ p.ticker }}" onclick="openChart('{{ p.ticker_tv }}', {{ 'true' if p.is_crypto else 'false' }})">
                <div class="tk-top">
                    <span class="tk-code">{{ p.ticker }}</span>
                    <span class="tk-change {{ 'up' if p.change_pct >= 0 else 'dn' }}">
                        {{ '▲' if p.change_pct >= 0 else '▼' }} %{{ '{:.2f}'.format(p.change_pct|abs) }}
                    </span>
                </div>
                <div class="tk-price">{{ p.price }} {{ 'TL' if page == 'main' else '$' }}</div>
                <div class="tk-sub">A: {{ p.open }} | Y: {{ p.high }} | D: {{ p.low }}</div>
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
                    <div class="sig-row"><span class="l">💰 Fiyat</span><span class="v">{{ '%.6f'|format(sig.price_at_signal) }}</span></div>
                    <div class="sig-row"><span class="l">🎯 Hedef</span><span class="v {{ 'g' if is_up else 'r' }}">{{ '%.6f'|format(sig.price_at_signal * (1 + (sig.expected_change_pct or 0)/100)) }}</span></div>
                    <div class="sig-row"><span class="l">🛑 Stop</span><span class="v o">{{ '%.6f'|format(sig.stop_price or 0) }} (%{{ '%.1f'|format(sig.stop_loss_pct or 0) }})</span></div>
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

    {% elif page == 'news' %}
    <!-- NEWS PAGE -->
    <div class="container">
        <div class="sec-title" style="display:flex; justify-content:space-between; align-items:center;">
            <span>📰 Canlı Haberler</span>
            <form method="get" action="/news" style="display:flex; gap:8px; align-items:center;">
                <label for="dateFilter" style="font-size:12px;color:var(--t2);">Tarih Seç:</label>
                <input type="date" id="dateFilter" name="date" value="{{ filter_date }}" style="background:var(--bg3);border:1px solid var(--br);border-radius:6px;padding:4px 8px;color:var(--t1);font-size:12px;font-family:inherit;outline:none;">
                <button type="submit" style="background:var(--accent);border:none;border-radius:6px;color:white;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;">Filtrele</button>
                {% if filter_date %}
                <a href="/news" style="font-size:12px;color:var(--r);text-decoration:none;font-weight:600;margin-left:8px;">Temizle</a>
                {% endif %}
            </form>
        </div>
        {% if news_list %}
        <div style="display:flex;flex-direction:column;gap:8px">
            {% for n in news_list %}
            <div style="background:var(--bg3);border:1px solid var(--br);border-radius:var(--radius);padding:14px 16px;transition:all .2s">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:6px">
                    <a href="{{ n.link }}" target="_blank" style="font-size:13px;font-weight:700;color:var(--t1);text-decoration:none;line-height:1.4;flex:1">{{ n.title }}</a>
                    <div style="display:flex;gap:6px;flex-shrink:0">
                        {% if n.sentiment_label == 'positive' %}
                        <span class="badge ok">📈 Pozitif</span>
                        {% elif n.sentiment_label == 'negative' %}
                        <span class="badge no">📉 Negatif</span>
                        {% else %}
                        <span class="badge" style="background:var(--bg4);color:var(--t3)">➖ Nötr</span>
                        {% endif %}
                        {% if n.is_macro %}
                        <span class="badge" style="background:rgba(188,140,255,.15);color:var(--p)">🌍 Makro</span>
                        {% endif %}
                    </div>
                </div>
                {% if n.summary %}
                <div style="font-size:11px;color:var(--t2);line-height:1.5;margin-bottom:6px">{{ n.summary[:200] }}{% if n.summary|length > 200 %}...{% endif %}</div>
                {% endif %}
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:10px;color:var(--t3)">
                    <div>
                        <span style="font-weight:600">{{ n.source or 'Bilinmeyen' }}</span>
                        {% if n.related_tickers %}
                        <span style="margin-left:8px">🏷 {{ n.related_tickers }}</span>
                        {% endif %}
                    </div>
                    <div>
                        {% if n.sentiment_score %}
                        <span>Skor: <span style="color:{{ 'var(--g)' if n.sentiment_score > 0 else 'var(--r)' if n.sentiment_score < 0 else 'var(--t3)' }};font-weight:700">{{ '%.2f'|format(n.sentiment_score) }}</span></span>
                        <span style="margin-left:8px">|</span>
                        {% endif %}
                        <span style="margin-left:4px">{{ n.published_at or n.fetched_at or '' }}</span>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty">📭 Henüz haber bulunmuyor. Sistem haberleri topluyor...</div>
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
                <td style="color:var(--o)">%{{ '%.1f'|format(sig.stop_loss_pct or 0) }} ({{ '%.6f'|format(sig.stop_price or 0) }})</td>
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

    <!-- TradingView Overlay -->
    <div id="tv-overlay">
        <div id="tv-close" onclick="closeChart()">&times;</div>
        <div id="tv-container"></div>
    </div>

    <script>
    function openChart(ticker, isCrypto) {
        document.getElementById('tv-overlay').style.display = 'flex';
        let prefix = isCrypto ? "BINANCE:" : "BIST:";
        
        if (isCrypto) {
            ticker = ticker + "T"; // BTCUSD -> BTCUSDT
        }

        new TradingView.widget({
            "autosize": true,
            "symbol": prefix + ticker,
            "interval": "15",
            "timezone": "Europe/Istanbul",
            "theme": "dark",
            "style": "1",
            "locale": "tr",
            "enable_publishing": false,
            "container_id": "tv-container"
        });
    }

    function closeChart() {
        document.getElementById('tv-overlay').style.display = 'none';
        document.getElementById('tv-container').innerHTML = '';
    }

    function filterTickers(){
        const q=document.getElementById('searchInput').value.toUpperCase();
        document.querySelectorAll('.tk').forEach(el=>{
            el.style.display=el.dataset.ticker.includes(q)?'':'none';
        });
    }

    // Refresh sayfası async olarak white ekran sorununu çözer
    setInterval(function(){
        fetch(window.location.href)
        .then(res => res.text())
        .then(html => {
            let doc = new DOMParser().parseFromString(html, 'text/html');
            // Sadece değişken içeriği yenile
            let newContainer = doc.querySelector('.container') || doc.body;
            if(document.querySelector('.container') && doc.querySelector('.container')) {
                document.querySelector('.container').innerHTML = doc.querySelector('.container').innerHTML;
            }
            if(document.querySelector('.ticker-bar') && doc.querySelector('.ticker-bar')) {
                document.querySelector('.ticker-bar').innerHTML = doc.querySelector('.ticker-bar').innerHTML;
            }
        }).catch(err => console.log(err));
    }, 45000);

    // Canlı Saat (Saniye Saniye)
    function updateClock() {
        const now = new Date();
        const trTime = new Intl.DateTimeFormat('tr-TR', {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false, timeZone: 'Europe/Istanbul'
        }).format(now);
        document.getElementById('js-clock').textContent = '🕐 ' + trTime;
    }
    setInterval(updateClock, 1000);
    updateClock();

    // Auto refresh prices with AJAX (live) - 2 Saniyede Bir
    setInterval(function(){
        fetch('/api/prices?q=' + (window.location.pathname === '/crypto' ? 'crypto' : 'bist'))
            .then(r=>r.json())
            .then(data=>{
                data.forEach(p=>{
                    const el=document.querySelector(`.tk[data-ticker="${p.ticker}"]`);
                    if(el){
                        el.querySelector('.tk-price').textContent=p.price.toFixed(p.is_crypto ? 8 : 2) + (p.is_crypto ? ' $' : ' ₺');
                        const ch=el.querySelector('.tk-change');
                        ch.textContent=(p.change_pct>=0?'▲':'▼')+' %'+Math.abs(p.change_pct).toFixed(2);
                        ch.className='tk-change '+(p.change_pct>=0?'up':'dn');
                    }
                });
            }).catch(()=>{});
    }, 2000);
    </script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
</body>
</html>
"""


# ─── Flask Routes ─────────────────────────────────────────────

import traceback

@app.errorhandler(Exception)
def handle_exception(e):
    # Tüm hataları ekranda göstermek için
    tb = traceback.format_exc()
    return f"<h1>Sistem Hatası (500)</h1><pre>{tb}</pre>", 500

def _get_common_context():
    """Tüm sayfalar için ortak context verileri."""
    stats = get_signal_success_rate()
    bist = get_bist_status()
    stopped = get_stopped_signals()
    won = get_won_signals()
    backtest = get_backtest_summary()
    turkey_time = datetime.now(TZ_TURKEY).strftime("%H:%M:%S")

    # Haber sayısı
    conn = get_connection()
    news_count_row = conn.execute("SELECT COUNT(*) as c FROM news WHERE processed=1").fetchone()
    news_count = news_count_row["c"] if news_count_row else 0
    conn.close()

    # Toplam sinyal sayısı (Veritabanından)
    conn = get_connection()
    total_signals_row = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status IN ('TAMAMLANDI', 'KAZANDI', 'STOP')").fetchone()
    total_count = (total_signals_row["c"] if total_signals_row else 0) + len(get_active_signals())
    conn.close()

    return {
        "active_count": len(get_active_signals()),
        "total_signals": total_count,
        "success_rate": stats.get("rate", 0),
        "won_count": len(won),
        "stopped_count": len(stopped),
        "news_count": news_count,
        "last_check": worker_status["last_check"],
        "cycle": worker_status["cycle"],
        "price_count": len(BIST_TICKERS) + len(CRYPTO_TICKERS),
        "last_scans": worker_status["last_scans"],
        "bist": bist,
        "backtest": backtest,
        "turkey_time": turkey_time,
    }


@app.route("/")
def dashboard():
    ctx = _get_common_context()
    active = [s for s in get_active_signals() if not s["ticker_yf"].endswith("-USD")]
    prices = get_live_prices(asset_type="bist", limit=50)

    return render_template_string(
        DASHBOARD_HTML,
        page="main",
        active_signals=active,
        prices=prices,
        **ctx,
    )

@app.route("/crypto")
def crypto_dashboard():
    ctx = _get_common_context()
    active = [s for s in get_active_signals() if s["ticker_yf"].endswith("-USD")]
    prices = get_live_prices(asset_type="crypto", limit=50)

    return render_template_string(
        DASHBOARD_HTML,
        page="crypto",
        active_signals=active,
        prices=prices,
        **ctx,
    )


@app.route("/stopped")
def stopped_page():
    ctx = _get_common_context()
    stopped = get_stopped_signals(limit=200)

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
        ORDER BY created_at DESC LIMIT 500
    """).fetchall()
    conn.close()
    all_history = [dict(r) for r in rows]

    return render_template_string(
        DASHBOARD_HTML,
        page="history",
        all_history=all_history,
        **ctx,
    )


@app.route("/news")
def news_page():
    ctx = _get_common_context()
    
    filter_date = request.args.get("date", "")

    conn = get_connection()
    if filter_date:
        rows = conn.execute("""
            SELECT * FROM news 
            WHERE DATE(published_at) = ? OR DATE(fetched_at) = ?
            ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 200
        """, (filter_date, filter_date)).fetchall()
    else:
        # Son 100 haberi getir, published_at (yayınlanma saati) baz alarak
        rows = conn.execute("""
            SELECT * FROM news ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 100
        """).fetchall()
    conn.close()
    
    news_list = [dict(r) for r in rows]

    return render_template_string(
        DASHBOARD_HTML,
        page="news",
        news_list=news_list,
        filter_date=filter_date,
        **ctx,
    )


@app.route("/api/signals")
def api_signals():
    return jsonify(get_active_signals())

@app.route("/api/prices")
def api_prices():
    search = request.args.get("q", None)
    asset_type = request.args.get("type", "bist")
    if search == "crypto" or search == "bist":
        asset_type = search
        search = None
    return jsonify(get_live_prices(asset_type=asset_type, search=search, limit=50))

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
        # Önce hızlıca canlı fiyatları al ki panel dolu gözüksün
        print("  [1/3] Hızlı canlı fiyatlar çekiliyor...")
        try:
            fetch_latest_prices()
            worker_status["last_check"] = datetime.now(TZ_TURKEY).strftime("%d.%m.%Y %H:%M:%S")
        except: pass
        
        # Sonra geçmiş verileri arka planda (hafifleşmiş modda) topla
        print("  [2/3] Geçmiş veriler ve Haberler toplanıyor...")
        try:
            fetch_all_historical_prices() # Artık daha hızlı (30 gün) çalışacak
            fetch_kap_notifications()
            fetch_all_feeds()
        except: pass
        
        print("  [3/3] Proaktif tarama yapılıyor...")
        try:
            tech_signals = run_proactive_scan()
            for sig in tech_signals[:5]:
                save_signal(sig)
                worker_status["total_signals"] += 1
        except: pass

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
    
    # background_worker artık bagimsiz thread olarak basliyor.


# ─── Program Giriş Noktası ────────────────────────────────────

print("  ⚙️  Veritabanı hazırlanıyor...")
init_db()
init_signals_table()

# 1. Thread: Hızlı Fiyat Worker
price_thread = threading.Thread(target=price_worker, daemon=True)
price_thread.start()
print("  ⚡ Canlı Fiyat Worker başlatıldı (2sn döngü).")

# 2. Thread: Sinyal ve Haber Worker
worker_thread = threading.Thread(target=background_worker, daemon=True)
worker_thread.start()
print("  📡 Sinyal/Haber Worker başlatıldı.")

# 3. Thread: Ağır init işlemleri
def _start_heavy():
    try: _heavy_init()
    except Exception as e: print(f"  [UYARI] Ağır init hatası: {e}")

heavy_thread = threading.Thread(target=_start_heavy, daemon=True)
heavy_thread.start()

print("  🚀 Sistem ayağa kalktı, web sunucu istekleri bekliyor!")



def start_web_app(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    print(f"\n  🌐 http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    start_web_app()
