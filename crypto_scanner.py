import csv
import io
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone


# =========================
# AYARLAR
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = "15m"

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
VOLUME_LOOKBACK = 20


# =========================
# BINANCE GEÇMİŞ VERİ
# =========================

def download_day(symbol, date):
    date_str = date.strftime("%Y-%m-%d")

    url = (
        f"https://data.binance.vision/"
        f"data/futures/um/daily/klines/"
        f"{symbol}/{INTERVAL}/"
        f"{symbol}-{INTERVAL}-{date_str}.zip"
    )

    print("Veri indiriliyor:", date_str)

    response = urllib.request.urlopen(url, timeout=30)
    data = response.read()

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        filename = z.namelist()[0]

        with z.open(filename) as f:
            rows = list(csv.reader(io.TextIOWrapper(f)))

    # Header varsa çıkar
    if rows and rows[0][0].lower() in ("open time", "open_time"):
        rows = rows[1:]

    candles = []

    for row in rows:
        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })

    return candles


def get_history():
    now = datetime.now(timezone.utc)

    candles = []

    # Son iki tamamlanmış gün
    for days_ago in [2, 1]:
        date = (now - timedelta(days=days_ago)).date()

        try:
            candles.extend(
                download_day(SYMBOL, date)
            )
        except Exception as e:
            print("Bu gün alınamadı:", date, e)

    candles.sort(key=lambda x: x["time"])

    return candles


# =========================
# PIVOT HIGH
# =========================

def find_pivot_highs(candles):
    pivots = []

    start = PIVOT_LEFT
    end = len(candles) - PIVOT_RIGHT

    for i in range(start, end):

        current_high = candles[i]["high"]

        left_highs = [
            candles[j]["high"]
            for j in range(i - PIVOT_LEFT, i)
        ]

        right_highs = [
            candles[j]["high"]
            for j in range(i + 1, i + PIVOT_RIGHT + 1)
        ]

        if (
            current_high > max(left_highs)
            and
            current_high >= max(right_highs)
        ):
            pivots.append({
                "index": i,
                "price": current_high,
                "time": candles[i]["time"]
            })

    return pivots


# =========================
# SON LOWER HIGH
# =========================

def find_last_lower_high(candles, pivots):

    # En yeni pivotlardan geriye doğru bakıyoruz
    for i in range(len(pivots) - 1, 0, -1):

        previous_pivot = pivots[i - 1]
        current_pivot = pivots[i]

        if current_pivot["price"] < previous_pivot["price"]:

            return {
                "price": current_pivot["price"],
                "time": current_pivot["time"],
                "previous_high": previous_pivot["price"]
            }

    return None


# =========================
# ANA TEST
# =========================

def scan():

    print("")
    print("====================================")
    print(" BINANCE 15M SCANNER TEST")
    print("====================================")
    print("Sembol:", SYMBOL)

    candles = get_history()

    print("")
    print("Toplam mum:", len(candles))

    if len(candles) < 30:
        print("Yeterli mum bulunamadı.")
        return

    # Son mumun kapanmış olduğunu varsayarak
    # son 20 mumun hacmini hesapla
    recent = candles[-1]

    volume_candles = candles[-21:-1]

    avg_volume = (
        sum(c["volume"] for c in volume_candles)
        / len(volume_candles)
    )

    print("")
    print("SON MUM")
    print("Açılış :", recent["open"])
    print("Yüksek :", recent["high"])
    print("Düşük  :", recent["low"])
    print("Kapanış:", recent["close"])
    print("Hacim  :", recent["volume"])

    print("")
    print("SON 20 MUM ORTALAMA HACMİ:")
    print(avg_volume)

    if avg_volume > 0:
        volume_ratio = recent["volume"] / avg_volume
        print("Hacim oranı:", round(volume_ratio, 2), "x")

    # Pivotlar
    pivots = find_pivot_highs(candles)

    print("")
    print("Bulunan pivot high:", len(pivots))

    # Son 5 pivotu göster
    print("")
    print("SON PIVOT HIGH'LAR:")

    for pivot in pivots[-5:]:
        print(
            "Fiyat:",
            pivot["price"],
            "| Mum:",
            pivot["index"]
        )

    # LH
    last_lh = find_last_lower_high(candles, pivots)

    print("")
    print("====================================")

    if last_lh:

        print("SON LOWER HIGH BULUNDU!")
        print("LH:", last_lh["price"])
        print("Önceki High:", last_lh["previous_high"])

        distance = (
            (recent["close"] - last_lh["price"])
            / last_lh["price"]
        ) * 100

        print(
            "Fiyatın LH'ye uzaklığı:",
            round(distance, 2),
            "%"
        )

    else:
        print("Henüz uygun Lower High bulunamadı.")

    print("====================================")
    print("")
    print("TEST TAMAMLANDI!")


if __name__ == "__main__":
    scan()
