# How the demo trading agent works

A plain-English walk through the Trading 212 demo autopilot, from the market data it reads to the
order it sends and how that order is closed. Every number here is the setting in use from 11 Nov 2026,
taken from the code and `trader/config.json`. Where a number is a setting, the setting name is given
in `code` so it can be checked or changed.

This is education and tooling, not financial advice. The tickers the agent trades are the output of a
mechanical rule, not recommendations. It runs on a **demo** account only. Going live is the owner's
decision and is never done by the agent.

**The honest headline first.** The research in `research/README.md` found that this rule (the 15-minute
opening-range breakout) is about break-even before costs and loses about 0.65R a trade after Trading
212's costs. Nothing tested beat simply holding an S&P 500 tracker. The demo runs as an open-ended
forward test ("evidence mode") to measure that with real fills, not because the rule is expected to win.

---

## 1. The big picture

Once every 15 minutes during the US session, the agent runs one pipeline. Each stage can stop a
candidate, and a stop is logged with its reason:

```
universe  ->  scanner  ->  entry check  ->  exit plan  ->  Time Machine  ->  sizing  ->  risk gate
                                                                                          |
          guard (stops, trailing, 15:50 close)  <-  Trading 212 market buy + stop  <-  placing checks
```

| Stage | Code | Question it answers |
|---|---|---|
| Universe | `trader-scanner/universe.py` | Which stocks may be looked at at all? |
| Scanner | `trader-scanner/scan.py orb` | Which of them closed a 5-minute bar above their opening range? |
| Entry check | `trader-entry/entry.py` | Is the breakout real, inside the entry window, and what was the trigger price? |
| Exit plan | `trader-exit/exit.py` | Where are the stop and the target? |
| Time Machine | `trader-timemachine/timemachine.py` | On 10,000 simulated sessions, exited the way real trades are, do the wins outweigh the losses? |
| Sizing | `trader-budget/size.py` | How many shares, given the budget, cash buffer, position cap and max loss? |
| Risk gate | `trader-risk/check.py` | Does the order break any rule: daily loss, trade count, lists, reward:risk after costs, time window? |
| Placing | `trader-broker/autopilot.py`, `t212.py place` | Is the price still close enough to the plan, and does it still pass? Then buy, then set the stop. |
| Guard | `t212.py guard` | Is every position protected? Trail the stop; close the day at 15:50. |

The agent is long only. It never shorts, never uses margin, never trades options, and only buys
US-dollar stocks.

---

## 2. The daily timetable

All market times are New York time; the Mac runs on UK time. Jobs are launchd agents (see `CLAUDE.md`).

| When | What happens |
|---|---|
| Sunday 18:00 UK | The universe is rebuilt (or at the next chance outside market hours). |
| 07:30 UK, weekdays | Morning briefing to the phone (skipped on US holidays). |
| Before the open | The budget and loss limits are reset from the account value (section 4). |
| 13:00 UK | The guard job starts and waits for the 09:30 open. |
| 09:30 | Market opens. The opening range (09:30 to 09:45) forms. MARKET OPEN message. |
| 09:33 | Drift buys (only with `drift.buy_enabled` true; OFF by default): today's drift shortlist, before the intraday window (section 12). |
| 09:45 to 15:30 | The pipeline runs every 15 minutes (`autopilot.every_minutes`). No entries outside this window (`no_entries_before`, `no_entries_after`). |
| Every 5 minutes, 09:30 to 16:00 | The guard checks every position and its stop. |
| 15:35 | The post-earnings drift **shadow** scan (no orders, section 12). |
| 15:50 | End-of-day close: intraday positions, and drift positions on or after their sell date, are sold at market (`eod_close.at`). |
| About 15:55 | Closing report from Trading 212's own fills; virtual results filled in; a DRIFT SELL message for each drift position sold. |
| Fridays | Evidence report: real and virtual results, slippage, against the S&P 500 tracker. |

