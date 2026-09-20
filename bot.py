import os
import time
import traceback



print("=== CRYPTO SCALP BOT V5.2 STRUCTURAL SL STARTING ===", flush=True)

try:
    import ccxt
    import requests
    print("Imports OK", flush=True)
except Exception as e:
    print("IMPORT ERROR:", repr(e), flush=True)
    raise

SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,"
    "HBAR/USDT:USDT,FET/USDT:USDT,JUP/USDT:USDT,LINK/USDT:USDT,VIRTUAL/USDT:USDT"
).split(",") if s.strip()]

SCAN_SECONDS = max(15, int(os.getenv("SCAN_SECONDS", "30")))
COOLDOWN_SECONDS = max(60, int(os.getenv("COOLDOWN_SECONDS", "900")))
MIN_ROOM = float(os.getenv("MIN_ROOM", "0.005"))
MAX_ROOM = float(os.getenv("MAX_ROOM", "0.007"))
TP1_PCT = float(os.getenv("TP1_PCT", "0.005"))
TP2_PCT = float(os.getenv("TP2_PCT", "0.007"))
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.025"))
MAX_CHASE_PCT = float(os.getenv("MAX_CHASE_PCT", "0.0025"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
HEARTBEAT_SECONDS = max(60, int(os.getenv("HEARTBEAT_SECONDS", "300")))
TG = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
last_sent = {}
last_heartbeat = 0.0

print("Symbols:", ", ".join(SYMBOLS), flush=True)
print(f"Target move: 0.50% - 0.70% | Structural SL max: {MAX_RISK_PCT*100:.2f}% | Chase max: {MAX_CHASE_PCT*100:.2f}%", flush=True)
print("Telegram configured:", bool(TG and CHAT), flush=True)
print("Chat ID configured:", CHAT if CHAT else "<empty>", flush=True)

ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})

def send(text):
    if not TG or not CHAT:
        print("[TG] NOT CONFIGURED", flush=True)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG}/sendMessage",
            json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        print(f"[TG] HTTP {r.status_code}", flush=True)
        if not r.ok:
            print("[TG] RESPONSE", r.text[:500], flush=True)
            return False
        return True
    except Exception as e:
        print("[TG ERROR]", repr(e), flush=True)
        return False

def fetch(symbol, timeframe, limit):
    try:
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        print(f"[FETCH ERROR] {symbol} {timeframe}: {e}", flush=True)
        return None

def ema(values, period):
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    x = float(values[0])
    for v in values[1:]:
        x = float(v) * k + x * (1 - k)
    return x

def context_5m(rows):
    closed = rows[:-1]
    if len(closed) < 60:
        return "NEUTRAL"
    closes = [r[4] for r in closed]
    e20 = ema(closes[-50:], 20)
    e50 = ema(closes[-60:], 50)
    return "LONG" if e20 > e50 else "SHORT" if e20 < e50 else "NEUTRAL"

