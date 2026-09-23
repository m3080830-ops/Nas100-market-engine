import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "NAS100_USDT"

INTERVALS = {
    "D1": "Day1",
    "H4": "Hour4",
    "H1": "Min60",
    "M15": "Min15",
    "M5": "Min5",
}

LIMITS = {
    "D1": 300,
    "H4": 500,
    "H1": 1000,
    "M15": 1000,
    "M5": 1000,
}

START_BALANCE = 10000.0
RISK_PCT = 1.0

EMA_FAST = 9
EMA_MID = 18
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

TRAIL_ACTIVATION_R = 1.0
ATR_SL_MULT = 1.5
MIN_RR = 1.5

MAX_BARS_IN_TRADE = 288


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "NAS100-Backtest/1.0"}
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_klines(interval, limit):
    params = urllib.parse.urlencode({
        "interval": interval,
        "limit": limit
    })

    url = f"{BASE_URL}/{SYMBOL}?{params}"

    obj = get_json(url)

    data = obj.get("data", obj)

    rows = []

    if isinstance(data, dict):
        required = ["time", "open", "high", "low", "close"]

        for key in required:
            if key not in data:
                raise RuntimeError(
                    f"Brak pola {key} w odpowiedzi MEXC: {obj}"
                )

        n = min(
            len(data["time"]),
            len(data["open"]),
            len(data["high"]),
            len(data["low"]),
            len(data["close"])
        )

        volumes = data.get("vol", [0] * n)

        for i in range(n):
            rows.append({
                "ts": int(data["time"][i]),
                "open": float(data["open"][i]),
                "high": float(data["high"][i]),
                "low": float(data["low"][i]),
                "close": float(data["close"][i]),
                "volume": float(volumes[i]) if i < len(volumes) else 0.0
            })

    elif isinstance(data, list):

        for row in data:
            rows.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0
            })

    else:
        raise RuntimeError(
            f"Nieznany format odpowiedzi MEXC: {obj}"
        )

    rows.sort(key=lambda x: x["ts"])

    return rows


def calculate_ema(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    seed = sum(values[:period]) / period

    result[period - 1] = seed

    multiplier = 2.0 / (period + 1)

    previous = seed

    for i in range(period, len(values)):
        previous = (
            values[i] * multiplier
            + previous * (1.0 - multiplier)
        )

        result[i] = previous

    return result


def calculate_rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, period + 1):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        result[period] = 100.0
    else:
        rs = average_gain / average_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, len(values)):

        change = values[i] - values[i - 1]

        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        average_gain = (
            average_gain * (period - 1) + gain
        ) / period

        average_loss = (
            average_loss * (period - 1) + loss
        ) / period

        if average_loss == 0:
            result[i] = 100.0
        else:
            rs = average_gain / average_loss
            result[i] = 100.0 - (
                100.0 / (1.0 + rs)
            )

    return result


def calculate_atr(bars, period=14):
    result = [None] * len(bars)

    true_ranges = []

    for i, bar in enumerate(bars):

        if i == 0:
            tr = bar["high"] - bar["low"]

        else:
            previous_close = bars[i - 1]["close"]

            tr = max(
                bar["high"] - bar["low"],
                abs(bar["high"] - previous_close),
                abs(bar["low"] - previous_close)
            )

        true_ranges.append(tr)

    if len(true_ranges) <= period:
        return result

    average = sum(
        true_ranges[1:period + 1]
    ) / period

    result[period] = average

    for i in range(period + 1, len(bars)):

        average = (
            average * (period - 1)
            + true_ranges[i]
        ) / period

        result[i] = average

    return result


def add_indicators(bars):
    closes = [b["close"] for b in bars]

    ema9 = calculate_ema(closes, EMA_FAST)
    ema18 = calculate_ema(closes, EMA_MID)
    ema50 = calculate_ema(closes, EMA_SLOW)

    rsi_values = calculate_rsi(
        closes,
        RSI_PERIOD
    )

    atr_values = calculate_atr(
        bars,
        ATR_PERIOD
    )

    for i, bar in enumerate(bars):

        bar["ema9"] = ema9[i]
        bar["ema18"] = ema18[i]
        bar["ema50"] = ema50[i]

        bar["rsi"] = rsi_values[i]
        bar["atr"] = atr_values[i]


