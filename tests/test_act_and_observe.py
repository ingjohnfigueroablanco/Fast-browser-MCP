"""Real-Chrome tests for act_and_observe (Fase C).

Covers: a transient toast that self-destructs is captured by the mutation
timeline (both its appearance AND its disappearance), and a console.error
emitted during the action shows up in console_delta in the SAME response.
"""

from __future__ import annotations

import urllib.parse

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config

pytestmark = pytest.mark.asyncio

_TOAST_HTML = """
<!doctype html><html><body>
  <button id="btn" onclick="
    console.error('save-failed-xyz');
    var t = document.createElement('div');
    t.setAttribute('role', 'alert');
    t.textContent = 'Conductor asignado';
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 800);
  ">Guardar</button>
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
async def test_act_and_observe_captures_toast_lifecycle(mgr):
    await mgr.navigate(_data_url(_TOAST_HTML), wait="load")
    snap = await mgr.snapshot()
    ref = next(
        line.split()[0]
        for line in snap["snapshot"].splitlines()
        if line.strip().startswith("@e") and "button" in line
    )

    result = await mgr.act_and_observe(action="click", ref=ref, watch_ms=2000)

    assert any("role=alert" in line and " + " in line for line in result["timeline"])
    assert any("role=alert" in line and " - " in line for line in result["timeline"])
    assert any("save-failed-xyz" in line for line in result["console_delta"])


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_act_and_observe_unknown_action_is_bad_argument(mgr):
    from fast_browser_mcp.errors import BadArgumentError

    await mgr.navigate(_data_url("<html><body></body></html>"), wait="load")
    with pytest.raises(BadArgumentError):
        await mgr.act_and_observe(action="not_a_real_action", watch_ms=100)
