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
| `trader-broker/drift.py` | post-earnings drift **shadow** scan: shortlist, signal log, virtual results. No Trading 212 connection, never an order |
| `trader-broker/evidence.py` | evidence mode (forward test): signal log, virtual results, slippage, Friday report |
| `trader-broker/briefing.py`, `report.py` | 07:30 morning briefing; closing report from Trading 212's own fills |
| `trader-broker/allocation.py` | demo cash allocation: per-trade cap, morning reservations, free pool, max open positions |
| `trader-broker/account_budget.py` | budget and loss limits from the account value, every trading morning |
| `trader-broker/day_risk.py` | today's realised losses plus risk still open at the stops, for the daily loss limit (`state/day-risk.json`) |
| `trader-broker/netwait.py` | no internet: check every 2 min for up to 30, then a BACK ONLINE message saying what was missed |
| `trader-broker/go_live.py` | the user's own switch to live (checklist in `GO_LIVE.md`). **Claude never runs it** |
| `trader-kronos/` | Kronos-small forecasts, **shadow only**; own Python 3.11 venv; `.venv/`, `vendor/`, `models/` are git-ignored |
| `research/` | backtest scripts and `README.md` with the results (data goes in `trader/research-data/`) |

The guard's protections:
- stops are good till cancelled, and checked again 1 minute after placing and on every cycle
- a REJECTED or missing stop becomes a **software stop**: a market sell at the stop, with a loud alert
- trailing stop: starts once 1R in profit (`activate_r`, default 1), then trails 1R under the high
- end-of-day close at 15:50 New York: it cancels each stop, then sells at market. Which positions
  depends on the buy record in `trader/state/broker.json` (see "Strategy labels" below)

## Strategy labels (post-earnings drift, stage 1 of 4)

- Each buy record has `"strategy"`: `"intraday"` or `"drift"`. `t212.py place` (and so the autopilot)
  labels every buy `"intraday"`. A record with no label counts as intraday. Nothing buys drift yet:
  the drift scan, drift buying behind a switch, and switching it on are stages 2 to 4.
- A drift record also has `"sell_date"`, a New York trading date. `t212.drift_sell_date(entry, n)`
  works it out as n NYSE trading days after the entry date (the holiday list in
  `trader-alerts/alerts.py`, 2026 and 2027 only; past that it refuses).
- The 15:50 close sells intraday and unlabelled positions, and drift positions on or after their
  `sell_date` (or with no readable one). It keeps a drift position before its sell date, stop untouched.
  A position with no buy record at all is **not sold**: it gets a GUARD ALERT instead.
- The 1R trailing stop and half-sell-at-target are intraday only. Drift keeps its fixed stop, the
  missing-stop check and the software-stop backup.
- Tests: `tests/test_drift.py`.

## Drift shadow scan (stage 2 of 4, from 10 Nov 2026): `trader-broker/drift.py`

- **Shadow only.** It has no Trading 212 connection (a test checks the source and fails on any call).
  It reads Nasdaq's earnings calendar and Yahoo daily bars, and writes only `trader/state/drift/`.
  Demo only: it refuses when `paper` is not true. Stage 3 adds buying behind a switch; stage 4 turns it on.
- **The rule** (research/pead_tracker.py, ranking B, 10 slots):
  - a beat (EPS above forecast), surprise >= 10%, forecast >= $0.10 either side of zero, as tested
    (`forecast_either_side: false` needs +$0.10)
  - in the current universe (allowed_tickers when it is off or stale), not on blocked_tickers
  - report on day D: virtual buy at the open of the 2nd trading day after D; virtual sell at the close
    of the 20th trading day held (the buy day is day 1)
  - ranked by (EPS - forecast) / price (the latest price; the buy-day open is recorded later)
  - a virtual book of `slots` (10): shortlisted only when a slot is free on the buy day
- **When:** the autopilot runs the scan once a day at `scan_at` (15:35 New York), or just before the
  closing report if it missed that. After the report it fills in results. Errors are reported, never raised.
