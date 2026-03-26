import os
from database import get_connection

conn = get_connection()
rows = conn.execute("SELECT ticker, close, date FROM price_data WHERE ticker LIKE '%USD%' ORDER BY date DESC LIMIT 10").fetchall()
print("Kripto fiyatlari DB:", rows)
conn.close()

import requests
headers = {"User-Agent": "Mozilla/5.0"}
try:
    req = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT", headers=headers, timeout=5)
    print("Futures BTCUSDT:", req.status_code, req.json())
except Exception as e:
    print("Error:", e)
    
# Test bitti
