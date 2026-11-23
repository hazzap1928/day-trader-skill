# day-trader-skill: notes for Claude

(The repo and folder keep their old name, `day-trader-skill`, for now.)

## What this is now (17 Nov 2026)

A **news-feed and post-earnings-drift agent** that runs a Trading 212 **demo** account unattended from
this Mac: the always-on Mac mini at home (Apple M6, 24 GB, Apple Silicon; it replaced the old 2017 Intel
MacBook in Nov 2026; UK time). It buys each share for a written reason, holds it until a
rule says sell, and reports how it did. Education and tooling, not financial advice.
Branch `t212-adapter`. Run the tests with `cd tests && python3 -m unittest discover -s .`. Every test is
offline (fakes for Trading 212, Yahoo, Nasdaq, Telegram, launchd). Keep them passing.

**The two strategies that trade:**
- **Drift** (post-earnings drift): buys EPS beats the morning after the report and holds 20 trading days
  (`drift.py`, `drift_buy.py`). Buying ON since 12 Nov.
- **News** (the news feed): two scheduled `claude -p` web-research runs a day produce dated, sourced ideas
  with a stop and a sell date (`news_ideas.py`). Buying ON since 17 Nov.

**Day trading is OFF.** `"autopilot": {"intraday_enabled": false}` (the owner's decision, evening of
17 Nov): the ORB scan never runs. The ORB code, its tests and its limits are **still in the repo**, dormant,
and are marked **(dormant: day trading)** below. Do not build on them. Do not delete them unless the owner
asks (the spec retires the ORB scanner last).

**More additions are to be confirmed (TBC).** Nothing else is planned in writing. When the owner adds a
strategy or a feed, add it to "The parts" and give it its own section. Known loose ends:
- asking about a holding that left the universe or went on the blocked list (the review only says it would)
- any approval step for drift (its verdicts are logged only)
- removing the dormant ORB code, tests and limits
- the entry timing shadow is built (22 Nov, price-based); changing WHEN or HOW drift and news buy is the owner's decision
- a serious filing triggering the existing "sell it?" approval: proposed 22 Nov, not built
- the holiday list in `trader-alerts/alerts.py` covers 2026 and 2027 only

## The parts

| Folder | What it does |
|---|---|
| `trader-scanner/` | **(dormant: day trading)** `scan.py orb`: opening-range breakouts; scans `allowed_tickers` (or the universe) only, skips `blocked_tickers` |
| `trader-scanner/universe.py` | **(in use)** the tradable universe, which drift and news both read: S&P 500 -> on Trading 212 -> $5+ -> $20m+ a day traded (the spread proxy: no free bid/ask); weekly, with a change log |
| `trader-entry/`, `trader-exit/` | **(dormant: day trading)** entry trigger check; stop (opening-range midpoint) and target (2R by default) |
| `trader-timemachine/` | `timemachine.py` **(dormant: day trading)**: replays the day-trade plan on 5-minute bars. `holds.py` **(in use, shadow)**: the Time Machine for held trades on daily bars (see "Reworked tools for holds") |
| `trader-budget/` | `size.py`: (used by news sizing through `t212.size_entry`) shares from the budget, the cash buffer, max position and max loss |
| `trader-risk/` | `check.py`: the last gate for news buys (strategy caps) and the dormant ORB path. Loss limits, trades a day, allow/block lists, reward:risk **after costs** |
| `trader-alerts/`, `trader-news/`, `trader-analyst/` | quotes and market clock, news, facts, filings. `trader-news/news.py` is also read by `hold_watch.py` |
| `trader-scanner/chart.py` | **(in use, labels only)** chart labels at each drift and news buy: the scanner's daily patterns plus "falling apart" checks |
| `trader-broker/t212.py` | Trading 212 API: reads, `place` (a typed yes in a terminal), `guard` (exits only), `budget`, `fx` |
| `trader-broker/autopilot.py` | always-on demo autopilot: runs the drift and news buy steps, the holdings match, reports and phone commands; the ORB scan (scan, Time Machine, size, risk, place) is skipped while `intraday_enabled` is false |
| `trader-broker/phone.py` | the user's own Telegram bot: messages, Approve/Reject buttons, an outbox for failed sends |
| `trader-broker/messages.py` | **every word sent to the phone**, one function per message type (see "Phone messages") |
| `trader-broker/drift.py` | post-earnings drift **shadow** scan: shortlist, signal log, virtual results. No Trading 212 connection, never an order |
| `trader-broker/drift_buy.py` | drift **buying** (stage 3), only with `drift.buy_enabled` (ON since 12 Nov): the 09:33 buys, reservations, holdings lines, DRIFT SELL, the Friday drift section |
| `trader-broker/holdings.py` | the **holdings record**: one page per buy (reason, stop, sell rule, result), the match and the open-risk sum. Broker-neutral: no broker code |
| `trader-broker/holdings_t212.py` | Trading 212's side of the record: the account as a plain "view", and the daily match. **Read-only, report only** |
| `trader-broker/news_ideas.py` | the **"news" strategy**: ideas from two scheduled `claude -p` runs a day, checked by the pack's rules, plus a verdict on every holding and the day's hot topics. Buys only with `news.buy_enabled` (ON since 17 Nov, see "News ideas") |
| `trader-broker/review.py` | the **daily review** of what is held, after the close. **Report only**: it says what it would sell or ask about (see "The daily review") |
| `trader-broker/board.py` | the **evening strategy board**: one HTML page, a card per strategy, sent to the phone as a file after the review. **Report only** (see "The evening strategy board") |
| `trader-broker/evidence.py` | **(mostly dormant: day trading)** evidence mode (forward test): signal log, virtual results, slippage, Friday report |
| `trader-broker/briefing.py`, `report.py` | 07:30 morning briefing; closing report from Trading 212's own fills |
| `trader-broker/allocation.py` | demo cash allocation (drift reserves its slots first; the intraday reservations are dormant): per-trade cap, morning reservations, free pool, max open positions |
| `trader-broker/account_budget.py` | budget and loss limits from the account value, every trading morning |
| `trader-broker/day_risk.py` | writes the total-risk figures the buy checks read; the daily loss count is for day trades only: today's realised losses plus risk still open at the stops, for the daily loss limit (`state/day-risk.json`) |
| `trader-broker/heartbeat.py` | the outside "is this Mac alive?" check: the always-on autopilot pings `HEALTHCHECK_URL` (healthchecks.io) every 5 minutes; when the pings stop, that service tells the owner. Off without the address |
| `trader-broker/wallpaper.py` | the desktop wallpaper's status: the always-on autopilot POSTs running/stopped, value, day change, cash share, positions, last trade, a 30-value trend and up to 3 build items, every 15 minutes, after each trade and at shutdown. Off without the address and key |
| `trader-broker/netwait.py` | no internet: check every 2 min for up to 30, then a BACK ONLINE message saying what was missed |
| `trader-broker/go_live.py` | the user's own switch to live (checklist in `GO_LIVE.md`). **Claude never runs it** |
| `trader-broker/shadow.py` | **review only**: Kronos's opinion of each drift and news buy, the stop shadow of each holding its stop sold, and the sell-rule replay and chart labels of each holding, written on the holdings pages (see "Kronos opinions and the stop shadow") |
| `trader-broker/hold_watch.py` | **report only**: new SEC filings and headlines on each holding; serious ones in the briefing (see "Reworked tools for holds") |
| `trader-broker/earnings_watch.py` | **report only**: each holding's next results date, flagged when it comes before the sell date |
| `trader-broker/hold_slippage.py` | **report only**: every drift and news fill against the price expected and the real market that minute, in the Friday report |
| `trader-broker/entry_shadow.py` | **shadow only**: what each buy would have cost at 09:45, 10:00, 10:30 or with a limit order |
| `trader-kronos/` | Kronos-small forecasts, **shadow only**: `kronos_daily.py` (the daily forecast to the sell date, used by `shadow.py` and the news runs) and `kronos_shadow.py` (the dormant day-trade one); own Python 3.11 venv; `.venv/`, `vendor/`, `models/` are git-ignored |
| `research/checkpoint_review.py` | the review checkpoint in plain English (read-only; see "Review checkpoint") |
| `research/` | backtest scripts and `README.md` with the results (data goes in `trader/research-data/`) |
| `docs/hold-agent-spec.md` | the hold-and-review spec and the owner's decisions of 16 Nov 2026 |

