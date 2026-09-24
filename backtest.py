import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone

import requests


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

INITIAL_BALANCE = 10000.0
RISK_PER_TRADE_R = 1.0

MIN_RR = 1.5
ATR_SL = 1.5
TRAIL_R = 1.0

LOOKBACK_LEVELS = 30

MAX_M15_CONFIRM_BARS = 8
MAX_M5_ENTRY_BARS = 12
MAX_TRADE_M5_BARS = 288

OUT_TRADES = "nas100_backtest_trades.csv"
OUT_SUMMARY = "nas100_backtest_summary.json"


def get_json(url, params, retries=4):
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=20,
            )

            response.raise_for_status()

            data = response.json()

            if isinstance(data, dict) and data.get("success") is False:
                raise RuntimeError(str(data))

            return data

        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)

    raise RuntimeError(
        f"MEXC request failed: {last_error}"
    )


def fetch_klines(
    interval,
    seconds_per_bar,
    start_ts,
    end_ts,
):
    bars = []
    cursor = start_ts
    safety = 0

    while cursor < end_ts and safety < 100:
        safety += 1

        batch_end = min(
            end_ts,
            cursor + seconds_per_bar * (LIMIT - 1),
        )

        params = {
            "interval": interval,
            "start": int(cursor),
            "end": int(batch_end),
        }

        payload = get_json(
            f"{BASE_URL}/{SYMBOL}",
            params,
        )

        rows = (
            payload.get("data", [])
            if isinstance(payload, dict)
            else []
        )

        if not rows:
            break

        times = rows["time"]

        for i in range(len(times)):
            ts = int(times[i])

            if ts < start_ts or ts >= end_ts:
                continue

            bars.append(
                {
                    "ts": ts,
                    "open": float(rows["open"][i]),
                    "high": float(rows["high"][i]),
                    "low": float(rows["low"][i]),
                    "close": float(rows["close"][i]),
                    "vol": float(rows["vol"][i]),
                }
            )

        last_ts = int(times[-1])

        if last_ts < cursor:
            break

        next_cursor = last_ts + seconds_per_bar

        if next_cursor <= cursor:
            break

        cursor = next_cursor

    unique = {
        bar["ts"]: bar
        for bar in bars
    }

    return [
        unique[ts]
        for ts in sorted(unique)
    ]


def ema(values, period):
    out = [None] * len(values)

    if len(values) < period:
        return out

    seed = sum(values[:period]) / period

    out[period - 1] = seed

    alpha = 2.0 / (period + 1.0)

    previous = seed

    for i in range(period, len(values)):
        previous = (
            (values[i] - previous) * alpha
            + previous
        )

        out[i] = previous

    return out


def rsi(values, period=14):
    out = [None] * len(values)

    if len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0

    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]

        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (
            100.0 / (1.0 + rs)
        )

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]

        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        avg_gain = (
            (avg_gain * (period - 1))
            + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + loss
        ) / period

        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss

            out[i] = 100.0 - (
                100.0 / (1.0 + rs)
            )

    return out


def atr(bars, period=14):
    out = [None] * len(bars)

    if len(bars) <= period:
        return out

    true_ranges = [0.0] * len(bars)

    for i, bar in enumerate(bars):

        if i == 0:
            true_ranges[i] = (
                bar["high"] - bar["low"]
            )

        else:
            previous_close = bars[i - 1]["close"]

            true_ranges[i] = max(
                bar["high"] - bar["low"],
                abs(
                    bar["high"]
                    - previous_close
                ),
                abs(
                    bar["low"]
                    - previous_close
                ),
            )

    value = (
        sum(true_ranges[1:period + 1])
        / period
    )

    out[period] = value

    for i in range(period + 1, len(bars)):
        value = (
            (value * (period - 1))
            + true_ranges[i]
        ) / period

        out[i] = value

    return out


