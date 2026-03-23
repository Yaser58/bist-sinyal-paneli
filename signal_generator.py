"""
BIST Haber Analiz Sistemi - Sinyal Üretici (Signal Generator)
=============================================================
Haberlere ve fiyat verilerine dayanarak somut AL/SAT sinyalleri üretir.
Her sinyal şunları içerir:
  - Hisse kodu
  - Yön (YÜKSELİŞ / DÜŞÜŞ)
  - Başlangıç tarihi ve bitiş tarihi
  - Beklenen yüzdelik değişim
  - Güvenilirlik seviyesi
  - Tetikleyen haberler

Sinyaller veritabanında saklanır ve sonradan gerçekleşip gerçekleşmediği takip edilir.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from config import TICKER_NAMES, BIST_TICKERS, MACRO_KEYWORDS
from database import get_connection, init_db


# ─── BIST İş Günü Hesaplama ──────────────────────────────────

# BIST resmi tatil günleri (2026 tahmini)
BIST_HOLIDAYS = [
    "2026-01-01",  # Yılbaşı
    "2026-03-29",  # Ramazan Bayramı (tahmini)
    "2026-03-30",
    "2026-03-31",
    "2026-04-23",  # Ulusal Egemenlik
    "2026-05-01",  # İşçi Bayramı
    "2026-05-19",  # Gençlik Bayramı
    "2026-06-05",  # Kurban Bayramı (tahmini)
    "2026-06-06",
    "2026-06-07",
    "2026-06-08",
    "2026-07-15",  # Demokrasi Bayramı
    "2026-08-30",  # Zafer Bayramı
    "2026-10-29",  # Cumhuriyet Bayramı
]

def add_business_days(start_date, num_days):
    """Başlangıç tarihinden itibaren N iş günü ekler (hafta sonu ve tatil hariç)."""
    current = start_date
    added = 0
    while added < num_days:
        current += timedelta(days=1)
        # Hafta sonu mu? (5=Cumartesi, 6=Pazar)
        if current.weekday() >= 5:
            continue
        # Resmi tatil mi?
        if current.strftime("%Y-%m-%d") in BIST_HOLIDAYS:
            continue
        added += 1
    return current


def has_active_signal(ticker_code):
    """Bu hisse için zaten aktif bir sinyal var mı kontrol eder."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM signals WHERE ticker=? AND status='AKTIF'",
        (ticker_code,)
    ).fetchone()
    conn.close()
    return row["c"] > 0


# ─── Sinyal Tablosu Oluşturma ────────────────────────────────

# ─── Backtest Öğrenme Verileri ────────────────────────────────
# Bu sözlük, geçmiş sinyallerden öğrenilen bilgileri tutar
backtest_adjustments = {
    "confidence_multiplier": 1.0,
    "stop_loss_multiplier": 1.0,
    "expected_change_multiplier": 1.0,
    "min_score_threshold": 1.5,
    "avoid_tickers": [],  # Sürekli kaybeden hisseler
    "favor_tickers": [],  # Sürekli kazanan hisseler
    "last_backtest": None,
    "total_analyzed": 0,
    "win_rate": 0,
}


