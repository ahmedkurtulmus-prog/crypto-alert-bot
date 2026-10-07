import os
import json
import time
import threading
from collections import defaultdict, deque

import requests
import websocket


# ============================================================
# AYARLAR
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Binance USD-M Futures
WS_API_URL = "wss://ws-fapi.binance.com/ws-fapi/v1"
WS_MARKET_BASE = "wss://fstream.binance.com/market/stream?streams="

INTERVAL = "15m"
LOOKBACK = 20
MULTIPLIER = 5.0

# Bir WebSocket bağlantısında kullanacağımız maksimum stream
MAX_STREAMS_PER_CONNECTION = 300


# ============================================================
# HAFIZA
# ============================================================

# Her coin için son 20 TAMAMLANMIŞ 15 dk mum hacmi
volume_history = defaultdict(
    lambda: deque(maxlen=LOOKBACK)
)

# Her coin için şu anda takip edilen mum
current_candle_start = {}

# Mevcut mumun son görülen hacmi
current_candle_volume = {}

# Aynı mumda ikinci alarmı engeller
alerted_candles = set()

alert_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

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

        print(
            "Telegram gönderme hatası:",
            e
        )


# ============================================================
# BINANCE WS API İLE TEK İSTEK
# ============================================================

def ws_api_request(ws, request_id, method, params=None):

    payload = {
        "id": request_id,
        "method": method,
        "params": params or {}
    }

    ws.send(
        json.dumps(payload)
    )

    while True:

        raw = ws.recv()

        if raw is None:
            raise RuntimeError(
                "Binance WS API bağlantısı kapandı."
            )

        data = json.loads(raw)

        # Bizim request'in cevabı mı?
        if data.get("id") != request_id:
            continue

        status = data.get("status")

        if status != 200:

            error = data.get(
                "error",
                {}
            )

            raise RuntimeError(
                f"Binance API hatası: "
                f"{error}"
            )

        return data.get(
            "result"
        )


# ============================================================
# TÜM AKTİF USDT PERPETUAL COINLERİ BUL
# ============================================================

def get_symbols_from_exchange_info():

    print("")
    print("==========================================")
    print("BINANCE FUTURES MARKET BİLGİSİ ALINIYOR")
    print("==========================================")

    ws = None

    try:

        ws = websocket.create_connection(
            WS_API_URL,
            timeout=30
        )

        result = ws_api_request(
            ws,
            1,
            "exchangeInfo"
        )

        symbols = []

        for item in result.get(
            "symbols",
            []
        ):

            if (
                item.get("status") == "TRADING"
                and item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
            ):

                symbols.append(
                    item["symbol"].lower()
                )

        symbols = sorted(
            set(symbols)
        )

        print(
            f"Bulunan aktif USDT perpetual: "
            f"{len(symbols)}"
        )

        if symbols:

            print(
                "İlk 20 coin:"
            )

            print(
                ", ".join(
                    symbols[:20]
                )
            )

        return symbols

    finally:

        if ws:

            try:
                ws.close()
            except Exception:
                pass


# ============================================================
# SON 20 TAMAMLANMIŞ 15 DK MUMUN HACİMLERİ
# ============================================================

def load_symbol_history(
    ws,
    symbol,
    request_id
):

    result = ws_api_request(
        ws,
        request_id,
        "klines",
        {
            "symbol": symbol.upper(),
            "interval": INTERVAL,
            "limit": LOOKBACK + 1
        }
    )

    if not result:

        return False

    now_ms = int(
        time.time() * 1000
    )

    completed = []

    for candle in result:

        # Binance kline:
        # [0] open time
        # [5] volume
        # [6] close time

        close_time = int(
            candle[6]
        )

        if close_time < now_ms:

            completed.append(
                float(candle[5])
            )

    if len(completed) < LOOKBACK:

        return False

    completed = completed[
        -LOOKBACK:
    ]

    volume_history[symbol].clear()

    volume_history[symbol].extend(
        completed
    )

    return True


# ============================================================
# TÜM COİNLERİN GEÇMİŞ HACİMLERİNİ HAZIRLA
# ============================================================