def add_indicators(bars):
    closes = [
        bar["close"]
        for bar in bars
    ]

    ema9 = ema(closes, 9)
    ema18 = ema(closes, 18)
    ema50 = ema(closes, 50)

    rsi_values = rsi(
        closes,
        14,
    )

    atr_values = atr(
        bars,
        14,
    )

    result = []

    for i, bar in enumerate(bars):

        item = dict(bar)

        item["ema9"] = ema9[i]
        item["ema18"] = ema18[i]
        item["ema50"] = ema50[i]
        item["rsi"] = rsi_values[i]
        item["atr"] = atr_values[i]

        result.append(item)

    return result


def latest_at_or_before(
    bars,
    ts,
):
    low = 0
    high = len(bars) - 1
    answer = None

    while low <= high:

        middle = (low + high) // 2

        if bars[middle]["ts"] <= ts:
            answer = bars[middle]
            low = middle + 1

        else:
            high = middle - 1

    return answer


def first_after(
    bars,
    ts,
):
    low = 0
    high = len(bars)

    while low < high:

        middle = (low + high) // 2

        if bars[middle]["ts"] <= ts:
            low = middle + 1

        else:
            high = middle

    return low


def levels_from_h1(
    h1,
    index,
    lookback=LOOKBACK_LEVELS,
):
    start = max(
        0,
        index - lookback,
    )

    end = max(
        start,
        index - 1,
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
    ts,
    d1,
    h4,
    h1,
):
    d1_bar = latest_at_or_before(
        d1,
        ts - 1,
    )

    h4_bar = latest_at_or_before(
        h4,
        ts - 1,
    )

    h1_bar = latest_at_or_before(
        h1,
        ts,
    )

    bars = [
        d1_bar,
        h4_bar,
        h1_bar,
    ]

    if any(
        bar is None
        or bar["ema18"] is None
        or bar["ema50"] is None
        for bar in bars
    ):
        return None

    bullish = sum(
        bar["ema18"] > bar["ema50"]
        and bar["close"] > bar["ema50"]
        for bar in bars
    )

    bearish = sum(
        bar["ema18"] < bar["ema50"]
        and bar["close"] < bar["ema50"]
        for bar in bars
    )

    if bullish >= 2:
        return "LONG"

    if bearish >= 2:
        return "SHORT"

    return None


def detect_h1_setup(
    h1,
    index,
    direction,
):
    if index < 20:
        return None

    resistance, support = levels_from_h1(
        h1,
        index,
    )

    if resistance is None or support is None:
        return None

    current = h1[index]
    previous = h1[index - 1]

    atr_value = current["atr"]

    if atr_value is None:
        return None

    if direction == "LONG":

        swept = (
            previous["low"] < support
            and previous["close"] > support
        )

        momentum = (
            current["close"] > current["open"]
            and current["close"]
            > previous["close"]
        )

        if swept and momentum:

            return {
                "direction": "LONG",
                "setup_ts": current["ts"],
                "setup_index": index,
                "setup_end_ts": (
                    current["ts"]
                    + TIMEFRAMES["H1"][1]
                ),
                "support": support,
                "resistance": resistance,
                "atr": atr_value,
            }

    if direction == "SHORT":

        swept = (
            previous["high"] > resistance
            and previous["close"] < resistance
        )

        momentum = (
            current["close"] < current["open"]
            and current["close"]
            < previous["close"]
        )

        if swept and momentum:

            return {
                "direction": "SHORT",
                "setup_ts": current["ts"],
                "setup_index": index,
                "setup_end_ts": (
                    current["ts"]
                    + TIMEFRAMES["H1"][1]
                ),
                "support": support,
                "resistance": resistance,
                "atr": atr_value,
            }

    return None


def confirm_m15(
    m15,
    start_ts,
    direction,
):
    start = first_after(
        m15,
        start_ts,
    )

    end = min(
        len(m15),
        start + MAX_M15_CONFIRM_BARS,
    )

    for i in range(start, end):

        bar = m15[i]

        if (
            bar["ema18"] is None
            or bar["ema50"] is None
            or bar["rsi"] is None
        ):
            continue

        if direction == "LONG":

            if (
                bar["close"] > bar["ema18"]
                and bar["ema18"] > bar["ema50"]
                and 52 <= bar["rsi"] <= 72
            ):
                return i, bar

        else:

            if (
                bar["close"] < bar["ema18"]
                and bar["ema18"] < bar["ema50"]
                and 28 <= bar["rsi"] <= 48
            ):
                return i, bar

    return None, None


