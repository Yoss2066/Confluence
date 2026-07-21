"""
Confluence Scanner — runs on GitHub Actions, completely independent of your
phone or browser. Checks all 7 pairs on a schedule and sends a push
notification via ntfy.sh the moment a clean setup appears.
"""
import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone

PAIRS = ["XAU/USD","GBP/USD","EUR/USD","USD/JPY","AUD/USD","GBP/JPY","NZD/USD"]
INTERVAL = "1h"
MIN_CONFLUENCE = 3
STATE_FILE = "state.json"

API_KEYS = [k for k in [os.environ.get("TWELVEDATA_KEY_1"), os.environ.get("TWELVEDATA_KEY_2")] if k]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

def is_active_hours():
    now = datetime.now(timezone.utc)
    eat_hour = (now.hour + 3) % 24  # Ethiopia is fixed UTC+3, no DST
    return 7 <= eat_hour < 22

def next_key(i):
    if not API_KEYS: return None
    return API_KEYS[i % len(API_KEYS)]

def fetch_candles(pair, key, interval=None):
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair, "interval": interval or INTERVAL, "outputsize": 300, "apikey": key
    })
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode())
    if "values" not in data:
        raise RuntimeError(data.get("message", "no data returned"))
    candles = [{
        "time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
        "low": float(v["low"]), "close": float(v["close"])
    } for v in data["values"]]
    candles.reverse()
    return candles

# ---------- ICT / SMC engine ----------
def find_swings(candles):
    swings = []
    for i in range(2, len(candles) - 2):
        c = candles[i]
        is_high = c["high"] > candles[i-1]["high"] and c["high"] > candles[i-2]["high"] and c["high"] > candles[i+1]["high"] and c["high"] > candles[i+2]["high"]
        is_low  = c["low"]  < candles[i-1]["low"]  and c["low"]  < candles[i-2]["low"]  and c["low"]  < candles[i+1]["low"]  and c["low"]  < candles[i+2]["low"]
        if is_high: swings.append({"index": i, "price": c["high"], "type": "high"})
        if is_low:  swings.append({"index": i, "price": c["low"],  "type": "low"})
    return swings

def label_structure(swings):
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    events = []
    for i in range(1, len(highs)):
        events.append({**highs[i], "label": "HH" if highs[i]["price"] > highs[i-1]["price"] else "LH"})
    for i in range(1, len(lows)):
        events.append({**lows[i], "label": "HL" if lows[i]["price"] > lows[i-1]["price"] else "LL"})
    events.sort(key=lambda e: e["index"])
    return events

def detect_shift(candles, swings):
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if not highs or not lows: return None
    last_high, last_low = highs[-1], lows[-1]
    last_idx = len(candles) - 1
    last_close = candles[-1]["close"]
    return {
        "bos_up":   last_close > last_high["price"] and last_idx > last_high["index"],
        "bos_down": last_close < last_low["price"]  and last_idx > last_low["index"]
    }

def find_fvgs(candles):
    fvgs = []
    for i in range(2, len(candles)):
        a, c = candles[i-2], candles[i]
        if a["high"] < c["low"]:  fvgs.append({"type": "bullish", "top": c["low"], "bottom": a["high"], "index": i})
        if a["low"]  > c["high"]: fvgs.append({"type": "bearish", "top": a["low"], "bottom": c["high"], "index": i})
    return fvgs

def avg_range(candles, i):
    seg = candles[max(0, i-10):i]
    return (sum(c["high"] - c["low"] for c in seg) / len(seg)) if seg else 0

def find_obs(candles):
    obs = []
    for i in range(3, len(candles)):
        c = candles[i]
        rng = c["high"] - c["low"]
        body_pct = abs(c["close"] - c["open"]) / (rng or 1e-9)
        if body_pct > 0.6 and rng > avg_range(candles, i) * 1.3:
            bullish = c["close"] > c["open"]
            for j in range(i-1, max(0, i-5)-1, -1):
                p = candles[j]
                p_bull = p["close"] > p["open"]
                if bullish and not p_bull:
                    obs.append({"type": "bullish", "top": p["high"], "bottom": p["low"], "index": j}); break
                if not bullish and p_bull:
                    obs.append({"type": "bearish", "top": p["high"], "bottom": p["low"], "index": j}); break
    return obs

