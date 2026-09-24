import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
SYMBOL = "NAS100_USDT"

LOOKBACK_DAYS = 90
REQUEST_LIMIT = 2000
REQUEST_PAUSE = 0.15

TIMEFRAMES = {
    "D1": ("Day1", 86400),
    "H4": ("Hour4", 14400),
    "H1": ("Min60", 3600),
    "M15": ("Min15", 900),
    "M5": ("Min5", 300),
}

EMA_FAST = 9
EMA_MID = 18
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

ATR_SL_MULT = 1.5
MIN_RR = 1.5
TRAIL_ACTIVATION_R = 1.0
MAX_BARS_IN_TRADE = 288

START_BALANCE = 10000.0
RISK_PCT = 1.0


def http_get(url, retries=4):
    last_error = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NAS100-Backtest/2.0"}
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except Exception as exc:
            last_error = exc
            time.sleep(1.0 + attempt)

    raise RuntimeError(
        f"MEXC request failed: {last_error}"
    )


def parse_klines(obj):
    data = obj.get("data", obj)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Nieznany format MEXC: {obj}"
        )

    required = (
        "time",
        "open",
        "high",
        "low",
        "close"
    )

    if any(key not in data for key in required):
        raise RuntimeError(
            f"Brak danych OHLC w odpowiedzi MEXC: {obj}"
        )

    n = min(
        len(data["time"]),
        len(data["open"]),
        len(data["high"]),
        len(data["low"]),
        len(data["close"])
    )

    volumes = data.get(
        "vol",
        [0.0] * n
    )

    rows = []

    for i in range(n):
        rows.append({
            "ts": int(data["time"][i]),
            "open": float(data["open"][i]),
            "high": float(data["high"][i]),
            "low": float(data["low"][i]),
            "close": float(data["close"][i]),
            "volume": (
                float(volumes[i])
                if i < len(volumes)
                else 0.0
            ),
        })

    return rows


def fetch_history(
    interval,
    seconds_per_bar,
    days=LOOKBACK_DAYS
):
    now = int(time.time())

    wanted_start = (
        now - days * 86400
    )

    end = now

    all_rows = []

    safety = 0

    while (
        end > wanted_start
        and safety < 100
    ):

        safety += 1

        span = (
            REQUEST_LIMIT
            * seconds_per_bar
        )

        start = max(
            wanted_start,
            end - span
        )

        params = urllib.parse.urlencode({
            "interval": interval,
            "start": start,
            "end": end,
        })

        url = (
            f"{BASE_URL}/{SYMBOL}?{params}"
        )

        rows = parse_klines(
            http_get(url)
        )

        if not rows:
            break

        all_rows.extend(rows)

        oldest = min(
            row["ts"]
            for row in rows
        )

        newest = max(
            row["ts"]
            for row in rows
        )

        if oldest <= wanted_start:
            break

        if oldest >= end:
            break

        end = (
            oldest
            - seconds_per_bar
        )

        time.sleep(
            REQUEST_PAUSE
        )

    unique = {
        row["ts"]: row
        for row in all_rows
    }

    result = [
        unique[key]
        for key in sorted(unique)
        if key >= wanted_start
    ]

    return result


def ema(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    value = (
        sum(values[:period])
        / period
    )

    result[period - 1] = value

    alpha = 2.0 / (period + 1)

    for i in range(
        period,
        len(values)
    ):
        value = (
            values[i] * alpha
            + value * (1.0 - alpha)
        )

        result[i] = value

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gain = 0.0
    loss = 0.0

    for i in range(
        1,
        period + 1
    ):
        change = (
            values[i]
            - values[i - 1]
        )

        gain += max(
            change,
            0.0
        )

        loss += max(
            -change,
            0.0
        )

    avg_gain = gain / period
    avg_loss = loss / period

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
            100.0
            - 100.0
            / (1.0 + rs)
        )

    result[period] = calculate(
        avg_gain,
        avg_loss
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
            0.0
        )

        current_loss = max(
            -change,
            0.0
        )

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + current_gain
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + current_loss
        ) / period

        result[i] = calculate(
            avg_gain,
            avg_loss
        )

    return result


def atr(bars, period=14):
    result = [None] * len(bars)

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
                )
            )

        true_ranges.append(
            value
        )

    if len(true_ranges) <= period:
        return result

    value = (
        sum(
            true_ranges[1:period + 1]
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
                value
                * (period - 1)
            )
            + true_ranges[i]
        ) / period

        result[i] = value

    return result


