"""OneLap (Magene) client: login, activity list, FIT download.

Uses only the standard library. The API is unofficial and reverse-engineered
by community projects; see the README for sources and what is unverified.
"""

import base64
import copy
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .exceptions import (ApiError, AuthError, NetworkError, RateLimited,
                         SessionExpired)
from .fit import validate_fit

__all__ = ["OneLap", "Activity", "DEFAULT_BASE_URL"]

log = logging.getLogger("onelap")

DEFAULT_BASE_URL = "https://api-fitness.rfsvr.com"
DEFAULT_USER_AGENT = "python-onelap/0.1.0"
LOGIN_PATH = "/api/v1/app/login"
LIST_PATH = "/api/v1/app/record/riding/list"

# Application-level "not authenticated" codes in a JSON body (UNVERIFIED:
# the probe has to confirm what an expired token really looks like).
EXPIRED_CODES = frozenset({401, 403})
EXPIRED_STATUSES = frozenset({401, 403})

_MAX_DOWNLOAD = 64 * 1024 * 1024

# Candidate keys of an item in the activity list (UNVERIFIED except
# fileKey/fileAddr). First present key wins.
_START_KEYS = ("start_riding_time", "start_time", "created_at", "created_time",
               "start", "time")
_NAME_KEYS = ("name", "title", "ride_name")
_DISTANCE_KEYS = ("distance", "total_distance", "distance_m")
_DURATION_KEYS = ("duration", "total_time", "time_seconds", "moving_time",
                  "elapsed_time")
_URL_KEYS = ("fileAddr", "durl", "fitUrl")
_ID_KEYS = ("fileKey", "id")

_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?]*)\?[^\s\"'<>]*")


class _Expired(Exception):
    """Internal: the server did not accept the session."""

    def __init__(self, status=None, code=None, msg=None, snippet=""):
        super().__init__()
        self.info = {"status": status, "code": code, "server_message": msg,
                     "snippet": snippet}


@dataclass(frozen=True)
class Activity:
    id: str
    start: datetime
    name: "str | None" = None
    distance_m: "float | None" = None
    duration_s: "float | None" = None
    raw: dict = field(default_factory=dict, compare=False)
    # Signed download link; deliberately not part of repr/raw/equality.
    _download_url: "str | None" = field(default=None, repr=False,
                                        compare=False)


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _first(item, keys):
    for k in keys:
        if item.get(k) not in (None, ""):
            return item[k]
    return None


def _parse_time(value):
    """Unix seconds (or ms) or ISO string -> aware UTC datetime, or None."""
    n = _num(value)
    if n is not None:
        if n > 1e11:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _scrub_urls(obj):
    """Deep copy with the query string of every URL-looking string removed."""
    if isinstance(obj, dict):
        return {k: _scrub_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_urls(v) for v in obj]
    if isinstance(obj, str) and "://" in obj:
        return _URL_QUERY_RE.sub(r"\1?<redacted>", obj)
    return obj


