"""
BIST Haber Analiz Sistemi - Analiz ve Korelasyon Motoru
=======================================================
Geçmiş haberleri fiyat hareketleriyle eşleştirerek istatistiksel kalıplar çıkarır.
Backtesting ve olasılık hesaplama motoru.
"""

import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

from config import TICKER_NAMES, ANALYSIS_PERIODS, BIST_TICKERS
from database import (
    get_connection, get_price_on_date, get_price_after_days,
    insert_news_impact, get_historical_impacts
)


def run_backtest():
    """
    Veritabanındaki tüm işlenmiş haberleri alır,
    her haber için sonraki 1, 3, 5 günlük fiyat değişimini hesaplar
    ve news_impact tablosuna kaydeder.
    """
    print(f"\n{'='*60}")
    print(f"  🔬 GERİYE DÖNÜK TEST (BACKTESTING) BAŞLADI")
    print(f"{'='*60}")

    conn = get_connection()
    # İşlenmiş ve en az bir hisse ile ilişkili haberleri al
    news_rows = conn.execute("""
        SELECT id, title, published_at, sentiment_score, sentiment_label, related_tickers, is_macro
        FROM news
        WHERE processed = 1 AND (related_tickers IS NOT NULL AND related_tickers != '[]')
        ORDER BY published_at ASC
    """).fetchall()
    conn.close()

    if not news_rows:
        print("  ⚠️  İşlenmiş haber bulunamadı. Önce haberleri toplayıp NLP ile işleyin.")
        return

    print(f"  📰 {len(news_rows)} haber analiz edilecek.\n")

    impact_count = 0
    for row in news_rows:
        news_id = row["id"]
        news_date = row["published_at"][:10]  # YYYY-MM-DD
        sentiment_score = row["sentiment_score"] or 0.0
        tickers_str = row["related_tickers"]

        # Ticker listesini parse et
        try:
            import json
            tickers = json.loads(tickers_str) if tickers_str else []
        except (json.JSONDecodeError, TypeError):
            tickers = [t.strip() for t in tickers_str.split(",") if t.strip()] if tickers_str else []

        for ticker_code in tickers:
            # Yahoo Finance formatına çevir
            yf_ticker = TICKER_NAMES.get(ticker_code, f"{ticker_code}.IS")

            # Haber tarihindeki kapanış fiyatını al
            price_before = get_price_on_date(yf_ticker, news_date)
            if not price_before:
                continue

            # 1, 3, 5 gün sonraki fiyatları al
            price_1d = get_price_after_days(yf_ticker, news_date, 1)
            price_3d = get_price_after_days(yf_ticker, news_date, 3)
            price_5d = get_price_after_days(yf_ticker, news_date, 5)

            insert_news_impact(
                news_id=news_id,
                ticker=yf_ticker,
                news_date=news_date,
                sentiment_score=sentiment_score,
                price_before=price_before,
                price_1d=price_1d,
                price_3d=price_3d,
                price_5d=price_5d,
            )
            impact_count += 1

    print(f"  ✅ Backtest tamamlandı. {impact_count} etki kaydı oluşturuldu.")