def init_signals_table():
    """Sinyaller tablosunu oluşturur."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            ticker TEXT NOT NULL,
            ticker_yf TEXT,
            direction TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            expected_change_pct REAL,
            actual_change_pct REAL,
            price_at_signal REAL,
            price_at_end REAL,
            stop_loss_pct REAL,
            stop_price REAL,
            confidence TEXT,
            confidence_score REAL,
            sentiment_score REAL,
            sentiment_label TEXT,
            trigger_news TEXT,
            status TEXT DEFAULT 'AKTIF',
            result TEXT,
            notes TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker)")
    
    # Mevcut tabloya stop_loss kolonları ekle (yoksa)
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN stop_loss_pct REAL")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN stop_price REAL")
    except Exception:
        pass
    
    # Backtest sonuçları tablosu
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            total_signals INTEGER,
            won_signals INTEGER,
            stopped_signals INTEGER,
            win_rate REAL,
            avg_win_pct REAL,
            avg_loss_pct REAL,
            best_ticker TEXT,
            worst_ticker TEXT,
            adjustments TEXT,
            notes TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    
    # İlk backtest çalıştır
    run_backtest_learning()


# ─── Sinyal Üretme ───────────────────────────────────────────

def generate_signal(ticker_code, sentiment_score, sentiment_label, news_title, news_summary=""):
    """
    Bir hisse için somut sinyal üretir.
    Aynı hisse için aktif sinyal varsa yeni sinyal ÜRETMEZ.
    
    Returns: dict with signal details or None if no signal
    """
    # ── Tekrar sinyal kontrolü ──
    if has_active_signal(ticker_code):
        return None  # Bu hisse için zaten aktif sinyal var
    
    yf_ticker = TICKER_NAMES.get(ticker_code, f"{ticker_code}.IS")
    conn = get_connection()
    
    # 1. Mevcut fiyatı al (en son kapanış)
    price_row = conn.execute(
        "SELECT close, date FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1",
        (yf_ticker,)
    ).fetchone()
    
    current_price = price_row["close"] if price_row else None
    last_date = price_row["date"] if price_row else None
    
    # 2. Son 30 günlük fiyat trendi
    trend_rows = conn.execute(
        "SELECT close, date FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 30",
        (yf_ticker,)
    ).fetchall()
    
    # 3. Geçmiş benzer haberlerdeki etki istatistikleri
    if sentiment_label == "positive":
        sentiment_condition = "> 0.15"
    elif sentiment_label == "negative":
        sentiment_condition = "< -0.15"
    else:
        sentiment_condition = "BETWEEN -0.15 AND 0.15"
    
    # Hisse bazlı geçmiş etkiler
    impact_rows = conn.execute(f"""
        SELECT change_1d_pct, change_3d_pct, change_5d_pct, sentiment_score
        FROM news_impact 
        WHERE ticker=? AND sentiment_score {sentiment_condition}
        AND change_1d_pct IS NOT NULL
    """, (yf_ticker,)).fetchall()
    
    # Genel geçmiş etkiler (hisse bazlı yoksa)
    if len(impact_rows) < 3:
        impact_rows = conn.execute(f"""
            SELECT change_1d_pct, change_3d_pct, change_5d_pct, sentiment_score
            FROM news_impact 
            WHERE sentiment_score {sentiment_condition}
            AND change_1d_pct IS NOT NULL
        """).fetchall()
    
    conn.close()
    
    # ─── Sinyal Hesaplama ─────────────────────────────────────
    
    today = datetime.now()
    start_date = today.strftime("%d.%m.%Y")
    
    # Fiyat trendi analizi
    trend_factor = 0
    volatility = 0
    if len(trend_rows) >= 5:
        prices = [r["close"] for r in trend_rows]
        # Son 5 günlük trend
        short_trend = (prices[0] - prices[4]) / prices[4] * 100 if prices[4] else 0
        # Son 20 günlük trend
        if len(prices) >= 20:
            long_trend = (prices[0] - prices[19]) / prices[19] * 100 if prices[19] else 0
        else:
            long_trend = short_trend
        
        trend_factor = (short_trend * 0.6 + long_trend * 0.4)
        
        # Volatilite hesapla
        if len(prices) >= 10:
            daily_changes = [abs(prices[i] - prices[i+1]) / prices[i+1] * 100 
                           for i in range(min(len(prices)-1, 20))]
            volatility = sum(daily_changes) / len(daily_changes) if daily_changes else 2.0
    
    # Geçmiş etki istatistikleri
    hist_avg_5d = 0
    hist_up_prob = 50
    sample_size = len(impact_rows)
    
    if impact_rows:
        changes_5d = [r["change_5d_pct"] or 0 for r in impact_rows]
        hist_avg_5d = sum(changes_5d) / len(changes_5d)
        
        if sentiment_label == "positive":
            hist_up_prob = sum(1 for c in changes_5d if c > 0) / len(changes_5d) * 100
        elif sentiment_label == "negative":
            hist_up_prob = sum(1 for c in changes_5d if c < 0) / len(changes_5d) * 100
    
    # ─── Beklenen Değişim Hesaplama ──────────────────────────
    
    # Duygu skoruna göre baz beklenti
    base_expectation = sentiment_score * 3.0  # -3% ile +3% arası
    
    # Geçmiş verilere göre ayarlama
    if sample_size > 0:
        base_expectation = (base_expectation * 0.4) + (hist_avg_5d * 0.6)
    
    # Trend faktörü ile ayarlama
    base_expectation += trend_factor * 0.2
    
    # Volatiliteye göre aralık genişletme
    if volatility > 3:
        base_expectation *= 1.3
    
    # Backtest öğrenmelerini uygula
    expected_change = round(base_expectation * backtest_adjustments["expected_change_multiplier"], 2)
    
    # Backtestten öğrenilen kaçınılacak hisseler
    if ticker_code in backtest_adjustments["avoid_tickers"]:
        return None  # Bu hisse sürekli kaybettiriyor, sinyal üretme
    
    # Yön belirleme — eşik backtest ile ayarlanır
    threshold = backtest_adjustments["min_score_threshold"]
    if expected_change > threshold:
        direction = "YÜKSELİŞ 📈"
    elif expected_change < -threshold:
        direction = "DÜŞÜŞ 📉"
    else:
        # Zayıf sinyal, güvenilir değil — üretme
        return None
    
    # Bitiş tarihi: 5 İŞ GÜNÜ sonra (BIST sadece hafta içi açık)
    end_date_dt = add_business_days(today, 5)
    end_date = end_date_dt.strftime("%d.%m.%Y")
    
    # Güvenilirlik (backtest ile ayarlanmış)
    conf_mult = backtest_adjustments["confidence_multiplier"]
    if sample_size >= 20:
        confidence = "YÜKSEK ⭐⭐⭐"
        confidence_score = min(0.85 * conf_mult, 1.0)
    elif sample_size >= 10:
        confidence = "ORTA ⭐⭐"
        confidence_score = min(0.65 * conf_mult, 1.0)
    elif sample_size >= 3:
        confidence = "DÜŞÜK ⭐"
        confidence_score = min(0.45 * conf_mult, 1.0)
    else:
        confidence = "ÇOK DÜŞÜK (Yetersiz veri)"
        confidence_score = min(0.25 * conf_mult, 1.0)
    
    # Favori hisseler için güvenilirliği artır
    if ticker_code in backtest_adjustments["favor_tickers"]:
        confidence_score = min(confidence_score * 1.2, 1.0)
    
    # Stop-loss hesapla (backtest ile ayarlanmış)
    sl_mult = backtest_adjustments["stop_loss_multiplier"]
    stop_loss_pct = round(abs(expected_change) * 0.5 * sl_mult, 2)
    stop_loss_pct = max(stop_loss_pct, 1.0)  # Minimum %1 stop-loss
    stop_loss_pct = min(stop_loss_pct, 8.0)  # Maksimum %8 stop-loss
    
    # Stop fiyatı hesapla
    if current_price:
        if "YÜKSELİŞ" in direction:
            stop_price = round(current_price * (1 - stop_loss_pct / 100), 2)
        else:
            stop_price = round(current_price * (1 + stop_loss_pct / 100), 2)
    else:
        stop_price = None
    
    signal = {
        "ticker": ticker_code,
        "ticker_yf": yf_ticker,
        "direction": direction,
        "start_date": start_date,
        "end_date": end_date,
        "expected_change_pct": expected_change,
        "current_price": current_price,
        "stop_loss_pct": stop_loss_pct,
        "stop_price": stop_price,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
        "trigger_news": news_title[:300],
        "sample_size": sample_size,
        "hist_up_prob": round(hist_up_prob, 1),
        "volatility": round(volatility, 2),
    }
    
    # Veritabanına kaydet
    save_signal(signal)
    
    return signal


def generate_macro_signal(sentiment_score, sentiment_label, news_title):
    """
    Makro/jeopolitik haber için BIST-100 geneli üzerinde sinyal üretir.
    Savaş, faiz, döviz gibi haberler tüm piyasayı etkiler.
    Sadece en hassas 5 hisse için sinyal üretir (spam önleme).
    """
    # En çok etkilenen 5 hisse (makro haberlere en duyarlı sektörler)
    macro_sensitive = ["THYAO", "GARAN", "TUPRS", "EREGL", "KCHOL"]
    
    signals = []
    for ticker in macro_sensitive:
        sig = generate_signal(ticker, sentiment_score, sentiment_label, news_title)
        if sig:
            signals.append(sig)
        if len(signals) >= 3:  # Makro haberlerde max 3 sinyal
            break
    
    return signals


# ─── Sinyal Kaydetme ──────────────────────────────────────────

def save_signal(signal):
    """Sinyali veritabanına kaydeder."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO signals 
        (ticker, ticker_yf, direction, start_date, end_date, 
         expected_change_pct, price_at_signal, stop_loss_pct, stop_price,
         confidence, confidence_score,
         sentiment_score, sentiment_label, trigger_news, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AKTIF')
    """, (
        signal["ticker"], signal["ticker_yf"], signal["direction"],
        signal["start_date"], signal["end_date"],
        signal["expected_change_pct"], signal.get("current_price"),
        signal.get("stop_loss_pct"), signal.get("stop_price"),
        signal["confidence"], signal["confidence_score"],
        signal["sentiment_score"], signal["sentiment_label"],
        signal["trigger_news"]
    ))
    conn.commit()
    conn.close()


# ─── Sinyal Takip (Sonuç Kontrolü) ──────────────────────────

def check_stop_loss():
    """
    Aktif sinyallerin stop-loss seviyelerini kontrol eder.
    Fiyat stop seviyesine ulaştıysa sinyali 'STOP' olarak işaretler.
    """
    conn = get_connection()
    active_signals = conn.execute("""
        SELECT * FROM signals WHERE status = 'AKTIF'
    """).fetchall()
    
    stopped = []
    for sig in active_signals:
        ticker_yf = sig["ticker_yf"]
        if not ticker_yf or not sig["price_at_signal"]:
            continue
        
        # Son fiyatı al
        price_row = conn.execute(
            "SELECT close FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker_yf,)
        ).fetchone()
        
        if not price_row:
            continue
        
        current_price = price_row["close"]
        start_price = sig["price_at_signal"]
        actual_change = round(((current_price - start_price) / start_price) * 100, 2)
        
        is_up = "YÜKSELİŞ" in (sig["direction"] or "")
        stop_loss_pct = sig["stop_loss_pct"] or (abs(sig["expected_change_pct"] or 3) * 0.5)
        
        # Stop-loss kontrolü
        hit_stop = False
        if is_up and actual_change < -stop_loss_pct:
            hit_stop = True
        elif not is_up and actual_change > stop_loss_pct:
            hit_stop = True
        
        if hit_stop:
            conn.execute("""
                UPDATE signals SET 
                    status='STOP', 
                    actual_change_pct=?,
                    price_at_end=?,
                    result='🛑 STOP OLDU'
                WHERE id=?
            """, (actual_change, current_price, sig["id"]))
            
            stopped.append({
                "id": sig["id"],
                "ticker": sig["ticker"],
                "direction": sig["direction"],
                "expected": sig["expected_change_pct"],
                "actual": actual_change,
                "stop_loss_pct": stop_loss_pct,
            })
            print(f"  🛑 STOP: {sig['ticker']} - Beklenen: %{sig['expected_change_pct']:+.2f}, Gerçek: %{actual_change:+.2f}")
    
    if stopped:
        conn.commit()
        # Stop olduğunda backtest çalıştır (öğrensin)
        run_backtest_learning()
    conn.close()
    return stopped


def check_signal_results():
    """
    Süresi dolan sinyallerin sonuçlarını kontrol eder.
    Gerçek fiyatla karşılaştırıp BAŞARILI/BAŞARISIZ olarak işaretler.
    Ayrıca stop-loss kontrolü yapar.
    """
    # Önce stop-loss kontrolü
    check_stop_loss()
    
    conn = get_connection()
    
    # Süresi dolmuş ama henüz kontrol edilmemiş sinyaller
    active_signals = conn.execute("""
        SELECT * FROM signals WHERE status = 'AKTIF'
    """).fetchall()
    
    results = []
    for sig in active_signals:
        end_date_str = sig["end_date"]
        try:
            end_dt = datetime.strptime(end_date_str, "%d.%m.%Y")
        except ValueError:
            continue
        
        if datetime.now() < end_dt:
            continue  # Henüz süresi dolmamış
        
        ticker_yf = sig["ticker_yf"]
        
        # Son fiyatı al
        price_row = conn.execute(
            "SELECT close FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker_yf,)
        ).fetchone()
        
        if not price_row or not sig["price_at_signal"]:
            continue
        
        end_price = price_row["close"]
        start_price = sig["price_at_signal"]
        actual_change = round(((end_price - start_price) / start_price) * 100, 2)
        
        # Yön doğru mu?
        expected = sig["expected_change_pct"]
        if (expected > 0 and actual_change > 0) or (expected < 0 and actual_change < 0):
            result = "✅ KAZANDI"
            status = "KAZANDI"
        else:
            result = "❌ BAŞARISIZ"
            status = "TAMAMLANDI"
        
        # Güncelle
        conn.execute("""
            UPDATE signals SET 
                status=?, 
                actual_change_pct=?,
                price_at_end=?,
                result=?
            WHERE id=?
        """, (status, actual_change, end_price, result, sig["id"]))
        
        results.append({
            "id": sig["id"],
            "ticker": sig["ticker"],
            "direction": sig["direction"],
            "expected": expected,
            "actual": actual_change,
            "result": result,
        })
    
    conn.commit()
    conn.close()
    
    # Her kontrol sonrası backtest çalıştır
    if results:
        run_backtest_learning()
    
    return results


# ─── Aktif Sinyalleri Göster ──────────────────────────────────

def get_active_signals():
    """Aktif sinyalleri döndürür."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status = 'AKTIF' ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_completed_signals(limit=50):
    """Tamamlanmış sinyalleri döndürür (BAŞARISIZ olanlar)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status = 'TAMAMLANDI' ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stopped_signals(limit=50):
    """Stop-loss tetiklenen sinyalleri döndürür."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status = 'STOP' ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_won_signals(limit=50):
    """Kazanan sinyalleri döndürür."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status = 'KAZANDI' ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_signal_success_rate():
    """Sinyal başarı oranını hesaplar (tüm tamamlanan sinyaller üzerinden)."""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status IN ('TAMAMLANDI','KAZANDI','STOP')").fetchone()["c"]
    won = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status='KAZANDI'").fetchone()["c"]
    stopped = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status='STOP'").fetchone()["c"]
    failed = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status='TAMAMLANDI'").fetchone()["c"]
    conn.close()
    
    if total == 0:
        return {"total": 0, "success": 0, "stopped": 0, "failed": 0, "rate": 0}
    
    return {
        "total": total,
        "success": won,
        "stopped": stopped,
        "failed": failed,
        "rate": round(won / total * 100, 1)
    }


# ─── Backtest Öğrenme Sistemi ─────────────────────────────────

def run_backtest_learning():
    """
    Geçmiş sinyal sonuçlarını analiz eder ve algoritmayı ayarlar.
    Bu fonksiyon, hangi tür sinyallerin başarılı/başarısız olduğunu öğrenir
    ve gelecek sinyalleri buna göre optimize eder.
    """
    global backtest_adjustments
    
    conn = get_connection()
    
    # Tamamlanan tüm sinyaller
    all_completed = conn.execute("""
        SELECT * FROM signals WHERE status IN ('TAMAMLANDI', 'KAZANDI', 'STOP')
    """).fetchall()
    
    if len(all_completed) < 3:
        conn.close()
        return  # Yeterli veri yok
    
    won = [dict(s) for s in all_completed if s["status"] == "KAZANDI"]
    stopped = [dict(s) for s in all_completed if s["status"] == "STOP"]
    failed = [dict(s) for s in all_completed if s["status"] == "TAMAMLANDI"]
    
    total = len(all_completed)
    win_count = len(won)
    win_rate = win_count / total * 100 if total > 0 else 0
    
    # ─── Hisse bazlı analiz ──────────────────────────────
    ticker_stats = {}
    for s in all_completed:
        t = s["ticker"]
        if t not in ticker_stats:
            ticker_stats[t] = {"won": 0, "lost": 0, "total": 0}
        ticker_stats[t]["total"] += 1
        if s["status"] == "KAZANDI":
            ticker_stats[t]["won"] += 1
        else:
            ticker_stats[t]["lost"] += 1
    
    # Sürekli kaybeden hisseler (3+ sinyal, %25'ten az kazanma)
    avoid = []
    favor = []
    for t, st in ticker_stats.items():
        if st["total"] >= 3:
            rate = st["won"] / st["total"] * 100
            if rate < 25:
                avoid.append(t)
            elif rate > 70:
                favor.append(t)
    
    # ─── Parametre ayarlamaları ──────────────────────────
    
    # Win rate düşükse -> daha yüksek eşik, daha geniş stop-loss
    if win_rate < 40:
        conf_mult = 0.8
        sl_mult = 1.3  # Daha geniş stop-loss
        exp_mult = 0.85
        min_threshold = 2.0  # Sadece çok güçlü sinyaller
    elif win_rate < 55:
        conf_mult = 0.9
        sl_mult = 1.1
        exp_mult = 0.95
        min_threshold = 1.7
    elif win_rate > 70:
        conf_mult = 1.1
        sl_mult = 0.9  # Daha sıkı stop-loss (risk alabilir)
        exp_mult = 1.1
        min_threshold = 1.3  # Daha fazla sinyal üretebilir
    else:
        conf_mult = 1.0
        sl_mult = 1.0
        exp_mult = 1.0
        min_threshold = 1.5
    
    # Ortalama kazanç/kayıp analizi
    avg_win = 0
    avg_loss = 0
    if won:
        avg_win = sum(abs(s.get("actual_change_pct", 0) or 0) for s in won) / len(won)
    if stopped or failed:
        losses = stopped + failed
        avg_loss = sum(abs(s.get("actual_change_pct", 0) or 0) for s in losses) / len(losses)
    
    # Stop-loss çok erken tetikleniyorsa genişlet
    if stopped and len(stopped) > len(won):
        sl_mult *= 1.2
    
    # Sonuçları kaydet
    backtest_adjustments.update({
        "confidence_multiplier": round(conf_mult, 2),
        "stop_loss_multiplier": round(sl_mult, 2),
        "expected_change_multiplier": round(exp_mult, 2),
        "min_score_threshold": round(min_threshold, 2),
        "avoid_tickers": avoid,
        "favor_tickers": favor,
        "last_backtest": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "total_analyzed": total,
        "win_rate": round(win_rate, 1),
    })
    
    # Backtest sonucunu veritabanına kaydet
    best_ticker = max(ticker_stats, key=lambda t: ticker_stats[t]["won"] / max(ticker_stats[t]["total"], 1)) if ticker_stats else None
    worst_ticker = min(ticker_stats, key=lambda t: ticker_stats[t]["won"] / max(ticker_stats[t]["total"], 1)) if ticker_stats else None
    
    try:
        conn.execute("""
            INSERT INTO backtest_results 
            (total_signals, won_signals, stopped_signals, win_rate, avg_win_pct, avg_loss_pct,
             best_ticker, worst_ticker, adjustments, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            total, win_count, len(stopped), round(win_rate, 1),
            round(avg_win, 2), round(avg_loss, 2),
            best_ticker, worst_ticker,
            json.dumps(backtest_adjustments),
            f"Kaçınılan: {avoid}, Favori: {favor}"
        ))
        conn.commit()
    except Exception as e:
        print(f"  [BACKTEST] Kayıt hatası: {e}")
    
    conn.close()
    
    print(f"  📊 BACKTEST: {total} sinyal analiz edildi | Win Rate: %{win_rate:.1f} | "
          f"Kaçınılan: {len(avoid)} | Favori: {len(favor)}")
    
    return backtest_adjustments


