# Strategy research (November 2026)

Backtests behind the decision about what the demo autopilot should trade. Analysis only:
nothing here places an order or changes the autopilot. Not financial advice.

## Costs used

- **Trading 212 GBP account through the API:** every buy and every sell of a US stock pays the
  0.15% FX fee plus 0.05% slippage, so a round trip costs about 0.4%. Trading 212's API can only
  place orders in the account's primary currency, so a USD balance cannot avoid the fee for API
  trades (it can for orders placed by hand in the app).
- **USD-balance scenario:** 0.05% slippage per fill, plus one 0.15% conversion up front.
- **S&P 500 tracker:** the GBP-listed UCITS version pays no FX fee, only 0.05% slippage.
- **Sizing:** GBP 2,250 invested. The intraday and swing tests risk at most GBP 30 a trade, with
  at most GBP 1,875 in one position.

## Results

| Test | Period | Result after costs | Verdict |
|---|---|---|---|
| Opening-range breakout (the current autopilot rule), 100 allowed tickers | 59 sessions, Aug to Nov 2026 | -0.65R (about -GBP 7.90) a trade on the GBP account; -0.18R with a USD balance | Loses even before costs |
| ORB variant (stop at the opening-range low, 2R target, above the 50-day average) | same | -0.38R (GBP account), -0.13R (USD) | Loses before costs |
| Trend swing (55-day high, 2xATR stop, up to 20 days) | 2022 to 2026, 5 years of daily bars | +0.09R (GBP account), +0.17R (USD) | Beat only 76% of random uptrend entries: market drift, not skill |
| Pullback (RSI(2) under 10 in an uptrend, out above the 5-day average) | same | -0.01R (GBP account), +0.04R (USD) | Real but tiny edge, only without the FX fee |
| 12-month momentum, top 10, monthly | develop 2019-2023, test 2024-2026 | S&P only: +107% (SPY +109%), then +99% (SPY +60%), drawdowns 41-50% | Not proven: 84% of the 2024-2026 profit came from 5 trades (redone 22 Nov with the real index: see below) |
| Pullback, 20-day hold | same | +56%, then -13% (random entries +24%) | No edge; the exit was picked from 4 tries |
| Post-earnings drift, 20-day hold (signal level) | same | +2.4% a trade, both periods, about 1-1.6 points better than random reports | Real signal |
| Post-earnings drift on top of an S&P 500 tracker | same | 2019-2023: +96.7% vs tracker +109.5%; **2024-2026: +46.5% vs tracker +60.7%** | **Does not beat the index on the unseen period** |

The rules for post-earnings drift on the tracker were chosen on 2019-2023 only:
- ranking by surprise relative to the share price
- 10 slots, out of 9 combinations of 3 rankings and 5, 10 or 15 slots

All 9 combinations lost to the tracker in 2019-2023. The chosen one was run once on 2024-2026:

| | Return | Max drawdown | Earnings trades |
|---|---|---|---|
| Strategy | +46.5% | 22.2% | 7.5 a month |
| Tracker alone | +60.7% | 18.8% | none |

The strategy beat 80% of portfolios built on random earnings reports, which is not significant.

**Conclusion:** after Trading 212's costs, none of these strategies beat simply holding an S&P 500
tracker over these periods.

## Kronos and low volatility (17 to 18 Nov 2026)

**Kronos as a stock picker** (`kronos_backtest.py`, Kronos-small on daily bars, run 17 to 18 Nov):
- 50 random S&P 500 stocks (seed 42); on the first trading day of each month, Kronos forecast each
  one 20 trading days ahead (5 paths, median). Buy its top 10 at the next open, sell at the close 20
  trading days later, 0.15% FX + 0.05% slippage each way.