On a 1:00 p.m. early-close day the entry cutoff, flat-by time and simulations move earlier to match.
The holiday calendar covers 2026 and 2027 only.

---

## 3. Data in

| Data | Source | Used for |
|---|---|---|
| 5-minute price bars | Yahoo Finance (unofficial, free) | Scanner, entry check, Time Machine (60 days of history), virtual results |
| Latest quote | Yahoo, via `trader-alerts/alerts.py quote` | Sizing, the chase check at placing time |
| Daily bars | Yahoo | Universe liquidity filter, drift scan |
| S&P 500 members | Public list | The universe pool |
| Instruments, account value, cash, positions, pending orders, fills | Trading 212 API (demo) | What is buyable, the budget, duplicate checks, the guard, reports |
| GBP/USD rate | Yahoo (`GBPUSD=X`) | Converting pound limits into dollars; refreshed when over 6 hours old |
| Earnings calendar | Nasdaq | Drift shadow scan only |

There is no free bid/ask spread data, so liquidity (dollar volume traded) stands in for spread.

---

## 4. Budget and loss limits, set each morning

`trader-broker/account_budget.py` reads the account's total value from Trading 212 every trading
morning and writes these into the config (`account_budget`):

| Setting | Rule |
|---|---|
| `budget_gbp` | The whole account value |
| `cash_buffer_pct` | 10%: always kept in cash, so at most 90% can be invested (`budget_pct` 90) |
| `max_loss_per_trade_gbp` | 1% of the account value (`max_loss_pct`) |
| `max_daily_loss_gbp` | 3% of the account value at the start of the day (`daily_loss_pct`) |
| `max_budget_gbp` | Not set: no cap on the amount invested |

Every pound limit is converted into dollars at the current GBP/USD rate before the dollar-priced
checks run (rounded down, so the dollar limit is never looser than the pound one). The rate must be
under 12 hours old for sizing and the risk check.

---

## 5. Step 1: the universe (which stocks may be looked at)

Built weekly by `trader-scanner/universe.py`:

1. Start from the **S&P 500** (about 502 stocks).
2. Keep only those **on Trading 212** as US-dollar instruments.
3. Drop any whose last close is **under $5** (`min_price`).
4. Drop any that traded **under $20 million a day** on average over the last 20 sessions
   (`min_avg_traded_usd`, `avg_days`); at least 15 of the 20 sessions must have data.
5. Remove anything on `blocked_tickers` (empty today). The block list always wins.

Safety nets for the list itself:

- A refresh that looks broken is **not saved** and last week's list is kept: under 100 stocks
  (`min_count`), more than 25% fewer than last week (`max_drop_pct`), or more than 10% of Yahoo
  fetches failing. A failed refresh is tried again after 6 hours.
- If the universe file is missing, broken or **over 14 days old** (`max_age_days`), the hand-picked
  `allowed_tickers` list (100 liquid large caps) is used instead.
- A live account would always use `allowed_tickers`; the universe applies on demo only.
- Every refresh is logged to `trader/universe/changes.jsonl`.

Within a day, a stock that was already tried (passed or failed) or is already held is **not scanned
again**, so it can make at most one trade a day.

---

## 6. Step 2: the scanner (opening-range breakouts)

`scan.py orb` looks at the session so far in 5-minute bars:

- **The opening range** is the first 15 minutes, 09:30 to 09:45 (three 5-minute bars). Its high and
  low are the highest high and lowest low of those bars.
- **Stage 1 (cheap, closes only):** a stock is a candidate if any completed bar after the range
  closed at or above the highest *close* of the range. This is a quick pre-filter fetched 20
  symbols per request.
- **Stage 2 (real highs and lows):** for up to **200 candidates, most traded first** (price times
  volume so far), a full chart is fetched. A hit is the **first completed bar to close above the
  range high**. Candidates beyond the top 200 are counted as "unconfirmed" and skipped that cycle.
- A bar only counts once it has fully closed. Every 5-minute slot from 09:30 to now must be present,
  or the stock is skipped as unusable (missing data is never guessed).
