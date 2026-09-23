import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
import html
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

SYMBOL = "NAS100_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
POLL_SECONDS = 30

D1_LIMIT = 120
H4_LIMIT = 180
H1_LIMIT = 240
M15_LIMIT = 400
M5_LIMIT = 600

DB_FILE = "/tmp/nas100_market_history.db"

TRAIL_ACTIVATION_R = 1.0
TRAIL_ATR_MULT = 1.5
MIN_RR = 1.0

PORT = int(os.environ.get("PORT", "3000"))

INTERVALS = {
    "D1": "Day1",
    "H4": "Hour4",
    "H1": "Min60",
    "M15": "Min15",
    "M5": "Min5",
}


def log(message):
    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] "
        f"{message}",
        flush=True
    )


def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT,
            price REAL,
            payload TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            direction TEXT NOT NULL,
            market_state TEXT NOT NULL,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            rr REAL,
            score INTEGER,
            key_level REAL,
            status TEXT NOT NULL,
            outcome TEXT,
            payload TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def state_get(key, default=None):
    conn = db()
    row = conn.execute(
        "SELECT value FROM state WHERE key=?",
        (key,)
    ).fetchone()
    conn.close()

    if row is None:
        return default

    return json.loads(row["value"])


def state_set(key, value):
    conn = db()

    conn.execute(
        """
        INSERT INTO state(key,value)
        VALUES(?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, json.dumps(value))
    )

    conn.commit()
    conn.close()


def event(event_type, direction=None, price=None, payload=None):
    conn = db()

    conn.execute(
        """
        INSERT INTO events(ts,event_type,direction,price,payload)
        VALUES(?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            event_type,
            direction,
            price,
            json.dumps(payload or {}, ensure_ascii=False)
        )
    )

    conn.commit()
    conn.close()


def mexc_get(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NAS100-Market-Engine/1.0",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get_klines(tf, limit):
    interval = INTERVALS[tf]

    params = urllib.parse.urlencode({
        "interval": interval,
        "limit": limit
    })

    url = f"{BASE_URL}/{SYMBOL}?{params}"

    obj = mexc_get(url)

    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        data = obj["data"]

        rows = []

        times = data.get("time", [])

        for i in range(len(times)):
            try:
                rows.append(
                    {
                        "time": int(data["time"][i]),
                        "open": float(data["open"][i]),
                        "high": float(data["high"][i]),
                        "low": float(data["low"][i]),
                        "close": float(data["close"][i]),
                        "volume": float(
                            data.get(
                                "vol",
                                [0] * len(times)
                            )[i]
                        ),
                    }
                )
            except (ValueError, TypeError, IndexError):
                continue

        rows.sort(key=lambda x: x["time"])

        return rows

    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        raw = obj["data"]

    elif isinstance(obj, list):
        raw = obj

    else:
        raise RuntimeError(
            f"Unexpected MEXC kline response: {str(obj)[:300]}"
        )

    rows = []

    for r in raw:

        if isinstance(r, dict):

            rows.append(
                {
                    "time": int(r.get("time", r.get("t"))),
                    "open": float(r.get("open", r.get("o"))),
                    "high": float(r.get("high", r.get("h"))),
                    "low": float(r.get("low", r.get("l"))),
                    "close": float(r.get("close", r.get("c"))),
                    "volume": float(
                        r.get("vol", r.get("v", 0))
                    ),
                }
            )

        else:

            rows.append(
                {
                    "time": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]) if len(r) > 5 else 0.0,
                }
            )

    rows.sort(key=lambda x: x["time"])

    return rows[-limit:]


def ema(values, period):

    if len(values) < period:
        return None

    k = 2.0 / (period + 1.0)

    e = sum(values[:period]) / period

    for v in values[period:]:
        e = v * k + e * (1.0 - k)

    return e


def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    prev = candles[0]["close"]

    for c in candles[1:]:

        trs.append(
            max(
                c["high"] - c["low"],
                abs(c["high"] - prev),
                abs(c["low"] - prev)
            )
        )

        prev = c["close"]

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        d = values[i] - values[i - 1]

        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def closed(candles):

    if len(candles) >= 3:
        return candles[:-1]

    return candles


