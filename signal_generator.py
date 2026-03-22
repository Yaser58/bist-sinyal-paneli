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


# ─── Sinyal Tablosu Oluşturma ────────────────────────────────

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
    conn.commit()
    conn.close()


# ─── Sinyal Üretme ───────────────────────────────────────────

def generate_signal(ticker_code, sentiment_score, sentiment_label, news_title, news_summary=""):
    """
    Bir hisse için somut sinyal üretir.
    
    Returns: dict with signal details or None if no signal
    """
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
    
    expected_change = round(base_expectation, 2)
    
    # Yön belirleme
    if expected_change > 0.5:
        direction = "YÜKSELİŞ 📈"
    elif expected_change < -0.5:
        direction = "DÜŞÜŞ 📉"
    else:
        # Çok zayıf sinyal, üretme
        return None
    
    # Bitiş tarihi: 7 gün sonra (1 haftalık sinyal)
    end_date_dt = today + timedelta(days=7)
    end_date = end_date_dt.strftime("%d.%m.%Y")
    
    # Güvenilirlik
    if sample_size >= 20:
        confidence = "YÜKSEK ⭐⭐⭐"
        confidence_score = 0.85
    elif sample_size >= 10:
        confidence = "ORTA ⭐⭐"
        confidence_score = 0.65
    elif sample_size >= 3:
        confidence = "DÜŞÜK ⭐"
        confidence_score = 0.45
    else:
        confidence = "ÇOK DÜŞÜK (Yetersiz veri)"
        confidence_score = 0.25
    
    signal = {
        "ticker": ticker_code,
        "ticker_yf": yf_ticker,
        "direction": direction,
        "start_date": start_date,
        "end_date": end_date,
        "expected_change_pct": expected_change,
        "current_price": current_price,
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
    """
    # En çok işlem gören 10 hisse için sinyal üret
    top_tickers = ["THYAO", "ASELS", "GARAN", "AKBNK", "EREGL",
                   "KCHOL", "SAHOL", "TUPRS", "BIMAS", "SISE"]
    
    signals = []
    for ticker in top_tickers:
        sig = generate_signal(ticker, sentiment_score, sentiment_label, news_title)
        if sig:
            signals.append(sig)
    
    return signals


# ─── Sinyal Kaydetme ──────────────────────────────────────────

def save_signal(signal):
    """Sinyali veritabanına kaydeder."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO signals 
        (ticker, ticker_yf, direction, start_date, end_date, 
         expected_change_pct, price_at_signal, confidence, confidence_score,
         sentiment_score, sentiment_label, trigger_news, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AKTIF')
    """, (
        signal["ticker"], signal["ticker_yf"], signal["direction"],
        signal["start_date"], signal["end_date"],
        signal["expected_change_pct"], signal.get("current_price"),
        signal["confidence"], signal["confidence_score"],
        signal["sentiment_score"], signal["sentiment_label"],
        signal["trigger_news"]
    ))
    conn.commit()
    conn.close()


# ─── Sinyal Takip (Sonuç Kontrolü) ──────────────────────────

def check_signal_results():
    """
    Süresi dolan sinyallerin sonuçlarını kontrol eder.
    Gerçek fiyatla karşılaştırıp BAŞARILI/BAŞARISIZ olarak işaretler.
    """
    conn = get_connection()
    today = datetime.now().strftime("%d.%m.%Y")
    
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
            result = "✅ BAŞARILI"
        else:
            result = "❌ BAŞARISIZ"
        
        # Güncelle
        conn.execute("""
            UPDATE signals SET 
                status='TAMAMLANDI', 
                actual_change_pct=?,
                price_at_end=?,
                result=?
            WHERE id=?
        """, (actual_change, end_price, result, sig["id"]))
        
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
    """Tamamlanmış sinyalleri döndürür."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM signals WHERE status = 'TAMAMLANDI' ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_signal_success_rate():
    """Sinyal başarı oranını hesaplar."""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status='TAMAMLANDI'").fetchone()["c"]
    success = conn.execute("SELECT COUNT(*) as c FROM signals WHERE result LIKE '%BAŞARILI%' AND status='TAMAMLANDI'").fetchone()["c"]
    conn.close()
    
    if total == 0:
        return {"total": 0, "success": 0, "rate": 0}
    
    return {
        "total": total,
        "success": success,
        "fail": total - success,
        "rate": round(success / total * 100, 1)
    }


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