**Direction (16 to 17 Nov 2026):** the day-trading engine was replaced by a hold-and-review agent
(`docs/hold-agent-spec.md`). Built: the holdings record and daily match, "keep and report" at the close, a
risk cap for each strategy, pausing new buys on a mismatch, the daily review (report only), the news
strategy with two runs a day, a keep/watch/sell verdict on every holding, and the approval step for news
holdings (Approve sells soon after the next open). **The ORB scan is switched off**, not deleted:
`Pilot.cycle` returns "day trading is off" before it scans. **Since 17 Nov the code default of
`intraday_enabled` is `false`**: only `"intraday_enabled": true` turns it on, so a config without the key
(every backup but the newest) keeps day trading off. With it off (`t212.intraday_on`), no position gets
the trailing stop or the half sale, and the 15:50 close keeps and reports intraday and unlabelled
positions instead of selling them (tests: `test_autopilot.py`, `test_holdings.py`; the day-trading tests
switch it on in `test_t212_place.Base`). The guard, drift, news, the reviews and the reports run as before.
The four ORB tool folders are kept as repurpose candidates (`docs/repurpose-plan.md`).

The guard's protections:
- stops are good till cancelled, and checked again 1 minute after placing and on every cycle
- a REJECTED or missing stop becomes a **software stop**: a market sell at the stop, with a loud alert
- trailing stop (intraday only, dormant while day trading is off): starts once 1R in profit (`activate_r`, default 1), then trails 1R under the high
- end-of-day close at 15:50 New York: it cancels each stop, then sells at market. Which positions
  depends on the buy record in `trader/state/broker.json` (see "Strategy labels" below)

## The owner's decisions of 22 Nov 2026 on the stop and on slippage

- **The drift stop stays at 3 x ATR.** 4 x ATR was considered and REJECTED: its worst single trade in 2019-23
  was -40.3% (EOG, which opened 26% down in the March 2020 oil crash, straight through the stop), over the
  owner's limit of about -30%. Do not change `drift.atr_mult`, or the news stop, without the owner's written say.
- **Drift's edge over the S&P 500 disappears at 0.3% slippage a side** (`research/pead_stop.py --slip 0.3`):
  at the assumed 0.05% drift leads the S&P 500 over the same days by only +0.1 to +0.4 points a trade. Slippage
  is now the owner's main concern: every real fill is measured (`hold_slippage.py`), and after 10 fills the
  phone is told once whether it is consistently above 0.3% a side.
- **The first real fill (Everpure, 20 Nov):** +0.64% against the quote; split, Trading 212's own price was
  +0.97% above the real market in that minute (an "OTC" Practice fill) and the market had moved -0.32%.

## Reworked tools for holds (22 Nov 2026): the old day-trading tools, reused. ALL REPORT ONLY OR SHADOW

