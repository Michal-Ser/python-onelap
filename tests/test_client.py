import contextlib
import io
import logging
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from onelap import (ApiError, AuthError, NotFitError, OneLap, RateLimited,
                    SessionExpired, validate_fit)
from onelap import __main__ as probe_main

from .fake_server import (ACCOUNT, BASE_TS, PASSWORD, SECRET_URL_PARAM,
                          SIGN_KEY, FakeOneLap, make_fit)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOneLap().start()
        self.addCleanup(self.fake.stop)
        self.changes = []

    def client(self, **kw):
        kw.setdefault("on_session_change", self.changes.append)
        c = OneLap(SIGN_KEY, base_url=self.fake.url, backoff=0, timeout=5,
                   **kw)
        return c

    def logged_in_session(self):
        c = OneLap(SIGN_KEY, base_url=self.fake.url, backoff=0)
        c.login(ACCOUNT, PASSWORD)
        return c.session


class TestAuth(Base):
    def test_login_session_has_no_password_and_callback_once(self):
        c = self.client()
        c.login(ACCOUNT, PASSWORD)
        s = c.session
        self.assertEqual(set(s), {"token", "uid", "region"})
        self.assertNotIn(PASSWORD, repr(s))
        self.assertEqual(len(self.changes), 1)

    def test_wrong_password(self):
        with self.assertRaises(AuthError):
            self.client().login(ACCOUNT, "nope")
        self.assertEqual(self.changes, [])

    def test_restore_session_without_login(self):
        session = self.logged_in_session()
        before = self.fake.logins
        c = self.client(session=session)
        self.assertEqual(len(list(c.iter_activities())), 5)
        self.assertEqual(self.fake.logins, before)
        self.assertEqual(self.changes, [])

    def test_expired_with_credentials_logs_in_once(self):
        c = self.client(session=self.logged_in_session(), username=ACCOUNT,
                        password=PASSWORD)
        self.fake.expire_all()
        before = self.fake.logins
        self.assertEqual(len(list(c.iter_activities())), 5)
        self.assertEqual(self.fake.logins, before + 1)
        self.assertEqual(len(self.changes), 1)

    def test_expired_without_credentials(self):
        c = self.client(session=self.logged_in_session())
        self.fake.expire_all()
        before = self.fake.logins
        with self.assertRaises(SessionExpired):
            list(c.iter_activities())
        self.assertEqual(self.fake.logins, before)

    def test_second_expiry_does_not_log_in_again(self):
        c = self.client(session=self.logged_in_session(), username=ACCOUNT,
                        password=PASSWORD)
        self.fake.expire_all()
        list(c.iter_activities())
        logins = self.fake.logins
        self.fake.expire_all()
        with self.assertRaises(SessionExpired):
            list(c.iter_activities())
        self.assertEqual(self.fake.logins, logins)

    def test_expiry_shapes(self):
        for mode in ("json", "html", "status"):
            with self.subTest(mode=mode):
                self.fake.expire_mode = mode
                c = self.client(session=self.logged_in_session(),
                                username=ACCOUNT, password=PASSWORD)
                self.fake.expire_all()
                before = self.fake.logins
                self.assertEqual(len(list(c.iter_activities())), 5)
                self.assertEqual(self.fake.logins, before + 1)
                c2 = self.client(session=self.logged_in_session())
                self.fake.expire_all()
                with self.assertRaises(SessionExpired):
                    list(c2.iter_activities())


