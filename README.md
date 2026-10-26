# python-onelap

Unofficial, dependency-free Python (≥ 3.11) client for **OneLap / Magene**:
log in, list rides, download the raw **FIT** file of each ride.

> Not affiliated with or endorsed by OneLap or Magene. OneLap has no public
> API; this library uses the one their app uses, as reverse-engineered by
> community projects. It can break whenever they change it.

The library keeps no state of its own: it does not remember which rides you
fetched, does not write to disk (the session is a `dict` you store), does not
parse FIT contents and never returns your password.

## Install

```
pip install https://github.com/Michal-Ser/python-onelap/archive/refs/tags/v0.1.0.tar.gz
```

## The signing key

Every request is signed with an MD5 over a constant that ships inside the
OneLap app (`BuildConfig.ApiKey`). **This repository does not contain it.**
Pass it as `sign_key=` (see the *OneLapFit* protocol notes in the project
linked below, which publishes it). Keep it in your config, not in code.

## Usage

```python
import json
from onelap import OneLap, SessionExpired, RateLimited, NotFitError

def save(session):                      # you decide where it lives
    json.dump(session, open("/var/lib/me/onelap.json", "w"))

client = OneLap(
    sign_key=SIGN_KEY,
    session=load_session_or_None(),
    username=USER, password=PASSWORD,   # optional: allows ONE automatic login
    on_session_change=save,
)

try:
    for a in client.iter_activities(since=last_seen):   # newest first, lazy
        if a.id in already_done:
            continue
        data = client.download_fit(a)                   # validated bytes
        ...
except (SessionExpired, AuthError):
    ...   # human needed
except RateLimited:
    ...   # try next hour
```

`Activity`: `id` (the FIT `fileKey`), `start` (aware UTC datetime), `name`,
`distance_m`, `duration_s` (`None` when absent), `raw` (the list item with
URL query strings replaced by `?<redacted>`; the signed download link is kept
privately on the object and used by `download_fit`).

`download_fit` validates: more than 14 bytes, header byte 12 or 14, `.FIT` at
bytes 8–11, and file length ≥ header + declared data size + 2 (CRC). Anything
else (e.g. an HTML error page) raises `NotFitError`.

### Exceptions (all derive from `OneLapError`)

| Exception | When | For the caller |
|---|---|---|
| `AuthError` | login rejected | human needed |
| `SessionExpired` | session rejected and cannot/may not be renewed | human needed |
| `RateLimited` | HTTP 429 after 3 attempts | try later |
| `NetworkError` | transport failure after 3 attempts | try later |
| `ApiError` | unexpected status/shape; has `status`, `snippet` (≤200 chars, scrubbed) | API changed, report |
| `NotFitError` | downloaded file is not FIT | that ride only |

Session handling: the stored session is always tried first. If it is rejected
and credentials were given, **one** re-login happens per instance (then the
credentials are dropped; only the password's MD5 is ever held). A second
rejection raises `SessionExpired`. Expiry is recognised by content, not just
by status: HTTP 401/403, a JSON body with `code` 401/403, or a non-JSON (HTML)
body on a JSON endpoint. 429/5xx and transport errors are retried up to 3
attempts with growing pauses (`retries=`, `backoff=`).

Logging goes to the `onelap` logger; passwords, tokens, the signing key and
signed URLs are never logged or put into exception text.

## What we know about the API, and from where

Primary variant: the **international OneLapFit backend**
(`https://api-fitness.rfsvr.com`, `base_url=` is a parameter). Facts below
come from the README and source of
[just-nobody/onelap-fit-export](https://github.com/just-nobody/onelap-fit-export)
(MIT); only facts were used, no code was copied. Nothing was verified against
a live account.

- `POST /api/v1/app/login`, JSON `{"account", "password": md5hex}` →
  `{"code": 200, "data": {"token", "userinfo": {"uid"}, "region"?}}`.
- Every call is signed: headers `nonce` (base64 of 6 random bytes),
  `timestamp` (unix s), `sign` = md5(`path?` + sorted `k=v&` of nonce,
  timestamp and non-empty query params + `key=<sign_key>`); plus
  `authorization` (raw token), `userid`, `platform`, `app-name: onelapfit`,
  `app-version`, `onelap-region`.
- `GET /api/v1/app/record/riding/list?data_type=all&source=all&start_time&end_time&p=N`
  → `data.days.{timestamp}.info[]` with `fileKey`, `fileAddr`; `data.page_count`.
  An empty `days` can be `[]`; a wrong region gives an empty list, not an error.
- `fileAddr` is a presigned storage URL fetched without auth headers; it
  expires, so download soon after listing.
- Related notes: [Tyan66666/WanSync#14](https://github.com/Tyan66666/WanSync/pull/14)
  (bare `fileKey`s; a nonexistent path can return a 160-byte placeholder FIT,
  which the length check rejects).
- The Chinese edition (`onelap.cn`, `u.onelap.cn`) reportedly uses a different,
  also signed API; it is **not** supported in 0.1.0.

**Unverified guesses** (the probe should settle them): the per-ride field
names for start time, distance, duration and name (several candidates are
tried, falling back to the day key), their units/time zone (assumed unix
seconds, metres, seconds), the exact shape of an expired-token reply, the
token lifetime, whether list pages are newest-first, and whether `start_time`
filtering works server-side (the library also filters client-side).

## Probe

Read-only diagnostics for the account owner; writes nothing to disk and prints
structures, never secrets:

```
python -m onelap probe [--base-url URL] [--pages 3] [--days 365] [--debug]
```

It prompts for login, password and sign key (or reads `ONELAP_USER`,
`ONELAP_PASSWORD`, `ONELAP_SIGN_KEY`), then prints the login response shape,
token lifetime if readable, pagination, the ride item structure, the three
newest rides and the result of downloading one FIT into memory. It ends with
`PROBE OK` or `PROBE FAILED: <ExceptionName>`.

## Development

```
pip install pyflakes && python -m pyflakes onelap tests && python -m unittest
```

Tests run against a local `http.server` stand-in with invented data and no
network. License: MIT.
