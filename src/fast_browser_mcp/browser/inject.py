"""Persistent page scripts: JS that survives navigation.

Every Runtime.evaluate call in this codebase runs once, in the current
document — a navigation (full reload) wipes any state it defined on
``window``. ``Page.addScriptToEvaluateOnNewDocument`` is the CDP mechanism to
re-run a script on every new document automatically, before any page script
executes, so it becomes the foundation for cross-navigation state (the
bootstrap MutationObserver) and for LLM-defined helpers that should survive
a reload without the caller having to know when one happened.
"""

from __future__ import annotations

from pathlib import Path

from ..cdp.connection import CDPConnection

BOOTSTRAP_ID = "__fbm_bootstrap__"
_BOOTSTRAP_SOURCE = (Path(__file__).resolve().parents[1] / "assets" / "bootstrap.js").read_text(
    encoding="utf-8"
)


class PersistentScripts:
    """Registry of {identifier: source} installed via addScriptToEvaluateOnNewDocument.

    Re-`install_all` after every session swap (cross-origin navigation attaches
    a NEW target/session in Chrome — CDP's per-script registration does not
    carry over, so scripts must be re-registered against the new session).
    """

    def __init__(self) -> None:
        self._scripts: dict[str, str] = {BOOTSTRAP_ID: _BOOTSTRAP_SOURCE}
        self._cdp_script_ids: dict[str, str] = {}

    def add(self, identifier: str, source: str) -> None:
        self._scripts[identifier] = source

    def remove(self, identifier: str) -> bool:
        if identifier == BOOTSTRAP_ID:
            raise ValueError("No se puede eliminar el script bootstrap del core.")
        return self._scripts.pop(identifier, None) is not None

    def list_ids(self) -> list[str]:
        return [i for i in self._scripts if i != BOOTSTRAP_ID]

    async def install_all(self, conn: CDPConnection, session_id: str | None) -> None:
        """(Re-)register every script against the current session.

        Best-effort: CDP has no "unregister by identifier" — Page.setBypassCSP
        aside, ``addScriptToEvaluateOnNewDocument`` calls stack, so calling
        this repeatedly against the SAME session_id would leak duplicate
        registrations. We only call this after a genuine session swap, when
        the previous registrations are gone with the old target anyway.
        """
        self._cdp_script_ids.clear()
        for identifier, source in self._scripts.items():
            res = await conn.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": source},
                session_id=session_id,
            )
            script_id = res.get("identifier")
            if script_id:
                self._cdp_script_ids[identifier] = script_id
