"""Human-like mouse actions via CDP Input domain (real coordinates, not JS click)."""

from __future__ import annotations

import asyncio
import random

from ..cdp.connection import CDPConnection
from ..config import HUMAN_MOUSE_MOVE_DELAY, HUMAN_MOUSE_PRESS_DELAY
from ..snapshot.refmap import RefEntry
from .resolve import center_of


async def _delay(window: tuple[float, float], human: bool) -> None:
    if human:
        await asyncio.sleep(random.uniform(*window))


async def click(conn: CDPConnection, entry: RefEntry, human: bool = True) -> None:
    """Click an element via real CDP Input events (mousemove/down/up).

    This dispatches ONLY the CDP mouse sequence — no follow-up JS click. A
    genuine click already fires everything a real user click fires (including
    React 17+ delegated handlers); adding a JS `element.click()` on top double-
    fires any handler bound to the click event (double form submit, duplicate
    row creation). Use the separate `js_click` tool when CDP mouse events truly
    don't reach a handler.
    """
    x, y = await center_of(conn, entry)
    sid = entry.session_id
    base = {"x": x, "y": y}
    await conn.call("Input.dispatchMouseEvent", {**base, "type": "mouseMoved"}, sid)
    await _delay(HUMAN_MOUSE_MOVE_DELAY, human)
    await conn.call(
        "Input.dispatchMouseEvent",
        {**base, "type": "mousePressed", "button": "left", "clickCount": 1},
        sid,
    )
    await _delay(HUMAN_MOUSE_PRESS_DELAY, human)
    await conn.call(
        "Input.dispatchMouseEvent",
        {**base, "type": "mouseReleased", "button": "left", "clickCount": 1},
        sid,
    )


async def js_click(conn: CDPConnection, entry: RefEntry) -> None:
    """Fire element.click() via JS — reliable for React synthetic event handlers."""
    obj = await conn.call(
        "DOM.resolveNode",
        {"backendNodeId": entry.backend_node_id},
        session_id=entry.session_id,
    )
    object_id = (obj.get("object") or {}).get("objectId")
    if object_id:
        await conn.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(){this.click();}",
                "returnByValue": True,
            },
            session_id=entry.session_id,
        )


async def hover(conn: CDPConnection, entry: RefEntry) -> None:
    x, y = await center_of(conn, entry)
    await conn.call(
        "Input.dispatchMouseEvent",
        {"x": x, "y": y, "type": "mouseMoved"},
        entry.session_id,
    )