The owner's request (22 Nov 2026): reuse the useful parts of the day-trading tools for drift and news holdings.
**Nothing here buys, sells, blocks, delays, resizes or changes a stop**; each only logs and shows. Whether any
of it may ever affect a real buy or sale is the owner's decision, in writing. Broker-neutral: they read the
holdings record (`holdings.py`), never Trading 212 (except `hold_slippage.py`, which reads the guard's own file).

- **New filings and headlines on holdings** (`trader-broker/hold_watch.py`, reuses `trader-news/news.py`):
  each held drift or news page gets the 8-Ks, late-filing notices (NT 10-K/10-Q) and Yahoo headlines since
  its buy day, each kept once. The **serious rule, version 2** (the owner's, 22 Nov; changing it starts version 3):
  - filings: NT 10-K/10-Q; 8-K items 1.02, 1.03, 1.05, 2.04, 2.06, 3.01, 3.02, 4.01, 4.02, 5.01
    (`SERIOUS_8K`); and, read from the filing's own text (items 5.02, 1.01, 8.01): the company's own CEO or
    CFO leaving (not a division head; not a biography) or a new share offering. Other 5.02 changes: review only.
  - headlines: the title names the company AND matches `SERIOUS_WORDS` (bankruptcy, halt, fraud or
    restatement, short-seller report, CEO/CFO leaving, forecast cut, takeover, new share offering, recall or
    hack, regulator says no), or names it as a takeover target, or the SEC, Justice Department, FTC or
    prosecutors are named as acting. Shareholder law-firm notices never count unless those are named.
  - tested on real past examples (`tests/test_hold_watch.py` `PastExamples`, Intel and Boeing filings in
    `tests/fixtures/hold-watch/`).
  - **notable (version 1, the owner's, 22 Nov):** a headline naming the company with a weak forecast or results
    below estimates (`NOTABLE_WORDS`) is listed by name in the evening review (marked), never in the briefing.
  - **where:** the daily review lists each holding's new items (serious marked); the 07:30 briefing shows the
    serious ones only, inside the message, at most 3 a holding, each once. No messages of their own.
  - files: `trader/state/holdwatch/state.json`, `items.jsonl`. Switch: `"hold_watch": {"enabled": true}`
    (code default off; ON since 22 Nov). By hand: `python3 trader-broker/hold_watch.py show|check|briefed --since DATE`.
  - a one-time scheduled task (7 Dec 2026, "hold-watch-two-week-list") lists what reached the briefing.
- **Next results date during a hold** (`trader-broker/earnings_watch.py`): Nasdaq's company page (Zacks;
  "expected" = the company's date, "estimated" = from past dates). The review shows it; the phone hears a date
  inside the hold when first seen or moved, and 3 trading days before. Each new news idea's extra
  information records `earnings` with `in_hold`. Files: `trader/state/earnings-watch.json`, `.jsonl`. Switch:
  `"earnings_watch": {"enabled": true}` (code default off; ON since 22 Nov). By hand: `earnings_watch.py show|lookup SYM`.
- **The Time Machine for holds** (`trader-timemachine/holds.py`, beside the untouched 5-minute engine):
  `walk()` holds a share on daily bars under a sell rule: `fixed` (the stop as set), `rising` (raised after
  each close to the highest close minus the same distance, never lowered; untested idea), `none`. A day that
  opens under the stop sells at that open; costs 0.2% each way. `noise_test()` replays a holding 1,000 times
  on its own past ups and downs (trend taken out, 5-day blocks, bars before the buy day only).
  - per holding: `shadow.py` writes `page["replay"]` once (stop hit first %, typical result per rule).
  - research: `research/pead_stop.py`, the drift stop backtest (results in `research/README.md`, 22 Nov): the
    live 3 x ATR stop cost -0.18 (2019-23) and -0.29 (2024-26) points a trade against no stop; 4 x ATR about
    even; 2 x ATR and the rising stop clearly worse. Today's S&P 500 list only (delisted companies missing),
    so the stop's cost is probably overstated a little. Changing the stop is the owner's decision. With 4 x ATR
    the worst trade was -40.3% in 2019-23 (EOG, a gap down in March 2020), so the owner's condition for moving to
    4 x (worst no worse than about -30%) was not met; the stop stays 3 x. Slippage of 0.3% a side instead of
    0.05% leaves drift behind the S&P 500 over the same days (`pead_stop.py --slip 0.3`).
- **Chart labels at the buy** (`trader-scanner/chart.py`, reuses `scan.py find_setups`, version 1): plus
  `uptrend`, `below_ma50`, `down_10pct_5d`, `new_20d_low` ("falling apart" = any of the last three). From
  closes before the buy day only. `shadow.py` writes `page["chart"]` once; `research/pead_chart.py` compares
  past drift trades with and without each label (22 Nov: no label held up in both periods; in 2019-23 the
  "falling apart" charts did better, in 2024-26 no difference). **The owner's decision (22 Nov): the labels stay
  on the holdings page and in the daily review for information only; they are used in no decision, and no
  labelled-against-unlabelled comparison goes into any report (Friday, briefing, review, checkpoint).**
- **Every fill checked** (`trader-broker/hold_slippage.py`, `Pilot.fill_checks`, each evening after the review,
  only with `"fill_checks": {"enabled": true}`, ON since 22 Nov): one row per drift and news buy and sale in
  `trader/state/fills.json` (log `slippage.jsonl`): expected price (buy: the quote just before the order; stop
  sale: the stop; close or approved sale: the price the guard saw), Trading 212's own fill time and price
  (`holdings_t212.fill_finder`, the order history, READ-ONLY), and the real market in that minute (Yahoo 1-minute
  bars, `drift.minute_bars`, kept about 30 days). `slip_pct` (fill against expected) splits into `move_pct`
  (the market moving) and `market_gap_pct` (the broker's own pricing). Positive = worse for us.
  - The Friday report lists every fill (the last 12 on the phone), the running average and the split. After
    `JUDGE_AT` (10) measured fills the phone is told ONCE whether slippage is consistently above 0.3% a side
    (average over 0.3% AND at least 7 in 10 fills over it); the Friday report repeats the verdict.
  - Note on every report: Practice account fills are simulated and may understate real slippage.
- **Entry timing shadow** (`trader-broker/entry_shadow.py`, version 1, after each buy day, same switch): for each
  drift and news buy, the price at 09:45, 10:00 and 10:30 and a buy limit at the 09:32 price (good for the day;
  in the order's own minute it fills at the limit only), each against the real market when we filled
  (`vs_market_pct`, the timing alone) and against what we paid (`vs_fill_pct`). Never delays or changes a buy.
  Friday report: "If I had bought at another time". Files: `trader/state/entry-shadow.json`, `.jsonl`.
  `research/open_timing.py`: the same question on the last ~30 days of 1-minute prices (22 Nov: on 106 drift buy
  days, waiting to 10:00 cost +0.34% more on average, within chance; the gap for one buy is about 1% either way).
- **The daily review** shows, under each holding, one line per new item: chart labels and the replay (on the
  phone once, the day after they are worked out), new filings and headlines, and a results date in the hold.
- **Not reused:** the ORB scan, the entry timer, the exit planner, trader-budget, the day-trade Kronos, the
  trailing stop and half sale in the guard. **Opening-range timing** for drift and news buys (shadow) is the
  last candidate; the owner asked to be consulted before it is built.
- **Tests:** `test_hold_watch.py`, `test_earnings_watch.py`, `test_holds.py`, `test_chart.py`,
  `test_hold_slippage.py`, `test_shadow.py` (replay and chart).

## Strategy labels, and "keep and report" at the close (from 16 Nov 2026)

- Each buy record has `"strategy"`: `"intraday"`, `"drift"` or `"news"` (`t212.STRATEGIES`).
  `t212.py place` labels a buy `"intraday"` unless told otherwise. A record with **no** label counts
  as intraday. Any other label is kept as written: `t212.is_hold(rec)` is true for everything that
  is not intraday, unknown labels included.
- A drift or news record also has `"sell_date"`, a New York trading date. `t212.drift_sell_date(entry, n)`
  works one out as n NYSE trading days after the entry date (the holiday list in
  `trader-alerts/alerts.py`, 2026 and 2027 only; past that it refuses).
- **The 15:50 close** (`t212.eod_close`, `hold_due`) sells:
  - intraday and unlabelled positions, **only while day trading is on** (`t212.intraday_on`); with it off
    (the default) they are kept, stop untouched, and reported with a GUARD ALERT
  - a drift or news position on or after its `sell_date`
  - a position whose sale the owner approved on the phone (a holdings page with status `selling`), if the
    guard's sale after the open (below) did not already sell it
- **A sale the owner approved is made soon after the open** (`t212.run_approved`, from the guard's loop at
  or after `APPROVED_SELL_AT` 09:35 New York, or within one guard cycle when approved in market hours):
  `eod_close(..., approved_only=True)` cancels the stop, sells at market, and puts the stop back if the sale
  is refused. Tried once a day per stock (`approved_sale` in `t212-guard.json`); a failure alarms and the
  15:50 close tries again. Demo only, exits only. One phone line (`messages.approved_sale_sent`).
- **It keeps, and reports with a GUARD ALERT, never sells:** an unknown label, a holding with no
  readable sell date, a position with no buy record at all. (Before 16 Nov a drift record with no
  date, or an unknown label, was sold as a day trade.)
- The 1R trailing stop and half-sell-at-target are intraday only. Every holding keeps its fixed stop,
  the missing-stop check and the software-stop backup.
- Tests: `tests/test_drift.py`, `tests/test_holdings.py`.

## The holdings record and the daily match (from 16 Nov 2026)

- **`trader/state/holdings.json`:** one page per buy: `symbol`, `strategy`, `reason`, `wrong_if`, `event`,
  `sources`, `buy_date`, `buy_price`, `qty`, `stop`, `sell_rule` (`date`, `rule`), `status`, `result`,
  plus `broker`, `account` and `broker_ref` (the broker's own ticker). Every change is also added to
  `holdings-log.jsonl`. `holdings.py` has no broker code, so an Interactive Brokers version can reuse it.
- **Status:** `waiting` (not filled), `open`, `sell_asked`, `selling` (approved: the guard sells it after the next open),
  `closed` (with the result from the broker's own fills), `mismatch`.
- **The daily match** (`holdings_t212.daily_match`, from the autopilot before the MARKET OPEN message and
  after the closing report): it reads positions, pending orders and, when needed, the fill history. It
  makes pages for new buys, closes pages whose sale shows in the fills, and lists every disagreement:
  held with no page, a page with nothing held and no sale, share counts that differ, no stop, a part
  stop, a software stop, a stray stop, an unknown label, no sell date.
- **Report only.** It never places, cancels or changes an order, and never "fixes" a page. A new
  disagreement goes to the phone once (`messages.holdings_mismatch`); it is told again only if it changes.
- **A mismatch pauses NEW buys** (`holdings.pause_update`, `"holdings": {"pause_on_mismatch": true}`, on by
  default): while a disagreement of a kind in `holdings.PAUSE_KINDS` stands (everything except `soft_stop`),
  `holdings.json` carries `"pause"`. The ORB scan, the drift buys and the news buys all read it
  (`Pilot.hold_pause`) and buy nothing; each skip is logged with the reason. One message when it starts
  ("I've paused new buys"), one when the two match again. While paused the autopilot runs the match again at
  most every 10 minutes, so buying comes back by itself. Nothing is sold, cancelled or fixed. `t212.py place`
  by hand is not paused.
- **By hand:** `python3 trader-broker/holdings.py show [--all]`, `python3 trader-broker/holdings_t212.py match`.
- **Tests:** `tests/test_holdings.py`, `tests/test_review.py` (the pause).

## Risk caps by strategy, and the daily loss limit for day trades (16 Nov 2026)

- **`"risk_caps_pct": {"drift": 2, "news": 2, "intraday": 1}`** in the config: what each strategy's
  holdings (plus its buys waiting to fill) could lose at their stops together, as a share of the account.
  Each strategy answers only to its own cap, so one cannot use up the room of another. The three add up to
  5%: with this key set, the single `"max_total_risk_pct": 4` is **no longer what stops a buy** (it is used
  only when `risk_caps_pct` is missing).
- **The 3% daily loss limit is back, for day trades only** (`max_daily_loss_gbp`, counted the
  `daily_loss_count` way). A holding (drift, news) is never stopped by it; a day trade answers to it AND to
  intraday's cap. It stays until the ORB scanner is retired.
- **How risk is counted** (`holdings.open_risk`, with `"by_strategy"`): per position, shares x (price paid -
  stop) plus the FX fee, at the live stop order, else the guard's software stop, else the stop its buy
  planned. A position with no stop at all counts its **whole value**. A buy waiting to fill counts its
  planned loss. A position with no buy record counts as intraday; an unknown label counts under its own
  label (no cap covers it: the mismatch pause does).
- **Where it is enforced:**
  - trader-risk (`check.total_risk(c, day_risk, today, now, strategy)`): the autopilot writes `"total"`
    with `"by_strategy_usd"` into `trader/state/day-risk.json` (`day_risk.total`, read-only from Trading 212,
    read again before every check, never reused). Missing, stale or unreadable means the entry is
    **blocked** ("total risk is not known"), never passed. A strategy with no cap written is blocked too.
  - drift buying (`drift_buy.buy_today`: `risk_caps_pct.drift`, beside its own `drift.max_open_risk_pct`)
    and the news strategy (through trader-risk with `strategy="news"`).
- `max_trades_per_day` (5) and the 1% a trade still limit ORB.
- **Tests:** `tests/test_total_risk.py`.

## The daily review (from 16 Nov 2026): `trader-broker/review.py`, REPORT ONLY

- **When:** once a trading day, after the closing report and the holdings match (`Pilot.daily_review`).
- **What it checks, in order:** the account can be read; the record matches; every holding has a stop (a
  missing one is the first line); the money limits (cash at or over the 10% buffer, drift at or under its
  share, each strategy's risk at or under its cap); a rule sale due (sell date next session), overdue (past
  its sell date and still held) or approved by the owner; a reason that looks broken (the stock left the
  universe or went on the blocked list, or a news run's request is waiting); each holding's gain in pounds
  after fees, with the S&P 500 beside it for information.
- **Report only.** It never places, cancels or changes an order, never sends Approve/Reject and never edits
  a page (a test reads the source). A sale on the sell date is still made by the guard's 15:50 close, as
  before. "Would ask" is words only. A breach of a money limit sells nothing: the buy checks already refuse
  a buy that adds to it.
- **It shows each holding's verdict** from the day's news runs (`news_ideas.todays_verdicts`, "Today's
  read: keep / watch / sell" with the reason). The question for a news "sell" is sent by the news runs, not
  by the review.
- **Phone:** one message (`messages.daily_review`), only when something is held or something is wrong.
  "Couldn't check today" is sent only when the record says something should be held.
- **Files:** `trader/state/reviews/DATE.json`. **By hand:** `python3 trader-broker/review.py` (prints, sends nothing).
- **Tests:** `tests/test_review.py`.

## The evening strategy board (from 22 Nov 2026): `trader-broker/board.py`, REPORT ONLY

- **What:** one self-contained HTML page (inline CSS, no scripts, no outside links; light and dark mode on an
  iPhone), one card per strategy: drift ("Earnings trades") and news ("News ideas"); day trading is one
  "switched off" line while `autopilot.intraday_enabled` is false (a card of its own when it is on).
  - top: the date, account value (the review's), change today (`account-days.json`), cash and the cash kept
    aside (the review's `cash` limit, `cash_buffer_pct`), and the S&P 500's move today
  - each card: **today** in UK time (news runs, drift's earnings check, buys with the page's reason, names not
    bought grouped by reason, sales with the reason and the result in pounds, the close: what is held), or
    "Nothing happened today"; **holding now** (company, days held, sell date, gain after fees from the review,
    the page's reason); **progress so far** (completed trades since the first buy, made / lost money, total
    after fees, and the same money in the S&P 500 over the same days where recorded: drift's `trades.jsonl`;
    news trades have no S&P figure recorded, and the board says so)
  - green and red only on pound results. Company names and money from `messages.py`.
- **Data:** only what the agent already saved: `holdings.json`, `fills.json` (fill times), `news/runs.jsonl`,
  `ideas.jsonl`, `buys.jsonl`, `drift/buys.jsonl`, `trades.jsonl`, `shortlist.json`, `reviews/DATE.json`,
  `account-days.json`, and `market-day.json` (the closing report's S&P 500 move, saved by `report.send` since
  22 Nov when `report.market_line` is on). No broker call and no web call (a test reads the source).
- **When:** `Pilot.board`, at the end of `Pilot.daily_review` (after the review message and the fill checks),
  every US trading day. Saved to `trader/state/boards/DATE.html`; sent with `phone.Phone.send_document`
  (Telegram sendDocument, a multipart upload, standard library, the token scrubbed from errors) with the
  caption `messages.board_caption()`. A failure to build or send: one short message (`messages.board_failed`);
  it never stops the evening run, and the review has already gone. No switch: always on.
- **By hand:** `python3 trader-broker/board.py [DATE]` writes the file and prints its path, sending nothing;
  `--send` also sends it to the phone.
- **Tests:** `tests/test_board.py`.

## News ideas (from 16 Nov 2026; buying ON since 17 Nov): `trader-broker/news_ideas.py`

- **Switch:** `"news": {"buy_enabled": false}` is the code's default; the owner's config has it `true` since
  17 Nov 2026 (the owner's decision, set by Claude on the owner's written instruction for that one change).
  **Only the owner turns it on; Claude never does on its own.** Demo only: it refuses when `paper` is not true, and `t212.py place` refuses a news order on
  live, with buying off, or without a sell date after today.
- **Two runs each US trading day** (`run --kind main|update`; each `claude -p` uses the owner's Claude Code
  plan, no API key, **web search and web fetch only**, in an empty folder without the pack's keys):
  - **main, 06:00 UK** (launchd job `news`): the review of overnight news, results and filings. Its ideas go
    **into the 07:30 briefing** (`news_ideas.briefing_data`, `messages.news_briefing`), not a message of
    their own. Only when the briefing has already started do they get their own message.
  - **update, 08:30 New York** (launchd job `news-update`, started at 12:30 AND 13:30 UK; only the start at
    or after 08:30 New York does anything): a shorter look at what is new since 06:00 (results before the
    open included). It answers `checks` (keep or drop each morning idea, with a one-line reason), may add
    new ideas, and gives a verdict on every holding. **One short message only if something changed** (an
    idea dropped or added, or a holding's verdict turned to watch or sell; `messages.news_update`). An idea
    it does not mention is "unconfirmed".
- **Proof of web searches** (`read_stream`, `searched`, `check_sources`): the run uses `--output-format
  stream-json --verbose`, which lists every tool call. From it: how many WebSearch and WebFetch calls, the
  pages web fetch really got ("opened"), and addresses only seen in search results. (The envelope's own
  `server_tool_use` counters read 0 in Claude Code, so they are not used.)
  - A run with no search and no opened page, or with no record at all, **fails**: its answer is thrown
    away, and a failed update means no news buy that day.
  - An idea needs at least one source the run opened (the same page whatever its `www.`, trailing slash or
    `?tag`); a source neither opened nor seen is dropped. Each kept source carries `opened: true/false`.
  - The counts and the first opened pages are kept in `today.json` (`runs.KIND.web`) and `runs.jsonl`.
- **A missed run** (`news_ideas.timing`, `catch_up`, `Pilot.catch_up_news`): the always-on autopilot starts
  it when the Mac wakes (10 minutes of grace, once a day each). A run that can no longer finish in time is
  written down as `skipped`, never run late: main must finish before 08:30 New York, the update before
  `buy_at`. One run of each kind a day; a second start stands down (`begin`).
- **Never bought from a run that did not finish:** the buy step takes only ideas that the update
  FINISHED (not failed, skipped, still running, or finished after `buy_at`) and kept or added
  (`news_ideas.buyable`). No finished update means no news buy that day.
- **Each idea needs:** company, `why`, `event`, `event_date` (today or later), `wrong_if`, `sell_rule`,
  `sell_date` (after today, at most `max_hold_days` 120 away, moved back to a trading day), and at least one
  source with a URL and a date. Anything missing is rejected. Also logged: `moved_pct` with `moved_note`
  (how far the share has already moved on the news, Claude's estimate).
- **Two ratings (from 22 Nov 2026, the owner's decision):** `date_confidence` (with `date_why`: is the date
  confirmed?) and `edge_confidence` (with `edge_why` and `edge_source`: is the market underestimating the
  event?). Each is low, medium or high; anything else is "unknown". **Only the edge decides a buy:** it is
  stored as `confidence` (and `edge_why` as `confidence_why`), which everything else reads as before.
  - The prompt defines the levels: low = no specific, sourced reason the market is underestimating it;
    medium = ONE specific reason backed by a page the run opened ("the shares fell recently" or "the outcome
    is uncertain" alone are not enough); high = as medium, clear and from more than one opened source.
    "Most ideas should be low; medium should be uncommon, high rare." Market mood goes in `notes`, not the
    rating. A bare `confidence` from Claude is ignored (the edge is then "unknown").
  - **The date must be rated too:** an idea is bought (and counts toward the pretend book and the caps,
    `in_book`) only when its edge is at a `buy_confidence` level AND its `date_confidence` is medium or high
    (`DATE_LEVELS`, `rated_to_buy`). An idea held back by a low or missing date rating is logged in
    `buys.jsonl` as skipped with `"held_back": "date"`, and the phone line says "its date is not confirmed".
  - **By code:** medium or high without an `edge_why` or with an `edge_source` the run did not open counts
    as low; high with fewer than two opened sources counts as medium. Either is written in `edge_note`.
  - Before 22 Nov one rating mixed the two ("happens as dated AND not already in the price", "Be strict"):
    all 10 ideas of 18 to 20 Nov were low.
- **Where to look (from 22 Nov):** the main prompt asks for events that are large for the company's size,
  names two kinds of event to start with each day (`EVENT_KINDS`, `kinds_for`: all eight in four days),
  and lists the last 3 run days' searches (`recent_queries`, from `runs.jsonl`, up to 30) so they are not
  repeated word for word. The update run gets neither list.
- **Hold length (by code):** the sell date may be at most `max_days_after_event` (10) trading days after the
  event date, unless Claude gives a `long_hold_reason`.
- **Which confidence may be bought** is the setting `buy_confidence`: one level (`"high"`, the code's
  default) or a list. The owner's config has `["medium", "high"]` since 17 Nov ("for now"); `"high"` goes
  back to high only. Every other passed idea stays logged with a virtual result, so the levels can be
  compared. Only ideas that would be bought count toward the pretend book, the caps and `max_positions`.
  `settings()` gives `buy_levels` (a tuple) and `buy_confidence` (words, e.g. "medium and high").
- **Checks by code:** the universe, `blocked_tickers`, a stop (`atr_mult` 3 x the 14-day ATR under the
  price, a setting, untested), size (`t212.size_entry`: 1% max loss, `max_position_pct`, and the room
  left after drift and everything held, at least `min_buy_gbp` 100), trader-risk with `strategy="news"`,
  and news's own risk cap. With the account unreadable no idea passes.
- **At buy time** (`news_buy_step`, `buy_at` 09:35 New York, only with buying on): each idea is checked
  again at a live quote. Skipped, logged with the reason in `buys.jsonl`: moved more than `max_move_pct` (5)
  either way from the price when proposed (the last close); at or under its stop; held or waiting; paused
  (/pause or a holdings mismatch); the cash check; trader-risk. Then `t212.cmd_place` with
  `strategy="news"`, the sell date, the idea id and a real stop. Never twice.
- **Virtual results** (`resolve`, at the start of each main run): bought at the open of its day (not
  bought when it opens at or under its stop, or more than `max_move_pct` from the proposed price), sold at
  its stop (a gap fills at the open) or at the close on its sell date, in pounds after 0.15% FX + 0.05%
  slippage each way, from final daily closes only. SPY over the same days is logged for information.
- **The Friday report** has a news section: virtual results by confidence level
  (`news_ideas.by_confidence`, `messages.friday_news`).
- **A failed Claude run:** logged; main: said in the briefing (or its own message if the briefing has
  gone); update: one message. The plan's usage limit is recognised and said plainly. Nothing is bought.
- **What a run uses:** `runs.jsonl` and `today.json` keep each run's seconds, turns and tokens
  (`news_ideas.run_meta`); `news_ideas.py show` prints them.
- **A verdict on every holding** (`take_verdicts`, both runs): each open news AND drift page gets `keep`,
  `watch` or `sell` with a one-line reason, a confidence and dated sources (checked like an idea's). Logged
  in `trader/state/news/verdicts.jsonl`; today's are in `today.json` (`verdicts`). A holding the run did not
  mention is logged `none`; a `sell` without a reason and an opened source counts as `watch`. Its
  `confidence` uses the idea edge's scale (from 22 Nov): medium needs a reason and an opened page, high two
  opened pages, or it is lowered by code.
  - **News holding, `sell`:** a request in `trader/state/news/asks.json`; the autopilot sends it with
    Approve and Reject (`sa:`/`sr:` buttons). Approve sets the page to `selling` and the guard sells it soon
    after the next US open (see "Strategy labels"). Reject keeps it. No answer keeps it: the next day's run
    closes the old request (`no_answer`, old buttons answer "expired") and asks again if it still says sell.
    One request a holding a day. The stop stays until the sale itself. The autopilot never sells.
  - **Drift holding:** the verdict is written down and shown, never acted on. `resolve_verdicts` (each main
    run) fills in, for a finished holding with a `sell` verdict that was not acted on, what selling at the
    next day's open would have made against what the rule made; the Friday report shows both
    (`verdict_tally`, `messages.friday_verdicts`).
  - Shown in the 07:30 briefing ("My read on what I'm holding") and in the daily review.
- **Hot topics** (main run only, `clean_topics`): 3 to 5 plain sentences on what the US market is talking
  about, each with a dated source the run saw. In the briefing under "What the market is talking about"
  (`messages.hot_topics`), whether or not they led to an idea.
- **Files** (`trader/state/news/`): `ideas.jsonl` (every idea, with `from`, `state`: proposed, kept,
  dropped, unconfirmed, new), `today.json` (with `runs.main` and `runs.update`, `verdicts`, `topics`),
  `runs.jsonl`, `buys.jsonl`, `verdicts.jsonl`, `asks.json`.
- **Settings** (in `news`): `buy_enabled`, `enabled`, `buy_at`, `main_at` (06:00 UK), `update_at` (08:30 New
  York), `timeout_min` (25), `update_timeout_min` (15), `model`, `update_model`, `max_ideas` (5 a run),
  `max_positions` (5), `atr_mult` (3), `atr_days` (14), `max_hold_days` (120), `max_days_after_event` (10),
  `max_move_pct` (5), `buy_confidence` (high, or a list), `min_buy_gbp` (100), `kronos_minutes` (10).
- **Honest limits:** an idea is a model's reading of public web pages. It is untested, pages can be
  wrong or try to mislead, confidence is the model's own word, and nothing in `research/` has beaten
  holding an S&P 500 tracker.
- **By hand:** `python3 trader-broker/news_ideas.py show|resolve|selftest`; `run --kind main|update --force`
  asks Claude again at any hour (a forced update after `buy_at` is never bought on).
- **Tests:** `tests/test_news_ideas.py`, `tests/test_news_two_runs.py`, `tests/test_news_verdicts.py` (search
  proof, the confidence setting, verdicts, the approval step, the guard's sale, topics). `tests/test_news.py`
  is the older trader-news tool. A fake Claude answer in a test must carry a search record
  (`test_news_ideas.searched`).

## Kronos opinions and the stop shadow (from 22 Nov 2026): `trader-broker/shadow.py`, REVIEW ONLY

- **Why:** the Kronos backtest (`research/README.md`, 18 Nov) found no skill, so Kronos stays shadow only and is
  judged on the agent's own trades at the review checkpoint. Before 22 Nov no drift buy asked Kronos, and every
  news idea's Kronos verdict failed ("no time left before 15:50": the day-trade forecaster cannot look past a close).
- **Kronos's opinion** (`page["kronos"]`): for each drift and news page (not waiting, not intraday), the median of
  5 Kronos-small paths of the change from the last close BEFORE the buy day to the close on the sell date (daily
  bars, 256 of them; `trader-kronos/kronos_daily.py`, the backtest's forecast). No hindsight: bars from the buy day
  on are never given. Once per page; a failure is tried 3 times, then left (`{"error", "tries"}`). Kronos's
  forecasts lean negative (median -2.3% in the backtest), so the review compares halves, not up against down.
  News ideas get the same forecast at the 06:00 run (`news_ideas.kronos_for`, idea row `kronos`, verdict up/down).
- **The stop shadow** (`page["stop_shadow"]`): for a drift or news page closed by its stop (`result.why` "stop", or
  the guard's software-stop sale in `t212-guard.json` "sold"): stop exit price and date, planned sell date, and,
  once that date's close is final, the price then, the difference per share, in % and in pounds (shares sold, at
  `trader/fx.json`'s rate, before fees). Positive: the stop cost money.
- **When:** the autopilot starts `shadow.py run` after the daily review, as a separate program, not waited for
  (log `trader/logs/shadow.log`). Only when `run-autopilot.sh` sets `AUTOPILOT_SHADOW=1`, so a shell or a test
  starts nothing. It changes no order, stop, sell date or status, and blocks nothing (a test reads the source).
- **Timing on the old 2017 MacBook (22 Nov; the Mac mini should be faster, not yet re-measured):** model load 27 s cold (about 4 s warm), then about 14 s a share
  (5 paths x 20 days). A run of 5 holdings: about 1.5 minutes, once a day after the close, in its own program.
  The 06:00 news run: about 45 s an idea (the model is loaded per idea), inside `kronos_minutes` (10).
  (The dormant day-trade verdict took about 5 minutes each.)
- **The sell-rule replay and the chart labels** (from 22 Nov): `page["replay"]` and `page["chart"]`, once a page,
  from one daily-bar fetch, in the same evening run (see "Reworked tools for holds").
- **By hand:** `python3 trader-broker/shadow.py show|run|time SYMBOL`.
- **Tests:** `tests/test_shadow.py`.

## Review checkpoint (written 22 Nov 2026; the owner's rules)

At **20 or more completed drift + news trades** (`python3 research/checkpoint_review.py` prints all three; it
works with fewer and says when there are too few):
- **Kronos:** compare the trades Kronos rated highly (the top half of its forecasts) with those it rated poorly.
  If the highly rated ones have not clearly done better (the script's bar: at least 1 percentage point more a
  trade after fees), **remove Kronos from the agent**.
- **Stops:** from the stop shadows, did the stops help or hurt on average? Recommend whether to widen them
  (the script: over +1% on average means they cost money; under -1% means they saved money).
- **Benchmark:** each strategy against zero AND against holding the S&P 500 over the same days.
- Drift trades are likely to be rare until the next earnings season (mid to late January), so most early
  evidence will come from news trades.
Changing a stop or removing Kronos is the owner's decision after the checkpoint, never Claude's on its own.

## Market holidays and 1:00 p.m. early closes (from 21 Nov 2026)

- **Full NYSE holidays** (the list in `trader-alerts/alerts.py`, e.g. 26 Nov, 25 Dec 2026, 1 Jan 2027): the
  guard stops at once ("US market holiday"), with no read, no stop check, no approved sale and no end-of-day
  sale; the autopilot's day is "closed" (no buys, no report); the briefing and news runs skip the day, as before.
- **Early closes** (`NYSE_EARLY_CLOSE`, 27 Nov and 24 Dec 2026): `t212.market_close(d)` gives 13:00 and
  `t212.eod_time(at, d)` moves the end-of-day sale to `EARLY_EOD_BEFORE` (10) minutes before it, 12:50. The
  guard stops at the close (13:00, not 16:00); the autopilot's evening report (12:55), its "sold at the close"
  timing (`at_close`) and the close time in messages (`close_uk`) follow the same time.
- **Tests:** `tests/test_market_days.py`.

## The outside heartbeat (from 21 Nov 2026): `trader-broker/heartbeat.py`

- **Why:** Telegram cannot say the Mac is down, because the Mac sends it. The owner's free healthchecks.io
  check expects a ping every 5 minutes and messages the owner when they stop (Mac off, asleep, no internet,
  or the autopilot stuck or not starting).
- **How:** `export HEALTHCHECK_URL=https://hc-ping.com/...` in `~/.zshrc`. `run-autopilot.sh` hands it to the
  autopilot as `AUTOPILOT_HEARTBEAT_URL` and unsets the original, so a terminal or a test run never pings;
  `Pilot.beat` (at most every 5 minutes) runs at the start of every pass of the always-on loop, of the
  wait for the open and of the market-hours loop, and in `Pilot.listen`: 24/7, and a step that keeps failing
  (Trading 212 down) does not stop it. It runs in the main loop (no thread), so a frozen autopilot stops it.
  No address: nothing happens.
- The address is private: never printed or logged. A failed ping is logged once until one gets through, and
  never stops anything. By hand: `python3 trader-broker/heartbeat.py ping`.
- **Tests:** `tests/test_heartbeat.py`.

## The wallpaper status (from 22 Nov 2026): `trader-broker/wallpaper.py`

- **What:** the owner's desktop wallpaper shows the agent's status. The always-on autopilot sends an HTTPS
  POST (`project: trading`, merged with what is stored) every 15 minutes (`Pilot.wallpaper`, from `Pilot.beat`),
  at once after each buy or sale `watch_positions` sees (`last_trade`, UK time, e.g. "15:42 BUY NVDA"), and
  `status: stopped` when `serve` ends (SIGTERM from launchd becomes a normal exit, `stop_on_sigterm`) or
  starts not ready. Fields: status, mode (Practice on demo), value, day change since `start_value`, cash
  share (`availableToTrade` + `reservedForOrders`), positions, a 30-value trend (`trader/state/wallpaper.json`),
  `next` (up to 3 items: `"wallpaper": {"next": [...]}` in the config, else `wallpaper.NEXT`).
- **Keys:** `WALLPAPER_URL` and `WALLPAPER_WRITE_KEY` in `trader/.env` (mode 600; `trader/` is git-ignored).
  `run-autopilot.sh` reads only those two lines (never runs the file) and hands them over as
  `AUTOPILOT_WALLPAPER_URL`/`_KEY`, so a shell or a test never sends. The key is never printed or logged;
  the pre-push hook also refuses `WALLPAPER_WRITE_KEY=` lines and its value.
- **Never slows trading:** the two account reads happen in the loop, the POST in a background thread (8 s
  timeout, one at a time; a send while one is in flight is dropped). The shutdown send waits at most 4 s.
  A failure is logged once until one gets through. By hand: `python3 trader-broker/wallpaper.py show`.
- **Tests:** `tests/test_wallpaper.py`.

## The S&P 500 in reports (from 16 Nov 2026)

- Shown for information only, never a pass or fail test. The Friday report's ON TRACK now means a
  positive average per real trade after costs (`evidence.judge`); drift's Friday lines the same.

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

## Drift buying (stages 3 and 4: built 12 Nov 2026, ON since that evening): `trader-broker/drift_buy.py`

- **Switch:** `"drift": {"buy_enabled": false}` is the code's default; the owner's config has it `true`
  (stage 4, the owner's decision, confirmed 16 Nov). Off means no buys, no reservations, no
  logs: nothing changes. Demo only: `drift_buy.active` needs `paper` true and no `live_enabled`, and
  `t212.py place` refuses a drift order on live, with buying off, or without a sell date after today.
  Claude never changes this switch.
- **When:** `buy_at` 09:33 New York, once a day, before the first intraday scan (09:45). A start after
  09:45 logs every name "missed" and sends an alert: never bought late. /pause stops drift buys too.
- **Skips** (logged on the signal row as `buy` and in `state/drift/buys.jsonl`): blocked; held or
  waiting under either strategy; no free slot; the drift pool used; no cash reservation; no free cash;
  at or under its stop at the open (yesterday's close minus `atr_mult` x ATR); the open-risk cap.
- **The buy:** `t212.cmd_place` with `strategy="drift"`, `sell_date` (`drift_sell_date(day, 19)`: the
  20th trading day held), its own `state/drift/orders.json` and `risk.json` (drift's checks, not
  trader-risk), and the cash check against reservations for other stocks. Always a real broker stop
  (good till cancelled), whatever `stop_mode` or `fractional_stops` say. A stop that is refused or not
  placed raises a GUARD ALERT; the guard puts it back or holds it in software.
- **Stop:** `atr_mult` (3) x the 14-day daily ATR under the entry. **Not from the tested research,
  which held with no stop.** It is a setting.
- **Size:** the smaller of one slot and `max_loss_per_trade_gbp` (1%) at the stop. Slot = the drift
  pool / `slots`; drift pool = `max_share_pct` (50) % of the investable cash (the account less the 10%
  buffer). With 10 slots one slot is about 4.5% of the account.
- **Open-risk cap:** `max_open_risk_pct` (4) % of the account: the most all drift positions can lose
  at their stops together (gaps can lose more). The cap on total risk across ALL holdings
  (`max_total_risk_pct`) is checked as well.
- **Cash reservations** (`allocation.json`, reservations with `"pool": "drift"`, 09:30 to 10:15 NY,
  released when the buy step ends): the 07:30 briefing reserves drift's slots FIRST and intraday's
  reservable share comes out of what drift does not use (`allocation.make_plan(..., drift=,
  drift_used_gbp=)`). With no morning plan, or one without them, the buy step adds them; if it cannot
  (or allocation is off) it buys nothing that day and says why. `allocation.reservation_for` ignores
  drift reservations; `reserved_for_others` counts them.
- **Kept apart from intraday:** drift buys are left out of trader-risk's trades a day, daily loss and
  day trades (`check.is_drift`), of `day_risk.entries_today`, and of the autopilot's
  `max_open_positions` count; the closing report and the Friday intraday figures leave drift trades out.
- **Selling:** stage 1's 15:50 close on or after the sell date; an overdue one raises "DRIFT SELL
  OVERDUE". After the closing report, `drift_buy.closed_since` sends DRIFT SELL (reason, result after
  costs from Trading 212's fills, days held, SPY over the same days) once per buy and logs
  `state/drift/trades.jsonl`. Turning buying off stops new buys only.
- **Phone:** DRIFT BUY; DRIFT SELL; drift holdings ("day X of 20", gain, stop, sell date) in /status,
  MARKET OPEN, the briefing and the closing report; "NOT PROTECTED OVERNIGHT" when no stop order
  covers one. The briefing no longer calls drift positions "held overnight". The Friday report has a
  drift section: its own pass rule, real and shadow apart, `min_trades` (20).
- **Settings** (in `drift`): `buy_enabled`, `buy_at`, `slots` (10), `max_share_pct` (50),
  `max_open_risk_pct` (4), `atr_mult` (3), `atr_days` (14), `hold_days` (20), `min_trades` (20).
- **Tests:** `tests/test_drift_buy.py`.

## Real exit and the daily loss count (from 11 Nov 2026) (dormant: day trading)

All of this serves the ORB scanner. Kept for when the scan is switched back on or removed.

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

Drift still uses the morning reservations (its slots come first). The intraday reservations, the
per-trade cap and the Time Machine confidence bands are **dormant: day trading**.

- **Demo only:** `allocation.active(cfg)` is false unless `paper` is true (and no `live_enabled`).
- **Per-trade cap:** `max_position_pct` 22 (was 75) of `budget_gbp`, so 4 trades fit inside the 90%
  investable. trader-budget applies it; `t212.size_entry` applies it again (1% room for FX moves).
  With stops about 1% away the cap binds on most trades: about GBP 15 at the stop, not GBP 49.
- **`max_open_positions`** 4: held positions plus buys waiting to fill. A signal over it is still
  built and logged (`skipped: 4 positions open...`), so it gets its virtual result.
- **No day-trade reservations while day trading is off** (21 Nov 2026): the briefing passes no candidates to
  `make_plan` unless `t212.intraday_on`, so only drift's slots are reserved.
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

## Phone messages (from 12 Nov 2026): `trader-broker/messages.py`

- **All phone wording lives in `messages.py`.** New message types go there too, in the same style:
  plain English for a non-trader, emojis sparingly, money in GBP first, share prices in USD, company name
  with the ticker in brackets (names from `trader/universe/tradable.json`; `messages.use(state_dir)`).
  Never on the phone: R values, "Time Machine", "EVIDENCE MODE", "SHADOW", "end-of-day close", "at market",
  order or trace ids, `_US_EQ`, raw API errors. The past-trades verdict (`past_trades`) turns the Time
  Machine's average R into words before fees and pounds after fees (avg R after costs x the max loss).
- **Telegram HTML:** `phone.Phone.send` uses `parse_mode` HTML; every outside string goes through `esc()`
  (or `company()`/`tidy()`). If Telegram rejects the formatting it resends `messages.plain()`. Over 3,900
  characters it splits between paragraphs, buttons on the last piece.
- **Logs and reports keep the old full wording.** `Pilot.event(log_text, phone=...)` logs one and sends the
  other. `report.short_text`, `briefing.short_text`, `t212.render_eod` and the drift
  `buy_text`/`sell_text` are the log and CLI versions.
- **Fewer messages:** one buy message once the buy has filled (`Pilot.announce`, then `fill_message`;
  state `announce` in autopilot.json). A buy placed before it filled (stop due, entry not filled) sends
  nothing then; the fill message says the safety net is being set and the guard's "Safety net is now in"
  follows. One message per sale during the day (`sale_message`, result from Trading 212's fills); sales
  at the 15:50 close go in the evening report only, and the guard no longer sends its END-OF-DAY list
  (failures still alarm). A raised trailing stop is told once a day per stock; a sale at a software stop
  is told by the autopilot's sale message.
- **Market line:** `"report": {"market_line": true}` (set 12 Nov) adds how SPY moved today (Yahoo daily
  bars, the Friday report's source) to the evening report. Off when the key is missing (as in the tests).
- **Tests:** `tests/test_messages.py` (wording, escaping, fallback, splitting).

## Scheduling (launchd, `~/Library/LaunchAgents/com.daytraderskill.*.plist`)

| Job | When | What |
|---|---|---|
| `caffeinate` | always (KeepAlive) | `caffeinate -s`: no sleep on mains power (the Mac mini has no lid or battery) |
| `autopilot` | always (RunAtLoad + KeepAlive) | `run-autopilot.sh` runs `autopilot.py serve`: answers /status /pause /resume at any hour, delivers the outbox, catches up a missed briefing, rebuilds the universe each Sunday 18:00 UK (or next chance outside market hours), sets the budget before the open, starts a news run the Mac slept through, sends MARKET OPEN, (no ORB scan while `intraday_enabled` is false; otherwise every 15 min 09:45 to 15:30 New York), runs the drift buys at 09:33 (only with `drift.buy_enabled`), the news buys at 09:35 (only with `news.buy_enabled`), the holdings match before the open message and after the closing report (and every 10 min while buys are paused), sends "sell it?" requests at any hour, the drift shadow scan at 15:35, sends the closing report about 15:55 (then fills in drift results, sends DRIFT SELL messages, the daily review and the strategy board) and the evidence report on Fridays |
| `t212-guard` | weekdays 13:00 UK | `run-guard.sh`: `t212.py budget --apply`, then `guard --watch` from the 09:30 open to 16:00 New York |
| `briefing` | weekdays 07:30 UK | `run-briefing.sh`: the briefing; skips US holidays; retries its send for 30 min |
| `news` | weekdays 06:00 UK | `run-news.sh run --kind main`: the main Claude review, the checks, the log; its ideas go into the 07:30 briefing (log: `trader/logs/news.log`). Never an order |
| `news-update` | weekdays 12:30 and 13:30 UK | `run-news.sh run --kind update`: the update at 08:30 New York (the start before 08:30 New York does nothing). One short message only if something changed. Never an order |

The guard (`t212-guard`) also sells, from 09:35 New York, any holding whose sale the owner approved on the phone.

- **Keys:** the scripts read only the `export T212_API_KEY/T212_API_SECRET/T212_ENV/TELEGRAM_BOT_TOKEN`
  lines from `~/.zshrc` (`run-autopilot.sh` also `HEALTHCHECK_URL`), and use `/usr/bin/python3`. `~/.zshrc` is
  mode 600 (owner only) since 21 Nov 2026.
- **The python.org 3.11** at `/usr/local/bin/python3` lacks certificates: HTTPS fails from it. Use
  `/usr/bin/python3` for live calls.
- **Rate limits:** `t212.Client` keeps `trader/state/t212-ratelimit.json` (when each endpoint was last called,
  plus the `x-ratelimit-*` headers). The guard and the autopilot share it; a failed save is logged, never
  raised (`tests/test_ratelimit_note.py`, 17 Nov).
- **Restart a job:** `launchctl kickstart -k gui/$(id -u)/com.daytraderskill.<job>`

## Current config (`trader/config.json`, git-ignored; backups in `trader/backups/config/`, newest 5, older in `trader/archive/`)

The ORB keys below (`max_trades_per_day`, `min_reward_risk`, entry times, trailing stop, `allocation`,
`evidence.confidence_bands`) only matter for day trading, which is off (`autopilot.intraday_enabled` false).

- **Account:** GBP, Trading 212 **demo** (`"paper": true`), fractional shares, `stop_mode` broker,
  trailing stop 1R (from 1R profit), `eod_close` 15:50.
- **`account_budget` on:**
  - `budget_gbp` = the account value with `cash_buffer_pct` 10, so 90% can be invested
  - max loss 1% a trade
  - `risk_caps_pct` since 16 Nov: drift 2, news 2, intraday 1 (% of the account at risk at the stops)
  - the 3% daily loss limit (`max_daily_loss_gbp`, written each morning) covers day trades only
  - `max_total_risk_pct` 4 is still in the file; it is not checked while `risk_caps_pct` is set
  - `holdings.pause_on_mismatch` true
  - no `max_budget_gbp` cap
- **Allocation (12 Nov):** `max_position_pct` 22, `allocation` on (4 positions, 50% free pool, 2
  reservations, 90 min), `evidence.confidence_bands` [0.0, 0.10].
- **Trading rules (ORB only, dormant):** `max_trades_per_day` 5, `min_reward_risk` 1.5, entries 09:45 to 15:30 New York.
- **Lists:** `allowed_tickers` holds 100 liquid large caps and ETFs (US-listed ETFs are mostly not
  buyable from a UK account); `blocked_tickers` is empty and always wins.
- **`universe`** (`trader-scanner/universe.py`): `enabled` true since 10 Nov (502 S&P 500 stocks). On demo with `enabled` true, the filtered list in
  `trader/universe/tradable.json` replaces `allowed_tickers` at read time (the config file is never
  changed). `allowed_tickers` is the fallback (missing, broken or over 14 days old) and is what live
  always uses. A refresh that looks broken keeps last week's list. Changes: `trader/universe/changes.jsonl`.
- **Costs** (GBP account): 0.15% FX fee + 0.05% slippage on every buy and sell. Trading 212's API
  trades only in the primary currency, so a USD balance cannot avoid the fee for API orders.
- **`autopilot`:** `auto_buy: true`, `evidence_mode: true`, `intraday_enabled: false` (day trading off since the evening of 17 Nov).
- **`drift`:** `scan_enabled: true` since 10 Nov; `buy_enabled: true` since 12 Nov (the owner's decision).
  No drift buy yet: every shortlist so far was empty.
- **`hold_watch`, `earnings_watch`, `fill_checks`:** `{"enabled": true}` since 22 Nov 2026 (report only;
  backups `config.json.bak-20261122-before-hold-watch`, `-before-earnings-watch`, `-before-fill-checks`).
- **`news`:** `buy_enabled: true` and `buy_confidence: ["medium", "high"]` since 17 Nov 2026 (the owner's
  decision; backup `trader/backups/config/config.json.bak-*` from that day). Money: 10% cash always, drift up to 50% of the investable
  cash, news uses what drift and the other holdings leave.

## Evidence mode: the forward test (from 9 Nov 2026, no end date) (dormant: day trading)

This is the ORB forward test. With `intraday_enabled` false no new intraday signals are logged; the Friday
report still runs and carries the drift and news sections. Old rows and results stay.

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
- **Never turn on a buying switch:** `news.buy_enabled` and `drift.buy_enabled` are the owner's to set
  (change one only when the owner asks for that exact change in writing, as on 17 Nov for news).
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
| `trader/logs/` | `guard.log`, `autopilot.log`, `briefing.log`, `news.log`, `universe.log`, `guard-alerts.jsonl`, `outages.jsonl`, `t212-orders-DATE.jsonl` |
| `trader/state/` | `holdwatch/` (filings and headlines on holdings), `earnings-watch.json`, `fills.json` and `slippage.jsonl` (every fill), `entry-shadow.json`, `holdings.json` and `holdings-log.jsonl` (the holdings record, and the pause on new buys), `reviews/` (each day's review), `news/` (ideas, today's runs, buys, sell requests), `broker.json` (placements), `t212-guard.json` (stops, software stops, trail, end-of-day), `autopilot.json`, `risk.json`, `account-days.json`, `phone.json`, `phone-outbox.json`, `briefing.json`, `evidence/` |
| `trader/briefings/`, `trader/reports/` | each day's briefing; closing and evidence reports |
| `trader/universe/` | `tradable.json` (the universe), `changes.jsonl` (each refresh), `refresh.json` (last start) |
| `research/` | scripts and results summary; data in `trader/research-data/` or `$RESEARCH_DATA` |
| `trader-broker/PHONE.md`, `GO_LIVE.md`, `SKILL.md` | phone setup and messages, the go-live checklist, the broker skill |
