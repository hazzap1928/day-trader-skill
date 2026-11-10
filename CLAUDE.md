# day-trader-skill: notes for Claude

A Claude skill pack for US day trading, plus a Trading 212 adapter that runs a **demo** account
unattended from this Mac (Intel i7, 16 GB, macOS 13, UK time). Education and tooling, not financial advice.
Branch `t212-adapter`. Run the tests with `cd tests && python3 -m unittest discover -s .`. Every test is
offline (fakes for Trading 212, Yahoo, Nasdaq, Telegram, launchd). Keep them passing.

## The parts

| Folder | What it does |
|---|---|
| `trader-scanner/` | `scan.py orb`: opening-range breakouts; scans `allowed_tickers` (or the universe) only, skips `blocked_tickers` |
| `trader-scanner/universe.py` | the tradable universe: S&P 500 -> on Trading 212 -> $5+ -> $20m+ a day traded (the spread proxy: no free bid/ask); weekly, with a change log |
| `trader-entry/`, `trader-exit/` | entry trigger check; stop (opening-range midpoint) and target (2R by default) |
| `trader-timemachine/` | replays the plan on 10,000 simulated sessions plus history; reports R before **and after costs** |
| `trader-budget/` | `size.py`: shares from the budget, the cash buffer, max position and max loss |
| `trader-risk/` | `check.py`: the last gate. Loss limits, trades a day, allow/block lists, reward:risk **after costs** |
| `trader-alerts/`, `trader-news/`, `trader-analyst/`, `trader-pelosi/` | quotes and market clock, news, facts, filings |
| `trader-broker/t212.py` | Trading 212 API: reads, `place` (a typed yes in a terminal), `guard` (exits only), `budget`, `fx` |
| `trader-broker/autopilot.py` | always-on demo autopilot: scan, Time Machine, size, risk, place; phone commands |
| `trader-broker/phone.py` | the user's own Telegram bot: messages, Approve/Reject buttons, an outbox for failed sends |
| `trader-broker/evidence.py` | evidence mode (forward test): signal log, virtual results, slippage, Friday report |
| `trader-broker/briefing.py`, `report.py` | 07:30 morning briefing; closing report from Trading 212's own fills |
| `trader-broker/account_budget.py` | budget and loss limits from the account value, every trading morning |
| `trader-broker/netwait.py` | no internet: check every 2 min for up to 30, then a BACK ONLINE message saying what was missed |
| `trader-broker/go_live.py` | the user's own switch to live (checklist in `GO_LIVE.md`). **Claude never runs it** |
| `trader-kronos/` | Kronos-small forecasts, **shadow only**; own Python 3.11 venv; `.venv/`, `vendor/`, `models/` are git-ignored |
| `research/` | backtest scripts and `README.md` with the results (data goes in `trader/research-data/`) |

The guard's protections:
- stops are good till cancelled, and checked again 1 minute after placing and on every cycle
- a REJECTED or missing stop becomes a **software stop**: a market sell at the stop, with a loud alert
- trailing stop at 1R
- end-of-day close at 15:50 New York: it cancels each stop, then sells at market

## Scheduling (launchd, `~/Library/LaunchAgents/com.daytraderskill.*.plist`)

| Job | When | What |
|---|---|---|
| `caffeinate` | always (KeepAlive) | `caffeinate -s`: no sleep on mains power (a closed lid still sleeps the laptop) |
| `autopilot` | always (RunAtLoad + KeepAlive) | `run-autopilot.sh` runs `autopilot.py serve`: answers /status /pause /resume at any hour, delivers the outbox, catches up a missed briefing, rebuilds the universe each Sunday 18:00 UK (or next chance outside market hours), sets the budget before the open, sends MARKET OPEN, scans every 15 min 09:45 to 15:30 New York, sends the closing report about 15:55 and the evidence report on Fridays |
| `t212-guard` | weekdays 13:00 UK | `run-guard.sh`: `t212.py budget --apply`, then `guard --watch` from the 09:30 open to 16:00 New York |
| `briefing` | weekdays 07:30 UK | `run-briefing.sh`: the briefing; skips US holidays; retries its send for 30 min |

- **Keys:** the scripts read only the `export T212_API_KEY/T212_API_SECRET/T212_ENV/TELEGRAM_BOT_TOKEN`
  lines from `~/.zshrc`, and use `/usr/bin/python3`.
- **The python.org 3.11** at `/usr/local/bin/python3` lacks certificates: HTTPS fails from it. Use
  `/usr/bin/python3` for live calls.
- **Restart a job:** `launchctl kickstart -k gui/$(id -u)/com.daytraderskill.<job>`

## Current config (`trader/config.json`, git-ignored; backups beside it as `config.json.bak-*`)

- **Account:** GBP, Trading 212 **demo** (`"paper": true`), fractional shares, `stop_mode` broker,
  trailing stop 1R, `eod_close` 15:50.