def get_backtest_summary():
    """Backtest öğrenme özetini döndürür."""
    return backtest_adjustments.copy()


# ─── Sinyal Yazdırma ─────────────────────────────────────────

def print_signal(signal):
    """Tek bir sinyali güzel formatla ekrana basar."""
    
    is_up = "YÜKSELİŞ" in signal["direction"]
    color_start = "\033[92m" if is_up else "\033[91m"  # Yeşil veya Kırmızı
    color_end = "\033[0m"
    bold = "\033[1m"
    
    arrow = "▲" if is_up else "▼"
    
    print(f"\n{color_start}{'╔' + '═'*63 + '╗'}{color_end}")
    print(f"{color_start}║{bold}  {arrow} SİNYAL: {signal['ticker']}  {signal['direction']}{' '*10}{color_end}{color_start}║{color_end}")
    print(f"{color_start}{'╠' + '═'*63 + '╣'}{color_end}")
    print(f"{color_start}║{color_end}  📅 Tarih Aralığı : {signal['start_date']} → {signal['end_date']}{' '*15}{color_start}║{color_end}")
    
    exp = signal['expected_change_pct']
    print(f"{color_start}║{color_end}  📊 Beklenen Değişim: {bold}{color_start}%{exp:+.2f}{color_end}{' '*30}{color_start}║{color_end}")
    
    if signal.get('current_price'):
        target = signal['current_price'] * (1 + exp/100)
        print(f"{color_start}║{color_end}  💰 Şu anki Fiyat  : {signal['current_price']:.2f} TL{' '*28}{color_start}║{color_end}")
        print(f"{color_start}║{color_end}  🎯 Hedef Fiyat    : {bold}{color_start}{target:.2f} TL{color_end}{' '*28}{color_start}║{color_end}")
    
    print(f"{color_start}║{color_end}  🛡️  Güvenilirlik   : {signal['confidence']}{' '*20}{color_start}║{color_end}")
    print(f"{color_start}║{color_end}  📈 Tarihsel Örnek : {signal.get('sample_size', 0)} benzer haber{' '*22}{color_start}║{color_end}")
    
    # Stop-loss önerisi
    sl = abs(exp) * 0.5
    print(f"{color_start}║{color_end}  🔒 Stop-Loss      : %{sl:.1f}{' '*35}{color_start}║{color_end}")
    
    print(f"{color_start}║{color_end}{' '*63}{color_start}║{color_end}")
    
    news = signal.get('trigger_news', '')[:55]
    print(f"{color_start}║{color_end}  📰 {news}...{' '*(55-len(news))}{color_start}║{color_end}")
    
    print(f"{color_start}{'╚' + '═'*63 + '╝'}{color_end}")


