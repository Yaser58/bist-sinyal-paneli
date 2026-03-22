"""
BIST Haber Analiz Sistemi - Canlı İzleme Servisi (Live Monitor) v2.0
====================================================================
Tüm modülleri birleştirir. Somut sinyaller üretir:
  - Hisse kodu + yön (YÜKSELİŞ/DÜŞÜŞ)
  - Tarih aralığı (başlangıç → bitiş)
  - Beklenen % değişim
  - Sinyalleri takip edip sonuçları kontrol etme

Bu dosya ana giriş noktasıdır: python main.py
"""

import time
import json
import sys
from datetime import datetime

from config import FETCH_INTERVAL_MINUTES, BIST_TICKERS, TICKER_NAMES
from database import init_db, get_unprocessed_news, update_news_sentiment, insert_alert
from news_fetcher import fetch_all_feeds, fetch_kap_notifications
from price_fetcher import fetch_all_historical_prices, fetch_latest_prices
from nlp_engine import process_news_item
from analysis_engine import run_backtest, print_statistics_report
from signal_generator import (
    init_signals_table, generate_signal, generate_macro_signal,
    print_signal, print_active_signals_report, print_signal_history,
    check_signal_results, get_signal_success_rate
)


def print_banner():
    """Başlangıç banner'ı."""
    print(f"""
\033[96m\033[1m
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   ██████╗ ██╗███████╗████████╗  ███████╗██╗███╗   ██╗██╗   ║
║   ██╔══██╗██║██╔════╝╚══██╔══╝  ██╔════╝██║████╗  ██║██║   ║
║   ██████╔╝██║███████╗   ██║     ███████╗██║██╔██╗ ██║██║   ║
║   ██╔══██╗██║╚════██║   ██║     ╚════██║██║██║╚██╗██║██║   ║
║   ██████╔╝██║███████║   ██║     ███████║██║██║ ╚████║███║  ║
║   ╚═════╝ ╚═╝╚══════╝   ╚═╝     ╚══════╝╚═╝╚═╝  ╚═══╝╚══╝  ║
║                                                              ║
║   📡 Sinyal Üretici & Haber Analiz Sistemi v2.0             ║
║   🏦 Borsa İstanbul (BIST) - Canlı İzleme                  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
\033[0m""")


def process_and_generate_signals():
    """
    İşlenmemiş haberleri alır, NLP ile analiz eder,
    ve somut SİNYALLER üretir.
    """
    unprocessed = get_unprocessed_news()
    if not unprocessed:
        print(f"  ℹ️  İşlenecek yeni haber yok.")
        return 0

    print(f"\n  🧠 {len(unprocessed)} haber analiz ediliyor...\n")

    signal_count = 0
    for news in unprocessed:
        # NLP analizi
        result = process_news_item(news)

        # Veritabanını güncelle
        tickers_json = json.dumps(result["tickers"])
        macro_json = json.dumps(result["macro_keywords"]) if result["macro_keywords"] else None

        update_news_sentiment(
            news_id=result["news_id"],
            sentiment_score=result["sentiment_score"],
            sentiment_label=result["sentiment_label"],
            related_tickers=tickers_json,
            is_macro=result["is_macro"],
            macro_keywords=macro_json
        )

        # Nötr haberleri atla
        if result["sentiment_label"] == "neutral" and not result["is_macro"]:
            continue

        target_tickers = result["tickers"]

        # ── Makro haber ise tüm piyasaya sinyal ──
        if result["is_macro"] and not target_tickers:
            signals = generate_macro_signal(
                sentiment_score=result["sentiment_score"],
                sentiment_label=result["sentiment_label"],
                news_title=news["title"]
            )
            for sig in signals:
                print_signal(sig)
                signal_count += 1
            continue

        # ── Hisse bazlı sinyal ──
        for ticker_code in target_tickers:
            sig = generate_signal(
                ticker_code=ticker_code,
                sentiment_score=result["sentiment_score"],
                sentiment_label=result["sentiment_label"],
                news_title=news["title"],
                news_summary=news.get("summary", "")
            )
            if sig:
                print_signal(sig)
                signal_count += 1

    if signal_count > 0:
        print(f"\n  🔔 Toplam {signal_count} yeni sinyal üretildi!")
    else:
        print(f"  ℹ️  Yeterli güçte sinyal bulunamadı (tümü zayıf/nötr).")

    return signal_count


def initial_setup():
    """İlk kurulum."""
    print(f"\n\033[93m{'='*60}\033[0m")
    print(f"  ⚙️  İLK KURULUM")
    print(f"\033[93m{'='*60}\033[0m")

    print("\n  [1/5] Veritabanı oluşturuluyor...")
    init_db()
    init_signals_table()

    print("\n  [2/5] Geçmiş fiyat verileri çekiliyor (biraz sürebilir)...")
    fetch_all_historical_prices()

    print("\n  [3/5] Güncel haberler toplanıyor...")
    fetch_kap_notifications()
    fetch_all_feeds()

    print("\n  [4/5] Haberler analiz ediliyor ve sinyaller üretiliyor...")
    process_and_generate_signals()

    print("\n  [5/5] Geriye dönük test çalıştırılıyor...")
    run_backtest()

    print(f"\n\033[92m  ✅ İlk kurulum tamamlandı!\033[0m")
    print(f"  Artık '2' ile canlı izleme moduna geçebilirsiniz.\n")


