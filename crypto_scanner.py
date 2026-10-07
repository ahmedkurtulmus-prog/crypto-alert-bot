import csv
import io
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone

import websocket


# =========================================================
# AYARLAR
# =========================================================

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LINKUSDT",
]

INTERVAL = "15m"

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

VOLUME_LOOKBACK = 20
VOLUME_MULTIPLIER = 5.0

# LH'ye %0.30 yaklaşması yeterli
RETEST_TOLERANCE = 0.003

# Retest sonrası LH'nin %0.30 altında kapanırsa başarısız
FAIL_TOLERANCE = 0.003

STATE_FILE = "scanner_state.json"


# =========================================================
# BINANCE GEÇMİŞ VERİ
# =========================================================

def download_day(symbol, date):

    date_str = date.strftime("%Y-%m-%d")

    url = (
        f"https://data.binance.vision/"
        f"data/futures/um/daily/klines/"
        f"{symbol}/{INTERVAL}/"
        f"{symbol}-{INTERVAL}-{date_str}.zip"
    )

    print(f"{symbol} geçmiş veri: {date_str}")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    response = urllib.request.urlopen(
        request,
        timeout=30
    )

    data = response.read()

    with zipfile.ZipFile(io.BytesIO(data)) as z:

        filename = z.namelist()[0]

        with z.open(filename) as f:

            rows = list(
                csv.reader(
                    io.TextIOWrapper(f)
                )
            )

    candles = []

    for row in rows:

        if not row:
            continue

        if row[0].lower() in ("open time", "open_time"):
            continue

        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })

    return candles


def load_initial_history(symbol):

    now = datetime.now(timezone.utc)

    all_candles = []

    # Günlük arşiv bir sonraki gün oluştuğu için
    # son iki tamamlanmış günü alıyoruz.
    for days_ago in [2, 1]:

        date = (
            now - timedelta(days=days_ago)
        ).date()

        try:

            candles = download_day(
                symbol,
                date
            )

            all_candles.extend(candles)

        except Exception as e:

            print(
                f"{symbol} {date} alınamadı: {e}"
            )

    all_candles.sort(
        key=lambda x: x["time"]
    )

    # Son 250 mum yeterli
    return all_candles[-250:]


# =========================================================
# CANLI 15M KAPANMIŞ MUM
# =========================================================

def get_next_closed_candles():

    streams = "/".join(
        f"{symbol.lower()}@kline_15m"
        for symbol in SYMBOLS
    )

    url = (
        "wss://fstream.binance.com/"
        "market/stream?streams="
        + streams
    )

    print("")
    print("Binance WebSocket'e bağlanılıyor...")

    ws = websocket.create_connection(
        url,
        timeout=60
    )

    print("WebSocket BAĞLANDI.")

    closed = {}

    while len(closed) < len(SYMBOLS):

        message = ws.recv()

        data = json.loads(message)

        if "data" not in data:
            continue

        kline = data["data"].get("k")

        if not kline:
            continue

        symbol = kline["s"]

        # Sadece kapanmış mum
        if not kline["x"]:
            continue

        candle = {
            "time": int(kline["t"]),
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
        }

        closed[symbol] = candle

        print(
            f"{symbol} 15m mum kapandı | "
            f"Kapanış: {candle['close']}"
        )

    ws.close()

    return closed


# =========================================================
# PIVOT HIGH
# =========================================================

def find_pivot_highs(candles):

    pivots = []

    start = PIVOT_LEFT
    end = len(candles) - PIVOT_RIGHT

    for i in range(start, end):

        current_high = candles[i]["high"]

        left = [
            candles[j]["high"]
            for j in range(
                i - PIVOT_LEFT,
                i
            )
        ]

        right = [
            candles[j]["high"]
            for j in range(
                i + 1,
                i + PIVOT_RIGHT + 1
            )
        ]

        if (
            current_high > max(left)
            and
            current_high >= max(right)
        ):

            pivots.append({
                "index": i,
                "price": current_high,
                "time": candles[i]["time"]
            })

    return pivots


# =========================================================
# LOWER HIGH
# =========================================================

