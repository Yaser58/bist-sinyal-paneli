import sqlite3
import os

DB_PATH = r"c:\Users\hasan\OneDrive\Masaüstü\İş Saklama\Arşiv\Kripto CANLI\Kripto CANLI\Bist_Haber_Analiz\bist_analiz.db"

def inspect_db():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("\n--- Signals with status 'STOP' ---")
    rows = cursor.execute("SELECT * FROM signals WHERE status = 'STOP'").fetchall()
    for row in rows:
        print(dict(row))

    print("\n--- Signals for 'TRUMP' ---")
    rows = cursor.execute("SELECT * FROM signals WHERE ticker LIKE '%TRUMP%'").fetchall()
    for row in rows:
        print(dict(row))

    print("\n--- Recent Signals ---")
    rows = cursor.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 10").fetchall()
    for row in rows:
       print(dict(row))
    
    print("\n--- Signal Stats ---")
    stats = cursor.execute("SELECT status, COUNT(*) as count FROM signals GROUP BY status").fetchall()
    for s in stats:
        print(f"{s['status']}: {s['count']}")

    print("\n--- Price Data Sample for Crypto ---")
    rows = cursor.execute("SELECT * FROM price_data WHERE ticker LIKE '%-USD' ORDER BY date DESC LIMIT 5").fetchall()
    for row in rows:
        print(dict(row))

    conn.close()

if __name__ == "__main__":
    inspect_db()
