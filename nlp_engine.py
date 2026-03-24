"""
BIST Haber Analiz Sistemi - NLP / Duygu Analizi Motoru
======================================================
Türkçe haberlerin duygu analizini yapar, ilgili hisse kodlarını tespit eder,
ve makro/jeopolitik haberleri sınıflandırır.
"""

import re
from config import COMPANY_NAMES, TICKER_NAMES, MACRO_KEYWORDS, SENTIMENT_MODEL

# ─── Model yükleme (lazy loading) ────────────────────────────
_sentiment_pipeline = None


def _load_model():
    """Duygu analizi modelini yükler (ilk kullanımda)."""
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        print("  🧠 NLP Modeli yükleniyor (ilk kullanım, biraz sürebilir)...")
        try:
            from transformers import pipeline
            _sentiment_pipeline = pipeline(
                "sentiment-analysis",
                model=SENTIMENT_MODEL,
                tokenizer=SENTIMENT_MODEL,
                max_length=512,
                truncation=True
            )
            print("  ✅ NLP Modeli yüklendi.")
        except Exception as e:
            print(f"  ⚠️  NLP Modeli yüklenemedi: {e}")
            print("  ℹ️  Basit kural-tabanlı analiz kullanılacak.")
            _sentiment_pipeline = "fallback"
    return _sentiment_pipeline


# ─── Hisse Kodu Tespiti ──────────────────────────────────────

def extract_tickers(text):
    """
    Metin içinden bahsedilen BIST hisse kodlarını tespit eder.
    Hem ticker kodu (THYAO) hem de şirket adları (Türk Hava Yolları) aranır.
    """
    if not text:
        return []

    found_tickers = set()
    text_lower = text.lower()

    # 1. Doğrudan ticker kodu arama (Örn: THYAO, ASELS)
    for ticker_code in TICKER_NAMES.keys():
        # Ticker kodunun metinde geçip geçmediğini kontrol et (kelime sınırlarıyla)
        pattern = r'\b' + re.escape(ticker_code) + r'\b'
        if re.search(pattern, text, re.IGNORECASE):
            found_tickers.add(ticker_code)

    # 2. Şirket adı ile arama
    for ticker_code, names in COMPANY_NAMES.items():
        for name in names:
            pattern = r'\b' + re.escape(name) + r'\b'
            if re.search(pattern, text, re.IGNORECASE):
                found_tickers.add(ticker_code)
                break

    return list(found_tickers)


# ─── Makro/Jeopolitik Haber Tespiti ──────────────────────────

def detect_macro_keywords(text):
    """
    Metinde jeopolitik/makro anahtar kelimeler var mı kontrol eder.
    Savaş, faiz, döviz gibi haberlerin borsanın genelini etkilediğini biliyoruz.
    """
    if not text:
        return []

    text_lower = text.lower()
    found = []
    for keyword in MACRO_KEYWORDS:
        if keyword.lower() in text_lower:
            found.append(keyword)

    return found


# ─── Duygu Analizi ───────────────────────────────────────────

def analyze_sentiment(text):
    """
    Metin üzerinde duygu analizi yapar.
    Returns: (score: float [-1.0, 1.0], label: str ['positive','negative','neutral'])
    """
    if not text or len(text.strip()) < 10:
        return 0.0, "neutral"

    model = _load_model()

    if model == "fallback":
        return _rule_based_sentiment(text)

    try:
        # Modelin max token sınırı var, metni kırp
        truncated = text[:500]
        result = model(truncated)[0]

        label = result["label"].lower()
        confidence = result["score"]

        if label in ["positive", "pos", "olumlu"]:
            return round(confidence, 4), "positive"
        elif label in ["negative", "neg", "olumsuz"]:
            return round(-confidence, 4), "negative"
        else:
            return 0.0, "neutral"

    except Exception as e:
        print(f"  ⚠️  Duygu analizi hatası: {e}")
        return _rule_based_sentiment(text)