- The scan refuses to run before the first bar after the range has closed (09:50) or after the
  last entry time (15:30). It also drops anything under $5.

The output is a watchlist, not a buy list.

---

## 7. Step 3: the entry check

For each scanner hit, `entry.py` re-checks the same stock on its own:

- The breakout level is the **opening-range high**.
- The trigger is the **first** 5-minute bar after the range that **closes above** that level and that
  ends between **09:45 and 15:30** (`no_entries_before`, `no_entries_after`).
- The **entry price** in the plan is that trigger bar's close.
- There is no volume filter in the autopilot's use (the `--min-volume-x` option is not set).
- Statuses: `triggered` goes on; `waiting`, `expired` or `no-data` stops here.
- Only buys. A short or sell entry is refused outright.

Because the pipeline runs every 15 minutes, the trigger bar can be up to about 15 minutes old when
the order goes in. Step 9's chase check protects against buying too far above it.

---

## 8. Step 4: the exit plan (stop and target)

`exit.py plan` sets the exits **before** any order:

| Item | Rule (default for this setup) |
|---|---|
| Stop | **Opening-range midpoint**: (range high + range low) / 2, rounded down to the cent |
| Risk per share (1R) | Entry minus stop |
| Target | **2R**: entry plus twice the risk |
| Flat-by | 15:55 (`flat_by`), used by the simulations |

The plan is refused if the stop is at or above the entry, or if the risk is under **0.05% of the
price** (too tight to survive normal noise and the spread).

Important: see section 11. The 2R target is used by the reward:risk checks, but it is **not sent to
Trading 212 as an order**. Real exits are the stop, the trailing stop, or the 15:50 close, and since
11 Nov the simulations (section 9) model exactly that.

---

## 9. Step 5: the Time Machine (simulation filter)

`timemachine.py run` replays the plan before any money is risked.

**How the replays work:**

1. Fetch 60 days of 5-minute bars for the stock. Keep only full sessions (78 bars, 09:30 to 16:00)
   before today. At least 20 are needed, or it refuses.
2. Build a pool: for every bar slot of the day, each past session's bar as ratios to the previous
   bar's close.
3. Run **10,000 replays** (fixed seed 19, so results repeat exactly). Each one starts at the entry and
   walks the rest of the day, picking for each bar a random past bar from roughly the same time of
   day (plus or minus 2 slots) and scaling it to the current price.
4. **Each replay is exited the way the guard exits a real trade** (since 11 Nov), bar by bar:
   - the stop is checked first in every bar. A bar whose low reaches it sells at the stop, or at
     the bar's open if it opens below the stop (a gap);
   - then the guard's 5-minute check at the bar's close. Once the highest close is **1R above the
     entry**, the stop is raised to **1R under that highest close**. It is never lowered, never put
     within a cent of the price, and only raised in steps of at least $0.05 or 0.1R;
   - **half at the target** happens only if `sell_half_at_target` is switched on (it is off), and
     then, as in the guard, only with the trailing stop on;
   - whatever is still open is sold at the **15:50 close**.
   There is no fixed 2R take-profit in the replay, because there is none in real trading. With the
   half sale off, the target's position makes no difference to the result.
5. Separately, the same rule and the same real exit are re-run on each of those past sessions as a
   history check.

**Costs:** each replay's average R is reduced by the Trading 212 costs: **0.15% FX fee plus 0.05%
slippage on both the buy and the sell** (about 0.4% a round trip), expressed in R. A tight stop makes
the same 0.4% a bigger share of R.

**The decision rule (`autopilot.min_edge` = 0), calculated on the real exit:**

- Take the replays that made money and the ones that lost money (a replay that ended exactly flat
  is left out).
- The **winning share** is the winners' share of those.
- The **break-even share** is the share of winners needed to break even, given how big the average
  win and the average loss were: average loss / (average win + average loss). For example, with an
  average win of +0.7R and an average loss of -0.7R it is 50%; with +1.3R and -1.0R it is 43%.
