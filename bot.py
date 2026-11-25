#!/usr/bin/env python3
"""Kalshi 15-minute dry-run bot.

Watches Kalshi's 15-minute BTC and gold markets, applies your rules, and
records the bets it WOULD make with a pretend bankroll. It never places real
orders and needs no API key. Results go to docs/data.json for the dashboard.

Run:  python bot.py --minutes 9      (loops for 9 minutes, checking every 15 s)
"""
import argparse, json, math, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
DATA = os.path.join(ROOT, "docs", "data.json")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com/products/{}/candles?granularity=60"
# Price feed used to track each market's moves. Only the change since the
# window opened is used, so a small gap vs Kalshi's index doesn't matter.
FEED = {"KXBTC15M": "BTC-USD", "KXGOLD15M": "PAXG-USD"}
NAME = {"KXBTC15M": "BTC", "KXGOLD15M": "Gold"}
MAX_DECISIONS = 400


# ---------- helpers ----------
def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-dryrun/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def dollars(m, key):
    """Kalshi gives prices as dollar strings ('0.9300'); older fields are cents."""
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(key)
    if v not in (None, ""):
        return float(v) / 100.0
    return None

def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def kalshi_fee(contracts, price):
    """Kalshi taker fee: 7% x C x P x (1-P), rounded up to the cent."""
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100.0

def notify(cfg, title, msg):
    topic = (cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode(), headers={"Title": title})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ---------- state ----------
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def fresh_state(cfg):
    return {"started": iso(now()), "start_bankroll": cfg["start_bankroll"], "bankroll": cfg["start_bankroll"],
            "trades": [], "decisions": [], "skips": {}, "windows_seen": [], "pause_until": None,
            "status": "running", "last_run": None, "errors": []}

def log_decision(st, d):
    st["decisions"].append(d)
    del st["decisions"][:-MAX_DECISIONS]

def save(st, cfg):
    st["config"] = {k: v for k, v in cfg.items() if k != "notes"}
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, DATA)


# ---------- market data ----------
def open_market(series):
    """The open market in this series closing soonest."""
    d = http_json(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=20")
    ms = [m for m in d.get("markets", []) if m.get("close_time")]
    if not ms:
        return None
    return min(ms, key=lambda m: parse_ts(m["close_time"]))

def candles(product):
    """1-minute candles, oldest first: list of (unix_time, close)."""
    rows = http_json(COINBASE.format(product))
    out = sorted((int(r[0]), float(r[4])) for r in rows if len(r) >= 5)
    return out

def model(series, market, t):
    """Real chance YES wins, plus safety and jump measures."""
    strike = market.get("floor_strike")
    if strike in (None, ""):
        return None, "no target price on market"
    strike = float(strike)
    close = parse_ts(market["close_time"])
    opened = close - timedelta(minutes=15)
    cs = candles(FEED[series])
    if len(cs) < 30:
        return None, "not enough price history"
    closes = [c for _, c in cs]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 60), len(closes))]
    mu = sum(rets) / len(rets)
    sig1 = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1))
    if sig1 <= 0:
        return None, "price feed flat"
    # feed price around the window open (minute before open, like Kalshi's target)
    open_ts = int(opened.timestamp())
    before = [c for ts, c in cs if open_ts - 60 <= ts < open_ts] or [c for ts, c in cs if ts <= open_ts][-1:]
    if not before:
        return None, "no price at window open"
    feed_open = sum(before) / len(before)
    feed_now = closes[-1]
    est_now = strike * feed_now / feed_open
    mins_left = max((close - t).total_seconds() / 60.0, 0.05)
    spread = sig1 * math.sqrt(mins_left)
    z = math.log(est_now / strike) / spread
    p_yes = phi(z)
    last_move = abs(rets[-1]) / sig1 if rets else 0
    return {"strike": strike, "est_now": est_now, "sigma_1m": sig1, "z": z, "p_yes": p_yes,
            "jump": last_move, "mins_left": mins_left}, None


