"""
BIST Haber Analiz Sistemi - Proaktif Analizci
==============================================
Haberi beklemeden, mevcut fiyat verileri ve teknik göstergelerden
kendi başına sinyal üretir. Trendleri, momentumu ve volatiliteyi analiz eder.
"""

from datetime import datetime, timedelta
from database import get_connection, init_db
from config import BIST_TICKERS, TICKER_NAMES
from signal_generator import init_signals_table


def analyze_ticker_technicals(ticker_yf):
    """
    Bir hisse için teknik analiz yapar ve sinyal üretir.
    RSI benzeri momentum, trend yönü, destek/direnç seviyeleri hesaplar.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 60",
        (ticker_yf,)
    ).fetchall()
    conn.close()

    if len(rows) < 15:
        return None

    prices = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    dates = [r["date"] for r in rows]
    current_price = prices[0]

    # ── RSI Benzeri Momentum (14 günlük) ──
    gains = []
    losses = []
    for i in range(min(14, len(prices) - 1)):
        change = prices[i] - prices[i + 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / len(gains) if gains else 0
    avg_loss = sum(losses) / len(losses) if losses else 0.0001
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = 100 - (100 / (1 + rs))

    # ── Hareketli Ortalamalar ──
    ma5 = sum(prices[:5]) / 5 if len(prices) >= 5 else current_price
    ma20 = sum(prices[:20]) / 20 if len(prices) >= 20 else current_price
    ma50 = sum(prices[:50]) / 50 if len(prices) >= 50 else current_price

    # ── Trend Yönü ──
    short_trend = ((current_price - prices[4]) / prices[4] * 100) if len(prices) > 4 else 0
    mid_trend = ((current_price - prices[19]) / prices[19] * 100) if len(prices) > 19 else short_trend

    # ── Volatilite ──
    daily_changes = []
    for i in range(min(20, len(prices) - 1)):
        ch = abs(prices[i] - prices[i + 1]) / prices[i + 1] * 100
        daily_changes.append(ch)
    volatility = sum(daily_changes) / len(daily_changes) if daily_changes else 0

    # ── Hacim Analizi ──
    avg_vol_5 = sum(volumes[:5]) / 5 if len(volumes) >= 5 else 0
    avg_vol_20 = sum(volumes[:20]) / 20 if len(volumes) >= 20 else 1
    volume_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1

    # ── Destek / Direnç ──
    recent_low = min(lows[:20]) if len(lows) >= 20 else min(lows)
    recent_high = max(highs[:20]) if len(highs) >= 20 else max(highs)

    # ── Sinyal Skoru Hesaplama ──
    score = 0
    reasons = []

    # RSI sinyalleri
    if rsi < 30:
        score += 3
        reasons.append(f"RSI aşırı satım ({rsi:.0f})")
    elif rsi < 40:
        score += 1.5
        reasons.append(f"RSI düşük ({rsi:.0f})")
    elif rsi > 70:
        score -= 3
        reasons.append(f"RSI aşırı alım ({rsi:.0f})")
    elif rsi > 60:
        score -= 1.5
        reasons.append(f"RSI yüksek ({rsi:.0f})")

    # MA crossover
    if ma5 > ma20 and prices[1] <= sum(prices[1:6]) / 5:
        score += 2
        reasons.append("MA5 yukarı kesti MA20'yi")
    elif ma5 < ma20 and prices[1] >= sum(prices[1:6]) / 5:
        score -= 2
        reasons.append("MA5 aşağı kesti MA20'yi")

    # Trend
    if short_trend > 3:
        score += 1.5
        reasons.append(f"Kısa vadeli yükseliş trendi (%{short_trend:.1f})")
    elif short_trend < -3:
        score -= 1.5
        reasons.append(f"Kısa vadeli düşüş trendi (%{short_trend:.1f})")

    # Destek/direnç yakınlığı
    dist_to_support = (current_price - recent_low) / current_price * 100
    dist_to_resistance = (recent_high - current_price) / current_price * 100

    if dist_to_support < 3:
        score += 1.5
        reasons.append(f"Destek seviyesine yakın ({recent_low:.2f})")
    if dist_to_resistance < 3:
        score -= 1
        reasons.append(f"Direnç seviyesine yakın ({recent_high:.2f})")

    # Hacim artışı
    if volume_ratio > 1.5:
        reasons.append(f"Hacim ortalamanın {volume_ratio:.1f}x üstünde")
        if score > 0:
            score *= 1.2
        elif score < 0:
            score *= 1.2

    # ── Sinyal Kararı ──
    if abs(score) < 2:
        return None  # Zayıf sinyal

    today = datetime.now()
    ticker_code = ticker_yf.replace(".IS", "")

    expected_change = round(score * 0.8, 2)
    if expected_change > 15:
        expected_change = 15
    elif expected_change < -15:
        expected_change = -15

    direction = "YÜKSELİŞ 📈" if score > 0 else "DÜŞÜŞ 📉"

    confidence_val = min(abs(score) / 8, 1.0)
    if confidence_val > 0.7:
        confidence = "YÜKSEK ⭐⭐⭐"
    elif confidence_val > 0.4:
        confidence = "ORTA ⭐⭐"
    else:
        confidence = "DÜŞÜK ⭐"

    signal = {
        "ticker": ticker_code,
        "ticker_yf": ticker_yf,
        "direction": direction,
        "start_date": today.strftime("%d.%m.%Y"),
        "end_date": (today + timedelta(days=7)).strftime("%d.%m.%Y"),
        "expected_change_pct": expected_change,
        "current_price": current_price,
        "confidence": confidence,
        "confidence_score": round(confidence_val, 2),
        "sentiment_score": round(score / 10, 2),
        "sentiment_label": "technical",
        "trigger_news": f"Teknik Analiz: {', '.join(reasons[:3])}",
        "sample_size": len(prices),
        "hist_up_prob": round(50 + score * 5, 1),
        "volatility": round(volatility, 2),
        "details": {
            "rsi": round(rsi, 1),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "short_trend": round(short_trend, 2),
            "mid_trend": round(mid_trend, 2),
            "volatility": round(volatility, 2),
            "volume_ratio": round(volume_ratio, 2),
            "support": round(recent_low, 2),
            "resistance": round(recent_high, 2),
            "reasons": reasons,
        }
    }

    return signal


def run_proactive_scan():
    """Tüm BIST hisselerini tarayarak proaktif sinyaller üretir."""
    print(f"\n\033[95m{'='*60}\033[0m")
    print(f"  🔍 PROAKTİF TARAMA BAŞLADI - {datetime.now().strftime('%H:%M:%S')}")
    print(f"  📊 {len(BIST_TICKERS)} hisse analiz ediliyor...")
    print(f"\033[95m{'='*60}\033[0m")

    signals = []
    for ticker_yf in BIST_TICKERS:
        sig = analyze_ticker_technicals(ticker_yf)
        if sig:
            signals.append(sig)

    # En güçlü sinyalleri sırala
    signals.sort(key=lambda x: abs(x["expected_change_pct"]), reverse=True)

    if signals:
        print(f"\n  🎯 {len(signals)} sinyal bulundu!\n")
    else:
        print(f"\n  ℹ️ Güçlü teknik sinyal bulunamadı.")

    return signals


if __name__ == "__main__":
    init_db()
    init_signals_table()
    from signal_generator import print_signal, save_signal

    signals = run_proactive_scan()
    for sig in signals[:10]:
        print_signal(sig)
        save_signal(sig)