def load_all_histories(symbols):

    print("")
    print("==========================================")
    print("20 MUM HACİM ORTALAMALARI HAZIRLANIYOR")
    print("==========================================")

    ws = None

    success = 0
    failed = 0

    try:

        ws = websocket.create_connection(
            WS_API_URL,
            timeout=30
        )

        request_id = 100

        total = len(symbols)

        for index, symbol in enumerate(
            symbols,
            start=1
        ):

            try:

                ok = load_symbol_history(
                    ws,
                    symbol,
                    request_id
                )

                request_id += 1

                if ok:

                    success += 1

                else:

                    failed += 1

                if (
                    index % 25 == 0
                    or index == total
                ):

                    print(
                        f"Geçmiş veri: "
                        f"{index}/{total} | "
                        f"Hazır: {success} | "
                        f"Hata: {failed}"
                    )

                # Binance'a gereksiz yük bindirmemek
                time.sleep(0.03)

            except Exception as e:

                failed += 1

                print(
                    f"{symbol} geçmiş veri hatası: "
                    f"{e}"
                )

        print("")
        print(
            f"Geçmiş hacmi hazır: "
            f"{success}/{total}"
        )

        if success == 0:

            raise RuntimeError(
                "Hiçbir coin için geçmiş hacim alınamadı."
            )

    finally:

        if ws:

            try:
                ws.close()
            except Exception:
                pass


# ============================================================
# HACİM ALARMI
# ============================================================

def check_volume(
    symbol,
    candle_start,
    volume,
    price
):

    history = volume_history.get(
        symbol
    )

    if not history:

        return

    if len(history) < LOOKBACK:

        return

    average_volume = (
        sum(history)
        / len(history)
    )

    if average_volume <= 0:

        return

    ratio = (
        volume
        / average_volume
    )

    # ========================================================
    # 5X VE ÜZERİ
    # ========================================================

    if ratio < MULTIPLIER:

        return

    alert_key = (
        f"{symbol}:{candle_start}"
    )

    with alert_lock:

        if alert_key in alerted_candles:

            return

        alerted_candles.add(
            alert_key
        )

    message = (
        f"🔥 HACİM ALARMI\n\n"
        f"Coin: {symbol.upper()}\n"
        f"Periyot: 15 dakika\n"
        f"Fiyat: {price}\n\n"
        f"Mevcut hacim: "
        f"{volume:,.2f}\n"
        f"20 mum ortalaması: "
        f"{average_volume:,.2f}\n"
        f"Hacim oranı: "
        f"{ratio:.2f}X\n\n"
        f"🚨 5X VE ÜZERİ\n"
        f"⚠️ Mum kapanışı beklenmedi."
    )

    print("")
    print("==========================================")
    print("🔥🔥🔥 HACİM ALARMI")
    print(message)
    print("==========================================")

    threading.Thread(
        target=send_telegram,
        args=(message,),
        daemon=True
    ).start()


# ============================================================
# CANLI KLINE MESAJINI İŞLE
# ============================================================

def process_kline(data):

    try:

        k = data.get(
            "k"
        )

        if not k:

            return

        symbol = k[
            "s"
        ].lower()

        candle_start = int(
            k["t"]
        )

        volume = float(
            k["v"]
        )

        price = float(
            k["c"]
        )

        # Bu coin için geçmiş yoksa
        if symbol not in volume_history:

            return

        previous_start = (
            current_candle_start.get(
                symbol
            )
        )

        # ====================================================
        # YENİ 15 DK MUM BAŞLADI
        # ====================================================

        if (
            previous_start is not None
            and candle_start != previous_start
        ):

            previous_volume = (
                current_candle_volume.get(
                    symbol
                )
            )

            if previous_volume is not None:

                volume_history[
                    symbol
                ].append(
                    previous_volume
                )

        current_candle_start[
            symbol
        ] = candle_start

        current_candle_volume[
            symbol
        ] = volume

        # ====================================================
        # MUM KAPANMASINI BEKLEMİYORUZ
        # ====================================================

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


# ============================================================
# CANLI WEBSOCKET
# ============================================================

