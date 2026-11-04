#!/usr/bin/env python3
"""Read-only Trading 212 adapter for trader-broker. Standard library only.

This tool only ever sends GET requests. It cannot place, change or cancel an
order. Endpoints and rate limits are from the official reference at
docs.trading212.com (OpenAPI bundle checked 2026-11-04):

  GET /api/v0/equity/account/summary      1 req / 5s
  GET /api/v0/equity/positions            1 req / 1s
  GET /api/v0/equity/orders               1 req / 5s
  GET /api/v0/equity/metadata/instruments 1 req / 50s

Commands (each takes --json):

    python3 t212.py status                  # which account, is the key working
    python3 t212.py cash                    # free cash, reserved, in pies
    python3 t212.py info                    # account id, currency, totals
    python3 t212.py positions               # open positions
    python3 t212.py orders                  # pending orders
    python3 t212.py ticker TSLA             # TSLA -> TSLA_US_EQ
    python3 t212.py instruments --refresh   # re-download the instrument list

Demo (paper money) is the default: https://demo.trading212.com/api/v0. Live
is used only when T212_ENV=live AND --live is passed. Any mismatch is refused
before a request is sent.

Credentials come from T212_API_KEY and T212_API_SECRET (HTTP Basic: key as
user name, secret as password). They are never printed, logged or written to
disk, and they are scrubbed from every error message.

State lives under trader/ (override with --state-dir):
  trader/state/t212-ratelimit.json        when each endpoint was last called
  trader/cache/t212-instruments-ENV.json  the instrument list, kept 24 hours
"""

import argparse
import base64
import json
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
# seconds between calls, from each endpoint's documented "N req / Ts"
MIN_INTERVAL = {SUMMARY: 5.0, POSITIONS: 1.0, ORDERS: 5.0, INSTRUMENTS: 50.0}
# the scope each endpoint's 403 names in the reference
SCOPES = {SUMMARY: "account", POSITIONS: "portfolio", ORDERS: "orders:read", INSTRUMENTS: "metadata"}
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
    """GET-only client. There is deliberately no method that sends anything else."""

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
        if path not in MIN_INTERVAL:
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

    def _http_message(self, path, e):
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        if e.code == 401:
            msg = "401: Trading 212 rejected the API key or secret for the %s account" % self.env
        elif e.code == 403:
            msg = "403: the API key is missing the '%s' permission (scope) for GET %s" % (
                SCOPES.get(path, "?"), path)
        elif e.code == 408:
            msg = "408: Trading 212 timed out on GET %s" % path
        elif e.code == 429:
            msg = "429: still rate limited on GET %s after %d retries" % (path, MAX_RETRIES)
        elif 300 <= e.code < 400:
            msg = "%d: Trading 212 tried to redirect GET %s; not followed" % (e.code, path)
        else:
            msg = "%d: Trading 212 error on GET %s" % (e.code, path)
        if detail:
            msg += " (%s)" % detail[:200]
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
    lines.append("read only: this tool sends GET requests only and cannot place orders")
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


COMMANDS = {"status": cmd_status, "cash": cmd_cash, "info": cmd_info, "positions": cmd_positions,
            "orders": cmd_orders, "ticker": cmd_ticker, "instruments": cmd_instruments}


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
    p = argparse.ArgumentParser(description="Read-only Trading 212 adapter (GET requests only).")
    _add_common(p, defaults=True)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("status", parents=[common], help="check the connection and which account")
    sub.add_parser("cash", parents=[common], help="cash available, reserved and in pies")
    sub.add_parser("info", parents=[common], help="account id, currency and totals")
    sub.add_parser("positions", parents=[common], help="open positions")
    sub.add_parser("orders", parents=[common], help="pending orders")
    t = sub.add_parser("ticker", parents=[common], help="map a plain ticker to a Trading 212 ticker")
    t.add_argument("symbol")
    i = sub.add_parser("instruments", parents=[common], help="the cached instrument list")
    i.add_argument("--refresh", action="store_true", help="download it again now")
    return p


def main(argv=None, environ=None):
    args = build_parser().parse_args(argv)
    secrets = []
    try:
        env = pick_env(args.live, environ)          # refuse a mismatch before anything else
        key, secret = credentials(environ)
        secrets = _secrets(key, secret)
        client = Client(env, key, secret, state_dir=args.state_dir)
        out = COMMANDS[args.cmd](client, args)
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
