# Repurposing the old day-trading tools for holds: a design

Written 17 November 2026. **Report only: no code was changed.** Demo account only. Education and tooling,
not financial advice.

The owner wants to reuse some day-trading tools inside the drift and news strategies, so
`trader-timemachine/`, `trader-scanner/scan.py`, `trader-entry/` and `trader-exit/` are kept as
**repurpose candidates** (marked so in `docs/audit-2026-11-17.md`).

## In plain English

- **A Time Machine for holds is worth building, but for a narrower job than it sounds.** Replaying a
  share's own past daily moves can tell us how often a stop of this width gets hit by ordinary ups and
  downs before the sell date, and what the trade costs in fees. It cannot tell us whether the idea is any
  good: it knows nothing about the news or the earnings beat. The most useful part is different: replaying
  the **real past drift signals** from the research with today's stop, because the drift stop has never
  been tested. Roughly 2 to 3 working sessions.
- **Waiting for the opening range before buying is possible but risky to switch on.** It would reuse the
  entry timer almost as it is. But it changes the one rule that was tested (drift buys at the open), it
  can only be checked on about 60 days of 5-minute data, and on many days it would mean not buying at all.
  It should only ever be logged in shadow until the numbers say otherwise.
- **Everything starts in shadow:** each check writes a "liked" or "disliked" verdict on every trade and
  blocks nothing. The Friday report compares the two groups after costs, and says "too few trades to
  judge" until there are enough. Drift trades are rare (about 7 a month in the research), so a fair
  judgement on drift will take many months.
- **Most of the clean-up in the audit can still go ahead**, because none of it needs these four folders
  deleted. Only one audit step (14) changes.

---

## 1. Time Machine for holds (daily bars)

### What it would answer

For one drift or news trade, before or at the buy: "with this stop, this sell date and these costs, how
often does the stop get hit first, and what is the average result before and after fees?"

### Two separate tools, because they answer different questions

| | A. Replay of the stock's own days ("noise test") | B. Replay of past signals ("rule test") |
|---|---|---|
| What it replays | Random draws of the stock's own past daily moves, from the buy to the sell date | Every past drift signal in the research data, bought and sold by today's rule, stop included |
| What it tells you | How often normal volatility alone hits the stop before the sell date; the spread of outcomes; the fee drag | Whether the 3 x ATR stop helps or hurts the tested drift rule, after costs, over 2019 to 2026 |
| What it cannot tell you | Whether the idea is good (it has no edge in it by design) | Anything about news ideas (there is no history of them) |
| Per trade or once | Per trade (a shadow verdict) | Once, then again when a setting changes |
| Value | Medium: a sanity check on the stop width | **High:** the drift stop is the biggest untested setting |

**Recommendation:** build B first. It is a backtest on data the repo already fetches
(`research/pead_tracker.py`, `research/earnings_fetch.py`, `research/daily_fetch.py`) and it answers a
question that matters now. A is a per-trade shadow check after that.

### A. What needs to change in the Time Machine

The current engine (`timemachine.py`) is built around one trading day of 78 five-minute bars.

| Part | Today | For holds |
|---|---|---|
| Data | 60 days of 5-minute bars from Yahoo (`fetch_chart`, `parse_bars`, `full_sessions`) | About 1 year of daily bars. **Reuse the daily-bar fetch drift and news already use** (Yahoo daily bars in `drift.py`, `bars_fn` in `news_ideas.py`), not the 5-minute one |
| The pool | One pool per 5-minute slot of the day (`build_pool`), drawn with a slot window | One pool of daily (open, high, low, close) / previous close. Better: draw blocks of 5 days at a time, so calm and wild spells stay together (plain single-day draws understate runs of bad days) |
| The walk | `simulate`: stop checked before the target in each bar | Same loop, simplified: no target; stop checked on each day's low, a gap below the stop fills at the open (the same rule as `news_ideas.virtual` and drift's virtual results); out at the close of the sell date |
| Outcomes | target / stop / neither, R | stop first / sold on the date; result in % and pounds; share of replays that end positive |
| Costs | `costs_from`, `apply_costs` in R | **Reuse `costs_from`** (0.15% FX + 0.05% slippage each way); report pounds and % rather than R, as the phone rules require |
| Verdict | "Leans toward the target" etc. | A shadow verdict with a rule fixed in advance, for example "disliked" when more than half the replays hit the stop first. Information only |
| Report | `report`, `render_chart` (fan chart) | Reuse `pct` and the fan chart for the log and CLI; one plain line for the phone |
| History replay | `history_replay`: the ORB rule on past sessions | Not reusable. Replaced by tool B |
| Trailing stop study | `trail_study`, `GuardExit` | Not used: holdings have no trailing stop |