def _rule_based_sentiment(text):
    """
    Model çalışmazsa basit kural-tabanlı duygu analizi.
    Pozitif/negatif anahtar kelimelere göre skor üretir.
    """
    text_lower = text.lower()

    positive_words = [
        "kâr", "kar", "artış", "yükseliş", "rekor", "büyüme", "gelir artışı",
        "temettü", "olumlu", "pozitif", "güçlü", "iyi", "başarı", "ihale aldı",
        "anlaşma", "sözleşme", "yatırım", "kapasite artışı", "ihracat artışı",
        "not artışı", "hedef fiyat yükseltme", "al tavsiyesi",
        "barış", "ateşkes", "uzlaşma", "istikrar",
        "faiz indirimi", "teşvik", "destek paketi",
    ]

    negative_words = [
        "zarar", "düşüş", "kayıp", "gerileme", "daralma", "iflas", "borç",
        "ceza", "soruşturma", "olumsuz", "negatif", "zayıf", "kötü",
        "grev", "kriz", "risk", "tehdit", "uyarı",
        "savaş", "çatışma", "bomba", "füze", "terör",
        "faiz artışı", "enflasyon artışı", "devalüasyon",
        "not düşürme", "sat tavsiyesi", "hedef fiyat düşürme",
        "ambargo", "yaptırım", "resesyon",
    ]

    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)

    total = pos_count + neg_count
    if total == 0:
        return 0.0, "neutral"

    score = (pos_count - neg_count) / total
    if score > 0.15:
        return round(score, 4), "positive"
    elif score < -0.15:
        return round(score, 4), "negative"
    else:
        return round(score, 4), "neutral"


# ─── Ana İşleme Fonksiyonu ───────────────────────────────────

def process_news_item(news_dict):
    """
    Tek bir haber öğesini işler:
    1. Hisse kodu tespiti
    2. Makro anahtar kelime tespiti
    3. Duygu analizi
    Returns: dict with analysis results
    """
    title = news_dict.get("title", "")
    summary = news_dict.get("summary", "")
    full_text = f"{title}. {summary}"

    # 1. Hisse kodlarını tespit et
    tickers = extract_tickers(full_text)

    # 2. Makro haberleri tespit et
    macro_kws = detect_macro_keywords(full_text)
    is_macro = len(macro_kws) > 0

    # 3. Duygu analizi
    # Başlık genelde daha bilgilendirici, ona ağırlık ver
    score_title, _ = analyze_sentiment(title)
    score_full, _ = analyze_sentiment(full_text)
    # Ağırlıklı ortalama (başlık %60, tam metin %40)
    final_score = round(score_title * 0.6 + score_full * 0.4, 4)

    if final_score > 0.15:
        label = "positive"
    elif final_score < -0.15:
        label = "negative"
    else:
        label = "neutral"

    return {
        "news_id": news_dict.get("id"),
        "tickers": tickers,
        "is_macro": is_macro,
        "macro_keywords": macro_kws,
        "sentiment_score": final_score,
        "sentiment_label": label,
    }


if __name__ == "__main__":
    # Test
    test_news = [
        {
            "id": 1,
            "title": "Türk Hava Yolları rekor kâr açıkladı",
            "summary": "THY, 2025 yılında 5 milyar dolar net kâr elde ederek tarihi rekor kırdı."
        },
        {
            "id": 2,
            "title": "Orta Doğu'da savaş riski artıyor, piyasalar tedirgin",
            "summary": "Bölgesel gerginliklerin artmasıyla petrol fiyatları yükseldi, borsa endeksi geriledi."
        },
        {
            "id": 3,
            "title": "TCMB faiz kararını açıkladı",
            "summary": "Merkez Bankası politika faizini 50 baz puan indirdi. Piyasalar olumlu karşıladı."
        },
        {
            "id": 4,
            "title": "Aselsan'a 2 milyar dolarlık ihale",
            "summary": "ASELSAN savunma sanayi ihalesini kazandı. Şirket hisseleri yükselişe geçti."
        },
    ]

    print("🧪 NLP Modülü Test\n")
    for news in test_news:
        result = process_news_item(news)
        emoji = "🟢" if result["sentiment_label"] == "positive" else "🔴" if result["sentiment_label"] == "negative" else "⚪"
        print(f"\n{emoji} {news['title']}")
        print(f"   Skor: {result['sentiment_score']:.4f} ({result['sentiment_label']})")
        print(f"   Hisseler: {result['tickers'] or 'Genel piyasa'}")
        if result['is_macro']:
            print(f"   🌍 Makro: {', '.join(result['macro_keywords'])}")