- The plan goes on only if the winning share is **above** the break-even share. In plain terms: the
  average replay, before costs, must make money.
- Labels: within 5 points is "close to break-even", 5 to 15 points "leans toward a profit/loss", 15
  or more "strongly toward a profit/loss".

This is the same rule as before. Before 11 Nov the only outcomes were "target" (+2R) and "stop"
(-1R), and the break-even share was then 1 / (1 + 2) = 33%. The old target-first and stop-first
odds are still worked out and shown, for information only.

**The cost block (`autopilot.min_net_r`):** normally the average R *after costs*, on the real exit,
must be above 0 or the plan stops here. **In evidence mode this block is switched off**: a negative
after-costs average is logged (`cost_block_ignored`) but does not stop the trade. The before-costs
rule above still applies.

A simulation built from 60 days of the stock's own bars is not a prediction. Earnings, news and halts
can do things those 60 days never did.

---

## 10. Step 6 and 7: sizing and the risk gate

### Sizing (`trader-budget/size.py`)

The share count is the **smallest** of these limits, in 0.01-share steps (fractional shares are on):

| Limit | Calculation |
|---|---|
| Max loss | max loss per trade (1% of the account) / risk per share |
| Position cap | 22% of the budget / entry price (`max_position_pct`, 75% before 12 Nov) |
| Cash | (the cash free now at Trading 212 minus the 10% cash buffer) / entry price (see "The account snapshot" below) |
| Max order | Not set for this GBP account, so no limit |

The price it sizes at is the plan's entry, or a fresh quote (under 15 minutes old) if that is
**higher**, because a price that has run up means a bigger loss at the same stop. No order is made
when:

- zero shares fit (the stop is too far for the max loss),
- the reward:risk at that price is under **1.5** (`min_reward_risk`): the price has already run
  toward the target, or
- the order is under £5 (`min_order_gbp`).

### The account snapshot (from 12 Nov)

Before sizing each candidate, the autopilot reads Trading 212 (account, pending orders, positions;
read-only) and writes `trader/state/portfolio.json` in dollars: the cash a new buy may use (cash
available to trade, minus buys still waiting to fill that Trading 212 has not already held back,
minus morning reservations for other stocks) and every position held or waiting to fill. Sizing and
the risk gate read it, so their cash and position checks see the real account. Until 11 Nov there was
no such file, and both checked each buy against an empty account funded with the whole budget: on
11 Nov, after one buy took about half the account, a dozen more passed both checks and were refused
by Trading 212 for insufficient funds. If the snapshot cannot be read, the file is removed, both fall
back to the budget, and the cash check at placing time (step 9, also from Trading 212) still decides.

### The risk gate (`trader-risk/check.py`): the last check before an order

Each order gets PASS or BLOCK with every reason listed. An entry is blocked by any of these:

| Check | Rule |
|---|---|
| Format and asset | A valid US stock or ETF ticker; no options, crypto or short sales |
| Lists | On `blocked_tickers`: blocked. Not on the allowed list or universe: refused at placing |
| Stop required | Every entry must have a stop below the entry (`require_stop`) |
| Max loss | Shares x (entry minus stop) must not exceed 1% of the account. The entry used is the higher of the plan and a fresh quote |
| Reward:risk | (target minus entry) / (entry minus stop) at least **1.5** |
| Reward:risk after costs | With the 0.15% + 0.05% each way taken off the reward and added to the risk, at least **1.0** in evidence mode (otherwise the same 1.5) |
| Daily loss | Losses **already realised today**, plus the risk **still open at the stops**, plus this trade's max loss, must not exceed 3% of the start-of-day value (see below) |
| Trades a day | At most **5** entries a day (`max_trades_per_day`) |
| Orders a day | At most **10** orders a day (`max_orders_per_day`) |
| Duplicates | The same stock and side already placed today: blocked |
| Position size | The stock would be over 22% of the whole account (held positions, buys waiting to fill and cash) |
| Cash | The buy would dip into the 10% cash buffer or need margin, measured on the real free cash |
| Time window | Before 09:45, after 15:30, or the market closed (weekend, holiday, early close) |

