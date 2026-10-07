import json
import time
import urllib.request
import urllib.parse
import os


# ============================================================
# AYARLAR
# ============================================================

BYBIT_BASE = "https://api.bybit.com"

VOLUME_MULTIPLIER = 5.0
VOLUME_LOOKBACK = 20

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

# Kırılım sonrası fiyat LH'den %2'den fazla uzaklaşmışsa
# peşinden koşmuyoruz.
MAX_BREAKOUT_DISTANCE = 0.02

# Retest sırasında LH'nin %0.3 yakınına gelmesi yeterli.
RETEST_TOLERANCE = 0.003

# Çok fazla API isteği atmamak için en yüksek hacimli
# bu kadar coin taranacak.
MAX_SYMBOLS = 250


# ============================================================
# GENEL HTTP
# ============================================================

def get_json(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


# ============================================================
# BYBIT TICKERS
# ============================================================

def get_tickers():

    params = urllib.parse.urlencode({
        "category": "linear"
    })

    url = (
        f"{BYBIT_BASE}/v5/market/tickers?"
        f"{params}"
    )

    data = get_json(url)

    if data.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit ticker hatası: {data}"
        )

    tickers = {}

    for item in data["result"]["list"]:

        symbol = item["symbol"]

        # Sadece USDT perpetual piyasalar.
        if not symbol.endswith("USDT"):
            continue

        try:
            last_price = float(item["lastPrice"])
            turnover_24h = float(
                item.get("turnover24h", 0)
            )
        except (ValueError, TypeError):
            continue

        tickers[symbol] = {
            "price": last_price,
            "turnover": turnover_24h
        }

    return tickers


# ============================================================
# 15 DAKİKALIK MUM VERİSİ
# ============================================================

def get_klines(symbol):

    params = urllib.parse.urlencode({
        "category": "linear",
        "symbol": symbol,
        "interval": "15",
        "limit": 120
    })

    url = (
        f"{BYBIT_BASE}/v5/market/kline?"
        f"{params}"
    )

    data = get_json(url)

    if data.get("retCode") != 0:
        raise RuntimeError(
            f"{symbol} kline hatası: {data}"
        )

    raw = data["result"]["list"]

    candles = []

    # Bybit mumları yeniden eskiye sıralıyor.
    # Biz eski -> yeni kullanacağız.
    raw = list(reversed(raw))

    for k in raw:

        candles.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "turnover": float(k[6])
        })

    return candles


# ============================================================
# PIVOT HIGH BULMA
# ============================================================

def find_pivot_highs(candles):

    highs = [
        candle["high"]
        for candle in candles
    ]

    pivots = []

    start = PIVOT_LEFT
    end = len(highs) - PIVOT_RIGHT

    for i in range(start, end):

        left = highs[
            i - PIVOT_LEFT:i
        ]

        right = highs[
            i + 1:i + PIVOT_RIGHT + 1
        ]

        if not left or not right:
            continue

        if (
            highs[i] > max(left)
            and highs[i] >= max(right)
        ):

            pivots.append({
                "index": i,
                "price": highs[i]
            })

    return pivots


# ============================================================
# SON LOWER HIGH BULMA
# ============================================================

def find_last_lh(candles, before_index):

    pivots = find_pivot_highs(candles)

    valid = [
        pivot
        for pivot in pivots
        if pivot["index"] < before_index
    ]

    if len(valid) < 2:
        return None

    # En son oluşan Lower High'ı geriye doğru bul.
    for i in range(len(valid) - 1, 0, -1):

        previous = valid[i - 1]
        current = valid[i]

        if current["price"] < previous["price"]:

            return current

    return None


# ============================================================
# SİNYAL KONTROLÜ
# ============================================================

def find_signal(candles):

    # Son mum halen oluşuyor olabilir.
    # Bu yüzden son tamamlanmış mumu kullanıyoruz.
    signal_index = len(candles) - 2

    if signal_index < 30:
        return None

    # --------------------------------------------------------
    # SON LH
    # --------------------------------------------------------

    lh = find_last_lh(
        candles,
        signal_index
    )

    if lh is None:
        return None

    lh_price = lh["price"]

    # --------------------------------------------------------
    # SON 12 MUM İÇİNDE LH KIRILIMI ARA
    # --------------------------------------------------------

    search_start = max(
        lh["index"] + 1,
        signal_index - 12
    )

    breakout_index = None

    for i in range(
        search_start,
        signal_index + 1
    ):

        if i <= 0:
            continue

        previous_close = candles[i - 1]["close"]
        current_close = candles[i]["close"]

        # Önce LH altında/eşit,
        # sonra LH üzerinde kapanış.
        if (
            previous_close <= lh_price
            and current_close > lh_price
        ):

            breakout_index = i
            break

    if breakout_index is None:
        return None

    breakout = candles[breakout_index]

    # --------------------------------------------------------
    # KIRILIM MUMU ÇOK UZAMIŞ MI?
    # --------------------------------------------------------

    breakout_distance = (
        breakout["close"] - lh_price
    ) / lh_price

    if breakout_distance > MAX_BREAKOUT_DISTANCE:
        return None

    # --------------------------------------------------------
    # KIRILIM HACMİ
    # --------------------------------------------------------

    volume_start = (
        breakout_index - VOLUME_LOOKBACK
    )

    if volume_start < 0:
        return None

    previous_volumes = [
        candles[i]["volume"]
        for i in range(
            volume_start,
            breakout_index
        )
    ]

    if len(previous_volumes) < VOLUME_LOOKBACK:
        return None

    average_volume = (
        sum(previous_volumes)
        / len(previous_volumes)
    )

    if average_volume <= 0:
        return None

    volume_multiplier = (
        breakout["volume"]
        / average_volume
    )

    # En az 5x hacim.
    if volume_multiplier < VOLUME_MULTIPLIER:
        return None

    # --------------------------------------------------------
    # RETEST
    # --------------------------------------------------------

    # SADECE SON KAPANAN MUM retest yapmışsa alarm veriyoruz.
    #
    # Böylece aynı sinyal sonraki çalışmalarda tekrar
    # tekrar Telegram'a gitmez.
    retest = candles[signal_index]

    # Kırılım mumunun kendisi retest olamaz.
    if signal_index <= breakout_index:
        return None

    # Önceki mumlardan biri LH'nin altında kapanmışsa
    # kırılım başarısız kabul edilir.
    for i in range(
        breakout_index + 1,
        signal_index
    ):

        if candles[i]["close"] < lh_price:
            return None

    # --------------------------------------------------------
    # FİYAT LH'YE GERİ GELDİ Mİ?
    # --------------------------------------------------------

    touched_lh = (
        retest["low"]
        <= lh_price * (1 + RETEST_TOLERANCE)
    )

    if not touched_lh:
        return None

    # --------------------------------------------------------
    # RETEST MUMU LH'NİN ALTINDA KAPANMAMALI
    # --------------------------------------------------------

    if retest["close"] < lh_price:
        return None

    # --------------------------------------------------------
    # RETEST SONRASI GÜNCEL DURUM
    # --------------------------------------------------------

    current_price = retest["close"]

    return {
        "lh": lh_price,
        "breakout": breakout["close"],
        "retest": retest["close"],
        "current_price": current_price,
        "volume_multiplier": volume_multiplier,
        "breakout_distance": breakout_distance * 100,
        "breakout_index": breakout_index,
        "retest_index": signal_index
    }