def print_active_signals_report():
    """Tüm aktif sinyallerin özetini basar."""
    signals = get_active_signals()
    
    print(f"\n\033[96m{'='*65}\033[0m")
    print(f"  \033[1m📡 AKTİF SİNYALLER ({len(signals)} adet)\033[0m")
    print(f"\033[96m{'='*65}\033[0m")
    
    if not signals:
        print(f"  ℹ️  Henüz aktif sinyal yok.")
        return
    
    for sig in signals:
        is_up = "YÜKSELİŞ" in (sig.get("direction") or "")
        color = "\033[92m" if is_up else "\033[91m"
        arrow = "▲" if is_up else "▼"
        exp = sig.get("expected_change_pct", 0)
        
        print(f"\n  {color}{arrow} {sig['ticker']}\033[0m  |  "
              f"{sig['start_date']} → {sig['end_date']}  |  "
              f"{color}%{exp:+.2f}\033[0m  |  "
              f"{sig.get('confidence', '?')}")
    
    # Başarı oranı
    stats = get_signal_success_rate()
    if stats["total"] > 0:
        print(f"\n\033[93m{'─'*65}\033[0m")
        print(f"  📊 Toplam Sinyal Performansı: "
              f"{stats['success']}/{stats['total']} Başarılı "
              f"(\033[1m%{stats['rate']:.1f}\033[0m)")