def find_last_lower_high(candles):

    # Son mum breakout/retest için kullanılacak.
    # Pivot hesabında son 2 mumu kullanmıyoruz.
    usable = candles[:-1]

    if len(usable) < 20:
        return None

    pivots = find_pivot_highs(
        usable
    )

    if len(pivots) < 2:
        return None

    # En yeni LH'yi bul
    for i in range(
        len(pivots) - 1,
        0,
        -1
    ):

        previous = pivots[i - 1]
        current = pivots[i]

        if current["price"] < previous["price"]:

            return {
                "price": current["price"],
                "time": current["time"],
                "previous_high": previous["price"]
            }

    return None


# =========================================================
# STATE
# =========================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "symbols": {}
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {
            "symbols": {}
        }


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2
        )


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:

        print(
            "Telegram secret bulunamadı!"
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }).encode()

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = json.loads(
                response.read()
            )

        return result.get(
            "ok",
            False
        )

    except Exception as e:

        print(
            "Telegram gönderilemedi:",
            e
        )

        return False


# =========================================================
# GITHUB STATE KAYDET
# =========================================================

def commit_state():

    try:

        subprocess.run(
            [
                "git",
                "config",
                "user.name",
                "crypto-alert-bot"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "crypto-alert-bot@users.noreply.github.com"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "add",
                STATE_FILE
            ],
            check=True
        )

        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--quiet"
            ])

        # Değişiklik yok
        if result.returncode == 0:

            print(
                "State değişmedi."
            )

            return

        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "Update scanner state"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "push"
            ],
            check=True
        )

        print(
            "Scanner state GitHub'a kaydedildi."
        )

    except Exception as e:

        print(
            "State GitHub'a kaydedilemedi:",
            e
        )


# =========================================================
# SEMBOL TARAMA
# =========================================================