def latest_bar_at_or_before(bars, timestamp):
    left = 0
    right = len(bars) - 1

    answer = None

    while left <= right:

        middle = (left + right) // 2

        if bars[middle]["ts"] <= timestamp:
            answer = bars[middle]
            left = middle + 1
        else:
            right = middle - 1

    return answer


def recent_levels(bars, index, lookback=30):

    start = max(0, index - lookback)

    window = bars[start:index]

    if len(window) < 10:
        return None, None

    resistance = max(
        bar["high"]
        for bar in window
    )

    support = min(
        bar["low"]
        for bar in window
    )

    return resistance, support


def get_context_bar(bars, timestamp):
    return latest_bar_at_or_before(
        bars,
        timestamp
    )


def htf_direction(d1_bar, h4_bar, h1_bar):

    bullish = 0
    bearish = 0

    for bar in [d1_bar, h4_bar, h1_bar]:

        if not bar:
            continue

        if (
            bar["ema18"] is not None
            and bar["ema50"] is not None
        ):

            if (
                bar["close"] > bar["ema50"]
                and bar["ema18"] > bar["ema50"]
            ):
                bullish += 1

            if (
                bar["close"] < bar["ema50"]
                and bar["ema18"] < bar["ema50"]
            ):
                bearish += 1

    if bullish >= 2:
        return "BULLISH"

    if bearish >= 2:
        return "BEARISH"

    return "NEUTRAL"


def build_signal(
    h1_index,
    d1,
    h4,
    h1,
    m15,
    m5
):

    if h1_index < 2:
        return None

    h1_bar = h1[h1_index]
    previous = h1[h1_index - 1]

    if (
        h1_bar["atr"] is None
        or h1_bar["ema18"] is None
        or h1_bar["ema50"] is None
        or h1_bar["rsi"] is None
    ):
        return None

    d1_bar = get_context_bar(
        d1,
        h1_bar["ts"]
    )

    h4_bar = get_context_bar(
        h4,
        h1_bar["ts"]
    )

    m15_bar = get_context_bar(
        m15,
        h1_bar["ts"]
    )

    m5_bar = get_context_bar(
        m5,
        h1_bar["ts"]
    )

    if not m15_bar or not m5_bar:
        return None

    direction = htf_direction(
        d1_bar,
        h4_bar,
        h1_bar
    )

    resistance, support = recent_levels(
        h1,
        h1_index,
        30
    )

    if resistance is None or support is None:
        return None

    long_sweep = (
        previous["low"] < support
        and h1_bar["close"] > support
    )

    short_sweep = (
        previous["high"] > resistance
        and h1_bar["close"] < resistance
    )

    long_confirmation = (
        m15_bar["ema9"] is not None
        and m15_bar["ema18"] is not None
        and m5_bar["ema9"] is not None
        and m15_bar["close"] > m15_bar["ema18"]
        and m15_bar["ema9"] > m15_bar["ema18"]
        and m5_bar["close"] > m5_bar["ema9"]
    )

    short_confirmation = (
        m15_bar["ema9"] is not None
        and m15_bar["ema18"] is not None
        and m5_bar["ema9"] is not None
        and m15_bar["close"] < m15_bar["ema18"]
        and m15_bar["ema9"] < m15_bar["ema18"]
        and m5_bar["close"] < m5_bar["ema9"]
    )

    if (
        direction == "BULLISH"
        and long_sweep
        and long_confirmation
        and 40 <= h1_bar["rsi"] <= 70
    ):

        entry = m5_bar["close"]

        sl = min(
            previous["low"],
            support
        ) - (
            h1_bar["atr"] * ATR_SL_MULT
        )

        target = resistance

        risk = entry - sl

        if risk <= 0:
            return None

        reward = target - entry

        rr = reward / risk

        if rr < MIN_RR:
            return None

        return {
            "side": "LONG",
            "entry": entry,
            "sl": sl,
            "tp": target,
            "rr": rr,
            "signal_ts": h1_bar["ts"],
            "level": support,
            "score": calculate_score(
                h1_bar,
                m15_bar,
                m5_bar,
                "LONG"
            )
        }

    if (
        direction == "BEARISH"
        and short_sweep
        and short_confirmation
        and 30 <= h1_bar["rsi"] <= 60
    ):

        entry = m5_bar["close"]

        sl = max(
            previous["high"],
            resistance
        ) + (
            h1_bar["atr"] * ATR_SL_MULT
        )

        target = support

        risk = sl - entry

        if risk <= 0:
            return None

        reward = entry - target

        rr = reward / risk

        if rr < MIN_RR:
            return None

        return {
            "side": "SHORT",
            "entry": entry,
            "sl": sl,
            "tp": target,
            "rr": rr,
            "signal_ts": h1_bar["ts"],
            "level": resistance,
            "score": calculate_score(
                h1_bar,
                m15_bar,
                m5_bar,
                "SHORT"
            )
        }

    return None


