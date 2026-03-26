import sqlite3
import os

DB_PATH = r"c:\Users\hasan\OneDrive\Masaüstü\İş Saklama\Arşiv\Kripto CANLI\Kripto CANLI\Bist_Haber_Analiz\bist_analiz.db"

def cleanup_trump():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("Cleaning up TRUMP related data...")
    
    # Delete signals
    cursor.execute("DELETE FROM signals WHERE ticker LIKE '%TRUMP%' OR ticker_yf LIKE '%TRUMP%'")
    print(f"Deleted {cursor.rowcount} signals.")

    # Delete news impacts
    cursor.execute("DELETE FROM news_impact WHERE ticker LIKE '%TRUMP%'")
    print(f"Deleted {cursor.rowcount} news impact records.")

    # Delete price data
    cursor.execute("DELETE FROM price_data WHERE ticker LIKE '%TRUMP%'")
    print(f"Deleted {cursor.rowcount} price records.")

    conn.commit()
    conn.close()
    print("Cleanup complete.")

if __name__ == "__main__":
    cleanup_trump()