def live_monitor():
    """
    Ana canlı izleme döngüsü.
    Her FETCH_INTERVAL_MINUTES dakikada bir:
    1. Haberleri çeker
    2. NLP ile analiz eder  
    3. SİNYAL üretir
    4. Eski sinyallerin sonuçlarını kontrol eder
    """
    init_signals_table()
    
    print(f"\n\033[96m{'='*60}\033[0m")
    print(f"  🔴 CANLI İZLEME MODU AKTİF")
    print(f"  📡 Her {FETCH_INTERVAL_MINUTES} dakikada bir haber + sinyal kontrolü")
    print(f"  ⏹️  Durdurmak için Ctrl+C")
    print(f"\033[96m{'='*60}\033[0m")

    # Başlangıçta aktif sinyalleri göster
    print_active_signals_report()

    cycle = 0
    while True:
        try:
            cycle += 1
            print(f"\n{'─'*50}")
            print(f"  🔄 Döngü #{cycle} - {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'─'*50}")

            # 1. Fiyatları güncelle
            fetch_latest_prices()

            # 2. Süresi dolan sinyalleri kontrol et
            results = check_signal_results()
            if results:
                print(f"\n  📋 {len(results)} sinyalin süresi doldu:")
                for r in results:
                    print(f"     {r['result']} {r['ticker']}: "
                          f"Beklenen %{r['expected']:+.2f} → Gerçek %{r['actual']:+.2f}")

            # 3. KAP + haber çek
            kap_count = fetch_kap_notifications()
            news_count = fetch_all_feeds()

            # 4. Analiz et ve SİNYAL üret
            signal_count = process_and_generate_signals()

            # 5. Aktif sinyalleri özetle
            if signal_count > 0 or cycle % 5 == 0:
                print_active_signals_report()

            # Bekle
            print(f"\n  ⏳ Sonraki kontrol: {FETCH_INTERVAL_MINUTES} dakika sonra...")
            time.sleep(FETCH_INTERVAL_MINUTES * 60)

        except KeyboardInterrupt:
            print(f"\n\n\033[93m  ⏹️  Canlı izleme durduruldu.\033[0m")
            print_active_signals_report()
            break
        except Exception as e:
            print(f"\n  \033[91m[HATA] {e}\033[0m")
            print(f"  30 saniye sonra tekrar denenecek...")
            time.sleep(30)


def main():
    """Ana giriş noktası."""
    print_banner()

    print(f"  Seçenekler:")
    print(f"    1. 🚀 İlk Kurulum (veri çek + analiz + sinyal üret)")
    print(f"    2. 🔴 Canlı İzleme Başlat (otomatik sinyal üretimi)")
    print(f"    3. 📡 Aktif Sinyalleri Göster")
    print(f"    4. 📜 Sinyal Geçmişi ve Başarı Oranı")
    print(f"    5. 📊 İstatistik Raporu")
    print(f"    6. 📰 Sadece Haber Çek + Sinyal Üret")
    print(f"    7. 🔬 Geriye Dönük Test (Backtest)")
    print(f"    8. 🧪 NLP Test")
    print(f"")

    if len(sys.argv) > 1:
        choice = sys.argv[1]
    else:
        try:
            choice = input(f"  Seçiminiz (1-8): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Çıkış.")
            return

    if choice == "1":
        initial_setup()
    elif choice == "2":
        init_db()
        live_monitor()
    elif choice == "3":
        init_db()
        init_signals_table()
        print_active_signals_report()
    elif choice == "4":
        init_db()
        init_signals_table()
        print_signal_history()
        stats = get_signal_success_rate()
        if stats["total"] > 0:
            print(f"\n  📊 Başarı Oranı: %{stats['rate']:.1f} "
                  f"({stats['success']}/{stats['total']})")
    elif choice == "5":
        init_db()
        print_statistics_report()
    elif choice == "6":
        init_db()
        init_signals_table()
        fetch_kap_notifications()
        fetch_all_feeds()
        process_and_generate_signals()
    elif choice == "7":
        init_db()
        run_backtest()
        print_statistics_report()
    elif choice == "8":
        from nlp_engine import process_news_item
        test_news = [
            {"id": 0, "title": "Türk Hava Yolları rekor kâr açıkladı",
             "summary": "THY 5 milyar dolar net kâr elde etti."},
            {"id": 0, "title": "Orta Doğu'da savaş riski artıyor",
             "summary": "Bölgesel çatışma genişleyebilir, petrol fırladı."},
            {"id": 0, "title": "TCMB faiz indirdi: 50 baz puan",
             "summary": "Merkez Bankası faizi düşürdü."},
        ]
        print("\n🧪 NLP Test:\n")
        for news in test_news:
            r = process_news_item(news)
            emoji = "🟢" if r["sentiment_label"] == "positive" else "🔴" if r["sentiment_label"] == "negative" else "⚪"
            print(f"  {emoji} {news['title']}")
            print(f"     Skor: {r['sentiment_score']:+.4f} | Hisseler: {r['tickers'] or 'Genel'}")
            if r['macro_keywords']:
                print(f"     🌍 Makro: {', '.join(r['macro_keywords'])}")
            print()
    else:
        print("  Geçersiz seçim.")


if __name__ == "__main__":
    main()
