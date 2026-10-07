import os
import io
import csv
import json
import time
import zipfile
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import websocket


# ============================================================
# AYARLAR
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

WS_BASE = "wss://fstream.binance.com"
ALL_MARKET_STREAM = WS_BASE + "/ws/!miniTicker@arr"

INTERVAL = "15m"
LOOKBACK = 20
MULTIPLIER = 5.0

DATA_BASE = "https://data.binance.vision/data/futures/um/daily/klines"

MAX_SYMBOLS_PER_CONNECTION = 900

# Aynı mumda ikinci alarmı engeller
alerted_candles = set()
alert_lock = threading.Lock()

# Her coin için son 20 TAMAMLANMIŞ mumun hacimleri
volume_history = defaultdict(lambda: deque(maxlen=LOOKBACK))

# Her coin için mevcut mumun başlangıç zamanı
current_candle = {}

# Coinlerin aynı anda kaç kat olduğunu takip etmek için
last_print_time = 0


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=15
        )

        response.raise_for_status()
        print("Telegram gönderildi.")

    except Exception as e:
        print("Telegram gönderme hatası:", e)


# ============================================================
# BINANCE TÜM MARKET COINLERİNİ BUL
# ============================================================

def get_all_usdt_perpetual_symbols():

    print("Binance Futures marketi taranıyor...")

    symbols = set()

    def on_message(ws, message):
        nonlocal symbols

        try:
            data = json.loads(message)

            if not isinstance(data, list):
                return

            for item in data:

                symbol = item.get("s", "")

                if symbol.endswith("USDT"):
                    symbols.add(symbol.lower())

        except Exception as e:
            print("Market mesajı okunamadı:", e)

    def on_error(ws, error):
        print("Market WebSocket hatası:", error)

    def on_close(ws, code, msg):
        print("Market WebSocket kapandı.")

    ws = websocket.WebSocketApp(
        ALL_MARKET_STREAM,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    thread = threading.Thread(
        target=ws.run_forever,
        kwargs={
            "ping_interval": 20,
            "ping_timeout": 10
        },
        daemon=True
    )

    thread.start()

    # Market akışından coinleri toplamak için kısa süre bekle
    time.sleep(8)

    ws.close()

    symbols = sorted(symbols)

    print(f"Bulunan USDT market sayısı: {len(symbols)}")

    return symbols


# ============================================================
# ÖNCEKİ GÜNÜN 15 DK HACİMLERİNİ AL
# ============================================================

def download_previous_day_volumes(symbol):

    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

    date_string = yesterday.strftime("%Y-%m-%d")

    upper = symbol.upper()

    filename = f"{upper}-15m-{date_string}.zip"

    url = f"{DATA_BASE}/{upper}/15m/{filename}"

    try:

        response = requests.get(
            url,
            timeout=30
        )

        if response.status_code != 200:
            return symbol, []

        with zipfile.ZipFile(io.BytesIO(response.content)) as z:

            csv_name = z.namelist()[0]

            with z.open(csv_name) as f:

                reader = csv.reader(
                    io.TextIOWrapper(
                        f,
                        encoding="utf-8"
                    )
                )

                rows = list(reader)

        volumes = []

        for row in rows:

            if not row:
                continue

            try:
                volume = float(row[5])
                volumes.append(volume)

            except (ValueError, IndexError):
                continue

        # Son 20 tamamlanmış 15 dk mum
        volumes = volumes[-LOOKBACK:]

        return symbol, volumes

    except Exception as e:

        print(
            f"{symbol} geçmiş hacim alınamadı: {e}"
        )

        return symbol, []


def load_initial_history(symbols):

    print("")
    print("==========================================")
    print("GEÇMİŞ HACİM VERİLERİ HAZIRLANIYOR")
    print("==========================================")

    completed = 0

    # Aynı anda 20 coin indir
    with ThreadPoolExecutor(max_workers=20) as executor:

        futures = {
            executor.submit(
                download_previous_day_volumes,
                symbol
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):

            symbol = futures[future]

            try:

                symbol, volumes = future.result()

                if len(volumes) >= LOOKBACK:

                    volume_history[symbol].extend(volumes)

                    completed += 1

            except Exception as e:

                print(
                    f"{symbol} geçmiş veri hatası: {e}"
                )

    print(
        f"Geçmiş hacmi hazır olan coin: "
        f"{completed}/{len(symbols)}"
    )

    print("==========================================")


# ============================================================
# ALARM KONTROLÜ
# ============================================================

def check_volume(symbol, candle_start, volume, price):

    history = volume_history.get(symbol)

    if not history:
        return

    if len(history) < LOOKBACK:
        return

    average_volume = sum(history) / len(history)

    if average_volume <= 0:
        return

    ratio = volume / average_volume

    # 5X VE ÜZERİ
    if ratio >= MULTIPLIER:

        alert_key = f"{symbol}:{candle_start}"

        with alert_lock:

            if alert_key in alerted_candles:
                return

            alerted_candles.add(alert_key)

        message = (
            f"🔥 HACİM ALARMI\n\n"
            f"Coin: {symbol.upper()}\n"
            f"Periyot: 15 dakika\n"
            f"Fiyat: {price}\n\n"
            f"Mevcut hacim: {volume:,.2f}\n"
            f"20 mum ortalaması: {average_volume:,.2f}\n"
            f"Hacim oranı: {ratio:.2f}X\n\n"
            f"🚨 5X VE ÜZERİ\n"
            f"⚠️ Mum henüz kapanmamış olabilir."
        )

        print("")
        print("==========================================")
        print("🔥 ALARM")
        print(message)
        print("==========================================")

        threading.Thread(
            target=send_telegram,
            args=(message,),
            daemon=True
        ).start()


# ============================================================
# 15 DK KLINE MESAJI
# ============================================================

def process_kline(data):

    try:

        k = data.get("k")

        if not k:
            return

        symbol = k["s"].lower()

        candle_start = int(k["t"])

        volume = float(k["v"])

        price = float(k["c"])

        # Yeni 15 dk mum başladıysa
        previous_candle = current_candle.get(symbol)

        if previous_candle is not None:

            if candle_start != previous_candle:

                # Önceki mumun son hacmini geçmişe ekle
                previous_volume = last_candle_volume.get(
                    symbol
                )

                if previous_volume is not None:

                    volume_history[symbol].append(
                        previous_volume
                    )

        current_candle[symbol] = candle_start

        last_candle_volume[symbol] = volume

        # ÖNEMLİ:
        # Burada x değerine bakmıyoruz.
        # Mum kapanmadan da alarm verebilir.
        check_volume(
            symbol,
            candle_start,
            volume,
            price
        )

    except Exception as e:

        print(
            "Kline işleme hatası:",
            e
        )


# Bu sözlük yukarıda tanımlanıyor
last_candle_volume = {}


# ============================================================
# WEBSOCKET
# ============================================================

def run_websocket(symbols):

    streams = []

    for symbol in symbols:

        streams.append(
            f"{symbol}@kline_15m"
        )

    ws_url = (
        WS_BASE
        + "/stream?streams="
        + "/".join(streams)
    )

    print("")
    print("==========================================")
    print("CANLI BINANCE 15 DK HACİM TAKİBİ BAŞLADI")
    print("==========================================")
    print(
        f"Takip edilen coin: {len(symbols)}"
    )
    print(
        "Alarm şartı: 20 mum ortalaması x 5"
    )
    print(
        "Mum kapanışı beklenmiyor."
    )
    print("==========================================")

    def on_message(ws, message):

        try:

            data = json.loads(message)

            payload = data.get("data")

            if payload:

                process_kline(payload)

        except Exception as e:

            print(
                "WebSocket mesaj hatası:",
                e
            )

    def on_error(ws, error):

        print(
            "WebSocket hata:",
            error
        )

    def on_close(ws, code, msg):

        print(
            "WebSocket kapandı:",
            code,
            msg
        )

    def on_open(ws):

        print(
            "Binance WebSocket bağlantısı açıldı."
        )

    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever(
        ping_interval=20,
        ping_timeout=10
    )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    print("")
    print("##########################################")
    print("# CRYPTO FAST VOLUME ALERT")
    print("##########################################")
    print("")
    print(
        "Kural:"
    )
    print(
        "Mevcut 15m hacim >= "
        "son 20 mum ortalaması x 5"
    )
    print("")
    print(
        "MUM KAPANIŞI BEKLENMEYECEK."
    )
    print("")
    print(
        "Tüm Binance Futures USDT perpetual market"
    )
    print("")

    symbols = get_all_usdt_perpetual_symbols()

    if not symbols:

        raise RuntimeError(
            "Binance Futures coinleri bulunamadı."
        )

    # Geçmiş hacim ortalamalarını hazırla
    load_initial_history(symbols)

    # Canlı takip
    run_websocket(symbols)


if __name__ == "__main__":
    main()
