#!/usr/bin/env python3
"""Trading 212 adapter for trader-broker. Standard library only.

Every command but `place` sends GET requests only. `place` is the one way to
send an order, and only on demo for now. Endpoints and rate limits are from
the official reference at docs.trading212.com (OpenAPI bundle checked
2026-11-05):

  GET  /api/v0/equity/account/summary      1 req / 5s
  GET  /api/v0/equity/positions            1 req / 1s
  GET  /api/v0/equity/orders               1 req / 5s
  GET  /api/v0/equity/metadata/instruments 1 req / 50s
  POST /api/v0/equity/orders/market        50 req / 1m   (place only)
  POST /api/v0/equity/orders/stop          1 req / 2s    (place only)

Commands (each takes --json):

    python3 t212.py status                  # which account, is the key working
    python3 t212.py cash                    # free cash, reserved, in pies
    python3 t212.py info                    # account id, currency, totals
    python3 t212.py fx                      # save GBP/USD to trader/fx.json (no Trading 212 call)
    python3 t212.py guard                   # DEMO: one pass, add any missing ticket stop
    python3 t212.py guard --watch           # DEMO: from the open to the close, every 5 minutes
    python3 t212.py positions               # open positions
    python3 t212.py orders                  # pending orders
    python3 t212.py ticker TSLA             # TSLA -> TSLA_US_EQ
    python3 t212.py instruments --refresh   # re-download the instrument list
    python3 t212.py place 1                 # DEMO: place ticket 1 after your typed yes

Demo (paper money) is the default: https://demo.trading212.com/api/v0. Live
is used only when T212_ENV=live AND --live is passed. Any mismatch is refused
before a request is sent.

place <ticket-id> (ticket number from trader/orders.json, or its fingerprint):
  - only a ticket trader-risk marked PASS today, not already placed
  - sizes in GBP: the USD price converted at the current GBP/USD rate, plus
    Trading 212's 0.15% FX fee on the buy and on the sell at the stop; the
    max loss at the stop never goes over max_loss_per_trade_gbp (or
    max_loss_per_trade_usd converted). It can cut the ticket's quantity,
    never raise it
  - checks pending orders and positions for a duplicate before sending
  - shows the exact order and needs you to type "yes" in your own terminal
    (it refuses to run without one, so nothing else can say yes for you)
  - buys send a positive quantity, sells a negative one (Trading 212's rule);
    a side and a sign that disagree are refused before sending
  - sends each order once: no retry, ever. After a failed or unclear send,
    `place ID --resend` checks for a duplicate again before sending
  - the same on demo and live: a market entry, then (stop_mode "broker", the
    default) a separate good-till-cancelled stop once the entry fills, or
    (stop_mode "market") no stop order: t212.py guard sells at market when the
    price is at or under the stop
  - live needs all of: T212_ENV=live, --live, and "live_enabled": true with
    "paper": false in trader/config.json (trader-broker/go-live.sh sets them)
  - logs every order and response (never credentials) to trader/logs/

Credentials come from T212_API_KEY and T212_API_SECRET (HTTP Basic: key as
user name, secret as password). They are never printed, logged or written to
disk, and they are scrubbed from every error message.

State lives under trader/ (override with --state-dir):
  trader/state/t212-ratelimit.json        when each endpoint was last called
  trader/cache/t212-instruments-ENV.json  the instrument list, kept 24 hours
  trader/logs/t212-orders-DATE.jsonl      every order sent and every answer
  trader/state/broker.json                place records, as ticket.py does
"""

import argparse
import base64
import calendar
import datetime
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URLS = {"demo": "https://demo.trading212.com/api/v0",
             "live": "https://live.trading212.com/api/v0"}
SUMMARY = "/equity/account/summary"
POSITIONS = "/equity/positions"
ORDERS = "/equity/orders"
INSTRUMENTS = "/equity/metadata/instruments"
MARKET = "/equity/orders/market"
STOP = "/equity/orders/stop"
CANCEL = "/equity/orders/{id}"      # DELETE: the guardian cancels a stop only to raise it (demo)
HISTORY = "/equity/history/orders"  # filled and finished orders, with fills and fees (read-only)
READ_PATHS = (SUMMARY, POSITIONS, ORDERS, INSTRUMENTS, HISTORY)
PLACE_PATHS = (MARKET, STOP)
# seconds between calls, from each endpoint's documented "N req / Ts"
MIN_INTERVAL = {SUMMARY: 5.0, POSITIONS: 1.0, ORDERS: 5.0, INSTRUMENTS: 50.0, MARKET: 1.2, STOP: 2.0,
                CANCEL: 1.2, HISTORY: 10.0}
# the scope each endpoint's 403 names in the reference
SCOPES = {SUMMARY: "account", POSITIONS: "portfolio", ORDERS: "orders:read", INSTRUMENTS: "metadata",
          MARKET: "orders:execute", STOP: "orders:execute", CANCEL: "orders:execute", HISTORY: "history:orders"}
# A code-level master switch, left False: live is switched on in trader/config.json
# ("live_enabled": true and "paper": false, see go-live.sh), never here.
LIVE_PLACEMENT_ENABLED = False
STOP_MODES = ("broker", "market")
STOP_VALIDITY = "GOOD_TILL_CANCEL"   # the stop outlives the session: a forgotten position stays protected
FX_FEE = 0.0015          # Trading 212's FX fee, Invest and Stocks ISA, on each conversion
FX_URL = "https://query2.finance.yahoo.com/v8/finance/chart/GBPUSD=X?range=1d&interval=1d"
FX_MAX_AGE = 4 * 24 * 3600   # a weekend's gap is fine, an older rate is not
PRICE_FRESH = 15 * 60        # trader/prices.json counts if under 15 minutes old
FILL_POLLS = 10              # positions checks (1 per second) waiting for the entry to fill
CACHE_TTL = 24 * 3600
MAX_RETRIES = 3          # extra tries after a 429, and only after a 429
MAX_WAIT = 65.0          # longest single wait for the rate limiter
TIMEOUT = 30
USER_AGENT = "day-trader-skill-t212/1"


class T212Error(Exception):
    """A failure to report to the user. Exit code 2 means refused before any request."""

    def __init__(self, msg, code=1, status=None, network=False):
        Exception.__init__(self, msg)
        self.code = code
        self.status = status      # the HTTP status when Trading 212 answered with an error
        self.network = network    # True when Trading 212 could not be reached at all


class PlaceError(T212Error):
    """An order POST that did not come back 200. outcome is "rejected" (Trading 212
    said no, so nothing was placed) or "unknown" (it may have been placed)."""

    def __init__(self, msg, outcome, status=None):
        T212Error.__init__(self, msg, 1)
        self.outcome = outcome
        self.status = status


INSUFFICIENT_FUNDS = "insufficient-free-for-stocks-buy"   # Trading 212's error type for a buy with too little cash


POSITION_CAP_ROOM = 1.01        # size_entry's max_position_pct net: 1 % over the cap trader-budget sized to


class NoCash(T212Error):
    """A buy the free cash does not cover. Raised before anything is sent (the autopilot's cash check)."""

    def __init__(self, msg, info=None):
        T212Error.__init__(self, msg, 2)
        self.info = info or {}


def is_no_cash(e):
    """True for the autopilot's own cash check and for Trading 212's insufficient-funds refusal."""
    return isinstance(e, NoCash) or INSUFFICIENT_FUNDS in str(e)


# ---------------------------------------------------------------- environment

def pick_env(live_flag, environ=None):
    """demo unless T212_ENV=live and --live were both given. Anything else is refused."""
    environ = os.environ if environ is None else environ
    env = (environ.get("T212_ENV") or "demo").strip().lower()
    if env not in BASE_URLS:
        raise T212Error("T212_ENV must be demo or live, not %r. Nothing was sent." % env, 2)
    if env == "live" and not live_flag:
        raise T212Error("T212_ENV=live but --live was not passed. Refusing to use the live account; "
                        "nothing was sent.", 2)
    if live_flag and env != "live":
        raise T212Error("--live was passed but T212_ENV is not live. Refusing to use the live account; "
                        "nothing was sent.", 2)
    return env


def credentials(environ=None):
    environ = os.environ if environ is None else environ
    key = environ.get("T212_API_KEY") or ""
    secret = environ.get("T212_API_SECRET") or ""
    missing = [n for n, v in (("T212_API_KEY", key), ("T212_API_SECRET", secret)) if not v.strip()]
    if missing:
        raise T212Error("%s not set. Export them in your shell (never paste keys into the chat). "
                        "Nothing was sent." % " and ".join(missing), 2)
    return key.strip(), secret.strip()


def _secrets(key, secret):
    pair = "%s:%s" % (key, secret)
    token = base64.b64encode(pair.encode("utf-8")).decode("ascii")
    return [s for s in (pair, token, key, secret) if s]


def scrub(text, secrets):
    """Remove every credential form (key, secret, key:secret, its base64) from text."""
    text = "" if text is None else str(text)
    for s in sorted(secrets, key=len, reverse=True):
        if s:
            text = text.replace(s, "[redacted]")
    return text


# ---------------------------------------------------------------- state files

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header somewhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def _sleep(seconds):
    time.sleep(seconds)


def _now():
    return time.time()


def live_switch(cfg):
    """Live orders only with "live_enabled": true AND "paper": false in the config."""
    cfg = cfg or {}
    return cfg.get("live_enabled") is True and cfg.get("paper") is False


def stop_mode(cfg):
    """"broker": a real stop order at Trading 212. "market": the guard sells at market when the
    price crosses the stop. The same on demo and live."""
    m = str((cfg or {}).get("stop_mode") or "broker").strip().lower()
    if m not in STOP_MODES:
        raise T212Error("stop_mode in trader/config.json must be \"broker\" or \"market\", not %r"
                        % (cfg or {}).get("stop_mode"), 2)
    return m


def blocked_tickers(cfg):
    """blocked_tickers as a set: never bought, whatever the allow-list says."""
    b = (cfg or {}).get("blocked_tickers")
    if b is None:
        return set()
    if not isinstance(b, list) or not all(isinstance(x, str) and x.strip() for x in b):
        raise T212Error("blocked_tickers in trader/config.json must be a list of tickers", 2)
    return {x.strip().upper() for x in b}