def run_stream_chunk(
    symbols,
    chunk_number
):

    streams = []

    for symbol in symbols:

        streams.append(
            f"{symbol}@kline_15m"
        )

    stream_url = (
        WS_MARKET_BASE
        + "/".join(streams)
    )

    print("")
    print(
        f"Canlı bağlantı {chunk_number}: "
        f"{len(symbols)} coin"
    )

    while True:

        ws = None

        try:

            ws = websocket.WebSocketApp(
                stream_url,

                on_open=lambda ws:
                    print(
                        f"Canlı bağlantı {chunk_number} açıldı."
                    ),

                on_message=lambda ws, message:
                    handle_stream_message(
                        message
                    ),

                on_error=lambda ws, error:
                    print(
                        f"Canlı bağlantı "
                        f"{chunk_number} hata:",
                        error
                    ),

                on_close=lambda ws, code, msg:
                    print(
                        f"Canlı bağlantı "
                        f"{chunk_number} kapandı:",
                        code,
                        msg
                    )
            )

            ws.run_forever(
                ping_interval=20,
                ping_timeout=10
            )

        except Exception as e:

            print(
                f"Canlı bağlantı "
                f"{chunk_number} exception:",
                e
            )

        print(
            f"Bağlantı {chunk_number} "
            f"5 saniye sonra yeniden denenecek..."
        )

        time.sleep(5)


# ============================================================
# CANLI MESAJ PARSE
# ============================================================

def handle_stream_message(
    message
):

    try:

        data = json.loads(
            message
        )

        payload = data.get(
            "data"
        )

        if not payload:

            return

        if payload.get(
            "e"
        ) != "kline":

            return

        process_kline(
            payload
        )

    except Exception as e:

        print(
            "Canlı mesaj parse hatası:",
            e
        )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    print("")
    print("############################################")
    print("#                                          #")
    print("#       CRYPTO FAST VOLUME ALERT           #")
    print("#                                          #")
    print("############################################")
    print("")
    print(
        "KURAL:"
    )
    print(
        "Mevcut 15 dk mum hacmi >= "
        "son 20 tamamlanmış mum ortalaması x 5"
    )
    print("")
    print(
        "5.00X VE ÜZERİ = ALARM"
    )
    print("")
    print(
        "MUM KAPANIŞI BEKLENMEYECEK."
    )
    print("")
    print(
        "LH / KIRILIM / RETEST YOK."
    )
    print("")

    # --------------------------------------------------------
    # 1. TÜM AKTİF USDT PERPETUAL MARKET
    # --------------------------------------------------------

    symbols = (
        get_symbols_from_exchange_info()
    )

    if not symbols:

        raise RuntimeError(
            "Binance Futures coinleri bulunamadı."
        )

    # --------------------------------------------------------
    # 2. SON 20 TAMAMLANMIŞ MUMUN HACİMLERİ
    # --------------------------------------------------------

    load_all_histories(
        symbols
    )

    # Sadece geçmiş verisi hazır olan coinleri takip et
    ready_symbols = [
        symbol
        for symbol in symbols
        if len(
            volume_history[symbol]
        ) >= LOOKBACK
    ]

    print("")
    print(
        f"Canlı takibe hazır coin: "
        f"{len(ready_symbols)}"
    )

    if not ready_symbols:

        raise RuntimeError(
            "Canlı takip için hazır coin yok."
        )

    # --------------------------------------------------------
    # 3. STREAMLERİ PARÇALARA AYIR
    # --------------------------------------------------------

    chunks = []

    for i in range(
        0,
        len(ready_symbols),
        MAX_STREAMS_PER_CONNECTION
    ):

        chunks.append(
            ready_symbols[
                i:
                i + MAX_STREAMS_PER_CONNECTION
            ]
        )

    print(
        f"Toplam canlı WebSocket bağlantısı: "
        f"{len(chunks)}"
    )

    print("")
    print(
        "🔥 CANLI HACİM TARAMASI BAŞLADI"
    )
    print(
        "🔥 5X VE ÜZERİ BEKLENİYOR"
    )
    print("")

    # --------------------------------------------------------
    # 4. HER PARÇAYI AYRI THREAD'DE DİNLE
    # --------------------------------------------------------

    threads = []

    for number, chunk in enumerate(
        chunks,
        start=1
    ):

        thread = threading.Thread(
            target=run_stream_chunk,
            args=(
                chunk,
                number
            ),
            daemon=True
        )

        thread.start()

        threads.append(
            thread
        )

        time.sleep(1)

    # --------------------------------------------------------
    # 5. PROGRAMI CANLI TUT
    # --------------------------------------------------------

    while True:

        time.sleep(60)


# ============================================================
# BAŞLAT
# ============================================================

if __name__ == "__main__":

    main()
