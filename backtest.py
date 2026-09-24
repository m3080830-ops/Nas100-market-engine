import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "NAS100_USDT"
LOOKBACK_DAYS = 90
LIMIT = 2000

TIMEFRAMES = {
    "D1": ("Day1", 86400),
    "H4": ("Hour4", 14400),
    "H1": ("Min60", 3600),
    "M15": ("Min15", 900),
    "M5": ("Min5", 300),
}

MIN_RR = 1.5
ATR_SL = 1.5
TRAIL_R = 1.0
MAX_TRADE_BARS = 288


def get_json(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NAS100-Backtest/3.0"},
            )

            with urllib.request.urlopen(
                req,
                timeout=30
            ) as response:
                return json.loads(
                    response.read().decode()
                )

        except Exception:
            if attempt == 3:
                raise

            time.sleep(
                1 + attempt
            )


def parse(obj):
    data = obj["data"]

    n = min(
        len(data["time"]),
        len(data["open"]),
        len(data["high"]),
        len(data["low"]),
        len(data["close"]),
    )

    volumes = data.get(
        "vol",
        []
    )

    rows = []

    for i in range(n):
        rows.append(
            {
                "ts": int(
                    data["time"][i]
                ),
                "open": float(
                    data["open"][i]
                ),
                "high": float(
                    data["high"][i]
                ),
                "low": float(
                    data["low"][i]
                ),
                "close": float(
                    data["close"][i]
                ),
                "volume": (
                    float(volumes[i])
                    if i < len(volumes)
                    else 0.0
                ),
            }
        )

    return rows


def fetch(interval, step):
    end = int(
        time.time()
    )

    start_wanted = (
        end
        - LOOKBACK_DAYS * 86400
    )

    rows = []

    while end > start_wanted:

        start = max(
            start_wanted,
            end - LIMIT * step
        )

        params = urllib.parse.urlencode(
            {
                "interval": interval,
                "start": start,
                "end": end,
            }
        )

        url = (
            f"{BASE_URL}/{SYMBOL}?{params}"
        )

        batch = parse(
            get_json(url)
        )

        if not batch:
            break

        rows.extend(batch)

        oldest = min(
            x["ts"]
            for x in batch
        )

        if oldest <= start_wanted:
            break

        end = (
            oldest - step
        )

        time.sleep(0.15)

    unique = {
        x["ts"]: x
        for x in rows
    }

    return [
        unique[k]
        for k in sorted(unique)
        if k >= start_wanted
    ]


def ema(values, period):
    result = [
        None
        for _ in values
    ]

    if len(values) < period:
        return result

    value = (
        sum(values[:period])
        / period
    )

    result[period - 1] = value

    alpha = (
        2 / (period + 1)
    )

    for i in range(
        period,
        len(values)
    ):
        value = (
            values[i] * alpha
            + value * (1 - alpha)
        )

        result[i] = value

    return result


def rsi(values, period=14):
    result = [
        None
        for _ in values
    ]

    if len(values) <= period:
        return result

    gain = sum(
        max(
            values[i]
            - values[i - 1],
            0
        )
        for i in range(
            1,
            period + 1
        )
    ) / period

    loss = sum(
        max(
            values[i - 1]
            - values[i],
            0
        )
        for i in range(
            1,
            period + 1
        )
    ) / period

    def calculate(
        current_gain,
        current_loss
    ):
        if current_loss == 0:
            return 100.0

        rs = (
            current_gain
            / current_loss
        )

        return (
            100
            - 100 / (1 + rs)
        )

    result[period] = calculate(
        gain,
        loss
    )

    for i in range(
        period + 1,
        len(values)
    ):
        change = (
            values[i]
            - values[i - 1]
        )

        current_gain = max(
            change,
            0
        )

        current_loss = max(
            -change,
            0
        )

        gain = (
            gain * (period - 1)
            + current_gain
        ) / period

        loss = (
            loss * (period - 1)
            + current_loss
        ) / period

        result[i] = calculate(
            gain,
            loss
        )

    return result