- **Files:**
  - `trader/state/drift/shortlist.json` is tomorrow's shortlist
  - `signals.jsonl` holds every qualifying signal, shortlisted or not, with the source and when it
    reported (Nasdaq leaves the time out for most past reports: "time not given")
- **Results:** at 5, 10, 20 and 40 trading days, after 0.15% FX + 0.05% slippage each way, each
  against SPY over the same dates (0.05% a leg). Only final closes (before today) are used.
- **Untested extras, logged only, never used:** the report-day and reaction-day moves, volume against
  the 20-day average, the highest gain and its day, the first day at +5/+10/+15%, and the 3xATR stop day.
- **Phone:** "Drift shortlist for tomorrow" in the closing report and "Drift would buy today" in the
  briefing, both marked SHADOW. A missing, out-of-date or broken shortlist says "no drift scan yet";
  with the scan off there is no line.
- **By hand:** `python3 trader-broker/drift.py scan|resolve|show`.
- **Why not:** `shortlist.json` also has `filled` (rows with EPS, forecast, both, all three), `in_list`
  and `why_not` (a count per failed rule), and the "none" line says how many had the figures and were in the list.
- **Tests:** `tests/test_drift_scan.py`.

## Real exit and the daily loss count (from 11 Nov 2026)

- **Simulations use the real exit.** On a Trading 212 config (`broker` trading212) the Time Machine
  and the evidence virtual results walk each trade the way the guard runs it: `timemachine.GuardExit`
  (stop checked first in each bar, a gap fills at the open; the trail starts at `activate_r` from the
  highest 5-minute close, 1R under it, never lowered, raised by at least $0.05 or 0.1R; half at the
  target only with `sell_half_at_target`, which needs the trail on; out at the 15:50 close). There is
  no fixed target exit. `timemachine.exits_from(cfg)` reads the exits; any other broker keeps the
  fixed target. The replay ends at `eod_close.at` (15:50) even when `--until` is later.
- **The go/no-go rule is the same rule on the real exit:** the share of replays that made money
  (among those that made or lost money) must beat the break-even share at their average win and
  loss, avg loss / (avg win + avg loss). That is exactly "average R before costs above 0". The cost
  block (`min_net_r`) uses the real-exit average too (off in evidence mode). `p_target`/`p_stop`
  are still reported, for information.
- **Daily loss count** (`"daily_loss_count"` in the config): `"open_risk"` (on, demo) counts losses
  realised today (net per entry, after FX fees, from Trading 212's fills; a closed winner counts 0)
  plus risk still open at each held entry's current stop (stop order, else software stop, else the
  planned stop; 0 once the stop is at or over the price paid). An entry not seen yet counts its full
  max loss. `"full_risk"` (the default when the key is missing) is the old count: the full max loss
  of every entry today. The autopilot writes `trader/state/day-risk.json` before each risk check;
  trader-risk falls back to the old count (with a warning) if it is missing, broken, not today's,
  or over 15 minutes old. On a failed read the autopilot deletes it. The 3% cap is unchanged.
- **Reports say the same rule:** the closing report and the BOUGHT message give the Time Machine's average R
  before costs (and after), the winning share and the break-even share, and compare the day's trades by
  average R before and after costs. "Target first" odds appear only for old tickets, labelled the old
  fixed-2R measure (`report.tm_words`, `report.tm_line`).
- **Tests:** `tests/test_real_exit.py`.

## Cash check and late fills (from 12 Nov 2026)

- **Cash check before every autopilot buy** (`t212.free_cash`, in `cmd_place` when `args.cash_check`):
  `availableToTrade` minus buys still waiting to fill (priced at their record's planned entry, with the
  FX fee) minus the cash buffer (`cash_buffer_pct` of `budget_gbp`). A buy it does not cover is skipped
  (`NoCash`), never shrunk. Trading 212's `insufficient-free-for-stocks-buy` refusal is treated the same.
- **In the autopilot:** a cash skip sends no message of its own; the rest of that scan is not built (nor
  marked tried); one summary per scan ("N candidates skipped, no free cash (demo): ..."), to the phone
  only when it names a stock not reported today. After a skip, later scans build nothing until cash is free.
