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
    """Tüm BIST hisselerinin geçmişini YF'dan, Kripto paralarınkini Bybit'ten toplu çeker."""
    print(f"\n{'='*60}")
    print(f"  📈 FİYAT GEÇMİŞİ ÇEKME BAŞLADI - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    total = 0
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30) # Açılışta hızlı olması için 30 gün kafi (Haber etkisi için)

    # 1. BIST YF Çeki
    try:
        if len(BIST_TICKERS) > 0:
            bist_str = " ".join(BIST_TICKERS)
            df = yf.download(
                bist_str,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                progress=False,
                threads=True,
                group_by="ticker"
            )

            if not df.empty:
                for ticker in BIST_TICKERS:
                    try:
                        ticker_clean = ticker.replace(".", "-")
                        ticker_data = None
                        if len(BIST_TICKERS) > 1:
                            for t_name in [ticker, ticker_clean]:
                                try:
                                    if t_name in df.columns.get_level_values(0):
                                        ticker_data = df[t_name]
                                        break
                                except Exception: pass
                        else:
                            ticker_data = df
                        
                        if ticker_data is None or ticker_data.empty: continue
                        ticker_data = ticker_data.dropna(subset=["Close"])
                        
                        records = []
                        for date_idx, row in ticker_data.iterrows():
                            records.append((
                                ticker, date_idx.strftime("%Y-%m-%d"),
                                round(float(row["Open"]), 4), round(float(row["High"]), 4),
                                round(float(row["Low"]), 4), round(float(row["Close"]), 4),
                                int(row["Volume"]) if pd.notna(row["Volume"]) else 0
                            ))
                        insert_price_data_bulk(records)
                        total += len(records)
                        print(f"  ✅ {ticker}: {len(records)} gün geçmiş YF verisi kaydedildi.")
                    except Exception as e:
                        pass
    except Exception as e:
        print(f"  [HATA] BIST geçmiş fiyat çekme hatası: {e}")

    # 2. Kripto Binance Futures / Spot Çeki
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        for ticker in CRYPTO_TICKERS:
            binance_symbol = ticker.split("-")[0] + "USDT"
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_symbol}&interval=1d&limit=500"
            req = requests.get(url, headers=headers, timeout=10)
            
            # Futures'ta yoksa (örn. TRUMP) Spot'tan dene
            if req.status_code != 200:
                url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=1d&limit=500"
                req = requests.get(url, headers=headers, timeout=10)
            if req.status_code == 200:
                kline_list = req.json()
                
                records = []
                for item in kline_list:
                    # item format: [Open time, Open, High, Low, Close, Volume, ...]
                    ts = int(item[0]) / 1000.0
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    records.append((
                        ticker, date_str,
                        round(float(item[1]), 6), round(float(item[2]), 6),
                        round(float(item[3]), 6), round(float(item[4]), 6),
                        int(float(item[5]))
                    ))
                
                if records:
                    insert_price_data_bulk(records)
                    print(f"  ✅ {ticker}: {len(records)} gün geçmiş Binance Futures verisi kaydedildi.")
                    total += len(records)
    except Exception as e:
        print(f"  [HATA] Binance Kripto geçmiş fiyat çekme hatası: {e}")

    print(f"\n  📊 Toplam {total} fiyat kaydı oluşturuldu (BIST & BYBIT).")
    return total