# ============================================================
# FİYAT GÖRÜNÜMÜ
# ============================================================

def format_price(price):

    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:.4f}"

    if price >= 0.01:
        return f"{price:.6f}"

    if price >= 0.0001:
        return f"{price:.8f}"

    return f"{price:.10f}"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:

        raise RuntimeError(
            "Telegram secret bilgileri bulunamadı."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message
    }).encode()

    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        result = json.loads(
            response.read().decode("utf-8")
        )

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram hatası: {result}"
            )


# ============================================================
# ANA TARAMA
# ============================================================

def scan():

    print("=" * 60)
    print("CRYPTO ALERT BOT")
    print("BYBIT 15M LH + BREAKOUT + RETEST")
    print("=" * 60)

    print("\nBybit piyasa verileri alınıyor...")

    tickers = get_tickers()

    print(
        f"Toplam USDT sembolü: {len(tickers)}"
    )

    # 24 saatlik işlem hacmine göre sırala.
    sorted_symbols = sorted(
        tickers.keys(),
        key=lambda symbol:
            tickers[symbol]["turnover"],
        reverse=True
    )

    symbols = sorted_symbols[:MAX_SYMBOLS]

    print(
        f"Taranacak sembol sayısı: {len(symbols)}"
    )

    signals = []

    for number, symbol in enumerate(
        symbols,
        start=1
    ):

        try:

            candles = get_klines(symbol)

            signal = find_signal(candles)

            if signal is None:
                continue

            # Ticker'daki gerçek güncel fiyat.
            current_price = tickers[symbol]["price"]

            distance_now = (
                (
                    current_price
                    - signal["lh"]
                )
                / signal["lh"]
            ) * 100

            message = (
                "🚨 ERKEN LONG SİNYALİ\n\n"

                f"🪙 {symbol}\n"

                f"💰 Güncel fiyat: "
                f"{format_price(current_price)}\n\n"

                f"📌 LH: "
                f"{format_price(signal['lh'])}\n"

                f"📈 Kırılım: "
                f"{format_price(signal['breakout'])}\n"

                f"🔄 Retest: "
                f"{format_price(signal['retest'])}\n\n"

                f"📊 Kırılım hacmi: "
                f"{signal['volume_multiplier']:.1f}x\n"

                f"📍 Kırılım mesafesi: "
                f"{signal['breakout_distance']:.2f}%\n"

                f"📍 Güncel/LH mesafesi: "
                f"{distance_now:.2f}%\n\n"

                "🟢 LH kırıldı\n"
                "🟢 Fiyat LH bölgesine geri geldi\n"
                "🟢 LH destek olarak tutuldu\n"
                "🟢 Retest mumu LH üzerinde kapandı\n\n"

                "⏱️ Zaman dilimi: 15 dakika\n"

                "⚠️ Sinyal otomatik işlem değildir."
            )

            print("\n" + "=" * 60)
            print("SİNYAL BULUNDU:", symbol)
            print(message)
            print("=" * 60)

            send_telegram(message)

            signals.append(symbol)

            # API'yi gereksiz zorlamamak için.
            time.sleep(0.15)

        except Exception as error:

            print(
                f"[HATA] {symbol}: {error}"
            )

        # İlerlemeyi logda görelim.
        if number % 25 == 0:

            print(
                f"İlerleme: "
                f"{number}/{len(symbols)}"
            )

    print("\n" + "=" * 60)
    print("TARAMA TAMAMLANDI")
    print(
        f"Toplam sinyal: {len(signals)}"
    )

    if signals:
        print(
            "Sinyaller:",
            ", ".join(signals)
        )
    else:
        print(
            "Bu taramada şartları sağlayan "
            "coin bulunamadı."
        )

    print("=" * 60)


if __name__ == "__main__":
    scan()