- **`max_tickets_per_day`** counts only tickets that became a buy (not `refused` or `no cash`), and is
  checked between buys inside a scan too.
- **The guard** already places a missing stop when a late fill shows up (every 5 min) and pushes
  "stop placed". A position whose stop order was refused or is gone is alarmed again at most once an hour.
- Tests: `tests/test_cash_and_late_fill.py`.
- **The account snapshot** (`t212.portfolio_snapshot`, `Pilot.write_portfolio`): before trader-budget, the
  autopilot writes `trader/state/portfolio.json` (USD, read-only from Trading 212): cash = availableToTrade
  minus waiting buys not already in `reservedForOrders` minus reservations for other stocks (the buffer is
  taken off by trader-budget/trader-risk), plus every held or waiting position. Both are passed it with
  `--portfolio`. Before this, no file existed and both saw an empty account funded with the whole budget
  (the 11 Nov insufficient-funds refusals). A failed read removes the file (budget fallback; the
  placing-time check still decides). `free_cash` also no longer counts a waiting buy that Trading 212
  already holds back in `reservedForOrders`. Tests: `tests/test_cash_view.py`.

## Cash allocation and confidence (from 12 Nov 2026): `trader-broker/allocation.py`

- **Demo only:** `allocation.active(cfg)` is false unless `paper` is true (and no `live_enabled`).
- **Per-trade cap:** `max_position_pct` 22 (was 75) of `budget_gbp`, so 4 trades fit inside the 90%
  investable. trader-budget applies it; `t212.size_entry` applies it again (1% room for FX moves).
  With stops about 1% away the cap binds on most trades: about GBP 15 at the stop, not GBP 49.
- **`max_open_positions`** 4: held positions plus buys waiting to fill. A signal over it is still
  built and logged (`skipped: 4 positions open...`), so it gets its virtual result.
- **Reservations:** the 07:30 briefing writes `trader/state/allocation.json`: up to `max_reservations`
  (2) blocks of one max-per-trade, for listed stocks that reported earnings after the last close or
  report before the open (ranked by `adv_usd`). Never more than `100 - free_pool_pct` (50%) of the
  investable cash. A plan, never a buy. A reservation ends `used` (bought), `released` (failed the
  checks, skipped, not placed) or `expired` (`window_minutes` 90 after 09:45 NY). A re-run never
  resets the day's plan. No plan: every buy uses the free pool.
- **Cash check:** `t212.free_cash(..., reserved=)` also takes off active reservations for OTHER stocks
  (`args.reserved_gbp` in `cmd_place`).
- **Logs:** `trader/state/evidence/allocation.jsonl` (plan, reserved, used, released, expired, skipped).
  Trade rows add `pool` (`free` / `reserved:SYM`), `sizing` (`v2`), `risk_gbp`, `cost_gbp`, `confidence`.
- **Confidence** (logged only, never sizes or blocks): the Time Machine's average R before costs,
  `low` < 0.00R <= `medium` < 0.10R <= `high` (`evidence.confidence_bands`). On every signal and trade.
  The Friday report adds results in R by level, v2 rows only (real R = GBP result / `risk_gbp`).
- Settings: `"allocation": {"enabled", "max_open_positions", "free_pool_pct", "max_reservations",
  "window_minutes"}`. Tests: `tests/test_allocation.py`.

## Scheduling (launchd, `~/Library/LaunchAgents/com.daytraderskill.*.plist`)

| Job | When | What |
|---|---|---|
| `caffeinate` | always (KeepAlive) | `caffeinate -s`: no sleep on mains power (a closed lid still sleeps the laptop) |
| `autopilot` | always (RunAtLoad + KeepAlive) | `run-autopilot.sh` runs `autopilot.py serve`: answers /status /pause /resume at any hour, delivers the outbox, catches up a missed briefing, rebuilds the universe each Sunday 18:00 UK (or next chance outside market hours), sets the budget before the open, sends MARKET OPEN, scans every 15 min 09:45 to 15:30 New York, runs the drift shadow scan at 15:35, sends the closing report about 15:55 (then fills in drift results) and the evidence report on Fridays |
| `t212-guard` | weekdays 13:00 UK | `run-guard.sh`: `t212.py budget --apply`, then `guard --watch` from the 09:30 open to 16:00 New York |
| `briefing` | weekdays 07:30 UK | `run-briefing.sh`: the briefing; skips US holidays; retries its send for 30 min |

