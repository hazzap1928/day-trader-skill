---
name: trader-broker
description: >-
  Order Executor. Sends approved orders to your broker, paper trading by
  default, never without your yes. Shows each order as a plain ticket (a
  bracket shows its stop-loss, take-profit and the max loss at the stop)
  plus the exact JSON for Alpaca's place_stock_order, records your yes word
  for word, then Claude's own Allow once / Deny card asks again. Builds the
  sell ticket that closes a day trade by your flat-by time, records fills,
  and logs each trade's max loss so trader-risk can hold the daily limit.
  Covers setting up Alpaca's official MCP server in Claude Desktop (paper by
  default, keys in env vars, never in chat), the Interactive Brokers
  connector, and setting order tools to "Needs approval". Use when the user
  says "place the order", "buy it", "execute", "send it to my broker",
  "close my position", "flatten", "get me out", "connect Alpaca", "set up
  paper trading", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 8 --filled-price 364.20
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol TSLA --side sell --qty 8 --price 370.04
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol TSLA --qty 8
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the flatten ticket, and keeps the log in
`trader/state/broker.json`. `--now "2026-10-19 10:15"` pins the clock (New York time).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |

## Setup A: Alpaca (official MCP server, Claude Desktop)

Alpaca's own server is github.com/alpacahq/alpaca-mcp-server (MIT). It runs on
the user's computer inside Claude Desktop. It is not in Claude's connector
directory. It trades on a **paper** account unless told otherwise. Checked
against its source on 2026-10-20 (version 2.4.0).

1. The user makes an Alpaca account and creates **paper trading** API keys in
   the Alpaca dashboard. Paper keys are separate from live keys.
2. The user installs `uv` (it provides `uvx`), from docs.astral.sh/uv.
3. In Claude Desktop: Settings > Developer > Edit Config. That opens
   `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
   Windows: `%APPDATA%\Claude\`). The user adds this block (also in
   `claude_desktop_config.example.json` here) and types their own keys into the file:

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "paste your paper key here, in this file only",
        "ALPACA_SECRET_KEY": "paste your paper secret here, in this file only",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,trading,stock-data"
      }
    }
  }
}
```

   The `env` block hands the keys to the server as environment variables.
   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes a key anyway, tell them
   to delete that message and make new keys in the Alpaca dashboard.
   `ALPACA_PAPER_TRADE` is already true by default; writing it makes it
   visible. `ALPACA_TOOLSETS` is optional and hides the tools they do not need.
4. Restart Claude Desktop.
5. **Set the tool permissions.** Customize > Connectors (Settings > Connectors
   in older versions), open alpaca, and for each tool pick Always allow, Needs
   approval or Blocked:
   - `place_stock_order`: **Needs approval**. Never Always allow.
   - every other tool that places, changes, cancels or closes anything
     (`cancel_order_by_id`, `replace_order_by_id`, `close_position`,
     `update_account_config`): Needs approval.
   - `place_option_order`, `place_crypto_order`, `close_all_positions`,
     `cancel_all_orders`, `exercise_options_position`,
     `do_not_exercise_options_position`: **Blocked** unless the user has a
     reason and says so.
   - read-only tools (`get_account_info`, `get_all_positions`, `get_open_position`,
     `get_orders`, `get_clock`, `get_stock_snapshot`, `get_stock_latest_quote`):
     Always allow is fine.
6. Check it: ask Claude for the Alpaca account info (read only) and confirm
   in the Alpaca dashboard that it is the paper account.

In Claude Code instead of Claude Desktop, keep the order tool on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__alpaca__place_stock_order"]}`,
and never put an order tool in `allow`.

**Bracket orders on Alpaca.** `place_stock_order` takes
`take_profit_limit_price` and `stop_loss_stop_price` (and an optional
`stop_loss_limit_price`). When either is there, the server sets
`order_class` to "bracket" by itself; trader-budget writes it anyway so the
ticket shows it. A bracket is whole shares only, with `time_in_force` day or
gtc. After the entry fills, the stop-loss and the take-profit wait at the
broker as two orders that cancel each other. They are day orders too, so they
end at the close: one more reason to be flat by then.

## Setup B: Interactive Brokers (connector in Claude's directory)

