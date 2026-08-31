"""End-to-end smoke test against a real (headless) Chrome.

Skips automatically if no Chrome/Edge is installed. Exercises the full path:
launch -> navigate -> snapshot (refs) -> human click -> fill -> verify DOM state.
"""

from __future__ import annotations

import urllib.parse

import pytest

from fast_browser_mcp.browser.manager import BrowserManager
from fast_browser_mcp.chrome import launcher
from fast_browser_mcp.config import Config

pytestmark = pytest.mark.asyncio

_HTML = """
<!doctype html><html><body>
  <h1>Demo</h1>
  <form>
    <label>Email <input id="email" type="text" aria-label="Email" required></label>
    <button id="btn" type="button"
      onclick="document.getElementById('out').textContent='clicked:'+document.getElementById('email').value">
      Entrar
    </button>
  </form>
  <div id="out"></div>
</body></html>
"""


def _data_url() -> str:
    return "data:text/html," + urllib.parse.quote(_HTML)


def _chrome_available() -> bool:
    try:
        launcher.find_browser(Config.from_env())
        return True
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Edge installed")
async def test_navigate_snapshot_click_fill():
    cfg = Config.from_env()
    object.__setattr__(cfg, "headless", True)
    object.__setattr__(cfg, "human_delays", False)
    mgr = BrowserManager(cfg)
    try:
        started = await mgr.start(headless=True)
        assert started["status"] in ("started", "already_running")

        await mgr.navigate(_data_url(), wait="load")
        snap = await mgr.snapshot()
        assert "textbox" in snap["snapshot"]
        assert "button" in snap["snapshot"]
        assert snap["refs"] >= 2

        # Find the textbox ref from the snapshot text.
        email_ref = _ref_for(snap["snapshot"], "textbox")

        # fill() re-snapshots internally, so refs from the pre-fill snapshot
        # (like a button ref grabbed here) are a DIFFERENT generation by the
        # time click() runs — re-read the button ref from fill's own result.
        after_fill = await mgr.fill(email_ref, "qa@example.com")
        button_ref = _ref_for(after_fill["snapshot"], "button")
        await mgr.click(button_ref)

        out = await mgr.get_text(None)
        assert "clicked:qa@example.com" in out
    finally:
        await mgr.shutdown(kill=True)


def _ref_for(snapshot: str, role: str) -> str:
    for line in snapshot.splitlines():
        line = line.strip()
        if f" {role}" in line and line.startswith("@e"):
            return line.split()[0]
    raise AssertionError(f"No ref for role {role} in:\n{snapshot}")
