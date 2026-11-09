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

| Message | When |
|---|---|
| Autopilot started | each trading day at the open |
| DEMO TICKET + Approve / Reject | a ticket passed every check (only when `auto_buy` is off) |
| BOUGHT (demo, automatic) | with `"autopilot": {"auto_buy": true}`: what was bought, the cost and max loss in GBP, the stop, the target and the Time Machine's odds |
| PLACED / NOT PLACED | after your Approve, or when an automatic buy failed a placing-time check (and why) |
| FILLED / EXIT | the positions changed (checked each minute) |
| GUARD / GUARD ALERT | the guard placed a stop, trailed it or sold; or it cannot protect you |
| END-OF-DAY CLOSE | 15:50 New York: the guard sold every open position (`eod_close`) and lists them; a failed sale is a loud GUARD ALERT |
| MORNING BRIEFING | 07:30 UK on weekdays (`briefing.py`) |
| CLOSING REPORT | about 15:55 New York: each trade with entry, exit, reason and P&L after fees; day and week totals; win rate; account; anything open; results against the Time Machine |

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
