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
```

Yahoo Finance (unofficial) and Nasdaq's public calendar supply the data; both can lag or change.