- **Keys:** the scripts read only the `export T212_API_KEY/T212_API_SECRET/T212_ENV/TELEGRAM_BOT_TOKEN`
  lines from `~/.zshrc`, and use `/usr/bin/python3`.
- **The python.org 3.11** at `/usr/local/bin/python3` lacks certificates: HTTPS fails from it. Use
  `/usr/bin/python3` for live calls.
- **Restart a job:** `launchctl kickstart -k gui/$(id -u)/com.daytraderskill.<job>`

## Current config (`trader/config.json`, git-ignored; backups beside it as `config.json.bak-*`)

- **Account:** GBP, Trading 212 **demo** (`"paper": true`), fractional shares, `stop_mode` broker,
  trailing stop 1R (from 1R profit), `eod_close` 15:50.
- **`account_budget` on:**
  - `budget_gbp` = the account value with `cash_buffer_pct` 10, so 90% can be invested
  - max loss 1% a trade, 3% a day of the start-of-day value; `daily_loss_count` `open_risk`
    since 11 Nov (losses realised today + risk open at the stops; `full_risk` is the old count)
  - no `max_budget_gbp` cap
- **Allocation (12 Nov):** `max_position_pct` 22, `allocation` on (4 positions, 50% free pool, 2
  reservations, 90 min), `evidence.confidence_bands` [0.0, 0.10].
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
- **`drift`:** `scan_enabled: true` since 10 Nov (shadow only; every threshold below is a setting).

## Evidence mode: the forward test (from 9 Nov 2026, no end date)

- **Open-ended:** `"evidence": {"start": "2026-11-09", "min_trades": 20}`. It runs until it is turned
  off; an old `end` key is ignored. The demo agent runs indefinitely.
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
  Each row also has `rules`: `v1` (to 10 Nov: fixed 2R target in the simulations, full-risk daily
  loss count) or `v2` (from 11 Nov: real exit, open-risk count). New rows are logged `v2`; untagged
  rows are `v1` (`evidence.py tag` writes it). v1 rows keep their fixed-target virtual result.
- **Kronos** gives a verdict on each signal it has time for, up to 9 min a cycle, traded signals first.
  One verdict takes about 5 min on this Mac, so most signals are "skipped". It never blocks anything.
  Before 10 Nov every verdict was "error": the model loader printed to stdout before the JSON.
- **The Friday Telegram report** goes out every Friday, forever, and covers:
  - real and virtual results after costs
  - actual against assumed slippage
  - the return against an S&P 500 tracker
  - Kronos agree against disagree
  - hand list and universe results on separate lines
  - old rules (v1) and new rules (v2) results on separate lines
- **Running status, no final PASS/FAIL day:** each Friday shows ON TRACK or NOT ON TRACK for "this
  week" (Monday, or the start, to Friday) and "since the start", each with its closed-trade count.
  ON TRACK = a positive average per real trade after costs AND beating the tracker over the same days.
  Below `min_trades` closed trades it says "too few trades to judge" (and how it reads so far).
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
- **Commits:** commit only when asked; push only when asked. Commits use the GitHub no-reply email
  (`git config --local user.email`). The repo is a PUBLIC fork.
- **Before every push:** the `.githooks/pre-push` secrets check runs (`core.hooksPath .githooks`; a
  fresh clone needs `git config core.hooksPath .githooks`). It refuses commits that add `.env`,
  `config.json`, `phone.json`, anything under `trader/`, token-shaped lines, or the exact key values
  when they are in the shell. If it stops a push, stop and tell the user; never use `--no-verify`.
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
