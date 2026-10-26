"""Local stand-in for the OneLap API, mirroring the shapes the library expects.

All values are invented. The signature is verified with an independent
implementation of the documented scheme.
"""

import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

SIGN_KEY = "test-sign-key-not-real"
ACCOUNT = "rider@example.invalid"
PASSWORD = "correct horse battery staple"
SECRET_URL_PARAM = "SIGSECRET123"


def make_fit(records=4):
    body = b"\x00" * (records * 8)
    header = struct.pack("<BBHI4sH", 14, 0x20, 2100, len(body), b".FIT", 0)
    return header + body + b"\x00\x00"


class FakeOneLap:
    def __init__(self, rides=None, page_size=2):
        self.rides = rides if rides is not None else default_rides()
        self.page_size = page_size
        self.valid_tokens = set()
        self.expire_mode = "json"       # json | html | status
        self.logins = 0
        self.list_pages = []            # pages requested, in order
        self.rate_limit_list = 0        # answer 429 this many times
        self.always_429 = False
        self.list_hits = 0
        self.download_html = False
        self.server_error_body = None
        self._n = 0
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        self._thread = threading.Thread(
            target=self._srv.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()

    def expire_all(self):
        self.valid_tokens.clear()

    def _new_token(self):
        self._n += 1
        t = "tok%d.%s" % (self._n, "x" * 20)
        self.valid_tokens.add(t)
        return t

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sign_ok(self, parts):
                h = self.headers
                pairs = {"nonce": h["nonce"], "timestamp": h["timestamp"]}
                pairs.update({k: v for k, v in parse_qsl(parts.query) if v})
                s = parts.path + "?" + "".join(
                    "%s=%s&" % (k, pairs[k]) for k in sorted(pairs)
                ) + "key=" + SIGN_KEY
                return h["sign"] == hashlib.md5(s.encode()).hexdigest()

            def _expired(self):
                if fake.expire_mode == "status":
                    self._send(401, {"code": 401, "msg": "unauthorized"})
                elif fake.expire_mode == "html":
                    self._send(200, "<html><body>Please log in</body></html>",
                               "text/html")
                else:
                    self._send(200, {"code": 401, "msg": "token expired",
                                     "data": None})

            def do_POST(self):
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if parts.path != "/api/v1/app/login" or \
                        not self._sign_ok(parts):
                    return self._send(404, {"code": 404})
                fake.logins += 1
                md5 = hashlib.md5(PASSWORD.encode()).hexdigest()
                if body.get("account") != ACCOUNT or \
                        body.get("password") != md5:
                    return self._send(200, {"code": 400, "msg": "bad login"})
                self._send(200, {"code": 200, "data": {
                    "token": fake._new_token(), "region": 1,
                    "userinfo": {"uid": 424242, "nickname": "Test"}}})

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path.startswith("/files/"):
                    if fake.download_html:
                        return self._send(200, "<html>error</html>",
                                          "text/html")
                    return self._send(200, make_fit(),
                                      "application/octet-stream")
                if parts.path != "/api/v1/app/record/riding/list":
                    return self._send(404, {"code": 404})
                fake.list_hits += 1
                if fake.always_429 or fake.rate_limit_list > 0:
                    fake.rate_limit_list -= 1
                    return self._send(429, {"code": 429})
                if fake.server_error_body is not None:
                    return self._send(500, fake.server_error_body)
                if self.headers.get("authorization") not in fake.valid_tokens:
                    return self._expired()
                if not self._sign_ok(parts):
                    return self._send(200, {"code": 400, "msg": "bad sign"})
                q = dict(parse_qsl(parts.query))
                page = int(q.get("p", "1"))
                fake.list_pages.append(page)
                size = fake.page_size
                chunk = fake.rides[(page - 1) * size: page * size]
                days = {}
                for r in chunk:
                    day = str(r["start_time"] // 86400 * 86400)
                    days.setdefault(day, {"info": []})["info"].append(
                        fake._item(r))
                self._send(200, {"code": 200, "data": {
                    "days": days or [],
                    "page_count": -(-len(fake.rides) // size)}})

        return H

    def _item(self, r):
        return {"fileKey": r["key"],
                "fileAddr": "%s/files/%s?Signature=%s&Expires=1"
                            % (self.url, r["key"], SECRET_URL_PARAM),
                "start_time": r["start_time"], "distance": r["distance"],
                "duration": 3600, "name": "Ride %s" % r["key"]}


BASE_TS = 1_760_000_000  # invented moment in Oct 2025


def default_rides(n=5):
    # newest first, one per day
    return [{"key": "ride-%d" % i, "start_time": BASE_TS - i * 86400,
             "distance": 20000.0 + i} for i in range(n)]


