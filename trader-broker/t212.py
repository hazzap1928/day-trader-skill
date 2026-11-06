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
  - demo: a market entry, then a separate DAY stop order once the entry fills.
    Live placement is switched off (LIVE_PLACEMENT_ENABLED); when switched on,
    it places the entry only and you set the stop in the app yourself
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
READ_PATHS = (SUMMARY, POSITIONS, ORDERS, INSTRUMENTS)
PLACE_PATHS = (MARKET, STOP)
# seconds between calls, from each endpoint's documented "N req / Ts"
MIN_INTERVAL = {SUMMARY: 5.0, POSITIONS: 1.0, ORDERS: 5.0, INSTRUMENTS: 50.0, MARKET: 1.2, STOP: 2.0}
# the scope each endpoint's 403 names in the reference
SCOPES = {SUMMARY: "account", POSITIONS: "portfolio", ORDERS: "orders:read", INSTRUMENTS: "metadata",
          MARKET: "orders:execute", STOP: "orders:execute"}
# Live placement is off. Switching it on is a deliberate one-line change; even
# then a live run places the market entry only and never the stop.
LIVE_PLACEMENT_ENABLED = False
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

    def __init__(self, msg, code=1):
        Exception.__init__(self, msg)
        self.code = code


class PlaceError(T212Error):
    """An order POST that did not come back 200. outcome is "rejected" (Trading 212
    said no, so nothing was placed) or "unknown" (it may have been placed)."""

    def __init__(self, msg, outcome, status=None):
        T212Error.__init__(self, msg, 1)
        self.outcome = outcome
        self.status = status


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
        self.base = BASE_URLS[env]
        self._secrets = _secrets(key, secret)
        self._auth = "Basic " + self._secrets[1]
        self.rate_path = os.path.join(state_dir, "state", "t212-ratelimit.json")
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
                raise T212Error(self._http_message(path, e))
            except urllib.error.URLError as e:
                raise T212Error(self._clean("could not reach %s: %s" % (self.base, e.reason)))
            except OSError as e:
                raise T212Error(self._clean("could not reach %s: %s" % (self.base, e)))
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
        if self.env != "demo" and not LIVE_PLACEMENT_ENABLED:
            raise T212Error("placing orders on the live account is switched off; nothing was sent", 2)
        if self.env != "demo" and path == STOP:
            raise T212Error("the stop is never placed on live; set it in the app. Nothing was sent", 2)
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
    lines.append("read only: only the place command sends orders, after your typed yes (demo only)")
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
            "timeValidity": "DAY"}


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


def render_place(index, total, client, order, inst, sz, side, price_src, fx, bodies, rv_fp):
    ccy = "USD"
    lines = ["PLACE TICKET %d of %d on %s" % (index, total, _env_line(client)),
             "  %s %s %s (%s, %s), market order" % (side.upper(), _qty(sz["qty"]), inst.get("ticker"),
                                                   str(order.get("symbol", "")).upper(), inst.get("name", "")),
             "  price used:  %.2f %s (%s)" % (sz["entry_usd"], ccy, price_src),
             "  GBP/USD:     %.4f (%s, %s)" % (fx[0], fx[2], fx[1])]
    if side == "buy":
        lines.append("  est. cost:   %s + 0.15%% FX fee %s = %s (cash available %s)"
                     % (_gbp(sz["value_gbp"]), _gbp(sz["fee_gbp"]), _gbp(sz["cost_gbp"]), _gbp(sz["cash_gbp"])))
        if client.env == "demo":
            lines.append("  stop:        %.2f %s, sent as a separate STOP order once the entry fills: SELL %s, "
                         "good for the day" % (sz["stop_usd"], ccy, _qty(sz["qty"])))
        else:
            lines.append("  stop:        %.2f %s  NOT PLACED ON LIVE: set this stop yourself in the Trading 212 "
                         "app straight after the entry fills" % (sz["stop_usd"], ccy))
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
        lines.append("  !!! LIVE ACCOUNT, REAL MONEY. NO STOP WILL BE PLACED. SET IT IN THE APP. !!!")
    return "\n".join(lines)


def wait_for_fill(client, ticker, want, before):
    for _ in range(FILL_POLLS):
        pos = client.get(POSITIONS, {"ticker": ticker})
        held = sum(_num(p.get("quantity")) or 0 for p in pos or [] if _o_ticker(p) == ticker)
        if held - before + 1e-9 >= want:
            return True
    return False


def cmd_place(client, args):
    secrets = client._secrets
    sd = args.state_dir
    if client.env != "demo" and not LIVE_PLACEMENT_ENABLED:
        raise T212Error("placing orders is demo only for now; refusing on the LIVE account. Nothing was sent. "
                        "(When live placement is switched on it will place the market entry only and you "
                        "must set the stop yourself in the Trading 212 app.)", 2)
    tk = ticket_module()
    paths = {"orders": os.path.join(sd, "orders.json"), "config": os.path.join(sd, "config.json"),
             "risk": os.path.join(sd, "state", "risk.json"), "state": os.path.join(sd, "state", "broker.json"),
             "prices": os.path.join(sd, "prices.json")}
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

    # 5. size and build the exact bodies
    if side == "buy":
        stop = _num(order.get("stop_loss_stop_price")) or _num((order.get("plan") or {}).get("stop"))
        sz = size_entry(price, stop, ticket_qty, fx[0], cash, cfg, fractional_for(cfg, inst))
        bodies = [(MARKET, market_body(ticker, "buy", sz["qty"]))]
        if client.env == "demo":
            bodies.append((STOP, stop_body(ticker, sz["qty"], stop)))
    else:
        sz = size_exit(price, ticket_qty, fx[0])
        bodies = [(MARKET, market_body(ticker, "sell", sz["qty"]))]
    for path, body in bodies:
        check_sign(body, "sell" if path == STOP else side)

    _say(render_place(index, len(orders), client, order, inst, sz, side, price_src, fx, bodies, fp))

    # 6. your typed yes, for this one order
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
        base.update(max_loss=sz["max_loss_usd"], max_loss_gbp=sz["max_loss_gbp"], stop=sz["stop_usd"],
                    cost_gbp=sz["cost_gbp"], fx_rate=fx[0])
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

    # 8. the stop (demo only), once the entry has filled
    if side != "buy":
        return result
    if client.env != "demo":
        _say("!!! LIVE: NO STOP WAS PLACED. Open the Trading 212 app now and set a stop: SELL %s %s at %.2f. !!!"
             % (_qty(sz["qty"]), ticker, sz["stop_usd"]))
        result["stop_warning"] = "set the stop in the app"
        return result
    if not wait_for_fill(client, ticker, sz["qty"], held):
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
    _record(paths["state"], state, dict(base, at=tk.ny_iso(_utc_now()), status="stop_placed",
                                        broker_order_id=sresp.get("id")))
    _say("STOP PLACED: SELL %s %s at %.2f, good for the day, order id %s"
         % (_qty(sz["qty"]), ticker, sz["stop_usd"], _ident(sresp.get("id"))))
    result["stop"] = sresp
    return result


COMMANDS = {"status": cmd_status, "cash": cmd_cash, "info": cmd_info, "positions": cmd_positions,
            "orders": cmd_orders, "ticker": cmd_ticker, "instruments": cmd_instruments, "place": cmd_place}


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
        out = COMMANDS[args.cmd](client, args)
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
