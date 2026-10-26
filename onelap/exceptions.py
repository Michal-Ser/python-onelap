"""Exceptions raised by the library. Everything derives from OneLapError.

Every exception carries what the server said, when there was a response:
``status`` (HTTP status), ``code`` (application ``code`` from the JSON body),
``server_message`` (its ``msg``/``message``) and ``snippet`` (scrubbed start of
the body, at most 200 chars). Secrets are always redacted from these.
"""


class OneLapError(Exception):
    def __init__(self, message="", status=None, snippet="", code=None,
                 server_message=None):
        super().__init__(message)
        self.status = status
        self.snippet = snippet
        self.code = code
        self.server_message = server_message

    def __str__(self):
        parts = [super().__str__()]
        if self.status is not None:
            parts.append("http=%s" % self.status)
        if self.code is not None:
            parts.append("code=%s" % self.code)
        if self.server_message:
            parts.append("server says: %s" % self.server_message)
        text = " ".join(p for p in parts if p)
        if self.snippet and self.snippet != self.server_message:
            text += " | body: %s" % self.snippet
        return text


class AuthError(OneLapError):
    """Wrong credentials. A human has to fix the account/password."""


class SessionExpired(OneLapError):
    """The session is not accepted and could not (or may not) be renewed."""


class RateLimited(OneLapError):
    """HTTP 429 persisted after all retries. Try again later."""


class NetworkError(OneLapError):
    """Transport failure (DNS, connection, timeout) after all retries."""


class ApiError(OneLapError):
    """Unexpected status or response shape: the API probably changed."""


class NotFitError(OneLapError):
    """A downloaded file is not a FIT file."""