**Known limits of A, to be written next to every result:** past moves are not a forecast; draws ignore the
event the trade is waiting for (an earnings date or a launch is exactly the kind of day the pool treats as
random); and it says nothing about whether the idea is right.

### B. The drift stop backtest

- Input: the research signals (2019 to 2026), the same ranking and 10 slots as `research/pead_tracker.py`.
- Variants, all fixed in advance: no stop (the tested rule), 2, 3 and 4 x the 14-day ATR, each held 20
  trading days, gap fills at the open, costs as above.
- Output: average result a trade after costs, the share stopped out, the worst drawdown, and the result on
  top of an S&P 500 tracker, for 2019 to 2023 and 2024 to 2026 **separately**.
- Rule for using it: a stop width counts as "no worse" only if it is no worse in both periods. The
  research found the drift rule did not beat the tracker in 2024 to 2026, so the honest expectation is
  that a stop changes little.
- Caveats from `research/README.md` apply unchanged (survivorship, overlapping trades, revised surprise
  figures).

### Where it would live and how big a job it is

| Piece | Where | Rough size |
|---|---|---|
| B: drift stop backtest | `research/pead_stop.py`, results added to `research/README.md` | 150 to 250 lines, 1 session (most of the data code exists) |
| A: daily replay engine | a new file beside the old one, `trader-timemachine/holds.py`, reusing `costs_from`, `pct` and the chart; the 5-minute engine is left untouched with its 47 tests | 300 to 400 lines plus about 20 offline tests, 1 to 2 sessions |
| A: shadow hook | one call after each drift and news buy is placed, plus a row in the shadow log (section 4) | small |
| Total | | **about 2 to 3 sessions; medium** |

---

## 2. Opening-range timing for the drift and news buys

### The idea

Today drift buys at 09:33 and news at 09:35 New York, at market. Instead: wait for the first 15 minutes
(the opening range, 09:30 to 09:45), then buy only if the share "holds up", for example the first
completed 5-minute bar after 09:45 closes above the opening-range low, or above the 09:30 open. If it
does not by a cut-off (say 10:30), skip it.

### What it would reuse

| From | What | Fit |
|---|---|---|
| `trader-entry/entry.py` | `evaluate` (opening range, first completed bar that closes beyond a level, bar timing rules, early-close handling), `watch_loop` (wakes after each bar closes), `get_chart` with offline replay for tests | **Good.** The `level` setup already does "first close above a price"; a "holds above the range low" setup is a small addition |
| `trader-scanner/scan.py` | `orb_window`, `session_bars`, `usable`, `orb_hit` (the same opening-range maths for many tickers at once), `fetch_spark` | Partly. Drift buys at most a few names a day, so `entry.py` per stock is enough |
| `trader-exit/exit.py` | `atr_of`, the stop maths in cents | Little: drift and news already set a 3 x ATR stop on daily bars |

### What would have to change around it

- Drift: `LATEST_START` 09:45 (`drift_buy.py:49`), the cash reservation window (09:30 to 10:15), and
  `buy_at` would all move; a buy step that waits needs to keep running inside the autopilot loop rather
  than act once.
- News: `buy_at` 09:35 and the 5% move check (`max_move_pct`, measured against the last close) would be
  checked at the later time.
- The approved sale (09:35) is unaffected.

### The risks

| Risk | Why it matters |
|---|---|
| **Missing the buy completely** | On a weak open the share never "holds up" by the cut-off, so nothing is bought. For drift that throws away part of a rule whose edge is an average over all signals: the skipped days may well include winners that dipped first |
| **It changes the tested rule** | The research bought drift at the open of the second trading day. A later, conditional buy is a different rule, untested |
| **Paying more** | Buying after a share has proved itself usually means a higher price than 09:33 |
| **Little history to test on** | Yahoo keeps only about 60 days of 5-minute bars, so this cannot be backtested over years. The ORB research on 59 sessions **lost money even before costs**, which is a warning about opening-range signals in general |
| **Data failure** | If Yahoo's 5-minute bars are late or missing, the check cannot decide. Shadow mode: log "unknown". If ever switched on, the safe default is to buy at market as today (fail open), so a data problem never silently cancels the tested rule |

**Recommendation:** shadow only. At 09:33/09:35 buy as today; separately log what the opening-range check
would have said at 10:30, and the price it would have paid. The Friday report then compares (section 4).

---

## 3. Other parts of the old pack

### Worth reusing for holds

