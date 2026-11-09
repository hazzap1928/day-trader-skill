#!/usr/bin/env python3
"""Is the internet there? And waiting for it to come back.

Used by the briefing, the guard and the autopilot. When one of them cannot reach
the internet it checks again every 2 minutes, for up to 30 minutes. When the
connection comes back it tells your phone what was missed. A message that could
not be sent waits in the phone outbox (trader/state/phone-outbox.json) and goes
out with the next one that gets through.

    python3 trader-broker/netwait.py      # online or offline, from here, now
"""

import datetime
import json
import os
import socket
import sys
import time

RETRY_EVERY = 120          # seconds between checks while offline
GIVE_UP_AFTER = 30 * 60    # how long to keep trying before giving up
HOSTS = (("api.telegram.org", 443), ("demo.trading212.com", 443), ("query1.finance.yahoo.com", 443))
CONNECT_TIMEOUT = 5


def _connect(host, port, timeout):          # tests replace this
    socket.create_connection((host, port), timeout=timeout).close()


def online():
    """True when any of the hosts the pack needs answers a TCP connection on 443."""
    for host, port in HOSTS:
        try:
            _connect(host, port, CONNECT_TIMEOUT)
            return True
        except OSError:
            continue
    return False


def wait_online(sleep, now, every=RETRY_EVERY, limit=GIVE_UP_AFTER, deadline=None, log=None, label="",
                probe=None):
    """Check every `every` seconds until online, `limit` seconds have passed, or `deadline` (a
    timestamp) is reached. Returns {"ok", "start", "end", "tries", "gave_up"}."""
    probe = probe or online
    start = now()
    tries = 0
    while True:
        tries += 1
        if probe():
            return {"ok": True, "start": start, "end": now(), "tries": tries, "gave_up": False}
        waited = now() - start
        if waited >= limit or (deadline is not None and now() + every > deadline):
            return {"ok": False, "start": start, "end": now(), "tries": tries,
                    "gave_up": waited >= limit}
        if log:
            log("%s offline (check %d, %d min so far); trying again in %d min"
                % (label or "the internet", tries, waited // 60, every // 60))
        sleep(every)


def hm(ts):
    """A timestamp as the Mac's own clock (UK time), HH:MM."""
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def minutes(a, b):
    return max(0, int(round((b - a) / 60.0)))


def record(state_dir, what, start, end, missed, gave_up=False):
    """One line per outage in trader/logs/outages.jsonl."""
    path = os.path.join(state_dir, "logs", "outages.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"what": what, "start": start, "end": end, "minutes": minutes(start, end),
                                 "missed": list(missed), "gave_up": bool(gave_up)}) + "\n")
    except OSError:
        pass


def back_online(what, start, end, missed, extra=None):
    """The message for your phone when the connection is back."""
    lines = ["BACK ONLINE: %s had no internet from %s to %s (%d min)." % (what, hm(start), hm(end), minutes(start, end))]
    if missed:
        lines.append("Missed: " + "; ".join(missed) + ".")
    else:
        lines.append("Nothing was missed.")
    lines += list(extra or [])
    return "\n".join(lines)


def main():
    print("online" if online() else "OFFLINE: none of %s answered" % ", ".join(h for h, _ in HOSTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