def print_signal_history():
    """Tamamlanmış sinyallerin geçmişini gösterir."""
    signals = get_completed_signals()
    
    print(f"\n\033[96m{'='*65}\033[0m")
    print(f"  \033[1m📜 SİNYAL GEÇMİŞİ ({len(signals)} adet)\033[0m")
    print(f"\033[96m{'='*65}\033[0m")
    
    if not signals:
        print(f"  ℹ️  Henüz tamamlanmış sinyal yok.")
        return
    
    for sig in signals:
        result = sig.get("result", "?")
        exp = sig.get("expected_change_pct", 0)
        actual = sig.get("actual_change_pct", 0)
        
        emoji = "✅" if "BAŞARILI" in result else "❌"
        
        print(f"  {emoji} {sig['ticker']}  |  "
              f"Beklenen: %{exp:+.2f}  |  "
              f"Gerçekleşen: %{actual:+.2f}  |  "
              f"{sig['start_date']} → {sig['end_date']}")
    
    stats = get_signal_success_rate()
    if stats["total"] > 0:
        print(f"\n\033[93m{'─'*65}\033[0m")
        rate_color = "\033[92m" if stats["rate"] >= 60 else "\033[91m"
        print(f"  📊 Genel Başarı Oranı: {rate_color}\033[1m%{stats['rate']:.1f}\033[0m "
              f"({stats['success']} başarılı / {stats['total']} toplam)")


if __name__ == "__main__":
    init_db()
    init_signals_table()
    
    # Test sinyali
    test_sig = generate_signal(
        ticker_code="THYAO",
        sentiment_score=0.85,
        sentiment_label="positive",
        news_title="Türk Hava Yolları rekor kâr açıkladı, yolcu sayısı artıyor"
    )
    
    if test_sig:
        print_signal(test_sig)
    else:
        print("Sinyal üretilemedi (zayıf sinyal).")
