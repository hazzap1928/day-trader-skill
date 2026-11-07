# Go-live checklist (Trading 212, real money)

You do every step yourself. Claude never runs `go_live.py`, never types your
yes, and never enters a key. Going live is your decision; this pack is
education and tooling, not financial advice. Only trade money you can afford
to lose.

## 1. Know what changes, and what does not

- **Demo and live run the same code.** The only differences: the server
  (`live.trading212.com`), your keys, and three locks that must all be open:
  `T212_ENV=live`, the `--live` flag, and `"live_enabled": true` with
  `"paper": false` in `trader/config.json`.
- **Stops.** `"stop_mode": "broker"` (the default) sends a real stop order to
  Trading 212, good till cancelled. `"stop_mode": "market"` sends no stop order:
  `t212.py guard` sells at market when the price is at or under the stop.
  Whichever you pick runs the same way on demo, so test the one you will use.
- **Which one?** Trading 212's sources disagree. Its help centre says live API
  accounts can place market, limit, stop and stop-limit orders. An older API
  reference and older forum posts say live takes market orders only. If your
  first live stop is refused, switch to `"stop_mode": "market"`. The guard then
  protects the position on its next check.
- **A market-mode stop is only as good as the guard.** It needs the Mac awake,
  online, and the scheduled guard running. It looks once a minute, so a fast
  drop can sell well under the stop. A broker stop does not have these gaps.
- **Every buy still needs your typed yes** in your own terminal, a trader-risk
  PASS for that exact ticket today, and a ticker on `allowed_tickers`.

## 2. On demo, until it is boring

- [ ] At least a week of demo trades through the whole flow: scanner, entry,
      exit plan, Time Machine, trader-budget, trader-risk, `t212.py place N`.
- [ ] Use the exact `stop_mode` and `trailing_stop` settings you will use live.
- [ ] Watch the guard put back a stop you cancelled by hand in the app
      (broker mode), or sell at a stop (market mode), at least once.
- [ ] `trader/logs/guard.log` shows a run every weekday, starting before the open.
- [ ] `trader/state/t212-guard.json` has nothing under `"unclear"`.
- [ ] Run the scheduled guard by hand once to see the output:
      `launchctl kickstart gui/$(id -u)/com.daytraderskill.t212-guard`.

## 3. The config (`trader/config.json`)

- [ ] `allowed_tickers`: a short list of liquid US stocks you know. The scanner
      only scans these, trader-risk blocks any other buy, and `t212.py place`
      refuses one. Exits are never blocked.
- [ ] `budget_gbp`: the real money you will put in, and no more. Start small.
- [ ] `max_loss_per_trade_gbp` and `max_daily_loss_gbp`: what you can lose
      without changing your plans. The daily limit stops new trades for the day.
- [ ] `max_trades_per_day`, `min_reward_risk`, `require_stop: true`.
- [ ] `allow_margin`, `allow_short` and `allow_options` all false.
- [ ] `stop_mode` and `trailing_stop` as you tested them on demo.

## 4. The Trading 212 side

- [ ] An Invest or Stocks ISA account (the API works only on those), in GBP.
- [ ] A **live** API key with only these permissions: account, portfolio,
      orders:read, orders:execute, metadata. No pies, no history, no withdrawals.
- [ ] Restrict the key to your IP address if Trading 212 offers it.
- [ ] Note where you would revoke the key in the app, in case you need to fast.
- [ ] Turn on Trading 212's own price alerts for your positions, as a backup.

## 5. The Mac

- [ ] It stays awake on weekdays from before 14:30 UK time until 21:00 UK time
      (9:30 to 16:00 New York): plugged in, sleep off.
- [ ] The scheduled guard is installed (`go_live.py --check` looks).

## 6. Switch

```bash
python3 trader-broker/go_live.py --check
```

Fix every `STOP` line. Then, in your own terminal:

```bash
python3 trader-broker/go_live.py
```

It runs the test suite, asks for the live key and secret (hidden), checks them
with one read-only call, shows the live account, and asks you to type
`GO LIVE`. Only then does it back up your shell file and config to
`trader/backups/` and switch them. Open a new terminal afterwards.

## 7. The first live day

- [ ] `python3 trader-broker/t212.py status --live`: the right account, in GBP,
      live trading ON.
- [ ] One small trade, a fraction of your normal size.
- [ ] Straight after it fills, check in the app: is the stop there (broker
      mode), or is the guard watching it (market mode, in `guard.log`)?
- [ ] If the stop was refused, set it in the app now, then switch to
      `"stop_mode": "market"`.

## 8. Going back

```bash
python3 trader-broker/go_live.py --revert
```

That puts the demo keys and config back from the backup. To stop everything at
once: revoke the live key in the Trading 212 app. Every call then fails, so
nothing more is sent. Positions stay open; close them in the app.