def atr(bars, period=14):
    result = [
        None
        for _ in bars
    ]

    if len(bars) <= period:
        return result

    true_ranges = []

    for i, bar in enumerate(bars):

        if i == 0:
            value = (
                bar["high"]
                - bar["low"]
            )

        else:
            previous_close = (
                bars[i - 1]["close"]
            )

            value = max(
                bar["high"]
                - bar["low"],

                abs(
                    bar["high"]
                    - previous_close
                ),

                abs(
                    bar["low"]
                    - previous_close
                ),
            )

        true_ranges.append(
            value
        )

    value = (
        sum(
            true_ranges[
                1:period + 1
            ]
        )
        / period
    )

    result[period] = value

    for i in range(
        period + 1,
        len(bars)
    ):

        value = (
            (
                value * (period - 1)
                + true_ranges[i]
            )
            / period
        )

        result[i] = value

    return result


def indicators(bars):
    closes = [
        bar["close"]
        for bar in bars
    ]

    ema9 = ema(
        closes,
        9
    )

    ema18 = ema(
        closes,
        18
    )

    ema50 = ema(
        closes,
        50
    )

    rsi_values = rsi(
        closes
    )

    atr_values = atr(
        bars
    )

    for i, bar in enumerate(
        bars
    ):

        bar["ema9"] = ema9[i]
        bar["ema18"] = ema18[i]
        bar["ema50"] = ema50[i]

        bar["rsi"] = (
            rsi_values[i]
        )

        bar["atr"] = (
            atr_values[i]
        )


def idx(bars, timestamp):
    left = 0
    right = len(bars) - 1
    answer = -1

    while left <= right:

        middle = (
            left + right
        ) // 2

        if (
            bars[middle]["ts"]
            <= timestamp
        ):

            answer = middle
            left = middle + 1

        else:
            right = middle - 1

    return answer


def closed(
    bars,
    timestamp,
    step
):
    return idx(
        bars,
        timestamp - step
    )


def levels(
    h1,
    index,
    lookback=30
):
    start = max(
        0,
        index - lookback
    )

    window = h1[
        start:index
    ]

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

    return (
        resistance,
        support
    )


def direction(
    d1,
    h4,
    h1
):
    bullish = 0
    bearish = 0

    for bar in (
        d1,
        h4,
        h1
    ):

        if (
            not bar
            or bar["ema18"] is None
            or bar["ema50"] is None
        ):
            continue

        if (
            bar["close"]
            > bar["ema50"]
            and
            bar["ema18"]
            > bar["ema50"]
        ):
            bullish += 1

        if (
            bar["close"]
            < bar["ema50"]
            and
            bar["ema18"]
            < bar["ema50"]
        ):
            bearish += 1

    if bullish >= 2:
        return "BULLISH"

    if bearish >= 2:
        return "BEARISH"

    return "NEUTRAL"