def calculate_statistics():
    """
    news_impact tablosundaki verileri analiz ederek istatistiksel kalıplar çıkarır.
    Returns: dict with aggregated statistics
    """
    conn = get_connection()

    stats = {}

    # ── Genel İstatistikler ──
    rows = conn.execute("""
        SELECT
            sentiment_score,
            change_1d_pct,
            change_3d_pct,
            change_5d_pct
        FROM news_impact
        WHERE change_1d_pct IS NOT NULL
    """).fetchall()

    if not rows:
        print("  ⚠️  Yeterli etki verisi bulunamadı.")
        conn.close()
        return stats

    # Pozitif haberlerde ortalama değişim
    pos_rows = [r for r in rows if r["sentiment_score"] and r["sentiment_score"] > 0.15]
    neg_rows = [r for r in rows if r["sentiment_score"] and r["sentiment_score"] < -0.15]
    neu_rows = [r for r in rows if r["sentiment_score"] and -0.15 <= r["sentiment_score"] <= 0.15]

    def avg_changes(row_list):
        if not row_list:
            return {"count": 0, "avg_1d": 0, "avg_3d": 0, "avg_5d": 0,
                    "up_prob_1d": 0, "up_prob_3d": 0, "up_prob_5d": 0}
        n = len(row_list)
        avg_1d = sum(r["change_1d_pct"] or 0 for r in row_list) / n
        avg_3d = sum(r["change_3d_pct"] or 0 for r in row_list) / n
        avg_5d = sum(r["change_5d_pct"] or 0 for r in row_list) / n

        up_1d = sum(1 for r in row_list if (r["change_1d_pct"] or 0) > 0)
        up_3d = sum(1 for r in row_list if (r["change_3d_pct"] or 0) > 0)
        up_5d = sum(1 for r in row_list if (r["change_5d_pct"] or 0) > 0)

        return {
            "count": n,
            "avg_1d": round(avg_1d, 4),
            "avg_3d": round(avg_3d, 4),
            "avg_5d": round(avg_5d, 4),
            "up_prob_1d": round(up_1d / n * 100, 2),
            "up_prob_3d": round(up_3d / n * 100, 2),
            "up_prob_5d": round(up_5d / n * 100, 2),
        }

    stats["positive_news"] = avg_changes(pos_rows)
    stats["negative_news"] = avg_changes(neg_rows)
    stats["neutral_news"] = avg_changes(neu_rows)

    # ── Hisse Bazlı İstatistikler ──
    ticker_stats = defaultdict(list)
    for r in rows:
        # Ticker bilgisi için news_impact tablosundan çek
        pass

    ticker_rows = conn.execute("""
        SELECT ticker, sentiment_score, change_1d_pct, change_3d_pct, change_5d_pct
        FROM news_impact WHERE change_1d_pct IS NOT NULL
    """).fetchall()

    for r in ticker_rows:
        ticker_stats[r["ticker"]].append(r)

    stats["per_ticker"] = {}
    for ticker, t_rows in ticker_stats.items():
        pos = [r for r in t_rows if r["sentiment_score"] and r["sentiment_score"] > 0.15]
        neg = [r for r in t_rows if r["sentiment_score"] and r["sentiment_score"] < -0.15]
        stats["per_ticker"][ticker] = {
            "total": len(t_rows),
            "positive": avg_changes(pos),
            "negative": avg_changes(neg),
        }

    conn.close()
    return stats


def predict_impact(ticker_code, sentiment_score, sentiment_label):
    """
    Yeni bir haber geldiğinde, geçmiş verilerden olasılık tahmini yapar.
    Returns: dict with prediction
    """
    yf_ticker = TICKER_NAMES.get(ticker_code, f"{ticker_code}.IS")

    conn = get_connection()

    # Aynı hisse + benzer duygu skorundaki geçmiş haberleri al
    if sentiment_label == "positive":
        condition = "sentiment_score > 0.15"
    elif sentiment_label == "negative":
        condition = "sentiment_score < -0.15"
    else:
        condition = "sentiment_score BETWEEN -0.15 AND 0.15"

    rows = conn.execute(f"""
        SELECT change_1d_pct, change_3d_pct, change_5d_pct
        FROM news_impact
        WHERE ticker = ? AND {condition} AND change_1d_pct IS NOT NULL
    """, (yf_ticker,)).fetchall()
    conn.close()

    if not rows:
        # Hisse bazlı veri yoksa genel istatistikleri kullan
        return _predict_general(sentiment_label)

    n = len(rows)
    avg_1d = sum(r["change_1d_pct"] for r in rows) / n
    avg_3d = sum(r["change_3d_pct"] or 0 for r in rows) / n
    avg_5d = sum(r["change_5d_pct"] or 0 for r in rows) / n

    # Yükseliş olasılıkları
    up_1d = sum(1 for r in rows if r["change_1d_pct"] > 0) / n * 100
    up_3d = sum(1 for r in rows if (r["change_3d_pct"] or 0) > 0) / n * 100

    # Tavsiye edilen stop-loss (en kötü senaryonun %80'i)
    worst = min(r["change_1d_pct"] for r in rows)
    suggested_sl = round(abs(worst) * 0.8, 2)

    return {
        "ticker": ticker_code,
        "sample_size": n,
        "sentiment": sentiment_label,
        "expected_change_1d": round(avg_1d, 2),
        "expected_change_3d": round(avg_3d, 2),
        "expected_change_5d": round(avg_5d, 2),
        "up_probability_1d": round(up_1d, 1),
        "up_probability_3d": round(up_3d, 1),
        "suggested_stop_loss_pct": suggested_sl,
        "confidence": "Yüksek" if n >= 20 else "Orta" if n >= 5 else "Düşük",
    }