class TestActivities(Base):
    def test_pagination_order_and_fields(self):
        c = self.client(session=self.logged_in_session())
        acts = list(c.iter_activities())
        self.assertEqual([a.id for a in acts],
                         ["ride-%d" % i for i in range(5)])
        self.assertEqual(self.fake.list_pages, [1, 2, 3])
        a = acts[0]
        self.assertEqual(a.start, datetime.fromtimestamp(
            BASE_TS, tz=timezone.utc))
        self.assertEqual(a.distance_m, 20000.0)
        self.assertEqual(a.duration_s, 3600.0)
        self.assertEqual(a.name, "Ride ride-0")

    def test_since_stops_paging(self):
        c = self.client(session=self.logged_in_session())
        since = datetime.fromtimestamp(BASE_TS - 2 * 86400 - 10,
                                       tz=timezone.utc)
        acts = list(c.iter_activities(since=since))
        self.assertEqual([a.id for a in acts], ["ride-0", "ride-1", "ride-2"])
        self.assertEqual(self.fake.list_pages, [1, 2])  # page 3 not fetched

    def test_until(self):
        c = self.client(session=self.logged_in_session())
        until = datetime.fromtimestamp(BASE_TS - 86400, tz=timezone.utc)
        ids = [a.id for a in c.iter_activities(until=until)]
        self.assertEqual(ids[0], "ride-1")

    def test_lazy(self):
        c = self.client(session=self.logged_in_session())
        it = c.iter_activities()
        self.assertEqual(self.fake.list_pages, [])
        next(it)
        self.assertEqual(self.fake.list_pages, [1])
        next(it)
        self.assertEqual(self.fake.list_pages, [1])
        next(it)
        self.assertEqual(self.fake.list_pages, [1, 2])

    def test_empty_account(self):
        self.fake.rides = []
        c = self.client(session=self.logged_in_session())
        self.assertEqual(list(c.iter_activities()), [])

    def test_raw_has_no_signed_url(self):
        c = self.client(session=self.logged_in_session())
        a = next(c.iter_activities())
        self.assertNotIn(SECRET_URL_PARAM, repr(a))
        self.assertNotIn(SECRET_URL_PARAM, repr(a.raw))
        self.assertIn("fileAddr", a.raw)


class TestDownload(Base):
    def test_ok_bytes_unchanged(self):
        c = self.client(session=self.logged_in_session())
        a = next(c.iter_activities())
        self.assertEqual(c.download_fit(a), make_fit())

    def test_html_is_not_fit(self):
        c = self.client(session=self.logged_in_session())
        a = next(c.iter_activities())
        self.fake.download_html = True
        with self.assertRaises(NotFitError):
            c.download_fit(a)

    def test_validate_fit(self):
        good = make_fit()
        self.assertEqual(validate_fit(good), good)
        for bad in (b"", b"x" * 14, b"<html>" + b"x" * 40,
                    bytes([13]) + good[1:], good[:8] + b".FIX" + good[12:],
                    good[:-10]):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(NotFitError):
                    validate_fit(bad)


class TestRetries(Base):
    def test_429_twice_then_ok(self):
        c = self.client(session=self.logged_in_session())
        self.fake.rate_limit_list = 2
        self.assertEqual(len(list(c.iter_activities())), 5)
        self.assertEqual(self.fake.list_hits, 2 + 3)

    def test_429_always(self):
        c = self.client(session=self.logged_in_session())
        self.fake.always_429 = True
        with self.assertRaises(RateLimited):
            list(c.iter_activities())
        self.assertEqual(self.fake.list_hits, 3)

    def test_5xx_error_is_sanitized(self):
        session = self.logged_in_session()
        c = self.client(session=session)
        self.fake.server_error_body = "boom " + session["token"] + " " + \
            "x" * 500
        with self.assertRaises(ApiError) as cm:
            list(c.iter_activities())
        e = cm.exception
        self.assertEqual(e.status, 500)
        self.assertLessEqual(len(e.snippet), 200)
        self.assertNotIn(session["token"], str(e))


class TestNoSecretsLeak(Base):
    def test_probe_and_debug_logs(self):
        env = {"ONELAP_USER": ACCOUNT, "ONELAP_PASSWORD": PASSWORD,
               "ONELAP_SIGN_KEY": SIGN_KEY}
        buf = io.StringIO()
        self.addCleanup(logging.getLogger().handlers.clear)
        with mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(buf):
            rc = probe_main.main(["probe", "--base-url", self.fake.url,
                                  "--debug"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("PROBE OK", out)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(SIGN_KEY, out)
        self.assertNotIn(SECRET_URL_PARAM, out)
        self.assertNotIn("tok1.", out)
        self.assertNotIn("tok2.", out)

    def test_client_logs(self):
        with self.assertLogs("onelap", "DEBUG") as cm:
            c = self.client()
            c.login(ACCOUNT, PASSWORD)
            token = c.session["token"]
            list(c.iter_activities())
        text = "\n".join(cm.output)
        for secret in (PASSWORD, token, SIGN_KEY, SECRET_URL_PARAM):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
