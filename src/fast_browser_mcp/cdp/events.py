"""Ring buffers capturing console messages and network activity via CDP events."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .connection import CDPConnection

_MAX_CONSOLE = 500
_MAX_NETWORK = 1000
_DEDUP_WINDOW_S = 0.05  # console.error can fire BOTH consoleAPICalled and Log.entryAdded


@dataclass
class ConsoleEntry:
    timestamp_s: float
    level: str
    text: str


@dataclass
class NetEntry:
    method: str
    url: str
    status: int | None = None
    resource_type: str | None = None
    failed: bool = False


class EventBuffers:
    """Subscribes to console + network events and keeps recent entries."""

    def __init__(self, conn: CDPConnection) -> None:
        self._conn = conn
        self.console: deque[ConsoleEntry] = deque(maxlen=_MAX_CONSOLE)
        self.network: deque[NetEntry] = deque(maxlen=_MAX_NETWORK)
        self._by_request: dict[str, NetEntry] = {}
        # Short-lived signatures used only to dedup consoleAPICalled vs.
        # Log.entryAdded firing for the same underlying console.error call.
        self._recent_sigs: deque[tuple[str, str, float]] = deque(maxlen=20)

    def attach(self) -> None:
        self._conn.on("Runtime.consoleAPICalled", self._on_console)
        self._conn.on("Log.entryAdded", self._on_log)
        self._conn.on("Network.requestWillBeSent", self._on_request)
        self._conn.on("Network.responseReceived", self._on_response)
        self._conn.on("Network.loadingFailed", self._on_failed)

    # --- console ---
    def _record_console(self, level: str, text: str) -> None:
        now = time.time()
        for sig_level, sig_text, sig_ts in self._recent_sigs:
            if sig_level == level and sig_text == text and (now - sig_ts) < _DEDUP_WINDOW_S:
                return
        self._recent_sigs.append((level, text, now))
        self.console.append(ConsoleEntry(timestamp_s=now, level=level, text=text))

    def _on_console(self, params: dict, _sid: str | None) -> None:
        level = params.get("type", "log")
        args = params.get("args", [])
        text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
        self._record_console(level, text)

    def _on_log(self, params: dict, _sid: str | None) -> None:
        entry = params.get("entry", {})
        self._record_console(entry.get("level", "log"), entry.get("text", ""))

    # --- network ---
    def _on_request(self, params: dict, _sid: str | None) -> None:
        req = params.get("request", {})
        entry = NetEntry(
            method=req.get("method", ""),
            url=req.get("url", ""),
            resource_type=params.get("type"),
        )
        rid = params.get("requestId")
        if rid:
            self._by_request[rid] = entry
        self.network.append(entry)

    def _on_response(self, params: dict, _sid: str | None) -> None:
        rid = params.get("requestId")
        entry = self._by_request.get(rid) if rid else None
        if entry is not None:
            entry.status = (params.get("response") or {}).get("status")
        # See _on_failed: _by_request only exists to back-fill an
        # already-buffered NetEntry; once that back-fill happened there is
        # nothing left to update, so drop the reference instead of leaking it
        # for the rest of the session.
        if rid:
            self._by_request.pop(rid, None)

    def _on_failed(self, params: dict, _sid: str | None) -> None:
        rid = params.get("requestId")
        entry = self._by_request.get(rid) if rid else None
        if entry is not None:
            entry.failed = True
        # Unlike the console/network deques (bounded, oldest-evicted),
        # _by_request previously only shrank via read_network(clear=True) — on
        # a long session with many requests it grew without bound.
        if rid:
            self._by_request.pop(rid, None)

    # --- read / clear ---
    def read_console(
        self, clear: bool = False, level: str | None = None, since_ms: int | None = None
    ) -> list[str]:
        """Return buffered console lines, newest formatting first-shown as
        "Nms ago". ``level`` filters to an exact level (error/warning/log/...);
        ``since_ms`` keeps only entries from the last N milliseconds."""
        now = time.time()
        cutoff = now - (since_ms / 1000.0) if since_ms is not None else None
        items = [
            e
            for e in self.console
            if (level is None or e.level == level) and (cutoff is None or e.timestamp_s >= cutoff)
        ]
        if clear:
            self.console.clear()
        return [f"[{int((now - e.timestamp_s) * 1000)}ms ago] [{e.level}] {e.text}" for e in items]

    def read_network(self, filter_substr: str | None = None, clear: bool = False) -> list[NetEntry]:
        items = [e for e in self.network if not filter_substr or filter_substr in e.url]
        if clear:
            self.network.clear()
            self._by_request.clear()
        return items
