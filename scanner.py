import os
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CG = "https://api.coingecko.com/api/v3"
OKX = "https://www.okx.com/api/v5"

TIMEOUT = 20
RETRIES = 3

MIN_VOL = 5_000_000
MIN_SCORE = 85
DEEP = 50

S = requests.Session()
S.headers.update({
    "User-Agent": "AKAM-AI-SMART-SCANNER/2.1",
    "Accept": "application/json"
})


# =========================================================
# HTTP
# =========================================================

def get(url, params=None):
    err = None

    for n in range(RETRIES):
        try:
            r = S.get(
                url,
                params=params,
                timeout=TIMEOUT
            )

            if r.status_code == 429:
                time.sleep(2 + n)
                continue

            r.raise_for_status()

            data = r.json()

            if isinstance(data, dict) and "code" in data:
                if str(data["code"]) != "0":
                    raise RuntimeError(
                        f"API {data.get('code')}: {data.get('msg')}"
                    )

            return data

        except Exception as e:
            err = e
            time.sleep(1.5 * (n + 1))

    raise RuntimeError(
        f"request failed: {url}: {err}"
    )


# =========================================================
# TELEGRAM
# =========================================================

def tg(msg):
    r = S.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": msg,
            "disable_web_page_preview": "true"
        },
        timeout=TIMEOUT
    )

    r.raise_for_status()


# =========================================================
# COINGECKO UNIVERSE
# =========================================================

def universe():
    out = []

    for page in range(1, 5):

        rows = get(
            f"{CG}/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false"
            }
        )

        for x in rows:

            sym = str(
                x.get("symbol", "")
            ).upper().strip()

            if not sym:
                continue

            out.append({
                "rank": len(out) + 1,
                "symbol": sym,
                "name": x.get("name", sym),
                "market_cap": float(
                    x.get("market_cap") or 0
                )
            })

        if len(rows) < 250:
            break

        time.sleep(0.2)

    # حذف Symbolهای تکراری
    seen = set()
    clean = []

    for x in out:

        if x["symbol"] in seen:
            continue

        seen.add(x["symbol"])
        clean.append(x)

        if len(clean) >= 1000:
            break

    return clean


# =========================================================
# OKX INSTRUMENTS
# =========================================================

def instruments():

    rows = get(
        f"{OKX}/public/instruments",
        {
            "instType": "SPOT"
        }
    )["data"]

    result = {}

    for x in rows:

        inst_id = x.get("instId", "")

        if (
            x.get("state") == "live"
            and inst_id.endswith("-USDT")
        ):
            symbol = inst_id[:-5]
            result[symbol] = inst_id

    return result


# =========================================================
# OKX TICKERS
# =========================================================

def tickers():

    rows = get(
        f"{OKX}/market/tickers",
        {
            "instType": "SPOT"
        }
    )["data"]

    result = {}

    for x in rows:

        inst_id = x.get("instId", "")

        if not inst_id.endswith("-USDT"):
            continue

        try:

            result[inst_id[:-5]] = {
                "instId": inst_id,
                "price": float(x["last"]),
                "vol": float(
                    x.get("volCcy24h") or 0
                )
            }

        except Exception:
            continue

    return result


# =========================================================
# OKX CANDLES
# =========================================================

def candles(inst, bar, limit=220):

    rows = get(
        f"{OKX}/market/candles",
        {
            "instId": inst,
            "bar": bar,
            "limit": min(limit, 300)
        }
    )["data"]

    data = []

    for x in rows:

        if len(x) < 5:
            continue

        try:

            data.append([
                int(x[0]),                     # timestamp
                float(x[1]),                   # open
                float(x[2]),                   # high
                float(x[3]),                   # low
                float(x[4]),                   # close
                float(x[5]) if len(x) > 5 else 0,
                float(x[7]) if len(x) > 7 else 0,
                str(x[8]) if len(x) > 8 else "1"
            ])

        except Exception:
            continue

    d = pd.DataFrame(
        data,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "qvol",
            "confirm"
        ]
    )

    if d.empty:
        return d

    # فقط کندل بسته‌شده
    d = d[
        d["confirm"] != "0"
    ]

    d = d.drop_duplicates(
        "ts"
    )

    d = d.sort_values(
        "ts"
    ).reset_index(drop=True)

    return d