def candle_body(c):
    return abs(c["close"] - c["open"])


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def rejection(c, side):

    body = candle_body(c)

    if body <= 0:
        body = max(c["high"] - c["low"], 1e-9)

    if side == "LONG":

        return (
            lower_wick(c) >= body * 1.2
            and c["close"] >= c["open"]
        )

    return (
        upper_wick(c) >= body * 1.2
        and c["close"] <= c["open"]
    )


def displacement(c, atr_value):

    return bool(
        atr_value
        and candle_body(c) >= atr_value * 0.8
    )


def swing_levels(candles, lookback=3):

    levels = []

    if len(candles) < lookback * 2 + 1:
        return levels

    for i in range(
        lookback,
        len(candles) - lookback
    ):

        c = candles[i]

        left = candles[i - lookback:i]
        right = candles[i + 1:i + lookback + 1]

        if c["high"] > max(
            x["high"] for x in left + right
        ):

            levels.append(
                (
                    "RESISTANCE",
                    c["high"],
                    c["time"],
                    "SWING_HIGH"
                )
            )

        if c["low"] < min(
            x["low"] for x in left + right
        ):

            levels.append(
                (
                    "SUPPORT",
                    c["low"],
                    c["time"],
                    "SWING_LOW"
                )
            )

    return levels


def day_week_levels(candles):

    if len(candles) < 3:
        return []

    out = []

    by_day = {}

    for c in candles:

        d = datetime.fromtimestamp(
            c["time"],
            tz=timezone.utc
        ).date().isoformat()

        by_day.setdefault(d, []).append(c)

    days = sorted(by_day)

    if len(days) >= 2:

        prev = by_day[days[-2]]

        out.extend(
            [
                (
                    "RESISTANCE",
                    max(x["high"] for x in prev),
                    prev[-1]["time"],
                    "PDH"
                ),
                (
                    "SUPPORT",
                    min(x["low"] for x in prev),
                    prev[-1]["time"],
                    "PDL"
                )
            ]
        )

    if len(days) >= 6:

        prev_days = days[-6:-1]

        all_prev = [
            x
            for d in prev_days
            for x in by_day[d]
        ]

        out.extend(
            [
                (
                    "RESISTANCE",
                    max(x["high"] for x in all_prev),
                    all_prev[-1]["time"],
                    "PWH"
                ),
                (
                    "SUPPORT",
                    min(x["low"] for x in all_prev),
                    all_prev[-1]["time"],
                    "PWL"
                )
            ]
        )

    return out


def build_levels(d1, h4, h1, price):

    raw = (
        swing_levels(d1, 3)
        + swing_levels(h4, 3)
        + swing_levels(h1, 3)
        + day_week_levels(h1)
    )

    if not raw:
        return []

    atr_h1 = atr(h1, 14)

    if atr_h1 is None:
        atr_h1 = max(price * 0.002, 1.0)

    zone = max(
        atr_h1 * 0.25,
        price * 0.0005
    )

    grouped = []

    for side, level, ts, source in sorted(
        raw,
        key=lambda x: x[1]
    ):

        found = next(
            (
                z for z in grouped
                if abs(z["price"] - level) <= zone
            ),
            None
        )

        if found:

            found["prices"].append(level)
            found["sources"].append(source)

            found["price"] = (
                sum(found["prices"])
                / len(found["prices"])
            )

        else:

            grouped.append(
                {
                    "side": side,
                    "price": level,
                    "prices": [level],
                    "sources": [source],
                    "ts": ts,
                    "zone": zone
                }
            )

    for z in grouped:

        reactions = len(z["prices"])

        special = sum(
            1
            for s in z["sources"]
            if s in ("PDH", "PDL", "PWH", "PWL")
        )

        z["strength"] = min(
            100,
            35 + reactions * 10 + special * 10
        )

        z["lower"] = z["price"] - z["zone"]
        z["upper"] = z["price"] + z["zone"]

        z["sources_text"] = ", ".join(
            sorted(set(z["sources"]))
        )

    return sorted(
        grouped,
        key=lambda x: abs(x["price"] - price)
    )


def target_levels(levels, price, direction):

    if direction == "LONG":

        return sorted(
            [
                z for z in levels
                if z["price"] > price
            ],
            key=lambda z: z["price"]
        )

    return sorted(
        [
            z for z in levels
            if z["price"] < price
        ],
        key=lambda z: z["price"],
        reverse=True
    )


