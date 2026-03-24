"""
BIST Haber Analiz Sistemi - Veritabanı Modülü
==============================================
SQLite ile haberlerin, fiyat verilerinin ve analiz sonuçlarının saklanması.
"""

import sqlite3
import os
from datetime import datetime
from config import DB_PATH


def get_connection():
    """Veritabanı bağlantısı döndürür."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Veritabanı tablolarını oluşturur (yoksa)."""
    conn = get_connection()
    cursor = conn.cursor()

    # ── Haberler Tablosu ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT,
            link TEXT UNIQUE,
            source TEXT,
            published_at TEXT,
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            sentiment_score REAL,
            sentiment_label TEXT,
            related_tickers TEXT,
            is_macro INTEGER DEFAULT 0,
            macro_keywords TEXT,
            processed INTEGER DEFAULT 0
        )
    """)

    # ── Hisse Fiyat Verileri Tablosu ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            UNIQUE(ticker, date)
        )
    """)

    # ── Haber-Hisse Etki Analizi Tablosu ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news_impact (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            news_date TEXT,
            sentiment_score REAL,
            price_before REAL,
            price_after_1d REAL,
            price_after_3d REAL,
            price_after_5d REAL,
            change_1d_pct REAL,
            change_3d_pct REAL,
            change_5d_pct REAL,
            FOREIGN KEY (news_id) REFERENCES news(id)
        )
    """)

    # ── Canlı Uyarılar Tablosu ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            news_id INTEGER,
            ticker TEXT,
            alert_type TEXT,
            message TEXT,
            probability REAL,
            suggested_stop_loss REAL,
            FOREIGN KEY (news_id) REFERENCES news(id)
        )
    """)

    # İndeksler
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_link ON news(link)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_processed ON news(processed)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_ticker_date ON price_data(ticker, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_impact_ticker ON news_impact(ticker)")

    conn.commit()
    conn.close()
    print("[DB] Veritabanı başarıyla oluşturuldu/kontrol edildi.")


# ─── Haber CRUD ──────────────────────────────────────────────

def insert_news(title, summary, link, source, published_at):
    """Yeni haber ekler. Aynı link varsa atlar."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO news (title, summary, link, source, published_at)
               VALUES (?, ?, ?, ?, ?)""",
            (title, summary, link, source, published_at)
        )
        conn.commit()
        # Eklendi mi kontrol et
        row = conn.execute("SELECT changes()").fetchone()
        inserted = row[0] > 0
        return inserted
    finally:
        conn.close()


def get_unprocessed_news():
    """İşlenmemiş haberleri döndürür."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM news WHERE processed = 0 ORDER BY fetched_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_news_sentiment(news_id, sentiment_score, sentiment_label, related_tickers,
                          is_macro=False, macro_keywords=None):
    """Haber duygu analizi sonucunu günceller."""
    conn = get_connection()
    conn.execute(
        """UPDATE news SET sentiment_score=?, sentiment_label=?, related_tickers=?,
           is_macro=?, macro_keywords=?, processed=1 WHERE id=?""",
        (sentiment_score, sentiment_label, related_tickers,
         1 if is_macro else 0, macro_keywords, news_id)
    )
    conn.commit()
    conn.close()


# ─── Fiyat CRUD ──────────────────────────────────────────────

def insert_price_data(ticker, date_str, open_p, high, low, close, volume):
    """Fiyat verisi ekler veya günceller."""
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO price_data (ticker, date, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, date_str, open_p, high, low, close, volume)
    )
    conn.commit()
    conn.close()

def insert_price_data_bulk(records):
    """Çoklu fiyat verisini tek seferde veritabanına ekler (Süper Hızlı)."""
    if not records:
        return
    conn = get_connection()
    conn.executemany(
        """INSERT OR REPLACE INTO price_data (ticker, date, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        records
    )
    conn.commit()
    conn.close()


def get_price_on_date(ticker, date_str):
    """Belirli tarihte hisse kapanış fiyatını döndürür."""
    conn = get_connection()
    row = conn.execute(
        "SELECT close FROM price_data WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
        (ticker, date_str)
    ).fetchone()
    conn.close()
    return row["close"] if row else None


def get_price_after_days(ticker, date_str, days):
    """Belirli tarihten N gün sonraki kapanış fiyatını döndürür."""
    conn = get_connection()
    row = conn.execute(
        """SELECT close, date FROM price_data
           WHERE ticker=? AND date > ? ORDER BY date ASC LIMIT 1 OFFSET ?""",
        (ticker, date_str, days - 1)
    ).fetchone()
    conn.close()
    return row["close"] if row else None


# ─── Etki Analizi CRUD ───────────────────────────────────────

def insert_news_impact(news_id, ticker, news_date, sentiment_score,
                       price_before, price_1d, price_3d, price_5d):
    """Haber etki analizi sonucunu kaydeder."""
    def pct(before, after):
        if before and after and before != 0:
            return round(((after - before) / before) * 100, 4)
        return None

    conn = get_connection()
    conn.execute(
        """INSERT INTO news_impact
           (news_id, ticker, news_date, sentiment_score,
            price_before, price_after_1d, price_after_3d, price_after_5d,
            change_1d_pct, change_3d_pct, change_5d_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (news_id, ticker, news_date, sentiment_score,
         price_before, price_1d, price_3d, price_5d,
         pct(price_before, price_1d),
         pct(price_before, price_3d),
         pct(price_before, price_5d))
    )
    conn.commit()
    conn.close()


def get_historical_impacts(ticker=None, sentiment_label=None, limit=500):
    """Geçmiş etki analizlerini filtreli olarak döndürür."""
    conn = get_connection()
    query = "SELECT * FROM news_impact WHERE 1=1"
    params = []
    if ticker:
        query += " AND ticker=?"
        params.append(ticker)
    if sentiment_label:
        query += " AND sentiment_score " + (">= 0.3" if sentiment_label == "positive" else "<= -0.3")
    query += f" ORDER BY news_date DESC LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Uyarı CRUD ──────────────────────────────────────────────

def insert_alert(news_id, ticker, alert_type, message, probability, stop_loss):
    """Yeni uyarı kaydeder."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO alerts (news_id, ticker, alert_type, message, probability, suggested_stop_loss)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (news_id, ticker, alert_type, message, probability, stop_loss)
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
