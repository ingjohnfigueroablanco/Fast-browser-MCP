"""Collect accessibility snapshots from the page and its (OOPIF) iframes."""

from __future__ import annotations

import asyncio

from ..cdp.connection import CDPConnection
from ..errors import CdpProtocolError
from .refmap import RefMap
from .serializer import serialize

_MIN_USEFUL_NODES = 5   # below this, assume SPA hasn't rendered and retry
_RENDER_POLL_DELAY = 0.25
_RENDER_MAX_RETRIES = 12  # up to 3 seconds total


async def _get_ax_nodes(
    conn: CDPConnection, session_id: str | None, skip_retry: bool = False
) -> list[dict]:
    """Fetch AX tree with retry until React/SPA finishes rendering.

    ``skip_retry`` bypasses the up-to-3s retry loop for sessions already known
    to render a real tree — a legitimately sparse page (a bare ``<h1>``, an
    ``about:blank``) would otherwise pay the full retry cost on *every*
    snapshot for the life of that session.
    """
    if skip_retry:
        ax = await conn.call("Accessibility.getFullAXTree", session_id=session_id)
        return ax.get("nodes", [])
    for _ in range(_RENDER_MAX_RETRIES):
        ax = await conn.call("Accessibility.getFullAXTree", session_id=session_id)
        nodes = ax.get("nodes", [])
        if len(nodes) >= _MIN_USEFUL_NODES:
            return nodes
        await asyncio.sleep(_RENDER_POLL_DELAY)
    return nodes  # return whatever we got after max retries


async def capture(
    conn: CDPConnection,
    refmap: RefMap,
    session_id: str | None,
    child_sessions: list[str] | None = None,
    interactive_only: bool = True,
    warm_sessions: set[str | None] | None = None,
) -> tuple[str, int]:
    """Capture a snapshot for the main session plus any attached iframe sessions.

    Returns (text, snapshot_id). ``refmap`` is reset to a new generation.
    ``warm_sessions``, if provided, is a set the caller keeps across snapshots;
    a session is added to it once it has produced a real (non-sparse) tree, and
    from then on skips the SPA-render retry loop.
    """
    snapshot_id = refmap.begin()
    sessions: list[str | None] = [session_id, *(child_sessions or [])]
    blocks: list[str] = []

    for sid in sessions:
        try:
            skip_retry = warm_sessions is not None and sid in warm_sessions
            nodes = await _get_ax_nodes(conn, sid, skip_retry=skip_retry)
            if warm_sessions is not None and len(nodes) >= _MIN_USEFUL_NODES:
                warm_sessions.add(sid)
        except CdpProtocolError as exc:
            if sid != session_id and (
                "session" in exc.message.lower() or "not found" in exc.message.lower()
            ):
                # A child iframe session (e.g. a captcha widget) was torn down
                # by navigation; skip it rather than failing the whole snapshot.
                continue
            raise
        if not nodes:
            continue
        text = serialize(nodes, refmap, interactive_only=interactive_only, session_id=sid)
        if text:
            if sid and sid != session_id:
                blocks.append(f"# iframe [{sid[:8]}]\n{text}")
            else:
                blocks.append(text)

    return "\n".join(blocks), snapshot_id