def find_m5_entry(
    m5,
    start_ts,
    direction,
):
    start = first_after(
        m5,
        start_ts,
    )

    end = min(
        len(m5),
        start + MAX_M5_ENTRY_BARS,
    )

    for i in range(start, end):

        bar = m5[i]

        if (
            bar["ema9"] is None
            or bar["ema18"] is None
            or bar["rsi"] is None
        ):
            continue

        if direction == "LONG":

            if (
                bar["close"] > bar["ema9"]
                and bar["ema9"] > bar["ema18"]
                and bar["rsi"] >= 50
            ):
                return i, bar

        else:

            if (
                bar["close"] < bar["ema9"]
                and bar["ema9"] < bar["ema18"]
                and bar["rsi"] <= 50
            ):
                return i, bar

    return None, None


def build_trade(
    setup,
    entry_index,
    entry_bar,
):
    direction = setup["direction"]

    entry = entry_bar["close"]

    atr_value = (
        entry_bar["atr"]
        or setup["atr"]
    )

    if not atr_value or atr_value <= 0:
        return None, "NO_ATR"

    if direction == "LONG":

        structural_sl = min(
            setup["support"],
            entry - ATR_SL * atr_value,
        )

        risk = entry - structural_sl

        target = setup["resistance"]

        if target <= entry:
            return None, "NO_LONG_TARGET"

        if risk <= 0:
            return None, "NO_RISK"

        rr = (
            target - entry
        ) / risk

        if rr < MIN_RR:
            return None, "RR_LT_MIN"

        return {
            "direction": direction,
            "setup_ts": setup["setup_ts"],
            "entry_ts": entry_bar["ts"],
            "entry_index": entry_index,
            "entry": entry,
            "sl": structural_sl,
            "tp": target,
            "risk": risk,
            "rr": rr,
        }, None

    structural_sl = max(
        setup["resistance"],
        entry + ATR_SL * atr_value,
    )

    risk = structural_sl - entry

    target = setup["support"]

    if target >= entry:
        return None, "NO_SHORT_TARGET"

    if risk <= 0:
        return None, "NO_RISK"

    rr = (
        entry - target
    ) / risk

    if rr < MIN_RR:
        return None, "RR_LT_MIN"

    return {
        "direction": direction,
        "setup_ts": setup["setup_ts"],
        "entry_ts": entry_bar["ts"],
        "entry_index": entry_index,
        "entry": entry,
        "sl": structural_sl,
        "tp": target,
        "risk": risk,
        "rr": rr,
    }, None


def simulate_trade(
    trade,
    m5,
):
    start = trade["entry_index"] + 1

    end = min(
        len(m5),
        start + MAX_TRADE_M5_BARS,
    )

    direction = trade["direction"]

    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    risk = trade["risk"]

    trail_active = False
    best = entry

    for i in range(start, end):

        bar = m5[i]

        if direction == "LONG":

            best = max(
                best,
                bar["high"],
            )

            if (
                not trail_active
                and best - entry > TRAIL_R * risk
            ):
                trail_active = True

            if bar["low"] <= sl:
                return (
                    -1.0,
                    bar["ts"],
                    "SL",
                )

            if bar["high"] >= tp:
                return (
                    (tp - entry) / risk,
                    bar["ts"],
                    "TP",
                )

            if trail_active:

                trail = max(
                    sl,
                    best - risk,
                )

                if bar["low"] <= trail:
                    return (
                        (trail - entry) / risk,
                        bar["ts"],
                        "TRAIL",
                    )

        else:

            best = min(
                best,
                bar["low"],
            )

            if (
                not trail_active
                and entry - best > TRAIL_R * risk
            ):
                trail_active = True

            if bar["high"] >= sl:
                return (
                    -1.0,
                    bar["ts"],
                    "SL",
                )

            if bar["low"] <= tp:
                return (
                    (entry - tp) / risk,
                    bar["ts"],
                    "TP",
                )

            if trail_active:

                trail = min(
                    sl,
                    best + risk,
                )

                if bar["high"] >= trail:
                    return (
                        (entry - trail) / risk,
                        bar["ts"],
                        "TRAIL",
                    )

    if end <= start:
        return (
            0.0,
            trade["entry_ts"],
            "NO_DATA",
        )

    last = m5[end - 1]

    if direction == "LONG":
        pnl_r = (
            last["close"] - entry
        ) / risk
    else:
        pnl_r = (
            entry - last["close"]
        ) / risk

    return (
        pnl_r,
        last["ts"],
        "TIMEOUT",
    )