class OneLap:
    def __init__(self, sign_key, session=None, username=None, password=None,
                 base_url=DEFAULT_BASE_URL, timeout=30,
                 user_agent=DEFAULT_USER_AGENT, on_session_change=None,
                 retries=3, backoff=2.0, app_version="1.9.1", platform="43"):
        if not sign_key:
            raise ValueError("sign_key is required (see README)")
        self._sign_key = sign_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.on_session_change = on_session_change
        self.retries = max(1, int(retries))
        self.backoff = backoff
        self.app_version = app_version
        self.platform = platform
        self._device_id = str(uuid.uuid4())
        self._sleep = time.sleep
        self._session = None
        if session:
            if not session.get("token") or not session.get("uid"):
                raise ValueError("session must contain 'token' and 'uid'")
            self._session = {
                "token": str(session["token"]),
                "uid": str(session["uid"]),
                "region": str(session.get("region") or "1"),
            }
        # Only the MD5 digest is kept, and only until the single automatic
        # login has been spent.
        self._account = None
        self._pw_md5 = None
        if username and password:
            self._account = username
            self._pw_md5 = hashlib.md5(password.encode()).hexdigest()
        self._auto_login_used = False

    # ------------------------------------------------------------ session
    @property
    def session(self):
        """JSON-serializable session dict (no password), or None."""
        if self._session is None:
            return None
        return dict(self._session)

    def login(self, username, password):
        """Explicit login. Raises AuthError for rejected credentials."""
        md5 = hashlib.md5(password.encode()).hexdigest()
        self._login(username, md5)

    def _login(self, account, pw_md5):
        url = self.base_url + LOGIN_PATH
        body = json.dumps({"account": account, "password": pw_md5}).encode()
        headers = self._signed_headers(LOGIN_PATH, {}, token=None, uid="0",
                                       region="1")
        headers["content-type"] = "application/json"
        secrets = (pw_md5,)
        status, raw = self._http("POST", url, headers, body, secrets)
        payload = self._json(status, raw, secrets)
        if payload is None:
            raise ApiError("login returned non-JSON", status,
                           self._snippet(raw, secrets))
        if payload.get("code") != 200:
            raise AuthError("login rejected", status,
                            self._snippet(raw, secrets), payload.get("code"),
                            self._server_message(payload, secrets))
        data = payload.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        info = data.get("userinfo") if isinstance(data, dict) else None
        uid = info.get("uid") if isinstance(info, dict) else None
        if not token or uid in (None, ""):
            raise ApiError("login response lacks token/uid", status,
                           self._snippet(raw, secrets), payload.get("code"),
                           self._server_message(payload, secrets))
        self._session = {"token": str(token), "uid": str(uid),
                         "region": str(data.get("region") or "1")}
        log.debug("logged in")
        self._notify()

    def _notify(self):
        if self.on_session_change:
            self.on_session_change(self.session)

    # --------------------------------------------------------------- http
    def _secrets(self, extra=()):
        out = [self._sign_key, *extra]
        if self._session:
            out.append(self._session["token"])
        return [s for s in out if s]

    def _snippet(self, raw, extra=()):
        text = raw[:2000].decode("utf-8", "replace")
        text = _scrub_urls(text)
        for s in self._secrets(extra):
            text = text.replace(s, "<redacted>")
        return " ".join(text.split())[:200]

    def _server_message(self, payload, extra=()):
        for k in ("msg", "message", "error", "errmsg"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                text = _scrub_urls(v)
                for s in self._secrets(extra):
                    text = text.replace(s, "<redacted>")
                return " ".join(text.split())[:200]
        return None

    def _signed_headers(self, path, params, token, uid, region):
        nonce = base64.b64encode(os.urandom(6)).decode()
        ts = str(int(time.time()))
        pairs = {"nonce": nonce, "timestamp": ts}
        pairs.update({k: str(v) for k, v in params.items() if str(v) != ""})
        sign_str = path + "?" + "".join(
            "%s=%s&" % (k, pairs[k]) for k in sorted(pairs)
        ) + "key=" + self._sign_key
        headers = {
            "user-agent": self.user_agent,
            "accept": "*/*",
            "nonce": nonce,
            "timestamp": ts,
            "sign": hashlib.md5(sign_str.encode()).hexdigest(),
            "userid": uid,
            "deviceid": self._device_id,
            "platform": self.platform,
            "app-name": "onelapfit",
            "app-version": self.app_version,
            "version": self.app_version,
            "language": "en",
            "app-language": "en",
            "timezone": "UTC",
            "onelap-region": region,
        }
        if token:
            headers["authorization"] = token
        return headers

    def _http(self, method, url, headers, body=None, secrets=()):
        """One logical request with retries. Returns (status, bytes).

        Retries 429 and 5xx (and transport errors) up to ``self.retries``
        attempts with growing pauses, then raises.
        """
        parts = urllib.parse.urlsplit(url)
        where = "%s %s%s" % (method, parts.netloc, parts.path)
        last = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = r.read(_MAX_DOWNLOAD + 1)
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
                data = e.read(_MAX_DOWNLOAD + 1)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = ("net", type(e).__name__)
                log.debug("%s attempt %d: %s", where, attempt, type(e).__name__)
                status = None
            if status is not None:
                log.debug("%s attempt %d -> %s", where, attempt, status)
                if len(data) > _MAX_DOWNLOAD:
                    raise ApiError("response too large", status)
                if status != 429 and status < 500:
                    return status, data
                last = ("status", status, data)
            if attempt < self.retries:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
        if last[0] == "net":
            raise NetworkError("request failed: %s" % last[1])
        status, data = last[1], last[2]
        if status == 429:
            raise RateLimited("rate limited after %d attempts" % self.retries,
                              status, self._snippet(data, secrets))
        raise ApiError("server error", status, self._snippet(data, secrets))

    def _json(self, status, raw, secrets=()):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    # -------------------------------------------------------- authed calls
    def _ensure_session(self):
        if self._session is not None:
            return
        self._auto_login()

    def _auto_login(self, rejected=None):
        if self._auto_login_used or not self._account:
            raise SessionExpired(
                "session missing or expired and cannot be renewed",
                **(rejected.info if rejected else {}))
        self._auto_login_used = True
        account, pw = self._account, self._pw_md5
        self._account = self._pw_md5 = None
        self._login(account, pw)

    def _api_once(self, path, params):
        s = self._session
        headers = self._signed_headers(path, params, s["token"], s["uid"],
                                       s["region"])
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, raw = self._http("GET", url, headers)
        payload = self._json(status, raw)
        code = payload.get("code") if payload else None
        msg = self._server_message(payload) if payload else None
        if status in EXPIRED_STATUSES or payload is None \
                or code in EXPIRED_CODES:
            # Also covers an HTML login page or other non-JSON body.
            raise _Expired(status, code, msg, self._snippet(raw))
        if status != 200 or code != 200:
            raise ApiError("unexpected response", status, self._snippet(raw),
                           code, msg)
        return payload

    def _api(self, path, params):
        self._ensure_session()
        try:
            return self._api_once(path, params)
        except _Expired as first:
            log.debug("session rejected")
            rejected = first
        self._session = None
        self._auto_login(rejected)
        try:
            return self._api_once(path, params)
        except _Expired as again:
            raise SessionExpired("session rejected right after login",
                                 **again.info) from None

    # --------------------------------------------------------- activities
    def iter_activities(self, since=None, until=None):
        """Lazily yield activities, newest first, paging internally."""
        since, until = _aware(since), _aware(until)
        page = 1
        while True:
            params = {"data_type": "all", "source": "all", "p": str(page),
                      "end_time": str(int((until or datetime.now(timezone.utc))
                                          .timestamp()))}
            if since:
                params["start_time"] = str(int(since.timestamp()))
            payload = self._api(LIST_PATH, params)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ApiError("list response lacks data", 200,
                               self._snippet(json.dumps(payload).encode()))
            days = data.get("days")
            if not isinstance(days, dict):  # empty day map arrives as []
                days = {}
            acts = []
            for day_ts, day in days.items():
                items = day.get("info") if isinstance(day, dict) else None
                for item in items or []:
                    if isinstance(item, dict):
                        acts.append(self._make_activity(item, day_ts))
            acts.sort(key=lambda a: a.start, reverse=True)
            for a in acts:
                if until and a.start > until:
                    continue
                if since and a.start < since:
                    return
                yield a
            try:
                page_count = int(data.get("page_count") or 0)
            except (TypeError, ValueError):
                raise ApiError("bad page_count", 200) from None
            if page >= page_count:
                return
            page += 1

    def _make_activity(self, item, day_ts):
        ident = _first(item, _ID_KEYS)
        if ident is None:
            raise ApiError("activity without id/fileKey", 200,
                           "keys: " + ",".join(sorted(map(str, item)))[:150])
        start = _parse_time(_first(item, _START_KEYS)) or _parse_time(day_ts)
        if start is None:
            raise ApiError("activity without start time", 200,
                           "keys: " + ",".join(sorted(map(str, item)))[:150])
        name = _first(item, _NAME_KEYS)
        url = _first(item, _URL_KEYS)
        return Activity(
            id=str(ident), start=start,
            name=str(name) if name is not None else None,
            distance_m=_num(_first(item, _DISTANCE_KEYS)),
            duration_s=_num(_first(item, _DURATION_KEYS)),
            raw=_scrub_urls(copy.deepcopy(item)),
            _download_url=url if isinstance(url, str) else None,
        )

    def download_fit(self, activity):
        """Return the activity's FIT file as validated bytes."""
        url = activity._download_url
        if not url:
            raise ApiError("activity has no download link")
        # Presigned storage URL: no auth headers, nothing of ours attached.
        status, raw = self._http("GET", url, {"user-agent": self.user_agent})
        if status != 200:
            raise ApiError("FIT download failed (link expired?)", status,
                           self._snippet(raw))
        return validate_fit(raw)
