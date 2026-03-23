"""
BIST Haber Analiz Sistemi - Konfigürasyon Dosyası
==================================================
Tüm ayarlar burada merkezi olarak yönetilir.
"""

import os

# ─── Veritabanı ───────────────────────────────────────────────
# Render'da kalıcı disk kullan (deploy'larda veri kaybolmasın)
_RENDER_DATA_DIR = "/opt/render/project/data"
if os.path.exists("/opt/render"):
    # Render ortamında - kalıcı dizin oluştur
    os.makedirs(_RENDER_DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(_RENDER_DATA_DIR, "bist_analiz.db")
else:
    # Yerel geliştirme
    DB_PATH = os.path.join(os.path.dirname(__file__), "bist_analiz.db")

# ─── Haber Kaynakları (RSS) ──────────────────────────────────
# KAP + genel ekonomi/dünya haberleri
RSS_FEEDS = {
    # KAP Bildirimleri
    "KAP": "https://www.kap.org.tr/tr/rss/bildirim",
    # Genel Ekonomi / Borsa Haberleri
    "BloombergHT": "https://www.bloomberght.com/rss",
    "Dunya_Ekonomi": "https://www.dunya.com/rss",
    "Mynet_Ekonomi": "https://www.mynet.com/haber/rss/kategori/ekonomi",
    "NTV_Ekonomi": "https://www.ntv.com.tr/ekonomi.rss",
    # Dünya / Savaş / Jeopolitik Haberler (borsayı etkileyen)
    "NTV_Dunya": "https://www.ntv.com.tr/dunya.rss",
    "Mynet_Dunya": "https://www.mynet.com/haber/rss/kategori/dunya",
}

# ─── VİOP Pay Vadeli İşlem Sözleşmeleri ────────────────────────
# Sadece VİOP'ta vadeli işlem sözleşmesi olan hisseler
BIST_TICKERS = [
    "THYAO.IS", "ASELS.IS", "SASA.IS", "EREGL.IS", "KCHOL.IS",
    "GARAN.IS", "AKBNK.IS", "YKBNK.IS", "HALKB.IS", "VAKBN.IS",
    "ISCTR.IS", "SAHOL.IS", "TUPRS.IS", "BIMAS.IS", "KOZAL.IS",
    "KOZAA.IS", "PGSUS.IS", "TAVHL.IS", "TCELL.IS", "TTKOM.IS",
    "SISE.IS", "TOASO.IS", "FROTO.IS", "ARCLK.IS", "VESTL.IS",
    "PETKM.IS", "ENKAI.IS", "EKGYO.IS", "DOHOL.IS", "MGROS.IS",
    "SOKM.IS", "KRDMD.IS", "ISDMR.IS", "KONTR.IS",
    "GUBRF.IS", "HEKTS.IS", "GESAN.IS", "ODAS.IS", "ENJSA.IS",
]

# Ticker isimleri (THYAO.IS -> THYAO) - haber eşleştirme için
TICKER_NAMES = {t.replace(".IS", ""): t for t in BIST_TICKERS}

# Şirket tam adları - haberlerde isim geçtiğinde eşleştirebilmek için
COMPANY_NAMES = {
    "THYAO": ["türk hava yolları", "thy", "türk havayolları", "turkish airlines"],
    "ASELS": ["aselsan"],
    "SASA": ["sasa polyester", "sasa"],
    "EREGL": ["ereğli demir çelik", "erdemir", "ereğli"],
    "KCHOL": ["koç holding", "koç"],
    "GARAN": ["garanti bankası", "garanti bbva", "garanti"],
    "AKBNK": ["akbank"],
    "YKBNK": ["yapı kredi", "yapıkredi"],
    "HALKB": ["halkbank", "halk bankası"],
    "VAKBN": ["vakıfbank", "vakıf bankası"],
    "ISCTR": ["iş bankası", "işbank"],
    "SAHOL": ["sabancı holding", "sabancı"],
    "TUPRS": ["tüpraş"],
    "BIMAS": ["bim", "bim mağazaları"],
    "KOZAL": ["koza altın"],
    "KOZAA": ["koza anadolu"],
    "PGSUS": ["pegasus", "pegasus hava yolları"],
    "TAVHL": ["tav havalimanları", "tav"],
    "TCELL": ["turkcell"],
    "TTKOM": ["türk telekom", "türktelekom"],
    "SISE": ["şişecam", "şişe cam"],
    "TOASO": ["tofaş"],
    "FROTO": ["ford otosan", "ford otomotiv"],
    "ARCLK": ["arçelik"],
    "VESTL": ["vestel"],
    "PETKM": ["petkim"],
    "ENKAI": ["enka inşaat", "enka"],
    "MGROS": ["migros"],
    "SOKM": ["şok market", "şok"],
    "ULKER": ["ülker"],
    "OTKAR": ["otokar"],
}

# ─── NLP Modeli ───────────────────────────────────────────────
SENTIMENT_MODEL = "savasy/bert-base-turkish-sentiment-cased"

# ─── Analiz Parametreleri ─────────────────────────────────────
# Haber sonrası takip edilecek gün sayıları
ANALYSIS_PERIODS = [1, 3, 5]

# Geçmiş veri çekme süresi (gün)
HISTORY_DAYS = 365

# Haber çekme sıklığı (dakika)
FETCH_INTERVAL_MINUTES = 5

# ─── Jeopolitik / Makro Anahtar Kelimeler ─────────────────────
# Bunlar doğrudan hisse eşleşmese bile piyasa genelini etkiler
MACRO_KEYWORDS = [
    "savaş", "war", "çatışma", "bomba", "füze",
    "nato", "bm", "birleşmiş milletler",
    "faiz", "faiz kararı", "merkez bankası", "tcmb",
    "enflasyon", "tüfe", "üfe",
    "dolar", "euro", "kur", "döviz",
    "petrol", "doğalgaz", "enerji krizi",
    "deprem", "doğal afet", "sel",
    "seçim", "hükümet", "kabine", "cumhurbaşkanı",
    "ambargo", "yaptırım", "sanction",
    "imf", "dünya bankası", "kredi notu",
    "resesyon", "recession", "kriz",
    "ihracat", "ithalat", "cari açık",
    "borsa kapandı", "devre kesici",
]

# ─── Konsol Çıktısı ──────────────────────────────────────────
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