def fmt_ts(ts):
    return datetime.fromtimestamp(
        ts,
        tz=timezone.utc,
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def main():

    end_ts = int(time.time())

    start_ts = (
        end_ts
        - LOOKBACK_DAYS * 86400
    )

    print("=" * 50)
    print("NAS100 BACKTEST")
    print("=" * 50)

    print(
        f"Symbol: {SYMBOL}"
    )

    print(
        f"Period: "
        f"{fmt_ts(start_ts)}"
        f" -> "
        f"{fmt_ts(end_ts)}"
    )

    data = {}

    for name, (
        interval,
        seconds_per_bar,
    ) in TIMEFRAMES.items():

        print(
            f"Pobieranie {name}..."
        )

        bars = fetch_klines(
            interval,
            seconds_per_bar,
            start_ts,
            end_ts,
        )

        data[name] = add_indicators(
            bars
        )

        print(
            f"{name}: "
            f"{len(data[name])} swiec"
        )

    d1 = data["D1"]
    h4 = data["H4"]
    h1 = data["H1"]
    m15 = data["M15"]
    m5 = data["M5"]

    stats = Counter()

    trades = []

    used_setup_ts = set()

    print()
    print("=" * 50)
    print("DIAGNOSTYKA")
    print("=" * 50)

    for i in range(20, len(h1)):

        h1_bar = h1[i]

        direction = htf_direction(
            h1_bar["ts"],
            d1,
            h4,
            h1,
        )

        if direction is None:
            stats[
                "NO_HTF_DIRECTION"
            ] += 1

            continue

        stats["HTF"] += 1

        setup = detect_h1_setup(
            h1,
            i,
            direction,
        )

        if setup is None:

            if direction == "LONG":
                stats[
                    "NO_LONG_SWEEP"
                ] += 1

            else:
                stats[
                    "NO_SHORT_SWEEP"
                ] += 1

            continue

        if (
            setup["setup_ts"]
            in used_setup_ts
        ):
            stats[
                "DUPLICATE_SETUP"
            ] += 1

            continue

        used_setup_ts.add(
            setup["setup_ts"]
        )

        stats[
            f"H1_SETUP_{direction}"
        ] += 1

        print(
            f"H1 SETUP {direction}: "
            f"{fmt_ts(setup['setup_ts'])}"
        )

        m15_index, m15_bar = confirm_m15(
            m15,
            setup["setup_end_ts"],
            direction,
        )

        if m15_bar is None:

            stats[
                f"NO_M15_CONFIRM_{direction}"
            ] += 1

            print(
                "  -> brak M15 confirmation"
            )

            continue

        stats[
            f"M15_CONFIRM_{direction}"
        ] += 1

        print(
            "  -> M15 confirmation: "
            f"{fmt_ts(m15_bar['ts'])}"
        )

        m5_start_ts = (
            m15_bar["ts"]
            + TIMEFRAMES["M15"][1]
        )

        m5_index, m5_bar = find_m5_entry(
            m5,
            m5_start_ts,
            direction,
        )

        if m5_bar is None:

            stats[
                f"NO_M5_ENTRY_{direction}"
            ] += 1

            print(
                "  -> M15 OK, "
                "brak M5 entry"
            )

            continue

        stats[
            f"M5_ENTRY_{direction}"
        ] += 1

        print(
            "  -> M5 entry: "
            f"{fmt_ts(m5_bar['ts'])}"
        )

        trade, reject_reason = build_trade(
            setup,
            m5_index,
            m5_bar,
        )

        if trade is None:

            stats[
                reject_reason
            ] += 1

            print(
                "  -> M5 OK, "
                f"odrzucone: "
                f"{reject_reason}"
            )

            continue

        pnl_r, exit_ts, exit_reason = (
            simulate_trade(
                trade,
                m5,
            )
        )

        trade["m15_ts"] = m15_bar["ts"]
        trade["m5_ts"] = m5_bar["ts"]

        trade["exit_ts"] = exit_ts
        trade["exit_reason"] = exit_reason
        trade["pnl_r"] = pnl_r

        trades.append(trade)

        stats["TRADE"] += 1

        print(
            "  -> TRADE "
            f"{direction} "
            f"{pnl_r:.3f}R "
            f"{exit_reason}"
        )

    wins = sum(
        trade["pnl_r"] > 0
        for trade in trades
    )

    losses = sum(
        trade["pnl_r"] <= 0
        for trade in trades
    )

    total_r = sum(
        trade["pnl_r"]
        for trade in trades
    )

    balance = INITIAL_BALANCE
    peak = balance
    max_dd = 0.0

    for trade in trades:

        balance += (
            trade["pnl_r"]
            * 100.0
        )

        peak = max(
            peak,
            balance,
        )

        if peak:
            dd = (
                (peak - balance)
                / peak
                * 100.0
            )
        else:
            dd = 0.0

        max_dd = max(
            max_dd,
            dd,
        )

    print()
    print("=" * 50)
    print("DIAGNOSTYKA - PODSUMOWANIE")
    print("=" * 50)

    for key in sorted(stats):
        print(
            f"{key}: {stats[key]}"
        )

    print()
    print("=" * 50)
    print("WYNIK BACKTESTU")
    print("=" * 50)

    print(
        f"Trade'y: {len(trades)}"
    )

    print(
        f"Wygrane: {wins}"
    )

    print(
        f"Przegrane: {losses}"
    )

    win_rate = (
        wins / len(trades) * 100
        if trades
        else 0.0
    )

    print(
        f"Win rate: "
        f"{win_rate:.2f}%"
    )

    print(
        f"Wynik: "
        f"{total_r:.3f} R"
    )

    print(
        f"Saldo koncowe: "
        f"{balance:.2f}"
    )

    print(
        f"Max DD: "
        f"{max_dd:.2f}%"
    )

    with open(
        OUT_TRADES,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "direction",
                "setup_ts",
                "m15_ts",
                "m5_ts",
                "entry_ts",
                "entry",
                "sl",
                "tp",
                "risk",
                "rr",
                "exit_ts",
                "exit_reason",
                "pnl_r",
            ],
        )

        writer.writeheader()

        for trade in trades:

            row = dict(trade)

            for key in (
                "setup_ts",
                "m15_ts",
                "m5_ts",
                "entry_ts",
                "exit_ts",
            ):
                row[key] = fmt_ts(
                    row[key]
                )

            writer.writerow({
                key: row.get(key)
                for key in
writer.fieldnames
            })
        

    summary = {
        "symbol": SYMBOL,
        "lookback_days": LOOKBACK_DAYS,
        "bars": {
            name: len(values)
            for name, values in data.items()
        },
        "diagnostics": dict(stats),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "result_r": total_r,
        "final_balance": balance,
        "max_drawdown_pct": max_dd,
    }

    with open(
        OUT_SUMMARY,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            summary,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("Zapisano:")
    print(OUT_TRADES)
    print(OUT_SUMMARY)


if __name__ == "__main__":
    main()