def find_sweep(candles, swings):
    if len(candles) < 2: return None
    prev = candles[-2]
    for h in [s for s in swings if s["type"] == "high"][-3:]:
        if prev["high"] > h["price"] and prev["close"] < h["price"]: return {"type": "sell-side-sweep", "level": h["price"]}
    for l in [s for s in swings if s["type"] == "low"][-3:]:
        if prev["low"] < l["price"] and prev["close"] > l["price"]: return {"type": "buy-side-sweep", "level": l["price"]}
    return None

def fib_zone(swings, price):
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if not highs or not lows: return None
    high, low = highs[-1]["price"], lows[-1]["price"]
    rng = high - low
    if rng <= 0: return None
    pos = (price - low) / rng
    zone = "discount" if pos < 0.382 else ("premium" if pos > 0.618 else "equilibrium")
    return {"zone": zone}

def is_mitigated(candles, zone):
    for k in range(zone["index"] + 1, len(candles)):
        c = candles[k]
        if c["low"] <= zone["top"] and c["high"] >= zone["bottom"]: return True
    return False

def generate_signal(candles, daily_bias=None):
    swings = find_swings(candles)
    structure_events = label_structure(swings)
    shift = detect_shift(candles, swings)
    fvgs = find_fvgs(candles)
    obs = find_obs(candles)
    sweep = find_sweep(candles, swings)
    price = candles[-1]["close"]
    fib = fib_zone(swings, price)

    bias = "neutral"
    confluences = []
    recent = [e["label"] for e in structure_events[-4:]]
    bull_c = sum(1 for l in recent if l in ("HH", "HL"))
    bear_c = sum(1 for l in recent if l in ("LH", "LL"))
    if bull_c > bear_c: bias = "bullish"
    if bear_c > bull_c: bias = "bearish"
    if shift and shift["bos_up"]:   bias = "bullish"; confluences.append("bullish BOS")
    if shift and shift["bos_down"]: bias = "bearish"; confluences.append("bearish BOS")

    if sweep and sweep["type"] == "buy-side-sweep"  and bias == "bullish": confluences.append("buy-side sweep")
    if sweep and sweep["type"] == "sell-side-sweep" and bias == "bearish": confluences.append("sell-side sweep")
    if fib:
        if bias == "bullish" and fib["zone"] == "discount": confluences.append("discount zone")
        if bias == "bearish" and fib["zone"] == "premium":  confluences.append("premium zone")

    unmitigated_obs  = [o for o in obs  if not is_mitigated(candles, o)]
    unmitigated_fvgs = [f for f in fvgs if not is_mitigated(candles, f)]
    relevant_obs  = [o for o in unmitigated_obs  if o["type"] == bias]
    relevant_fvgs = [f for f in unmitigated_fvgs if f["type"] == bias]

    atr_now = avg_range(candles, len(candles)-1) or price * 0.0015
    max_reach = atr_now * 3  # if even the nearest zone is farther than this, price is unlikely to reach it

    def zone_distance(z):
        mid = (z["top"] + z["bottom"]) / 2
        return abs(mid - price)

    entry_zone = None
    zone_kind = None
    # Prefer the nearest OB if one exists within reach; else nearest FVG within reach.
    nearest_ob  = sorted(relevant_obs, key=zone_distance)[0] if relevant_obs else None
    nearest_fvg = sorted(relevant_fvgs, key=zone_distance)[0] if relevant_fvgs else None
    if nearest_ob and zone_distance(nearest_ob) <= max_reach:
        entry_zone = nearest_ob; zone_kind = "OB"; confluences.append("unmitigated OB within reach")
    elif nearest_fvg and zone_distance(nearest_fvg) <= max_reach:
        entry_zone = nearest_fvg; zone_kind = "FVG"; confluences.append("unmitigated FVG within reach")

    if not entry_zone or bias == "neutral" or len(confluences) < MIN_CONFLUENCE:
        return {"valid": False}

    # Hard reject if this goes against the daily trend — this was the main source of losses.
    if daily_bias and daily_bias != "neutral" and daily_bias != bias:
        return {"valid": False, "rejected_reason": "counter to daily trend"}

    entry = entry_zone["top"] if bias == "bullish" else entry_zone["bottom"]
    atr = avg_range(candles, len(candles)-1) or price * 0.0015
    buf = atr * 0.25
    sweep_level = sweep["level"] if sweep else None
    if bias == "bullish":
        sl = min(entry_zone["bottom"], sweep_level if sweep_level is not None else entry_zone["bottom"]) - buf
    else:
        sl = max(entry_zone["top"], sweep_level if sweep_level is not None else entry_zone["top"]) + buf

    risk = abs(entry - sl) or price * 0.001
    MIN_RR = 2
    if bias == "bullish":
        opp = sorted([s for s in swings if s["type"] == "high" and s["price"] > price], key=lambda s: abs(s["price"]-price))
    else:
        opp = sorted([s for s in swings if s["type"] == "low"  and s["price"] < price], key=lambda s: abs(s["price"]-price))

    tp = None
    for s in opp:
        if abs(s["price"] - entry) / risk >= MIN_RR:
            tp = s["price"]
            confluences.append("real liquidity target clearing 1:2+")
            break
    if tp is None:
        tp = entry + risk*MIN_RR if bias == "bullish" else entry - risk*MIN_RR
        confluences.append("no real target cleared 1:2 — extended synthetically, manage manually")

    rr = abs(tp - entry) / (risk or 1)
    return {"valid": True, "bias": bias, "entry": entry, "sl": sl, "tp": tp, "rr": rr, "confluence": len(confluences)}