- **`account_budget` on:**
  - `budget_gbp` = the account value with `cash_buffer_pct` 10, so 90% can be invested
  - max loss 1% a trade, 3% a day of the start-of-day value
  - no `max_budget_gbp` cap; on 9 Nov: GBP 5,024.80 account, GBP 50.24 a trade, GBP 150.74 a day
- **Trading rules:** `max_trades_per_day` 5, `min_reward_risk` 1.5, entries 09:45 to 15:30 New York.
- **Lists:** `allowed_tickers` holds 100 liquid large caps and ETFs (US-listed ETFs are mostly not
  buyable from a UK account); `blocked_tickers` is empty and always wins.
- **`universe`** (`trader-scanner/universe.py`): `enabled` true since 10 Nov (502 S&P 500 stocks). On demo with `enabled` true, the filtered list in
  `trader/universe/tradable.json` replaces `allowed_tickers` at read time (the config file is never
  changed). `allowed_tickers` is the fallback (missing, broken or over 14 days old) and is what live
  always uses. A refresh that looks broken keeps last week's list. Changes: `trader/universe/changes.jsonl`.
- **Costs** (GBP account): 0.15% FX fee + 0.05% slippage on every buy and sell. Trading 212's API
  trades only in the primary currency, so a USD balance cannot avoid the fee for API orders.
- **`autopilot`:** `auto_buy: true`, `evidence_mode: true`.

## Evidence mode: the forward test (9 to 20 Nov 2026)

- **Demo only.** Live refuses it in three places:
  - trader-risk, when `paper` is not true
  - `t212.py place --live`
  - `go_live.py --check`
- **Cost rules relaxed:** the cost block (`min_net_r`) is off, and the after-costs reward:risk minimum
  is 1.0. Every other limit still applies.
- **Logs:** every signal goes to `trader/state/evidence/signals.jsonl`, with a virtual result after
  costs added after the close. Real orders go to `trades.jsonl` with the expected price, for slippage.
  Each row has `list_from`: `allowed_tickers` (the hand list, 9 Nov) or `universe` (from 10 Nov);
  `evidence.py tag` tags old rows by day (`evidence.universe_from`, default 2026-11-10).
- **Kronos** gives a verdict on each signal it has time for, up to 9 min a cycle, traded signals first.
  One verdict takes about 5 min on this Mac, so most signals are "skipped". It never blocks anything.
  Before 10 Nov every verdict was "error": the model loader printed to stdout before the JSON.
- **The Friday Telegram report** covers:
  - real and virtual results after costs
  - actual against assumed slippage
  - the return against an S&P 500 tracker
  - Kronos agree against disagree
  - hand list and universe results on separate lines
- **To pass:** a positive average per real trade after costs AND beating the tracker over the same days.
- **Prior research** (`research/README.md`): the current ORB rule is about break-even before costs
  (-0.03R) and loses about 0.65R a trade after them. Nothing tested beat holding an S&P 500 tracker.

## Safety rules (always)

- **Never place, cancel or change an order yourself.** `t212.py place` needs the user's typed yes in
  their own terminal. Only the demo autopilot (with the user's `auto_buy` setting) and the guard
  (exits only: stops, sells) act unattended.
- **Never switch to live,** never run `go_live.py`, and never set `live_enabled`, `paper: false`, or
  `LIVE_PLACEMENT_ENABLED`. Going live is the user's decision.
- **Never read, print, log or ask for** API keys, secrets or the Telegram token. Only variable names
  appear in `~/.zshrc` reads. Errors are scrubbed.
- **Before editing `trader/config.json`,** back it up. Never turn on auto-buy or evidence mode on live.
- **No personalized investment advice:** tickers are a rule's output, not recommendations. Report
  backtests honestly, with costs, random baselines and buy-and-hold, and flag luck and overfitting.
- **Tests:** they must never touch the network, the real config, launchd or the phone (use the existing
  fakes).
- **Commits:** commit only when asked, and never push.
- **Text files:** no em dashes in repo text files (a test checks).

## Where things are

| Path | What |
|---|---|
| `trader/logs/` | `guard.log`, `autopilot.log`, `briefing.log`, `universe.log`, `guard-alerts.jsonl`, `outages.jsonl`, `t212-orders-DATE.jsonl` |
| `trader/state/` | `broker.json` (placements), `t212-guard.json` (stops, software stops, trail, end-of-day), `autopilot.json`, `risk.json`, `account-days.json`, `phone.json`, `phone-outbox.json`, `briefing.json`, `evidence/` |
| `trader/briefings/`, `trader/reports/` | each day's briefing; closing and evidence reports |
| `trader/universe/` | `tradable.json` (the universe), `changes.jsonl` (each refresh), `refresh.json` (last start) |
| `research/` | scripts and results summary; data in `trader/research-data/` or `$RESEARCH_DATA` |
| `trader-broker/PHONE.md`, `GO_LIVE.md`, `SKILL.md` | phone setup and messages, the go-live checklist, the broker skill |
