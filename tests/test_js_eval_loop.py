"""Real-Chrome tests for js_eval_loop's error/timeout handling (A2).

Exercises fast_browser_mcp.mcp.tools.js_eval_loop directly (not just the
BrowserManager layer) since the per-item error extraction and the
timeout-with-partial-progress reporting both live in the tool wrapper, not
in BrowserManager. get_browser() is monkeypatched to point at a real
BrowserManager instance instead of requiring a live FastMCP request context.
"""

from __future__ import annotations

import json

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config
from fast_browser_mcp.mcp import tools

pytestmark = pytest.mark.asyncio


def _chrome_available() -> bool:
    try:
        launcher.find_browser(Config.from_env())
        return True
    except FileNotFoundError:
        return False


@pytest.fixture
async def mgr(monkeypatch):
    cfg = Config.from_env()
    object.__setattr__(cfg, "headless", True)
    object.__setattr__(cfg, "human_delays", False)
    m = BrowserManager(cfg)
    await m.start(headless=True)
    monkeypatch.setattr(tools, "get_browser", lambda: m)
    await m.navigate("data:text/html,<html></html>", wait="load")
    try:
        yield m
    finally:
        await m.shutdown(kill=True)


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_thrown_string_error_is_not_lost(mgr):
    # __e.message would be undefined for a thrown string — String(__e) must
    # be used instead so the error field doesn't silently vanish (E4).
    out = await tools.js_eval_loop(
        items=[{"n": 1}],
        script="throw 'plain-string-failure'",
        delay_ms=0,
    )
    assert "js_result=" in out
    payload = json.loads(out.splitlines()[0].removeprefix("js_result="))
    assert payload[0]["ok"] is False
    assert payload[0]["error"] == "plain-string-failure"


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_timeout_reports_partial_progress_not_a_blank_error(mgr):
    out = await tools.js_eval_loop(
        items=[{"n": 1}, {"n": 2}, {"n": 3}],
        script="if (item.n === 2) { await new Promise(() => {}); } return item.n;",
        delay_ms=0,
        timeout_ms=1500,
    )
    assert out.startswith("ERROR code=TIMEOUT")
    assert "Progreso: 1/3" in out