def htf_context(d1, h4, h1):

    def trend(candles):

        c = closed(candles)

        vals = [
            x["close"]
            for x in c
        ]

        e18 = ema(vals, 18)
        e50 = ema(vals, 50)
        e100 = ema(vals, 100)

        if e50 is None or e100 is None:
            return "NEUTRAL"

        if (
            e18
            and e18 > e50 > e100
            and vals[-1] > e50
        ):
            return "BULLISH"

        if (
            e18
            and e18 < e50 < e100
            and vals[-1] < e50
        ):
            return "BEARISH"

        return "NEUTRAL"

    trends = [
        trend(d1),
        trend(h4),
        trend(h1)
    ]

    if trends.count("BULLISH") >= 2:
        main = "BULLISH"

    elif trends.count("BEARISH") >= 2:
        main = "BEARISH"

    else:
        main = "NEUTRAL"

    return {
        "main": main,
        "d1": trends[0],
        "h4": trends[1],
        "h1": trends[2]
    }


def structure(candles):

    c = closed(candles)

    if len(c) < 20:

        return {
            "state": "UNKNOWN",
            "swing_high": None,
            "swing_low": None
        }

    highs = []
    lows = []

    for i in range(2, len(c) - 2):

        if (
            c[i]["high"] > c[i - 1]["high"]
            and c[i]["high"] > c[i + 1]["high"]
        ):
            highs.append(c[i]["high"])

        if (
            c[i]["low"] < c[i - 1]["low"]
            and c[i]["low"] < c[i + 1]["low"]
        ):
            lows.append(c[i]["low"])

    if len(highs) < 2 or len(lows) < 2:

        return {
            "state": "UNKNOWN",
            "swing_high": highs[-1] if highs else None,
            "swing_low": lows[-1] if lows else None
        }

    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]

    lh = highs[-1] < highs[-2]
    ll = lows[-1] < lows[-2]

    if hh and hl:
        state = "BULLISH"

    elif lh and ll:
        state = "BEARISH"

    elif hl and ll:
        state = "CORRECTION_DOWN"

    elif hh and lh:
        state = "CORRECTION_UP"

    else:
        state = "MIXED"

    return {
        "state": state,
        "swing_high": highs[-1],
        "swing_low": lows[-1]
    }


def ema_status(candles):

    c = closed(candles)

    vals = [
        x["close"]
        for x in c
    ]

    current = vals[-1]

    out = {
        str(p): ema(vals, p)
        for p in (9, 18, 50, 100, 200)
    }

    e9 = out["9"]
    e18 = out["18"]
    e50 = out["50"]

    if (
        e9
        and e18
        and e50
        and current > e9 > e18 > e50
    ):
        out["state"] = "BULLISH_ALIGNMENT"

    elif (
        e9
        and e18
        and e50
        and current < e9 < e18 < e50
    ):
        out["state"] = "BEARISH_ALIGNMENT"

    elif e50 and current > e50:
        out["state"] = "ABOVE_EMA50"

    elif e50 and current < e50:
        out["state"] = "BELOW_EMA50"

    else:
        out["state"] = "MIXED"

    return out


def liquidity_signal(h1, levels, price):

    c = closed(h1)

    if len(c) < 3:

        return {
            "type": "NONE",
            "level": None,
            "reclaim": False,
            "rejection": False
        }

    last = c[-1]

    best = None
    best_dist = float("inf")

    for z in levels:

        swept_down = (
            last["low"] < z["lower"]
            and last["close"] > z["price"]
        )

        swept_up = (
            last["high"] > z["upper"]
            and last["close"] < z["price"]
        )

        if swept_down:

            dist = abs(
                last["low"] - z["price"]
            )

            if dist < best_dist:

                best = {
                    "type": "SWEEP_RECLAIM_LONG",
                    "level": z,
                    "reclaim": True,
                    "rejection": rejection(
                        last,
                        "LONG"
                    )
                }

                best_dist = dist

        elif swept_up:

            dist = abs(
                last["high"] - z["price"]
            )

            if dist < best_dist:

                best = {
                    "type": "SWEEP_RECLAIM_SHORT",
                    "level": z,
                    "reclaim": True,
                    "rejection": rejection(
                        last,
                        "SHORT"
                    )
                }

                best_dist = dist

    if best:
        return best

    return {
        "type": "NONE",
        "level": None,
        "reclaim": False,
        "rejection": False
    }