# =========================================================
# INDICATORS
# =========================================================

def ema(series, n):
    return series.ewm(
        span=n,
        adjust=False
    ).mean()


def rsi(series, n=14):

    z = series.diff()

    up = z.clip(lower=0)
    dn = -z.clip(upper=0)

    au = up.ewm(
        alpha=1 / n,
        min_periods=n,
        adjust=False
    ).mean()

    ad = dn.ewm(
        alpha=1 / n,
        min_periods=n,
        adjust=False
    ).mean()

    rs = au / ad.replace(
        0,
        np.nan
    )

    return (
        100 - 100 / (1 + rs)
    ).fillna(50)


def add(d):

    d = d.copy()

    # EMA
    d["ema20"] = ema(
        d["close"],
        20
    )

    d["ema50"] = ema(
        d["close"],
        50
    )

    d["ema200"] = ema(
        d["close"],
        200
    )

    # RSI
    d["rsi"] = rsi(
        d["close"]
    )

    # MACD
    fast = ema(
        d["close"],
        12
    )

    slow = ema(
        d["close"],
        26
    )

    macd = fast - slow

    signal_line = ema(
        macd,
        9
    )

    d["macd"] = macd
    d["signal"] = signal_line
    d["hist"] = macd - signal_line

    # ATR
    prev_close = d["close"].shift(1)

    tr = pd.concat(
        [
            d["high"] - d["low"],
            (d["high"] - prev_close).abs(),
            (d["low"] - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    d["atr"] = tr.ewm(
        alpha=1 / 14,
        min_periods=14,
        adjust=False
    ).mean()

    # Volume
    d["vma"] = d["volume"].rolling(
        20
    ).mean()

    d["vr"] = (
        d["volume"]
        / d["vma"].replace(0, np.nan)
    )

    return d


# =========================================================
# TREND SCORE
# =========================================================

def trend(d):

    if len(d) < 210:
        return 0

    x = d.iloc[-1]

    score = 0

    if x["close"] > x["ema20"]:
        score += 5

    if x["ema20"] > x["ema50"]:
        score += 5

    if x["ema50"] > x["ema200"]:
        score += 5

    return score


# =========================================================
# SETUP DETECTION
# =========================================================

def setup(d):

    if len(d) < 60:
        return None, 0

    x = d.iloc[-1]
    p = d.iloc[-2]

    # -----------------------------------------------------
    # Breakout / Retest
    # -----------------------------------------------------

    previous_high = d["high"].iloc[
        -21:-1
    ].max()

    if (
        x["close"] > previous_high
        or (
            p["low"] <= previous_high * 1.015
            and x["close"] >= previous_high * 0.995
        )
    ):
        return (
            "BREAKOUT + RETEST",
            15
        )

    # -----------------------------------------------------
    # Pullback
    # -----------------------------------------------------

    ema_distance = abs(
        x["close"] - x["ema20"]
    ) / x["close"]

    if (
        ema_distance <= 0.018
        or (
            p["close"] < p["ema20"]
            and x["close"] > x["ema20"]
        )
    ):
        return (
            "PULLBACK",
            15
        )

    # -----------------------------------------------------
    # Support Reversal
    # -----------------------------------------------------

    support = d["low"].iloc[
        -31:-1
    ].min()

    if (
        x["low"] <= support * 1.015
        and x["close"] > support * 1.01
    ):
        return (
            "SUPPORT REVERSAL",
            15
        )

    return None, 0


# =========================================================
# 1H CONFIRMATION
# =========================================================

def confirm(d):

    if len(d) < 60:
        return 0, False

    x = d.iloc[-1]

    score = 0

    if x["close"] > x["ema20"]:
        score += 3

    if x["ema20"] > x["ema50"]:
        score += 3

    if x["rsi"] >= 50:
        score += 2

    if x["hist"] > 0:
        score += 2

    return (
        score,
        score >= 6
    )


# =========================================================
# SIGNAL ENGINE
# =========================================================

def signal(c, d, h4, h1, btc):

    if (
        min(
            len(d),
            len(h4),
            len(h1)
        ) < 60
    ):
        return None

    if len(d) < 210:
        return None

    if len(h4) < 210:
        return None

    x = h4.iloc[-1]
    y = h1.iloc[-1]

    setup_name, setup_score = setup(
        h4
    )

    confirm_score, confirmed = confirm(
        h1
    )

    if not setup_name:
        return None

    if not confirmed:
        return None

    # -----------------------------------------------------
    # Base Trend
    # -----------------------------------------------------

    score = (
        trend(d)
        + trend(h4)
    )

    # -----------------------------------------------------
    # EMA Structure
    # -----------------------------------------------------

    if (
        x["close"] > x["ema20"]
        and x["ema20"] > x["ema50"]
        and x["ema50"] > x["ema200"]
    ):
        score += 10

    elif (
        x["close"] > x["ema20"]
        and x["ema20"] > x["ema50"]
    ):
        score += 7

    elif x["close"] > x["ema20"]:
        score += 4

    # -----------------------------------------------------
    # Setup
    # -----------------------------------------------------

    score += setup_score

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    if 52 <= x["rsi"] <= 68:
        score += 10

    elif (
        48 <= x["rsi"] < 52
        or 68 < x["rsi"] <= 74
    ):
        score += 6

    elif x["rsi"] > 74:
        score += 2

    # -----------------------------------------------------
    # MACD
    # -----------------------------------------------------

    if (
        x["hist"] > 0
        and x["macd"] > x["signal"]
    ):
        score += 10

    elif x["hist"] > 0:
        score += 6

    # -----------------------------------------------------
    # Volume
    # -----------------------------------------------------

    vr = x["vr"]

    if pd.isna(vr):
        vr = 0

    if vr >= 2:
        score += 15

    elif vr >= 1.5:
        score += 12

    elif vr >= 1.2:
        score += 8

    elif vr >= 1:
        score += 5

    # -----------------------------------------------------
    # 1H + BTC
    # -----------------------------------------------------

    score += confirm_score
    score += btc

    # -----------------------------------------------------
    # Minimum Score
    # -----------------------------------------------------

    if score < MIN_SCORE:
        return None

    # -----------------------------------------------------
    # No-Chase
    # -----------------------------------------------------

    if y["close"] > x["ema20"] * 1.10:
        return None

    # -----------------------------------------------------
    # Entry
    # -----------------------------------------------------

    entry = float(
        y["close"]
    )

    atr = float(
        x["atr"]
    )

    if not np.isfinite(atr):
        return None

    if atr <= 0:
        return None

    # -----------------------------------------------------
    # Stop Loss
    # -----------------------------------------------------

    recent_low = float(
        h4["low"].iloc[-20:].min()
    )

    atr_stop = (
        entry - 1.2 * atr
    )

    stop = min(
        recent_low,
        atr_stop
    )

    risk = entry - stop

    if risk <= 0:
        return None

    # -----------------------------------------------------
    # Targets
    # -----------------------------------------------------

    tp1 = entry + 2.5 * risk
    tp2 = entry + 3.0 * risk
    tp3 = entry + 4.0 * risk

    return {
        "symbol": c["symbol"],
        "score": int(score),
        "setup": setup_name,
        "entry": entry,
        "sl": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rsi": float(x["rsi"]),
        "vr": float(vr)
    }


# =========================================================
# PRICE FORMAT
# =========================================================

def fp(v):

    v = float(v)

    if v >= 1000:
        return f"{v:,.2f}"

    if v >= 1:
        return f"{v:,.4f}"

    if v >= 0.01:
        return f"{v:.5f}"

    if v >= 0.0001:
        return f"{v:.7f}"

    return (
        f"{v:.10f}"
        .rstrip("0")
        .rstrip(".")
    )


# =========================================================
# MAIN
# =========================================================

def main():

    start = time.time()

    print("AKAM V2.1 starting")

    # -----------------------------------------------------
    # Universe
    # -----------------------------------------------------

    u = universe()

    ins = instruments()

    tk = tickers()

    # -----------------------------------------------------
    # Liquidity Filter
    # -----------------------------------------------------

    liquid = []

    for c in u:

        symbol = c["symbol"]

        if symbol not in ins:
            continue

        z = tk.get(symbol)

        if not z:
            continue

        if z["vol"] < MIN_VOL:
            continue

        q = dict(c)

        q.update(z)

        liquid.append(q)

    liquid.sort(
        key=lambda x: x["market_cap"],
        reverse=True
    )

    liquid = liquid[:500]

    print(
        f"Universe={len(u)} "
        f"Liquid={len(liquid)}"
    )

    # -----------------------------------------------------
    # BTC Regime
    # -----------------------------------------------------

    btc_1d = add(
        candles(
            "BTC-USDT",
            "1Dutc",
            220
        )
    )

    btc_4h = add(
        candles(
            "BTC-USDT",
            "4H",
            220
        )
    )

    btc = 0

    if (
        len(btc_1d) >= 210
        and btc_1d.iloc[-1]["close"]
        > btc_1d.iloc[-1]["ema200"]
    ):
        btc += 5

    if (
        len(btc_4h) >= 210
        and btc_4h.iloc[-1]["close"]
        > btc_4h.iloc[-1]["ema50"]
    ):
        btc += 5

    print(
        f"BTC regime score={btc}/10"
    )

    # -----------------------------------------------------
    # 1D Pre-Filter
    # -----------------------------------------------------

    pre = []

    for i, c in enumerate(
        liquid,
        1
    ):

        try:

            d = add(
                candles(
                    c["instId"],
                    "1Dutc",
                    220
                )
            )

            if len(d) < 210:
                continue

            x = d.iloc[-1]

            if (
                x["close"] > x["ema20"]
                and x["ema20"] > x["ema50"]
                and x["close"] > x["ema200"]
            ):

                c["_d"] = d

                pre.append(c)

        except Exception as e:

            print(
                "1D skip",
                c["symbol"],
                str(e)
            )

        if i % 50 == 0:

            print(
                "1D",
                i,
                "/",
                len(liquid),
                "candidates",
                len(pre)
            )

        time.sleep(0.08)

    # -----------------------------------------------------
    # Deep Scan
    # -----------------------------------------------------

    pre = sorted(
        pre,
        key=lambda x: x["market_cap"],
        reverse=True
    )[:DEEP]

    print(
        f"Deep candidates={len(pre)}"
    )

    signals = []

    for i, c in enumerate(
        pre,
        1
    ):

        try:

            d = c["_d"]

            h4 = add(
                candles(
                    c["instId"],
                    "4H",
                    220
                )
            )

            h1 = add(
                candles(
                    c["instId"],
                    "1H",
                    100
                )
            )

            z = signal(
                c,
                d,
                h4,
                h1,
                btc
            )

            if z:

                signals.append(z)

                print(
                    "SIGNAL",
                    z["symbol"],
                    z["score"]
                )

        except Exception as e:

            print(
                "deep skip",
                c["symbol"],
                str(e)
            )

        if i % 10 == 0:

            print(
                "Deep",
                i,
                "/",
                len(pre)
            )

        time.sleep(0.08)

    # -----------------------------------------------------
    # Top 5 Signals
    # -----------------------------------------------------

    signals = sorted(
        signals,
        key=lambda x: x["score"],
        reverse=True
    )[:5]

    # -----------------------------------------------------
    # Telegram Message
    # -----------------------------------------------------

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    msg = [
        "🚨 AKAM AI SMART SCANNER V2.1",
        "",
        f"🕒 {now}",
        "📡 CoinGecko + OKX Public API",
        f"🔎 Universe: {len(u)} | 💧 Liquid: {len(liquid)}",
        f"🧠 Deep Scan: {len(pre)}",
        f"🎯 Minimum score: {MIN_SCORE}/110",
        ""
    ]

    # -----------------------------------------------------
    # No Signal
    # -----------------------------------------------------

    if not signals:

        msg += [
            "❌ سیگنال واجد شرایط پیدا نشد.",
            "",
            "فیلترها:",
            "1D Trend",
            "4H Trend",
            "EMA Structure",
            "Setup",
            "RSI",
            "MACD",
            "Volume",
            "1H Confirmation",
            "BTC Regime",
            "No-Chase"
        ]

    # ----------------------------------------