def median(values):
    vals=sorted(float(x) for x in values if x is not None)
    if not vals: return 0.0
    return vals[len(vals)//2]

def fvg_after(rows, side, start_idx):
    # 3-candle imbalance/FVG. Return the newest valid zone after start_idx.
    if len(rows) < 4: return None
    for i in range(len(rows)-1, max(start_idx+1, 2), -1):
        a,b,c=rows[i-2],rows[i-1],rows[i]
        if side == "LONG" and float(a[2]) < float(c[3]):
            return (float(a[2]), float(c[3]), i)
        if side == "SHORT" and float(a[3]) > float(c[2]):
            return (float(c[2]), float(a[3]), i)
    return None

def detect(symbol):
    m5 = fetch(symbol, "5m", 150)
    m1 = fetch(symbol, "1m", 180)
    if not m5 or not m1 or len(m5) < 80 or len(m1) < 60:
        return None

    ctx = context_5m(m5)
    d = m1[:-1]  # never use unfinished 1m candle
    if len(d) < 50:
        return None

    e = d[-1]
    # Recent liquidity range. Sweep can happen in the last 8 closed candles.
    base_start = max(10, len(d)-60)
    sweep_idx = None
    side = None
    sweep_level = None
    sweep_extreme = None
    # V5.1: prefer the most recent sweep that matches the 5m context.
    # This avoids discarding a valid LONG because a later opposite-side sweep
    # is the newest event in the raw 1m window.
    preferred = ctx if ctx in ("LONG", "SHORT") else None
    candidates = []
    for i in range(len(d)-1, base_start-1, -1):
        prev = d[max(0, i-12):i]
        if len(prev) < 5: continue
        lo = min(float(r[3]) for r in prev)
        hi = max(float(r[2]) for r in prev)
        c = d[i]
        if float(c[3]) < lo and float(c[4]) > lo:
            candidates.append((i, "LONG", lo, float(c[3])))
        if float(c[2]) > hi and float(c[4]) < hi:
            candidates.append((i, "SHORT", hi, float(c[2])))
    if preferred:
        matching = [x for x in candidates if x[1] == preferred]
        chosen = matching[0] if matching else (candidates[0] if candidates else None)
    else:
        chosen = candidates[0] if candidates else None
    if chosen:
        sweep_idx, side, sweep_level, sweep_extreme = chosen
    if not side:
        print(f"[FILTER SWEEP] {symbol} no recent SSL/BSL sweep", flush=True)
        return None

    if ctx != side:
        print(f"[FILTER CONTEXT] {symbol} sweep={side} 5m={ctx}", flush=True)
        return None

    # POI = last opposite/indecision candle immediately before the displacement leg.
    # We first look for a strong directional candle after the sweep.
    bodies = [abs(float(r[4])-float(r[1])) for r in d[max(0,sweep_idx):len(d)-2]]
    med = median(bodies[-24:])
    disp_idx = None
    # V5.1: first qualifying displacement after the selected sweep.
    for i in range(sweep_idx+1, len(d)-1):
        c=d[i]
        body=abs(float(c[4])-float(c[1]))
        if side == "LONG" and float(c[4])>float(c[1]) and body>=max(med*1.35, float(c[4])*0.0006):
            disp_idx=i
            break
        elif side == "SHORT" and float(c[4])<float(c[1]) and body>=max(med*1.35, float(c[4])*0.0006):
            disp_idx=i
            break
    if disp_idx is None:
        print(f"[FILTER DISPLACEMENT] {symbol} {side}", flush=True)
        return None

    poi_idx=max(sweep_idx, disp_idx-1)
    poi=d[poi_idx]
    poi_low=float(poi[3]); poi_high=float(poi[2])
    # POI must be retested after the sweep/displacement, not entered from nowhere.
    retest_idx=None
    # First meaningful retest after the displacement; do not keep replacing it
    # with later candles because that can shift the setup away from the original POI.
    for i in range(max(poi_idx+1, disp_idx+1), len(d)-1):
        c=d[i]
        if float(c[3]) <= poi_high and float(c[2]) >= poi_low:
            retest_idx=i
            break
    if retest_idx is None or retest_idx >= len(d)-1:
        print(f"[FILTER POI] {symbol} {side} no POI retest", flush=True)
        return None

    # Reaction after POI retest.
    reaction_idx=None
    for i in range(retest_idx+1, len(d)-1):
        c=d[i]
        if side=="LONG" and float(c[4])>float(c[1]) and float(c[4])>float(d[i-1][4]):
            reaction_idx=i
            break
        if side=="SHORT" and float(c[4])<float(c[1]) and float(c[4])<float(d[i-1][4]):
            reaction_idx=i
            break
    if reaction_idx is None:
        print(f"[FILTER REACTION] {symbol} {side}", flush=True)
        return None

    # CHoCH/BOS: close through the local structure created before the reaction.
    structure = d[max(0,retest_idx-4):retest_idx]
    if len(structure)<2: return None
    local_high=max(float(r[2]) for r in structure)
    local_low=min(float(r[3]) for r in structure)
    bos_idx=None
    for i in range(reaction_idx, len(d)):
        c=d[i]
        if side=="LONG" and float(c[4])>local_high: bos_idx=i
        if side=="SHORT" and float(c[4])<local_low: bos_idx=i
    if bos_idx is None:
        print(f"[FILTER BOS] {symbol} {side}", flush=True)
        return None

    # IMB/FVG after the structure shift.
    fvg=fvg_after(d, side, bos_idx)
    if not fvg:
        print(f"[FILTER IMB] {symbol} {side}", flush=True)
        return None
    fvg_low,fvg_high,fvg_idx=fvg

    entry=float(e[4])
    # Price must remain close to the event/POI area; no chasing an already-run move.
    anchor=float(d[bos_idx][4])
    chase=(entry-anchor)/anchor if side=="LONG" else (anchor-entry)/anchor
    if chase > MAX_CHASE_PCT:
        print(f"[ANTI-CHASE] {symbol} {side} chase={chase*100:.2f}%", flush=True)
        return None

    # Entry should be near the fresh imbalance or POI, not far beyond it.
    zone_mid=(fvg_low+fvg_high)/2
    zone_dist=abs(entry-zone_mid)/zone_mid
    if zone_dist > 0.0035:
        print(f"[FILTER IMB DIST] {symbol} {side} dist={zone_dist*100:.2f}%", flush=True)
        return None

    # Scalp targets are deliberately small: the goal is the first 0.50–0.70% move.
    tp1=entry*(1+TP1_PCT) if side=="LONG" else entry*(1-TP1_PCT)
    tp2=entry*(1+TP2_PCT) if side=="LONG" else entry*(1-TP2_PCT)

    # Structural SL: beyond the sweep extreme and POI invalidation, with a small buffer.
    if side=="LONG":
        invalid=min(sweep_extreme, poi_low, fvg_low)
        sl=invalid*0.9985
        room=TP2_PCT
        risk=(entry-sl)/entry
    else:
        invalid=max(sweep_extreme, poi_high, fvg_high)
        sl=invalid*1.0015
        room=TP2_PCT
        risk=(sl-entry)/entry

    if risk > MAX_RISK_PCT:
        print(f"[FILTER RISK] {symbol} {side} structural_risk={risk*100:.2f}%", flush=True)
        return None

    key=(symbol,side,round(entry,8))
    now=time.time()
    if now-last_sent.get(key,0)<COOLDOWN_SECONDS:
        return None
    last_sent[key]=now

    icon="🟢" if side=="LONG" else "🔴"
    msg=(
        f"{icon} <b>CONFIRMED SCALP V5.2</b>\n\n"
        f"<b>{symbol}</b>\n\n<b>{side}</b>\n\n"
        f"Price: {entry:.8g}\n5m Context: {ctx}\n"
        f"Trigger: SSL/BSL SWEEP + POI + REACTION + DISPLACEMENT + CHoCH/BOS + IMB\n"
        f"Potential move: 0.50–0.70%\n\n"
        f"POI: {poi_low:.8g} – {poi_high:.8g}\n"
        f"IMB: {fvg_low:.8g} – {fvg_high:.8g}\n\n"
        f"Entry: {entry:.8g}\nSL: {sl:.8g}\nTP1: {tp1:.8g}\nTP2: {tp2:.8g}\n\n"
        f"Leverage: {LEVERAGE}x\n"
        f"TP1 potential ROI: +{TP1_PCT*LEVERAGE*100:.1f}% (before fees/funding)\n"
        f"TP2 potential ROI: +{TP2_PCT*LEVERAGE*100:.1f}% (before fees/funding)\n"
        f"SL potential ROI: -{risk*LEVERAGE*100:.1f}% (before fees/funding)\n\n"
        f"<b>SCALP V5.2 — STRUCTURAL SL</b>\n\n<b>Трейдер Василь Павлів</b>\n"
        f"https://t.me/vasylpavliv"
    )
    print(f"[SIGNAL] {symbol} {side} entry={entry:.8g} sl={sl:.8g} tp1={tp1:.8g} tp2={tp2:.8g} risk={risk*100:.2f}%", flush=True)
    sent=send(msg)
    if not sent:
        print(f"[SIGNAL WARNING] {symbol} {side} signal generated but Telegram send failed", flush=True)
    return sent

def heartbeat():
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        print(f"[HEARTBEAT] Scalp V5.2 alive | symbols={len(SYMBOLS)} | scan={SCAN_SECONDS}s", flush=True)

print("Connecting to MEXC...", flush=True)
try:
    ex.load_markets()
    print(f"MEXC connected. Markets loaded: {len(ex.markets)}", flush=True)
except Exception as e:
    print("[MEXC INIT ERROR]", repr(e), flush=True)
    traceback.print_exc()
    raise

print("=== SCALP BOT V5.2 STRUCTURAL SL RUNNING ===", flush=True)

while True:
    cycle_start = time.time()
    for symbol in SYMBOLS:
        try:
            detect(symbol)
        except Exception as e:
            print(f"[DETECT ERROR] {symbol}: {e}", flush=True)
            traceback.print_exc()
            
    heartbeat()
    elapsed = time.time() - cycle_start
    sleep_for = max(1, SCAN_SECONDS - elapsed)
    print(f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep_for:.1f}s", flush=True)
    time.sleep(sleep_for)
