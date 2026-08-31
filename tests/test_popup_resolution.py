"""Real-Chrome tests for popup resolution (B2 snapshot annotation + B3 tool).

Reproduces the universal "dropdown rendered as a child of <body>, alongside
unrelated background content that happens to share the same role/text" case
that caused wrong clicks in the original bug report — with and without
aria-controls wiring.
"""

from __future__ import annotations

import urllib.parse

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config

pytestmark = pytest.mark.asyncio

# A combobox wired with aria-expanded/aria-controls to a listbox rendered as a
# direct child of <body> (the "portal" pattern), PLUS a background element
# with role=option and the exact same text, to prove only the real popup gets
# resolved/clicked.
_ARIA_WIRED_HTML = """
<!doctype html><html><body>
  <div role="option" id="decoy" style="position:absolute; left:-9999px;">Juan Perez</div>
  <button id="combo" role="combobox" aria-expanded="true" aria-controls="popup">CONDUCTOR</button>
  <ul id="popup" role="listbox" style="position:fixed; top:100px; left:100px;">
    <li role="option" id="real-juan" onclick="window.__picked='real-juan'">Juan Perez</li>
    <li role="option" id="ana" onclick="window.__picked='ana'">Ana Gomez</li>
  </ul>
</body></html>
"""

# Same scenario but with NO aria-controls/aria-expanded at all — forces the
# tier 2/3 JS-based fallback (visible role=listbox search).
_NO_ARIA_HTML = """
<!doctype html><html><body>
  <span>Juan Perez</span>
  <div role="listbox" style="position:fixed; top:100px; left:100px;">
    <div role="option" onclick="window.__picked='fallback-juan'">Juan Perez</div>
    <div role="option" onclick="window.__picked='fallback-ana'">Ana Gomez</div>
  </div>
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
async def test_snapshot_marks_only_the_real_popup(mgr):
    await mgr.navigate(_data_url(_ARIA_WIRED_HTML), wait="load")
    snap = await mgr.snapshot(interactive_only=False)
    lines = snap["snapshot"].splitlines()
    marked = [line for line in lines if "[POPUP-ABIERTO]" in line]
    assert len(marked) == 1
    assert "listbox" in marked[0]


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_click_popup_option_clicks_real_option_not_decoy(mgr):
    await mgr.navigate(_data_url(_ARIA_WIRED_HTML), wait="load")
    await mgr.click_popup_option("Juan Perez")
    picked = await mgr.js_eval("window.__picked || null")
    assert picked["js_result"] == "real-juan"


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_click_popup_option_falls_back_without_aria(mgr):
    await mgr.navigate(_data_url(_NO_ARIA_HTML), wait="load")
    await mgr.click_popup_option("Ana Gomez")
    picked = await mgr.js_eval("window.__picked || null")
    assert picked["js_result"] == "fallback-ana"


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_click_popup_option_no_match_lists_available_options(mgr):
    await mgr.navigate(_data_url(_ARIA_WIRED_HTML), wait="load")
    from fast_browser_mcp.browser.manager import BrowserManager as _BM  # noqa: F401
    from fast_browser_mcp.errors import BadArgumentError

    with pytest.raises(BadArgumentError) as excinfo:
        await mgr.click_popup_option("Nonexistent Person")
    assert "Juan Perez" in str(excinfo.value)