- 22 months, 2025-01 to 2026-10 (after the model's training data, which may run to mid-2024).
- This tests Kronos ranking stocks on price history alone. It did **not** test Kronos as a filter on
  drift or news picks; there are too few of those so far to test.

| | Total, 22 months | Average a month | Worst fall |
|---|---|---|---|
| Kronos top 10 | +14.0% | +0.7% | 9.8% |
| The other 40 | +13.0% | +0.6% | |
| 10 random of the 50 (median of 1,000) | +12.3% | | |
| SPY | +25.7% | +1.1% | 8.5% |

Kronos beat 56% of random portfolios (a coin flip), SPY in 8 of 22 months and the other 40 in 11.
The rank correlation between its forecast and the actual 20-day return averaged +0.04 (t 1.0,
positive in 13 of 22 months): no reliable skill.

**Kronos as a risk forecast** (`kronos_risk_check.py`, from the same cached forecasts): the spread of
its 5 paths does track how much a stock will move, but the 14-day ATR does it far better (rank
correlation with next-month volatility +0.66 against +0.38), and with ATR known Kronos adds nothing
to forecasting the worst fall. A stop sized from Kronos at the same average distance as 3xATR was
hit more often (37% against 30%). Side finding: on these 1,100 random 20-day holdings, a 3xATR stop
cut the average from +0.63% (no stop) to +0.23% after costs. That is random stocks, not drift or news
picks, but the stop's cost is worth watching.

**Verdict:** no evidence Kronos improves picks or stops after costs. It stays shadow only (logged,
never deciding).

**Low-volatility stocks** (`lowvol_backtest.py`): each month hold the 20 S&P 500 members with the
lowest one-year volatility. 2019 to 2023: +44.7% against SPY +105.5% (beat 10% of random portfolios);
2024 to Oct 2026: +32.9% against SPY +65.2% (beat 51%). It fell far less in 2022 (-3.5% against
-18.7%) but trailed the index badly overall. Not a candidate.

## The drift stop and chart labels (22 Nov 2026)

`research/pead_stop.py` and `research/pead_chart.py`, on the daily-bar Time Machine for holds
(`trader-timemachine/holds.py`). Every signal of the tested drift rule (ranking B, 10 slots), bought at the open
of the 2nd trading day after the report, held 20 trading days, 0.2% costs each way. 2,624 signals in
2019-2023 and 1,387 in 2024-2026, each period reported on its own. The no-stop book reproduces the
original drift research exactly on the same data (2019-2023 +96.7% against the tracker's +109.5%); 2024-2026
now reads +48.1% (was +46.5%) because Nasdaq's figures changed since the first download (the original code
gives the same +48.1% on today's data).

**The live stop (3 x ATR, about 8 to 9% under the buy for a typical share) cost money in both periods.**

| Sell rule | Avg a trade after costs, 2019-23 | vs no stop | Avg, 2024-26 | vs no stop | Stopped |
|---|---|---|---|---|---|
| No stop (the tested rule) | +1.53% | | +2.20% | | 0% |
| 2 x ATR | +0.88% | -0.65 | +1.45% | -0.75 | 32-40% |
| **3 x ATR (live)** | +1.35% | **-0.18** | +1.91% | **-0.29** | 16-22% |
| 4 x ATR | +1.57% | +0.04 | +1.97% | -0.22 | 8-12% |
| Rising 3 x ATR | +1.15% | -0.38 | +1.49% | -0.71 | 32-39% |

As the 10-slot book against holding the tracker: no stop -12.7 and -12.6 points; 3 x ATR -28.7 and -35.2;
4 x ATR -27.2 and -23.1. A stop cut the worst single trade (-75.6% to -21.5% in 2019-2023) and the largest
fall of the book a little (40.3% to 36.9%), but on average it sold shares on ordinary dips that then
recovered. The "rising" stop (raised as the price rises) was worse than the fixed one in both periods.
In plain words: on this history, the stop protected against a few disasters and paid for it with many
small, unnecessary sales.

**The worst trades.** With 4 x ATR the worst trade was -40.3% in 2019-2023 (EOG, which opened 26% down on
9 March 2020, the oil price crash, straight through its stop) and -28.9% in 2024-2026 (Oracle, held to its date).
With 3 x ATR: -21.5% and -29.3%. A stop cannot help when a share opens far below it.

**How much of the edge survives more slippage** (`pead_stop.py --slip 0.3`, `--slip 0.6`; the FX fee stays
0.15%). Average a trade after costs, and in brackets against the S&P 500 over the same days:

| Slippage a side | No stop, 2019-23 | 3 x ATR, 2019-23 | 4 x ATR, 2019-23 | No stop, 2024-26 | 3 x ATR, 2024-26 | 4 x ATR, 2024-26 |
|---|---|---|---|---|---|---|
| 0.05% (assumed) | +1.53% (+0.11) | +1.35% (+0.02) | +1.57% (+0.11) | +2.20% (+0.42) | +1.91% (+0.28) | +1.97% (+0.28) |
| 0.3% | +1.02% (-0.40) | +0.85% (-0.48) | +1.07% (-0.40) | +1.69% (-0.09) | +1.40% (-0.23) | +1.47% (-0.23) |
| 0.6% | +0.42% (-1.00) | +0.24% (-1.09) | +0.46% (-1.01) | +1.08% (-0.69) | +0.79% (-0.83) | +0.86% (-0.84) |

The edge over the S&P 500 was small to begin with (+0.1 to +0.4 points a trade) and is gone at 0.3% a side.
The agent's first drift buy (Everpure, 20 Nov 2026) filled 0.64% above the quote just before the order; Trading
212's own record shows it filled at 09:33:21 at $102.37 ("OTC", filled by Trading 212 itself), while the market
traded at $101.10 to $101.68 in that minute (Yahoo 1-minute bars). That was a Practice account fill.

**Buying in the opening minutes or later** (`research/open_timing.py`, 22 Nov 2026; Yahoo 1-minute prices,
which reach back only about 30 days: 26 Oct to 20 Nov 2026, one earnings season). The first trade at each time
against the first trade at 09:33 (when drift's order goes in); positive = waiting would have cost more:

| | 09:45 | 10:00 | 10:30 |
|---|---|---|---|
| 106 drift buy days: average (give or take) | +0.18% (0.19) | +0.34% (0.26) | +0.20% (0.33) |
| typical size of the gap, either way | 0.74% | 1.05% | 1.27% |
| 1,936 days of 97 large shares: average (give or take) | -0.01% (0.04) | +0.03% (0.05) | -0.15% (0.07) |

On drift buy days, waiting was if anything DEARER on average (the shares kept rising after a beat), though
within chance on so few days. The gap for any single buy is large (0.7 to 1.3%, well over 0.3%), but it goes
either way, so waiting is a coin toss for each trade, not a saving. The opening minutes are rougher: a single
minute's high and low were 0.51% apart on drift days at 09:31-09:35 against 0.32% at 10:00. The first real fill
(Everpure) was 0.97% above the highest trade in its minute, which no choice of time would have avoided: on that
evidence the larger part of the slippage is Trading 212's own (Practice) pricing, not the open. One fill and
one season: the evening fill checks will show whether that holds.

**Chart labels at the buy: none held up.** In 2019-2023, drift trades whose chart was "falling apart"
(under the 50-day average, down 10% in a week or at a 20-day low) did BETTER (+2.9% against +0.9%), and
those in an uptrend did worse. In 2024-2026 neither gap was there (+2.3% against +2.2%). No label had a gap
of the same sign in both periods that random groups of trades rarely match. So "skip a drift buy whose
chart looks bad" has no support here, and on 2019-2023 it would have skipped the better trades.

**Shares that were delisted, taken over or went bust.** The price data is TODAY's S&P 500 list (each
company counted only from the day it joined), so a company that was in the index in 2019-2026 and has since
left it (taken over, shrank, or went bust) is missing entirely: its signals are not in the test. With about
20-25 index changes a year, very roughly one in seven of the companies in the index over the period is
missing. Those are more often companies that fell, which is exactly where a stop helps most. So the cost
of the stop is probably overstated a little. The 3 x ATR cost (-0.18 and -0.29 points a trade) is small
enough that the missing companies could cancel it; the larger costs of 2 x ATR and the rising stop are less
likely to be overturned. A fair test of the stop needs point-in-time index membership with delisted prices,
which free sources do not give.

**No data it could not have had at the time:** the ATR uses the 14 days before the buy day only (a test
checks the buy day never counts); the stop is set from the buy-day open; a day that opens under the stop sells
at that open, and a day whose low reaches it sells at the stop; the chart labels use closes before the buy
day; S&P membership is as of the report day. Two known exceptions: the ranking uses the buy-day open (known
at the moment of buying, as live), and the surprise figures are Nasdaq's as shown today (they may have been
revised since). Prices are adjusted for dividends and splits, which scales levels but not the results.
Trades overlap, so the 95% ranges in the script's output are too narrow.

**What it means for the agent (the owner decides; nothing was changed):** the review checkpoint already
compares the stops on the agent's own trades (the stop shadow). This backtest suggests a wider stop (4 x ATR)
or none would have done better for drift on this history, with the survivorship caveat above. News ideas
have no history, so none of this applies to them.

## 12-1 momentum on our stock list, with the real index (22 Nov 2026)

`research/momentum_12_1.py` (and `momentum_fetch_removed.py` for the companies that left the index).
The rule: at each month-end close, rank the S&P 500 members that pass the live universe's filters ($5+,
$20m+ a day traded) by their return from 12 months ago to 1 month ago (the latest month skipped). Buy the
top 10 in equal amounts at the next open, rebalance a month later (only the difference is traded). Costs
0.15% FX + 0.05% slippage on every pound bought or sold; the S&P 500 tracker pays 0.05% only. Rules set on
2019-2023, checked once on Jan 2024 to Oct 2026. No stop.

