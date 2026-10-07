import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone


# ============================================================
# AYARLAR
# ============================================================

TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None

BINANCE_BASE = "https://fapi.binance.com"

VOLUME_MULTIPLIER = 5.0
VOLUME_LOOKBACK = 20

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

# LH kırıldıktan sonra fiyatın LH'den en fazla ne kadar
# yukarıda olmasına izin veriyoruz.
MAX_BREAKOUT_DISTANCE = 0.02      # %2

# Retest sırasında LH'nin biraz altına sarkmasına
# küçük bir tolerans veriyoruz.
RETEST_TOLERANCE = 0.003           # %0.3


# ============================================================
# BINANCE VERİ ÇEKME
# ============================================================

def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get_symbols():
    url = f"{BINANCE_BASE}/fapi/v1/exchangeInfo"
    data = get_json(url)

    symbols = []

    for item in data["symbols"]:
        if (
            item["status"] == "TRADING"
            and item["contractType"] == "PERPETUAL"
            and item["quoteAsset"] == "USDT"
        ):
            symbols.append(item["symbol"])

    return symbols


def get_klines(symbol, limit=120):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "15m",
        "limit": limit
    })

    url = f"{BINANCE_BASE}/fapi/v1/klines?{params}"

    data = get_json(url)

    candles = []

    for k in data:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })

    return candles


def get_current_prices():
    url = f"{BINANCE_BASE}/fapi/v1/ticker/price"

    data = get_json(url)

    prices = {}

    for item in data:
        prices[item["symbol"]] = float(item["price"])

    return prices


# ============================================================
# PIVOT / LH BULMA
# ============================================================

def find_pivot_highs(candles):
    highs = [c["high"] for c in candles]

    pivots = []

    start = PIVOT_LEFT
    end = len(highs) - PIVOT_RIGHT

    for i in range(start, end):

        left_side = highs[i - PIVOT_LEFT:i]
        right_side = highs[i + 1:i + PIVOT_RIGHT + 1]

        if not left_side or not right_side:
            continue

        if (
            highs[i] > max(left_side)
            and highs[i] >= max(right_side)
        ):
            pivots.append({
                "index": i,
                "price": highs[i]
            })

    return pivots


def find_last_lh(candles, signal_index):
    pivots = find_pivot_highs(candles)

    # Sadece sinyal mumundan önce oluşmuş pivotları kullan.
    valid_pivots = [
        p for p in pivots
        if p["index"] < signal_index
    ]

    if len(valid_pivots) < 2:
        return None

    # En son oluşan Lower High'ı geriye doğru ara.
    for i in range(len(valid_pivots) - 1, 0, -1):

        previous = valid_pivots[i - 1]
        current = valid_pivots[i]

        if current["price"] < previous["price"]:

            return {
                "index": current["index"],
                "price": current["price"]
            }

    return None


# ============================================================
# RETEST KONTROLÜ
# ============================================================

def check_signal(candles):

    # Son mum halen oluşuyor olabilir.
    # Bu nedenle -2 = son kapanmış 15 dakikalık mum.
    signal_index = len(candles) - 2

    if signal_index < VOLUME_LOOKBACK + 5:
        return None

    signal = candles[signal_index]

    # ========================================================
    # 1. HACİM
    # ========================================================

    previous_volumes = [
        candles[i]["volume"]
        for i in range(
            signal_index - VOLUME_LOOKBACK,
            signal_index
        )
    ]

    average_volume = sum(previous_volumes) / len(previous_volumes)

    if average_volume <= 0:
        return None

    volume_multiplier = signal["volume"] / average_volume

    # İlk şart:
    # Son kapanan mum 20 mum ortalamasının en az 5 katı.
    if volume_multiplier < VOLUME_MULTIPLIER:
        return None

    # ========================================================
    # 2. SON LH
    # ========================================================

    lh = find_last_lh(candles, signal_index)

    if lh is None:
        return None

    lh_price = lh["price"]

    # ========================================================
    # 3. LH KIRILIMI
    # ========================================================

    previous_candle = candles[signal_index - 1]

    # Önceki mum LH altında/eşit,
    # sinyal mumu LH üzerinde kapanmalı.
    breakout = (
        previous_candle["close"] <= lh_price
        and signal["close"] > lh_price
    )

    if not breakout:
        return None

    # ========================================================
    # 4. KIRILIM ÇOK UZAMIŞ MI?
    # ========================================================

    breakout_distance = (
        signal["close"] - lh_price
    ) / lh_price

    # %2'den fazla yukarı kaçmışsa peşinden koşmuyoruz.
    if breakout_distance > MAX_BREAKOUT_DISTANCE:
        return None

    # ========================================================
    # 5. RETEST AŞAMASI
    # ========================================================

    # Burada henüz alarm vermiyoruz.
    # Önümüzdeki mumlarda LH'nin test edilmesini bekleyeceğiz.

    return {
        "lh": lh_price,
        "breakout_index": signal_index,
        "breakout_close": signal["close"],
        "volume_multiplier": volume_multiplier
    }


# ============================================================
# RETEST SONRASI SİNYAL
# ============================================================

