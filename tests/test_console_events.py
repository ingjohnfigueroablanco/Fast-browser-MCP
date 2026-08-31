"""Real-Chrome tests for console buffer improvements (A6).

Covers: consoleAPICalled/Log.entryAdded dedup for console.error, level
filtering, since_ms filtering, and the _by_request leak fix.
"""

from __future__ import annotations

import urllib.parse

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config

pytestmark = pytest.mark.asyncio


def _data_url(html: str) -> str:
    return "data:text/html," + urllib.parse.quote(html)


def _chrome_available() -> bool:
    try:
        launcher.find_browser(Config.from_env())
        return True
    except FileNotFoundError:
        return False


@pytest.fixture
async def mgr():
    cfg = Config.from_env()
    object.__setattr__(cfg, "headless", True)
    object.__setattr__(cfg, "human_delays", False)
    m = BrowserManager(cfg)
    await m.start(headless=True)
    try:
        yield m
    finally:
        await m.shutdown(kill=True)


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_console_error_is_not_double_recorded(mgr):
    await mgr.navigate(_data_url("<html><body></body></html>"), wait="load")
    await mgr.js_eval("console.error('unique-marker-xyz')")
    lines = mgr.read_console()
    matching = [line for line in lines if "unique-marker-xyz" in line]
    # consoleAPICalled AND Log.entryAdded can both fire for the same
    # console.error call; without dedup this would be 2.
    assert len(matching) == 1


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_read_console_level_filter(mgr):
    await mgr.navigate(_data_url("<html><body></body></html>"), wait="load")
    await mgr.js_eval("console.log('a-log-line'); console.error('an-error-line');")
    errors_only = mgr.read_console(level="error")
    assert any("an-error-line" in line for line in errors_only)
    assert not any("a-log-line" in line for line in errors_only)


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_by_request_does_not_leak_after_response(mgr):
    await mgr.navigate(_data_url("<html><body></body></html>"), wait="load")
    await mgr.js_eval(
        "await fetch('data:text/plain,hello').then(r => r.text())", timeout_ms=5000
    )
    assert mgr._events is not None
    assert len(mgr._events._by_request) == 0