def market_state(context, h1_structure):

    if context["main"] == "BULLISH":

        if h1_structure["state"] in (
            "BEARISH",
            "CORRECTION_DOWN",
            "CORRECTION_UP"
        ):
            return "CORRECTION"

        return "TREND"

    if context["main"] == "BEARISH":

        if h1_structure["state"] in (
            "BULLISH",
            "CORRECTION_DOWN",
            "CORRECTION_UP"
        ):
            return "CORRECTION"

        return "TREND"

    if h1_structure["state"] in (
        "BULLISH",
        "BEARISH"
    ):
        return "REVERSAL"

    return "CORRECTION"


def confirmation(direction, m15, m5):

    c15 = closed(m15)
    c5 = closed(m5)

    if len(c15) < 3 or len(c5) < 3:
        return False, "Brak danych LTF"

    last15 = c15[-1]
    last5 = c5[-1]

    e15 = ema_status(m15)
    e5 = ema_status(m5)

    s15 = structure(m15)

    atr15 = atr(c15, 14)

    if direction == "LONG":

        ema_ok = bool(
            e15["50"]
            and last15["close"] >= e15["50"]
        )

        struct_ok = s15["state"] in (
            "BULLISH",
            "CORRECTION_DOWN",
            "CORRECTION_UP"
        )

        pa_ok = (
            last15["close"] > last15["open"]
            or rejection(last15, "LONG")
            or displacement(last15, atr15)
        )

        m5_ok = (
            last5["close"] > last5["open"]
            and (
                not e5["50"]
                or last5["close"] >= e5["50"]
            )
        )

    else:

        ema_ok = bool(
            e15["50"]
            and last15["close"] <= e15["50"]
        )

        struct_ok = s15["state"] in (
            "BEARISH",
            "CORRECTION_DOWN",
            "CORRECTION_UP"
        )

        pa_ok = (
            last15["close"] < last15["open"]
            or rejection(last15, "SHORT")
            or displacement(last15, atr15)
        )

        m5_ok = (
            last5["close"] < last5["open"]
            and (
                not e5["50"]
                or last5["close"] <= e5["50"]
            )
        )

    ok = (
        ema_ok
        and struct_ok
        and pa_ok
        and m5_ok
    )

    return (
        ok,
        f"EMA={'OK' if ema_ok else 'NO'} "
        f"STRUCT={'OK' if struct_ok else 'NO'} "
        f"PA={'OK' if pa_ok else 'NO'} "
        f"M5={'OK' if m5_ok else 'NO'}"
    )


