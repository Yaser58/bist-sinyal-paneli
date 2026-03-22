"""
BIST Haber Analiz Sistemi - Fiyat Verisi Çekici (Price Fetcher)
===============================================================
Yahoo Finance üzerinden BIST hisselerinin geçmiş ve güncel fiyat verilerini çeker.
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

from config import BIST_TICKERS, HISTORY_DAYS
from database import insert_price_data, init_db


def fetch_historical_prices(ticker, days=None):
    """
    Belirli bir hisse için geçmiş fiyat verilerini çeker ve veritabanına kaydeder.
    """
    if days is None:
        days = HISTORY_DAYS

    try:
        stock = yf.Ticker(ticker)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        df = stock.history(start=start_date.strftime("%Y-%m-%d"),
                          end=end_date.strftime("%Y-%m-%d"))

        if df.empty:
            print(f"  ⚠️  {ticker}: Veri bulunamadı.")
            return 0

        count = 0
        for date_idx, row in df.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d")
            insert_price_data(
                ticker=ticker,
                date_str=date_str,
                open_p=round(row["Open"], 4),
                high=round(row["High"], 4),
                low=round(row["Low"], 4),
                close=round(row["Close"], 4),
                volume=int(row["Volume"]) if pd.notna(row["Volume"]) else 0
            )
            count += 1

        return count

    except Exception as e:
        print(f"  [HATA] {ticker} fiyat çekme hatası: {e}")
        return 0


def fetch_all_historical_prices():
    """Tüm BIST hisselerinin geçmiş fiyat verilerini çeker."""
    print(f"\n{'='*60}")
    print(f"  📈 FİYAT VERİSİ ÇEKME BAŞLADI - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  📅 Son {HISTORY_DAYS} günlük veri çekilecek.")
    print(f"  🏦 Toplam {len(BIST_TICKERS)} hisse")
    print(f"{'='*60}")

    total = 0
    for i, ticker in enumerate(BIST_TICKERS, 1):
        print(f"  [{i}/{len(BIST_TICKERS)}] {ticker}...", end=" ")
        count = fetch_historical_prices(ticker)
        if count > 0:
            print(f"✅ {count} gün verisi kaydedildi.")
        else:
            print(f"⚠️  Veri yok.")
        total += count

    print(f"\n  📊 Toplam {total} fiyat kaydı oluşturuldu.")
    return total


def fetch_latest_prices():
    """Sadece son birkaç günün fiyat verilerini günceller (canlı kullanım için)."""
    print(f"\n  🔄 Güncel fiyatlar güncelleniyor...")
    total = 0
    for ticker in BIST_TICKERS:
        count = fetch_historical_prices(ticker, days=7)
        total += count
    print(f"  ✅ {total} fiyat verisi güncellendi.")
    return total


def get_ticker_info(ticker):
    """Hisse hakkında temel bilgileri döndürür."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "name": info.get("longName", info.get("shortName", ticker)),
            "sector": info.get("sector", "N/A"),
            "market_cap": info.get("marketCap", 0),
            "current_price": info.get("currentPrice", info.get("regularMarketPrice", 0)),
            "52w_high": info.get("fiftyTwoWeekHigh", 0),
            "52w_low": info.get("fiftyTwoWeekLow", 0),
        }
    except Exception:
        return {"name": ticker, "sector": "N/A"}


if __name__ == "__main__":
    init_db()
    print("\n🚀 Fiyat verisi bağımsız çalıştırma modu\n")

    # Sadece ilk 5 hisseyi test olarak çek
    test_tickers = BIST_TICKERS[:5]
    for t in test_tickers:
        print(f"\n--- {t} ---")
        count = fetch_historical_prices(t, days=30)
        print(f"  {count} gün verisi çekildi.")
        info = get_ticker_info(t)
        print(f"  İsim: {info['name']}, Sektör: {info['sector']}")