def fetch_realtime_prices():
    """Canlı BIST fiyatlarını YFinance'den, Kripto (Futures) fiyatlarını Bybit'ten çeker."""
    print(f"\n  🔴 CANLI fiyatlar çekiliyor ({len(ALL_TICKERS)} sembol)...")
    try:
        count_bist = 0
        if len(BIST_TICKERS) > 0:
            bist_str = " ".join(BIST_TICKERS)
            df_bist = yf.download(bist_str, period="1d", interval="1m", progress=False, group_by="ticker")
            
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            for ticker in BIST_TICKERS:
                try:
                    ticker_clean = ticker.replace(".", "-")
                    ticker_data = None
                    if len(BIST_TICKERS) > 1:
                        for t_name in [ticker, ticker_clean]:
                            try:
                                if t_name in df_bist.columns.get_level_values(0):
                                    ticker_data = df_bist[t_name]
                                    break
                            except Exception:
                                pass
                    else:
                        ticker_data = df_bist
                    
                    if ticker_data is None or ticker_data.empty:
                        continue
                    
                    ticker_data = ticker_data.dropna(subset=["Close"])
                    if ticker_data.empty:
                        continue
                    
                    last_row = ticker_data.iloc[-1]
                    day_open = ticker_data["Open"].iloc[0]
                    day_high = ticker_data["High"].max()
                    day_low = ticker_data["Low"].min()
                    last_close = last_row["Close"]
                    last_volume = int(ticker_data["Volume"].sum()) if pd.notna(ticker_data["Volume"].sum()) else 0
                    
                    insert_price_data(
                        ticker=ticker,
                        date_str=today_str,
                        open_p=round(float(day_open), 4),
                        high=round(float(day_high), 4),
                        low=round(float(day_low), 4),
                        close=round(float(last_close), 4),
                        volume=last_volume
                    )
                    count_bist += 1
                except Exception:
                    pass
        
        # 2. Kripto Fiyatları (Per-Ticker Resilience: Futures -> Spot -> YF)
        count_crypto = 0
        from config import TZ_TURKEY
        import requests
        import yfinance as yf
        today_crypto_str = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        
        # Toplu veri çekme (Hız için)
        futures_map = {}
        spot_map = {}
        try:
            r_f = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", headers=headers, timeout=5)
            if r_f.status_code == 200:
                futures_map = {item["symbol"]: item for item in r_f.json()}
            
            r_s = requests.get("https://api.binance.com/api/v3/ticker/24hr", headers=headers, timeout=5)
            if r_s.status_code == 200:
                spot_map = {item["symbol"]: item for item in r_s.json()}
        except: pass

        for ticker in CRYPTO_TICKERS:
            try:
                success = False
                base_sym = ticker.split("-")[0]
                binance_sym = base_sym + "USDT"
                
                # Kaynak 1: Binance Futures
                if binance_sym in futures_map:
                    b = futures_map[binance_sym]
                    insert_price_data(ticker, today_crypto_str, round(float(b["openPrice"]), 6), 
                                      round(float(b["highPrice"]), 6), round(float(b["lowPrice"]), 6), 
                                      round(float(b["lastPrice"]), 6), int(float(b["volume"])))
                    success = True
                
                # Kaynak 2: Binance Spot
                if not success and binance_sym in spot_map:
                    b = spot_map[binance_sym]
                    insert_price_data(ticker, today_crypto_str, round(float(b["openPrice"]), 6), 
                                      round(float(b["highPrice"]), 6), round(float(b["lowPrice"]), 6), 
                                      round(float(b["lastPrice"]), 6), int(float(b["volume"])))
                    success = True
                
                # Kaynak 3: Yahoo Finance (TRUMP gibi Binance dışı coinler için)
                if not success:
                    try:
                        ticker_yf = yf.Ticker(ticker)
                        df = ticker_yf.history(period="1d", interval="1m")
                        if not df.empty:
                            row = df.iloc[-1]
                            insert_price_data(ticker, today_crypto_str, round(float(df["Open"].iloc[0]), 6), 
                                              round(float(df["High"].max()), 6), round(float(df["Low"].min()), 6), 
                                              round(float(row["Close"]), 6), int(df["Volume"].sum()))
                            success = True
                    except: pass
                
                if success: count_crypto += 1
            except Exception as e:
                print(f"  [UYARI] {ticker} fiyati guncellenemedi: {e}")

        print(f"  ✅ {count_bist + count_crypto}/{len(ALL_TICKERS)} sembol canlı fiyat güncellendi.")
        return count_bist + count_crypto

    except Exception as e:
        print(f"  [HATA] Canlı fiyat çekme hatası: {e}")
        return fetch_latest_prices_fast()


def fetch_latest_prices_fast():
    """
    Her hisseyi tek tek ama sadece bugünün verisini çeker.
    fetch_realtime_prices başarısız olursa fallback olarak kullanılır (SADECE BIST).
    """
    print(f"\n  🔄 Hızlı fiyat güncelleme ({len(BIST_TICKERS)} sembol)...")
    total = 0
    from config import TZ_TURKEY
    today_str = datetime.now(TZ_TURKEY).strftime("%Y-%m-%d")
    
    for ticker in ALL_TICKERS:
        try:
            if ticker in CRYPTO_TICKERS:
                # Binance -> YF Resilience
                import requests
                import yfinance as yf
                headers = {"User-Agent": "Mozilla/5.0"}
                base_sym = ticker.split("-")[0]
                binance_sym = base_sym + "USDT"
                success = False
                
                # 1. Binance (Futures or Spot)
                urls = [f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={binance_sym}", 
                        f"https://api.binance.com/api/v3/ticker/24hr?symbol={binance_sym}"]
                for url in urls:
                    try:
                        req = requests.get(url, headers=headers, timeout=5)
                        if req.status_code == 200:
                            b = req.json()
                            insert_price_data(ticker, today_str, round(float(b["openPrice"]), 6), 
                                              round(float(b["highPrice"]), 6), round(float(b["lowPrice"]), 6), 
                                              round(float(b["lastPrice"]), 6), int(float(b["volume"])))
                            success = True
                            break
                    except: continue
                
                # 2. Yahoo Finance Fallback
                if not success:
                    try:
                        stock = yf.Ticker(ticker)
                        df = stock.history(period="1d")
                        if not df.empty:
                            row = df.iloc[-1]
                            insert_price_data(ticker, today_str, round(row["Open"], 6), round(row["High"], 6), 
                                              round(row["Low"], 6), round(row["Close"], 6), int(row["Volume"]))
                            success = True
                    except: pass
                
                if success: total += 1
            else:
                # BIST yfinance
                import yfinance as yf
                stock = yf.Ticker(ticker)
                df = stock.history(period="1d")
                if not df.empty:
                    row = df.iloc[-1]
                    insert_price_data(ticker, today_str, round(row["Open"], 4), round(row["High"], 4), 
                                      round(row["Low"], 4), round(row["Close"], 4), int(row["Volume"]))
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