def build_setup(d1, h4, h1, m15, m5):

    h1c = closed(h1)

    if len(h1c) < 100:

        return {
            "direction": "WAIT",
            "market_state": "WAIT",
            "score": 0,
            "reasons": ["Za mało danych"]
        }

    price = h1c[-1]["close"]

    levels = build_levels(
        d1,
        h4,
        h1,
        price
    )

    context = htf_context(
        d1,
        h4,
        h1
    )

    s1 = structure(h1)

    ema1 = ema_status(h1)

    rsi1 = rsi(
        [x["close"] for x in h1c],
        14
    )

    liq = liquidity_signal(
        h1,
        levels,
        price
    )

    state = market_state(
        context,
        s1
    )

    if liq["type"] == "SWEEP_RECLAIM_LONG":
        direction = "LONG"

    elif liq["type"] == "SWEEP_RECLAIM_SHORT":
        direction = "SHORT"

    else:

        return {
            "direction": "WAIT",
            "market_state": state,
            "score": 0,
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
            "key_level": None,
            "liquidity": liq["type"],
            "ema": ema1,
            "structure": s1["state"],
            "rsi": rsi1,
            "reasons": [
                "Brak kompletnego sweep + reclaim"
            ]
        }

    level = liq["level"]

    atr1 = atr(
        h1c,
        14
    )

    if atr1 is None:
        atr1 = max(
            price * 0.002,
            1.0
        )

    targets = target_levels(
        levels,
        price,
        direction
    )

    tp1 = (
        targets[0]["price"]
        if targets
        else None
    )

    tp2 = (
        targets[1]["price"]
        if len(targets) > 1
        else None
    )

    if direction == "LONG":

        sl = (
            min(
                h1c[-1]["low"],
                level["lower"]
            )
            - atr1 * 0.15
        )

        risk = price - sl

    else:

        sl = (
            max(
                h1c[-1]["high"],
                level["upper"]
            )
            + atr1 * 0.15
        )

        risk = sl - price

    rr = (
        abs(tp1 - price) / risk
        if tp1 is not None and risk > 0
        else None
    )

    score = 0

    reasons = [
        f"Poziom: {level['sources_text']} "
        f"strength={level['strength']}",
        f"Liquidity: {liq['type']}"
    ]

    if (
        direction == "LONG"
        and ema1["state"] in (
            "BULLISH_ALIGNMENT",
            "ABOVE_EMA50"
        )
    ):

        score += 25

    elif (
        direction == "SHORT"
        and ema1["state"] in (
            "BEARISH_ALIGNMENT",
            "BELOW_EMA50"
        )
    ):

        score += 25

    elif (
        direction == "LONG"
        and ema1["50"]
        and price > ema1["50"]
    ):

        score += 15

    elif (
        direction == "SHORT"
        and ema1["50"]
        and price < ema1["50"]
    ):

        score += 15

    reasons.append(
        f"EMA: {ema1['state']}"
    )

    score += 20 if liq["reclaim"] else 0

    if liq["rejection"]:
        score = min(100, score + 5)

    if (
        direction == "LONG"
        and s1["state"] in (
            "BULLISH",
            "CORRECTION_DOWN"
        )
    ):

        score += 15

    elif (
        direction == "SHORT"
        and s1["state"] in (
            "BEARISH",
            "CORRECTION_UP"
        )
    ):

        score += 15

    else:
        score += 5

    reasons.append(
        f"Structure: {s1['state']}"
    )

    if rsi1 is not None:

        if (
            direction == "LONG"
            and rsi1 >= 50
        ):

            score += 10

        elif (
            direction == "SHORT"
            and rsi1 <= 50
        ):

            score += 10

        else:

            score += 5

        reasons.append(
            f"RSI: {rsi1:.2f}"
        )

    if rr is not None:

        if rr >= 2:
            score += 5

        elif rr >= 1.5:
            score += 3

        elif rr >= 1:
            score += 1

        reasons.append(
            f"R:R={rr:.2f}"
        )

    conf_ok, conf_text = confirmation(
        direction,
        m15,
        m5
    )

    reasons.append(
        f"LTF confirmation: {conf_text}"
    )

    if not conf_ok:

        return {
            "direction": "WAIT",
            "market_state": state,
            "score": min(100, score),
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "key_level": level["price"],
            "liquidity": liq["type"],
            "ema": ema1,
            "structure": s1["state"],
            "rsi": rsi1,
            "reasons": reasons + [
                "Brak pełnego potwierdzenia LTF"
            ]
        }

    if rr is None or rr < MIN_RR:

        return {
            "direction": "WAIT",
            "market_state": state,
            "score": min(100, score),
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "key_level": level["price"],
            "liquidity": liq["type"],
            "ema": ema1,
            "structure": s1["state"],
            "rsi": rsi1,
            "reasons": reasons + [
                "R:R < 1:1 — setup odrzucony"
            ]
        }

    return {
        "direction": direction,
        "market_state": state,
        "score": min(100, score),
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "key_level": level["price"],
        "liquidity": liq["type"],
        "ema": ema1,
        "structure": s1["state"],
        "rsi": rsi1,
        "reasons": reasons
    }


def case_signature(setup, h1):

    candle_time = closed(h1)[-1]["time"]

    return (
        f"{setup['direction']}|"
        f"{round(setup.get('key_level') or 0, 2)}|"
        f"{candle_time}"
    )


