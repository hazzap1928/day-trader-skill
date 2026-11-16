# Hold-and-review agent: the spec

Written 16 November 2026. Demo account only. Education and tooling, not financial advice.

**The aim:** replace the day-trading engine with an agent that buys each share for a written reason,
holds it until a rule says sell, and reports how it did. A sale because the reason has broken needs
the owner's approval on Telegram.

## Decisions (16 Nov 2026)

| Topic | Decision |
|---|---|
| Drift buying | Stays ON (`drift.buy_enabled: true`, intended). Drift stays its own strategy |
| Day trading (the ORB scanner) | Retired at the END of the build, not before |
| Loss limit | The 3% daily loss limit is replaced by one cap on total risk: everything all holdings could lose at their stops together is at most 4% of the account |
| Money | 10% cash always. Drift may use up to 50% of the investable cash. "news" uses what drift does not |
| The holdings record | Broker-neutral, so a later Interactive Brokers version can use the same record |
| The S&P 500 | Shown in reports for information only. Never a pass or fail test |
| First fix | The failing em-dash test (it read a saved news headline in the local state folder) |

**Later on 16 Nov (the owner's changes after the first build):**

| Topic | Decision |
|---|---|
| Daily loss limit | The 3% daily loss limit is back, for day trades only, until the day-trading scanner is retired |
| Risk cap | Split by strategy so one cannot block another: drift 2%, news 2%, intraday 1% of the account (settings). They add up to 5%; the single 4% line no longer stops a buy |
| A mismatch | Pauses new buys until the record and the broker agree. One message. Nothing is sold or fixed |
| The daily review | Built, report only: it says what it would sell or ask about |
| News runs | Two a day: 06:00 UK (into the 07:30 briefing) and 08:30 New York (a short update; a message only if something changed). A run that cannot finish in time is skipped, and nothing is bought from a run that did not finish |
| News quality | Confidence (only "high" may be bought), how far the share has already moved, a sell date at most 10 trading days after the event unless a reason is given, a 5% price check at buy time, results by confidence on Fridays |

## 1. Where the drift build stood on 16 Nov

| Stage | What it is | Status |
|---|---|---|
| 1 | Label each buy "intraday" or "drift"; the 15:50 close keeps drift shares until their sell date | Done |
| 2 | Shadow scan: finds qualifying earnings beats, logs them, buys nothing | Done, running since 10 Nov |
| 3 | Real drift buying behind a switch | Built |
| 4 | The switch | ON since the evening of 12 Nov (the owner's decision) |

- The 09:33 buy step had run twice and bought nothing: the shortlist was empty both times.
- Three shadow scans had found 0 qualifying stocks, so there was no result yet to judge the rule by.
- The drift stop (3x ATR, a measure of the stock's normal daily swing) is a setting, not a tested rule.
- The holiday list ends in 2027, so a sell date in 2028 is refused.
- Whether a drift buy works end to end on the real demo account was unknown: none had been placed.

## 2. What treated positions as day trades

The biggest trap: the code knew two labels. Anything not labelled "drift" was treated as intraday and
sold at 15:50 New York.

| Where | What it did | Why it breaks a hold |
|---|---|---|
| `t212.eod_close` | Sold every intraday or unlabelled position at 15:50 | A share with a new label would be sold the same day |
| `t212.position_strategy` | Turned any unknown label into "intraday" | The same trap, at the root |
| `t212.drift_due` | Sold a drift record with no readable sell date | A holding sold by a rule has no date |
| `t212.cmd_guard` | Watches 09:30 to 16:00 New York on weekdays only | A software stop does nothing overnight or at weekends |
| `t212.guard_trail`, `guard_market` | Trail the stop 1R under the high, every 5 minutes | Tuned for one day; over weeks it sells on normal wobbles |
| `check.check`, `placed_today`, `day_trades` | Trades a day, orders a day, daily loss, entry window | They count today only |
| `day_risk.entries_today` | Daily loss = today's losses plus risk on today's buys | Risk on last week's buys is not counted |
| `autopilot.positions_full` | At most 4 open positions, drift left out | Needs one view across all holdings |
| `autopilot.Pilot.day`, `cycle` | Scan every 15 minutes for opening-range breakouts | This is the day-trading engine |
| `exit.resolve_flat_by`, `size.py` `flat_by` | Plans say "flat by 15:55, no overnight hold" | Wrong for a holding |
| `timemachine.GuardExit`, `exits_from` | Simulations end at 15:50 | Cannot test a hold of weeks |
| `evidence.py`, `report.py` | Results cover one day's fills | A holding needs a result over its whole life |
| `briefing.py` | Warns about anything "held overnight" | A false alarm every morning for a holding |

## 3. The holdings record

**In plain English:** one notebook page per holding, written when the share is bought. It says why the
share was bought and what would make the agent sell it. Nothing is erased: every change is also added
to a log.

**Files:** `trader/state/holdings.json` (the pages) and `trader/state/holdings-log.jsonl` (every change).
The existing `portfolio.json` is a different thing (a cash snapshot) and stays.

**Broker-neutral:** a page names the share by its plain ticker (AAPL), the broker by name
(`trading212`) and the account (`demo`). The broker's own code for the share and its order numbers
sit together under `broker_ref`, so another broker can fill that part its own way.

| Field | Meaning |
|---|---|
| `id` | A unique name for this holding |
| `symbol`, `company` | What it is |
| `strategy` | Which rule bought it: `intraday`, `drift` or `news` |
| `reason` | The written reason, in a sentence |
| `wrong_if` | What would prove the reason wrong |
| `event`, `event_date` | For "news": the event the idea waits for |
| `sources` | Where the reason came from, each with a date |
| `buy_date`, `buy_price`, `qty`, `cost_gbp` | What was bought |
| `stop` | The safety-net price |
| `sell_rule` | A date, a named rule, or both |
| `tracker_at_buy` | The S&P 500 tracker's price on the buy day (information only) |
| `status` | `open`, `sell_asked`, `selling`, `closed` or `mismatch` |
| `result` | Filled in when closed: date, price, why it was sold, the result in pounds after fees |
| `broker`, `account`, `broker_ref` | Which broker and account, and that broker's own references |

**Rules**
- A buy that is not a day trade needs a page with a reason and a sell rule.
- A position with no page, an unknown label, or no readable sell rule is never sold at the close. It is
  kept and reported.

**The daily match against the broker**

The agent reads what the broker says is held and compares it with the pages.

| Mismatch | What it means |
|---|---|
| Held at the broker, no page | Something was bought outside the agent |
| A page says open, nothing is held | It was sold (a stop, or by hand) and not recorded |
| The share counts differ | A part sale or a part fill |
| No stop order, or a stop for too few shares | The safety net is missing or short |
| A stop order for a share that is not held | A leftover order |

On a mismatch the agent sends one message naming the stock, what the record says and what the broker
says. It does not sell, buy, or edit the page to fix it. While the mismatch stands, new buys are paused
(built 16 Nov); they come back by themselves when the two agree again.

The one automatic repair is the safety net: the guard already puts back a missing stop.

## 4. The daily review

Once a trading day after the close, on final closing prices. Safety first, then money, then judgement.

1. **Can I read the account?** If not: "could not check today", and stop.
2. **Does the record match?** The check in section 3.
3. **Does every holding have a stop?** A missing one is the first line of the message.
4. **Are the money limits met?** Cash at 10% or more, drift at or under half of the investable cash,
   total risk at the stops at or under 4% of the account. A breach pauses buys. Nothing is sold to fix it.
5. **Is a rule sale due?** A holding at its sell date is sold. No approval: the rule was agreed at the buy.
6. **Is a reason broken?** For example the event was cancelled or moved, the company withdrew the figure
   the idea relied on, the stock left the universe or went on the blocked list, a takeover or delisting
   was announced. A broken reason never sells by itself. It sends Approve and Reject buttons.
7. **How is each holding doing?** Its gain in pounds after fees, with the S&P 500 beside it for information.
8. **One message.**

**Approval:** Approve sells in the next session through the guard's sell path (the only part allowed
to sell). Reject or no answer keeps the share with its stop, and the question comes back the next day.

## 5. The "news" strategy

- **What:** each weekday before the US open, a scheduled Claude Code run reads news, company reports,
  filings and coming events for the tradable universe, and writes down forward-looking ideas: events
  expected to lift a share price.
- **Each idea has:** the company, why the price should rise, the event and its date, what would prove
  it wrong, the sell rule, and dated sources.
- **Checks:** every idea goes through the universe, the blocked list, sizing, a stop, the cash rules
  and the total-risk cap. News, filings and Kronos are added for information. The opening-range scan
  and the Time Machine do not apply: they measure a one-day trade.
- **Shadow by default:** ideas and their virtual results (in pounds, after fees) are logged. No orders.
  `"news": {"buy_enabled": false}`. Only the owner switches it on.
- **When buying is on:** buys are labelled "news", held until the sell rule, and a broken-reason sale
  needs approval on Telegram.
- **If the Claude run fails:** it is logged, one message is sent, and nothing is bought.
- **Honest limits:** these ideas are a model's reading of public news. They are untested. Nothing in
  this project's research has beaten simply holding an S&P 500 tracker.

## 6. Safety tests

All offline, with the fake broker, phone and clock.

**No double orders**
- A buy for a stock already held or waiting to fill is refused.
- The same run twice in one day sends each order once.
- An approval tapped twice acts once.
- An unclear answer from the broker is never retried automatically.

**Every position always has a stop**
- A buy with no stop is refused before it is sent.
- A refused or vanished stop raises an alert and is put back.
- A holding never gets the 1R trailing stop or the half sale.
- The daily match reports a holding with no stop.

**Held shares survive the close**
- A holding before its sell date is still there after 15:50, its stop untouched.
- A holding with an unknown label is kept and reported, never sold.
- A holding with no readable sell date is kept and reported.
- A position with no record is not sold.

**Cash stays at 10% or more**
- A buy that would take cash under the buffer is refused, not shrunk.
- Buys still waiting to fill count as spent.

**Drift stays at or under half of the investable cash**
- A buy that would breach it is skipped and logged with the reason.
- "news" can only use what drift does not.

**Total risk stays at or under 4% of the account**
- A buy that would take the total over the cap is refused.
- A holding with no stop counts its whole value as at risk.

## 7. Build order

1. Fix the failing test. Save this spec.
2. The holdings record and the daily match, report-only. "Keep and report" becomes the default at the close.
3. The "news" strategy in shadow: ideas logged, nothing bought.
4. The daily review in report-only mode (done 16 Nov), then rule sales, then approval sales.
5. Retire the day-trading scan last.
