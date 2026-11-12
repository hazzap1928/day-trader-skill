# Your phone: tickets, approvals and alerts (demo only)

The autopilot sends each passing ticket to your phone with **Approve** and
**Reject** buttons. Nothing is placed without your Approve, and a ticket
expires after 10 minutes. Guard alerts, fills, exits and a daily summary come
to the same chat. Everything here runs on the **demo** account only.

## The app: Telegram

Install **Telegram** (free, iPhone or Android) and sign in. You create a
private bot that only this Mac talks to.

Why Telegram:

- **No open port.** The Mac only calls out to Telegram; nothing on the
  internet can reach the Mac.
- **Taps are tied to you.** Every tap carries your Telegram user id. The
  autopilot ignores taps and messages from anyone else, even if they find
  the bot.
- **Real buttons.** The buttons work from the notification's chat without
  any other app.

## Set it up (about 5 minutes)

1. **Make the bot.** In Telegram, open **@BotFather**, send `/newbot` and
   pick a name. BotFather replies with a **token** like `123456:ABC...`. Treat
   it like a password, and never paste it into the chat with Claude.
2. **Give the Mac the token.** Add this line to `~/.zshrc` yourself, then
   open a new terminal:
   ```bash
   export TELEGRAM_BOT_TOKEN='paste-your-token-here'
   ```
3. **Pair your phone.** Open your new bot in Telegram and send it `/start`.
   Then run:
   ```bash
   python3 trader-broker/phone.py setup
   ```
4. **Test the buttons.** No order is involved. Run this, then tap a button:
   ```bash
   python3 trader-broker/phone.py test
   ```
5. **Allow notifications.** Telegram must be allowed to notify you, with
   sound, in your phone's settings.

## Before you leave

- [ ] `allowed_tickers` in `trader/config.json`: the autopilot scans only
      these and refuses to start without the list.
- [ ] `python3 trader-broker/autopilot.py status` prints no "not starting"
      lines.
- [ ] **The Mac stays awake.** The `com.daytraderskill.caffeinate` launchd
      job runs `caffeinate -s` all the time, and `autopilot.py status` checks it.
      It only works on mains power, and **closing a laptop's lid still sleeps
      it** unless an external display, keyboard and power are attached. So keep
      it plugged in with the lid open. launchd cannot run anything on a
      sleeping Mac.
- [ ] Both jobs are loaded: the guard (`com.daytraderskill.t212-guard`) and
      the autopilot (`com.daytraderskill.autopilot`). Both start at 13:00 UK
      time on weekdays and wait for the 9:30 New York open.

## What you get on your phone

Every message is plain English for someone who is not a trader, written in one place:
`trader-broker/messages.py` (one function per message type; change the wording there). No R values, no
"stop"/"target" jargon, no order ids. Money in pounds, share prices in dollars. Sent with Telegram's HTML
formatting (bold); if Telegram rejects the formatting the same words go as plain text, and a message over
Telegram's 4,096-character limit is split between paragraphs. The logs and the reports in `trader/reports/`
keep the full detail.

| Message | When |
|---|---|
| 🔔 US market open | each trading day at the open: safety guard and autopilot running, what is held and its safety net |
| 👀 Buy X? + Approve / Reject | a trade passed every check (only when `auto_buy` is off) |
| 🟢 Bought X | once the buy has filled, with the real fill price: shares, cost in GBP, the safety net, the most it could lose, when the trail starts, and a one-line verdict from past similar trades (after fees, in pounds). BOUGHT and FILLED are one message |
| ⚠️ Couldn't buy X | an automatic buy failed a placing-time check: one plain sentence (the raw error stays in the log). Cash skips are summed up once per scan |
| 🔴/🟢 Sold X | a sale during the day (safety net hit, profit locked in), with the result in pounds from Trading 212's fills. Sales at the 15:50 close are in the evening report instead |
| 🛡️ Safety net | the guard set a missing safety net (a late fill), or raised it (once a day per stock) |
| ⚠️ safety guard problems | the guard cannot protect a position, or a sale at the close failed: what to do, in plain words |
| ☀️ Good morning | 07:30 UK on weekdays (`briefing.py`) |
| 📊 Day summary | about 15:55 New York: the day and week in pounds, each trade, past trades vs today, how the US market moved (`"report": {"market_line": true}`), tomorrow's earnings watchlist |
| 📊 Week summary | every Friday after the close while the forward test is on: on track or not, this week and since the start, against holding the S&P 500 |

The autopilot is **always on**: launchd starts `com.daytraderskill.autopilot` at login and restarts it if
it stops. Whenever the Mac is on it answers commands, weekends and before the open included. It also:
- **Sends the outbox:** delivers anything that could not be sent earlier.
- **Catches up the briefing:** on a trading day it reruns a morning briefing that never started or never
  got through, from 07:45 UK until the open.
- **Confirms the open:** at each open it sends a "US market open" message with the guard's and the
  autopilot's state and every open position with its safety net. It says so loudly if a position has none.

The briefing itself tries to send every 2 minutes for 30 minutes, then waits in the outbox.

From the chat, send `/pause` to stop new tickets and automatic buys (the guard keeps
protecting positions), `/resume` to start them again, and `/status` to see
the state.

## What an Approve does

When you tap Approve, the autopilot takes these steps in order and stops at
the first one that fails:

1. **Is the ticket still live?** It must be under 10 minutes old and still
   pending. A second tap does nothing.
2. **Fresh quote.** It refuses if the price has run more than 0.5R above the
   entry (`autopilot.max_chase_r`), or is at or under the stop.
3. **trader-risk again.** The check must still pass for the exact ticket you
   saw.
4. **Place it.** It runs the same `t212.py place` code as your typed yes, on
   the demo account, with the stop placed the same way.

The tickets each cycle builds are the scanner's picks under your rules. They
are not recommendations. This is demo money, education and tooling, not
financial advice.

## Turning it off

- **For the rest of the day:** send `/pause` to the bot.
- **For good:**
  ```bash
  launchctl bootout gui/$(id -u)/com.daytraderskill.autopilot
  ```
- **Cut the phone off at once:** revoke the token with @BotFather
  (`/revoke`). After that, no taps reach the Mac.
