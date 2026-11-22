#!/usr/bin/env python3
"""The agent's status for your desktop wallpaper (22 Nov 2026).

The always-on autopilot sends a short status to your own wallpaper service: every 15 minutes, once after
every buy or sale it sees, and once with status "stopped" when it shuts down. The address and the key live
in trader/.env (owner only; trader/ is git-ignored, so it is never committed):

    WALLPAPER_URL=https://<your-project>.supabase.co/functions/v1/wallpaper-status
    WALLPAPER_WRITE_KEY=<the key>

  - Either one missing: nothing is sent and nothing changes.
  - run-autopilot.sh hands them to the launchd autopilot under other names, so a terminal or a test run
    never sends.
  - The key is never printed or logged. A failed send is logged once (until one gets through) and never
    stops or slows anything: each send runs in the background with a short timeout.

What is sent: running or stopped, Practice or Live, the account value in GBP, its change since today's
open, the share held as cash, how many positions are open, the last trade (time and ticker), the last 30
values, and up to 3 build items (`"wallpaper": {"next": [...]}` in the config, else the list below).

By hand: python3 trader-broker/wallpaper.py show    (prints what would be sent; sends nothing)
"""
import json
import os
import sys
import threading
import time
import urllib.request

URL_ENV, KEY_ENV = "WALLPAPER_URL", "WALLPAPER_WRITE_KEY"                 # the lines in .env
SERVE_URL_ENV, SERVE_KEY_ENV = "AUTOPILOT_WALLPAPER_URL", "AUTOPILOT_WALLPAPER_KEY"   # what serve reads
EVERY = 900                 # seconds between regular updates
TIMEOUT = 8                 # seconds a send may take (in the background)
STOP_TIMEOUT = 4            # the "stopped" send at shutdown waits at most this long
TREND = 30                  # account values kept, oldest first
NEXT = [{"t": "News strategy: twice-daily reviews"},
        {"t": "Kronos backtest results"},
        {"t": "Telegram approval for sells"}]


def _post(url, key, body, timeout):     # tests replace this
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", "x-wallpaper-key": key,
                                          "User-Agent": "trading-status"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def address(environ=None, url_name=SERVE_URL_ENV, key_name=SERVE_KEY_ENV):
    """(url, key), or (None, None) unless both are set and the address is https."""
    env = environ if environ is not None else os.environ
    u, k = (env.get(url_name) or "").strip(), (env.get(key_name) or "").strip()
    return (u, k) if u.startswith("https://") and k else (None, None)


def next_items(cfg):
    items = ((cfg or {}).get("wallpaper") or {}).get("next")
    if not isinstance(items, list):
        return list(NEXT)
    out = []
    for it in items:
        if isinstance(it, str) and it.strip():
            it = {"t": it.strip()}
        if isinstance(it, dict) and str(it.get("t") or "").strip():
            out.append({"t": str(it["t"]).strip()[:80], **({"issue": True} if it.get("issue") else {})})
    return out[:3]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def account_fields(summary, positions, start_value):
    """value, day change, cash share and positions from Trading 212's summary and positions (GBP account)."""
    s = summary or {}
    value = _num(s.get("totalValue"))
    if value is None:
        return {}
    cash = s.get("cash") or {}
    held_cash = (_num(cash.get("availableToTrade")) or 0.0) + (_num(cash.get("reservedForOrders")) or 0.0)
    start = _num(start_value)
    change = value - start if start else 0.0
    return {"value": round(value, 2),
            "day_change": round(change, 2),
            "day_change_pct": round(change / start * 100, 2) if start else 0.0,
            "cash_pct": int(round(held_cash / value * 100)) if value > 0 else 0,
            "positions": sum(1 for p in positions or [] if (_num(p.get("quantity")) or 0) > 1e-9)}


class Reporter:
    def __init__(self, url, key, state_path=None, clock=time.time, log=None, mode="Practice"):
        self.url, self.key, self.path, self.clock = url, key, state_path, clock
        self.log = log or (lambda text: None)
        self.mode = mode
        self.tried = None
        self.fails = 0
        self.said_ok = False
        self._busy = threading.Lock()

    @property
    def on(self):
        return bool(self.url and self.key)

    def due(self):
        return self.on and (self.tried is None or self.clock() - self.tried >= EVERY)

    def _trend(self, value):
        st = {}
        if self.path:
            try:
                with open(self.path) as fh:
                    st = json.load(fh) or {}
            except (OSError, ValueError):
                st = {}
        trend = [v for v in st.get("trend") or [] if isinstance(v, (int, float))]
        if value is not None:
            trend = (trend + [value])[-TREND:]
            if self.path:
                try:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    tmp = self.path + ".tmp"
                    with open(tmp, "w") as fh:
                        json.dump({"trend": trend}, fh)
                    os.replace(tmp, self.path)
                except OSError:
                    pass
        return trend

    def body(self, fields):
        data = {"status": "running", "mode": self.mode}
        data.update(fields or {})
        if "value" in data:
            data["trend"] = self._trend(data["value"])
        return {"project": "trading", "data": data}

    def _send(self, body, timeout):
        try:
            status = _post(self.url, self.key, body, timeout)
            ok = 200 <= int(status or 0) < 300
            why = "HTTP %s" % status
        except Exception as e:                       # never the key: only the kind of failure
            ok, why = False, getattr(e, "code", None) and "HTTP %s" % e.code or type(e).__name__
        if ok:
            if self.fails or not self.said_ok:
                self.log("wallpaper: status sent%s" % (" again" if self.fails else ""))
            self.fails, self.said_ok = 0, True
        else:
            self.fails += 1
            if self.fails == 1:
                self.log("wallpaper: status send failed (%s); carrying on, trying again later" % why)
        return ok

    def send(self, fields, wait=False):
        """Send in the background (or, with wait, here, for the shutdown). Returns "started", "sent",
        "failed", "busy" (one still in flight: this one is dropped) or None (no address). Never raises."""
        if not self.on:
            return None
        self.tried = self.clock()
        try:
            body = self.body(fields)
        except Exception as e:
            self.log("wallpaper: status not built (%s)" % type(e).__name__)
            return "failed"
        if wait:
            return "sent" if self._send(body, STOP_TIMEOUT) else "failed"
        if not self._busy.acquire(blocking=False):
            return "busy"

        def run():
            try:
                self._send(body, TIMEOUT)
            finally:
                self._busy.release()
        try:
            threading.Thread(target=run, name="wallpaper", daemon=True).start()
        except Exception:
            self._busy.release()
            return "failed"
        return "started"

    def stopped(self):
        return self.send({"status": "stopped"}, wait=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["show"]:
        print(__doc__.strip())
        return 2
    url, key = address(None, URL_ENV, KEY_ENV)
    print("address and key: %s" % ("set" if url else "not set (see the top of this file)"))
    print(json.dumps(Reporter("https://x", "x").body({"next": NEXT}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