def allowed_tickers(cfg):
    """The allow-list minus blocked_tickers, as a set of plain tickers, or None when there is none."""
    a = (cfg or {}).get("allowed_tickers")
    if a is None:
        return None
    if not isinstance(a, list) or not all(isinstance(x, str) and x.strip() for x in a):
        raise T212Error("allowed_tickers in trader/config.json must be a list of tickers", 2)
    return {x.strip().upper() for x in a} - blocked_tickers(cfg)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Client(object):
    """GET for reading. post_order is the one write: the two order endpoints,
    one attempt, never retried, sign checked against the side."""

    def __init__(self, env, key, secret, state_dir="trader", log=None):
        self.env = env
        self.live_ok = False        # set from the config by main(); see live_switch
        self.base = BASE_URLS[env]
        self._secrets = _secrets(key, secret)
        self._auth = "Basic " + self._secrets[1]
        self.rate_path = os.path.join(state_dir, "state", "t212-ratelimit.json")
        self.state_dir = state_dir
        self.environ = None         # main() sets it; the phone token is read from it
        self.log = log or (lambda msg: sys.stderr.write(msg + "\n"))
        self.last_headers = {}

    def _clean(self, text):
        return scrub(text, self._secrets)

    # rate limiting: documented spacing per endpoint, plus x-ratelimit-* from the last reply
    def _wait_turn(self, path):
        state = _read_json(self.rate_path, {}).get(self.env, {}).get(path, {})
        now = _now()
        ready = max(_num(state.get("last")) or 0.0, 0.0) + MIN_INTERVAL.get(path, 1.0)
        if state.get("remaining") == 0 and _num(state.get("reset")):
            ready = max(ready, _num(state["reset"]))
        wait = ready - now
        if wait > 0:
            if wait > MAX_WAIT:
                wait = MAX_WAIT
            self.log("waiting %.0fs for the Trading 212 rate limit on %s" % (wait, path))
            _sleep(wait)

    def _remember(self, path, headers):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        self.last_headers = {k: h[k] for k in h if k.startswith("x-ratelimit-")}
        all_state = _read_json(self.rate_path, {})
        entry = {"last": _now()}
        for name in ("limit", "period", "remaining", "reset", "used"):
            v = _num(h.get("x-ratelimit-" + name))
            if v is not None:
                entry[name] = int(v) if v == int(v) else v
        all_state.setdefault(self.env, {})[path] = entry
        _write_json(self.rate_path, all_state)

    def _backoff(self, headers, attempt):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        reset = _num(h.get("x-ratelimit-reset"))
        if reset is not None and reset - _now() > 0:
            wait = reset - _now() + 0.5
        elif _num(h.get("retry-after")) is not None:
            wait = _num(h.get("retry-after"))
        else:
            wait = 2.0 ** attempt
        return min(max(wait, 1.0), MAX_WAIT)

    def get(self, path, query=None):
        if path not in READ_PATHS:
            raise T212Error("unknown endpoint %s" % path)
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        for attempt in range(MAX_RETRIES + 1):
            self._wait_turn(path)
            req = urllib.request.Request(url, method="GET", headers={
                "Authorization": self._auth, "Accept": "application/json", "User-Agent": USER_AGENT})
            try:
                resp = _open(req, TIMEOUT)
            except urllib.error.HTTPError as e:
                headers = dict(e.headers.items()) if e.headers is not None else {}
                self._remember(path, headers)
                if e.code == 429 and attempt < MAX_RETRIES:
                    wait = self._backoff(headers, attempt)
                    self.log("Trading 212 said 429 (rate limited) on %s; retry %d of %d in %.0fs"
                             % (path, attempt + 1, MAX_RETRIES, wait))
                    _sleep(wait)
                    continue
                raise T212Error(self._http_message(path, e), status=e.code)
            except urllib.error.URLError as e:
                raise T212Error(self._clean("could not reach %s: %s" % (self.base, e.reason)), network=True)
            except OSError as e:
                raise T212Error(self._clean("could not reach %s: %s" % (self.base, e)), network=True)
            try:
                headers = dict(resp.headers.items()) if resp.headers is not None else {}
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
            finally:
                resp.close()
            self._remember(path, headers)
            if status != 200:
                raise T212Error("Trading 212 answered %s on GET %s (expected 200)" % (status, path))
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError:
                raise T212Error("Trading 212 sent something that is not JSON for GET %s" % path)
        raise T212Error("still rate limited after %d retries on GET %s" % (MAX_RETRIES, path))

    def post_order(self, path, body, side):
        """Send one order, once. Returns (status, Order JSON). Never retries, not
        even on a 429: the order endpoints are not idempotent. Raises PlaceError."""
        if path not in PLACE_PATHS:
            raise T212Error("unknown order endpoint %s" % path)
        if self.env != "demo" and not (self.live_ok or LIVE_PLACEMENT_ENABLED):
            raise T212Error("live trading is switched off (trader-broker/go-live.sh switches it on); "
                            "nothing was sent", 2)
        check_sign(body, side)
        self._wait_turn(path)
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=json.dumps(body, sort_keys=True).encode("utf-8"), headers={
                                         "Authorization": self._auth, "Accept": "application/json",
                                         "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            resp = _open(req, TIMEOUT)
        except urllib.error.HTTPError as e:
            headers = dict(e.headers.items()) if e.headers is not None else {}
            self._remember(path, headers)
            # a 4xx is a refusal (nothing placed); 408, 5xx and redirects leave it unclear
            outcome = "rejected" if 400 <= e.code < 500 and e.code != 408 else "unknown"
            raise PlaceError(self._http_message(path, e, "POST"), outcome, e.code)
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise PlaceError(self._clean("no answer from %s for POST %s: %s" % (self.base, path, reason)),
                             "unknown")
        try:
            headers = dict(resp.headers.items()) if resp.headers is not None else {}
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
        finally:
            resp.close()
        self._remember(path, headers)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise PlaceError("Trading 212 answered %s to POST %s with something that is not JSON"
                             % (status, path), "unknown", status)
        if status != 200:
            raise PlaceError("Trading 212 answered %s to POST %s (expected 200)" % (status, path),
                             "unknown", status)
        return status, data

    def cancel_order(self, order_id):
        """Cancel one pending order, once, on the DEMO account only. Raises PlaceError."""
        if self.env != "demo" and not (self.live_ok or LIVE_PLACEMENT_ENABLED):
            raise T212Error("live trading is switched off; nothing was cancelled", 2)
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            raise T212Error("refusing to cancel: %r is not an order id" % (order_id,), 2)
        self._wait_turn(CANCEL)
        req = urllib.request.Request(self.base + CANCEL.replace("{id}", str(order_id)), method="DELETE", headers={
            "Authorization": self._auth, "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            resp = _open(req, TIMEOUT)
        except urllib.error.HTTPError as e:
            self._remember(CANCEL, dict(e.headers.items()) if e.headers is not None else {})
            outcome = "rejected" if 400 <= e.code < 500 and e.code != 408 else "unknown"
            raise PlaceError(self._http_message(CANCEL, e, "DELETE"), outcome, e.code)
        except (urllib.error.URLError, OSError) as e:
            raise PlaceError(self._clean("no answer from %s for DELETE %s: %s"
                                         % (self.base, CANCEL, getattr(e, "reason", e))), "unknown")
        try:
            headers = dict(resp.headers.items()) if resp.headers is not None else {}
            status = getattr(resp, "status", None) or resp.getcode()
            resp.read()
        finally:
            resp.close()
        self._remember(CANCEL, headers)
        if status != 200:
            raise PlaceError("Trading 212 answered %s to DELETE %s (expected 200)" % (status, CANCEL),
                             "unknown", status)
        return status

    def _http_message(self, path, e, method="GET"):
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        if e.code == 401:
            msg = "401: Trading 212 rejected the API key or secret for the %s account" % self.env
        elif e.code == 403:
            msg = "403: the API key is missing the '%s' permission (scope) for %s %s" % (
                SCOPES.get(path, "?"), method, path)
        elif e.code == 408:
            msg = "408: Trading 212 timed out on %s %s" % (method, path)
        elif e.code == 429 and method == "GET":
            msg = "429: still rate limited on GET %s after %d retries" % (path, MAX_RETRIES)
        elif e.code == 429:
            msg = "429: rate limited on POST %s; not retried" % path
        elif 300 <= e.code < 400:
            msg = "%d: Trading 212 tried to redirect %s %s; not followed" % (e.code, method, path)
        else:
            msg = "%d: Trading 212 error on %s %s" % (e.code, method, path)
        if detail:
            msg += " (%s)" % detail[:300]
        return self._clean(msg)


# ---------------------------------------------------------------- instruments

def cache_path(state_dir, env):
    return os.path.join(state_dir, "cache", "t212-instruments-%s.json" % env)


def load_instruments(client, state_dir, refresh=False):
    """The instrument list, from the 24-hour cache unless it is stale or refresh is asked."""
    path = cache_path(state_dir, client.env)
    cached = _read_json(path, None)
    if not refresh and isinstance(cached, dict) and isinstance(cached.get("instruments"), list):
        age = _now() - (_num(cached.get("fetched_at")) or 0)
        if 0 <= age < CACHE_TTL:
            return cached, False
    data = client.get(INSTRUMENTS)
    if not isinstance(data, list):
        raise T212Error("the instrument list from Trading 212 was not a list")
    cached = {"env": client.env, "fetched_at": _now(), "instruments": data}
    _write_json(path, cached)
    return cached, True


def _is_us(inst):
    return str(inst.get("ticker", "")).upper().endswith("_US_EQ")


def resolve(symbol, instruments):
    """TSLA -> the TSLA_US_EQ instrument. Prefers the US listing; ambiguity is an error."""
    s = (symbol or "").strip().upper()
    if not s:
        raise T212Error("give a symbol, e.g. TSLA", 2)
    for inst in instruments:
        if str(inst.get("ticker", "")).upper() == s:
            return inst
    us_ticker = s.replace(".", "_").replace("/", "_").replace("-", "_") + "_US_EQ"
    found, seen = [], set()
    for inst in instruments:
        t = str(inst.get("ticker", ""))
        if (str(inst.get("shortName") or "").upper() == s or t.upper() == us_ticker) and t not in seen:
            seen.add(t)
            found.append(inst)
    if not found:
        raise T212Error("no Trading 212 instrument matches %s. If it is newly listed, run "
                        "'t212.py instruments --refresh'." % s)
    us = [i for i in found if _is_us(i)]
    if len(us) == 1:
        return us[0]
    if not us and len(found) == 1:
        return found[0]
    pool = us or found
    lines = ["  %s  %s  (%s, %s)" % (i.get("ticker"), i.get("name", ""), i.get("currencyCode", "?"),
                                     i.get("type", "?")) for i in pool]
    raise T212Error("%s is ambiguous on Trading 212; pass one of these tickers instead:\n%s"
                    % (s, "\n".join(lines)))


# ---------------------------------------------------------------- output

def _fmt(v, places=2):
    if v is None or v == "":
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return ("{:,.%df}" % places).format(v)
    return str(v)


def _ident(v):
    """Account and order ids: as given, never with commas or decimals."""
    return "-" if v is None or v == "" else str(v)


def _qty(v):
    if isinstance(v, (int, float)):
        return ("%.6f" % v).rstrip("0").rstrip(".")
    return _fmt(v)


def _table(rows, headers):
    if not rows:
        return ""
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    out = ["  ".join(str(c).ljust(w) for c, w in zip(headers, widths)).rstrip()]
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(out)


def _env_line(client):
    label = "DEMO (paper money)" if client.env == "demo" else "LIVE (REAL MONEY)"
    return "Trading 212 %s  %s" % (label, client.base)


# ---------------------------------------------------------------- commands

def cmd_status(client, args):
    s = client.get(SUMMARY)
    rl = client.last_headers
    data = {"env": client.env, "base_url": client.base, "connected": True,
            "account_id": s.get("id"), "currency": s.get("currency"),
            "total_value": s.get("totalValue"),
            "rate_limit": {k[len("x-ratelimit-"):]: v for k, v in sorted(rl.items())}}
    if args.json:
        return data
    lines = [_env_line(client),
             "connected: yes, account %s (%s)" % (_ident(s.get("id")), s.get("currency") or "?"),
             "total value: %s %s" % (_fmt(s.get("totalValue")), s.get("currency") or "")]
    if rl:
        lines.append("rate limit on account/summary: %s of %s left per %ss"
                     % (rl.get("x-ratelimit-remaining", "?"), rl.get("x-ratelimit-limit", "?"),
                        rl.get("x-ratelimit-period", "?")))
    lines.append("orders: only place (after your typed yes) and guard (exits only) send any%s"
                 % ("" if client.env == "demo" else
                    "; live trading is %s" % ("ON" if (client.live_ok or LIVE_PLACEMENT_ENABLED) else "OFF")))
    return "\n".join(lines)


def cmd_cash(client, args):
    s = client.get(SUMMARY)
    cash = s.get("cash") or {}
    if args.json:
        return {"env": client.env, "currency": s.get("currency"), "cash": cash}
    cur = s.get("currency") or ""
    return "\n".join([_env_line(client),
                      "available to trade:   %s %s" % (_fmt(cash.get("availableToTrade")), cur),
                      "reserved for orders:  %s %s" % (_fmt(cash.get("reservedForOrders")), cur),
                      "in pies (uninvested): %s %s" % (_fmt(cash.get("inPies")), cur)])


def cmd_info(client, args):
    s = client.get(SUMMARY)
    if args.json:
        return dict(s, env=client.env)
    inv = s.get("investments") or {}
    cash = s.get("cash") or {}
    cur = s.get("currency") or ""
    return "\n".join([_env_line(client),
                      "account id:      %s" % _ident(s.get("id")),
                      "currency:        %s" % (cur or "-"),
                      "total value:     %s %s" % (_fmt(s.get("totalValue")), cur),
                      "free cash:       %s %s" % (_fmt(cash.get("availableToTrade")), cur),
                      "invested (cost): %s %s" % (_fmt(inv.get("totalCost")), cur),
                      "invested (now):  %s %s" % (_fmt(inv.get("currentValue")), cur),
                      "unrealised P/L:  %s %s" % (_fmt(inv.get("unrealizedProfitLoss")), cur),
                      "realised P/L:    %s %s" % (_fmt(inv.get("realizedProfitLoss")), cur)])


def cmd_positions(client, args):
    data = client.get(POSITIONS)
    if args.json:
        return data
    rows = []
    for p in data or []:
        inst = p.get("instrument") or {}
        w = p.get("walletImpact") or {}
        rows.append([inst.get("ticker", "?"), _qty(p.get("quantity")), _fmt(p.get("averagePricePaid")),
                     _fmt(p.get("currentPrice")), inst.get("currency", ""),
                     _fmt(w.get("currentValue")), _fmt(w.get("unrealizedProfitLoss")), w.get("currency", "")])
    if not rows:
        return _env_line(client) + "\nno open positions"
    return _env_line(client) + "\n" + _table(
        rows, ["ticker", "qty", "avg paid", "price", "ccy", "value", "P/L", "acct ccy"])


def cmd_orders(client, args):
    data = client.get(ORDERS)
    if args.json:
        return data
    rows = []
    for o in data or []:
        rows.append([_ident(o.get("id")), o.get("ticker") or (o.get("instrument") or {}).get("ticker", "?"),
                     o.get("side", "?"), o.get("type", "?"), _qty(o.get("quantity")),
                     _qty(o.get("filledQuantity")), _fmt(o.get("limitPrice")), _fmt(o.get("stopPrice")),
                     o.get("timeInForce") or "-", o.get("status", "?")])
    if not rows:
        return _env_line(client) + "\nno pending orders"
    return _env_line(client) + "\n" + _table(
        rows, ["id", "ticker", "side", "type", "qty", "filled", "limit", "stop", "tif", "status"])


def cmd_ticker(client, args):
    cached, _ = load_instruments(client, args.state_dir)
    inst = resolve(args.symbol, cached["instruments"])
    if args.json:
        return dict(inst, query=args.symbol)
    return "\n".join(["%s -> %s" % (args.symbol.strip().upper(), inst.get("ticker")),
                      "name:      %s" % (inst.get("name") or "-"),
                      "type:      %s" % (inst.get("type") or "-"),
                      "currency:  %s" % (inst.get("currencyCode") or "-"),
                      "isin:      %s" % (inst.get("isin") or "-"),
                      "ext hours: %s" % _fmt(inst.get("extendedHours"))])


def cmd_instruments(client, args):
    cached, fetched = load_instruments(client, args.state_dir, refresh=args.refresh)
    insts = cached["instruments"]
    age = max(0.0, _now() - (_num(cached.get("fetched_at")) or 0))
    data = {"env": client.env, "count": len(insts), "us_equities": sum(1 for i in insts if _is_us(i)),
            "fetched_now": fetched, "age_seconds": int(age),
            "cache": cache_path(args.state_dir, client.env)}
    if args.json:
        return data
    return "%s\n%d instruments (%d US listings) %s, cached at %s for 24h" % (
        _env_line(client), data["count"], data["us_equities"],
        "downloaded now" if fetched else "from cache, %d min old" % (age // 60), data["cache"])


# ---------------------------------------------------------------- placing: sides and signs

def signed_quantity(side, qty):
    """Trading 212's rule: a buy is a positive quantity, a sell a negative one.
    The ticket's qty must be a positive number; its side alone sets the sign."""
    s = str(side or "").strip().lower()
    if s not in ("buy", "sell"):
        raise T212Error("the ticket's side must be buy or sell, not %r" % side, 2)
    if isinstance(qty, bool):
        raise T212Error("the ticket's quantity is not a number", 2)
    q = _num(qty)
    if q is None or not math.isfinite(q) or q <= 0:
        raise T212Error("the ticket's quantity must be a positive number (the side, not the sign, says "
                        "buy or sell); got %r" % (qty,), 2)
    q = int(q) if q == int(q) else round(q, 6)
    return q if s == "buy" else -q


def check_sign(body, side):
    """Refuse a body whose quantity sign does not match the side it is meant to be."""
    q = body.get("quantity") if isinstance(body, dict) else None
    s = str(side or "").lower()
    if isinstance(q, bool) or not isinstance(q, (int, float)) or s not in ("buy", "sell"):
        raise T212Error("refusing to send: the order has no numeric quantity or no side", 2)
    if (s == "buy" and not q > 0) or (s == "sell" and not q < 0):
        raise T212Error("refusing to send: a %s must have a %s quantity, this one is %s"
                        % (s, "positive" if s == "buy" else "negative", q), 2)


def market_body(ticker, side, qty):
    return {"ticker": ticker, "quantity": signed_quantity(side, qty), "extendedHours": False}


def stop_body(ticker, qty, stop):
    """The stop-loss for a long position: always a SELL, so always a negative quantity."""
    return {"ticker": ticker, "quantity": signed_quantity("sell", qty), "stopPrice": round(float(stop), 2),
            "timeValidity": STOP_VALIDITY}


# ---------------------------------------------------------------- placing: money

def _floor(x, step):
    return math.floor(x / step + 1e-9) * step


def _gbp(x):
    return "GBP {:,.2f}".format(x)


def loss_limit_gbp(cfg, rate):
    """(limit in GBP, how it was found). A GBP setting wins; else the USD one at today's rate."""
    if _num(cfg.get("max_loss_per_trade_gbp")) is not None:
        v = float(cfg["max_loss_per_trade_gbp"])
        return v, "max_loss_per_trade_gbp %s" % _gbp(v)
    if _num(cfg.get("max_loss_per_trade_usd")) is not None:
        usd = float(cfg["max_loss_per_trade_usd"])
        return usd / rate, "max_loss_per_trade_usd $%s at %.4f" % (_fmt(usd), rate)
    raise T212Error("your config has no max_loss_per_trade_gbp or max_loss_per_trade_usd; "
                    "nothing is placed without a loss limit", 2)


def size_entry(entry, stop, ticket_qty, rate, cash_gbp, cfg, fractional=None):
    """GBP sizing for a long entry. Never more than the ticket's quantity, and the loss at
    the stop (price move plus the 0.15% FX fee on the buy and on the sell) never over the limit."""
    if not entry or entry <= 0 or not rate or rate <= 0:
        raise T212Error("need a positive price and GBP/USD rate to size this order", 2)
    if stop is None:
        raise T212Error("this entry has no stop; every entry needs one", 2)
    if stop >= entry:
        raise T212Error("the stop %.2f is not below the entry %.2f, so it is not a stop-loss for a buy"
                        % (stop, entry), 2)
    cost_ps = entry / rate * (1 + FX_FEE)
    loss_ps = (entry - stop) / rate + (entry / rate) * FX_FEE + (stop / rate) * FX_FEE
    limit, limit_src = loss_limit_gbp(cfg, rate)
    if fractional is None:
        fractional = bool(cfg.get("fractional"))
    step = 0.01 if fractional else 1
    caps = [("the ticket", float(ticket_qty)), ("your max loss per trade", limit / loss_ps),
            ("the cash available to trade", max(cash_gbp, 0.0) / cost_ps)]
    cap_pct, budget = _num(cfg.get("max_position_pct")), _num(cfg.get("budget_gbp"))
    if cap_pct and budget and 0 < cap_pct < 100:
        # trader-budget already caps the ticket at this; a second net here, with 1 % room for the GBP/USD
        # rate moving between sizing and placing (so it never shaves a correctly sized ticket)
        caps.append(("max_position_pct", budget * cap_pct / 100.0 * POSITION_CAP_ROOM / (entry / rate)))
    if _num(cfg.get("max_order_gbp")) is not None:
        caps.append(("max_order_gbp", float(cfg["max_order_gbp"]) / cost_ps))
    elif _num(cfg.get("max_order_usd")) is not None:
        caps.append(("max_order_usd", float(cfg["max_order_usd"]) / rate / cost_ps))
    binding, raw = min(caps, key=lambda c: c[1])
    qty = _floor(raw, step)
    while qty > 0 and qty * loss_ps > limit + 1e-9:   # belt and braces against float rounding
        qty = _floor(qty - step, step)
    qty = round(qty, 2)
    if qty <= 0:
        raise T212Error("not even %s share fits: %s allows %.4f" % ("0.01" if step < 1 else "1", binding, raw), 2)
    value = qty * entry / rate
    return {"qty": int(qty) if step == 1 else qty, "ticket_qty": float(ticket_qty), "binding": binding,
            "entry_usd": entry, "stop_usd": stop, "rate": rate, "value_gbp": round(value, 2),
            "fee_gbp": round(value * FX_FEE, 2), "cost_gbp": round(value * (1 + FX_FEE), 2),
            "max_loss_gbp": round(qty * loss_ps, 2), "max_loss_usd": round(qty * loss_ps * rate, 2),
            "limit_gbp": round(limit, 2), "limit_src": limit_src, "cash_gbp": cash_gbp}


def pending_buys_gbp(pending, rate, state=None):
    """GBP cost, with the FX fee, of buy orders at Trading 212 that have not filled yet: cash that is
    spoken for. A market order has no price of its own, so it is priced at the entry its buy record
    planned. Returns (gbp, [tickers whose cost is unknown])."""
    planned = {}
    for r in (state or {}).get("orders", []):
        if str(r.get("side", "")).lower() == "buy" and r.get("t212_ticker") and _num(r.get("entry")):
            planned[r["t212_ticker"]] = _num(r["entry"])
    total, unknown = 0.0, []
    for o in pending or []:
        q = _num(o.get("quantity")) or 0
        side = str(o.get("side") or "").upper()
        if side != "BUY" and not (not side and q > 0):
            continue
        left = abs(q) - abs(_num(o.get("filledQuantity")) or 0)
        if left <= 1e-9:
            continue
        t = _o_ticker(o)
        px = _num(o.get("limitPrice")) or _num(o.get("stopPrice")) or planned.get(t)
        if not px:
            unknown.append(t or "?")
            continue
        total += left * px / rate * (1 + FX_FEE)
    return round(total, 2), unknown


def waiting_not_held_back(summary, waiting):
    """The part of `waiting` (GBP of buys still waiting to fill) that Trading 212 has not already taken out
    of availableToTrade. Trading 212 holds cash back for pending orders in reservedForOrders; counting the
    same order again would make the free cash look smaller than it is. With nothing held back, all of it."""
    held_back = _num(((summary or {}).get("cash") or {}).get("reservedForOrders")) or 0.0
    return round(max(waiting - max(held_back, 0.0), 0.0), 2)


def free_cash(summary, pending, rate, cfg, state=None, reserved=0.0):
    """Cash a new buy may use: Trading 212's cash available to trade, minus buys still waiting to
    fill (the part Trading 212 has not already held back in reservedForOrders), minus the cash buffer
    (cash_buffer_pct of the day's budget_gbp, else of the account value), minus `reserved`: the demo
    autopilot's active reservations for OTHER stocks (trader-broker/allocation.py)."""
    cash = _num(((summary or {}).get("cash") or {}).get("availableToTrade")) or 0.0
    waiting, unknown = pending_buys_gbp(pending, rate, state)
    waiting = waiting_not_held_back(summary, waiting)
    pct = _num((cfg or {}).get("cash_buffer_pct")) or 0.0
    base = _num((cfg or {}).get("budget_gbp")) or _num((summary or {}).get("totalValue")) or 0.0
    buffer = round(base * pct / 100.0, 2)
    reserved = round(max(_num(reserved) or 0.0, 0.0), 2)
    return {"cash": round(cash, 2), "waiting": waiting, "unknown": unknown, "buffer": buffer,
            "reserved": reserved, "available": round(cash - waiting - buffer - reserved, 2)}


def portfolio_snapshot(summary, pending, positions, rate, state=None, reserved=0.0, now=None):
    """The account as trader-budget and trader-risk read it (their --portfolio file), in US dollars, from
    Trading 212: `cash` is the cash a new buy may use BEFORE the cash buffer (they take the buffer off
    themselves): availableToTrade, minus buys waiting to fill that Trading 212 has not held back yet, minus
    `reserved` GBP for other stocks. Each held position, and each buy waiting to fill, is a position
    valued now (held: Trading 212's current price; waiting: the planned entry), so the position cap and
    the buffer are worked out on the whole account. Without it they assume an empty account."""
    if not rate or rate <= 0:
        raise T212Error("need a GBP/USD rate for the portfolio snapshot", 2)
    cash_gbp = _num(((summary or {}).get("cash") or {}).get("availableToTrade")) or 0.0
    waiting, unknown = pending_buys_gbp(pending, rate, state)
    if unknown:
        raise T212Error("a buy waiting to fill has no known price (%s)" % ", ".join(unknown), 2)
    reserved = round(max(_num(reserved) or 0.0, 0.0), 2)
    free_gbp = cash_gbp - waiting_not_held_back(summary, waiting) - reserved
    rows = {}

    def add(t, qty, px):
        sym = str(t or "").split("_")[0].upper()
        if not sym or qty <= 1e-9 or not px:
            return
        r = rows.setdefault(sym, {"ticker": sym, "shares": 0.0, "lots": [{"shares": 0.0, "cost": 0.0}]})
        r["shares"] = round(r["shares"] + qty, 6)
        r["lots"][0]["shares"] = r["shares"]
        r["lots"][0]["cost"] = round(r["lots"][0]["cost"] + qty * px, 2)

    for p in positions or []:
        q = _num(p.get("quantity")) or 0.0
        add(_o_ticker(p), q, _num(p.get("currentPrice")) or _num(p.get("averagePricePaid")) or _num(p.get("averagePrice")))
    planned = {r["t212_ticker"]: _num(r.get("entry")) for r in (state or {}).get("orders", [])
               if str(r.get("side", "")).lower() == "buy" and r.get("t212_ticker") and _num(r.get("entry"))}
    for o in pending or []:
        q = _num(o.get("quantity")) or 0
        side = str(o.get("side") or "").upper()
        if side != "BUY" and not (not side and q > 0):
            continue
        left = abs(q) - abs(_num(o.get("filledQuantity")) or 0)
        t = _o_ticker(o)
        add(t, left, _num(o.get("limitPrice")) or _num(o.get("stopPrice")) or planned.get(t))
    return {"as_of": (now or datetime.datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Trading 212 (demo autopilot snapshot)", "currency": "USD", "usd_per_gbp": rate,
            "cash": math.floor(max(free_gbp, 0.0) * rate * 100) / 100.0, "cash_gbp": round(free_gbp, 2),
            "reserved_gbp": reserved, "positions": sorted(rows.values(), key=lambda r: r["ticker"])}


def no_cash_text(fc, need=None):
    """One plain line on the cash, for the autopilot log (never the phone, never Trading 212's error JSON)."""
    if fc.get("reserved"):
        s = ("free cash GBP %.2f, minus GBP %.2f for buys waiting to fill, minus the GBP %.2f buffer, minus GBP %.2f "
             "reserved for other stocks = GBP %.2f" % (fc["cash"], fc["waiting"], fc["buffer"], fc["reserved"],
                                                       fc["available"]))
    else:
        s = "free cash GBP %.2f, minus GBP %.2f for buys waiting to fill, minus the GBP %.2f buffer = GBP %.2f" % (
            fc["cash"], fc["waiting"], fc["buffer"], fc["available"])
    if fc.get("unknown"):
        s += "; a buy waiting to fill has no known price (%s), so no cash is counted as free" % ", ".join(fc["unknown"])
    if need is not None:
        s += "; this buy needs about GBP %.2f with the FX fee" % need
    return s


FRACTIONAL_TYPES = ("STOCK", "ETF")


def fractional_for(cfg, inst):
    """Fractions (0.01 of a share) only with "fractional": true AND an instrument type that
    Trading 212 sells in fractions. Its instrument list has no per-stock flag, so stocks and
    ETFs get fractions and anything else (warrants) whole shares; Trading 212 refuses the
    order if a particular stock does not take them, and nothing is ever retried."""
    return bool(cfg.get("fractional")) and str((inst or {}).get("type", "")).upper() in FRACTIONAL_TYPES


def cmd_fx(args):
    """Fetch GBP/USD and save it to trader/fx.json for trader-budget and trader-risk.
    Read-only: it asks Yahoo, never Trading 212, and sends no order."""
    rate, when, src = fetch_gbpusd()
    fetched = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {"pair": "GBPUSD", "usd_per_gbp": rate, "quote_time": when, "fetched_at": fetched, "source": src}
    path = os.path.join(args.state_dir, "fx.json")
    _write_json(path, doc)
    if args.json:
        return dict(doc, path=path)
    return ("GBP/USD %.4f (1 GBP = %.4f USD, %s, quoted %s)\nsaved to %s; trader-budget and trader-risk "
            "use it for 12 hours" % (rate, rate, src, when, path))


def size_exit(price, qty, rate):
    value = qty * price / rate
    return {"qty": qty, "entry_usd": price, "rate": rate, "value_gbp": round(value, 2),
            "fee_gbp": round(value * FX_FEE, 2), "proceeds_gbp": round(value * (1 - FX_FEE), 2)}


def _iso_ts(text):
    try:
        dt = datetime.datetime.strptime(str(text).strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return calendar.timegm(dt.timetuple())


def ticket_price(order, prices_path):
    """The price to size with: the HIGHER of the plan's entry and a quote under 15
    minutes old from trader/prices.json (trader-risk's rule), so neither can understate it."""
    sym = str(order.get("symbol", "")).upper()
    found = []
    entry = _num((order.get("plan") or {}).get("entry"))
    if entry:
        found.append((entry, "the plan's entry"))
    doc = _read_json(prices_path, {})
    if isinstance(doc, dict):
        p = _num((doc.get("prices") or {}).get(sym))
        d = (doc.get("detail") or {}).get(sym) or {}
        t = _iso_ts(d.get("time") or "") or _iso_ts(d.get("fetched") or "") or _iso_ts(doc.get("as_of") or "")
        if p and t is not None and 0 <= _now() - t <= PRICE_FRESH:
            found.append((p, "a fresh quote"))
    if not found:
        raise T212Error("no price for %s: the ticket has no plan entry and trader/prices.json has no fresh "
                        "quote (run trader-alerts quote %s --write)" % (sym, sym), 2)
    return max(found)


def _fx_open(req, timeout):   # a plain request: no Trading 212 credentials go to Yahoo
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_gbpusd():
    """(USD per GBP, quote time, source) from Yahoo's GBPUSD=X."""
    req = urllib.request.Request(FX_URL, headers={"Accept": "application/json",
                                                  "User-Agent": "Mozilla/5.0 (compatible; day-trader-skill/2.0)"})
    try:
        resp = _fx_open(req, 20)
        try:
            data = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        meta = data["chart"]["result"][0]["meta"]
        rate = float(meta["regularMarketPrice"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
        raise T212Error("could not get the GBP/USD rate from Yahoo (%s); pass --fx-rate to give it "
                        "yourself" % type(e).__name__, 2)
    if not 0.5 < rate < 3.0:
        raise T212Error("Yahoo's GBP/USD rate %r looks wrong; pass --fx-rate" % rate, 2)
    t = _num(meta.get("regularMarketTime"))
    if t is not None and _now() - t > FX_MAX_AGE:
        raise T212Error("Yahoo's GBP/USD rate is more than 4 days old; pass --fx-rate", 2)
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t is not None else "?")
    return rate, when, "Yahoo GBPUSD=X"


# ---------------------------------------------------------------- placing: files

_TICKET = []


def ticket_module():
    """trader-broker/ticket.py, for the risk PASS, the fingerprint and the strict yes."""
    if not _TICKET:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket.py")
        spec = importlib.util.spec_from_file_location("trader_broker_ticket", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TICKET.append(mod)
    return _TICKET[0]


def _utc_now():
    return datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def log_event(state_dir, secrets, event, **fields):
    """One line per order sent and per answer, in trader/logs/t212-orders-DATE.jsonl."""
    now = _utc_now()
    rec = dict(fields, event=event, at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    line = scrub(json.dumps(rec, sort_keys=True), secrets)
    path = os.path.join(state_dir, "logs", "t212-orders-%s.jsonl" % now.strftime("%Y-%m-%d"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _record(state_path, state, rec):
    state.setdefault("orders", []).append(rec)
    ticket_module().write_json(state_path, state)


def pick_ticket(orders, ticket_id, fingerprint):
    tid = str(ticket_id or "").strip()
    if tid.isdigit():
        i = int(tid)
        if not 1 <= i <= len(orders):
            raise T212Error("there is no ticket %d (there are %d)" % (i, len(orders)), 2)
        return i, orders[i - 1]
    hits = [(i, o) for i, o in enumerate(orders, 1) if len(tid) >= 6 and fingerprint(o).startswith(tid)]
    if len(hits) != 1:
        raise T212Error("ticket id %r matches %d tickets; use its number from ticket.py show" % (tid, len(hits)), 2)
    return hits[0]


def _o_ticker(o):
    return o.get("ticker") or (o.get("instrument") or {}).get("ticker")


def duplicate_problems(pending, positions, ticker, side, qty):
    """What in the account says this order may already be there. Returns (problems, held, available)."""
    problems = []
    mine = [o for o in pending or [] if _o_ticker(o) == ticker]
    held = sum(_num(p.get("quantity")) or 0 for p in positions or [] if _o_ticker(p) == ticker)
    avail = sum(_num(p.get("quantityAvailableForTrading", p.get("quantity"))) or 0
                for p in positions or [] if _o_ticker(p) == ticker)
    if mine:
        problems.append("there %s already %d pending order%s for %s: %s%s" % (
            "is" if len(mine) == 1 else "are", len(mine), "" if len(mine) == 1 else "s", ticker,
            "; ".join("%s %s %s (%s)" % (o.get("side", "?"), o.get("type", "?"), _qty(o.get("quantity")),
                                         o.get("status", "?")) for o in mine),
            "" if side == "buy" else ". Cancel it in the app first if you mean to sell"))
    if side == "buy" and held > 0:
        problems.append("you already hold %s %s, so this entry may already have been placed" % (_qty(held), ticker))
    if side == "sell" and avail + 1e-9 < qty:
        problems.append("you hold %s %s available to sell, not %s" % (_qty(avail), ticker, _qty(qty)))
    return problems, held, avail


def _is_tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline()


def _say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _today_ny(tk):
    return tk.to_eastern(_utc_now()).date().isoformat()


def render_place(index, total, client, order, inst, sz, side, price_src, fx, bodies, rv_fp, mode="broker"):
    ccy = "USD"
    lines = ["PLACE TICKET %d of %d on %s" % (index, total, _env_line(client)),
             "  %s %s %s (%s, %s), market order" % (side.upper(), _qty(sz["qty"]), inst.get("ticker"),
                                                   str(order.get("symbol", "")).upper(), inst.get("name", "")),
             "  price used:  %.2f %s (%s)" % (sz["entry_usd"], ccy, price_src),
             "  GBP/USD:     %.4f (%s, %s)" % (fx[0], fx[2], fx[1])]
    if side == "buy":
        lines.append("  est. cost:   %s + 0.15%% FX fee %s = %s (cash available %s)"
                     % (_gbp(sz["value_gbp"]), _gbp(sz["fee_gbp"]), _gbp(sz["cost_gbp"]), _gbp(sz["cash_gbp"])))
        if mode == "broker":
            lines.append("  stop:        %.2f %s, sent as a separate STOP order once the entry fills: SELL %s, "
                         "good till cancelled" % (sz["stop_usd"], ccy, _qty(sz["qty"])))
        else:
            lines.append("  stop:        %.2f %s, enforced by t212.py guard (%s): it sells at market "
                         "when the price is at or under it. No stop order is sent; keep the guard running"
                         % (sz["stop_usd"], ccy, "fractional_stops software" if mode == "software" else "stop_mode market"))
        target = _num((order.get("plan") or {}).get("target")) or _num(order.get("take_profit_limit_price"))
        if target:
            lines.append("  target:      %.2f %s (shown only, not sent: Trading 212's API has no bracket order)"
                         % (target, ccy))
        lines.append("  max loss:    %s at the stop, FX fees in (limit %s, from %s)"
                     % (_gbp(sz["max_loss_gbp"]), _gbp(sz["limit_gbp"]), sz["limit_src"]))
        if sz["qty"] < sz["ticket_qty"]:
            lines.append("  size:        %s shares, cut from the ticket's %s by %s"
                         % (_qty(sz["qty"]), _qty(sz["ticket_qty"]), sz["binding"]))
    else:
        lines.append("  est. proceeds: %s - 0.15%% FX fee %s = %s"
                     % (_gbp(sz["value_gbp"]), _gbp(sz["fee_gbp"]), _gbp(sz["proceeds_gbp"])))
    lines.append("  trader-risk: PASS today (fingerprint %s)" % rv_fp)
    lines.append("  will send exactly:")
    for path, body in bodies:
        lines.append("    POST %s %s" % (path, json.dumps(body, sort_keys=True)))
    lines.append("  A market order can fill worse than this price, and a stop can fill below its price in a "
                 "fast market.")
    if client.env != "demo":
        lines.append("  !!! LIVE ACCOUNT, REAL MONEY. !!!")
    return "\n".join(lines)


def wait_for_fill(client, ticker, want, before, before_free=None):
    """True once the bought shares are held AND free to sell (quantityAvailableForTrading): a stop
    sent while they are still settling can be accepted and then REJECTED (DHR, 6 Nov)."""
    held_ok = False
    for i in range(FILL_POLLS + SELLABLE_POLLS):
        pos = client.get(POSITIONS, {"ticker": ticker})
        mine = [p for p in pos or [] if _o_ticker(p) == ticker]
        held = sum(_num(p.get("quantity")) or 0 for p in mine)
        free = sum(_num(p.get("quantityAvailableForTrading", p.get("quantity"))) or 0 for p in mine)
        if held - before + 1e-9 >= want:
            held_ok = True
            if free - (before if before_free is None else before_free) + 1e-9 >= want:
                return True
        elif i >= FILL_POLLS - 1 and not held_ok:
            return False
    return held_ok


def cmd_place(client, args, confirm=None):
    """confirm: None asks for your typed yes in this terminal. trader-broker/autopilot.py passes
    one that answers for the Approve you tapped on your phone (demo only); it gets
    (index, order, sizing) and returns (ok, reply, why)."""
    secrets = client._secrets
    sd = args.state_dir
    if client.env != "demo" and not (client.live_ok or LIVE_PLACEMENT_ENABLED):
        raise T212Error("live trading is switched off: it needs \"live_enabled\": true and \"paper\": false in "
                        "trader/config.json (trader-broker/go-live.sh). Nothing was sent.", 2)
    tk = ticket_module()
    paths = {"orders": getattr(args, "orders_path", None) or os.path.join(sd, "orders.json"),
             "config": os.path.join(sd, "config.json"),
             "risk": getattr(args, "risk_path", None) or os.path.join(sd, "state", "risk.json"),
             "state": os.path.join(sd, "state", "broker.json"), "prices": os.path.join(sd, "prices.json")}
    strategy = str(getattr(args, "strategy", None) or "intraday").strip().lower()
    if strategy not in STRATEGIES:
        raise T212Error("strategy must be one of %s, not %r" % (", ".join(STRATEGIES), strategy), 2)
    try:
        doc = tk.read_json(paths["orders"], "orders file (run trader-budget first)")
        cfg = tk.read_json(paths["config"], "config (trader/config.json)")
        risk = tk.read_json(paths["risk"], "risk check", required=False)
    except tk.TicketError as e:
        raise T212Error(str(e), 2)
    try:
        tk.config_currency(cfg)               # a GBP config with a dollar name in it stops here
    except tk.CurrencyError as e:
        raise T212Error(str(e), 2)
    if client.env != "demo" and (cfg.get("autopilot") or {}).get("evidence_mode"):
        raise T212Error("evidence mode is on in trader/config.json; it is a demo forward test and never places on "
                        "a live account. Turn it off first. Nothing was sent.", 2)
    mode = stop_mode(cfg)
    sell_date = None
    if strategy == "drift":
        # drift buying (trader-broker/drift_buy.py): demo only, behind drift.buy_enabled, a real stop always
        if client.env != "demo" or cfg.get("paper") is not True or cfg.get("live_enabled"):
            raise T212Error("drift buying is demo only (\"paper\": true); nothing was sent", 2)
        if (cfg.get("drift") or {}).get("buy_enabled") is not True:
            raise T212Error("drift.buy_enabled is off in trader/config.json; nothing was sent", 2)
        sell_date = _iso_date(getattr(args, "sell_date", None))
        if sell_date is None or sell_date <= datetime.date.fromisoformat(_today_ny(ticket_module())):
            raise T212Error("a drift buy needs a sell_date after today, not %r; nothing was sent"
                            % (getattr(args, "sell_date", None),), 2)
        mode = "broker"               # held overnight and at weekends: a real stop, whatever stop_mode says
    allow = allowed_tickers(with_universe(cfg, sd))
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise T212Error("no tickets in %s" % paths["orders"], 2)
    state = tk.load_state(paths["state"])
    today = _today_ny(tk)
    index, order = pick_ticket(orders, args.ticket_id, tk.fingerprint)
    fp = tk.fingerprint(order)

    # 1. trader-risk must have passed THIS exact order today
    rv, why = tk.risk_verdict(risk, fp, today)
    if rv is None:
        raise T212Error("ticket %d is not ready: %s" % (index, why), 2)
    if rv.get("verdict") != "PASS":
        raise T212Error("ticket %d was BLOCKED by trader-risk: %s" % (index, "; ".join(rv.get("reasons", []))), 2)
    prev = tk.latest(state, fp, today)
    prev_status = (prev or {}).get("status")
    if prev_status in ("failed", "unknown", "sending"):
        if not args.resend:
            raise T212Error("ticket %d was already sent today and came back %s. It is never retried by itself; "
                            "check the app, then use 'place %d --resend' (it checks for a duplicate first)"
                            % (index, prev_status, index), 2)
    elif prev_status not in (None, "approved"):
        raise T212Error("ticket %d is done for today (%s at %s); nothing was sent" % (index, prev_status, prev.get("at")), 2)

    # 2. what it is
    side = str(order.get("side", "")).strip().lower()
    signed_quantity(side, order.get("qty"))     # a bad side or a non-positive qty stops here
    kind = tk.kind_of(order, rv)
    if side == "buy" and str(order.get("symbol", "")).upper() in blocked_tickers(cfg):
        raise T212Error("ticket %d buys %s, which is on your blocked_tickers list; nothing was sent"
                        % (index, str(order.get("symbol", "")).upper()), 2)
    if side == "buy" and allow is not None and str(order.get("symbol", "")).upper() not in allow:
        raise T212Error("ticket %d buys %s, which is not on your allowed_tickers list; nothing was sent"
                        % (index, str(order.get("symbol", "")).upper()), 2)
    if strategy == "drift" and (side != "buy" or kind != "entry"):
        raise T212Error("ticket %d: a drift order must be a buy to open; nothing was sent" % index, 2)
    if side == "sell" and kind != "exit":
        raise T212Error("ticket %d would sell short; this tool only buys to open and sells what you hold" % index, 2)
    if str(order.get("type", "market")).lower() != "market":
        raise T212Error("ticket %d is a %s order; only market orders are supported for now" % (index, order.get("type")), 2)
    if order.get("notional") is not None and order.get("qty") is None:
        raise T212Error("ticket %d is sized in dollars; Trading 212 orders need a share quantity" % index, 2)

    cached, _ = load_instruments(client, sd)
    inst = resolve(order.get("symbol"), cached["instruments"])
    ticker = inst["ticker"]
    if str(inst.get("currencyCode", "")).upper() != "USD":
        raise T212Error("%s trades in %s; only US dollar stocks are supported for now" % (ticker, inst.get("currencyCode")), 2)

    # 3. account, rate, price
    summary = client.get(SUMMARY)
    if str(summary.get("currency", "")).upper() != "GBP":
        raise T212Error("this account is in %s; sizing here is for a GBP account" % summary.get("currency"), 2)
    cash = _num((summary.get("cash") or {}).get("availableToTrade")) or 0.0
    if args.fx_rate is not None:
        if not 0.5 < args.fx_rate < 3.0:
            raise T212Error("--fx-rate is USD per GBP, e.g. 1.27", 2)
        fx = (args.fx_rate, "given with --fx-rate", "your --fx-rate")
    else:
        fx = fetch_gbpusd()

    # 4. duplicates: pending orders and positions, every time
    pending = client.get(ORDERS)
    positions = client.get(POSITIONS, {"ticker": ticker})
    ticket_qty = abs(_num(order.get("qty")))
    problems, held, avail = duplicate_problems(pending, positions, ticker, side, ticket_qty)
    if problems:
        log_event(sd, secrets, "refused", env=client.env, ticket=index, fingerprint=fp, ticker=ticker,
                  reasons=problems)
        raise T212Error("not sending ticket %d: %s" % (index, "; ".join(problems)), 2)
    live_px = [_num(p.get("currentPrice")) for p in positions or [] if _o_ticker(p) == ticker]
    if side == "sell" and live_px and live_px[0]:
        price, price_src = live_px[0], "Trading 212's current price for your position"
    else:
        price, price_src = ticket_price(order, paths["prices"])

    # 4b. the autopilot's cash check: skip (never shrink) a buy the free cash does not cover
    if side == "buy" and getattr(args, "cash_check", False):
        fc = free_cash(summary, pending, fx[0], cfg, state, reserved=getattr(args, "reserved_gbp", 0.0) or 0.0)
        need = round(ticket_qty * price / fx[0] * (1 + FX_FEE), 2)
        if fc["unknown"] or need > fc["available"] + 0.005:
            why = "no free cash: %s" % no_cash_text(fc, need)
            log_event(sd, secrets, "refused", env=client.env, ticket=index, fingerprint=fp, ticker=ticker,
                      reasons=[why])
            raise NoCash("NOT PLACED: " + why, dict(fc, need=need))

    # 5. size and build the exact bodies
    if side == "buy":
        stop = _num(order.get("stop_loss_stop_price")) or _num((order.get("plan") or {}).get("stop"))
        sz = size_entry(price, stop, ticket_qty, fx[0], cash, cfg, fractional_for(cfg, inst))
        bodies = [(MARKET, market_body(ticker, "buy", sz["qty"]))]
        if mode == "broker" and is_fractional(sz["qty"]) and fractional_stops(cfg) == "software" and strategy != "drift":
            mode = "software"                     # fractional position: the guard holds the stop
        if mode == "broker":
            bodies.append((STOP, stop_body(ticker, sz["qty"], stop)))
    else:
        sz = size_exit(price, ticket_qty, fx[0])
        bodies = [(MARKET, market_body(ticker, "sell", sz["qty"]))]
    for path, body in bodies:
        check_sign(body, "sell" if path == STOP else side)

    _say(render_place(index, len(orders), client, order, inst, sz, side, price_src, fx, bodies, fp, mode))

    # 6. your yes, for this one order: typed here, or your Approve tap (autopilot, demo only)
    if confirm is not None:
        if client.env != "demo":
            raise T212Error("a phone approval places on demo only; nothing was sent", 2)
        ok, reply, why = confirm(index, order, sz)
    else:
        if not _is_tty():
            raise T212Error("place needs you to type yes in your own terminal; nothing was sent. Run it there: "
                            "python3 trader-broker/t212.py place %d" % index, 2)
        reply = (_ask('Type "yes" to place exactly this order (anything else cancels): ') or "").strip()
        shown = dict(order, qty=str(sz["qty"]))
        shown["plan"] = dict(order.get("plan") or {}, max_loss=sz.get("max_loss_usd"))
        ok, why = tk.parse_yes(reply, shown, [o.get("symbol", "") for o in orders])
    if not ok:
        log_event(sd, secrets, "declined", env=client.env, ticket=index, fingerprint=fp, reply=reply, why=why)
        _say("Not placed: %s." % why)
        return None

    # 7. send the entry, once
    base = {"date": today, "index": index, "fingerprint": fp, "symbol": str(order.get("symbol", "")).upper(),
            "side": side, "qty": str(sz["qty"]), "kind": kind, "broker": "trading212", "env": client.env,
            "paper": client.env == "demo", "t212_ticker": ticker, "reply": reply,
            "plain": "%s %s %s, market" % (side.upper(), _qty(sz["qty"]), ticker)}
    if side == "buy":
        base.update(strategy=strategy,               # "intraday", or "drift" with its sell_date
                    max_loss=sz["max_loss_usd"], max_loss_gbp=sz["max_loss_gbp"], stop=sz["stop_usd"],
                    entry=sz["entry_usd"], cost_gbp=sz["cost_gbp"], fx_rate=fx[0],
                    target=_num(order.get("take_profit_limit_price")) or _num((order.get("plan") or {}).get("target")))
        if sell_date is not None:
            base["sell_date"] = sell_date.isoformat()
    path, body = bodies[0]
    _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="sending"))
    log_event(sd, secrets, "request", env=client.env, ticket=index, fingerprint=fp, method="POST", path=path, body=body)
    try:
        status, resp = client.post_order(path, body, side)
    except PlaceError as e:
        log_event(sd, secrets, "response", env=client.env, ticket=index, path=path, status=e.status,
                  outcome=e.outcome, error=str(e))
        st = "failed" if e.outcome == "rejected" else "unknown"
        _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status=st, note=str(e)))
        if st == "failed":
            raise T212Error("NOT PLACED: %s. Not retried." % e)
        raise T212Error("UNCLEAR whether it was placed: %s. Not retried. Check the app; if it is not there, "
                        "'place %d --resend' checks for a duplicate before sending again." % (e, index))
    log_event(sd, secrets, "response", env=client.env, ticket=index, path=path, status=status, body=resp)
    _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="placed",
                                        broker_order_id=resp.get("id"), broker_status=resp.get("status")))
    got_side = str(resp.get("side", "")).lower()
    _say("PLACED: %s, order id %s, status %s" % (base["plain"], _ident(resp.get("id")), resp.get("status", "?")))
    if got_side and got_side != side:
        _say("!!! Trading 212 reports this order as %s, not %s. Check it in the app now. !!!" % (got_side.upper(), side.upper()))
    result = {"ticket": index, "env": client.env, "entry": resp, "stop": None}

    # 8. the stop, once the entry has filled: the same on demo and live
    if side != "buy":
        return result
    if mode == "software":
        _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="software_stop"))
        _say("STOP %.2f is enforced by t212.py guard (fractional_stops software): %s is a fractional position, so no "
             "stop order was sent; the guard sells at market at or under the stop. Keep the guard running."
             % (sz["stop_usd"], _qty(sz["qty"])))
        result["stop_mode"] = "software"
        return result
    if mode == "market":
        _say("STOP %.2f is enforced by t212.py guard (stop_mode market): it sells %s %s at market when the price "
             "is at or under it. No stop order was sent. Keep the guard running." % (sz["stop_usd"], _qty(sz["qty"]), ticker))
        result["stop_mode"] = "market"
        return result
    if not wait_for_fill(client, ticker, sz["qty"], held, avail):
        msg = ("the entry has not filled yet (the market may be closed), so the stop was NOT placed. "
               "When it fills, set a stop in the app: SELL %s %s at %.2f" % (_qty(sz["qty"]), ticker, sz["stop_usd"]))
        log_event(sd, secrets, "stop_skipped", env=client.env, ticket=index, reason=msg)
        _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="stop_not_placed", note=msg))
        raise T212Error("ENTRY PLACED, STOP NOT PLACED: " + msg, 3)
    path, body = bodies[1]
    log_event(sd, secrets, "request", env=client.env, ticket=index, fingerprint=fp, method="POST", path=path, body=body)
    try:
        status, sresp = client.post_order(path, body, "sell")
    except PlaceError as e:
        log_event(sd, secrets, "response", env=client.env, ticket=index, path=path, status=e.status,
                  outcome=e.outcome, error=str(e))
        _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="stop_not_placed", note=str(e)))
        raise T212Error("ENTRY PLACED, STOP %s: %s. Not retried. Set the stop in the app now: SELL %s %s at %.2f"
                        % ("NOT PLACED" if e.outcome == "rejected" else "UNCLEAR", e, _qty(sz["qty"]), ticker,
                           sz["stop_usd"]), 3)
    log_event(sd, secrets, "response", env=client.env, ticket=index, path=path, status=status, body=sresp)
    _say("STOP SENT: SELL %s %s at %.2f, good till cancelled, order id %s; checking it again in %ds"
         % (_qty(sz["qty"]), ticker, sz["stop_usd"], _ident(sresp.get("id")), VERIFY_AFTER))
    _sleep(VERIFY_AFTER)
    how, raw = stop_status(client, ticker, sresp.get("id"))
    log_event(sd, secrets, "stop_check", env=client.env, ticket=index, order_id=sresp.get("id"), result=how, status=raw)
    rec_base = dict(base, broker_order_id=sresp.get("id"))
    if how != "ok" and how != "filled":
        _record(paths["state"], state, dict(rec_base, at=tk.ny_iso(_utc_now()), status="stop_rejected",
                                            note="stop %s (%s) a minute after it was sent" % (how, raw)))
        software_on(sd, ticker, rec_base, sz["stop_usd"], "the stop order was %s (%s)" % (how, raw))
        stop_rejected_alarm(client, ticker, sz["qty"], sz["stop_usd"],
                            "REJECTED by Trading 212" if how == "rejected" else "GONE (%s)" % raw)
        raise T212Error("ENTRY PLACED, STOP %s by Trading 212 (%s). The guard enforces the %.2f stop in software "
                        "now (market sell at or under it); keep it running, or set a stop in the app."
                        % ("REJECTED" if how == "rejected" else "NOT WORKING", raw, sz["stop_usd"]), 3)
    _record(paths["state"], state, dict(rec_base, at=tk.ny_iso(_utc_now()), status="stop_placed", verified=raw))
    verify_track(sd, ticker, sresp.get("id"), rec_base, sz["stop_usd"])
    _say("STOP PLACED and checked a minute later: Trading 212 shows it as %s" % raw)
    result["stop"] = sresp
    return result