def signal_at(
    timestamp,
    data
):
    d1 = data["D1"]
    h4 = data["H4"]
    h1 = data["H1"]
    m15 = data["M15"]
    m5 = data["M5"]

    h1_index = closed(
        h1,
        timestamp,
        3600
    )

    m15_index = closed(
        m15,
        timestamp,
        900
    )

    m5_index = idx(
        m5,
        timestamp
    )

    if (
        h1_index < 31
        or m15_index < 20
        or m5_index < 20
    ):
        return None, "DATA"

    h1_bar = h1[
        h1_index
    ]

    m15_bar = m15[
        m15_index
    ]

    m5_bar = m5[
        m5_index
    ]

    required = (
        h1_bar["atr"],
        h1_bar["ema18"],
        h1_bar["ema50"],
        h1_bar["rsi"],
        m15_bar["ema9"],
        m15_bar["ema18"],
        m5_bar["ema9"],
    )

    if any(
        value is None
        for value in required
    ):
        return None, "INDICATORS"

    d1_index = idx(
        d1,
        timestamp - 86400
    )

    h4_index = idx(
        h4,
        timestamp - 14400
    )

    d1_bar = (
        d1[d1_index]
        if d1_index >= 0
        else None
    )

    h4_bar = (
        h4[h4_index]
        if h4_index >= 0
        else None
    )

    market_direction = direction(
        d1_bar,
        h4_bar,
        h1_bar
    )

    if market_direction == "NEUTRAL":
        return None, "HTF"

    resistance, support = levels(
        h1,
        h1_index
    )

    if (
        resistance is None
        or support is None
    ):
        return None, "LEVEL"

    previous = h1[
        h1_index - 1
    ]

    long_sweep = (
        previous["low"]
        < support
        and
        h1_bar["close"]
        > support
    )

    short_sweep = (
        previous["high"]
        > resistance
        and
        h1_bar["close"]
        < resistance
    )

    if market_direction == "BULLISH":

        if not long_sweep:
            return None, "NO_LONG_SWEEP"

        confirmation = (
            m15_bar["close"]
            > m15_bar["ema18"]
            and
            m15_bar["ema9"]
            > m15_bar["ema18"]
            and
            m5_bar["close"]
            > m5_bar["ema9"]
        )

        if not confirmation:
            return None, "LONG_CONFIRMATION"

        if not (
            40
            <= h1_bar["rsi"]
            <= 70
        ):
            return None, "LONG_RSI"

        entry = (
            m5_bar["close"]
        )

        sl = (
            min(
                previous["low"],
                support
            )
            - h1_bar["atr"]
            * ATR_SL
        )

        tp = resistance

        risk = (
            entry - sl
        )

        reward = (
            tp - entry
        )

        if (
            risk <= 0
            or reward <= 0
        ):
            return None, "LONG_RISK"

        rr = reward / risk

        if rr < MIN_RR:
            return None, "LONG_RR"

        return (
            {
                "side": "LONG",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": rr,
                "signal_ts": timestamp,
            },
            "READY",
        )

    if not short_sweep:
        return None, "NO_SHORT_SWEEP"

    confirmation = (
        m15_bar["close"]
        < m15_bar["ema18"]
        and
        m15_bar["ema9"]
        < m15_bar["ema18"]
        and
        m5_bar["close"]
        < m5_bar["ema9"]
    )

    if not confirmation:
        return None, "SHORT_CONFIRMATION"

    if not (
        30
        <= h1_bar["rsi"]
        <= 60
    ):
        return None, "SHORT_RSI"

    entry = (
        m5_bar["close"]
    )

    sl = (
        max(
            previous["high"],
            resistance
        )
        + h1_bar["atr"]
        * ATR_SL
    )

    tp = support

    risk = (
        sl - entry
    )

    reward = (
        entry - tp
    )

    if (
        risk <= 0
        or reward <= 0
    ):
        return None, "SHORT_RISK"

    rr = reward / risk

    if rr < MIN_RR:
        return None, "SHORT_RR"

    return (
        {
            "side": "SHORT",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "signal_ts": timestamp,
        },
        "READY",
    )


