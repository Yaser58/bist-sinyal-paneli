"""
BIST Haber Analiz Sistemi - Fiyat Verisi Çekici (Price Fetcher)
===============================================================
Yahoo Finance üzerinden BIST hisselerinin geçmiş ve CANLI fiyat verilerini çeker.
Canlı mod: Tüm hisseleri toplu olarak çeker (hızlı).
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

from config import BIST_TICKERS, CRYPTO_TICKERS, ALL_TICKERS, HISTORY_DAYS
from database import insert_price_data, insert_price_data_bulk, init_db


def fetch_all_historical_prices():
    """Tüm BIST hisselerinin ve Kripto paraların geçmiş fiyat verilerini toplu ve hızlıca çeker."""
    print(f"\n{'='*60}")
    print(f"  📈 FİYAT VERİSİ ÇEKME BAŞLADI - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  📅 Son {HISTORY_DAYS} günlük veri çekilecek.")
    print(f"  🏦 Toplam {len(ALL_TICKERS)} Sembol")
    print(f"{'='*60}")

    total = 0
    end_date = datetime.now()
    start_date = end_date - timedelta(days=HISTORY_DAYS)

    try:
        tickers_str = " ".join(ALL_TICKERS)
        df = yf.download(
            tickers_str,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
            threads=True,
            group_by="ticker"
        )

        if df.empty:
            print("  ⚠️  Toplu geçmiş veri boş geldi.")
            return 0
        
        for ticker in ALL_TICKERS:
            try:
                # Çoklu ticker için seviye/kolon yapısı kontrolü
                ticker_clean = ticker.replace(".", "-")
                ticker_data = None
                for t_name in [ticker, ticker_clean]:
                    try:
                        if t_name in df.columns.get_level_values(0):
                            ticker_data = df[t_name]
                            break
                    except Exception:
                        pass
                
                if ticker_data is None or ticker_data.empty:
                    print(f"  ⚠️  {ticker}: Geçmiş veri bulunamadı.")
                    continue
                
                ticker_data = ticker_data.dropna(subset=["Close"])
                
                # Bulk list oluştur
                records = []
                for date_idx, row in ticker_data.iterrows():
                    date_str = date_idx.strftime("%Y-%m-%d")
                    records.append((
                        ticker,
                        date_str,
                        round(float(row["Open"]), 4),
                        round(float(row["High"]), 4),
                        round(float(row["Low"]), 4),
                        round(float(row["Close"]), 4),
                        int(row["Volume"]) if pd.notna(row["Volume"]) else 0
                    ))
                
                insert_price_data_bulk(records)
                print(f"  ✅ {ticker}: {len(records)} gün geçmiş veri kaydedildi.")
                total += len(records)
            except Exception as e:
                print(f"  [HATA] {ticker} işlenirken hata: {e}")
                
        print(f"\n  📊 Toplam {total} fiyat kaydı oluşturuldu (Süper Hızlı Bulk Mod).")
        return total
        
    except Exception as e:
        print(f"  [HATA] Toplu geçmiş fiyat çekme hatası: {e}")
        return 0


def fetch_realtime_prices():
    """
    Tüm BIST hisselerinin ve kriptoların CANLI fiyatlarını toplu olarak çeker.
    yf.download ile tek seferde tüm hisseleri çeker - çok daha hızlı.
    Bugünün tarihiyle kaydeder.
    """
    print(f"\n  🔴 CANLI fiyatlar çekiliyor ({len(ALL_TICKERS)} sembol)...")
    
    try:
        # Tüm hisseleri tek seferde toplu çek (çok hızlı!)
        tickers_str = " ".join(ALL_TICKERS)
        df = yf.download(
            tickers_str,
            period="1d",
            interval="1m",
            progress=False,
            threads=True,
            group_by="ticker"
        )
        
        if df.empty:
            print("  ⚠️  Toplu veri boş, tek tek çekiliyor...")
            return fetch_latest_prices()
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        total = 0
        
        for ticker in ALL_TICKERS:
            try:
                # Çoklu ticker için kolon yapısı farklı
                if len(ALL_TICKERS) > 1:
                    ticker_clean = ticker.replace(".", "-")  # yfinance bazen . yerine - kullanır
                    
                    # Hem orijinal hem de temizlenmiş isimle dene
                    ticker_data = None
                    for t_name in [ticker, ticker_clean]:
                        try:
                            if t_name in df.columns.get_level_values(0):
                                ticker_data = df[t_name]
                                break
                        except Exception:
                            pass
                    
                    if ticker_data is None or ticker_data.empty:
                        continue
                else:
                    ticker_data = df
                
                # NaN olmayan son satırı al (en güncel fiyat)
                ticker_data = ticker_data.dropna(subset=["Close"])
                if ticker_data.empty:
                    continue
                
                last_row = ticker_data.iloc[-1]
                
                # Bugünün tüm verilerinden high/low hesapla
                day_high = ticker_data["High"].max()
                day_low = ticker_data["Low"].min()
                day_open = ticker_data.iloc[0]["Open"]
                last_close = last_row["Close"]
                last_volume = int(ticker_data["Volume"].sum()) if "Volume" in ticker_data.columns else 0
                
                insert_price_data(
                    ticker=ticker,
                    date_str=today_str,
                    open_p=round(float(day_open), 4),
                    high=round(float(day_high), 4),
                    low=round(float(day_low), 4),
                    close=round(float(last_close), 4),
                    volume=last_volume
                )
                total += 1
                
            except Exception as e:
                # Sessizce devam et, hata çok fazla log üretmesin
                pass
        
        print(f"  ✅ {total}/{len(ALL_TICKERS)} sembol canlı fiyat güncellendi.")
        return total
        
    except Exception as e:
        print(f"  [HATA] Toplu canlı fiyat hatası: {e}")
        print(f"  🔄 Alternatif yöntem deneniyor...")
        return fetch_latest_prices_fast()


def fetch_latest_prices_fast():
    """
    Her hisseyi tek tek ama sadece bugünün verisini çeker.
    fetch_realtime_prices başarısız olursa fallback olarak kullanılır.
    """
    print(f"\n  🔄 Hızlı fiyat güncelleme ({len(ALL_TICKERS)} sembol)...")
    total = 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    for ticker in ALL_TICKERS:
        try:
            stock = yf.Ticker(ticker)
            # Sadece bugünün verisi (hızlı)
            df = stock.history(period="1d")
            
            if df.empty:
                continue
            
            row = df.iloc[-1]
            date_str = df.index[-1].strftime("%Y-%m-%d")
            
            insert_price_data(
                ticker=ticker,
                date_str=date_str,
                open_p=round(row["Open"], 4),
                high=round(row["High"], 4),
                low=round(row["Low"], 4),
                close=round(row["Close"], 4),
                volume=int(row["Volume"]) if pd.notna(row["Volume"]) else 0
            )
            total += 1
        except Exception:
            pass
    
    print(f"  ✅ {total}/{len(ALL_TICKERS)} hisse güncellendi.")
    return total


def fetch_latest_prices():
    """
    BIST açıkken canlı fiyat çeker, kapalıyken son kapanış verilerini günceller.
    """
    from datetime import timezone
    
    # Türkiye saati kontrolü
    TZ_TURKEY = timezone(timedelta(hours=3))
    now = datetime.now(TZ_TURKEY)
    weekday = now.weekday()
    current_minutes = now.hour * 60 + now.minute
    
    bist_open = 9 * 60 + 40   # 09:40
    bist_close = 18 * 60 + 10  # 18:10
    
    is_market_open = (weekday < 5 and bist_open <= current_minutes <= bist_close)
    
    # Kripto piyasası 7/24 açıktır, BIST kapalı olsa bile API'den çekilir. `yf.download` çok hızlı, bu yüzden tümünü canlı modda çek.
    # Ancak yine de genel mantığı koruyarak:
    if is_market_open:
        # Borsa açık - tüm canlı dakikalık veri çek
        print(f"  📊 BIST AÇIK - Canlı fiyatlar çekiliyor...")
        return fetch_realtime_prices()
    else:
        # Borsa kapalı - BIST son kapanış kalır ama Kriptolar için kapanış yoktur. 
        # En iyisi fetch_realtime_prices'ı her zaman kullanarak canlı kurları yakalamak!
        print(f"  🌙 BIST KAPALI - Ama Kriptolar için fiyatlar çekiliyor...")
        return fetch_realtime_prices()


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

    # Canlı fiyat testi
    print("--- Canlı Fiyat Testi ---")
    count = fetch_latest_prices()
    print(f"Toplam {count} hisse güncellendi.")