**Two lists.** "Today's list" is today's S&P 500, each company counted from the day it joined (as in all
earlier research). "The real index" is the index as it was each month (github.com/fja05680/sp500), with the
companies that have since left added back where Yahoo still has their prices: 78 of 191. The other 113
(mostly takeovers such as Activision or Xilinx, plus failures such as SVB and First Republic) have no prices
anywhere free, so even the real index is missing about 55 members a month in 2019-2023 and 17 in 2024-2026.
Nine old tickers that Yahoo has since given to other companies (FB, LB, STI and others) were dropped.

| | 2019-2023 total | a year | worst drop | months up | 2024 to Oct 2026 total | a year | worst drop | months up |
|---|---|---|---|---|---|---|---|---|
| Momentum, today's list | +129.1% | +18.0% | 40.9% | 60% | +145.9% | +37.4% | 36.5% | 65% |
| same at 0.3% slippage a side | +105.2% | +15.5% | 41.0% | 60% | +132.1% | +34.6% | 36.6% | 65% |
| **Momentum, the real index** | **+85.4%** | +13.1% | 38.5% | 55% | **+159.0%** | +39.9% | 36.6% | 65% |
| S&P 500 tracker (SPY) | +109.5% | +15.9% | 33.7% | 65% | +65.1% | +19.4% | 18.8% | 68% |
| Equal-weight S&P 500 (RSP) | +91.5% | +13.9% | 39.0% | 62% | +45.2% | +14.1% | 17.8% | 68% |

