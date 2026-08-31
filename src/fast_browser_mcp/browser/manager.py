"""BrowserManager: owns the Chrome process, CDP connection, active target and refmap.

This is the long-lived 'daemon' object held by the MCP server lifespan. One persistent
CDP WebSocket survives across every tool call, eliminating per-command startup latency.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from ..actions import forms, keyboard, mouse
from ..cdp.connection import CDPConnection
from ..cdp.events import EventBuffers, NetEntry
from ..errors import (
    BadArgumentError,
    BrowserNotStartedError,
    CdpProtocolError,
    ElementGoneError,
    NavigatedDuringExecutionError,
)
from ..chrome import launcher, ws_url
from ..config import Config
from ..snapshot import collector
from ..snapshot.filter import name_of, role_of
from ..snapshot.refmap import RefEntry, RefMap
from ..snapshot.serializer import resolve_open_popup_ids
from . import waits
from .inject import PersistentScripts


_POPUP_OPTION_ROLES = frozenset(
    {"option", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem", "tab"}
)


def _find_text_match(nodes: list[dict], text: str, exact: bool) -> dict | None:
    query = text.strip().lower()
    for node in nodes:
        candidate = name_of(node).strip().lower()
        if (exact and candidate == query) or (not exact and query in candidate):
            return node
    return None


# Tier 2/3 fallback for click_popup_option when no aria-controls relationship
# exists: find the topmost visible listbox/menu/dialog (or the bootstrap's
# last-inserted popup-like container as a last resort), then click a
# text-matching item inside it via a plain JS element.click(). {query}/{exact}
# are substituted with json.dumps'd/literal values, not raw string interpolation
# of caller-controlled text without escaping.
_POPUP_CLICK_JS_TEMPLATE = """
(() => {{
  function visible(el) {{
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  }}
  let containers = Array.from(document.querySelectorAll('[role=listbox],[role=menu],[role=dialog]'))
    .filter(visible);
  containers.sort((a, b) =>
    (parseInt(getComputedStyle(b).zIndex) || 0) - (parseInt(getComputedStyle(a).zIndex) || 0)
  );
  let container = containers[0] || null;
  if (!container && window.__fbm && window.__fbm.lastInsertedContainer &&
      document.contains(window.__fbm.lastInsertedContainer)) {{
    container = window.__fbm.lastInsertedContainer;
  }}
  if (!container) return {{clicked: false, options: []}};
  const itemSel = '[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],li,button,a';
  const items = Array.from(container.querySelectorAll(itemSel)).filter(visible);
  const norm = (s) => (s || '').trim().toLowerCase();
  const query = norm({query});
  const exact = {exact};
  const target = items.find((el) => exact ? norm(el.textContent) === query : norm(el.textContent).includes(query));
  if (!target) return {{clicked: false, options: items.map((el) => el.textContent.trim())}};
  target.click();
  return {{clicked: true}};
}})()
"""


def _extract_js_exception(exception_details: dict) -> str:
    """Pull the actual JS error message out of a CDP exceptionDetails object.

    ``exceptionDetails.text`` is almost always the useless literal "Uncaught"
    — the real message (including the stack) lives on
    ``exceptionDetails.exception.description`` for thrown Error objects, or
    ``.value`` for a thrown primitive (string/number). Fall back through both
    before resorting to ``.text`` or the raw dict.
    """
    exc = exception_details.get("exception") or {}
    description = exc.get("description")
    if description:
        return str(description)
    value = exc.get("value")
    if value is not None:
        return str(value)
    text = exception_details.get("text")
    if text:
        return str(text)
    return str(exception_details)


class BrowserManager:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config.from_env()
        self._proc: subprocess.Popen | None = None
        self._conn: CDPConnection | None = None
        self._events: EventBuffers | None = None
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._child_sessions: list[str] = []
        self._refmap = RefMap()
        self._warm_ax_sessions: set[str | None] = set()
        self._injected = PersistentScripts()
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------
    async def start(self, headless: bool | None = None) -> dict:
        async with self._lock:
            if self._conn and self._conn.is_connected:
                # Verify the stored session is still alive; reattach if stale.
                try:
                    await self._current_url()
                    return {"status": "already_running", "url": await self._current_url()}
                except Exception:
                    # Session stale — fall through to full reattach below.
                    await self._conn.close()
                    self._conn = None
                    self._session_id = None
                    self._child_sessions.clear()

            if headless is not None:
                object.__setattr__(self.cfg, "headless", headless)

            port = self.cfg.debug_port
            existing = await ws_url.probe_version(port)
            if existing is None:
                if not launcher._port_is_free(port):
                    port = launcher.find_free_port(port + 1)
                self._proc = launcher.launch(self.cfg, port)
                payload = await ws_url.wait_for_version(port)
            else:
                payload = existing  # reattach to a browser already running

            self._conn = CDPConnection(ws_url.parse_ws_url(payload))
            await self._conn.connect()
            await self._attach_to_page()
            await self._enable_domains()
            self._events = EventBuffers(self._conn)
            self._events.attach()
            return {"status": "started", "port": port, "url": await self._current_url()}

    async def _attach_to_page(self) -> None:
        assert self._conn is not None
        targets = await self._conn.call("Target.getTargets")
        all_pages = [t for t in targets.get("targetInfos", []) if t.get("type") == "page"]
        # Prefer real pages over browser-internal chrome:// URLs which reject CDP commands.
        real_pages = [t for t in all_pages if not (t.get("url") or "").startswith("chrome://")]
        if real_pages:
            target_id = real_pages[0]["targetId"]
        elif all_pages:
            # Only chrome:// tabs — create a blank tab to work with.
            created = await self._conn.call("Target.createTarget", {"url": "about:blank"})
            target_id = created["targetId"]
        else:
            created = await self._conn.call("Target.createTarget", {"url": "about:blank"})
            target_id = created["targetId"]
        attached = await self._conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._session_id = attached["sessionId"]
        self._target_id = target_id
        # Auto-attach to OOPIF iframes / popups under this page.
        await self._conn.call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            session_id=self._session_id,
        )
        self._conn.on("Target.attachedToTarget", self._on_attached)
        self._conn.on("Target.detachedFromTarget", self._on_detached)
        self._conn.on("Page.javascriptDialogOpening", self._on_dialog)

    def _on_attached(self, params: dict, _sid: str | None) -> None:
        info = params.get("targetInfo", {})
        sid = params.get("sessionId")
        if not sid:
            return
        if info.get("type") == "page":
            # Chrome swapped the primary page session (e.g. cross-process
            # navigation during an OAuth/SSO redirect). The old sessionId is
            # now detached; re-point at the new one or every call 404s with
            # "Session with given id not found".
            self._session_id = sid
            self._target_id = info.get("targetId")
            asyncio.create_task(self._enable_domains())
        elif info.get("type") == "iframe":
            if sid not in self._child_sessions:
                self._child_sessions.append(sid)

    def _on_detached(self, params: dict, _sid: str | None) -> None:
        # Prune dead iframe sessions (e.g. a login-page captcha widget torn down
        # on navigation) so snapshot() stops trying to query them.
        sid = params.get("sessionId")
        if sid and sid in self._child_sessions:
            self._child_sessions.remove(sid)

    def _on_dialog(self, params: dict, sid: str | None) -> None:
        # Auto-accept JS dialogs so navigation never hangs waiting for input.
        if self._conn is not None:
            asyncio.create_task(
                self._conn.call(
                    "Page.handleJavaScriptDialog",
                    {"accept": True},
                    session_id=sid or self._session_id,
                )
            )

    async def _enable_domains(self) -> None:
        assert self._conn is not None
        for domain in ("Page", "DOM", "Runtime", "Network", "Log", "Accessibility"):
            await self._conn.call(f"{domain}.enable", session_id=self._session_id)
        # Registrations from Page.addScriptToEvaluateOnNewDocument are scoped
        # to the session/target they were made on; a cross-origin nav that
        # swaps _session_id (see _on_attached) leaves the OLD registrations
        # behind with the dead target, so every (re)enable reinstalls them.
        await self._injected.install_all(self._conn, self._session_id)

    async def shutdown(self, kill: bool = False) -> dict:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        if kill and self._proc is not None:
            self._proc.terminate()
            self._proc = None
        return {"status": "stopped", "killed": kill}

    # --- navigation + snapshot ----------------------------------------------
    async def _read_fbm_marker(self) -> float | None:
        """Read window.__fbm.installedAt (the Date.now() timestamp of when the
        bootstrap script last ran), or None if it hasn't run at all (blocked
        by CSP, about:blank).

        This — not the persisted `epoch` counter — is what detects an actual
        reload: a genuine navigation always creates a brand-new `window`, so
        the bootstrap re-runs and installedAt changes, unconditionally. The
        `epoch` counter round-trips through localStorage instead, which is
        the right signal for "how many reloads has this ORIGIN seen" but is
        unreliable here: an opaque origin (e.g. a `data:` URL, a sandboxed
        iframe) has no persistent storage across navigations, so epoch alone
        would misreport a genuine reload as a same-document transition.
        """
        try:
            res = await self._conn.call(
                "Runtime.evaluate",
                {
                    "expression": "window.__fbm ? window.__fbm.installedAt : null",
                    "returnByValue": True,
                },
                session_id=self._session_id,
            )
            return (res.get("result") or {}).get("value")
        except Exception:
            return None

    async def navigate(self, url: str, wait: str = "load") -> dict:
        """Navigate to url. Returns {"reloaded": bool} — True if the target
        document was actually torn down and recreated (the normal case for
        Page.navigate: any window state / injected helpers the LLM defined
        are gone), False for a same-document navigation (e.g. a hash change)
        where the existing window survived. Unknown when the bootstrap script
        couldn't run (about:blank, strict CSP) — reported as reloaded=True
        since that's the safe assumption for "did I lose my state".
        """
        conn = self._require_conn()
        self._warm_ax_sessions.clear()
        marker_before = await self._read_fbm_marker()
        try:
            await conn.call("Page.navigate", {"url": url}, session_id=self._session_id)
        except CdpProtocolError as exc:
            if "not found" in exc.message.lower() or "session" in exc.message.lower():
                # Session went stale (e.g. Chrome closed/replaced the tab). Reattach.
                await self._attach_to_page()
                await self._enable_domains()
                await conn.call("Page.navigate", {"url": url}, session_id=self._session_id)
            else:
                raise
        if wait == "load":
            await waits.wait_load(conn, lambda: self._session_id)
        elif wait == "networkidle":
            await waits.wait_networkidle(conn, self._session_id)
        marker_after = await self._read_fbm_marker()
        reloaded = marker_before is None or marker_after is None or marker_after != marker_before
        return {"reloaded": reloaded}

    async def snapshot(self, interactive_only: bool = True) -> dict:
        conn = self._require_conn()
        text, snap_id = await collector.capture(
            conn,
            self._refmap,
            self._session_id,
            child_sessions=self._child_sessions,
            interactive_only=interactive_only,
            warm_sessions=self._warm_ax_sessions,
        )
        return {"snapshot_id": snap_id, "snapshot": text, "refs": len(self._refmap)}

    async def wait_for(self, text: str | None, timeout_ms: int) -> dict:
        conn = self._require_conn()
        if text is not None:
            ok = await waits.wait_for_text(conn, text, lambda: self._session_id, timeout_ms)
            return {"ok": ok, "waited_for": text}
        return {"ok": True}

    # --- actions (all re-snapshot at the end to keep refs fresh) -------------
    async def click(self, ref: str) -> dict:
        conn = self._require_conn()
        await mouse.click(conn, self._refmap.resolve(ref), human=self.cfg.human_delays)
        return await self.snapshot()

    async def js_click(self, ref: str) -> dict:
        """JS element.click() — bypasses CDP mouse events, reliable for React SPAs."""
        conn = self._require_conn()
        await mouse.js_click(conn, self._refmap.resolve(ref))
        return await self.snapshot()

    async def hover(self, ref: str) -> dict:
        conn = self._require_conn()
        await mouse.hover(conn, self._refmap.resolve(ref))
        return await self.snapshot()

    async def fill(self, ref: str, text: str, submit: bool = False) -> dict:
        conn = self._require_conn()
        await forms.fill(
            conn, self._refmap.resolve(ref), text,
            human=self.cfg.human_delays, submit=submit,
        )
        return await self.snapshot()

    async def select_option(self, ref: str, value: str | None = None, label: str | None = None) -> dict:
        conn = self._require_conn()
        await forms.select_option(conn, self._refmap.resolve(ref), value=value, label=label)
        return await self.snapshot()

    async def press_key(self, key: str, ref: str | None) -> dict:
        conn = self._require_conn()
        sid = self._refmap.resolve(ref).session_id if ref else self._session_id
        await keyboard.press_key(conn, key, sid)
        return await self.snapshot()

    # --- js / cdp escape hatches ---------------------------------------------
    async def js_eval(
        self, script: str, ref: str | None = None, timeout_ms: int | None = None
    ) -> dict:
        """Execute arbitrary JS in the page context.

        ref=None  → Runtime.evaluate(script)          (expression mode)
        ref=@eN   → Runtime.callFunctionOn(script)     (function bound to element as `this`)

        Returns {"js_result": <value>, "exception": <msg|None>,
        "snapshot_error": <msg|None>} + snapshot fields.
        awaitPromise=True so async scripts work; returnByValue serialises primitives/objects.

        A TIMEOUT/NAVIGATED_DURING_EXECUTION from the eval call itself still
        propagates (there is no partial js_result to salvage in that case).
        But once the eval call has returned, the closing snapshot() is never
        allowed to swallow that result: if the script navigated the page and
        the snapshot fails, js_result/exception are still returned alongside a
        snapshot_error, instead of the whole call raising and the caller
        being unable to tell whether the script itself ran.
        """
        conn = self._require_conn()
        exception = None
        call_kwargs = {"timeout": timeout_ms / 1000.0} if timeout_ms else {}

        if ref is None:
            res = await conn.call(
                "Runtime.evaluate",
                {"expression": script, "returnByValue": True, "awaitPromise": True},
                session_id=self._session_id,
                **call_kwargs,
            )
            value = (res.get("result") or {}).get("value")
            if res.get("exceptionDetails"):
                exception = _extract_js_exception(res["exceptionDetails"])
        else:
            entry = self._refmap.resolve(ref)
            obj = await conn.call(
                "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
                session_id=entry.session_id,
            )
            object_id = (obj.get("object") or {}).get("objectId")
            if object_id is None:
                raise ElementGoneError(
                    f"{ref} ya no resuelve a un nodo vivo (backend_node_id="
                    f"{entry.backend_node_id}). Vuelve a llamar snapshot."
                )
            res = await conn.call(
                "Runtime.callFunctionOn",
                {"objectId": object_id, "functionDeclaration": script,
                 "returnByValue": True, "awaitPromise": True},
                session_id=entry.session_id,
                **call_kwargs,
            )
            value = (res.get("result") or {}).get("value")
            if res.get("exceptionDetails"):
                exception = _extract_js_exception(res["exceptionDetails"])

        snapshot_error = None
        snap = None
        try:
            snap = await self.snapshot()
        except (NavigatedDuringExecutionError, CdpProtocolError) as exc:
            snapshot_error = str(exc)

        if snap is None:
            return {
                "js_result": value, "exception": exception, "snapshot_error": snapshot_error,
                "snapshot_id": self._refmap.snapshot_id, "snapshot": "", "refs": 0,
            }
        return {
            "js_result": value, "exception": exception, "snapshot_error": snapshot_error,
            "snapshot_id": snap["snapshot_id"], "snapshot": snap["snapshot"], "refs": snap["refs"],
        }

    async def cdp_call(self, method: str, params: dict | None = None, use_session: bool = True) -> dict:
        """Pass-through for any CDP domain.method call.

        use_session=True (default) routes through the active page session so DOM/Runtime
        commands reach the current page. use_session=False sends at browser level
        (useful for Target.*, Browser.* methods).
        """
        conn = self._require_conn()
        sid = self._session_id if use_session else None
        result = await conn.call(method, params or {}, session_id=sid)
        return {"cdp_result": result}

    async def scroll(self, ref: str | None = None, x: int = 0, y: int = 400) -> dict:
        """Scroll the page or a specific element.

        ref=None → window.scrollBy(x, y)
        ref=@eN  → element.scrollBy(x, y) then scrollIntoView fallback
        y>0 scrolls down, y<0 up; x>0 right, x<0 left.
        """
        conn = self._require_conn()
        if ref is None:
            await conn.call(
                "Runtime.evaluate",
                {"expression": f"window.scrollBy({x},{y})", "returnByValue": True},
                session_id=self._session_id,
            )
        else:
            entry = self._refmap.resolve(ref)
            obj = await conn.call(
                "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
                session_id=entry.session_id,
            )
            oid = (obj.get("object") or {}).get("objectId")
            if oid:
                await conn.call(
                    "Runtime.callFunctionOn",
                    {"objectId": oid,
                     "functionDeclaration": f"function(){{this.scrollIntoView({{block:'center'}});this.scrollBy({x},{y});}}",
                     "returnByValue": True},
                    session_id=entry.session_id,
                )
        return await self.snapshot()

    async def set_value(self, ref: str, value: str) -> dict:
        """Set the value of any input using the native HTMLInputElement setter.

        Bypasses React/Vue/Angular's synthetic event system and triggers real
        'input' + 'change' events — required for controlled inputs in SPAs.
        Equivalent to what jQuery's .val() used to do but React-compatible.
        """
        conn = self._require_conn()
        entry = self._refmap.resolve(ref)
        obj = await conn.call(
            "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id,
        )
        oid = (obj.get("object") or {}).get("objectId")
        if oid:
            await conn.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": oid,
                    "functionDeclaration": (
                        "function(v){"
                        " const proto=this.tagName==='SELECT'"
                        "   ? window.HTMLSelectElement.prototype"
                        "   : window.HTMLInputElement.prototype;"
                        " const setter=Object.getOwnPropertyDescriptor(proto,'value').set;"
                        " setter.call(this,v);"
                        " this.dispatchEvent(new Event('input',{bubbles:true}));"
                        " this.dispatchEvent(new Event('change',{bubbles:true}));"
                        "}"
                    ),
                    "arguments": [{"value": str(value)}],
                    "returnByValue": True,
                },
                session_id=entry.session_id,
            )
        return await self.snapshot()

    # --- reads ---------------------------------------------------------------
    async def get_text(self, ref: str | None) -> str:
        conn = self._require_conn()
        if ref is None:
            res = await conn.call(
                "Runtime.evaluate",
                {"expression": "document.body ? document.body.innerText : ''",
                 "returnByValue": True},
                session_id=self._session_id,
            )
            return (res.get("result") or {}).get("value") or ""
        entry = self._refmap.resolve(ref)
        obj = await conn.call(
            "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id,
        )
        object_id = (obj.get("object") or {}).get("objectId")
        res = await conn.call(
            "Runtime.callFunctionOn",
            {"objectId": object_id,
             "functionDeclaration": "function(){return this.innerText||this.value||'';}",
             "returnByValue": True},
            session_id=entry.session_id,
        )
        return (res.get("result") or {}).get("value") or ""

    async def screenshot(self, full_page: bool = False) -> str:
        conn = self._require_conn()
        params = {"format": "png", "captureBeyondViewport": full_page}
        res = await conn.call("Page.captureScreenshot", params, session_id=self._session_id)
        return res.get("data", "")

    def read_console(
        self, clear: bool = False, level: str | None = None, since_ms: int | None = None
    ) -> list[str]:
        return self._events.read_console(clear, level=level, since_ms=since_ms) if self._events else []

    def read_network(self, filter_substr: str | None, clear: bool = False) -> list[NetEntry]:
        return self._events.read_network(filter_substr, clear) if self._events else []

    async def current_url(self) -> dict:
        return {"url": await self._current_url(), "title": await self._title()}

    # --- persistent (survives-navigation) helpers ----------------------------
    async def inject_persistent(self, script: str, identifier: str) -> dict:
        """Register `script` to re-run on every future document via
        Page.addScriptToEvaluateOnNewDocument, then install it immediately in
        the current document too (the registration only affects FUTURE
        documents, not the one already loaded)."""
        conn = self._require_conn()
        self._injected.add(identifier, script)
        await self._injected.install_all(conn, self._session_id)
        try:
            await conn.call(
                "Runtime.evaluate", {"expression": script}, session_id=self._session_id
            )
        except Exception:
            pass  # best-effort in the CURRENT document; it's live from the next one on
        return {"identifier": identifier}

    def list_persistent(self) -> list[str]:
        return self._injected.list_ids()

    def remove_persistent(self, identifier: str) -> bool:
        return self._injected.remove(identifier)

    # --- popup resolution ------------------------------------------------------
    async def click_popup_option(self, text: str, exact: bool = False) -> dict:
        """Resolve the CURRENTLY OPEN dropdown/menu/listbox and click the option
        matching `text`, in one call — no separate "read snapshot, guess which
        @eN is the real popup vs. a same-text background element" round-trip.

        Tier 1 (preferred): the AX tree's own aria-expanded/aria-controls
        relationship — same resolution the snapshot annotation uses, so if a
        line was marked [POPUP-ABIERTO] this will click inside exactly that
        subtree via a real CDP mouse click.
        Tier 2/3 fallback (sites with no aria-controls wiring): a JS-side
        search for the topmost visible [role=listbox|menu|dialog], falling
        back to the bootstrap's last-inserted popup-like container, then a
        JS-side element.click() inside it — this ONE extra CDP round-trip is
        only paid when tier 1 finds nothing, not on every snapshot.
        """
        conn = self._require_conn()
        ax = await conn.call("Accessibility.getFullAXTree", session_id=self._session_id)
        nodes = ax.get("nodes", [])
        by_id = {n["nodeId"]: n for n in nodes}
        popup_root_ids = resolve_open_popup_ids(nodes, by_id)

        candidates: list[dict] = []

        def collect(node_id: str) -> None:
            node = by_id.get(node_id)
            if node is None:
                return
            if role_of(node) in _POPUP_OPTION_ROLES:
                candidates.append(node)
            for cid in node.get("childIds") or []:
                collect(cid)

        for root_id in popup_root_ids:
            collect(root_id)

        target = _find_text_match(candidates, text, exact)
        if target is not None:
            entry = RefEntry(
                ref="@popup",
                backend_node_id=target["backendDOMNodeId"],
                session_id=self._session_id,
                role=role_of(target),
                name=name_of(target),
            )
            await mouse.click(conn, entry, human=self.cfg.human_delays)
            return await self.snapshot()

        js_result = await self._click_popup_option_js(text, exact)
        if js_result.get("clicked"):
            return await self.snapshot()

        available = js_result.get("options")
        if available is None:
            available = [name_of(n) for n in candidates]
        raise BadArgumentError(
            f"Ninguna opcion de popup coincide con {text!r}. Opciones visibles: {available}"
        )

    async def _click_popup_option_js(self, text: str, exact: bool) -> dict:
        script = _POPUP_CLICK_JS_TEMPLATE.format(
            query=json.dumps(text), exact="true" if exact else "false"
        )
        res = await self._require_conn().call(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": True},
            session_id=self._session_id,
        )
        return (res.get("result") or {}).get("value") or {}

    # --- helpers -------------------------------------------------------------
    def _require_conn(self) -> CDPConnection:
        if self._conn is None or not self._conn.is_connected:
            raise BrowserNotStartedError("Llama browser_start primero.")
        return self._conn

    async def _current_url(self) -> str:
        if self._conn is None:
            return ""
        res = await self._conn.call(
            "Runtime.evaluate",
            {"expression": "location.href", "returnByValue": True},
            session_id=self._session_id,
        )
        return (res.get("result") or {}).get("value") or ""

    async def _title(self) -> str:
        if self._conn is None:
            return ""
        res = await self._conn.call(
            "Runtime.evaluate",
            {"expression": "document.title", "returnByValue": True},
            session_id=self._session_id,
        )
        return (res.get("result") or {}).get("value") or ""
