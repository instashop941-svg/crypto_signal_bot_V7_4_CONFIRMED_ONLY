import os
import time
import logging
import requests
import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

SYMBOLS = os.getenv(
    "SYMBOLS",
    "BTC/USDT:USDT ETH/USDT:USDT NEAR/USDT:USDT PYTH/USDT:USDT ADA/USDT:USDT "
    "ENA/USDT:USDT FET/USDT:USDT VIRTUAL/USDT:USDT TAO/USDT:USDT PENDLE/USDT:USDT "
    "JUP/USDT:USDT INJ/USDT:USDT AAVE/USDT:USDT LINK/USDT:USDT",
).split()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LEV = int(os.getenv("LEVERAGE", "30"))
FAST_SECONDS = int(os.getenv("FAST_SECONDS", "300"))
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.7"))
MAX_CHASE_PCT = float(os.getenv("MAX_CHASE_PCT", "0.45"))
COOLDOWN = int(os.getenv("COOLDOWN", "1800"))
PAUSE = float(os.getenv("PAUSE", "1.5"))

exchange = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
last_alert = {}
last_state = {}


def tg(msg):
    if not TOKEN or not CHAT_ID:
        logging.warning("Telegram variables are missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
        if r.status_code != 200:
            logging.warning("telegram status=%s body=%s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logging.warning("telegram error: %s", e)
        return False


def get(symbol, timeframe="5m", limit=180):
    try:
        x = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        d = pd.DataFrame(x, columns=["ts", "open", "high", "low", "close", "volume"])
        # Never use the still-forming candle.
        return d.iloc[:-1].reset_index(drop=True) if len(d) > 3 else pd.DataFrame()
    except Exception as e:
        logging.warning("%s %s fetch error: %s", symbol, timeframe, e)
        return pd.DataFrame()


def atr(d, n=14):
    tr = pd.concat(
        [d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


def add(d):
    d = d.copy()
    d["ema14"] = d.close.ewm(span=14, adjust=False).mean()
    d["ema30"] = d.close.ewm(span=30, adjust=False).mean()
    d["atr"] = atr(d)
    return d


def trend(d):
    if len(d) < 35:
        return None
    if d.ema14.iloc[-1] > d.ema30.iloc[-1]:
        return "LONG"
    if d.ema14.iloc[-1] < d.ema30.iloc[-1]:
        return "SHORT"
    return None


def candle_side(r):
    if r.close > r.open:
        return "LONG"
    if r.close < r.open:
        return "SHORT"
    return None


def candle_color(r):
    if r.close > r.open:
        return "GREEN"
    if r.close < r.open:
        return "RED"
    return "DOJI"


def liquidity_sweep(d, lookback=8):
    if len(d) < lookback + 3:
        return None, None, None
    c = d.iloc[-1]
    prev = d.iloc[-lookback - 1 : -1]
    lo, hi = prev.low.min(), prev.high.max()
    if c.low < lo and c.close > lo:
        return "LONG", float(c.low), float(lo)
    if c.high > hi and c.close < hi:
        return "SHORT", float(c.high), float(hi)
    return None, None, None


def displacement(d):
    if len(d) < 20:
        return None
    c = d.iloc[-1]
    a = d.atr.iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    return candle_side(c) if abs(c.close - c.open) >= 1.05 * a else None


def bos(d, side, lookback=4):
    if len(d) < lookback + 3:
        return False
    c = d.iloc[-1]
    prev = d.iloc[-lookback - 1 : -1]
    return bool(c.close > prev.high.max()) if side == "LONG" else bool(c.close < prev.low.min())


def reaction(d, side):
    if len(d) < 3:
        return False
    c, p = d.iloc[-1], d.iloc[-2]
    return bool(c.close > c.open and c.close > p.close) if side == "LONG" else bool(c.close < c.open and c.close < p.close)


def event_flags(d, side, window=12):
    start = max(30, len(d) - window)
    events = {"sweep": [], "disp": [], "bos": [], "reaction": []}
    for i in range(start, len(d)):
        sub = add(d.iloc[: i + 1].copy())
        s, _, _ = liquidity_sweep(sub)
        if s == side:
            events["sweep"].append(i)
        if displacement(sub) == side:
            events["disp"].append(i)
        if bos(sub, side):
            events["bos"].append(i)
        if reaction(sub, side):
            events["reaction"].append(i)
    return events


def sequence_ok(events, side):
    sweeps = events["sweep"]
    disps = events["disp"]
    bosses = events["bos"]
    reacts = events["reaction"]
    if not sweeps or not disps or not bosses or not reacts:
        return False
    for s in reversed(sweeps):
        dps = [x for x in disps if x > s]
        if not dps:
            continue
        d_i = dps[0]
        bps = [x for x in bosses if x > d_i]
        if not bps:
            continue
        b_i = bps[0]
        if any(abs(r - b_i) <= 2 or abs(r - d_i) <= 2 for r in reacts):
            return True
    return False


def pivot_levels(d, side, left=2, right=2, lookback=72):
    w = d.iloc[-lookback:].reset_index(drop=True)
    levels = []
    if len(w) < left + right + 3:
        return levels
    for i in range(left, len(w) - right):
        hi = float(w.high.iloc[i])
        lo = float(w.low.iloc[i])
        if side == "LONG":
            if hi >= float(w.high.iloc[i-left:i].max()) and hi >= float(w.high.iloc[i+1:i+right+1].max()):
                levels.append(hi)
        else:
            if lo <= float(w.low.iloc[i-left:i].min()) and lo <= float(w.low.iloc[i+1:i+right+1].min()):
                levels.append(lo)
    return sorted(set(levels))


def actual_room(d, side, price, min_room=0.0):
    levels = pivot_levels(d, side)
    if side == "LONG":
        candidates = [x for x in levels if x > price and ((x / price - 1) * 100) >= min_room]
        target = min(candidates) if candidates else None
        return ((target / price - 1) * 100, target) if target else (0.0, None)
    candidates = [x for x in levels if x < price and ((1 - x / price) * 100) >= min_room]
    target = max(candidates) if candidates else None
    return ((1 - target / price) * 100, target) if target else (0.0, None)


def event_anchor(d, side, events):
    idxs = sorted(set(events["sweep"] + events["disp"] + events["bos"]))
    if not idxs:
        return None
    i = idxs[-1]
    if len(d) - 1 - i > 2:
        return None
    return float(d.close.iloc[i])


def signal_for(symbol, d5, d15, d1h):
    d5, d15, d1h = add(d5), add(d15), add(d1h)
    if len(d5) < 60 or len(d15) < 35 or len(d1h) < 35:
        return None

    price = float(d5.close.iloc[-1])
    t1, t15 = trend(d1h), trend(d15)
    sw, extreme, _ = liquidity_sweep(d5)
    dp = displacement(d5)
    side = sw or dp
    if not side:
        if bos(d5, "LONG"):
            side = "LONG"
        elif bos(d5, "SHORT"):
            side = "SHORT"
    if not side:
        return None

    events = event_flags(d5, side, window=12)
    has_sweep = bool(events["sweep"])
    has_disp = bool(events["disp"])
    has_bos = bool(events["bos"])
    has_react = bool(events["reaction"])

    score = (
        (35 if has_sweep else 0)
        + (25 if has_disp else 0)
        + (25 if has_bos else 0)
        + (15 if has_react else 0)
        + (10 if t15 == side else 0)
        + (10 if t1 == side else -10 if t1 else 0)
    )
    reasons = []
    for ok, name in [
        (has_sweep, "SWEEP"),
        (has_disp, "DISPLACEMENT"),
        (has_react, "REACTION"),
        (has_bos, "CHoCH/BOS"),
        (t15 == side, "15M TREND"),
        (t1 == side, "1H TREND"),
    ]:
        if ok:
            reasons.append(name)

    room, target = actual_room(d5, side, price, min_room=MIN_MOVE_PCT)
    anchor = event_anchor(d5, side, events)
    chase = 0.0
    if anchor:
        chase = (price / anchor - 1) * 100 if side == "LONG" else (anchor / price - 1) * 100

    core = has_sweep and has_disp and has_bos and has_react
    htf = t15 == side or t1 == side
    confirmed = core and sequence_ok(events, side) and htf and room >= MIN_MOVE_PCT and chase <= MAX_CHASE_PCT

    if confirmed:
        stage = "CONFIRMED"
    elif (has_sweep and (has_react or has_bos)) or (has_disp and has_bos and has_react):
        stage = "TRIGGER"
    elif has_sweep or has_disp:
        stage = "SETUP"
    else:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "stage": stage,
        "price": price,
        "room": room,
        "target": target,
        "extreme": extreme,
        "score": score,
        "reasons": reasons,
        "chase": chase,
    }


def reversal_v2_signal(s, d5):
    """Experimental direction reversal for CONFIRMED signals only.

    Uses the actual latest CLOSED 5m candle, never the live candle. This fixes the
    earlier V2 implementation that depended on a missing last_color field.
    """
    if not s or s.get("stage") != "CONFIRMED" or d5.empty:
        return s

    last_color = candle_color(d5.iloc[-1])
    side = s["side"]

    # Preserve the original confirmed direction when the latest closed candle
    # agrees with it. Reverse only when the latest closed candle contradicts it.
    flip = (last_color == "RED" and side == "LONG") or (last_color == "GREEN" and side == "SHORT")
    if not flip:
        return s

    ns = dict(s)
    ns["side"] = "SHORT" if side == "LONG" else "LONG"
    ns["reasons"] = list(s.get("reasons", [])) + ["REVERSAL V2"]
    ns["reversed_from"] = side
    ns["last_closed_color"] = last_color
    logging.info(
        "%s REVERSAL V2 FLIP %s -> %s | last_closed_5m=%s",
        s["symbol"], side, ns["side"], last_color,
    )
    return ns


def send_signal(s):
    if s["stage"] != "CONFIRMED":
        logging.info(
            "SUPPRESS TELEGRAM %s %s stage=%s | confirmation not reached",
            s["symbol"], s["side"], s["stage"],
        )
        return

    key = f"{s['symbol']}:{s['side']}:{s['stage']}"
    now = time.time()
    if now - last_alert.get(key, 0) < COOLDOWN:
        return

    p = s["price"]
    target = s["target"]
    if target is None:
        return

    ex = s["extreme"]
    sl = ex if ex else (p * 0.995 if s["side"] == "LONG" else p * 1.005)
    title = "🟢 CONFIRMED ENTRY"
    reversed_note = ""
    if s.get("reversed_from"):
        reversed_note = f"\nDirection reversed: {s['reversed_from']} → {s['side']}\n"

    msg = (
        f"{title}\n\n{s['symbol']}\n"
        f"{'LONG 🟢' if s['side'] == 'LONG' else 'SHORT 🔴'}\n"
        f"Price: {p}\nPotential room: {s['room']:.2f}%\n"
        f"Score: {s['score']}\nSignals: {', '.join(s['reasons'])}\n"
        f"{reversed_note}\n"
        f"Entry: {p}\nSL: {sl}\nTP: {target}\nLeverage: {LEV}x\n\n"
        "V7.4 REVERSAL V2"
    )
    if tg(msg):
        last_alert[key] = now
    logging.info(
        "%s %s %s room=%.2f score=%s chase=%.2f reasons=%s",
        s["symbol"], s["side"], s["stage"], s["room"], s["score"], s["chase"], ",".join(s["reasons"]),
    )


def scan(symbol):
    d5 = get(symbol, "5m", 180)
    d15 = get(symbol, "15m", 100)
    d1h = get(symbol, "1h", 100)
    if d5.empty or d15.empty or d1h.empty:
        return
    s = signal_for(symbol, d5, d15, d1h)
    if s:
        # V2 is deliberately applied AFTER the original V7.4 confirmation gate.
        s = reversal_v2_signal(s, d5)
        state = (s["side"], s["stage"])
        if state != last_state.get(symbol):
            send_signal(s)
            last_state[symbol] = state
    else:
        last_state.pop(symbol, None)
    time.sleep(PAUSE)


def main():
    tg(
        "🤖 Crypto Signal Bot V7.4 REVERSAL V2 запущений\n\n"
        "MEXC Futures\n"
        "🟢 У групу надсилаються тільки CONFIRMED ENTRY\n\n"
        "V7.4 confirmation + experimental direction reversal.\n"
        "Остання закрита 5m свічка використовується як додатковий reversal filter.\n"
        "30x. Без авто-торгівлі."
    )
    logging.info("V7.4 REVERSAL V2 started")
    while True:
        started = time.time()
        for symbol in SYMBOLS:
            try:
                scan(symbol)
            except Exception as e:
                logging.warning("%s scan error: %s", symbol, e)
        time.sleep(max(5, FAST_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