By year (today's list): 2019 +47.3, 2020 +5.8, 2021 +4.9, 2022 +10.7, 2023 +26.6; 2024 +19.6 (SPY +25.7),
2025 +26.2 (SPY +17.7), 2026 to Oct +63.0 (SPY +11.6). It beat the S&P 500 in 55% and 59% of months.

**Setting the rules on 2019-2023.** Eight versions were tried on 2019-2023 only: look-back 6 or 12 months,
top 10 or 20, and a market filter (hold cash when the S&P 500 is under its 200-day average) off or on.
The filter was a disaster in every version (+13% to +18% against +101% to +165%: it sold out after the
2020 and 2022 falls and missed the rebounds). The best on 2019-2023 was 6 months, top 10 (+165%); on the
unseen years it made +136%, a little LESS than the owner's 12-month rule (+146%). So 2019-2023 gave no
reason to change the rule, and tuning would not have helped.

**Against luck.** Random sets of 10 from the same list, with the same turnover: momentum beat 82% of them in
2019-2023 (not significant) and all 200 in 2024-2026.

**Concentration.** About two-thirds of the profit came from 5 names in each period: AMD, Devon, Nvidia,
Chipotle and FICO in 2019-2023; Micron, Sandisk, Seagate, Western Digital and Palantir in 2024-2026. Four of
those five are one theme (memory and storage chips, 2026), and 2026 alone (+63%) made most of the lead.

**Survivorship bias, measured.** It hits momentum harder than the index, because momentum buys yesterday's
high-flyers and some of those later crash out of the index:
- 2019-2023: on the real index the result falls from +129% to +85%, from 20 points AHEAD of the S&P 500 to
  24 points BEHIND it. The picks only the real index had (71 of the 600 slots) averaged -1.7% a month:
  Enphase held 10 months (-64% in all), Penn, Bath & Body Works, MarketAxess, EPAM. They are no longer in
  the S&P 500, so today's list never sees them. The 113 departed companies with no prices are still
  missing; they are mostly takeovers (usually bought at a premium), so the true figure could be a little
  higher or lower than +85%; the size of the bias (about 40 points over 5 years) is the solid finding.
- For the whole index the same bias is smaller: an equal-weight portfolio of today's list made +91.9%
  against +76.5% for the real index (RSP, the real fund: +91.7%).
- 2024-2026: the two lists agree (+146% and +159%): few companies have left the index yet. That does not
  mean this period is clean: the bias appears in hindsight, when today's high-flyers that later collapse
  drop out of the index. By the 2019-2023 evidence, a figure measured today on recent years flatters
  momentum the most.

**Risks the totals hide.** Worst drop 36 to 41%, about twice the S&P 500's in 2024-2026. One month lost
25%. With 10 stocks, no stop and the money fully invested, it would not fit the agent's risk caps (2% of
the account at risk at the stops for each strategy; a position with no stop counts its whole value).

