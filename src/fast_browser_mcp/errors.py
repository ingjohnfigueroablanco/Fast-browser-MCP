"""Shared error taxonomy for CDP/browser failures.

Every failure that can reach an MCP tool response should end up as a
BrowserError with an explicit ``code`` so the calling LLM can distinguish
"my script failed" from "the transport hiccuped" from "the page navigated
mid-script" — instead of getting an empty or misleading message.
"""

from __future__ import annotations


class BrowserError(Exception):
    """Base error carrying a machine-readable code plus a human message."""

    code = "CDP_ERROR"

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class TimeoutBrowserError(BrowserError):
    code = "TIMEOUT"


class ConnectionLostError(BrowserError):
    code = "CONNECTION_LOST"


class NavigatedDuringExecutionError(BrowserError):
    code = "NAVIGATED_DURING_EXECUTION"


class JsExceptionError(BrowserError):
    code = "JS_EXCEPTION"


class StaleRefBrowserError(BrowserError):
    code = "STALE_REF"


class ElementNotVisibleError(BrowserError):
    code = "ELEMENT_NOT_VISIBLE"


class ElementGoneError(BrowserError):
    code = "ELEMENT_GONE"


class BrowserNotStartedError(BrowserError):
    code = "BROWSER_NOT_STARTED"


class CdpProtocolError(BrowserError):
    code = "CDP_ERROR"


class BadArgumentError(BrowserError):
    code = "BAD_ARGUMENT"


# Substrings cdp_use / Chrome uses to signal a session died because the page
# navigated or the execution context was torn down mid-call. Matched against
# the raw CDP error message to classify it as NAVIGATED_DURING_EXECUTION
# rather than a generic CDP_ERROR.
_NAVIGATED_MARKERS = (
    "execution context was destroyed",
    "cannot find context",
    "inspected target navigated",
)

# Substrings meaning "the session/target is gone" — kept as CDP_ERROR (not
# translated to a new code) so existing string-matching retry sites that key
# on "session"/"not found" keep working after this lands.
_SESSION_GONE_MARKERS = ("session", "not found")


def classify_cdp_error(message: str) -> str:
    """Best-effort classification of a raw CDP error message into a code."""
    low = message.lower()
    if any(marker in low for marker in _NAVIGATED_MARKERS):
        return "NAVIGATED_DURING_EXECUTION"
    return "CDP_ERROR"
