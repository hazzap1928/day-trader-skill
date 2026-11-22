#!/usr/bin/env python3
"""A heartbeat to an outside service, so you hear when this Mac (or the autopilot) goes quiet.

Telegram cannot tell you the Mac is down, because the Mac sends those messages. An outside check
service can: the autopilot pings a private address every few minutes, and when the pings stop the
service messages you (email, or Telegram if you connect it there). healthchecks.io is free for this.

    export HEALTHCHECK_URL=https://hc-ping.com/<your-check-id>     # in ~/.zshrc, beside the other keys

  - No address set: nothing is sent and nothing changes.
  - The address is private (anyone with it can ping your check), so it is never printed or logged.
  - A ping is a plain GET with nothing about you, your account or your trades in it.
  - A failed ping never stops anything; the next one is tried a few minutes later.

By hand: python3 trader-broker/heartbeat.py ping    (one ping now; says "sent" or why not)
"""
import os
import sys
import time
import urllib.request

ENV = "HEALTHCHECK_URL"            # the line you put in ~/.zshrc
SERVE_ENV = "AUTOPILOT_HEARTBEAT_URL"   # what run-autopilot.sh hands the autopilot: no shell (and so no test
                                        # run from a terminal) has it, so only the launchd autopilot pings
EVERY = 300                 # seconds between pings (set the check's period to 5 minutes)
TIMEOUT = 10


def _open(url, timeout):    # tests replace this
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "heartbeat"}),
                                timeout=timeout) as r:
        return r.status


def url_from(environ=None, name=ENV):
    """The ping address, or None when unset. Only https addresses are used."""
    u = ((environ if environ is not None else os.environ).get(name) or "").strip()
    return u if u.startswith("https://") else None


class Heartbeat:
    def __init__(self, url, every=EVERY, clock=time.time):
        self.url, self.every, self.clock = url, every, clock
        self.last = None            # when the last ping went through
        self.tried = None           # when the last ping was tried
        self.fails = 0

    def due(self):
        return bool(self.url) and (self.tried is None or self.clock() - self.tried >= self.every)

    def beat(self):
        """Ping if one is due. Returns "sent", "failed" or None (not due / no address). Never raises,
        and never puts the address in what it returns."""
        if not self.due():
            return None
        self.tried = self.clock()
        try:
            status = _open(self.url, TIMEOUT)
            ok = 200 <= int(status or 0) < 300
        except Exception:
            ok = False
        if ok:
            self.last, self.fails = self.tried, 0
            return "sent"
        self.fails += 1
        return "failed"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["ping"]:
        print(__doc__.strip())
        return 2
    url = url_from()
    if not url:
        print("No heartbeat address: set %s=https://... in ~/.zshrc (see the top of this file)." % ENV)
        return 1
    res = Heartbeat(url).beat()
    print("Heartbeat %s." % res)
    return 0 if res == "sent" else 1


if __name__ == "__main__":
    sys.exit(main())