# ---------------------------------------------------------------- the stop guardian (demo only)

GUARD_STOP_TYPES = ("STOP", "STOP_LIMIT")
DEAD_STATUSES = ("REJECTED", "CANCELLED", "CANCELLING")   # a pending order in one of these protects nothing
VERIFY_AFTER = 60             # seconds after placing a stop before its status is checked again
SELLABLE_POLLS = 15           # positions checks (1 a second) waiting for bought shares to be free to sell
FRACTIONAL_STOP_MODES = ("broker", "software")
GUARD_OPEN = (9, 30)
GUARD_STATUSES = ("placed", "stop_placed", "stop_not_placed", "stop_rejected", "software_stop")
GUARD_GONE_POLLS = 3          # looks at the pending list after a cancel, before the new stop goes in
GUARD_CANCELLED_KEEP = 200    # stop ids the guard cancelled itself (to raise them), newest kept


def guard_sell_orders(pending, ticker):
    """(pending sell stops, other pending sells) for one ticker."""
    stops, others = [], []
    for o in pending or []:
        if _o_ticker(o) != ticker:
            continue
        q = _num(o.get("quantity")) or 0
        if not (q < 0 or str(o.get("side", "")).upper() == "SELL"):
            continue
        if str(o.get("status", "")).upper() in DEAD_STATUSES:
            continue
        (stops if str(o.get("type", "")).upper() in GUARD_STOP_TYPES else others).append(o)
    return stops, others


