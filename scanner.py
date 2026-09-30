import os
import time
import requests
import ccxt
import pandas as pd
import numpy as np

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MAX_UNIVERSE = 1000
MAX_LIQUID = 500
DEEP_CANDIDATES = 50
MIN_QUOTE_VOLUME = 5_000_000

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text
        },
        timeout=20
    )

    r.raise_for_status()


def get_top_1000_coins():
    coins = []

    try:
        for page in range(1, 5):

            r = requests.get(
                COINGECKO_URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false"
                },
                timeout=20
            )

            r.raise_for_status()

            data = r.json()

            if not data:
                break

            coins.extend(data)

            time.sleep(1)

        return coins[:MAX_UNIVERSE]

    except Exception as e:

        print("CoinGecko unavailable:", e)

        return []


def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def atr(df, period=14):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def indicators(df):

    x = df.copy()

    x["ema20"] = ema(x["close"], 20)
    x["ema50"] = ema(x["close"], 50)
    x["ema200"] = ema(x["close"], 200)

    x["rsi"] = rsi(x["close"])

    x["macd"] = x["ema20"] - x["ema50"]

    x["macd_signal"] = x["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    x["atr"] = atr(x)

    x["volume_ma"] = x["volume"].rolling(20).mean()

    x["volume_ratio"] = (
        x["volume"] /
        x["volume_ma"].replace(0, np.nan)
    )

    typical_price = (
        x["high"] +
        x["low"] +
        x["close"]
    ) / 3

    x["vwap20"] = (
        (typical_price * x["volume"]).rolling(20).sum()
        /
        x["volume"].rolling(20).sum()
    )

    x["high20"] = x["high"].rolling(20).max()
    x["low20"] = x["low"].rolling(20).min()

    return x.dropna()


def fetch_ohlcv(exchange, symbol, timeframe, limit=220):

    try:

        data = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit
        )

        if len(data) < 100:
            return None

        return pd.DataFrame(
            data,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

    except Exception as e:

        print("OHLCV error:", symbol, timeframe, e)

        return None


def btc_regime(exchange):

    d1 = fetch_ohlcv(
        exchange,
        "BTC/USDT",
        "1d"
    )

    h4 = fetch_ohlcv(
        exchange,
        "BTC/USDT",
        "4h"
    )

    if d1 is None or h4 is None:
        return 5

    d1 = indicators(d1)
    h4 = indicators(h4)

    a = d1.iloc[-1]
    b = h4.iloc[-1]

    score = 0

    if a["close"] > a["ema200"]:
        score += 4

    if a["ema20"] > a["ema50"]:
        score += 2

    if b["close"] > b["ema200"]:
        score += 2

    if b["ema20"] > b["ema50"]:
        score += 2

    return score


def first_stage_score(df):

    a = df.iloc[-1]

    score = 0

    # 1D trend / 15
    if a["close"] > a["ema200"]:
        score += 7

    if a["ema20"] > a["ema50"]:
        score += 5

    if a["close"] > a["ema20"]:
        score += 3

    # EMA / 10
    if a["close"] > a["ema20"] > a["ema50"]:
        score += 7

    if a["close"] > a["ema200"]:
        score += 3

    # RSI / 10
    if 50 <= a["rsi"] <= 68:
        score += 10

    elif 45 <= a["rsi"] < 50:
        score += 6

    # MACD / 10
    if a["macd"] > a["macd_signal"]:
        score += 7

    if a["macd"] > 0:
        score += 3

    # Volume / 15
    if a["volume_ratio"] >= 1.5:
        score += 15

    elif a["volume_ratio"] >= 1.2:
        score += 10

    elif a["volume_ratio"] >= 1:
        score += 6

    # Setup / 15
    pullback = (
        a["low"] <= a["ema20"] * 1.02
        and
        a["close"] > a["ema20"]
    )

    breakout = (
        a["close"] >= a["high20"] * 0.995
        and
        a["volume_ratio"] >= 1.3
    )

    reversal = (
        a["close"] <= a["low20"] * 1.05
        and
        a["close"] > a["open"]
    )

    if pullback or breakout or reversal:
        score += 15

    # VWAP / structure / 10
    if a["close"] > a["vwap20"]:
        score += 5

    if a["close"] > a["open"]:
        score += 5

    return score


def deep_analysis(d1, h4, h1, btc_score):

    a = d1.iloc[-1]
    b = h4.iloc[-1]
    c = h1.iloc[-1]

    # 1H must confirm the 4H trend
    if not (
        c["close"] > c["ema20"] > c["ema50"]
    ):
        return 0, "NONE"

    score = 0

    # 1D trend / 15
    if a["close"] > a["ema200"]:
        score += 7

    if a["ema20"] > a["ema50"]:
        score += 5

    if a["close"] > a["ema20"]:
        score += 3

    # 4H trend / 15
    if b["close"] > b["ema200"]:
        score += 7

    if b["ema20"] > b["ema50"]:
        score += 5

    if b["close"] > b["ema20"]:
        score += 3

    # EMA / 10
    if a["close"] > a["ema20"] > a["ema50"]:
        score += 7

    if b["close"] > b["ema20"] > b["ema50"]:
        score += 3

    # Setup / 15
    pullback = (
        b["low"] <= b["ema20"] * 1.025
        and
        b["close"] > b["ema20"]
    )

    breakout = (
        b["close"] >= b["high20"] * 0.995
        and
        b["volume_ratio"] >= 1.25
    )

    reversal = (
        b["close"] <= b["low20"] * 1.05
        and
        b["close"] > b["open"]
    )

    if pullback:
        setup = "PULLBACK"

    elif breakout:
        setup = "BREAKOUT + RETEST"

    elif reversal:
        setup = "SUPPORT REVERSAL"

    else:
        setup = "NONE"

    if setup != "NONE":
        score += 15

    # RSI / 10
    if 50 <= b["rsi"] <= 68:
        score += 10

    elif 45 <= b["rsi"] < 50:
        score += 6

    # MACD / 10
    if b["macd"] > b["macd_signal"]:
        score += 7

    if b["macd"] > 0:
        score += 3

    # Volume / 15
    if b["volume_ratio"] >= 1.5:
        score += 15

    elif b["volume_ratio"] >= 1.2:
        score += 10

    elif b["volume_ratio"] >= 1:
        score += 6

    # 1H structure / 10
    if c["close"] > c["ema20"] > c["ema50"]:
        score += 7

    if c["close"] > c["vwap20"]:
        score += 3

    # BTC market conditions / 10
    score += btc_score

    return score, setup


def make_signal(symbol, h4, h1, score, setup):

    a = h4.iloc[-1]
    b = h1.iloc[-1]

    entry = float(b["close"])
    atr_value = float(a["atr"])

    if entry > float(a["ema20"]) * 1.10:
        return None

    if setup == "BREAKOUT + RETEST":

        entry_low = entry * 0.995
        entry_high = entry * 1.005

    else:

        entry_low = entry * 0.99
        entry_high = entry * 1.01

    stop_loss = min(
        float(a["low20"]),
        entry - atr_value * 1.2
    )

    risk = entry - stop_loss

    if risk <= 0:
        return None

    tp1 = entry + risk * 2.5
    tp2 = entry + risk * 3
    tp3 = entry + risk * 4

    return {
        "symbol": symbol,
        "score": score,
        "setup": setup,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3
    }


def main():

    exchange = ccxt.binance({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {
            "defaultType": "spot"
        }
    })

    print("Loading Binance markets...")

    markets = exchange.load_markets()

    print("Loading Binance tickers...")

    tickers = exchange.fetch_tickers()

    coins = get_top_1000_coins()

    if not coins:

        # Emergency fallback:
        # use Binance liquid USDT markets
        candidates = []

        for symbol, market in markets.items():

            if (
                market.get("spot")
                and market.get("active")
                and market.get("quote") == "USDT"
            ):

                ticker = tickers.get(symbol, {})

                volume = float(
                    ticker.get("quoteVolume") or 0
                )

                if volume >= MIN_QUOTE_VOLUME:

                    candidates.append(
                        (symbol, volume)
                    )

        candidates.sort(
            key=lambda x: x[1],
            reverse=True
        )

        universe = [
            {
                "symbol": x[0],
                "rank": 99999,
                "volume": x[1]
            }
            for x in candidates[:MAX_LIQUID]
        ]

    else:

        banned = {
            "USDT",
            "USDC",
            "BUSD",
            "DAI",
            "FDUSD",
            "TUSD",
            "USDE",
            "USDD",
            "PYUSD"
        }

        universe = []
        seen = set()

        for coin in coins:

            base = str(
                coin.get("symbol", "")
            ).upper()

            if not base:
                continue

            if base in banned:
                continue

            if base in seen:
                continue

            symbol = f"{base}/USDT"

            market = markets.get(symbol)

            if not market:
                continue

            if not market.get("active"):
                continue

            if not market.get("spot"):
                continue

            ticker = tickers.get(symbol, {})

            volume = float(
                ticker.get("quoteVolume") or 0
            )

            if volume < MIN_QUOTE_VOLUME:
                continue

            seen.add(base)

            universe.append({
                "symbol": symbol,
                "rank": coin.get(
                    "market_cap_rank",
                    99999
                ),
                "volume": volume
            })

        universe.sort(
            key=lambda x: (
                x["rank"],
                -x["volume"]
            )
        )

        universe = universe[:MAX_LIQUID]

    print(
        f"Liquid universe: {len(universe)}"
    )

    btc_score = btc_regime(exchange)

    print(
        f"BTC regime score: {btc_score}/10"
    )

    first_pass = []

    for index, item in enumerate(
        universe,
        start=1
    ):

        df = fetch_ohlcv(
            exchange,
            item["symbol"],
            "1d",
            220
        )

        if df is None:
            continue

        df = indicators(df)

        if len(df) < 100:
            continue

        score = first_stage_score(df)

        first_pass.append(
            (
                score,
                item,
                df
            )
        )

        if index % 50 == 0:

            print(
                f"1D scan: "
                f"{index}/{len(universe)}"
            )

    first_pass.sort(
        key=lambda x: x[0],
        reverse=True
    )

    finalists = first_pass[
        :DEEP_CANDIDATES
    ]

    print(
        f"Deep analysis: "
        f"{len(finalists)} coins"
    )

    signals = []

    for score_1d, item, d1 in finalists:

        h4 = fetch_ohlcv(
            exchange,
            item["symbol"],
            "4h",
            220
        )

        h1 = fetch_ohlcv(
            exchange,
            item["symbol"],
            "1h",
            220
        )

        if h4 is None or h1 is None:
            continue

        h4 = indicators(h4)
        h1 = indicators(h1)

        score, setup = deep_analysis(
            d1,
            h4,
            h1,
            btc_score
        )

        if score < 85:
            continue

        if setup == "NONE":
            continue

        signal = make_signal(
            item["symbol"],
            h4,
            h1,
            score,
            setup
        )

        if signal:

            signal["rank"] = item["rank"]

            signals.append(signal)

    signals.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    message = (
        "🚀 AKAM AI SMART SCANNER\n\n"
        f"🌐 Universe: {len(coins)}\n"
        f"💧 Liquid: {len(universe)}\n"
        f"🧠 Deep scan: {len(finalists)}\n"
        f"₿ BTC regime: {btc_score}/10\n\n"
    )

    if not signals:

        message += (
            "🔎 سیگنال واجد شرایط پیدا نشد.\n\n"
            "فیلتر نهایی:\n"
            "✅ Score ≥ 85/110\n"
            "✅ تأیید 1H\n"
            "✅ ساختار 4H\n"
            "✅ R/R حداقل 1:2.5\n\n"
            "🛡️ عدم وجود سیگنال بهتر از "
            "سیگنال ضعیف است."
        )

        send_telegram(message)

        print("No qualified signals.")

        return

    message += (
        f"🎯 Signals found: {len(signals)}\n\n"
    )

    for signal in signals[:5]:

        message += (
            f"🔥 {signal['symbol']}\n"
            f"⭐ Score: "
            f"{signal['score']}/110\n"
            f"📌 Setup: "
            f"{signal['setup']}\n\n"
            f"🎯 Entry: "
            f"{signal['entry_low']:.8g}"
            f" – "
            f"{signal['entry_high']:.8g}\n"
            f"🛑 SL: "
            f"{signal['sl']:.8g}\n"
            f"🥇 TP1: "
            f"{signal['tp1']:.8g}\n"
            f"🥈 TP2: "
            f"{signal['tp2']:.8g}\n"
            f"🥉 TP3: "
            f"{signal['tp3']:.8g}\n\n"
        )

    message += (
        "⚠️ خروجی اسکن تکنیکال است؛ "
        "تضمین سود نیست."
    )

    send_telegram(message)

    print("Scanner completed successfully.")


if __name__ == "__main__":
    main()