def simulate_trade(
    signal,
    m5,
    start_index
):
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    side = signal["side"]

    risk = abs(
        entry - sl
    )

    best_price = entry
    trailing_stop = None

    end_index = min(
        len(m5),
        start_index
        + MAX_TRADE_BARS
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
                > TRAIL_R * risk
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
                and
                bar["low"]
                <= trailing_stop
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
                > TRAIL_R * risk
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
                and
                bar["high"]
                >= trailing_stop
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

    last_bar = m5[
        end_index - 1
    ]

    if side == "LONG":
        result_r = (
            last_bar["close"]
            - entry
        ) / risk
    else:
        result_r = (
            entry
            - last_bar["close"]
        ) / risk

    return (
        last_bar["ts"],
        last_bar["close"],
        result_r,
        "TIME"
    )


def iso(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).isoformat()


def main():

    print(
        "========================================"
    )

    print(
        " NAS100 BACKTEST - 90 DNI"
    )

    print(
        " MEXC FUTURES"
    )

    print(
        "========================================"
    )

    data = {}

    for (
        timeframe,
        values
    ) in TIMEFRAMES.items():

        interval, step = values

        print(
            f"Pobieranie {timeframe}..."
        )

        bars = fetch(
            interval,
            step
        )

        if len(bars) < 60:
            raise RuntimeError(
                f"Za malo danych dla "
                f"{timeframe}: "
                f"{len(bars)}"
            )

        indicators(bars)

        data[timeframe] = bars

        print(
            f"{timeframe}: "
            f"{len(bars)} swiec"
        )

    m5 = data["M5"]

    trades = []

    reasons = {}

    next_allowed_ts = 0

    printed_setup = set()

    for i in range(
        60,
        len(m5)
    ):

        timestamp = m5[i]["ts"]

        if timestamp < next_allowed_ts:
            continue

        signal, reason = signal_at(
            timestamp,
            data
        )

        reasons[reason] = (
            reasons.get(
                reason,
                0
            )
            + 1
        )

        if reason in (
            "LONG_CONFIRMATION",
            "SHORT_CONFIRMATION",
        ):

            h1_index = closed(
                data["H1"],
                timestamp,
                3600
            )

            if (
                h1_index >= 0
                and
                h1_index
                not in printed_setup
            ):

                print(
                    "H1 SETUP BEZ "
                    "POTWIERDZENIA:",
                    iso(timestamp),
                    reason
                )

                printed_setup.add(
                    h1_index
                )

        if signal is None:
            continue

        print(
            "SETUP POTWIERDZONY:",
            iso(timestamp),
            signal["side"],
            "RR",
            round(
                signal["rr"],
                2
            )
        )

        if (
            i + 1
            >= len(m5)
        ):
            break

        (
            exit_ts,
            exit_price,
            result_r,
            exit_reason
        ) = simulate_trade(
            signal,
            m5,
            i + 1
        )

        trades.append(
            {
                "case_id":
                    f"NAS100-"
                    f"{len(trades) + 1:05d}",

                "signal_time":
                    iso(
                        timestamp
                    ),

                "exit_time":
                    iso(
                        exit_ts
                    ),

                "side":
                    signal["side"],

                "entry":
                    round(
                        signal["entry"],
                        4
                    ),

                "sl":
                    round(
                        signal["sl"],
                        4
                    ),

                "tp":
                    round(
                        signal["tp"],
                        4
                    ),

                "rr":
                    round(
                        signal["rr"],
                        3
                    ),

                "exit_price":
                    round(
                        exit_price,
                        4
                    ),

                "result_r":
                    round(
                        result_r,
                        3
                    ),

                "exit_reason":
                    exit_reason,
            }
        )

        next_allowed_ts = (
            exit_ts + 300
        )

    total = len(
        trades
    )

    wins = sum(
        trade["result_r"] > 0
        for trade in trades
    )

    losses = (
        total - wins
    )

    total_r = sum(
        trade["result_r"]
        for trade in trades
    )

    win_rate = (
        wins / total * 100
        if total
        else 0.0
    )

    balance = 10000.0
    peak = balance
    max_drawdown = 0.0

    for trade in trades:

        balance *= (
            1
            + 0.01
            * trade["result_r"]
        )

        peak = max(
            peak,
            balance
        )

        drawdown = (
            (peak - balance)
            / peak
            * 100
        )

        max_drawdown = max(
            max_drawdown,
            drawdown
        )

    fields = [
        "case_id",
        "signal_time",
        "exit_time",
        "side",
        "entry",
        "sl",
        "tp",
        "rr",
        "exit_price",
        "result_r",
        "exit_reason",
    ]

    with open(
        "nas100_backtest_trades.csv",
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()

        writer.writerows(
            trades
        )

    summary = {
        "symbol": SYMBOL,
        "lookback_days":
            LOOKBACK_DAYS,

        "bars": {
            key: len(value)
            for key, value
            in data.items()
        },

        "trades":
            total,

        "wins":
            wins,

        "losses":
            losses,

        "win_rate_pct":
            round(
                win_rate,
                2
            ),

        "total_r":
            round(
                total_r,
                3
            ),

        "ending_balance":
            round(
                balance,
                2
            ),

        "max_drawdown_pct":
            round(
                max_drawdown,
                2
            ),

        "diagnostics":
            reasons,
    }

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

    print(
        "========================================"
    )

    print(
        " DIAGNOSTYKA"
    )

    print(
        "========================================"
    )

    for (
        reason,
        count
    ) in sorted(
        reasons.items(),
        key=lambda x: -x[1]
    ):

        print(
            f"{reason}: {count}"
        )

    print()

    print(
        "========================================"
    )

    print(
        " WYNIK BACKTESTU"
    )

    print(
        "========================================"
    )

    print(
        f"Trade'y:       {total}"
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
        f"Saldo koncowe: {balance:.2f}"
    )

    print(
        f"Max DD:        {max_drawdown:.2f}%"
    )

    print(
        "========================================"
    )


if __name__ == "__main__":
    main()
