#!/usr/bin/env python3
"""Your phone, through a Telegram bot you own: messages out, Approve / Reject taps back.

Why Telegram: the Mac only ever calls out to api.telegram.org (no open port, no web
server), the bot token is a secret only you hold, and every tap carries the Telegram
user id of whoever tapped, so a tap from anyone but you is ignored.

    python3 trader-broker/phone.py setup     # after you send /start to your bot: pairs this chat
    python3 trader-broker/phone.py test      # a test message with buttons; tap one (no order)
    python3 trader-broker/phone.py send "hello"

The token comes from TELEGRAM_BOT_TOKEN in the environment (an export line in
~/.zshrc). It is never written to a file or a log. The paired chat lives in
trader/state/phone.json. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot%s/%s"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TIMEOUT = 40
MAX_TEXT = 3900               # Telegram's limit is 4096 characters a message
_MSG = []


def messages():
    """trader-broker/messages.py: every word sent to the phone."""
    if not _MSG:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.py")
        spec = importlib.util.spec_from_file_location("trader_broker_messages", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MSG.append(mod)
    return _MSG[0]


def split(text, limit=MAX_TEXT):
    """Long messages in pieces under Telegram's limit, cut between paragraphs (else lines), never inside one."""
    text = str(text)
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        for piece in ([para] if len(para) <= limit else _lines(para, limit)):
            add = piece if not cur else cur + "\n\n" + piece
            if len(add) <= limit:
                cur = add
            else:
                if cur:
                    parts.append(cur)
                cur = piece
    if cur:
        parts.append(cur)
    return parts


def _lines(para, limit):
    out, cur = [], ""
    for line in para.split("\n"):
        while len(line) > limit:                  # one huge line: cut it (it never holds a tag across)
            out.append(line[:limit])
            line = line[limit:]
        add = line if not cur else cur + "\n" + line
        if len(add) <= limit:
            cur = add
        else:
            out.append(cur)
            cur = line
    if cur:
        out.append(cur)
    return out


def _bad_format(e):
    return "can't parse entities" in str(e).lower() or "can't find end" in str(e).lower() \
        or "unsupported start tag" in str(e).lower()


class PhoneError(Exception):
    pass


def _open(req, timeout):          # tests replace this
    return urllib.request.urlopen(req, timeout=timeout)


def _now():
    return time.time()


def state_path(state_dir):
    return os.path.join(state_dir, "state", "phone.json")


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def token_from(environ=None):
    environ = os.environ if environ is None else environ
    return (environ.get(TOKEN_ENV) or "").strip()