| Part | Reuse | Value |
|---|---|---|
| `evidence.slippage` | Compare each drift and news fill with the price quoted just before the order. The pack assumes 0.05% slippage and has never measured it on holds | **High, cheap**: make it part of the Friday report when it moves to `friday.py` (audit step 7) |
| `evidence.virtual_result`, results "before and after costs" | The pattern is already copied in drift and news; reuse the wording and the cost model, not the 5-minute code | Medium |
| `timemachine.costs_from`, `pct`, `render_chart` | In tool A | Medium |
| `scan.py daily` setups (20-day breakout, back above the 20-day average, RSI under 30) | As **tags** on each drift and news trade ("in an uptrend", "oversold"), logged in shadow. Cheap context; the research found only a tiny edge for these setups on their own | Low to medium |
| `entry.py` | Section 2 | Medium, shadow only |
| `trader-news`, `trader-analyst` | Already used by the news strategy (extra information on each idea) | In use |

### Genuinely dead (no use for holds)

| Part | Why |
|---|---|
| `scan.py universe` and `trader/universe.txt` | Replaced by `trader-scanner/universe.py` and `trader/universe/tradable.json` |
| `scan.py orb` scanning many tickers every 15 minutes | Holds do not need a market-wide intraday scan |
| `timemachine.history_replay`, `trail_study`, `GuardExit` | ORB-specific; holdings have no trailing stop |
| `exit.py` targets (2R, flat by 15:55) | Holds sell on a date or a broken reason, not at a target |
| `trader-budget/size.py` | Sizing for holds is `t212.size_entry` |
| The ticket chat flow (`ticket.py` CLI), `Pilot.cycle` | Replaced by the drift and news buy steps |
| Evidence mode's ORB signal log, the daily loss limit, the intraday risk cap, `max_trades_per_day` | Day-trade limits |
| `trader-pelosi/` | Not used by anything; congressional trades are reported weeks late |
| Kronos | Forecasts hours ahead with 5-minute bars; holds last weeks. Your call (1 GB on disk) |

---

## 4. Shadow first, and how to judge

### The rule for every idea in this plan

- **SHADOW ONLY first:** the check runs on every drift and news trade (and, for drift, on every
  qualifying signal in `state/drift/signals.jsonl`, bought or not, which gives more data). It writes a
  verdict, **liked**, **disliked** or **unknown**, with its reason and inputs. It never blocks, delays,
  resizes or changes a buy.
- One log for all checks: `trader/state/shadow/checks.jsonl`, one row per trade and check: `trade_id`,
  `strategy`, `check` (for example `holds_tm`, `opening_range`), `version`, `verdict`, `why`, `inputs`,
  `at`. The check's rule is fixed and versioned **before** it runs; changing it starts a new version and
  its counts start again.
- Results come from what already exists: drift `trades.jsonl` and the drift and news virtual results,
  in pounds after costs (0.15% FX + 0.05% slippage each way), with SPY over the same days for information.

### The Friday report section (words via `messages.py`, no R on the phone)

For each check and version, two lines, then a verdict:

```
Opening-range check (v1), drift
  Liked: 18 trades, average +GBP 2.10 a trade after fees
  Disliked: 9 trades, average -GBP 0.40 a trade after fees
  Too few trades to judge (need 30 in each group)
```

- **Minimum before judging:** 30 closed trades in **each** group, and at least two earnings seasons for
  drift. Below that it says "too few trades to judge" and how it reads so far. At about 7 drift trades a
  month in the research, that is likely 9 months or more; drift's virtual signals help.
- **"The check works"** only if all of these hold: the liked group beats the disliked group after costs;
  the gap is larger than what random splits of the same trades give in 95 out of 100 tries (a simple
  shuffle test, computed in the report); and it holds in both halves of the period. Otherwise the report
  says "no proven difference".
- **Turning a check on is the owner's decision**, written down like the buying switches, never Claude's.

### Drift needs a stricter bar

The drift rule is **the one rule in this project that was tested** (a real signal at trade level, though
it did not beat the tracker in 2024 to 2026). Any filter on it removes trades from a tested average, and a
filter chosen after looking at a few months of results is very likely to be luck. So for drift:

- a filter must pass the rule above **and** be checked on the historical drift signals wherever the data
  allows (possible for tool A and B on daily bars; not possible for the opening-range check beyond
  60 days);
- the comparison must include what the skipped trades would have made, not only the kept ones;
- until then it stays in shadow, however good the early weeks look.

News ideas are untested from the start, so the same shadow rule applies, with the plain caveat that
"liked" and "disliked" are both judged on very few trades for a long time.

---

