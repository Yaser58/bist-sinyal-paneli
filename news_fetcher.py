"""
BIST Haber Analiz Sistemi - Haber Toplayıcı (News Fetcher)
===========================================================
RSS feedlerinden ve web kaynaklarından haberleri çeker ve veritabanına kaydeder.
KAP bildirimleri + genel ekonomi/dünya haberleri (savaş, jeopolitik, faiz vb.)
"""

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import re

from config import RSS_FEEDS
from database import insert_news, init_db


def get_tr_time():
    """Türkiye saatini (UTC+3) döndürür."""
    # Render gibi UTC makinalarında doğru saati almak için +3 eklenir
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

def parse_date(date_str):
    """Farklı formatlardaki tarihleri standart formata (TR Saati) çevirir."""
    if not date_str:
        return get_tr_time()

    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            # Eğer timezone içeriyorsa, önce UTC'ye çevirip üstüne 3 saat ekleyelim (TR saati)
            if dt.tzinfo:
                from datetime import timezone
                dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return (dt_utc + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    # feedparser'ın kendi struct_time formatı
    try:
        import calendar
        if hasattr(date_str, 'tm_year'):
            ts = calendar.timegm(date_str)
            return (datetime.utcfromtimestamp(ts) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    return get_tr_time()


def clean_html(html_text):
    """HTML etiketlerini temizleyip sade metin döndürür."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def fetch_rss_feed(feed_name, feed_url):
    """Tek bir RSS feed'i çeker ve haberleri veritabanına kaydeder."""
    new_count = 0
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        # feedparser ile çek
        feed = feedparser.parse(feed_url, request_headers=headers)

        if feed.bozo and not feed.entries:
            # RSS parse edilemezse doğrudan requests ile dene
            resp = requests.get(feed_url, headers=headers, timeout=15)
            feed = feedparser.parse(resp.content)

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            summary = clean_html(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "")
            published = entry.get("published", entry.get("updated", ""))

            # feedparser struct_time formatı
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                ts = calendar.timegm(entry.published_parsed)
                pub_date = (datetime.utcfromtimestamp(ts) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                pub_date = parse_date(published)

            inserted = insert_news(
                title=title,
                summary=summary[:2000] if summary else "",
                link=link,
                source=feed_name,
                published_at=pub_date
            )
            if inserted:
                new_count += 1

    except Exception as e:
        print(f"  [HATA] {feed_name} feed çekilirken hata: {e}")

    return new_count


def fetch_all_feeds():
    """Tüm RSS kaynaklarından haberleri çeker."""
    print(f"\n{'='*60}")
    print(f"  📰 HABER TOPLAMA BAŞLADI - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    total_new = 0
    for feed_name, feed_url in RSS_FEEDS.items():
        print(f"  📡 {feed_name} kontrol ediliyor...", end=" ")
        count = fetch_rss_feed(feed_name, feed_url)
        if count > 0:
            print(f"✅ {count} yeni haber eklendi.")
        else:
            print(f"— Yeni haber yok.")
        total_new += count
        time.sleep(1)  # Kaynakları yormamak için

    print(f"\n  📊 Toplam {total_new} yeni haber kaydedildi.")
    return total_new


def fetch_kap_notifications():
    """
    KAP bildirimlerini özel olarak çeker.
    KAP'ın RSS'i çalışmazsa, alternatif olarak ana sayfadan scraping yapar.
    """
    new_count = 0
    try:
        url = "https://www.kap.org.tr/tr/bildirim-sorgu"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }

        # KAP API endpoint (JSON formatı)
        api_url = "https://www.kap.org.tr/tr/api/bildirim/son"
        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
            if resp.status_code == 200 and resp.text.strip().startswith("["):
                data = resp.json()
                for item in data[:50]:  # Son 50 bildirim
                    title = item.get("baslik", item.get("title", ""))
                    company = item.get("sirketAdi", item.get("company", ""))
                    disclosure_type = item.get("tip", "")
                    published = item.get("tarih", item.get("date", ""))
                    link = f"https://www.kap.org.tr/tr/bildirim/{item.get('id', '')}"

                    full_title = f"[KAP] {company} - {title}"
                    summary = f"Bildirim Tipi: {disclosure_type}. Şirket: {company}. {title}"

                    inserted = insert_news(
                        title=full_title[:500],
                        summary=summary[:2000],
                        link=link,
                        source="KAP_API",
                        published_at=parse_date(published)
                    )
                    if inserted:
                        new_count += 1
        except Exception as e:
            print(f"  [BİLGİ] KAP API'ye erişilemedi ({e}), RSS ile devam ediliyor.")

    except Exception as e:
        print(f"  [HATA] KAP bildirim çekme hatası: {e}")

    return new_count


if __name__ == "__main__":
    init_db()
    print("\n🚀 Haber toplama bağımsız çalıştırma modu\n")
    kap_count = fetch_kap_notifications()
    print(f"  KAP: {kap_count} yeni bildirim")
    total = fetch_all_feeds()
    print(f"\n✅ Tamamlandı. Toplam yeni haber: {total + kap_count}")