class Phone(object):
    def __init__(self, token, state_dir):
        if not token:
            raise PhoneError("%s is not set. Add an export line for it to ~/.zshrc (see trader-broker/PHONE.md); "
                             "never paste it into the chat" % TOKEN_ENV)
        self._token = token
        self.sd = state_dir
        self.path = state_path(state_dir)
        self.state = _read(self.path, {}) or {}

    # ------------------------------------------------------------ plumbing

    def scrub(self, text):
        return str(text).replace(self._token, "<token>")

    def save(self):
        _write(self.path, self.state)

    def call(self, method, http_timeout=TIMEOUT, **params):
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(API % (self._token, method), data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            resp = _open(req, http_timeout)
            try:
                doc = json.loads(resp.read().decode("utf-8"))
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            raise PhoneError(self.scrub("Telegram %s: HTTP %s %s" % (method, e.code, body)))
        except Exception as e:      # the URL holds the token: never let it out in an error
            raise PhoneError(self.scrub("Telegram %s failed: %s" % (method, e)))
        if not isinstance(doc, dict) or not doc.get("ok"):
            raise PhoneError(self.scrub("Telegram %s: %s" % (method, (doc or {}).get("description", doc))))
        return doc.get("result")

    @property
    def paired(self):
        return bool(self.state.get("chat_id") and self.state.get("user_id"))

    # ------------------------------------------------------------ out

    def _html(self, method, text, **params):
        """Telegram HTML (bold and so on). If Telegram rejects the formatting, the same words go as plain text."""
        try:
            return self.call(method, text=text, parse_mode="HTML", **params)
        except PhoneError as e:
            if not _bad_format(e):
                raise
            return self.call(method, text=messages().plain(text)[:MAX_TEXT], **params)

    def send(self, text, buttons=None):
        """buttons: [(label, callback_data), ...] on one row, on the last piece. Returns the message id
        (of the last piece: a long message goes in pieces under Telegram's 4,096-character limit)."""
        if not self.paired:
            raise PhoneError("no phone paired yet: run python3 trader-broker/phone.py setup")
        res = None
        pieces = split(text)
        for i, piece in enumerate(pieces):
            params = {"chat_id": self.state["chat_id"], "disable_web_page_preview": True}
            if buttons and i == len(pieces) - 1:
                params["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
            res = self._html("sendMessage", piece, **params)
        return (res or {}).get("message_id")

    def edit(self, message_id, text):
        """Replace a message's text and take its buttons away."""
        if message_id is None:
            return
        self._html("editMessageText", split(text)[0], chat_id=self.state["chat_id"], message_id=message_id,
                   reply_markup={"inline_keyboard": []})

    def answer(self, callback_id, text=""):
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    # ------------------------------------------------------------ in

    def updates(self, wait=0):
        """New taps and messages from YOUR chat only, oldest first; each is seen once.
        Returns [{"kind": "tap", "id", "data", "message_id"} | {"kind": "text", "text"}]."""
        params = {"timeout": int(wait), "allowed_updates": ["message", "callback_query"]}
        if self.state.get("offset"):
            params["offset"] = self.state["offset"]
        res = self.call("getUpdates", http_timeout=TIMEOUT + int(wait), **params) or []
        out = []
        for u in res:
            self.state["offset"] = max(int(self.state.get("offset") or 0), int(u.get("update_id", 0)) + 1)
            cq = u.get("callback_query")
            if cq:
                msg = cq.get("message") or {}
                if self._mine((cq.get("from") or {}).get("id"), (msg.get("chat") or {}).get("id")):
                    out.append({"kind": "tap", "id": cq.get("id"), "data": str(cq.get("data") or ""),
                                "message_id": msg.get("message_id")})
                else:
                    self.state["ignored"] = int(self.state.get("ignored") or 0) + 1
                continue
            m = u.get("message")
            if m:
                if self._mine((m.get("from") or {}).get("id"), (m.get("chat") or {}).get("id")):
                    out.append({"kind": "text", "text": str(m.get("text") or "")})
                else:
                    self.state["ignored"] = int(self.state.get("ignored") or 0) + 1
        if res:
            self.save()
        return out

    def _mine(self, user_id, chat_id):
        return (self.paired and user_id is not None and chat_id is not None
                and str(user_id) == str(self.state["user_id"]) and str(chat_id) == str(self.state["chat_id"]))

    def pair(self):
        """Find the /start you sent the bot in a private chat, and remember that chat and you."""
        res = self.call("getUpdates", allowed_updates=["message"]) or []
        found = None
        for u in res:
            m = u.get("message") or {}
            chat = m.get("chat") or {}
            if chat.get("type") == "private" and str(m.get("text") or "").strip().startswith("/start"):
                found = (chat.get("id"), (m.get("from") or {}).get("id"), (m.get("from") or {}).get("first_name"))
            self.state["offset"] = max(int(self.state.get("offset") or 0), int(u.get("update_id", 0)) + 1)
        if not found:
            raise PhoneError("no /start found: open your bot in Telegram, send it /start, then run setup again")
        self.state.update(chat_id=found[0], user_id=found[1], paired_at=_iso(_now()))
        self.save()
        return found


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_phone(state_dir, environ=None):
    """The paired phone, or None when there is no token or no pairing yet."""
    tok = token_from(environ)
    if not tok:
        return None
    p = Phone(tok, state_dir)
    return p if p.paired else None


def push(state_dir, text, environ=None, once_key=None, phone=None):
    """Best effort: a message to your phone. Never raises (the log has the text anyway).
    once_key: send this at most once per New York day (for warnings a check repeats).
    A message that fails is queued and goes out with the next one that gets through."""
    p = None
    try:
        p = phone or open_phone(state_dir, environ)
        if p is None:
            return False
        if once_key is not None:
            day = datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).strftime("%Y-%m-%d")
            sent = p.state.setdefault("sent_once", {})
            if sent.get("day") != day:
                sent.clear()
                sent["day"] = day
            key = str(once_key)[:200]
            if key in sent:
                return False
            sent[key] = 1
            p.save()
        flush_outbox(p, state_dir)
        p.send(text)
        return True
    except Exception as e:
        sys.stderr.write("phone: could not send: %s\n" % (phone.scrub(e) if phone else _scrub_env(e, environ)))
        if p is not None:
            queue(state_dir, text)
        return False


OUTBOX_MAX = 50


def outbox_path(state_dir):
    return os.path.join(state_dir, "state", "phone-outbox.json")


def queue(state_dir, text):
    """Keep a message that could not be sent; the next one that gets through takes it along."""
    doc = _read(outbox_path(state_dir), {}) or {}
    items = doc.get("items") or []
    items.append({"at": _now(), "text": str(text)[:3000]})
    doc["items"] = items[-OUTBOX_MAX:]
    doc["dropped"] = int(doc.get("dropped") or 0) + max(0, len(items) - OUTBOX_MAX)
    _write(outbox_path(state_dir), doc)


def flush_outbox(p, state_dir):
    """Send what waited while the phone could not be reached, oldest first, in one message."""
    doc = _read(outbox_path(state_dir), {}) or {}
    items = doc.get("items") or []
    if not items:
        return 0
    p.send(messages().delivered_late([(datetime.datetime.fromtimestamp(i["at"]).strftime("%a %H:%M"), i["text"])
                                      for i in items], doc.get("dropped") or 0))
    _write(outbox_path(state_dir), {"items": []})
    return len(items)


def _scrub_env(e, environ):
    tok = token_from(environ)
    return str(e).replace(tok, "<token>") if tok else str(e)


# ---------------------------------------------------------------- command line

def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Your phone through a Telegram bot: messages and Approve / Reject.")
    ap.add_argument("--state-dir", default="trader")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("setup", help="pair the chat where you sent /start to your bot")
    t = sub.add_parser("test", help="send a test message with buttons and wait for your tap (no order)")
    t.add_argument("--wait", type=int, default=120, help="seconds to wait for the tap")
    s = sub.add_parser("send", help="send one message")
    s.add_argument("text")
    a = ap.parse_args(argv)
    try:
        p = Phone(token_from(environ), a.state_dir)
        if a.cmd == "setup":
            chat, user, name = p.pair()
            p.send(messages().paired(name or "you"))
            print("Paired: chat %s, user %s. A confirmation is on your phone." % (chat, user))
            return 0
        if a.cmd == "send":
            p.send(a.text)
            print("Sent.")
            return 0
        p.updates()                                   # skip anything older than this test
        mid = p.send(messages().test_tap(),
                     [("Approve", "test:approve"), ("Reject", "test:reject")])
        end = _now() + a.wait
        print("Sent. Tap Approve or Reject on your phone (waiting %ds)." % a.wait)
        while _now() < end:
            for u in p.updates(wait=min(20, max(1, int(end - _now())))):
                if u["kind"] == "tap" and u["data"].startswith("test:"):
                    which = u["data"].split(":", 1)[1]
                    p.answer(u["id"], "Got it: %s" % which)
                    p.edit(mid, "\U0001f9ea Test: you tapped %s. The round trip works. Nothing was bought." % which.upper())
                    print("You tapped %s: the round trip works." % which.upper())
                    return 0
        p.edit(mid, "\U0001f9ea Test: no tap within %ds." % a.wait)
        print("No tap within %ds." % a.wait)
        return 1
    except PhoneError as e:
        sys.stderr.write("phone: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