def fractional_stops(cfg):
    """"broker" (default): fractional positions get a real stop order like any other.
    "software": a fractional position gets no stop order; the guard sells at market at the stop."""
    m = str((cfg or {}).get("fractional_stops") or "broker").strip().lower()
    if m not in FRACTIONAL_STOP_MODES:
        raise T212Error("fractional_stops in trader/config.json must be \"broker\" or \"software\"", 2)
    return m


def is_fractional(qty):
    return abs(float(qty) - round(float(qty))) > 1e-9


def stop_status(client, ticker, order_id, pending=None):
    """Is this stop still working at Trading 212? ("ok" | "rejected" | "cancelled" | "filled" | "missing", raw
    status). A stop on the pending list counts unless its status is dead; otherwise the order history says."""
    if pending is None:
        pending = client.get(ORDERS) or []
    for o in pending:
        if o.get("id") == order_id:
            st = str(o.get("status", "")).upper()
            if st not in DEAD_STATUSES:
                return "ok", st
            return ("rejected" if st == "REJECTED" else "cancelled"), st
    try:
        doc = client.get(HISTORY, {"ticker": ticker, "limit": 20}) or {}
    except T212Error as e:
        if e.status in (401, 403):
            raise
        return "missing", "not pending, and the order history did not load (%s)" % scrub(e, client._secrets)
    for it in doc.get("items") or []:
        o = it.get("order") or {}
        if o.get("id") == order_id:
            st = str(o.get("status", "")).upper()
            if st == "FILLED" or (_num(o.get("filledQuantity")) or 0) > 0:
                return "filled", st
            if st == "REJECTED":
                return "rejected", st
            if st in ("CANCELLED", "CANCELLING"):
                return "cancelled", st
            return "missing", st or "?"
    return "missing", "not pending and not in the recent order history"