def create_or_update_case(setup, h1):

    if setup["direction"] not in (
        "LONG",
        "SHORT"
    ):
        return None

    sig = case_signature(
        setup,
        h1
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    payload = {
        "liquidity": setup["liquidity"],
        "ema": setup["ema"],
        "structure": setup["structure"],
        "rsi": setup["rsi"],
        "reasons": setup["reasons"]
    }

    conn = db()

    row = conn.execute(
        "SELECT case_id FROM cases WHERE case_id=?",
        (sig,)
    ).fetchone()

    if row is None:

        conn.execute(
            """
            INSERT INTO cases
            (
                case_id,
                created_at,
                updated_at,
                direction,
                market_state,
                entry,
                sl,
                tp1,
                tp2,
                rr,
                score,
                key_level,
                status,
                outcome,
                payload
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sig,
                now,
                now,
                setup["direction"],
                setup["market_state"],
                setup["entry"],
                setup["sl"],
                setup["tp1"],
                setup["tp2"],
                setup["rr"],
                setup["score"],
                setup["key_level"],
                "ACTIVE_CASE",
                None,
                json.dumps(
                    payload,
                    ensure_ascii=False
                )
            )
        )

        conn.commit()
        conn.close()

        event(
            "CASE_CREATED",
            setup["direction"],
            setup["entry"],
            {
                "case_id": sig,
                **setup
            }
        )

        return sig

    conn.execute(
        """
        UPDATE cases
        SET
            updated_at=?,
            market_state=?,
            entry=?,
            sl=?,
            tp1=?,
            tp2=?,
            rr=?,
            score=?,
            payload=?
        WHERE case_id=?
        """,
        (
            now,
            setup["market_state"],
            setup["entry"],
            setup["sl"],
            setup["tp1"],
            setup["tp2"],
            setup["rr"],
            setup["score"],
            json.dumps(
                payload,
                ensure_ascii=False
            ),
            sig
        )
    )

    conn.commit()
    conn.close()

    return sig


def get_position():
    return state_get(
        "position",
        None
    )


def set_position(position):
    state_set(
        "position",
        position
    )


def maybe_enter(setup, h1):

    if setup["direction"] not in (
        "LONG",
        "SHORT"
    ):
        return

    if get_position():
        return

    if (
        setup["rr"] is None
        or setup["rr"] < MIN_RR
    ):
        return

    case_id = create_or_update_case(
        setup,
        h1
    )

    if not case_id:
        return

    price = setup["entry"]

    atr1 = atr(
        closed(h1),
        14
    )

    if atr1 is None:
        atr1 = max(
            price * 0.002,
            1.0
        )

    position = {
        "case_id": case_id,
        "direction": setup["direction"],
        "entry": price,
        "sl": setup["sl"],
        "initial_sl": setup["sl"],
        "atr": atr1,
        "opened_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "trailing_active": False
    }

    set_position(position)

    event(
        "ENTRY",
        setup["direction"],
        price,
        {
            "case_id": case_id,
            **setup
        }
    )

    log(
        f"ENTRY {setup['direction']} "
        f"{price:.2f} "
        f"SL={setup['sl']:.2f} "
        f"RR={setup['rr']:.2f}"
    )


def manage_position(price):

    pos = get_position()

    if not pos:
        return

    entry = pos["entry"]

    direction = pos["direction"]

    initial_r = abs(
        entry - pos["initial_sl"]
    )

    if initial_r <= 0:
        return

    if direction == "LONG":

        r_now = (
            price - entry
        ) / initial_r

    else:

        r_now = (
            entry - price
        ) / initial_r

    if r_now > TRAIL_ACTIVATION_R:

        if direction == "LONG":

            new_sl = (
                price
                - pos["atr"] * TRAIL_ATR_MULT
            )

            if new_sl > pos["sl"]:

                pos["sl"] = new_sl
                pos["trailing_active"] = True

                set_position(pos)

                event(
                    "TRAIL_UPDATE",
                    direction,
                    price,
                    {
                        "new_sl": new_sl,
                        "R": r_now
                    }
                )

        else:

            new_sl = (
                price
                + pos["atr"] * TRAIL_ATR_MULT
            )

            if new_sl < pos["sl"]:

                pos["sl"] = new_sl
                pos["trailing_active"] = True

                set_position(pos)

                event(
                    "TRAIL_UPDATE",
                    direction,
                    price,
                    {
                        "new_sl": new_sl,
                        "R": r_now
                    }
                )

    if direction == "LONG":
        hit = price <= pos["sl"]
    else:
        hit = price >= pos["sl"]

    if hit:

        event(
            "EXIT_SL",
            direction,
            price,
            {
                "R": r_now,
                "trailing": pos.get(
                    "trailing_active",
                    False
                )
            }
        )

        conn = db()

        conn.execute(
            """
            UPDATE cases
            SET
                status=?,
                outcome=?,
                updated_at=?
            WHERE case_id=?
            """,
            (
                "CLOSED",
                "TRAIL/SL",
                datetime.now(
                    timezone.utc
                ).isoformat(),
                pos["case_id"]
            )
        )

        conn.commit()
        conn.close()

        set_position(None)


def make_dashboard():

    conn = db()

    events = conn.execute(
        """
        SELECT *
        FROM events
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()

    cases = conn.execute(
        """
        SELECT *
        FROM cases
        ORDER BY updated_at DESC
        LIMIT 30
        """
    ).fetchall()

    conn.close()

    position = get_position()

    price = state_get(
        "last_price"
    )

    last_data = state_get(
        "last_data"
    )

    setup = state_get(
        "latest_setup"
    ) or {}

    ema = setup.get(
        "ema"
    ) or {}

    def esc(v):

        if v is None:
            return "-"

        return html.escape(
            str(v)
        )

    event_rows = "".join(
        "<tr>"
        "<td>" + esc(e["ts"]) + "</td>"
        "<td>" + esc(e["event_type"]) + "</td>"
        "<td>" + esc(e["direction"]) + "</td>"
        "<td>" +
        esc(
            f"{e['price']:.2f}"
            if e["price"] is not None
            else "-"
        ) +
        "</td>"
        "</tr>"
        for e in events
    )

    case_rows = "".join(
        "<tr>"
        "<td>" +
        esc(c["case_id"][:34]) +
        "</td>"
        "<td>" +
        esc(c["direction"]) +
        "</td>"
        "<td>" +
        esc(c["market_state"]) +
        "</td>"
        "<td>" +
        esc(c["status"]) +
        "</td>"
        "<td>" +
        esc(c["score"]) +
        "</td>"
        "<td>" +
        esc(
            f"{c['rr']:.2f}"
            if c["rr"] is not None
            else "-"
        ) +
        "</td>"
        "</tr>"
        for c in cases
    )

    pos_text = (
        "BRAK AKTYWNEJ POZYCJI"
    )

    if position:

        pos_text = (
            f"{position['direction']} | "
            f"ENTRY {position['entry']:.2f} | "
            f"SL {position['sl']:.2f} | "
            f"TRAILING "
            f"{'ON' if position.get('trailing_active') else 'OFF'}"
        )

    reasons = "".join(
        "<li>" +
        esc(x) +
        "</li>"
        for x in setup.get(
            "reasons",
            []
        )
    )

    return (
        "<!doctype html>"
        "<html>"
        "<head>"
        "<meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='5'>"
        "<meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        "<title>NAS100 MARKET ENGINE</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;"
        "background:#0b0d10;color:#eee;"
        "margin:0;padding:18px}"
        ".card{background:#151922;"
        "border:1px solid #2a3040;"
        "border-radius:12px;"
        "padding:16px;"
        "margin-bottom:14px}"
        ".big{font-size:28px;font-weight:700}"
        ".muted{color:#9aa3b2}"
        "table{width:100%;"
        "border-collapse:collapse;"
        "font-size:12px}"
        "th,td{padding:7px;"
        "border-bottom:1px solid #2a3040;"
        "text-align:left}"
        "</style>"
        "</head>"
        "<body>"

        "<div class='card'>"
        "<div class='big'>"
        "NAS100 MARKET ENGINE"
        "</div>"
        "<div class='muted'>"
        "MEXC Futures — NAS100_USDT"
        "</div>"
        "<div class='muted'>"
        "PAPER / MARKET OBSERVATION ONLY "
        "— NO REAL ORDERS"
        "</div>"
        "</div>"

        "<div class='card'>"
        "<div class='big'>" +
        esc(
            f"{price:.2f}"
            if isinstance(
                price,
                (int, float)
            )
            else "-"
        ) +
        "</div>"
        "<div class='muted'>"
        "Ostatnie dane: " +
        esc(last_data) +
        "</div>"
        "</div>"

        "<div class='card'>"
        "<h3>AKTUALNY SETUP</h3>"

        "<div>Direction: <b>" +
        esc(
            setup.get("direction")
        ) +
        "</b></div>"

        "<div>Market State: <b>" +
        esc(
            setup.get("market_state")
        ) +
        "</b></div>"

        "<div>Score: <b>" +
        esc(
            setup.get("score")
        ) +
        "/100</b></div>"

        "<div>Entry: " +
        esc(
            f"{setup['entry']:.2f}"
            if setup.get("entry") is not None
            else "-"
        ) +
        "</div>"

        "<div>SL: " +
        esc(
            f"{setup['sl']:.2f}"
            if setup.get("sl") is not None
            else "-"
        ) +
        "</div>"

        "<div>TP1 analityczny: " +
        esc(
            f"{setup['tp1']:.2f}"
            if setup.get("tp1") is not None
            else "-"
        ) +
        "</div>"

        "<div>TP2 analityczny: " +
        esc(
            f"{setup['tp2']:.2f}"
            if setup.get("tp2") is not None
            else "-"
        ) +
        "</div>"

        "<div>R:R: " +
        esc(
            f"{setup['rr']:.2f}"
            if setup.get("rr") is not None
            else "-"
        ) +
        "</div>"

        "<div>Key Level: " +
        esc(
            f"{setup['key_level']:.2f}"
            if setup.get("key_level") is not None
            else "-"
        ) +
        "</div>"

        "<div>Liquidity: " +
        esc(
            setup.get("liquidity")
        ) +
        "</div>"

        "<div>EMA: " +
        esc(
            ema.get("state")
        ) +
        "</div>"

        "<div>Structure: " +
        esc(
            setup.get("structure")
        ) +
        "</div>"

        "<div>RSI: " +
        esc(
            f"{setup['rsi']:.2f}"
            if setup.get("rsi") is not None
            else "-"
        ) +
        "</div>"

        "<h4>REASONS</h4>"
        "<ul>" +
        reasons +
        "</ul>"

        "</div>"

        "<div class='card'>"
        "<h3>POZYCJA</h3>"
        "<div>" +
        esc(pos_text) +
        "</div>"
        "</div>"

        "<div class='card'>"
        "<h3>CASE HISTORY</h3>"
        "<table>"
        "<tr>"
        "<th>CASE</th>"
        "<th>DIR</th>"
        "<th>STATE</th>"
        "<th>STATUS</th>"
        "<th>SCORE</th>"
        "<th>R:R</th>"
        "</tr>" +
        case_rows +
        "</table>"
        "</div>"

        "<div class='card'>"
        "<h3>EVENT HISTORY</h3>"
        "<table>"
        "<tr>"
        "<th>UTC</th>"
        "<th>EVENT</th>"
        "<th>DIR</th>"
        "<th>PRICE</th>"
        "</tr>" +
        event_rows +
        "</table>"
        "</div>"

        "</body>"
        "</html>"
    )


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/health":

            body = (
                b"NAS100 MARKET ENGINE OK\n"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

            return

        body = make_dashboard().encode(
            "utf-8"
        )

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    log(
        f"Dashboard listening on port {PORT}"
    )

    server.serve_forever()


def engine_loop():

    init_db()

    log(
        f"Starting NAS100 engine "
        f"for {SYMBOL}"
    )

    event(
        "ENGINE_START",
        payload={
            "symbol": SYMBOL
        }
    )

    while True:

        try:

            d1 = get_klines(
                "D1",
                D1_LIMIT
            )

            h4 = get_klines(
                "H4",
                H4_LIMIT
            )

            h1 = get_klines(
                "H1",
                H1_LIMIT
            )

            m15 = get_klines(
                "M15",
                M15_LIMIT
            )

            m5 = get_klines(
                "M5",
                M5_LIMIT
            )

            setup = build_setup(
                d1,
                h4,
                h1,
                m15,
                m5
            )

            price = closed(
                h1
            )[-1]["close"]

            state_set(
                "last_price",
                price
            )

            state_set(
                "last_data",
                datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            )

            state_set(
                "latest_setup",
                setup
            )

            if get_position():

                manage_position(
                    price
                )

            else:

                maybe_enter(
                    setup,
                    h1
                )

            log(
                f"PRICE={price:.2f} "
                f"DIR={setup['direction']} "
                f"STATE={setup['market_state']} "
                f"SCORE={setup['score']} "
                f"RR={setup.get('rr')}"
            )

        except Exception as exc:

            log(
                f"ERROR: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            event(
                "ERROR",
                payload={
                    "type": type(exc).__name__,
                    "message": str(exc)
                }
            )

        time.sleep(
            POLL_SECONDS
        )


if __name__ == "__main__":

    init_db()

    threading.Thread(
        target=start_server,
        daemon=True
    ).start()

    engine_loop()