**Verdict:** mixed. On the fairest data, 2019-2023 trailed the S&P 500 (+85% against +110%), and 2024-2026
beat it by a wide margin (+146% to +159% against +65%), even at 0.3% slippage, but on the back of one
theme in one year. Momentum is one of the best-documented effects in published research, and it is also
known for sudden crashes (2009 is the famous one).

**The owner's decision (22 Nov 2026): momentum is a no-go.** No shadow log, no buying. The prices of past index
members (`trader/research-data/prices_removed.json`, `sp500-history/`) are kept for the next test: any
backtest that picks from the S&P 500 should use the real index (`members(d, "pit")` in `momentum_12_1.py`).

## Insider cluster buys, with the real index (22 Nov 2026): no edge

`research/insider_cluster.py` (data: `form4_fetch.py`, `insider_extract.py`). The owner's rule: at least 2
different directors or officers buying shares in the open market (Form 4 code P) within 30 days, $100k or more
together, dated by the filing date (when it became public), bought at the next open, held 20, 40 or 60 trading
days with the drift stop (3 x ATR), 0.2% costs each way. Sales, exercises, grants, joint filings, 10% owners who
are not officers, and fund-like names left out. Universe: the S&P 500 as it was on the filing date, $5+, $20m a
day. Rules set on 2019-2023, checked once on 2024 to 15 Sep 2026 (the SEC's newest quarter ends 30 Sep; no
October data yet).

- **Signals:** 588 in all, about 72 a year (of 10,682 clusters in all US companies, 94% were not in the S&P 500).
  Lumpy: 100 in 2020 (51 in March 2020 alone), 36 in 2021.
- **2019-2023, per trade, against the S&P 500 over the same days:** 20 days -0.34%, 40 days -0.90%, 60 days
  +0.17% (95% range -1.5 to +1.9). None clearly above zero. Random S&P buys on the same days did almost as well
  (cluster buys come after falls, and so does the rebound).
- **The 10-slot book, 2019-2023:** +54% (20 days), +36% (40), +79% (60) against the tracker's +110%; worst drop
  about -52% (March 2020) against the tracker's -34%. Worst single trade -47% (a gap through the stop).
- **Chosen on 2019-2023: the 60-day hold.** Its one check on 2024-2026: **-1.09% a trade** against the S&P 500,
  48% of trades made money (average win +14.0% against average loss -9.8%), book +24% against the tracker's
  +60%. It failed.
- The 20-day hold (not chosen) looked best on 2024-2026 (+0.37% a trade, 95% range -0.8 to +1.5; book +60.5%,
  equal to the tracker); picking it after seeing that would be hindsight.
- **At 0.3% slippage a side:** every hold is behind the S&P 500 in both periods (the chosen 60 days: -1.59% a
  trade in 2024-2026, book +17% against +60%).
- **A few winners:** in 5 of the 6 books (period x hold) the 5 best trades made more than the whole profit, so
  without them the profit turns negative; the exception is 20 days in 2024-2026. Sectors: Financials (2019-2023) and companies that have
  since left the index lost most; no single sector carried the result.
