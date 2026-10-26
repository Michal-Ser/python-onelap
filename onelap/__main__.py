"""``python -m onelap probe``: read-only diagnostics for the account owner.

Prints structures (keys and value types) rather than values, never writes to
disk and never prints the password, tokens, signing key or signed URLs.
"""

import argparse
import getpass
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from . import ApiError, OneLap, OneLapError
from .client import DEFAULT_BASE_URL, LIST_PATH

_SAFE_STR = re.compile(r"^[^/=?&:]{0,40}$")
_SHOW_KEYS = re.compile(r"expir|exp$|ttl|_at$|time|date|region|code|count|"
                        r"page", re.I)


def describe(obj, show_scalars=False, key="", indent=0):
    """Render keys and types; scalar values only where explicitly safe."""
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        lines.append("%sdict(%d)" % (pad, len(obj)))
        for k, v in list(obj.items())[:40]:
            sub = describe(v, show_scalars, str(k), indent + 1).lstrip()
            lines.append("%s  %s: %s" % (pad, k, sub))
    elif isinstance(obj, list):
        lines.append("%slist(%d)" % (pad, len(obj)))
        if obj:
            sub = describe(obj[0], show_scalars, key, indent + 1)
            lines.append("%s  [0]: %s" % (pad, sub.lstrip()))
    elif isinstance(obj, bool) or obj is None:
        lines.append("%s%r" % (pad, obj))
    elif isinstance(obj, (int, float)):
        shown = show_scalars or _SHOW_KEYS.search(key)
        lines.append("%s%s%s" % (pad, type(obj).__name__,
                                 " = %r" % obj if shown else ""))
    elif isinstance(obj, str):
        ok = show_scalars and _SAFE_STR.match(obj)
        lines.append("%sstr(len=%d)%s" % (pad, len(obj),
                                          " = %r" % obj if ok else ""))
    else:
        lines.append("%s%s" % (pad, type(obj).__name__))
    return "\n".join(lines)


class _Out:
    def __init__(self, secrets):
        self.secrets = secrets

    def __call__(self, text=""):
        for s in self.secrets:
            if s:
                text = text.replace(s, "<redacted>")
        print(text)


def _probe(args):
    secrets = []
    out = _Out(secrets)
    user = args.username or os.environ.get("ONELAP_USER") or input("Login: ")
    password = os.environ.get("ONELAP_PASSWORD") or getpass.getpass("Password: ")
    sign_key = os.environ.get("ONELAP_SIGN_KEY") or getpass.getpass("Sign key: ")
    secrets.extend([password, sign_key])
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

    captured = []
    client = OneLap(sign_key, base_url=args.base_url, timeout=args.timeout)
    real_http = client._http

    def spy(method, url, headers, body=None, secrets_=()):
        status, raw = real_http(method, url, headers, body, secrets_)
        captured.append((method, url.split("?")[0], status, raw))
        return status, raw

    client._http = spy
    try:
        out("base_url: %s" % args.base_url)
        client.login(user, password)
        secrets.append(client.session["token"])
        _, url, status, raw = captured[-1]
        out("\n== login: HTTP %s, response structure ==" % status)
        import json
        out(describe(json.loads(raw)))
        out("session keys: %s" % sorted(client.session))
        out("token: %s" % _token_life(client.session["token"]))

        out("\n== list ==")
        now = datetime.now(timezone.utc)
        base = {"data_type": "all", "source": "all",
                "end_time": str(int(now.timestamp()))}
        pages = []
        page_count = 1
        p = 1
        while p <= min(page_count, args.pages):
            payload = client._api(LIST_PATH, dict(base, p=str(p)))
            data = payload["data"]
            page_count = int(data.get("page_count") or 0)
            days = data.get("days") if isinstance(data.get("days"), dict) else {}
            items = [(d, i) for d, day in days.items()
                     for i in (day.get("info") or [])]
            pages.append((days, items))
            out("page %d: days=%d items=%d (page_count=%s)"
                % (p, len(days), len(items), page_count))
            if p == 1 and items:
                out("data keys: %s" % sorted(data))
                out("day key sample: %r" % next(iter(days)))
                out("item structure (safe scalars shown):")
                out(describe(items[0][1], show_scalars=True, indent=1))
            p += 1
        day_keys = [list(d.keys()) for d, _ in pages]
        out("day keys in server order, page 1: %s" % day_keys[0][:5]
            if day_keys else "no pages")

        out("\n== newest rides (library view) ==")
        newest = list(_take(client.iter_activities(
            since=now - timedelta(days=args.days)), 3))
        for a in newest:
            out("id=%s start=%s distance_m=%s duration_s=%s name=%r"
                % (a.id, a.start.isoformat(), a.distance_m, a.duration_s,
                   (a.name or "")[:40]))
        if not newest:
            out("no rides in the last %d days (try --days)" % args.days)
        else:
            starts = [a.start for a in newest]
            out("newest-first order ok: %s" % (starts == sorted(starts, reverse=True)))
            out("\n== FIT download (memory only) ==")
            data = client.download_fit(newest[0])
            out("size=%d bytes, header byte0=%d, signature=%r, valid=True"
                % (len(data), data[0], data[8:12]))
        out("\nPROBE OK")
        return 0
    except OneLapError as e:
        extra = ""
        if isinstance(e, ApiError):
            extra = " status=%s snippet=%r" % (e.status, e.snippet)
        out("\nPROBE FAILED: %s%s" % (type(e).__name__, extra))
        return 1
    except Exception as e:  # keep the report copy-pasteable
        out("\nPROBE FAILED: %s" % type(e).__name__)
        return 1


def _take(it, n):
    for i, x in enumerate(it):
        if i >= n:
            return
        yield x


def _token_life(token):
    """Report token lifetime only if it is a JWT with an exp claim."""
    import base64
    import json
    parts = token.split(".")
    if len(parts) == 3:
        try:
            pad = "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
            exp = claims.get("exp")
            iat = claims.get("iat")
            if isinstance(exp, (int, float)):
                left = exp - datetime.now(timezone.utc).timestamp()
                life = " (lifetime %.1f days)" % ((exp - iat) / 86400) \
                    if isinstance(iat, (int, float)) else ""
                return "JWT, expires in %.1f days%s" % (left / 86400, life)
        except (ValueError, TypeError):
            pass
        return "JWT-like, no readable exp"
    return "opaque, length %d, lifetime not readable" % len(token)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m onelap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="diagnose the API with your account")
    p.add_argument("--username")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--pages", type=int, default=3, help="list pages to fetch")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    return _probe(args)


if __name__ == "__main__":
    sys.exit(main())
