"""Unofficial client for fetching rides (FIT files) from OneLap / Magene."""

from .client import DEFAULT_BASE_URL, Activity, OneLap
from .exceptions import (ApiError, AuthError, NetworkError, NotFitError,
                         OneLapError, RateLimited, SessionExpired)
from .fit import validate_fit

__version__ = "0.1.0"

__all__ = ["OneLap", "Activity", "validate_fit", "DEFAULT_BASE_URL",
           "OneLapError", "AuthError", "SessionExpired", "RateLimited",
           "NetworkError", "ApiError", "NotFitError", "__version__"]
