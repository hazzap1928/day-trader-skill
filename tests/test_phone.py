"""phone.py: the Telegram channel. A fake Telegram, never the network, never a real token."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import email.message
from contextlib import redirect_stderr

from _load import load

phone = load("trader-broker", "phone")

TOKEN = "123456:TEST-token-not-real"
ME, CHAT, STRANGER = 111, 111, 999


class FakeTelegram(object):
    def __init__(self):
        self.calls = []
        self.replies = {}

    def add(self, method, result=None, error=None):
        self.replies.setdefault(method, []).append((result, error))

    def __call__(self, req, timeout):
        method = req.full_url.rsplit("/", 1)[1]
        self.calls.append((method, json.loads(req.data.decode()), req.full_url))
        q = self.replies.get(method) or [({"message_id": 1} if method == "sendMessage" else True, None)]
        result, error = q.pop(0) if len(q) > 1 else q[0]
        if error is not None:
            raise error
        body = json.dumps({"ok": True, "result": result}).encode()

        class R(object):
            def read(self):
                return body

            def close(self):
                pass
        return R()


def tap(uid, data, user=ME, chat=CHAT):
    return {"update_id": uid, "callback_query": {"id": "cb%d" % uid, "data": data, "from": {"id": user},
                                                 "message": {"message_id": 7, "chat": {"id": chat}}}}


def text(uid, t, user=ME, chat=CHAT, kind="private"):
    return {"update_id": uid, "message": {"text": t, "from": {"id": user, "first_name": "H"},
                                          "chat": {"id": chat, "type": kind}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tg = FakeTelegram()
        self._open = phone._open
        phone._open = self.tg

    def tearDown(self):
        phone._open = self._open
        shutil.rmtree(self.dir)

    def paired(self):
        p = phone.Phone(TOKEN, self.dir)
        p.state.update(chat_id=CHAT, user_id=ME)
        p.save()
        return phone.Phone(TOKEN, self.dir)


class Pairing(Base):
    def test_pair_takes_the_private_start_and_saves_no_token(self):
        self.tg.add("getUpdates", [text(5, "hello", user=STRANGER, chat=STRANGER, kind="group"),
                                   text(6, "/start")])
        p = phone.Phone(TOKEN, self.dir)
        self.assertEqual(p.pair()[:2], (CHAT, ME))
        with open(phone.state_path(self.dir)) as fh:
            saved = fh.read()
        self.assertNotIn(TOKEN, saved)
        self.assertEqual(json.loads(saved)["offset"], 7)

    def test_no_start_is_an_error(self):
        self.tg.add("getUpdates", [])
        with self.assertRaises(phone.PhoneError):
            phone.Phone(TOKEN, self.dir).pair()

    def test_no_token_is_refused(self):
        with self.assertRaises(phone.PhoneError):
            phone.Phone("", self.dir)


class Messages(Base):
    def test_send_puts_the_buttons_on_one_row(self):
        p = self.paired()
        p.send("ticket", [("Approve", "ap:x"), ("Reject", "rj:x")])
        m, body, _ = self.tg.calls[-1]
        self.assertEqual(m, "sendMessage")
        self.assertEqual(body["chat_id"], CHAT)
        self.assertEqual([b["text"] for b in body["reply_markup"]["inline_keyboard"][0]], ["Approve", "Reject"])

    def test_edit_takes_the_buttons_away(self):
        self.paired().edit(7, "done")
        self.assertEqual(self.tg.calls[-1][1]["reply_markup"], {"inline_keyboard": []})

    def test_only_your_taps_count_and_each_is_seen_once(self):
        self.tg.add("getUpdates", [tap(10, "ap:1", user=STRANGER), tap(11, "ap:2", chat=STRANGER),
                                   tap(12, "rj:3"), text(13, "/pause", user=STRANGER), text(14, "/status")])
        p = self.paired()
        got = p.updates()
        self.assertEqual([(u["kind"], u.get("data") or u.get("text")) for u in got],
                         [("tap", "rj:3"), ("text", "/status")])
        self.assertEqual(p.state["ignored"], 3)
        self.tg.add("getUpdates", [])
        p.updates()
        self.assertEqual(self.tg.calls[-1][1]["offset"], 15)

    def test_the_token_never_appears_in_an_error(self):
        err = urllib.error.HTTPError("https://api.telegram.org/bot%s/sendMessage" % TOKEN, 401, "no",
                                     email.message.Message(), io.BytesIO(TOKEN.encode()))
        self.tg.add("sendMessage", error=err)
        with self.assertRaises(phone.PhoneError) as cm:
            self.paired().send("x")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.tg.add("getUpdates", error=OSError("cannot reach https://api.telegram.org/bot%s/x" % TOKEN))
        with self.assertRaises(phone.PhoneError) as cm:
            self.paired().updates()
        self.assertNotIn(TOKEN, str(cm.exception))


class Push(Base):
    def test_push_without_a_token_or_pairing_is_a_quiet_no(self):
        self.assertFalse(phone.push(self.dir, "x", environ={}))
        self.assertFalse(phone.push(self.dir, "x", environ={phone.TOKEN_ENV: TOKEN}))
        self.assertEqual(self.tg.calls, [])

    def test_push_never_raises(self):
        self.paired()
        self.tg.add("sendMessage", error=OSError("down"))
        with redirect_stderr(io.StringIO()):
            self.assertFalse(phone.push(self.dir, "x", environ={phone.TOKEN_ENV: TOKEN}))

    def test_once_key_sends_a_repeated_warning_once_a_day(self):
        self.paired()
        env = {phone.TOKEN_ENV: TOKEN}
        self.assertTrue(phone.push(self.dir, "w", environ=env, once_key="w"))
        self.assertFalse(phone.push(self.dir, "w", environ=env, once_key="w"))
        self.assertTrue(phone.push(self.dir, "w2", environ=env, once_key="w2"))
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "sendMessage"]), 2)


if __name__ == "__main__":
    unittest.main()
