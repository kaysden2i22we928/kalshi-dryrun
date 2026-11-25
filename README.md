# Kalshi dry-run bot

Pretend $150 account that bets Kalshi's 15-minute BTC and gold markets by your rules, for one week, **with no real money and no API key**. Results show on a dashboard website.

## Set it up (about 10 minutes, free)

1. Make a free account at github.com.
2. Create a new **public** repository (public = free unlimited run time). Name it `kalshi-dryrun`.
3. Upload every file in this folder, keeping the folders (`docs/`, `.github/workflows/`). On a computer: "Add file → Upload files", drag the whole folder in.
4. **Settings → Pages** → Source: "Deploy from a branch" → Branch `main`, folder `/docs` → Save. Your dashboard will be at `https://YOUR-NAME.github.io/kalshi-dryrun/`.
5. **Actions** tab → enable workflows → open "Dry-run bot" → **Run workflow**. After that it runs by itself every 10 minutes.

## Backtest

Right after upload, GitHub replays the last 7 days of real Kalshi windows with your rules (Actions → "Backtest last 7 days"). Results appear at the top of the dashboard in 10–30 minutes. Run it again any time from the Actions tab.

## Change the rules / stop it

Edit `config.json` on GitHub (pencil icon):
- `"enabled": false` = kill switch.
- `min_price`, `max_price`, `max_minutes_left`, `max_bet`, `daily_loss_limit`, `losing_streak_pause`, `pause_minutes`, `min_safety_sigmas`, `max_jump_sigmas`.
- `news_blackouts_utc`: add times to sit out (Fed days, jobs reports). Times are UTC.
- `ntfy_topic`: install the free **ntfy** app, subscribe to a made-up topic name, put the same name here to get phone alerts for bets, losses and pauses.

## What it does every 15 seconds

1. Finds the current 15-minute BTC and gold market on Kalshi.
2. In the last 2 minutes, if one side costs 92–97¢, it estimates the real chance from live price moves (Coinbase BTC-USD and PAXG-USD as the gold feed).
3. Bets (on paper) only if: real chance beats price + Kalshi's fee, price is at least 2× a normal move away from the target, the last minute wasn't a jump, it's not a news blackout, today's loss is under the limit, it isn't paused after 2 losses in a row, and it hasn't bet this window yet.
4. Settles bets from Kalshi's official results and updates `docs/data.json`.

## Limits

- GitHub starts scheduled runs late sometimes, so it will miss some windows. "Windows watched" on the dashboard shows how many it saw.
- Paper fills assume you get the ask price. Real orders can fill worse.
- GitHub Actions is meant for software projects. Fine for a one-week test; for real money, run it on a small server instead.
- `dry_run` must stay `true`. This code has no way to place real orders.