# ---------- rules ----------
def today_pl(st, t):
    day = t.date().isoformat()
    return sum(x["pl"] for x in st["trades"] if x.get("settled") and x["settled"][:10] == day)

def in_blackout(cfg, t):
    for b in cfg.get("news_blackouts_utc", []):
        try:
            s = datetime.fromisoformat(b["start"]).replace(tzinfo=timezone.utc)
            e = datetime.fromisoformat(b["end"]).replace(tzinfo=timezone.utc)
            if s <= t <= e:
                return b.get("why", "news")
        except Exception:
            continue
    return None

def account_blocks(st, cfg, t):
    """Rules that stop all betting right now."""
    if not cfg.get("enabled", True):
        return "kill switch is on"
    if today_pl(st, t) <= -cfg["daily_loss_limit"]:
        return "daily loss limit hit"
    if st.get("pause_until") and t < parse_ts(st["pause_until"]):
        return "paused after losing streak"
    b = in_blackout(cfg, t)
    if b:
        return "news blackout: " + b
    return None

def evaluate(st, cfg, series, t):
    m = open_market(series)
    if not m:
        return {"series": series, "action": "skip", "reason": "no open market"}
    ticker = m["ticker"]
    close = parse_ts(m["close_time"])
    mins_left = (close - t).total_seconds() / 60.0
    base = {"t": iso(t), "series": series, "ticker": ticker, "mins_left": round(mins_left, 2)}
    if ticker not in st["windows_seen"]:
        st["windows_seen"].append(ticker)
        del st["windows_seen"][:-3000]
    if mins_left > cfg["max_minutes_left"] or mins_left <= 0.1:
        return None  # not in the betting zone yet; not worth logging
    if any(x["ticker"] == ticker for x in st["trades"]):
        return None  # rule: one bet per window
    # pick the favored side
    ya, na = dollars(m, "yes_ask"), dollars(m, "no_ask")
    side, ask = None, None
    if ya and ya >= cfg["min_price"]:
        side, ask = "yes", ya
    elif na and na >= cfg["min_price"]:
        side, ask = "no", na
    d = dict(base, yes_ask=ya, no_ask=na)
    block = account_blocks(st, cfg, t)
    if block:
        return dict(d, action="skip", reason=block)
    if not side:
        return dict(d, action="skip", reason=f"no side at {int(cfg['min_price']*100)}c+")
    d.update(side=side, ask=ask)
    if ask > cfg["max_price"]:
        return dict(d, action="skip", reason=f"price above limit {int(cfg['max_price']*100)}c")
    mod, err = model(series, m, t)
    if err:
        return dict(d, action="skip", reason=err)
    p = mod["p_yes"] if side == "yes" else 1 - mod["p_yes"]
    stake = min(cfg["max_bet"], st["bankroll"])
    contracts = int(stake // ask)
    if contracts < 1:
        return dict(d, action="skip", reason="bankroll too low")
    fee = kalshi_fee(contracts, ask)
    cost_per = ask + fee / contracts
    edge = p - cost_per
    safety = abs(mod["z"])
    favored_ok = (mod["z"] > 0) == (side == "yes")
    d.update(p=round(p, 4), edge=round(edge, 4), safety=round(safety, 2), jump=round(mod["jump"], 2),
             strike=mod["strike"], est=round(mod["est_now"], 2))
    if not favored_ok:
        return dict(d, action="skip", reason="market and model disagree on side")
    if mod["jump"] > cfg["max_jump_sigmas"]:
        return dict(d, action="skip", reason="price too jumpy")
    if safety < cfg["min_safety_sigmas"]:
        return dict(d, action="skip", reason="too close to target")
    if edge < cfg["min_edge"]:
        return dict(d, action="skip", reason="real chance below price")
    cost = round(contracts * ask + fee, 2)
    trade = {"id": f"{ticker}-{side}", "ticker": ticker, "series": series, "side": side, "price": ask,
             "contracts": contracts, "fee": fee, "cost": cost, "p": round(p, 4), "edge": round(edge, 4),
             "safety": round(safety, 2), "strike": mod["strike"], "opened": iso(t), "close_time": m["close_time"],
             "mins_left": round(mins_left, 2), "settled": None, "result": None, "pl": 0.0}
    st["trades"].append(trade)
    st["bankroll"] = round(st["bankroll"] - cost, 2)
    notify(cfg, "Dry-run bet", f"{NAME[series]} {side.upper()} {contracts} @ {int(ask*100)}c, chance {p*100:.1f}%")
    return dict(d, action="bet", reason=f"{contracts} contracts, cost ${cost:.2f}")

def settle(st, cfg, t):
    for x in st["trades"]:
        if x.get("settled") or t < parse_ts(x["close_time"]) + timedelta(seconds=30):
            continue
        try:
            m = http_json(f"{KALSHI}/markets/{x['ticker']}").get("market", {})
        except Exception:
            continue
        res = m.get("result")
        if res not in ("yes", "no"):
            continue
        won = res == x["side"]
        payout = x["contracts"] * 1.0 if won else 0.0
        x["result"] = "win" if won else "loss"
        x["pl"] = round(payout - x["cost"], 2)
        x["settled"] = iso(t)
        st["bankroll"] = round(st["bankroll"] + payout, 2)
        if not won:
            notify(cfg, "Dry-run loss", f"{NAME[x['series']]} lost {x['pl']:.2f}. Bankroll ${st['bankroll']:.2f}")
            done = [y for y in st["trades"] if y.get("settled")]
            streak = 0
            for y in reversed(sorted(done, key=lambda y: y["settled"])):
                if y["result"] == "loss":
                    streak += 1
                else:
                    break
            if streak >= cfg["losing_streak_pause"]:
                st["pause_until"] = iso(t + timedelta(minutes=cfg["pause_minutes"]))
                notify(cfg, "Bot paused", f"{streak} losses in a row. Paused {cfg['pause_minutes']} min.")


def step(st, cfg):
    t = now()
    settle(st, cfg, t)
    for series in cfg["series"]:
        try:
            d = evaluate(st, cfg, series, t)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
            st["errors"].append({"t": iso(t), "series": series, "error": str(e)[:200]})
            del st["errors"][:-50]
            continue
        if d:
            # one row per window: a later check in the same window replaces the earlier skip
            if st["decisions"] and st["decisions"][-1].get("ticker") == d.get("ticker") and st["decisions"][-1]["action"] == "skip":
                st["decisions"][-1] = d
            else:
                prev = [i for i, x in enumerate(st["decisions"][-6:]) if x.get("ticker") == d.get("ticker") and x["action"] == "skip"]
                if prev:
                    st["decisions"].pop(len(st["decisions"]) - 6 + prev[-1] if len(st["decisions"]) >= 6 else prev[-1])
                log_decision(st, d)
            if d.get("ticker"):
                st["skips"][d["ticker"]] = "bet" if d["action"] == "bet" else d["reason"].split(":")[0]
                if len(st["skips"]) > 3000:
                    for k in list(st["skips"])[:500]:
                        del st["skips"][k]
    blocked = account_blocks(st, cfg, t)
    st["status"] = "stopped: " + blocked if blocked else "running"
    st["last_run"] = iso(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=9)
    ap.add_argument("--every", type=float, default=15, help="seconds between checks")
    a = ap.parse_args()
    cfg = load(CONFIG, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    if not cfg.get("dry_run", True):
        sys.exit("This bot is dry-run only. It never places real orders.")
    st = load(DATA, None) or fresh_state(cfg)
    end = time.time() + a.minutes * 60
    while True:
        step(st, cfg)
        save(st, cfg)
        if time.time() + a.every > end:
            break
        time.sleep(a.every)
        cfg = load(CONFIG, cfg)  # pick up kill switch changes


if __name__ == "__main__":
    main()