Interactive Brokers publishes a verified connector: claude.ai/directory/interactive-brokers
(connector URL `https://api.ibkr.com/v1/api/mcp`). Add it from Customize >
Connectors > Browse connectors and sign in to IBKR there, not in the chat.
Its listing says Claude can draft trade instructions that are sent to the
user's IBKR account for their review and approval, and that Claude never
submits orders directly to the market. So with IBKR the final yes happens in
IBKR as well. Set `"broker": "ibkr"` in `trader/config.json`. Set its tools
that draft orders to Needs approval too.

## Setup C: Trading 212 (`t212.py`, demo orders only)

`t212.py` reads a Trading 212 account over its public API (GET only), and its
`place` command sends one order, on the **demo** account only for now.

1. In the Trading 212 app, make an API key on the **demo** (practice) account.
   Reading needs account, portfolio, orders:read and metadata; `place` also
   needs orders:execute.
2. The user exports `T212_API_KEY` and `T212_API_SECRET` in their own shell.
   **Keys never go in the chat**, same rule as Alpaca.
3. Demo is the default, and demo runs exactly what live runs. Live needs
   `T212_ENV=live`, `--live`, **and** `"live_enabled": true` with `"paper": false`
   in `trader/config.json`; anything less is refused before a request is sent.
   Only the user switches it, with `trader-broker/go_live.py` and the checklist
   in `trader-broker/GO_LIVE.md`. Never run `go_live.py`, never edit those keys.

```bash
python3 "${CLAUDE_SKILL_DIR}/t212.py" status            # which account, is the key working
python3 "${CLAUDE_SKILL_DIR}/t212.py" cash               # available, reserved, in pies
python3 "${CLAUDE_SKILL_DIR}/t212.py" positions --json
python3 "${CLAUDE_SKILL_DIR}/t212.py" orders
python3 "${CLAUDE_SKILL_DIR}/t212.py" ticker TSLA        # TSLA -> TSLA_US_EQ
python3 "${CLAUDE_SKILL_DIR}/t212.py" instruments --refresh
python3 "${CLAUDE_SKILL_DIR}/t212.py" fx                 # GBP/USD -> trader/fx.json (Yahoo; no Trading 212 call)
```

It keeps each endpoint's documented rate limit, retries only a GET's 429, and
caches the instrument list for 24 hours in `trader/cache/`.

**Placing (demo and live alike).** `t212.py place N` places ticket N from `trader/orders.json`:

- only if trader-risk marked that exact ticket PASS today, and it is not
  already placed
- sized in GBP: the USD price at the current GBP/USD rate plus Trading 212's
  0.15% FX fee on the buy and on the sell at the stop. The loss at the stop
  never goes over `max_loss_per_trade_gbp` (or `max_loss_per_trade_usd`
  converted); it can cut the ticket's shares, never add to them
- it checks pending orders and positions for a duplicate first, shows the
  exact order and its GBP cost, and needs the user to **type yes in their own
  terminal**. It has no `--reply` and refuses without a terminal, so Claude
  cannot say yes for them. Tell the user to run it themselves; never run it.
- only a ticker on `allowed_tickers` (when the config has the list; the
  scanner only scans those and trader-risk blocks any other entry)
- a market entry, then, with `"stop_mode": "broker"` (the default), a separate
  **good-till-cancelled** stop order once the entry fills. With
  `"stop_mode": "market"` no stop order is sent: the guard sells at market when
  the price is at or under the stop (Trading 212's sources disagree on whether
  live takes stop orders; market mode works either way). A broker stop stays
  after the close, so a position you forget is still covered; cancel it in the
  app if you sell another way. In market mode the guard checks every minute
  and must be running for the stop to exist at all. Buys send
  a positive quantity, sells a negative one. Nothing is ever retried; after a
  failed or unclear send, `place N --resend` checks for a duplicate again
- every order and answer goes to `trader/logs/`, and each placement to
  `trader/state/broker.json`, so trader-risk counts it

