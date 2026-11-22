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
| 12-month momentum, top 10, monthly | develop 2019-2023, test 2024-2026 | S&P only: +107% (SPY +109%), then +99% (SPY +60%), drawdowns 41-50% | Not proven: 84% of the 2024-2026 profit came from 5 trades |
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
python3 research/three_strategies.py mom pull && python3 research/momentum_check.py
python3 research/pead_run.py && python3 research/pead_tracker.py
caffeinate -i -s nice -n 19 trader-kronos/.venv/bin/python research/kronos_backtest.py   # about 4 hours
python3 research/kronos_risk_check.py trader/research-data                 # from the Kronos cache
python3 research/lowvol_backtest.py trader/research-data
/usr/bin/python3 research/checkpoint_review.py                             # the review checkpoint (reads the holdings record)
```

Yahoo Finance (unofficial) and Nasdaq's public calendar supply the data; both can lag or change.