def calculate_score(
    h1,
    m15,
    m5,
    side
):

    score = 0

    if side == "LONG":

        if h1["close"] > h1["ema50"]:
            score += 10

        if h1["ema18"] > h1["ema50"]:
            score += 10

        if m15["close"] > m15["ema18"]:
            score += 10

        if m15["ema9"] > m15["ema18"]:
            score += 10

        if m5["close"] > m5["ema9"]:
            score += 10

        if 40 <= h1["rsi"] <= 70:
            score += 10

    else:

        if h1["close"] < h1["ema50"]:
            score += 10

        if h1["ema18"] < h1["ema50"]:
            score += 10

        if m15["close"] < m15["ema18"]:
            score += 10

        if m15["ema9"] < m15["ema18"]:
            score += 10

        if m5["close"] < m5["ema9"]:
            score += 10

        if 30 <= h1["rsi"] <= 60:
            score += 10

    return score


def simulate_trade(
    signal,
    m5,
    start_index
):

    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]

    side = signal["side"]

    risk = abs(entry - sl)

    best_price = entry

    trailing_stop = None

    end_index = min(
        len(m5),
        start_index + MAX_BARS_IN_TRADE
    )

    for i in range(
        start_index,
        end_index
    ):

        bar = m5[i]

        if side == "LONG":

            best_price = max(
                best_price,
                bar["high"]
            )

            if (
                best_price - entry
                > TRAIL_ACTIVATION_R * risk
            ):

                trailing_stop = max(
                    entry,
                    best_price - risk
                )

            if bar["low"] <= sl:

                return (
                    bar["ts"],
                    sl,
                    -1.0,
                    "SL"
                )

            if (
                trailing_stop is not None
                and bar["low"] <= trailing_stop
            ):

                result_r = (
                    trailing_stop - entry
                ) / risk

                return (
                    bar["ts"],
                    trailing_stop,
                    result_r,
                    "TRAIL"
                )

            if bar["high"] >= tp:

                result_r = (
                    tp - entry
                ) / risk

                return (
                    bar["ts"],
                    tp,
                    result_r,
                    "TP"
                )

        else:

            best_price = min(
                best_price,
                bar["low"]
            )

            if (
                entry - best_price
                > TRAIL_ACTIVATION_R * risk
            ):

                trailing_stop = min(
                    entry,
                    best_price + risk
                )

            if bar["high"] >= sl:

                return (
                    bar["ts"],
                    sl,
                    -1.0,
                    "SL"
                )

            if (
                trailing_stop is not None
                and bar["high"] >= trailing_stop
            ):

                result_r = (
                    entry - trailing_stop
                ) / risk

                return (
                    bar["ts"],
                    trailing_stop,
                    result_r,
                    "TRAIL"
                )

            if bar["low"] <= tp:

                result_r = (
                    entry - tp
                ) / risk

                return (
                    bar["ts"],
                    tp,
                    result_r,
                    "TP"
                )

    last_bar = m5[end_index - 1]

    if side == "LONG":

        result_r = (
            last_bar["close"] - entry
        ) / risk

    else:

        result_r = (
            entry - last_bar["close"]
        ) / risk

    return (
        last_bar["ts"],
        last_bar["close"],
        result_r,
        "TIME"
    )


def timestamp_to_iso(timestamp):
    return datetime.fromtimestamp(
        timestamp / 1000,
        timezone.utc
    ).isoformat()