def scan_symbol(
    symbol,
    candles,
    state
):

    if len(candles) < 30:

        print(
            symbol,
            "→ yeterli mum yok."
        )

        return

    candles.sort(
        key=lambda x: x["time"]
    )

    # Aynı mumu iki kere ekleme
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(
        unique.values()
    )

    candles.sort(
        key=lambda x: x["time"]
    )

    candles = candles[-250:]

    latest = candles[-1]

    symbol_state = state["symbols"].setdefault(
        symbol,
        {
            "candles": [],
            "pending": None,
            "last_processed": 0
        }
    )

    # Aynı mum daha önce işlendi mi?
    if latest["time"] <= symbol_state["last_processed"]:

        print(
            symbol,
            "→ bu mum zaten işlendi."
        )

        return

    # =====================================================
    # ÖNCE PENDING BREAKOUT KONTROLÜ
    # =====================================================

    pending = symbol_state.get(
        "pending"
    )

    if pending:

        lh = pending["lh"]

        # Fiyat LH'ye geri geldi mi?
        touched = (
            latest["low"]
            <=
            lh * (1 + RETEST_TOLERANCE)
        )

        # LH'nin altında belirgin kapanış
        failed = (
            latest["close"]
            <
            lh * (1 - FAIL_TOLERANCE)
        )

        if failed:

            print(
                f"{symbol} → "
                f"BREAKOUT BAŞARISIZ."
            )

            symbol_state["pending"] = None

        elif touched:

            # LH destek olarak tutuldu
            if latest["close"] >= lh:

                distance = (
                    (latest["close"] - lh)
                    / lh
                ) * 100

                message = (
                    "🟢 <b>CRYPTO ALERT</b>\n\n"
                    f"<b>{symbol}</b>\n"
                    "15m LH kırılım + retest\n\n"
                    f"LH: <b>{lh:.8g}</b>\n"
                    f"Retest kapanış: "
                    f"<b>{latest['close']:.8g}</b>\n"
                    f"Hacim: "
                    f"<b>{pending['volume_ratio']:.1f}x</b>\n"
                    f"LH uzaklığı: "
                    f"<b>{distance:.2f}%</b>\n\n"
                    "✅ LH destek olarak tutuldu."
                )

                print(
                    f"{symbol} → "
                    "RETEST BAŞARILI!"
                )

                sent = send_telegram(
                    message
                )

                if sent:

                    print(
                        f"{symbol} → "
                        "Telegram gönderildi."
                    )

                    symbol_state["pending"] = None

                else:

                    print(
                        f"{symbol} → "
                        "Telegram başarısız."
                    )

        else:

            print(
                f"{symbol} → "
                "Retest bekleniyor."
            )

        symbol_state[
            "last_processed"
        ] = latest["time"]

        symbol_state[
            "candles"
        ] = candles[-250:]

        return

    # =====================================================
    # YENİ KIRILIM ARIYORUZ
    # =====================================================

    lh = find_last_lower_high(
        candles
    )

    if not lh:

        print(
            symbol,
            "→ uygun LH yok."
        )

        symbol_state[
            "last_processed"
        ] = latest["time"]

        symbol_state[
            "candles"
        ] = candles[-250:]

        return

    lh_price = lh["price"]

    # Son 20 mumun ortalama hacmi
    if len(candles) < 22:

        return

    previous_20 = candles[-21:-1]

    avg_volume = (
        sum(
            c["volume"]
            for c in previous_20
        )
        /
        len(previous_20)
    )

    if avg_volume <= 0:

        return

    volume_ratio = (
        latest["volume"]
        /
        avg_volume
    )

    # Önceki mum LH altında/eşit,
    # son kapanış LH üzerinde
    previous_close = candles[-2]["close"]

    breakout = (
        previous_close <= lh_price
        and
        latest["close"] > lh_price
    )

    high_volume = (
        volume_ratio >= VOLUME_MULTIPLIER
    )

    if breakout and high_volume:

        print("")
        print(
            f"🔥 {symbol} KIRILIM BULUNDU!"
        )

        print(
            "LH:",
            lh_price
        )

        print(
            "Kapanış:",
            latest["close"]
        )

        print(
            "Hacim:",
            round(
                volume_ratio,
                2
            ),
            "x"
        )

        # Hemen alarm göndermiyoruz.
        # Retest beklemek için state'e yazıyoruz.
        symbol_state["pending"] = {

            "lh": lh_price,

            "breakout_price":
                latest["close"],

            "volume_ratio":
                volume_ratio,

            "breakout_time":
                latest["time"]
        }

    else:

        print(
            f"{symbol} → "
            "sinyal yok."
        )

        if breakout:

            print(
                "  LH kırıldı fakat "
                f"hacim sadece {volume_ratio:.2f}x"
            )

    symbol_state[
        "last_processed"
    ] = latest["time"]

    symbol_state[
        "candles"
    ] = candles[-250:]


# =========================================================
# ANA PROGRAM
# =========================================================

def main():

    print("")
    print("==========================================")
    print("   BINANCE 15M CRYPTO ALERT SCANNER")
    print("==========================================")

    state = load_state()

    # İlk çalışmada geçmiş veriyi hazırla
    for symbol in SYMBOLS:

        symbol_state = (
            state["symbols"]
            .setdefault(
                symbol,
                {
                    "candles": [],
                    "pending": None,
                    "last_processed": 0
                }
            )
        )

        if len(
            symbol_state.get(
                "candles",
                []
            )
        ) < 30:

            print("")
            print(
                f"{symbol} geçmişi hazırlanıyor..."
            )

            history = load_initial_history(
                symbol
            )

            symbol_state[
                "candles"
            ] = history

    # Bir sonraki kapanmış 15m mumu bekle
    print("")
    print(
        "Bir sonraki 15m kapanışı bekleniyor..."
    )

    live = get_next_closed_candles()

    print("")
    print(
        "Kapanmış mumlar alındı."
    )

    # Her sembole son canlı mumu ekle
    for symbol in SYMBOLS:

        if symbol not in live:
            continue

        symbol_state = state["symbols"][symbol]

        candles = symbol_state.get(
            "candles",
            []
        )

        candles.append(
            live[symbol]
        )

        # Tarama
        scan_symbol(
            symbol,
            candles,
            state
        )

    save_state(state)

    print("")
    print(
        "State kaydedildi."
    )

    # GitHub'a state'i kaydet
    commit_state()

    print("")
    print("==========================================")
    print(" SCAN TAMAMLANDI")
    print("==========================================")


if __name__ == "__main__":
    main()