def add_indicators(bars):
    closes = [
        bar["close"]
        for bar in bars
    ]

    ema9 = ema(
        closes,
        EMA_FAST
    )

    ema18 = ema(
        closes,
        EMA_MID
    )

    ema50 = ema(
        closes,
        EMA_SLOW
    )

    rsi_values = rsi(
        closes,
        RSI_PERIOD
    )

    atr_values = atr(
        bars,
        ATR_PERIOD
    )

    for i, bar in enumerate(bars):

        bar["ema9"] = ema9[i]
        bar["ema18"] = ema18[i]
        bar["ema50"] = ema50[i]

        bar["rsi"] = rsi_values[i]
        bar["atr"] = atr_values[i]


def latest_index_at_or_before(
    bars,
    timestamp
):
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


def closed_index_at_time(
    bars,
    timestamp,
    seconds_per_bar
):
    return latest_index_at_or_before(
        bars,
        timestamp - seconds_per_bar
    )


def recent_levels(
    h1,
    index,
    lookback=30
):
    start = max(
        0,
        index - lookback
    )

    end = max(
        start,
        index - 1
    )

    window = h1[start:end]

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


def htf_direction(
    d1_bar,
    h4_bar,
    h1_bar
):
    bullish = 0
    bearish = 0

    for bar in (
        d1_bar,
        h4_bar,
        h1_bar
    ):

        if (
            not bar
            or bar["ema18"] is None
            or bar["ema50"] is None
        ):
            continue

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


def score_signal(
    h1_bar,
    m15_bar,
    m5_bar,
    side
):
    score = 0

    if side == "LONG":

        if (
            h1_bar["close"]
            > h1_bar["ema50"]
        ):
            score += 10

        if (
            h1_bar["ema18"]
            > h1_bar["ema50"]
        ):
            score += 10

        if (
            m15_bar["close"]
            > m15_bar["ema18"]
        ):
            score += 10

        if (
            m15_bar["ema9"]
            > m15_bar["ema18"]
        ):
            score += 10

        if (
            m5_bar["close"]
            > m5_bar["ema9"]
        ):
            score += 10

        if (
            40
            <= h1_bar["rsi"]
            <= 70
        ):
            score += 10

    else:

        if (
            h1_bar["close"]
            < h1_bar["ema50"]
        ):
            score += 10

        if (
            h1_bar["ema18"]
            < h1_bar["ema50"]
        ):
            score += 10

        if (
            m15_bar["close"]
            < m15_bar["ema18"]
        ):
            score += 10

        if (
            m15_bar["ema9"]
            < m15_bar["ema18"]
        ):
            score += 10

        if (
            m5_bar["close"]
            < m5_bar["ema9"]
        ):
            score += 10

        if (
            30
            <= h1_bar["rsi"]
            <= 60
        ):
            score += 10

    return score