def main():

    print()
    print("================================")
    print(" NAS100 BACKTEST")
    print(" MEXC FUTURES")
    print("================================")
    print()

    data = {}

    for timeframe, interval in INTERVALS.items():

        print(
            f"Pobieranie {timeframe}..."
        )

        bars = fetch_klines(
            interval,
            LIMITS[timeframe]
        )

        add_indicators(bars)

        data[timeframe] = bars

        print(
            f"{timeframe}: {len(bars)} świec"
        )

        time.sleep(0.3)

    d1 = data["D1"]
    h4 = data["H4"]
    h1 = data["H1"]
    m15 = data["M15"]
    m5 = data["M5"]

    trades = []

    last_trade_h1_index = -1

    for i in range(
        60,
        len(h1)
    ):

        if i <= last_trade_h1_index:
            continue

        signal = build_signal(
            i,
            d1,
            h4,
            h1,
            m15,
            m5
        )

        if not signal:
            continue

        start_index = None

        for j, bar in enumerate(m5):

            if (
                bar["ts"]
                >= signal["signal_ts"]
            ):

                start_index = j
                break

        if start_index is None:
            continue

        (
            exit_ts,
            exit_price,
            result_r,
            exit_reason
        ) = simulate_trade(
            signal,
            m5,
            start_index
        )

        trade_number = len(trades) + 1

        trades.append({
            "case_id": (
                f"NAS100-{trade_number:05d}"
            ),
            "signal_time": timestamp_to_iso(
                signal["signal_ts"]
            ),
            "exit_time": timestamp_to_iso(
                exit_ts
            ),
            "side": signal["side"],
            "score": signal["score"],
            "entry": round(
                signal["entry"],
                4
            ),
            "sl": round(
                signal["sl"],
                4
            ),
            "tp": round(
                signal["tp"],
                4
            ),
            "rr": round(
                signal["rr"],
                3
            ),
            "exit_price": round(
                exit_price,
                4
            ),
            "result_r": round(
                result_r,
                3
            ),
            "exit_reason": exit_reason
        })

        last_trade_h1_index = i

    total_trades = len(trades)

    wins = sum(
        1
        for trade in trades
        if trade["result_r"] > 0
    )

    losses = sum(
        1
        for trade in trades
        if trade["result_r"] <= 0
    )

    total_r = sum(
        trade["result_r"]
        for trade in trades
    )

    win_rate = (
        wins / total_trades * 100
        if total_trades
        else 0
    )

    balance = START_BALANCE
    peak_balance = balance
    max_drawdown = 0.0

    for trade in trades:

        balance *= (
            1
            + (
                RISK_PCT / 100.0
            ) * trade["result_r"]
        )

        peak_balance = max(
            peak_balance,
            balance
        )

        drawdown = (
            (peak_balance - balance)
            / peak_balance
            * 100
        )

        max_drawdown = max(
            max_drawdown,
            drawdown
        )

    summary = {
        "symbol": SYMBOL,
        "market": "MEXC Futures",
        "timeframes": [
            "D1",
            "H4",
            "H1",
            "M15",
            "M5"
        ],
        "bars": {
            timeframe: len(bars)
            for timeframe, bars
            in data.items()
        },
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(
            win_rate,
            2
        ),
        "total_r": round(
            total_r,
            3
        ),
        "starting_balance": START_BALANCE,
        "ending_balance": round(
            balance,
            2
        ),
        "risk_per_trade_pct": RISK_PCT,
        "max_drawdown_pct": round(
            max_drawdown,
            2
        ),
        "minimum_rr": MIN_RR,
        "trailing_activation_r": (
            TRAIL_ACTIVATION_R
        ),
        "atr_sl_multiplier": ATR_SL_MULT
    }

    trade_fields = [
        "case_id",
        "signal_time",
        "exit_time",
        "side",
        "score",
        "entry",
        "sl",
        "tp",
        "rr",
        "exit_price",
        "result_r",
        "exit_reason"
    ]

    with open(
        "nas100_backtest_trades.csv",
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=trade_fields
        )

        writer.writeheader()

        for trade in trades:
            writer.writerow(trade)

    with open(
        "nas100_backtest_summary.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            summary,
            file,
            indent=2,
            ensure_ascii=False
        )

    print()
    print("================================")
    print(" WYNIK BACKTESTU")
    print("================================")
    print(
        f"Trade'y:       {total_trades}"
    )
    print(
        f"Wygrane:       {wins}"
    )
    print(
        f"Przegrane:     {losses}"
    )
    print(
        f"Win rate:      {win_rate:.2f}%"
    )
    print(
        f"Wynik:         {total_r:.3f} R"
    )
    print(
        f"Saldo końcowe: {balance:.2f}"
    )
    print(
        f"Max DD:        {max_drawdown:.2f}%"
    )
    print("================================")
    print()
    print(
        "Utworzono:"
    )
    print(
        "nas100_backtest_trades.csv"
    )
    print(
        "nas100_backtest_summary.json"
    )
    print()


if __name__ == "__main__":
    main()