def software_on(state_dir, ticker, rec, stop, why):
    """From now on the guard enforces this position's stop itself (sells at market at the stop)."""
    path = os.path.join(state_dir, "state", "t212-guard.json")
    st = _read_json(path, {}) or {}
    st.setdefault("software", {})[ticker] = {"fingerprint": (rec or {}).get("fingerprint"), "stop": stop,
                                             "why": why, "since": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    (st.get("verify") or {}).pop(ticker, None)
    _write_json(path, st)


def verify_track(state_dir, ticker, order_id, rec, stop):
    path = os.path.join(state_dir, "state", "t212-guard.json")
    st = _read_json(path, {}) or {}
    st.setdefault("verify", {})[ticker] = {"id": order_id, "fingerprint": (rec or {}).get("fingerprint"),
                                           "stop": stop, "placed_at": _now()}
    _write_json(path, st)


def stop_rejected_alarm(client, ticker, qty, stop, how):
    guard_alarm(client, "STOP %s for %s (SELL %s at %.2f). The guard now ENFORCES IT IN SOFTWARE: it sells at "
                        "market when the price is at or under %.2f, checking every minute. Keep the Mac awake and "
                        "the guard running, or set a stop yourself in the app."
                % (how, ticker, _qty(qty), stop, stop))


REALERT_EVERY = 3600              # seconds: a position with no stop order is alarmed again at most this often


def stop_still_missing(g, ticker, qty, sw):
    """A position whose stop order was refused or is gone, held under a software stop: the urgent alert
    again, at most once an hour per position (the first one went out when it happened)."""
    realert = g.state.setdefault("realert", {})
    last = _num(realert.get(ticker)) or _num(sw.get("alerted"))
    if last is None:
        realert[ticker] = _now()
        g.save()
        return
    if _now() - last < REALERT_EVERY:
        return
    realert[ticker] = _now()
    g.save()
    stop_rejected_alarm(g.client, ticker, qty, float(sw["stop"]),
                        "STILL NOT PLACED (%s)" % (sw.get("why") or "no stop order"))


def guard_ticket(state, ticker, env):
    """The latest entry t212.py place sent for this ticker on this account that had a stop, or None."""
    found = None
    for r in (state or {}).get("orders", []):
        if (r.get("broker") == "trading212" and r.get("env") == env and r.get("t212_ticker") == ticker
                and str(r.get("side", "")).lower() == "buy" and r.get("kind") == "entry"
                and r.get("status") in GUARD_STATUSES and _num(r.get("stop")) and _num(r.get("qty"))):
            found = r
    return found


STRATEGIES = ("intraday", "drift")


def position_strategy(rec):
    """"intraday" or "drift" for a buy record. No label (records from before labels) is intraday."""
    s = str((rec or {}).get("strategy") or "intraday").strip().lower()
    return s if s in STRATEGIES else "intraday"


def is_drift(rec):
    return rec is not None and position_strategy(rec) == "drift"


def position_record(state, ticker, env):
    """The latest buy record this pack wrote for the ticker on this account, stop or not, or None.
    A buy Trading 212 refused ("failed") bought nothing, so it is not counted."""
    found = None
    for r in (state or {}).get("orders", []):
        if (r.get("broker") == "trading212" and r.get("env") == env and r.get("t212_ticker") == ticker
                and str(r.get("side", "")).lower() == "buy" and r.get("kind") == "entry"
                and r.get("status") != "failed"):
            found = r
    return found


def _iso_date(v):
    try:
        return datetime.date.fromisoformat(str(v).strip()[:10])
    except (TypeError, ValueError):
        return None


def drift_due(rec, today):
    """(due, why) for a drift record on the New York date `today` (a date or "YYYY-MM-DD").
    Due on its sell_date or later. A drift record with no readable sell_date is due (sold, as intraday is)."""
    d = _iso_date(rec.get("sell_date"))
    t = today if isinstance(today, datetime.date) else _iso_date(today)
    if d is None:
        return True, "drift record with no readable sell_date, so closed like an intraday position"
    if t >= d:
        return True, "drift sell date %s%s" % (d.isoformat(), "" if t == d else ", overdue")
    return False, "drift, sell date %s" % d.isoformat()


_ALERTS = []


def alerts_module():
    """trader-alerts/alerts.py, for the NYSE holiday calendar."""
    if not _ALERTS:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-alerts", "alerts.py")
        spec = importlib.util.spec_from_file_location("trader_alerts_calendar", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ALERTS.append(mod)
    return _ALERTS[0]


def drift_sell_date(entry_date, n):
    """The New York trading date n trading days after entry_date (a date or "YYYY-MM-DD"), skipping
    weekends and NYSE holidays. Returns "YYYY-MM-DD". Refuses a date the holiday list does not cover."""
    al = alerts_module()
    d = entry_date if isinstance(entry_date, datetime.date) else _iso_date(entry_date)
    if d is None:
        raise T212Error("drift_sell_date wants an entry date like 2026-11-10, not %r" % (entry_date,), 2)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise T212Error("drift_sell_date wants a whole number of trading days, 1 or more, not %r" % (n,), 2)
    left = n
    while left:
        d += datetime.timedelta(days=1)
        if d.year not in al.CALENDAR_YEARS:
            raise T212Error("the NYSE holiday list covers %s only, so a sell date in %d cannot be worked out; "
                            "add that year's holidays to trader-alerts/alerts.py"
                            % (" and ".join(map(str, al.CALENDAR_YEARS)), d.year), 2)
        if al.trading_day(d)[0]:
            left -= 1
    return d.isoformat()


def guard_send_stop(client, ticker, qty, stop):
    """A guardian write. It builds the body itself: a STOP that SELLS (negative
    quantity). Anything else is refused before a request is made, so it can never buy."""
    body = stop_body(ticker, qty, stop)
    if not (isinstance(body.get("quantity"), (int, float)) and body["quantity"] < 0):
        raise T212Error("guard refused to send: a stop must sell (negative quantity)", 2)
    check_sign(body, "sell")
    status, resp = client.post_order(STOP, body, "sell")
    return body, resp


def guard_send_sell(client, ticker, qty):
    """The other guardian write: a MARKET SELL of shares you hold (half at the target).
    Built and sign-checked here; a buy is refused before any request."""
    body = market_body(ticker, "sell", qty)
    if not (isinstance(body.get("quantity"), (int, float)) and body["quantity"] < 0):
        raise T212Error("guard refused to send: it only ever sells (negative quantity)", 2)
    check_sign(body, "sell")
    status, resp = client.post_order(MARKET, body, "sell")
    return body, resp


# ---------------------------------------------------------------- trailing stop

TRAIL_ACTIVATE_R = 1.0        # the rule: move the stop up only once the trade is 1R in profit


def trail_settings(cfg):
    """None (off) or {"distance_r" | "distance_pct", "half", "activate_r"} from "trailing_stop" in the config.
    activate_r (default 1): the trail starts only once the high is that many R over the entry; 0 = at once."""
    t = (cfg or {}).get("trailing_stop")
    if not t or not t.get("enabled"):
        return None
    if not isinstance(t, dict):
        raise T212Error("trailing_stop in trader/config.json must be an object", 2)
    r, p = _num(t.get("distance_r")), _num(t.get("distance_pct"))
    if (r is None) == (p is None):
        raise T212Error("trailing_stop needs exactly one of distance_r (in R) or distance_pct", 2)
    if r is not None and not 0.1 <= r <= 10:
        raise T212Error("trailing_stop.distance_r must be between 0.1 and 10 (R)", 2)
    if p is not None and not 0.1 <= p <= 50:
        raise T212Error("trailing_stop.distance_pct must be between 0.1 and 50", 2)
    a = _num(t.get("activate_r")) if t.get("activate_r") is not None else TRAIL_ACTIVATE_R
    if a is None or not 0 <= a <= 10:
        raise T212Error("trailing_stop.activate_r must be between 0 and 10 (R in profit before the trail starts)", 2)
    return {"distance_r": r, "distance_pct": p, "half": bool(t.get("sell_half_at_target")), "activate_r": a}


def _cent_down(x):
    return math.floor(round(x * 100, 6)) / 100.0


def trail_level(trail, high, risk):
    """The trailing stop for a high-water mark: high minus the distance, rounded down to the cent."""
    if trail["distance_r"] is not None:
        if not risk or risk <= 0:
            return None
        return _cent_down(high - trail["distance_r"] * risk)
    return _cent_down(high * (1 - trail["distance_pct"] / 100.0))


def trail_armed(trail, high, entry, risk):
    """(True, None) once the high is activate_r x risk over the entry (the "1R in profit" rule),
    else (False, why). activate_r 0 trails from the entry."""
    a = trail.get("activate_r") or 0
    if a <= 0:
        return True, None
    if not entry or not risk or risk <= 0:
        return False, "not trailed: no entry price in the ticket record to measure %gR of profit from" % a
    at = entry + a * risk
    if high + 1e-9 < at:
        return False, "not trailed yet: the trail starts at %gR profit (%.2f), high so far %.2f" % (a, at, high)
    return True, None


def _gone(client, order_id):
    """Has a cancelled order left the pending list? A few looks, 5 seconds apart."""
    for _ in range(GUARD_GONE_POLLS):
        if not any(o.get("id") == order_id for o in client.get(ORDERS) or []):
            return True
    return False


class _Guard(object):
    """One pass's bookkeeping: state file, log, warnings."""

    def __init__(self, client, state_dir, out):
        self.client, self.sd, self.out = client, state_dir, out
        self.path = os.path.join(state_dir, "state", "t212-guard.json")
        self.state = _read_json(self.path, {}) or {}
        self.unclear = self.state.setdefault("unclear", {})
        self.trail = self.state.setdefault("trail", {})

    def save(self):
        _write_json(self.path, self.state)

    def log(self, event, **kw):
        log_event(self.sd, self.client._secrets, event, env=self.client.env, by="guard", **kw)

    def warn(self, text):
        self.out["warnings"].append(text)

    def stop(self, ticker, qty, price, why, rec=None):
        """Place a stop; returns ('ok', resp) / ('rejected', err) / ('unknown', err). A placed stop
        becomes the one the check a minute later (and every cycle after) looks up, so a stop the
        guard replaced is never mistaken for the one that protects the position."""
        self.log("request", method="POST", path=STOP, body=stop_body(ticker, qty, price), why=why)
        try:
            _, resp = guard_send_stop(self.client, ticker, qty, price)
        except PlaceError as e:
            self.log("response", path=STOP, status=e.status, outcome=e.outcome, error=str(e))
            if e.outcome != "rejected":
                self.unclear[ticker] = "stop SELL %s at %.2f: %s at %s" % (_qty(qty), price, e, self.out["at"])
                self.save()
            return e.outcome, e
        self.log("response", path=STOP, body=resp)
        self.state.setdefault("placed", []).append({"at": self.out["at"], "ticker": ticker, "qty": qty,
                                                    "stop": price, "why": why, "order_id": resp.get("id")})
        if rec is not None and resp.get("id") is not None:
            self.state.setdefault("verify", {})[ticker] = {"id": resp.get("id"), "fingerprint": rec.get("fingerprint"),
                                                           "stop": price, "placed_at": _now()}
            self.out["verify_soon"] = True
        self.save()
        return "ok", resp

    def cancel(self, ticker, order_id):
        self.log("request", method="DELETE", path=CANCEL, order_id=order_id)
        try:
            self.client.cancel_order(order_id)
        except PlaceError as e:
            self.log("response", path=CANCEL, status=e.status, outcome=e.outcome, error=str(e))
            if e.outcome != "rejected":
                self.unclear[ticker] = "cancel of stop %s: %s at %s" % (order_id, e, self.out["at"])
                self.save()
            return e.outcome, e
        self.log("response", path=CANCEL, order_id=order_id, status=200)
        own = self.state.setdefault("cancelled", [])
        own.append({"at": self.out["at"], "ticker": ticker, "order_id": order_id})
        del own[:-GUARD_CANCELLED_KEEP]
        self.save()
        return "ok", None

    def cancelled_by_guard(self, order_id):
        return any(x.get("order_id") == order_id for x in self.state.get("cancelled") or [])


def guard_trail(g, row, rec, stop_order, held, price, trail, cfg):
    """Raise the one pending stop to the trail (never lower it); with sell_half_at_target,
    sell half at the target first. Cancel, then place, because Trading 212 has no modify
    and no trailing order; a refused new stop puts the old one back."""
    ticker = row["ticker"]
    cur = _num(stop_order.get("stopPrice"))
    oid = stop_order.get("id")
    sq = abs(_num(stop_order.get("quantity")) or 0)
    entry, stop0, target = _num(rec.get("entry")), float(rec["stop"]), _num(rec.get("target"))
    risk = (entry - stop0) if entry else None
    t = g.trail.get(ticker)
    if not t or t.get("fingerprint") != rec.get("fingerprint"):
        t = g.trail[ticker] = {"fingerprint": rec.get("fingerprint"), "high": entry or price,
                               "stop": cur, "half_sold": False}
    if price > t["high"]:
        t["high"] = price
    row["high"] = t["high"]
    g.save()
    if cur is None or not isinstance(oid, int):
        row["trail"] = "not trailed: the stop order has no price or id"
        return
    new = trail_level(trail, t["high"], risk)
    if new is None:
        row["trail"] = "not trailed: no entry price in the ticket record to measure R from (use distance_pct)"
        return
    new = max(new, cur, _num(t.get("stop")) or 0)            # never lower
    if new >= price - 0.01:
        new = cur                                             # a stop at the price would sell at once
    armed, why = trail_armed(trail, t["high"], entry, risk)
    if not armed:
        new = max(cur, _num(t.get("stop")) or 0)              # under the activation profit: no raise at all
    step = max(0.05, 0.1 * (risk or new * 0.01))

    if trail["half"] and not t.get("half_sold") and target and price >= target and sq > 0:
        unit = 0.01 if (cfg.get("fractional") and str(cfg.get("broker", "")).lower() in ("trading212", "t212")) \
            or abs(sq - round(sq)) > 1e-9 else 1
        half = _floor(sq / 2.0, unit)
        rest = round(sq - half, 2)
        if half <= 0:
            row["trail"] = "half at target: %s shares cannot be halved" % _qty(sq)
            t["half_sold"] = True
            g.save()
        else:
            o, err = g.cancel(ticker, oid)
            if o != "ok":
                g.warn("%s reached the target %.2f but the stop could not be cancelled (%s), so nothing was sold. "
                       "Not retried%s." % (ticker, target, err, "" if o == "rejected" else "; it is UNCLEAR, check the app"))
                return
            _gone(g.client, oid)
            g.log("request", method="POST", path=MARKET, body=market_body(ticker, "sell", half), why="half at target")
            try:
                _, resp = guard_send_sell(g.client, ticker, half)
            except PlaceError as e:
                g.log("response", path=MARKET, status=e.status, outcome=e.outcome, error=str(e))
                if e.outcome == "rejected":
                    o2, _ = g.stop(ticker, sq, cur, "put back after a refused half sale", rec)
                    g.warn("%s: the half sale at the target was refused (%s); the stop %s"
                           % (ticker, e, "was put back at %.2f" % cur if o2 == "ok" else
                              "COULD NOT BE PUT BACK: the position has NO STOP. Set one in the app now"))
                    return
                g.unclear[ticker] = "half sale SELL %s: %s at %s" % (_qty(half), e, g.out["at"])
                g.save()
                o2, _ = g.stop(ticker, rest, new, "after an unclear half sale", rec)
                g.warn("%s: the half sale is UNCLEAR (%s). A stop for the other %s was %s. The guard stops "
                       "touching %s until you check the app and clear it in %s."
                       % (ticker, e, _qty(rest), "placed at %.2f" % new if o2 == "ok" else "NOT placed", ticker, g.path))
                return
            g.log("response", path=MARKET, body=resp)
            t["half_sold"] = True
            g.state.setdefault("sold", []).append({"at": g.out["at"], "ticker": ticker, "qty": half,
                                                   "price_seen": price, "target": target, "order_id": resp.get("id")})
            g.save()
            o2, err2 = g.stop(ticker, rest, new, "the rest after half at target", rec)
            if o2 == "ok":
                t["stop"] = new
                g.save()
                row["trail"] = "SOLD HALF (%s) at the target, stop on the other %s at %.2f" % (_qty(half), _qty(rest), new)
            else:
                g.warn("%s: sold half at the target, but the stop for the other %s %s (%s). %s"
                       % (ticker, _qty(rest), "was refused" if o2 == "rejected" else "is UNCLEAR", err2,
                          "The next pass puts it back." if o2 == "rejected" else "Check the app now."))
            g.out["placed"] += 1
            return

    if not armed:
        row["trail"] = "stop %.2f; %s" % (cur, why)
        return
    if new < cur + step - 1e-9:
        row["trail"] = "stop %.2f, high %.2f: no raise yet (trail at %.2f)" % (cur, t["high"], max(new, cur))
        return
    o, err = g.cancel(ticker, oid)
    if o != "ok":
        g.warn("%s: could not cancel the stop at %.2f to raise it to %.2f (%s). %s"
               % (ticker, cur, new, err, "It stays where it is." if o == "rejected"
                  else "UNCLEAR: check the app; the guard leaves %s alone until you clear it." % ticker))
        return
    _gone(g.client, oid)
    o, err = g.stop(ticker, sq, new, "trail raise from %.2f" % cur, rec)
    if o == "ok":
        t["stop"] = new
        g.save()
        row["trail"] = "RAISED the stop %.2f -> %.2f (high %.2f)" % (cur, new, t["high"])
        g.out["placed"] += 1
        return
    if o == "rejected":
        o2, _ = g.stop(ticker, sq, cur, "put back after a refused raise", rec)
        g.warn("%s: the raised stop %.2f was refused (%s); the old stop %s"
               % (ticker, new, err, "was put back at %.2f" % cur if o2 == "ok" else
                  "COULD NOT BE PUT BACK: the position has NO STOP. Set one in the app now"))
        return
    g.warn("%s: the raised stop %.2f is UNCLEAR (%s). Check the app; the guard leaves %s alone until you "
           "clear it in %s." % (ticker, new, err, ticker, g.path))


def guard_market(g, row, rec, held, ticket_qty, price, trail, cfg):
    """stop_mode "market": no stop order at Trading 212. At or under the stop (the ticket's,
    or the trail's once it has risen) the guard sells the ticket's shares at market. With the
    trail it keeps the high and raises its own stop level (never lowers it); with
    sell_half_at_target it sells half at market at the first check at or above the target."""
    ticker = row["ticker"]
    entry, stop0, target = _num(rec.get("entry")), float(rec["stop"]), _num(rec.get("target"))
    risk = (entry - stop0) if entry else None
    t = g.trail.get(ticker)
    if not t or t.get("fingerprint") != rec.get("fingerprint"):
        t = g.trail[ticker] = {"fingerprint": rec.get("fingerprint"), "high": entry or price or stop0,
                               "stop": stop0, "half_sold": False}
    level = max(stop0, _num(t.get("stop")) or 0)
    row["stop"] = level
    if price is None:
        row["status"] = "no price"
        g.warn("%s: Trading 212 sent no current price, so the stop %.2f could not be checked this time"
               % (ticker, level))
        return
    qty = _floor(min(held, ticket_qty), 0.01 if (min(held, ticket_qty) % 1) > 1e-9 else 1)
    if price <= level + 0.005:
        if qty <= 0:
            row["status"] = "nothing to sell"
            return
        g.log("request", method="POST", path=MARKET, body=market_body(ticker, "sell", qty), why="stop %.2f hit" % level)
        try:
            _, resp = guard_send_sell(g.client, ticker, qty)
        except PlaceError as e:
            g.log("response", path=MARKET, status=e.status, outcome=e.outcome, error=str(e))
            if e.outcome != "rejected":
                g.unclear[ticker] = "stop sale SELL %s: %s at %s" % (_qty(qty), e, g.out["at"])
                g.save()
            row["status"] = "STOP SALE FAILED" if e.outcome == "rejected" else "unclear"
            g.warn("%s is at %s, at or under the stop %.2f, and the market sell of %s %s (%s). %s"
                   % (ticker, _fmt(price), level, _qty(qty), "was refused" if e.outcome == "rejected" else
                      "is UNCLEAR", e, "The next check tries again." if e.outcome == "rejected"
                      else "Check the app NOW; the guard will not send it again."))
            return
        g.log("response", path=MARKET, body=resp)
        g.state.setdefault("sold", []).append({"at": g.out["at"], "ticker": ticker, "qty": qty, "why": "stop",
                                               "stop": level, "price_seen": price, "order_id": resp.get("id")})
        t["sold_at_stop"] = g.out["at"]
        g.save()
        row.update(status="SOLD AT STOP", trail="market SELL %s at the %.2f stop (seen %s), order id %s"
                   % (_qty(qty), level, _fmt(price), _ident(resp.get("id"))))
        g.out["placed"] += 1
        return
    if trail and trail["half"] and not t.get("half_sold") and target and price >= target:
        half = _floor(qty / 2.0, 0.01 if (cfg.get("fractional") or qty % 1 > 1e-9) else 1)
        if half > 0:
            g.log("request", method="POST", path=MARKET, body=market_body(ticker, "sell", half), why="half at target")
            try:
                _, resp = guard_send_sell(g.client, ticker, half)
            except PlaceError as e:
                g.log("response", path=MARKET, status=e.status, outcome=e.outcome, error=str(e))
                if e.outcome != "rejected":
                    g.unclear[ticker] = "half sale SELL %s: %s at %s" % (_qty(half), e, g.out["at"])
                g.save()
                g.warn("%s: the half sale at the target %s (%s)." % (ticker, "was refused" if e.outcome == "rejected"
                                                                     else "is UNCLEAR, check the app", e))
                return
            g.log("response", path=MARKET, body=resp)
            g.state.setdefault("sold", []).append({"at": g.out["at"], "ticker": ticker, "qty": half,
                                                   "why": "half at target", "price_seen": price,
                                                   "order_id": resp.get("id")})
            row["trail"] = "SOLD HALF (%s) at the target" % _qty(half)
            g.out["placed"] += 1
        t["half_sold"] = True
    if price > t["high"]:
        t["high"] = price
    row["high"] = t["high"]
    if trail:
        new = trail_level(trail, t["high"], risk)
        armed, why = trail_armed(trail, t["high"], entry, risk)
        if new is not None and not armed:
            row["trail"] = ((row.get("trail") or "") + "; " if row.get("trail") else "") + why
        elif new is not None and level < new < price - 0.01:
            row["trail"] = ((row.get("trail") or "") + "; " if row.get("trail") else "") + \
                "raised the guard's stop %.2f -> %.2f (high %.2f)" % (level, new, t["high"])
            level = t["stop"] = new
        elif new is None:
            row["trail"] = "not trailed: no entry price in the ticket record (use distance_pct)"
    row.update(status="watching", stop=level)
    g.save()


def guard_known_stop(g, rec, ticker, stops):
    """The newest stop order id this pack placed for the ticket: the guard's own, else place's."""
    best = None
    for x in g.state.get("placed") or []:
        if x.get("ticker") == ticker and x.get("order_id"):
            best = x["order_id"]
    if best is None and rec.get("status") == "stop_placed":
        best = rec.get("broker_order_id")
    if best is None:
        return None
    return {"id": best, "fingerprint": rec.get("fingerprint"), "stop": rec.get("stop"), "placed_at": 0}


def guard_pass(client, state_dir):
    """One look at every open position. Returns {"at", "positions": [...], "warnings": [...], "placed": n}."""
    if client.env != "demo" and not (client.live_ok or LIVE_PLACEMENT_ENABLED):
        raise T212Error("live trading is switched off, so the guard does not run on live; nothing was sent", 2)
    tk = ticket_module()
    try:
        cfg = tk.read_config(os.path.join(state_dir, "config.json"))
    except tk.TicketError as e:
        raise T212Error(str(e), 2)
    trail = trail_settings(cfg)
    mode = stop_mode(cfg)
    state = tk.load_state(os.path.join(state_dir, "state", "broker.json"))
    positions = client.get(POSITIONS) or []
    pending = client.get(ORDERS) or []
    out = {"at": tk.ny_iso(_utc_now()), "env": client.env, "positions": [], "warnings": [], "placed": 0,
           "trailing": trail, "stop_mode": mode}
    g = _Guard(client, state_dir, out)
    software = g.state.setdefault("software", {})
    verify = g.state.setdefault("verify", {})
    frac_mode = fractional_stops(cfg)
    held_tickers = set()
    for p in positions:
        ticker = _o_ticker(p)
        held = _num(p.get("quantity")) or 0
        if not ticker or held <= 0:
            continue
        held_tickers.add(ticker)
        price = _num(p.get("currentPrice"))
        row = {"ticker": ticker, "held": held, "price": price}
        out["positions"].append(row)
        stops, others = guard_sell_orders(pending, ticker)
        covered = sum(abs(_num(o.get("quantity")) or 0) for o in stops)
        row["covered"] = covered
        if others or (mode == "market" and stops):
            row["status"] = "exit pending"
            row["note"] = "a sell is already pending at Trading 212 (%s); the guard adds nothing on top" % ", ".join(
                str(o.get("type", "?")) for o in others + (stops if mode == "market" else []))
            continue
        rec = guard_ticket(state, ticker, client.env)
        if rec is not None and mode == "broker":
            v = verify.get(ticker)
            if v is None:                         # a stop placed before verification existed: track the latest
                v = guard_known_stop(g, rec, ticker, stops)
                if v:
                    verify[ticker] = v
            if v and v.get("fingerprint") == rec.get("fingerprint"):
                alive = [o for o in stops if o.get("id") == v["id"]]
                if alive:
                    v["checked"] = out["at"]
                else:
                    how, raw = stop_status(client, ticker, v["id"], pending)
                    if how == "filled":
                        verify.pop(ticker, None)
                    elif how == "cancelled" and g.cancelled_by_guard(v["id"]):
                        # the guard cancelled it itself to raise it: not gone, replaced. Track the live one;
                        # with none live, the missing-stop step below puts a stop back
                        verify.pop(ticker, None)
                        if len(stops) == 1 and stops[0].get("id") is not None:
                            verify[ticker] = {"id": stops[0]["id"], "fingerprint": rec.get("fingerprint"),
                                              "stop": _num(stops[0].get("stopPrice")), "placed_at": _now(),
                                              "checked": out["at"]}
                    elif how != "ok":
                        verify.pop(ticker, None)
                        stop_now = max(float(rec["stop"]), _num((g.trail.get(ticker) or {}).get("stop")) or 0)
                        software[ticker] = {"fingerprint": rec.get("fingerprint"), "stop": stop_now,
                                            "why": "stop order %s was %s (%s)" % (_ident(v["id"]), how, raw),
                                            "since": out["at"], "alerted": _now()}
                        g.save()
                        stop_rejected_alarm(client, ticker, held, stop_now,
                                            "REJECTED by Trading 212" if how == "rejected" else
                                            "GONE at Trading 212 (%s)" % raw)
                g.save()
        sw = software.get(ticker) if rec is not None else None
        use_software = rec is not None and mode == "broker" and (
            (sw and sw.get("fingerprint") == rec.get("fingerprint")) or rec.get("status") == "software_stop"
            or (frac_mode == "software" and is_fractional(held)))
        if use_software and covered + 1e-9 < held:
            if g.unclear.get(ticker):
                row["status"] = "unclear"
                g.warn("%s: an earlier guard order came back unclear (%s). Nothing is sent for it; check the app"
                       % (ticker, g.unclear[ticker]))
                continue
            row["software"] = (sw or {}).get("why") or "fractional_stops software"
            if sw and sw.get("fingerprint") == rec.get("fingerprint"):
                stop_still_missing(g, ticker, held - covered, sw)
            ticket_qty = min(float(_num(rec["qty"])), held - covered)
            guard_market(g, row, rec, held - covered, ticket_qty, row["price"], None if is_drift(rec) else trail, cfg)
            if row.get("status") == "watching":
                row["status"] = "software stop"
                row["note"] = "the guard sells at market at or under %.2f (%s)" % (row["stop"], row["software"])
            out["software"] = out.get("software", 0) + 1
            continue
        if rec is None:
            if covered + 1e-9 >= held:
                row["status"] = "covered"
            else:
                row["status"] = "no ticket stop"
                g.warn("%s: %s held, %s covered by a stop, and no t212.py place ticket with a stop for it. "
                       "Set a stop yourself in the app." % (ticker, _qty(held), _qty(covered)))
            continue
        tr = g.trail.get(ticker) or {}
        trailed = _num(tr.get("stop")) if tr.get("fingerprint") == rec.get("fingerprint") else None
        stop = max(float(rec["stop"]), trailed or 0)              # a trailed stop is never put back lower
        ticket_qty = float(_num(rec["qty"]))
        if tr.get("half_sold") and tr.get("fingerprint") == rec.get("fingerprint"):
            ticket_qty = min(ticket_qty, held)
        row["stop"] = stop
        if g.unclear.get(ticker):
            row["status"] = "unclear"
            g.warn("%s: an earlier guard order came back unclear (%s). Nothing is sent for it; check the app, "
                   "then delete %s from \"unclear\" in %s" % (ticker, g.unclear[ticker], ticker, g.path))
            continue
        if mode == "market":
            guard_market(g, row, rec, held, ticket_qty, price, None if is_drift(rec) else trail, cfg)
            continue
        want = min(held, ticket_qty) - covered          # never more than the ticket bought
        want = _floor(want, 0.01 if (want % 1) > 1e-9 else 1)
        if want <= 0:
            row["status"] = "covered"
            if is_drift(rec):
                row["trail"] = "drift position: no trailing or half sale, the stop stays where it is"
            elif trail and price is not None:
                guard_trail(g, row, rec, stops[0], held, price, trail, cfg) if len(stops) == 1 else \
                    row.update(trail="not trailed: %d stop orders cover it, the guard trails exactly one" % len(stops))
            continue
        if price is None or price <= stop + 0.005:
            row["status"] = "below stop"
            g.warn("%s is at %s, at or under the stop %.2f: a stop now would sell at once, so the guard did not "
                   "place it. Decide yourself in the app."
                   % (ticker, _fmt(price) if price is not None else "an unknown price", stop))
            continue
        o, err = g.stop(ticker, want, stop, "missing stop", rec)
        if o != "ok":
            row["status"] = "stop failed" if o == "rejected" else "unclear"
            g.warn("%s: the guard's stop (SELL %s at %.2f) %s: %s. Not retried."
                   % (ticker, _qty(want), stop, "was refused" if o == "rejected" else "is UNCLEAR", err))
            if o == "rejected":           # refused outright: protect it in software from now on
                software[ticker] = {"fingerprint": rec.get("fingerprint"), "stop": stop,
                                    "why": "the guard's stop was refused (%s)" % err, "since": out["at"],
                                    "alerted": _now()}
                g.save()
                stop_rejected_alarm(client, ticker, want, stop, "REFUSED by Trading 212")
            continue
        oid = err.get("id") if isinstance(err, dict) else None
        row.update(status="stop placed", placed_qty=want, order_id=oid)
        out["placed"] += 1
    for t in [t for t in g.state.get("realert") or {} if t not in held_tickers]:
        g.state["realert"].pop(t, None)           # sold: no more reminders for it
        g.save()
    for o in pending:
        t = _o_ticker(o)
        if t and t not in held_tickers and o in guard_sell_orders(pending, t)[0]:
            g.warn("%s: a sell stop (%s at %s) is pending but you hold none; it is good till cancelled, so "
                   "cancel it in the app" % (t, _qty(o.get("quantity")), _fmt(o.get("stopPrice"))))
    return out


def render_guard(res):
    lines = ["%s  stop guard (%s stops) on %s: %d open position%s, %d order%s sent%s" % (
        res["at"], res.get("stop_mode", "broker"), res["env"], len(res["positions"]), "" if len(res["positions"]) == 1 else "s",
        res["placed"], "" if res["placed"] == 1 else "s",
        "" if not res.get("trailing") else ", trailing %s%s" % (
            ("%sR" % _fmt(res["trailing"]["distance_r"])) if res["trailing"]["distance_r"] is not None
            else "%s%%" % _fmt(res["trailing"]["distance_pct"]),
            ", half at target" if res["trailing"]["half"] else ""))]
    for r in res["positions"]:
        extra = ""
        if r.get("status") == "watching":
            extra = ": stop %.2f, price %s" % (r["stop"], _fmt(r.get("price")))
        if r.get("status") == "stop placed":
            extra = ": SELL %s at %.2f, good till cancelled, order id %s" % (_qty(r["placed_qty"]), r["stop"],
                                                                           _ident(r.get("order_id")))
        elif r.get("note"):
            extra = ": " + r["note"]
        if r.get("trail"):
            extra += "; " + r["trail"]
        lines.append("  %-12s %s held, %s covered  %s%s" % (r["ticker"], _qty(r["held"]), _qty(r.get("covered", 0)),
                                                         r.get("status", "?"), extra))
    for w in res["warnings"]:
        lines.append("  !!! " + w)
    return "\n".join(lines)


# ---------------------------------------------------------------- end-of-day close (demo, exits only)

EOD_AT = "15:50"


def eod_settings(cfg):
    """None (off) or {"at": (h, m)} from "eod_close" in the config. Off when it is absent."""
    e = (cfg or {}).get("eod_close")
    if not e:
        return None
    if not isinstance(e, dict):
        raise T212Error("eod_close in trader/config.json must be an object, e.g. {\"enabled\": true, \"at\": \"15:50\"}", 2)
    if not e.get("enabled"):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(e.get("at") or EOD_AT).strip())
    if not m or not (9 <= int(m.group(1)) < 16 and 0 <= int(m.group(2)) < 60):
        raise T212Error("eod_close.at must be a New York time in the session, like 15:50", 2)
    return {"at": (int(m.group(1)), int(m.group(2)))}


def eod_close(client, state_dir):
    """Sell the day's positions at market, after cancelling their pending sells (the stop holds
    the shares). Exits only: every write is a cancel or a SELL. Demo only.
    Which positions, from their buy record in trader/state/broker.json:
      intraday (or no strategy label): sold
      drift: sold on or after its sell_date; before it, kept with its stop untouched
      no buy record at all: not sold, and a GUARD ALERT says so
    Returns {"at", "closed": [...], "failed": [...], "kept": [...], "unknown": [...], "nothing": bool}."""
    if client.env != "demo":
        raise T212Error("the end-of-day close runs on the demo account only; nothing was sent", 2)
    tk = ticket_module()
    out = {"at": tk.ny_iso(_utc_now()), "env": client.env, "positions": [], "warnings": [], "placed": 0,
           "closed": [], "failed": [], "sold": [], "kept": [], "unknown": [], "overdue": []}
    g = _Guard(client, state_dir, out)
    state = tk.load_state(os.path.join(state_dir, "state", "broker.json"))
    today = _today_ny(tk)
    positions = client.get(POSITIONS) or []
    pending = client.get(ORDERS) or []
    for p in positions:
        ticker = _o_ticker(p)
        held = _num(p.get("quantity")) or 0
        if not ticker or held <= 0:
            continue
        rec = position_record(state, ticker, client.env)
        if rec is None:
            out["unknown"].append("%s: %s held with no buy record from this pack, so the end-of-day close did NOT "
                                  "sell it. Sell it yourself in the app if it should go" % (ticker, _qty(held)))
            continue
        why = ""
        if is_drift(rec):
            due, why = drift_due(rec, today)
            if not due:
                out["kept"].append("%s: %s held, %s: kept, its stop left in place" % (ticker, _qty(held), why))
                continue
            if "overdue" in why:
                out["overdue"].append("%s: its drift sell date was %s, so it is sold at today's close instead "
                                      "(the Mac or the guard was off on the day)" % (ticker, rec.get("sell_date")))
        stops, others = guard_sell_orders(pending, ticker)
        if g.unclear.get(ticker):
            out["failed"].append("%s: NOT CLOSED, an earlier guard order is unclear (%s); sell it in the app"
                                 % (ticker, g.unclear[ticker]))
            continue
        cancelled, ok = [], True
        for o in stops + others:
            res, err = g.cancel(ticker, o.get("id"))
            if res != "ok" or not _gone(client, o.get("id")):
                ok = False
                out["failed"].append("%s: NOT CLOSED, could not cancel its pending %s %s (%s); its stop may "
                                     "still be there. Sell it in the app" % (ticker, o.get("type", "?"), _ident(o.get("id")),
                                                                            err if res != "ok" else "still pending"))
                break
            cancelled.append(o)
        if not ok:
            continue
        g.log("request", method="POST", path=MARKET, body=market_body(ticker, "sell", held), why="end-of-day close")
        try:
            _, resp = guard_send_sell(client, ticker, held)
        except PlaceError as e:
            g.log("response", path=MARKET, status=e.status, outcome=e.outcome, error=str(e))
            if e.outcome != "rejected":
                g.unclear[ticker] = "end-of-day SELL %s: %s at %s" % (_qty(held), e, out["at"])
                g.save()
                out["failed"].append("%s: UNCLEAR whether the end-of-day SELL %s went through (%s); its stop was "
                                     "cancelled. Check the app NOW" % (ticker, _qty(held), e))
                continue
            back = []
            for o in cancelled:
                price = _num(o.get("stopPrice"))
                if price:
                    r, _ = g.stop(ticker, abs(_num(o.get("quantity")) or held), price, "restore after a failed close")
                    back.append("stop %.2f %s" % (price, "put back" if r == "ok" else "NOT put back"))
            out["failed"].append("%s: the end-of-day SELL %s was REFUSED (%s); %s. The position is still open"
                                 % (ticker, _qty(held), e, "; ".join(back) or "it had no stop"))
            continue
        g.log("response", path=MARKET, body=resp)
        out["placed"] += 1
        out["sold"].append({"ticker": ticker, "qty": held, "price_seen": _num(p.get("currentPrice")),
                            "order_id": resp.get("id")})       # for the evidence report's exit slippage
        out["closed"].append("%s: SELL %s at market, order id %s%s%s" % (
            ticker, _qty(held), _ident(resp.get("id")), " (%s)" % why if why else "",
            "" if not cancelled else ", cancelled %s" % ", ".join(
                "%s %s" % (o.get("type", "?"), _fmt(o.get("stopPrice"))) for o in cancelled)))
    g.state["eod"] = {"date": today, "at": out["at"], "closed": out["closed"], "failed": out["failed"],
                      "sold": out["sold"], "kept": out["kept"], "unknown": out["unknown"]}
    g.save()
    out["nothing"] = not out["closed"] and not out["failed"] and not out["kept"] and not out["unknown"]
    return out


def render_eod(res):
    lines = ["END-OF-DAY CLOSE (%s) %s" % (res["env"], res["at"])]
    if res["nothing"]:
        lines.append("  nothing open: nothing sold")
    lines += ["  closed: " + x for x in res["closed"]]
    lines += ["  kept: " + x for x in res.get("kept", [])]
    lines += ["  !!! " + x for x in res["failed"] + res.get("unknown", [])]
    return "\n".join(lines)


def eod_done_today(state_dir):
    st = _read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}
    return (st.get("eod") or {}).get("date") == _today_ny(ticket_module())