**How the daily loss is counted** (`daily_loss_count`, `open_risk` since 11 Nov). Before each risk
check the autopilot reads Trading 212 (positions, pending orders and today's fills; read-only) and
writes `trader/state/day-risk.json`. For each entry placed today:

- **Closed at a loss:** its realised loss, after the FX fees, counts.
- **Closed at a profit:** counts nothing. Its risk is freed, but its profit does not add room for
  extra risk either.
- **Still open:** shares held x (price paid minus the stop it has now, plus the FX fee each way)
  counts. The stop it has now is the stop order at Trading 212, else the guard's software stop,
  else the planned stop. Once the trailing stop is at or above the price paid, it counts 0.
- **Not showing yet** (not filled, or the history is slow): its full planned max loss counts.

If that file is missing, broken, not today's, or over 15 minutes old, the check falls back to the old
count (the full max loss of every entry today, win or lose) and says so. The 3% cap is unchanged.
`"daily_loss_count": "full_risk"` brings the old count back for good.

What this means in practice: each new trade still needs 1% of room (its max loss). The day is shut
for new trades once the realised losses plus the open risk are over 2%. So the limit now stops new
trades after about **three full-size losses**, or when about three full-size trades are open with
their stops still under the price paid. Days with winners or small losses run into the
**5-trades-a-day limit** instead. Under the old count, any three trades used up the 3%, wins included.

The verdict is saved with a fingerprint of the exact order. Nothing can be placed unless **that exact
order** passed today.

---

## 11. Step 8: placing the order, and how it is closed

### Who says yes

- With `autopilot.auto_buy` true (on, demo only), an order that passes every check is placed without
  a tap. With it off, the order goes to the phone as a ticket with Approve and Reject buttons that
  expires after 10 minutes.
- In evidence mode, **every** passing signal in a cycle is placed (limits permitting), not just the
  first.
- `t212.py place` run by hand needs a typed "yes" in a terminal.

### Checks again at placing time (`autopilot._approve`)

1. The order file must still match the fingerprint that passed.
2. A **fresh quote** is fetched. If the price is more than **0.5R above the planned entry**
   (`max_chase_r`), it is too late to chase: not placed. If it is at or under the stop: not placed.
3. The **risk gate runs again** on the current state (daily loss, trade count, lists) and must PASS.

### Inside `t212.py place`

1. Refuses on a live account unless live trading has been switched on by the owner; refuses evidence
   mode on live.
2. Checks the allow and block lists again, US-dollar instrument only, market orders only.
3. Checks Trading 212's pending orders and positions for a **duplicate**.
4. **Re-sizes in pounds** at the live rate: never more than the ticket's quantity, and the loss at the
   stop *including the 0.15% FX fee on the buy and the sell* never over the max loss. Also capped by
   Trading 212's `availableToTrade` cash.
5. Sends a **market buy**. A rejected or unclear send is **never retried automatically**.
6. Waits up to 10 seconds for the fill, then sends a **stop order, good till cancelled**, for the
   same quantity at the stop price.
7. **One minute later** it checks the stop is still live. If Trading 212 rejected it or it is gone,
   the guard takes over with a **software stop**: a market sell as soon as the price is at or under
   the stop, with a loud alert.
8. Records the buy in `trader/state/broker.json`, labelled `"strategy": "intraday"`.

### How a position is closed (the guard, `t212.py guard --watch`)

The guard only ever cancels stops and sells. It never buys. It checks every 5 minutes from the open
to 16:00:

- **The stop** is re-checked on every pass. A missing or rejected stop becomes a software stop.
- **Trailing stop** (`trailing_stop`, intraday only): once the highest price seen is **1R above the
  entry** (`activate_r`), the stop is raised to **1R under that high** (`distance_r`), rounded down
  to the cent. It is never lowered, only moves in steps of at least the larger of $0.05 or 0.1R, and
  is never set at or above the current price. Trading 212 has no "modify", so the guard cancels the
  old stop and places a new one; if the new one is refused, the old one is put back.
- **Half at target** (`sell_half_at_target`) is **off**. So the 2R target is not an exit by itself.
  A trade that reaches 2R has a stop trailing 1R under its high, and is closed by that stop or by the
  end-of-day close. The Time Machine and the virtual results model exactly this (section 9).
- **End-of-day close at 15:50:** for every intraday (or unlabelled) position, cancel its stop, confirm
  the cancel, then **sell at market**. A drift position is kept until its sell date. A position with
  no buy record at all is **not sold**: it raises a GUARD ALERT for the owner to deal with.

Stop prices are a plan, not a guarantee: a gap or fast market can fill past them.

---

## 12. What runs alongside but never trades

### Evidence mode (the forward test, since 9 Nov 2026)

- Every triggered signal is logged to `trader/state/evidence/signals.jsonl` with its reason, traded or
  not, and whether it came from the hand list or the universe.
- After the close each signal gets a **virtual result**: a market buy at the first bar after the
  signal, then the **same real exit as the Time Machine** (stop, 1R trailing stop from 1R profit,
  out at 15:50), after the same 0.4% round-trip costs.
- **Old and new rules are kept apart.** Every signal and trade has a `rules` tag: `v1` for rows up
  to 10 Nov (fixed 2R target in the simulations, full-risk daily loss count) and `v2` from 11 Nov
  (real exit, open-risk count). v1 rows keep their old fixed-target virtual results, so the two
  periods are measured as they were run. The Friday report shows each on its own line, as it does
  for the hand list and the universe.
- Real trades are logged with the expected price, so actual slippage can be compared with the 0.05%
  assumed.
- The Friday report shows ON TRACK only if the **average real trade after costs is positive AND
  beats an S&P 500 tracker** over the same days, for "this week" and "since the start". Under 20
  closed trades (`min_trades`) it says "too few trades to judge".

### Kronos (shadow)

A forecasting model gives a verdict on up to 9 minutes of signals per cycle (real trades first). Each
takes about 5 minutes on this Mac, so most are skipped. It is logged only and never blocks or
allows anything.

### Post-earnings drift scan (shadow, stage 2 of 4)

Runs at 15:35 with **no Trading 212 connection** and never places an order. It lists stocks that beat
earnings forecasts by 10% or more (forecast at least $0.10 either side of zero), in the universe,
ranked by surprise relative to price, into a virtual 10-slot book: buy at the open of the 2nd trading
day after the report, sell at the close of the 20th day held. Results are tracked at 5, 10, 20 and 40
days against SPY, after costs. In research it had a real signal but **did not beat the index** on
the unseen 2024 to 2026 period.

### Post-earnings drift buying (stage 3 of 4, OFF by default)

Behind `"drift": {"buy_enabled": false}` (demo only; live refuses it in `drift_buy.active` and in
`t212.py place`). With it off, nothing buys drift. When it is on, at 09:33 New York, before the
intraday window, `trader-broker/drift_buy.py` works through today's shortlist:

- **Skips, each logged with its reason** in the drift signal log and `state/drift/buys.jsonl`:
  blocked; held or waiting under either strategy; no free slot; drift's share used; no cash
  reservation; no free cash; already under its stop at the open (yesterday's close minus 3xATR);
  the open-risk cap; /pause; a start after 09:45 ("missed": never bought late).
- **The order:** a market buy through `t212.py place` (every placing check), labelled `"drift"` with
  `sell_date` = the 20th trading day held, and a real Trading 212 stop (good till cancelled) whatever
  `stop_mode` says. The stop is `atr_mult` (3) x the 14-day ATR under the entry. **This stop is not
  from the research, which held with no stop.** A refused stop raises a GUARD ALERT; the guard puts it
  back or holds it in software (market hours only), and /status, MARKET OPEN, the briefing and the
  closing report say "NOT PROTECTED OVERNIGHT".
- **Size:** the smaller of one slot and 1% of the account at the stop. One slot is the drift pool
  (`max_share_pct` 50% of the investable 90%) divided by `slots` (10): about 4.5% of the account.
- **Cash:** drift never buys without a reservation in `state/allocation.json`. The 07:30 plan reserves
  drift's slots first (intraday's reservations come from what drift does not use); if that plan is
  missing or has none, the buy step reserves them itself; if it cannot, that day's buys are skipped.
- **Limits of its own:** `slots`, `max_share_pct`, and `max_open_risk_pct` (4% of the account at the
  drift stops in total). Drift does not count toward intraday's 4 positions, trades a day or daily loss.
- **Selling:** the 15:50 close on the sell date (or the next close the Mac is on, with a DRIFT SELL
  OVERDUE alert), or earlier by the stop. No trailing stop or half sale. DRIFT SELL gives the result
  after costs, days held and SPY over the same days. Turning buying off stops new buys only.
- **Friday:** its own section and pass rule (positive average after costs AND beating SPY over the
  same holding periods), real trades and shadow signals apart, under 20 closed: too few to judge.

**Known risk:** a stop cannot stop a gap. A stock that opens far under its stop sells at the open,
so one drift loss can be more than 1% of the account.

---

## 13. Safety nets in one list

- **Demo only.** The autopilot refuses any environment but demo; evidence mode is refused on live in
  three places; live placing needs the owner's own switch.
- **Kill switch:** `/pause` from the phone stops all new scans and buys; `/resume` restarts them.
- **Every order passes twice:** once when built and again at placing time.
- **Stops are mandatory** and checked one minute after placing and on every guard pass, with a
  software stop as a backup.
- **No retries:** a failed or unclear order is never resent automatically; "unclear" raises an alarm.
- **Limits:** 1% max loss a trade, 3% a day (losses realised plus risk open at the stops), 5 entries a day, 10 orders a day, 10% cash
  kept, one trade per stock per day, no new entries after 15:30, everything intraday closed at 15:50.
- **Slow cycles:** a cycle that has used 75% of its 15 minutes stops building new trades (they wait
  for the next cycle); over 80% triggers a phone warning.
- **No internet:** checked every 2 minutes for up to 30, then a BACK ONLINE message saying what was
  missed. Failed phone messages wait in an outbox.
- **Secrets** are never printed or logged; errors are scrubbed.

---

## 14. Known gaps and caveats

- **The rule has no proven edge.** About -0.03R before costs and about -0.65R after costs in the
  research; nothing beat holding an S&P 500 tracker. Evidence mode deliberately relaxes the cost
  checks to collect real data, so it is expected to lose money on the demo.
- **The simulated exit is an approximation.** Since 11 Nov the Time Machine and the virtual results
  exit the way the guard does, but on 5-minute bars: the guard really sees the price every 5
  minutes, and a real stop can fill worse than its price in a fast market. Before 11 Nov (rules v1)
  they assumed a fixed 2R take-profit and, for the Time Machine, a 15:55 close.
- **The open-risk daily count depends on Trading 212's history.** If it cannot be read, the check
  falls back to the stricter old count. A loss realised in the last minute or so may not show yet;
  until it does, that entry still counts its open risk at the stop.
- **Costs dominate small stops.** The 0.4% round trip is fixed in price terms, so on a tight
  opening-range stop it can be a large share of 1R.
- **Free data.** Yahoo and Nasdaq can lag, change or go missing. Missing bars mean a stock is skipped,
  not guessed.
- **Stale triggers.** The plan's entry is the first breakout bar's close, which can be up to 15
  minutes old; the 0.5R chase limit bounds the damage but does not remove it.
- **Partial scans.** Only the 200 most-traded stage 1 candidates are confirmed each cycle.
- **Research biases:** survivorship (today's S&P 500), hindsight in the 100-stock hand list,
  overlapping trades, and ignored GBP/USD moves. See `research/README.md`.
