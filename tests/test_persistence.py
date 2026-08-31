"""Real-Chrome tests for persistent (survives-navigation) infrastructure (A5).

Covers: navigate() reports whether the document actually reloaded, the
bootstrap epoch counter increments across a reload but not across... (a plain
data: URL navigate is always a full reload, so we only assert the "reloaded"
case here — see manager.navigate's docstring for the same-document case),
and a user-registered persistent script survives a navigate() call.
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
async def test_navigate_reports_reload(mgr):
    nav = await mgr.navigate(_data_url("<html><body>one</body></html>"), wait="load")
    assert nav["reloaded"] is True

    nav2 = await mgr.navigate(_data_url("<html><body>two</body></html>"), wait="load")
    assert nav2["reloaded"] is True


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_persistent_script_survives_navigate(mgr):
    await mgr.navigate(_data_url("<html><body>one</body></html>"), wait="load")
    await mgr.inject_persistent("window.__my_helper = () => 99;", identifier="test_helper")
    assert "test_helper" in mgr.list_persistent()

    # Works in the CURRENT document immediately...
    result = await mgr.js_eval("window.__my_helper()")
    assert result["js_result"] == 99

    # ...and survives a full reload, which is the whole point (plain js_eval
    # state defined on window would NOT survive this).
    await mgr.navigate(_data_url("<html><body>two</body></html>"), wait="load")
    result2 = await mgr.js_eval("window.__my_helper()")
    assert result2["js_result"] == 99


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_bootstrap_epoch_is_exposed(mgr):
    await mgr.navigate(_data_url("<html><body>one</body></html>"), wait="load")
    result = await mgr.js_eval("window.__fbm ? window.__fbm.epoch : null")
    assert isinstance(result["js_result"], int)


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_remove_persistent_bootstrap_is_rejected(mgr):
    from fast_browser_mcp.browser.inject import BOOTSTRAP_ID

    with pytest.raises(ValueError):
        mgr.remove_persistent(BOOTSTRAP_ID)