def build_signal(
    timestamp,
    data
):
    d1 = data["D1"]
    h4 = data["H4"]
    h1 = data["H1"]
    m15 = data["M15"]
    m5 = data["M5"]

    h1_index = closed_index_at_time(
        h1,
        timestamp,
        3600
    )

    m15_index = closed_index_at_time(
        m15,
        timestamp,
        900
    )

    m5_index = latest_index_at_or_before(
        m5,
        timestamp
    )

    if (
        h1_index < 31
        or m15_index < 20
        or m5_index < 20
    ):
        return None

    h1_bar = h1[h1_index]
    m15_bar = m15[m15_index]
    m5_bar = m5[m5_index]

    required = (
        h1_bar["atr"],
        h1_bar["ema18"],
        h1_bar["ema50"],
        h1_bar["rsi"],
        m15_bar["ema9"],
        m15_bar["ema18"],
        m5_bar["ema9"]
    )

    if any(
        value is None
        for value in required
    ):
        return None

    d1_index = latest_index_at_or_before(
        d1,
        timestamp - 86400
    )

    h4_index = latest_index_at_or_before(
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

    direction = htf_direction(
        d1_bar,
        h4_bar,
        h1_bar
    )

    resistance, support = recent_levels(
        h1,
        h1_index
    )

    if (
        resistance is None
        or support is None
    ):
        return None

    previous = h1[
        h1_index - 1
    ]

    long_sweep = (
        previous["low"] < support
        and h1_bar["close"] > support
    )

    short_sweep = (
        previous["high"] > resistance
        and h1_bar["close"] < resistance
    )

    long_confirmation = (
        m15_bar["close"]
        > m15_bar["ema18"]
        and
        m15_bar["ema9"]
        > m15_bar["ema18"]
        and
        m5_bar["close"]
        > m5_bar["ema9"]
    )

    short_confirmation = (
        m15_bar["close"]
        < m15_bar["ema18"]
        and
        m15_bar["ema9"]
        < m15_bar["ema18"]
        and
        m5_bar["close"]
        < m5_bar["ema9"]
    )

    if (
        direction == "BULLISH"
        and long_sweep
        and long_confirmation
        and 40 <= h1_bar["rsi"] <= 70
    ):

        entry = m5_bar["close"]

        sl = (
            min(
                previous["low"],
                support
            )
            - h1_bar["atr"]
            * ATR_SL_MULT
        )

        tp = resistance

        risk = entry - sl
        reward = tp - entry

        if (
            risk <= 0
            or reward <= 0
        ):
            return None

        rr = reward / risk

        if rr < MIN_RR:
            return None

        return {
            "side": "LONG",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "score": score_signal(
                h1_bar,
                m15_bar,
                m5_bar,
                "LONG"
            ),
            "signal_ts": timestamp,
            "level": support,
        }

    if (
        direction == "BEARISH"
        and short_sweep
        and short_confirmation
        and 30 <= h1_bar["rsi"] <= 60
    ):

        entry = m5_bar["close"]

        sl = (
            max(
                previous["high"],
                resistance
            )
            + h1_bar["atr"]
            * ATR_SL_MULT
        )

        tp = support

        risk = sl - entry
        reward = entry - tp

        if (
            risk <= 0
            or reward <= 0
        ):
            return None

        rr = reward / risk

        if rr < MIN_RR:
            return None

        return {
            "side": "SHORT",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "score": score_signal(
                h1_bar,
                m15_bar,
                m5_bar,
                "SHORT"
            ),
            "signal_ts": timestamp,
            "level": resistance,
        }

    return None


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
        + MAX_BARS_IN_TRADE
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
                > TRAIL_ACTIVATION_R
                * risk
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
                > TRAIL_ACTIVATION_R
                * risk
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
        tz=timezone.utc
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

        interval, seconds = values

        print(
            f"Pobieranie {timeframe}..."
        )

        bars = fetch_history(
            interval,
            seconds
        )

        if len(bars) < 60:
            raise RuntimeError(
                f"Za mało danych dla {timeframe}: "
                f"{len(bars)} świec"
            )

        add_indicators(
            bars
        )

        data[timeframe] = bars

        print(
            f"{timeframe}: "
            f"{len(bars)} świec"
        )

    m5 = data["M5"]

    trades = []

    next_allowed_ts = 0

    for i in range(
        60,
        len(m5)
    ):

        bar = m5[i]

        timestamp = bar["ts"]

        if timestamp < next_allowed_ts:
            continue

        signal = build_signal(
    timestamp,
    data
)

if signal:
    print("SETUP:", timestamp, signal)

if not signal:
    continue

if i + 1 >= len(m5): break

exit_ts, exit_price, result_r, exit_reason = simulate_trade(signal, m5, i + 1)   

trades.append({
            "case_id":
                f"NAS100-{len(trades) + 1:05d}",

            "signal_time":
                iso(signal["signal_ts"]),

            "exit_time":
                iso(exit_ts),

            "side":
                signal["side"],

            "score":
                signal["score"],

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
        })

        next_allowed_ts = (
            exit_ts + 300
        )

    total = len(trades)

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

    balance = START_BALANCE

    peak = balance

    max_drawdown = 0.0

    for trade in trades:

        balance *= (
            1.0
            + (
                RISK_PCT
                / 100.0
            )
            * trade["result_r"]
        )

        peak = max(
            peak,
            balance
        )

        drawdown = (
            (peak - balance)
            / peak
            * 100.0
        )

        max_drawdown = max(
            max_drawdown,
            drawdown
        )

    summary = {
        "symbol": SYMBOL,

        "market":
            "MEXC Futures",

        "lookback_days":
            LOOKBACK_DAYS,

        "timeframes":
            list(TIMEFRAMES.keys()),

        "bars": {
            timeframe: len(bars)
            for timeframe, bars
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

        "starting_balance":
            START_BALANCE,

        "ending_balance":
            round(
                balance,
                2
            ),

        "risk_per_trade_pct":
            RISK_PCT,

        "max_drawdown_pct":
            round(
                max_drawdown,
                2
            ),

        "minimum_rr":
            MIN_RR,

        "trailing_activation_r":
            TRAIL_ACTIVATION_R,

        "atr_sl_multiplier":
            ATR_SL_MULT,
    }

    fields = [
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
            fieldnames=fields
        )

        writer.writeheader()

        writer.writerows(
            trades
        )

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
        f"Saldo końcowe: {balance:.2f}"
    )

    print(
        f"Max DD:        {max_drawdown:.2f}%"
    )

    print(
        "========================================"
    )

    print(
        "Pliki:"
    )

    print(
        "nas100_backtest_trades.csv"
    )

    print(
        "nas100_backtest_summary.json"
    )


if __name__ == "__main__":
    main()