def _predict_general(sentiment_label):
    """Hisse bazlı veri yoksa genel istatistiklerle tahmin yapar."""
    stats = calculate_statistics()
    key = f"{sentiment_label}_news"
    data = stats.get(key, {})

    return {
        "ticker": "GENEL",
        "sample_size": data.get("count", 0),
        "sentiment": sentiment_label,
        "expected_change_1d": data.get("avg_1d", 0),
        "expected_change_3d": data.get("avg_3d", 0),
        "expected_change_5d": data.get("avg_5d", 0),
        "up_probability_1d": data.get("up_prob_1d", 50),
        "up_probability_3d": data.get("up_prob_3d", 50),
        "suggested_stop_loss_pct": 3.0,
        "confidence": "Genel ortalama (hisse bazlı veri yetersiz)",
    }


def print_statistics_report():
    """İstatistik raporunu formatlanmış olarak konsola basar."""
    stats = calculate_statistics()
    if not stats:
        print("  Rapor oluşturulacak yeterli veri yok.")
        return

    print(f"\n{'='*70}")
    print(f"  📊 BIST HABER ETKİ ANALİZİ İSTATİSTİK RAPORU")
    print(f"{'='*70}")

    for label, key in [("🟢 POZİTİF Haberler", "positive_news"),
                       ("🔴 NEGATİF Haberler", "negative_news"),
                       ("⚪ NÖTR Haberler", "neutral_news")]:
        data = stats.get(key, {})
        if data.get("count", 0) == 0:
            continue
        print(f"\n  {label} (Toplam: {data['count']} haber)")
        print(f"  {'─'*50}")
        print(f"    1 Gün Sonra:  Ort. Değişim: %{data['avg_1d']:+.2f}  |  Yükseliş İhtimali: %{data['up_prob_1d']:.1f}")
        print(f"    3 Gün Sonra:  Ort. Değişim: %{data['avg_3d']:+.2f}  |  Yükseliş İhtimali: %{data['up_prob_3d']:.1f}")
        print(f"    5 Gün Sonra:  Ort. Değişim: %{data['avg_5d']:+.2f}  |  Yükseliş İhtimali: %{data['up_prob_5d']:.1f}")

    # Hisse bazlı en dikkat çekici sonuçlar
    per_ticker = stats.get("per_ticker", {})
    if per_ticker:
        print(f"\n\n  🏦 HİSSE BAZLI ÖZET")
        print(f"  {'─'*50}")
        for ticker, tdata in sorted(per_ticker.items(), key=lambda x: x[1]["total"], reverse=True)[:15]:
            pos = tdata.get("positive", {})
            neg = tdata.get("negative", {})
            print(f"\n    {ticker}: Toplam {tdata['total']} haber")
            if pos.get("count", 0) > 0:
                print(f"      Pozitif ({pos['count']}): 1G %{pos['avg_1d']:+.2f} | 3G %{pos['avg_3d']:+.2f}")
            if neg.get("count", 0) > 0:
                print(f"      Negatif ({neg['count']}): 1G %{neg['avg_1d']:+.2f} | 3G %{neg['avg_3d']:+.2f}")


if __name__ == "__main__":
    print("\n🔬 Backtest ve İstatistik Raporu\n")
    run_backtest()
    print_statistics_report()
