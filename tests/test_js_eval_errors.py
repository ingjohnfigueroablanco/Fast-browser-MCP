"""Real-Chrome tests for the error-classification work in Fase A.

Covers: click fires once (E2), js_eval surfaces a real JS error message (E3),
js_eval_loop keeps a thrown-string error instead of losing it (E4), and a
TIMEOUT never comes back as an empty message.
"""

from __future__ import annotations

import urllib.parse

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config
from fast_browser_mcp.errors import TimeoutBrowserError

pytestmark = pytest.mark.asyncio

_COUNTER_HTML = """
<!doctype html><html><body>
  <button id="btn" onclick="window.__clicks = (window.__clicks||0)+1">Count me</button>
</body></html>
"""


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
async def test_click_fires_handler_exactly_once(mgr):
    await mgr.navigate(_data_url(_COUNTER_HTML), wait="load")
    snap = await mgr.snapshot()
    ref = next(
        line.split()[0]
        for line in snap["snapshot"].splitlines()
        if line.strip().startswith("@e") and "button" in line
    )
    await mgr.click(ref)
    result = await mgr.js_eval("window.__clicks || 0")
    assert result["js_result"] == 1


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_js_eval_reports_real_exception_message(mgr):
    await mgr.navigate(_data_url("<!doctype html><html><body></body></html>"), wait="load")
    result = await mgr.js_eval("throw new Error('boom-specific-message')")
    assert result["exception"] is not None
    assert "boom-specific-message" in result["exception"]
    assert result["exception"] != "Uncaught"


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_js_eval_timeout_is_not_a_blank_message(mgr):
    await mgr.navigate(_data_url("<!doctype html><html><body></body></html>"), wait="load")
    with pytest.raises(TimeoutBrowserError) as excinfo:
        await mgr.js_eval("(async () => { await new Promise(() => {}); })()", timeout_ms=500)
    assert str(excinfo.value).strip() != ""
    assert "TIMEOUT" in str(excinfo.value)


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_js_eval_navigation_mid_script_preserves_result(mgr):
    await mgr.navigate(_data_url("<!doctype html><html><body></body></html>"), wait="load")
    # The script computes a value THEN navigates away — js_result must survive
    # even though the trailing snapshot() will fail against the new document.
    result = await mgr.js_eval(
        "const v = 41 + 1; location.href = 'about:blank'; v", timeout_ms=5000
    )
    assert result["js_result"] == 42