def run_eod(client, state_dir):
    """The close, then your phone: what was sold, and a loud alarm for anything that was not."""
    res = eod_close(client, state_dir)
    text = render_eod(res)
    _say(text)
    _phone_push(client, text)
    for f in res["failed"]:
        guard_alarm(client, "END-OF-DAY CLOSE FAILED: " + f)
    for f in res.get("unknown", []):
        guard_alarm(client, "END-OF-DAY CLOSE: " + f)
    for f in res.get("overdue", []):
        guard_alarm(client, "DRIFT SELL OVERDUE: " + f)
    return res


GUARD_FAIL_ALERT = 3          # failed checks in a row before the guard says so out loud
AUTH_STATUSES = (401, 403)    # the key is rejected or lacks a permission: retrying cannot fix it


def _notify(title, text):
    """A macOS notification with a sound, so a guard that cannot protect you is not silent.
    Best effort: the same text always goes to the log as well."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/osascript"):
        return False
    try:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv",
                        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Basso"',
                        "-e", "end run", title, text], timeout=10, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


_PHONE = []
_NET = []


def net_module():
    """trader-broker/netwait.py: is the internet there, and waiting for it to come back."""
    if not _NET:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netwait.py")
        spec = importlib.util.spec_from_file_location("trader_broker_netwait", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _NET.append(mod)
    return _NET[0]


def phone_module():
    if not _PHONE:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.py")
        spec = importlib.util.spec_from_file_location("trader_broker_phone", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PHONE.append(mod)
    return _PHONE[0]


def _phone_push(client, text, once_key=None):
    """Best effort, to your phone if one is paired (trader-broker/phone.py). Tests replace it."""
    try:
        return phone_module().push(client.state_dir, scrub(text, client._secrets),
                                   environ=getattr(client, "environ", None), once_key=once_key)
    except Exception:
        return False


def guard_alarm(client, text):
    """Say it in the log in capitals, as a notification, and on your phone."""
    text = scrub(text, client._secrets)
    _say("!!! GUARD ALERT (%s): %s !!!" % (client.env.upper(), text))
    try:                          # for the morning briefing: what went wrong since the last close
        path = os.path.join(client.state_dir, "logs", "guard-alerts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "env": client.env,
                                 "text": scrub(text, client._secrets)}) + "\n")
    except OSError:
        pass
    _notify("Trading 212 stop guard (%s)" % client.env, text)
    _phone_push(client, "GUARD ALERT (%s): %s" % (client.env.upper(), text))


GUARD_NEWS = ("stop placed", "SOLD AT STOP", "STOP SALE FAILED", "stop failed", "unclear", "below stop")


def guard_news(res):
    """What in one pass is worth your phone: orders the guard sent or could not send, and warnings."""
    out = []
    for r in res.get("positions") or []:
        st, tr = r.get("status") or "", r.get("trail") or ""
        if st in GUARD_NEWS or tr.startswith(("RAISED", "SOLD HALF")) or "SOLD HALF" in tr:
            line = "%s %s" % (r["ticker"], st)
            if st == "stop placed":
                line += ": SELL %s at %.2f, good till cancelled" % (_qty(r.get("placed_qty")), r["stop"])
            if tr and not tr.startswith(("not trailed", "stop ")):
                line += "; " + tr
            elif r.get("note"):
                line += ": " + r["note"]
            out.append(line)
    return out


def guard_phone(client, res):
    for line in guard_news(res):
        _phone_push(client, "GUARD (%s): %s" % (client.env, line))
    for w in res.get("warnings") or []:
        _phone_push(client, "GUARD WARNING (%s): %s" % (client.env, w), once_key=w)


def _auth_failed(client, e):
    msg = scrub(e, client._secrets)
    guard_alarm(client, "Trading 212 rejected the API key (%s). The guard has STOPPED and is not protecting any "
                        "position. Check the key in the Trading 212 app, update ~/.zshrc, then restart the guard. "
                        "Set any missing stop in the app now." % msg)
    return T212Error("guard stopped: %s" % msg, 4, e.status)


def guard_offline(client, state_dir, every, end_ts, eod_at, began_et):
    """No internet: check every 2 minutes. After 30 minutes say so loudly (the Mac's notification works
    offline; the phone gets it later) and keep trying: the guard never gives up while it should be
    watching. Once back, tell the phone what was missed."""
    net = net_module()
    say = lambda t: _say("%s  %s" % (ticket_module().to_eastern(_utc_now()).strftime("%H:%M"), t))
    w = net.wait_online(_sleep, _now, deadline=end_ts, log=say, label="the guard")
    start = w["start"]
    if not w["ok"] and w["gave_up"]:
        guard_alarm(client, "GUARD OFFLINE for %d minutes: positions are NOT being watched and software stops are "
                            "NOT enforced. It keeps trying every %d minutes. Check the Mac's internet."
                    % (net.minutes(start, _now()), net.RETRY_EVERY // 60))
        w = net.wait_online(_sleep, _now, limit=max(0.0, end_ts - _now()), deadline=end_ts, log=say,
                            label="the guard")
    end = _now()
    checks = int((end - start) // every)
    missed = ["%d guard check%s" % (checks, "" if checks == 1 else "s")] if checks else []
    if eod_at is not None:
        eod_ts = start + (eod_at - began_et).total_seconds()
        if start <= eod_ts <= end:
            missed.append("the %s end-of-day close, which runs now that it is back" % eod_at.strftime("%H:%M"))
    net.record(state_dir, "guard", start, end, missed, gave_up=not w["ok"])
    if w["ok"]:
        _phone_push(client, net.back_online("The guard (%s)" % client.env, start, end, missed,
                                            ["It is checking your positions again now."]))
    else:
        say("still offline at the end of the guard's day; giving up for today")
    return w


def cmd_guard(client, args):
    """Stop guardian. Never buys: its one write is a SELL stop for shares you hold."""
    if client.env != "demo" and not (client.live_ok or LIVE_PLACEMENT_ENABLED):
        raise T212Error("live trading is switched off, so the guard does not run on live; nothing was sent", 2)
    if not args.watch:
        try:
            res = guard_pass(client, args.state_dir)
        except T212Error as e:
            if e.status in AUTH_STATUSES:
                raise _auth_failed(client, e)
            raise
        return res if args.json else render_guard(res)
    tk = ticket_module()
    if args.every is None:      # a stop the guard enforces itself wants a closer look
        cfg = _read_json(os.path.join(args.state_dir, "config.json"), {}) or {}
        try:
            args.every = 1 if stop_mode(cfg) == "market" else 5
        except T212Error as e:
            guard_alarm(client, "the guard did not start: %s" % e)
            raise
    every = max(1.0, float(args.every)) * 60
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(args.until).strip())
    if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
        raise T212Error("--until wants a New York time like 16:00", 2)
    uh, um = int(m.group(1)), int(m.group(2))
    passes, fails = 0, 0
    while True:
        now_et = tk.to_eastern(_utc_now())
        if now_et.weekday() >= 5:
            _say("%s  weekend in New York: the guard has nothing to watch" % now_et.strftime("%a %H:%M"))
            return None
        open_et = now_et.replace(hour=GUARD_OPEN[0], minute=GUARD_OPEN[1], second=0)
        end_et = now_et.replace(hour=uh, minute=um, second=0)
        if now_et >= end_et:
            _say("%s  past %s New York: the guard stops (%d pass%s)" % (now_et.strftime("%H:%M"), args.until,
                                                                        passes, "" if passes == 1 else "es"))
            return None
        if now_et < open_et:
            wait = (open_et - now_et).total_seconds() + 30
            _say("%s  waiting for the open (9:30 New York)" % now_et.strftime("%H:%M"))
            _sleep(wait)
            continue
        eod, res = None, None
        try:
            eod = eod_settings(_read_json(os.path.join(args.state_dir, "config.json"), {}) or {})
        except T212Error as e:
            guard_alarm(client, "the guard has STOPPED: %s" % e)
            raise
        eod_at = now_et.replace(hour=eod["at"][0], minute=eod["at"][1], second=0) if eod else None
        if eod_at is not None and client.env == "demo" and now_et >= eod_at and not eod_done_today(args.state_dir):
            try:
                run_eod(client, args.state_dir)
            except T212Error as e:
                if e.status in AUTH_STATUSES:
                    raise _auth_failed(client, e)
                guard_alarm(client, "END-OF-DAY CLOSE FAILED, positions may still be open: %s. Sell them in the app."
                            % scrub(e, client._secrets))
                ticket_module()
                st_path = os.path.join(args.state_dir, "state", "t212-guard.json")
                st = _read_json(st_path, {}) or {}
                st["eod"] = {"date": _today_ny(tk), "at": tk.ny_iso(_utc_now()), "error": scrub(e, client._secrets)}
                _write_json(st_path, st)       # alarmed once; not retried every pass
            passes += 1
            _sleep(every)
            continue
        try:
            res = guard_pass(client, args.state_dir)
            _say(json.dumps(res, sort_keys=True) if args.json else render_guard(res))
            guard_phone(client, res)
            if fails >= GUARD_FAIL_ALERT:
                guard_alarm(client, "the guard is checking again after %d failed checks" % fails)
            fails = 0
        except T212Error as e:
            if e.status in AUTH_STATUSES:
                raise _auth_failed(client, e)
            if e.code == 2:
                guard_alarm(client, "the guard has STOPPED: %s" % scrub(e, client._secrets))
                raise
            if getattr(e, "network", False) and not net_module().online():
                end_ts = _now() + (end_et - now_et).total_seconds()
                guard_offline(client, args.state_dir, every, end_ts, eod_at, now_et)
                fails = 0
                continue                       # back online (or out of time): check again at once
            fails += 1
            _say("%s  guard pass failed (%d in a row), will try again: %s"
                 % (now_et.strftime("%H:%M"), fails, scrub(e, client._secrets)))
            if fails == GUARD_FAIL_ALERT:
                guard_alarm(client, "%d checks in a row have failed (%s). Positions are not being watched until "
                                    "it gets through." % (fails, scrub(e, client._secrets)))
        passes += 1
        wait = every
        if res is not None and (res.get("verify_soon") or res.get("software")):
            wait = min(wait, float(VERIFY_AFTER))   # a new stop is checked again in a minute; software stops every minute
        if eod_at is not None and client.env == "demo" and now_et < eod_at:
            wait = max(1.0, min(every, (eod_at - now_et).total_seconds() + 1))   # wake for the close on time
        _sleep(wait)


_UNIVERSE = []


def universe_module():
    """trader-scanner/universe.py: the filtered universe that replaces allowed_tickers on demo when it is on."""
    if not _UNIVERSE:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-scanner",
                            "universe.py")
        spec = importlib.util.spec_from_file_location("trader_scanner_universe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _UNIVERSE.append(mod)
    return _UNIVERSE[0]


def with_universe(cfg, state_dir):
    """cfg with allowed_tickers replaced by the universe when it is active (demo, enabled, fresh)."""
    return universe_module().apply(cfg or {}, state_dir)[0]


_BUDGET = []


def budget_module():
    if not _BUDGET:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_budget.py")
        spec = importlib.util.spec_from_file_location("trader_broker_account_budget", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BUDGET.append(mod)
    return _BUDGET[0]


def cmd_budget(client, args):
    """Budget and loss limits from the account value (account_budget in the config). Reads one account
    summary; with --apply writes the figures into trader/config.json (once a day unless --force)."""
    ab = budget_module()
    path = os.path.join(args.state_dir, "config.json")
    s = client.get(SUMMARY) or {}
    if str(s.get("currency", "")).upper() != "GBP":
        raise T212Error("this account is in %s; account_budget works with a GBP account" % s.get("currency"), 2)
    value = _num(s.get("totalValue"))
    today = _today_ny(ticket_module())
    try:
        if args.apply:
            r, changed = ab.apply(value, path, today, force=args.force)
            note = ("written to %s" % path) if changed else "already set today (use --force to read it again)"
        else:
            r = ab.compute(value, ab.settings(_read_json(path, {}) or {}) or
                           ab.settings({"account_budget": {"enabled": True}, "currency": "GBP"}))
            note = "not written (add --apply)"
    except ab.BudgetError as e:
        raise T212Error(str(e), 2)
    if args.json:
        return dict(r, env=client.env, today=today, note=note)
    return "%s\n%s\n%s" % (_env_line(client), ab.plain(r, today), note)


COMMANDS = {"status": cmd_status, "cash": cmd_cash, "info": cmd_info, "positions": cmd_positions,
            "orders": cmd_orders, "ticker": cmd_ticker, "instruments": cmd_instruments, "place": cmd_place,
            "guard": cmd_guard, "budget": cmd_budget}


def _add_common(parser, defaults):
    # SUPPRESS on the subcommand copy keeps it from overwriting a value given before the command
    d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=d(False), help="print JSON")
    parser.add_argument("--live", action="store_true", default=d(False),
                        help="use the LIVE account (also needs T212_ENV=live)")
    parser.add_argument("--state-dir", default=d("trader"), help="where state and cache go (default trader)")


def build_parser():
    """--json, --live and --state-dir work before or after the command name."""
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, defaults=False)
    p = argparse.ArgumentParser(description="Trading 212 adapter: reads, plus place (demo only, your typed yes).")
    _add_common(p, defaults=True)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("status", parents=[common], help="check the connection and which account")
    sub.add_parser("cash", parents=[common], help="cash available, reserved and in pies")
    sub.add_parser("info", parents=[common], help="account id, currency and totals")
    sub.add_parser("positions", parents=[common], help="open positions")
    sub.add_parser("orders", parents=[common], help="pending orders")
    sub.add_parser("fx", parents=[common], help="save the GBP/USD rate to trader/fx.json (Yahoo, no order)")
    t = sub.add_parser("ticker", parents=[common], help="map a plain ticker to a Trading 212 ticker")
    t.add_argument("symbol")
    i = sub.add_parser("instruments", parents=[common], help="the cached instrument list")
    i.add_argument("--refresh", action="store_true", help="download it again now")
    pl = sub.add_parser("place", parents=[common],
                        help="DEMO: place one ticket trader-risk passed, after your typed yes")
    pl.add_argument("ticket_id", help="the ticket number from ticket.py show, or its fingerprint")
    pl.add_argument("--resend", action="store_true",
                    help="send again after a failed or unclear send (checks for a duplicate first)")
    pl.add_argument("--fx-rate", type=float, help="USD per GBP to use instead of Yahoo's current rate")
    b = sub.add_parser("budget", parents=[common], help="budget and loss limits from the account value")
    b.add_argument("--apply", action="store_true", help="write them into trader/config.json")
    b.add_argument("--force", action="store_true", help="with --apply: read the account again even if done today")
    g = sub.add_parser("guard", parents=[common],
                       help="protect open positions with the ticket's stop (exits only, never buys)")
    g.add_argument("--watch", action="store_true", help="wait for the open, then check every --every minutes")
    g.add_argument("--every", type=float, default=None,
                   help="minutes between checks with --watch (default 5, or 1 with stop_mode market)")
    g.add_argument("--until", default="16:00", help="New York time --watch stops (default 16:00)")
    return p


def main(argv=None, environ=None):
    args = build_parser().parse_args(argv)
    secrets = []
    try:
        if args.cmd == "fx":                        # no Trading 212 request, no credentials needed
            out = cmd_fx(args)
            if args.json:
                out = json.dumps(out, indent=1, sort_keys=True)
            sys.stdout.write(out + "\n")
            return 0
        env = pick_env(args.live, environ)          # refuse a mismatch before anything else
        key, secret = credentials(environ)
        secrets = _secrets(key, secret)
        client = Client(env, key, secret, state_dir=args.state_dir)
        client.environ = environ
        if env == "live":
            client.live_ok = live_switch(_read_json(os.path.join(args.state_dir, "config.json"), {}) or {})
        out = COMMANDS[args.cmd](client, args)
        if args.cmd == "guard" and out is None:
            return 0                                # --watch has already printed every pass
        if args.cmd == "place":
            if out is None:
                return 1                            # you did not say yes; nothing was sent
            if not args.json:
                return 0                            # place has already printed what happened
    except T212Error as e:
        sys.stderr.write("t212: %s\n" % scrub(e, secrets))
        return e.code
    except Exception as e:  # never let a traceback carry anything sensitive
        sys.stderr.write("t212: unexpected %s: %s\n" % (type(e).__name__, scrub(e, secrets)))
        return 1
    if args.json:
        out = json.dumps(out, indent=1, sort_keys=True)
    sys.stdout.write(scrub(out, secrets) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