## 5. Which audit clean-up steps can still go ahead

From section 9 of `docs/audit-2026-11-17.md`:

| Step | Still safe? | Note |
|---|---|---|
| 1. Day trading defaults to off; unlabelled positions kept | **Yes** | Touches `autopilot.py` and `t212.py` only |
| 2. Move config backups, archive stale files | **Yes** | `entry.json`, `plan.json`, `timemachine.json`, `trail.json`, `watchlist.json` are old outputs; the repurposed tools will write new ones in their own place |
| 3. One locked JSON writer | **Yes** | |
| 4. Briefing says when a section failed | **Yes** | |
| 5. Security fixes | **Yes** | |
| 6. Delete Pelosi, never-called functions; rewrite `test_repo.py` | **Yes**, with a change | `test_repo.py` must keep listing the four kept folders |
| 7. Friday report out of `evidence.py` into `friday.py` | **Yes, and helpful** | The shadow comparisons (section 4) and the slippage figures go in the same new file |
| 8. Move the 5 helpers `universe.py` borrows from `scan.py` | **Yes** | Nothing is deleted; it only stops the live universe depending on the scanner |
| 9. Shared helpers out of `ticket.py` | **Yes** | |
| 10. Remove `Pilot.cycle` and its helpers | **Yes** | It is the only caller of entry, exit and the Time Machine; they stay as standalone tools with their tests. A future opening-range check would be a new, small caller |
| 11. ORB parts of `evidence.py`, `report.py`, `briefing.py`, `allocation.py`, `day_risk.py` | **Yes** | Keep `evidence.slippage` (moves to `friday.py`) |
| 12. Trailing stop and half sale out of `t212.py` | **Yes** | The Time Machine keeps its own copy of the trail maths with its tests |
| 13. Day-trade branches of `check.py` | **Yes** | |
| 14. Delete the dormant folders | **Changed** | Delete `trader-budget/`, the ticket CLI and `.claude-plugin/` only; keep the four repurpose candidates |
| 15. Docs | **Yes** | Describe the four folders as "kept for reuse, not run by the agent" |
| 16. Folder split | **Yes** | The four folders stay where they are for now |

---

## 6. For a later trim of CLAUDE.md (not done now)

**Stay** = keep in CLAUDE.md (shortened). **Move** = its own `docs/<topic>.md`, with one line and a link
left in CLAUDE.md. **Cut** = remove once the dormant code is gone (or move to `docs/history.md` if wanted).

| Section (current heading) | Decision | Where |
|---|---|---|
| What this is now | **Stay** (shortened, no dated history) | CLAUDE.md |
| The parts | **Stay** (table only, dormant rows removed or marked "kept for reuse") | CLAUDE.md |
| Strategy labels, and "keep and report" at the close | **Move** | `docs/guard-and-close.md` |
| The holdings record and the daily match | **Move** | `docs/holdings.md` |
| Risk caps by strategy, and the daily loss limit for day trades | **Move** the caps; **cut** the daily loss limit part | `docs/risk.md` |
| The daily review | **Move** | `docs/holdings.md` (with the record) |
| News ideas | **Move** (the longest section) | `docs/news.md` |
| The S&P 500 in reports | **Move** (two lines) | `docs/reports.md` |
| Drift shadow scan | **Move** | `docs/drift.md` |
| Drift buying | **Move** | `docs/drift.md` |
| Real exit and the daily loss count (dormant) | **Cut** | |
| Cash check and late fills | **Move** the cash check and the account snapshot; **cut** the ORB scan details | `docs/guard-and-close.md` or `docs/risk.md` |
| Cash allocation and confidence | **Move** drift's reservations; **cut** the intraday reservations and confidence bands | `docs/drift.md` |
| Phone messages | **Stay** as 3 rules (all wording in `messages.py`, plain English, never R on the phone); **move** the rest | `docs/phone.md` (or `trader-broker/PHONE.md`) |
| Scheduling (launchd) | **Move** the table; **stay**: the keys line, the `/usr/bin/python3` warning and the restart command | `docs/scheduling.md` |
| Current config | **Move** | `docs/config.md` (every setting with its default) |
| Evidence mode: the forward test (dormant) | **Cut** (keep the Friday report description, moved to `docs/reports.md`) | |
| Safety rules (always) | **Stay, word for word** | CLAUDE.md |
| Where things are | **Stay** (short table) | CLAUDE.md |
| *(new)* Repurpose candidates | **Stay**: one line naming the four kept folders and pointing to this plan | CLAUDE.md |

Expected result: CLAUDE.md from about 585 lines to about 150, with the safety rules unchanged.
