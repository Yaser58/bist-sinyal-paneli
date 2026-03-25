from database import get_connection
from config import CRYPTO_TICKERS, BIST_TICKERS
import datetime

conn = get_connection()
print("--- DATABASE CHECK ---")
for t in CRYPTO_TICKERS[:3] + ["TRUMP-USD"]:
    row = conn.execute("SELECT date, close FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
    if row:
        print(f"Ticker: {t} | Last Date: {row['date']} | Price: {row['close']}")
    else:
        print(f"Ticker: {t} | NO DATA")

count = conn.execute("SELECT COUNT(*) as c FROM price_data").fetchone()["c"]
print(f"Total Rows in price_data: {count}")

active_sigs = conn.execute("SELECT ticker, price_at_signal, current_price, status FROM signals WHERE status='AKTIF'").fetchall()
print("\n--- ACTIVE SIGNALS ---")
for s in active_sigs:
    print(f"Signal: {s['ticker']} | Entry: {s['price_at_signal']} | Current: {s['current_price']} | Status: {s['status']}")

conn.close()