**Stop guardian (demo and live alike).** `t212.py guard` looks at every open
position. In broker mode, when one has no pending stop, it places the stop of the `t212.py place`
ticket that bought it. `guard --watch` waits for the 9:30 open and checks
every 5 minutes (`--every N`) until 16:00 New York (`--until HH:MM`); it does
nothing on a weekend. It never buys: its one write is a SELL stop, good till
cancelled, built and sign-checked inside the guard, for no more shares than
the ticket bought and not already covered. In market mode it places no stop orders: at or under the stop (the ticket's,
or the trail's once it has risen) it sells the ticket's shares at market, once;
a refused sale is tried again on the next check, an unclear one never. It sends
nothing and says why when:

- the position has no ticket with a stop (it never invents a stop)
- the price is already at or under the stop (a stop then would sell at once)
- a sell is already pending, or an earlier guard stop came back unclear (it is
  never sent twice; check the app, then clear it in `trader/state/t212-guard.json`)

If Trading 212 rejects the key (401) or a permission (403), the guard stops at
once, writes a `GUARD ALERT` line to `trader/logs/guard.log` and shows a macOS
notification with a sound: it is protecting nothing until the key is fixed and
it is restarted. Three failed checks in a row (say, no network) raise the same
alert once, and another when it gets through again.

It also reports a pending sell stop with no position under it, and never
cancels anything except a stop it is about to raise (below).

**Trailing stop (demo, exits only, off by default).** In `trader/config.json`:

```json
"trailing_stop": {"enabled": true, "distance_r": 1.0, "sell_half_at_target": false}
```

`distance_r` is in R (the ticket's entry minus its first stop); `distance_pct`
works instead. On each check the guard keeps the highest price it has seen for
the position and raises its one pending stop to that high minus the distance,
rounded down to the cent. It never lowers a stop, never moves it to the price or
above, and skips a raise under 0.1R (or 5 cents) so it does not churn orders.
Trading 212 has no trailing order and no "modify", so a raise is: cancel the stop,
wait for it to leave the pending list, place the higher stop. A refused raise puts
the old stop back; an unclear one is never repeated and the guard leaves that
stock alone until the user checks the app. A stop it later has to put back comes
back at the trailed level, not the first one.

With `"sell_half_at_target": true`, the first check at or above the ticket's
target cancels the stop, sells half at market, and places a stop on the rest at
the trail. It happens once per ticket. Every order it sends is a sell; a buy is
refused before any request. Suggest a distance with `/trader-timemachine`
(`timemachine.py trail`), and say plainly when the study finds no edge. It runs unattended, so it needs no typed yes: that is the
one exception, and it only ever protects shares you already hold. Its log is
`trader/state/t212-guard.json` plus `trader/logs/`.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes", call `place_stock_order` with exactly the
   JSON the ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id> --filled-qty Q --filled-price P` (the fill from the
   response or `get_open_position`), or `--status denied`, or `--status failed`
   with `--note`. A failed order is not retried without a new check and a new yes.
   The log keeps the entry's max loss for trader-risk's daily limit.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The bracket's own orders do the stopping.
- When the stop-loss or the take-profit fills, record it:
  `ticket.py fill --symbol TSLA --side sell --qty 8 --price 358.32`.
- By `flat_by` (15:55 New York), anything still open gets closed:
  1. Ask Alpaca how many shares are held: `get_open_position` (read only).
  2. `ticket.py flatten --symbol TSLA --qty 8` writes a market sell for those
     shares to `trader/orders.json`.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. The bracket's stop-loss and take-profit still hold those shares, so
     Alpaca rejects a new sell until they are canceled: `get_orders` (status
     open, nested true) shows them; `cancel_order_by_id` each one (Needs
     approval). Then `place_stock_order` with the ticket, then `result`.

## Example: the reel

```
ORDER 1 of 1, PAPER account, Alpaca
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  take-profit 370.04
  max loss about $46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  why: opening-range breakout on TSLA: 8 shares, bound by your $50 max loss per trade (8 x 5.86 = $46.88 at the stop)
  READY: trader-risk PASS, waiting for your yes
  Claude will call place_stock_order with exactly:
    {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
     "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
```

The user replies "yes". Claude records it, calls `place_stock_order`, and the
card reads "Claude wants to use Place stock order" with that JSON and two
buttons: Allow once, Deny. Allow once sends it to the paper account. The
`why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never switches `ALPACA_PAPER_TRADE` or `"paper"` to live. The user does that themselves.
- Never calls `close_all_positions`, `cancel_all_orders`, option or crypto order tools on its own.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
