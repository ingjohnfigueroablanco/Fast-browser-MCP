"""Thin async wrapper over cdp-use CDPClient.

Adds two things on top of the raw client:
  * a uniform ``call(method, params, session_id)`` using ``send_raw`` (version-proof),
  * multi-callback event fan-out (the underlying registry keeps only ONE handler
    per method, so we register a single dispatcher per method and fan out ourselves).

This module knows nothing about MCP, snapshots, or actions — only CDP plumbing,
so the backend can be swapped for raw ``websockets`` without touching upper layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from cdp_use.client import CDPClient

from ..config import CDP_COMMAND_TIMEOUT
from ..errors import (
    CdpProtocolError,
    ConnectionLostError,
    NavigatedDuringExecutionError,
    TimeoutBrowserError,
    classify_cdp_error,
)

EventCallback = Callable[[dict, str | None], None]


class CDPConnection:
    """Owns one persistent CDP WebSocket for the whole MCP server lifetime."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._client: CDPClient | None = None
        self._handlers: dict[str, list[EventCallback]] = {}

    @property
    def ws_url(self) -> str:
        return self._ws_url

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.ws is not None

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = CDPClient(self._ws_url)
        await client.start()
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._handlers.clear()

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = CDP_COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a CDP command and await its result.

        Raises a ``BrowserError`` subclass with an explicit ``code`` instead of
        letting raw asyncio/cdp_use exceptions (some with empty ``str()``,
        e.g. ``asyncio.TimeoutError``) reach the tool layer as a blank message.
        """
        if self._client is None:
            raise TimeoutBrowserError(
                "CDP no conectado. Llama browser_start primero.", detail={"method": method}
            )
        try:
            return await asyncio.wait_for(
                self._client.send_raw(method, params or {}, session_id),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise TimeoutBrowserError(
                f"{method} no respondio en {timeout}s (session_id={session_id})",
                detail={"method": method, "timeout": timeout, "session_id": session_id},
            ) from exc
        except ConnectionError as exc:
            raise ConnectionLostError(
                f"Conexion CDP perdida durante {method}: {exc}",
                detail={"method": method},
            ) from exc
        except RuntimeError as exc:
            # cdp_use wraps the raw CDP error payload (a dict) as RuntimeError's
            # single arg; str(exc) still renders it usefully, but classify it
            # so callers can branch on .code instead of substring-matching.
            msg = str(exc)
            code = classify_cdp_error(msg)
            if code == "NAVIGATED_DURING_EXECUTION":
                raise NavigatedDuringExecutionError(
                    f"{method} interrumpido por navegacion: {msg}",
                    detail={"method": method},
                ) from exc
            raise CdpProtocolError(msg, detail={"method": method}) from exc

    # --- events --------------------------------------------------------------
    def on(self, method: str, callback: EventCallback) -> None:
        """Subscribe to a CDP event. Multiple callbacks per method are fanned out."""
        if self._client is None:
            raise RuntimeError("CDP no conectado.")
        callbacks = self._handlers.setdefault(method, [])
        if not callbacks:
            # First subscriber for this method: install the single dispatcher.
            self._client._event_registry.register(method, self._make_dispatcher(method))
        callbacks.append(callback)

    def off(self, method: str, callback: EventCallback) -> None:
        callbacks = self._handlers.get(method)
        if not callbacks:
            return
        with_removed = [cb for cb in callbacks if cb is not callback]
        if with_removed:
            self._handlers[method] = with_removed
        else:
            self._handlers.pop(method, None)
            if self._client is not None:
                self._client._event_registry.unregister(method)

    def _make_dispatcher(self, method: str) -> EventCallback:
        def dispatch(params: dict, session_id: str | None) -> None:
            for cb in list(self._handlers.get(method, [])):
                cb(params, session_id)

        return dispatch