def find_retest_signal(candles):

    signal_index = len(candles) - 2

    if signal_index < VOLUME_LOOKBACK + 10:
        return None

    # Son kapanmış mumdan geriye doğru
    # yakın geçmişte oluşmuş LH'ları kontrol ediyoruz.
    pivots = find_pivot_highs(candles)

    valid_pivots = [
        p for p in pivots
        if p["index"] < signal_index
    ]

    if len(valid_pivots) < 2:
        return None

    # Son LH
    lh = None

    for i in range(len(valid_pivots) - 1, 0, -1):

        previous = valid_pivots[i - 1]
        current = valid_pivots[i]

        if current["price"] < previous["price"]:
            lh = current
            break

    if lh is None:
        return None

    lh_price = lh["price"]

    # ========================================================
    # LH KIRILIMINDAN SONRAKİ MUMU BUL
    # ========================================================

    breakout_index = None

    # Son 12 adet kapanmış mum içinde kırılım ara.
    search_start = max(
        lh["index"] + PIVOT_RIGHT + 1,
        signal_index - 12
    )

    for i in range(search_start, signal_index + 1):

        if i <= 0:
            continue

        previous_close = candles[i - 1]["close"]
        current_close = candles[i]["close"]

        if (
            previous_close <= lh_price
            and current_close > lh_price
        ):
            breakout_index = i
            break

    if breakout_index is None:
        return None

    # Kırılım mumu çok uzamışsa işlem yok.
    breakout_candle = candles[breakout_index]

    breakout_distance = (
        breakout_candle["close"] - lh_price
    ) / lh_price

    if breakout_distance > MAX_BREAKOUT_DISTANCE:
        return None

    # ========================================================
    # RETEST KONTROLÜ
    # ========================================================

    # Kırılımdan sonra oluşan mumları kontrol ediyoruz.
    retest_candles = candles[
        breakout_index + 1:signal_index + 1
    ]

    if not retest_candles:
        return None

    for candle in retest_candles:

        # Fiyat LH'ye kadar geri gelmiş mi?
        touched_lh = (
            candle["low"]
            <= lh_price * (1 + RETEST_TOLERANCE)
        )

        if not touched_lh:
            continue

        # LH'nin altında çok güçlü kapanış yapmışsa
        # sahte kırılım kabul ediyoruz.
        invalid_close = (
            candle["close"]
            < lh_price
        )

        if invalid_close:
            continue

        # Destek tuttu.
        # Şimdi bu mumun hacmini de kontrol ediyoruz.
        previous_volumes = [
            candles[i]["volume"]
            for i in range(
                max(0, breakout_index - VOLUME_LOOKBACK),
                breakout_index
            )
        ]

        if len(previous_volumes) < VOLUME_LOOKBACK:
            continue

        average_volume = (
            sum(previous_volumes)
            / len(previous_volumes)
        )

        volume_multiplier = (
            breakout_candle["volume"]
            / average_volume
            if average_volume > 0
            else 0
        )

        if volume_multiplier < VOLUME_MULTIPLIER:
            continue

        return {
            "lh": lh_price,
            "breakout_index": breakout_index,
            "retest_index": candles.index(candle),
            "breakout_close": breakout_candle["close"],
            "retest_close": candle["close"],
            "volume_multiplier": volume_multiplier
        }

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram bilgileri bulunamadı.")
        return

    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message
    }).encode()

    request = urllib.request.Request(
        url,
        data=data,
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        print(response.read().decode())


# ============================================================
# TARAMA
# ============================================================

def scan():

    print("======================================")
    print("CRYPTO ALERT BOT")
    print("15M LH BREAK + RETEST SCANNER")
    print("======================================")

    symbols = get_symbols()

    print(f"Toplam sembol: {len(symbols)}")

    prices = get_current_prices()

    signals = []

    for number, symbol in enumerate(symbols, start=1):

        try:

            candles = get_klines(symbol)

            signal = find_retest_signal(candles)

            if signal is None:
                continue

            current_price = prices.get(symbol)

            if current_price is None:
                continue

            distance_from_lh = (
                (current_price - signal["lh"])
                / signal["lh"]
            ) * 100

            message = (
                "🚨 ERKEN LONG SİNYALİ\n\n"
                f"🪙 {symbol}\n"
                f"💰 Güncel fiyat: {current_price:.8f}\n\n"
                f"📌 LH: {signal['lh']:.8f}\n"
                f"📈 Kırılım: {signal['breakout_close']:.8f}\n"
                f"🔄 Retest: {signal['retest_close']:.8f}\n\n"
                f"📊 Kırılım hacmi: "
                f"{signal['volume_multiplier']:.1f}x\n"
                f"📍 LH'ye mesafe: "
                f"{distance_from_lh:.2f}%\n\n"
                "🟢 LH kırıldı\n"
                "🟢 LH retest edildi\n"
                "🟢 Destek olarak tuttu\n\n"
                "⏱️ Zaman dilimi: 15 dakika"
            )

            print("\nSİNYAL:", symbol)
            print(message)

            send_telegram(message)

            signals.append(symbol)

            # Telegram'a arka arkaya çok hızlı istek gitmesin.
            time.sleep(0.2)

        except Exception as error:

            print(
                f"[HATA] {symbol}: {error}"
            )

    print("\n======================================")
    print(f"Tarama tamamlandı.")
    print(f"Sinyal sayısı: {len(signals)}")
    print("======================================")


if __name__ == "__main__":
    scan()