# ---------- Notification via ntfy.sh (no account needed) ----------
def notify(title, message):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set — would have sent:", title, message, flush=True)
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url, data=message.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": "high"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("ntfy send failed:", e, flush=True)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def compute_bias(candles):
    if not candles or len(candles) < 15:
        return None
    swings = find_swings(candles)
    events = label_structure(swings)
    recent = [e["label"] for e in events[-4:]]
    bull = sum(1 for l in recent if l in ("HH", "HL"))
    bear = sum(1 for l in recent if l in ("LH", "LL"))
    if bull > bear: return "bullish"
    if bear > bull: return "bearish"
    return "neutral"

DAILY_CACHE_HOURS = 4

def get_daily_bias(pair, key, state):
    cache = state.setdefault("daily_bias_cache", {})
    entry = cache.get(pair)
    now = datetime.now(timezone.utc).timestamp()
    if entry and (now - entry.get("ts", 0)) < DAILY_CACHE_HOURS * 3600:
        return entry.get("bias")
    try:
        daily_candles = fetch_candles(pair, key, interval="1day")
        bias = compute_bias(daily_candles)
        cache[pair] = {"bias": bias, "ts": now}
        return bias
    except Exception as e:
        print(f"{pair}: could not fetch daily bias ({e}) — skipping trend filter this run", flush=True)
        return entry.get("bias") if entry else None

def main():
    print(f"Scanner started at {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Keys configured: {len(API_KEYS)}", flush=True)

    if not is_active_hours():
        print("Outside 7AM-10PM Addis Ababa hours — skipping this run.", flush=True)
        return
    if not API_KEYS:
        print("No API keys configured (set TWELVEDATA_KEY_1 as a repo secret).", flush=True)
        return

    state = load_state()
    for i, pair in enumerate(PAIRS):
        key = next_key(i)
        try:
            candles = fetch_candles(pair, key)
            if len(candles) < 30:
                print(f"{pair}: not enough history returned", flush=True)
                continue
            daily_bias = get_daily_bias(pair, key, state)
            sig = generate_signal(candles, daily_bias=daily_bias)
            if sig["valid"]:
                signature = f'{sig["bias"]}-{round(sig["entry"], 6)}'
                if state.get(pair) != signature:
                    state[pair] = signature
                    direction = "LONG" if sig["bias"] == "bullish" else "SHORT"
                    msg = f'{direction} · entry {sig["entry"]:.5f} · SL {sig["sl"]:.5f} · TP {sig["tp"]:.5f} · 1:{sig["rr"]:.1f} · {sig["confluence"]}/6'
                    notify(f"Clean setup: {pair}", msg)
                    print(datetime.now(timezone.utc).isoformat(), pair, msg, flush=True)
                else:
                    print(f"{pair}: valid setup, already alerted (no change)", flush=True)
            else:
                state[pair] = None
                print(f"{pair}: no clean setup right now (WAIT)", flush=True)
        except Exception as e:
            print(datetime.now(timezone.utc).isoformat(), "error scanning", pair, "-", e, flush=True)
    save_state(state)
    print("Scanner finished.", flush=True)

if __name__ == "__main__":
    main()
