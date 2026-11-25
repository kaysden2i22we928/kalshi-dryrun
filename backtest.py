#!/usr/bin/env python3
"""Backtest: would the rules have made money over the last N days?

Replays every settled Kalshi 15-minute BTC and gold market from the last N days.
Each minute in the last max_minutes_left minutes before each close it reads Kalshi's real ask prices
(1-minute candles) and the real BTC / PAXG price, applies the same rules as
bot.py, and settles with Kalshi's official result. Pretend $150, no real orders.

Run:  python backtest.py --days 7      -> writes docs/backtest.json
"""
import argparse, json, math, os, time
from datetime import datetime, timezone, timedelta
import bot

OUT = os.path.join(bot.ROOT, "docs", "backtest.json")


def get(url):
    for i in range(4):
        try:
            return bot.http_json(url, timeout=20)
        except Exception as e:
            if i == 3:
                raise
            time.sleep(1.5 * (i + 1))


def settled_markets(series, since):
    out, cursor = [], ""
    while True:
        url = f"{bot.KALSHI}/markets?series_ticker={series}&status=settled&min_close_ts={int(since.timestamp())}&limit=1000"
        if cursor:
            url += "&cursor=" + cursor
        d = get(url)
        out += d.get("markets", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            return out


def kalshi_minutes(series, ticker, start, end):
    """{minute_end_ts: (yes_ask, yes_bid)} in dollars, from 1-minute candles."""
    url = (f"{bot.KALSHI}/series/{series}/markets/{ticker}/candlesticks"
           f"?start_ts={int(start.timestamp())}&end_ts={int(end.timestamp())}&period_interval=1")
    d = get(url)
    out = {}

    def val(side):
        if not isinstance(side, dict):
            return None
        for k in ("close_dollars", "close"):
            v = side.get(k)
            if v not in (None, ""):
                v = float(v)
                return v / 100.0 if k == "close" and v > 1.5 else v
        return None

    for c in d.get("candlesticks", []):
        ts = int(c.get("end_period_ts", 0))
        out[ts] = (val(c.get("yes_ask")), val(c.get("yes_bid")))
    return out


def feed_minutes(product, start, end):
    """{minute_start_ts: close} from Coinbase 1-minute candles."""
    out, t = {}, start
    while t < end:
        t2 = min(t + timedelta(minutes=290), end)
        url = (bot.COINBASE.format(product) + "&start=" + t.strftime("%Y-%m-%dT%H:%M:%SZ")
               + "&end=" + t2.strftime("%Y-%m-%dT%H:%M:%SZ"))
        for r in get(url):
            out[int(r[0])] = float(r[4])
        t = t2
        time.sleep(0.35)
    return out


def run(days, cfg):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    feeds = {s: feed_minutes(bot.FEED[s], start - timedelta(hours=2), end) for s in cfg["series"]}
    events = []
    for s in cfg["series"]:
        for m in settled_markets(s, start):
            if m.get("result") in ("yes", "no") and m.get("close_time") and m.get("floor_strike") not in (None, ""):
                events.append((bot.parse_ts(m["close_time"]), s, m))
    events.sort(key=lambda e: e[0])

    bank, trades, skips, pause_until = float(cfg["start_bankroll"]), [], {}, None
    for close, s, m in events:
        t_open = close - timedelta(minutes=15)
        day = close.date().isoformat()
        day_pl = sum(x["pl"] for x in trades if x["day"] == day)
        reason = None
        if day_pl <= -cfg["daily_loss_limit"]:
            reason = "daily loss limit hit"
        elif pause_until and close <= pause_until:
            reason = "paused after losing streak"
        if reason:
            skips[reason] = skips.get(reason, 0) + 1
            continue
        try:
            km = kalshi_minutes(s, m["ticker"], close - timedelta(minutes=int(cfg['max_minutes_left']) + 2), close)
        except Exception:
            skips["no Kalshi price history"] = skips.get("no Kalshi price history", 0) + 1
            continue
        feed = feeds[s]
        strike = float(m["floor_strike"])
        o = int(t_open.timestamp())
        fo = feed.get(o - 60) or feed.get(o)
        placed, last_reason = None, "no side at %dc+" % int(cfg["min_price"] * 100)
        for mins_left in range(int(cfg['max_minutes_left']), 0, -1):
            t = close - timedelta(minutes=mins_left)
            ts = int(t.timestamp())
            ya, yb = km.get(ts, (None, None))
            if ya is None:
                last_reason = "no Kalshi price at that minute"
                continue
            na = (1 - yb) if yb is not None else None
            if ya >= cfg["min_price"]:
                side, ask = "yes", ya
            elif na is not None and na >= cfg["min_price"]:
                side, ask = "no", na
            else:
                last_reason = "no side at %dc+" % int(cfg["min_price"] * 100)
                continue
            if ask > cfg["max_price"]:
                last_reason = "price above limit"
                continue
            fnow = feed.get(ts - 60)
            hist = [feed.get(ts - 60 * k) for k in range(61, 0, -1)]
            hist = [h for h in hist if h]
            if not fo or not fnow or len(hist) < 30:
                last_reason = "missing price feed"
                continue
            rets = [math.log(hist[i] / hist[i - 1]) for i in range(1, len(hist))]
            mu = sum(rets) / len(rets)
            sig = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
            if sig <= 0:
                continue
            est = strike * fnow / fo
            z = math.log(est / strike) / (sig * math.sqrt(mins_left))
            p = bot.phi(z) if side == "yes" else 1 - bot.phi(z)
            if (z > 0) != (side == "yes"):
                last_reason = "market and model disagree on side"
                continue
            if abs(rets[-1]) / sig > cfg["max_jump_sigmas"]:
                last_reason = "price too jumpy"
                continue
            if abs(z) < cfg["min_safety_sigmas"]:
                last_reason = "too close to target"
                continue
            frac = cfg.get("bet_fraction") or 0
            stake = bank * frac if frac > 0 else min(cfg["max_bet"], bank)
            n = int(stake // ask)
            if n < 1:
                last_reason = "bankroll too low"
                break
            fee = bot.kalshi_fee(n, ask)
            if p - (ask + fee / n) < cfg["min_edge"]:
                last_reason = "real chance below price"
                continue
            won = m["result"] == side
            cost = n * ask + fee
            pl = round((n if won else 0) - cost, 2)
            placed = {"ticker": m["ticker"], "series": s, "close": bot.iso(close), "day": day, "side": side,
                      "price": round(ask, 4), "contracts": n, "cost": round(cost, 2), "p": round(p, 4),
                      "mins_left": mins_left, "result": "win" if won else "loss", "pl": pl}
            break
        if not placed:
            skips[last_reason] = skips.get(last_reason, 0) + 1
            continue
        trades.append(placed)
        bank = round(bank + placed["pl"], 2)
        skips["bet"] = skips.get("bet", 0) + 1
        k = 0
        for x in reversed(trades):
            if x["result"] == "loss":
                k += 1
            else:
                break
        if k >= cfg["losing_streak_pause"]:
            pause_until = close + timedelta(minutes=cfg["pause_minutes"])

    w = sum(1 for x in trades if x["result"] == "win")
    pl = round(sum(x["pl"] for x in trades), 2)
    be = (sum(x["cost"] / x["contracts"] for x in trades) / len(trades)) if trades else 0
    eq, peak, dd = cfg["start_bankroll"], cfg["start_bankroll"], 0
    for x in trades:
        eq += x["pl"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    def resize(frac):
        b, peak, worst_dd, lo = float(cfg["start_bankroll"]), float(cfg["start_bankroll"]), 0.0, float(cfg["start_bankroll"])
        for x in trades:
            n = int((b * frac if frac > 0 else min(cfg["max_bet"], b)) // x["price"])
            if n < 1:
                break
            cost = n * x["price"] + bot.kalshi_fee(n, x["price"])
            b = round(b + ((n if x["result"] == "win" else 0) - cost), 2)
            peak = max(peak, b); worst_dd = max(worst_dd, peak - b); lo = min(lo, b)
        return {"end_bankroll": round(b, 2), "max_drop": round(worst_dd, 2), "lowest": round(lo, 2)}
    compare = {"flat_20": resize(0), "half_account": resize(0.5)}
    return {"compare": compare, "ran": bot.iso(end), "days": days, "from": bot.iso(start), "to": bot.iso(end),
            "windows": len(events), "trades": trades, "skips": skips,
            "summary": {"bets": len(trades), "wins": w, "losses": len(trades) - w, "profit": pl,
                        "end_bankroll": round(cfg["start_bankroll"] + pl, 2),
                        "win_rate": round(w / len(trades), 4) if trades else 0,
                        "break_even": round(be, 4), "max_drop": round(dd, 2),
                        "worst": min([x["pl"] for x in trades], default=0)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    cfg = bot.load(bot.CONFIG, None)
    res = run(a.days, cfg)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res["summary"], indent=1))
    print("skips:", res["skips"])


if __name__ == "__main__":
    main()