- **Overlap with drift:** 5 to 8 signals a period landed on a stock drift's 10-slot book was holding (about 2 to
  4%); about 40 a period fell within some drift signal's 20 days.
- **Limits:** about half the companies that left the index have no Yahoo prices (63 signals had no prices or
  failed the filters); a handful of very large "director" buys (Kraft Heinz, TransDigm, $60m to $420m) are
  probably fund-linked; the filing date has no time of day, so the next open is used.
- **Verdict: not worth a shadow trial.** No edge over the S&P 500 or random buys before slippage, worse after.

## Review checkpoint (from 22 Nov 2026)

The agent's own trades decide what stays. At **20 or more completed drift + news trades**
(`python3 research/checkpoint_review.py`; it runs at any count and says when there are too few):

- **Kronos:** Kronos's forecast to the sell date is written on each drift and news holding before the buy
  (`trader-broker/shadow.py`). Compare the trades it rated highly (top half) with those it rated poorly. If the
  highly rated ones have not clearly done better (at least 1 percentage point a trade after fees), remove Kronos.
- **Stops:** each holding sold by its stop is followed to its planned sell date (the "stop shadow"). If, on
  average, the shares were worth clearly more by then than the stop sold them for, the stops are costing money:
  consider widening them. If clearly less, they helped.
- **Benchmark:** each strategy against zero and against holding the S&P 500 over the same days.
- **Timing:** drift trades will be rare until the next earnings season (mid to late January), so most early
  evidence will come from news trades. Twenty trades is still a small sample: luck can swing it either way.

## Caveats

- **Survivorship bias:** the universe is today's S&P 500, each stock counted from the day it joined.
  Companies that left the index are missing, which flatters every long strategy. SPY and RSP do
  not have this problem.
- **Hindsight in the allow-list:** the 100 allowed tickers were chosen in 2026. That inflated
  momentum (SHOP alone), so the momentum figures above use S&P members only.
- **Overstated t-statistics:** trades overlap, and earnings seasons cluster.
- **Data gaps:** the earnings data has no analyst counts or market caps, so 20-day dollar volume
  stands in for size. Nasdaq's historical surprise figures may have been revised since they were
  first published.
- **Exchange rate:** GBP/USD moves are ignored throughout.

## Rerun

The data is fetched by the scripts. It lands in `trader/research-data/`, which is git-ignored, or
in `$RESEARCH_DATA`. It is never committed.

```bash
python3 research/orb_fetch.py && python3 research/orb_fetch.py variant      # 5-minute ORB trades
python3 research/orb_costs.py                                              # ORB costs, by stop distance
python3 research/daily_fetch.py && python3 research/four_strategies.py     # four strategies, USD vs GBP costs
python3 research/prices_fetch.py                                           # S&P 500 daily, 2017 on
python3 research/earnings_fetch.py                                         # Nasdaq earnings, 2019 on (about 2 hours)
python3 research/pead_stop.py && python3 research/pead_chart.py             # the drift stop and chart labels (22 Nov)
python3 research/pead_stop.py --slip 0.3 --only none,3x,4x                   # the same at 0.3% slippage a side
python3 research/open_timing.py                                             # opening minutes against later (last ~30 days)
python3 research/three_strategies.py mom pull && python3 research/momentum_check.py
python3 research/momentum_fetch_removed.py && python3 research/momentum_12_1.py   # 12-1 momentum, real index (22 Nov)
SEC_USER_AGENT="name email" python3 research/form4_fetch.py                  # SEC Form 4 bulk data (contact required)
python3 research/insider_extract.py && python3 research/insider_cluster.py   # insider cluster buys (22 Nov); --slip 0.3
python3 research/pead_run.py && python3 research/pead_tracker.py
caffeinate -i -s nice -n 19 trader-kronos/.venv/bin/python research/kronos_backtest.py   # about 4 hours
python3 research/kronos_risk_check.py trader/research-data                 # from the Kronos cache
python3 research/lowvol_backtest.py trader/research-data
/usr/bin/python3 research/checkpoint_review.py                             # the review checkpoint (reads the holdings record)
```

Yahoo Finance (unofficial) and Nasdaq's public calendar supply the data; both can lag or change.
