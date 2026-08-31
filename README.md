# Fast-Browser-MCP (Fast-Browser-MCP Navigator)

Ultra-fast browser automation server over Chrome DevTools Protocol (CDP), exposed as a Model Context Protocol (MCP) server.

No screenshots. No Playwright relay. Direct CDP WebSocket — 20–50× fewer tokens, ~10ms per action.

> **Note**: The core Python package is named `fast_browser_mcp` internally.

## What it does

Controls a real Chrome browser from any AI agent that supports MCP. The agent receives a compact accessibility-tree snapshot with `@eN` references after every action — no pixels, no heavy HTML blobs.

```
Agent  ──MCP──►  Fast-Browser-MCP Server  ──CDP──►  Chrome
```

---

## Quick Start — Docker (Recommended)

```bash
git clone https://github.com/ingjohnfigueroablanco/Fast-browser-MCP.git
cd Fast-browser-MCP
cp .env.example .env          # optionally set MCP_API_KEY
docker compose up -d
```

Server ready at `http://localhost:3067/sse`.

## Quick Start — Local (No Docker)

```bash
git clone https://github.com/ingjohnfigueroablanco/Fast-browser-MCP.git
cd Fast-browser-MCP
python -m venv .venv 
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
python -m fast_browser_mcp  # stdio mode
```

---

## Connecting from Claude Code

### Option A — Local subprocess / stdio

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "fast-browser-mcp": {
      "command": "python",
      "args": ["-m", "fast_browser_mcp"]
    }
  }
}
```

### Option B — Docker / SSE

```json
{
  "mcpServers": {
    "fast-browser-mcp": {
      "type": "sse",
      "url": "http://localhost:3067/sse",
      "headers": { "X-API-Key": "your-key" }
    }
  }
}
```

---

## Connecting from other agents / frameworks

### Python agent (mcp SDK)

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://localhost:3067/sse",
                      headers={"X-API-Key": "your-key"}) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("navigate", {"url": "https://example.com"})
        print(result.content[0].text)
```

---

## Testing Localhost Apps (Docker)

The browser runs **inside the container**. `localhost` inside Docker ≠ your dev machine.

| Approach | How |
|---|---|
| **Docker Desktop** | Use `http://host.docker.internal:3000` instead of `localhost:3000` |
| **ngrok** | `ngrok http 3000` → gives a public URL the container can reach |
| **Local mode** | (stdio) Chrome runs on your machine — `localhost` works normally |

---

## Available Tools

| Tool | Description |
|---|---|
| `browser_start` | Launch / reconnect Chrome |
| `browser_stop` | Close Chrome |
| `navigate` | Go to URL, wait for load / networkidle |
| `snapshot` | Accessibility tree as compact text with `@eN` refs |
| `click` | Human-like click by coordinates |
| `js_click` | `element.click()` — reliable for React / Angular SPAs |
| `fill` | Clear + type text in an input |
| `press_key` | Key press (Enter, Tab, Escape, ArrowDown…) |
| `select_option` | Select native `<select>` by value or label |
| `hover` | Mouse hover (menus, tooltips) |
| `set_value` | React/Vue/Angular-safe input setter via native JS |
| `scroll` | Scroll page or element into view |
| `js_eval` | **Run any JavaScript** — drag, events, async fetch, bulk loops |
| `js_eval_loop` | **Bulk operations** — run a JS snippet once per item |
| `cdp_call` | **Raw CDP protocol** — file upload, device emulation, network intercept |
| `get_text` | innerText of element or full page |
| `wait_for` | Wait until text appears on page |
| `read_console` | JS console logs (filter by `level`/`since_ms`) |
| `read_network` | Network requests / responses |
| `screenshot` | PNG base64 (escape hatch) |
| `current_url` | Current URL + title |
| `inject_persistent` | Register a JS helper that survives `navigate()` (a full reload otherwise wipes `window`) |
| `list_persistent` | List identifiers of registered persistent scripts |
| `remove_persistent` | Remove a persistent script by identifier |
| `click_popup_option` | Resolve the currently-open dropdown/menu/listbox and click the matching option, in one call |
| `act_and_observe` | Act, then watch the DOM/console for `watch_ms` — one call instead of racing a toast's lifetime |

### Error format

Every tool catches its own failures and returns `ERROR code=<CODE> msg=<detail>`
instead of raising — `code` is one of `TIMEOUT`, `CONNECTION_LOST`,
`NAVIGATED_DURING_EXECUTION`, `JS_EXCEPTION`, `STALE_REF`,
`ELEMENT_NOT_VISIBLE`, `ELEMENT_GONE`, `BROWSER_NOT_STARTED`, `CDP_ERROR`,
`BAD_ARGUMENT`. A `TIMEOUT` never comes back as a blank message.

### `@eN` refs are single-use per snapshot

Every action tool returns a fresh snapshot with new `@eN` refs — always use
the refs from the MOST RECENT snapshot. A ref from an older snapshot raises
`STALE_REF` rather than silently resolving to a different element.

### `navigate` always destroys `window` state

`navigate` is a full page load (like typing a URL and pressing Enter) — any
variable/function you defined via `js_eval` is gone afterward. The tool
reports `reloaded=true|false` on every call so you know when this happened;
use `inject_persistent` for helpers that need to survive it.

### Raw text alongside the accessible name

A control's accessible name (what `snapshot` shows in `"..."`) is the browser's
computed fusion of its `<label>`/`aria-label`/`aria-labelledby` — not
necessarily what a plain DOM read of the element shows. When they differ,
`snapshot` prints both: `button "CONDUCTOR" (txt: "Selecciona un conductor")`.
No extra CDP calls — the raw text comes from `StaticText` descendants already
present in the accessibility tree.

### Popup / dropdown resolution

Any dropdown, combobox, or menu that renders as a child of `<body>` (React
portals, Vue teleports, a plain absolutely-positioned `<div>`) can end up
sharing the DOM with unrelated background content that happens to have the
same role/text — a naive "find the option with this text" click can land on
the wrong element with no visible error. `snapshot` now marks the subtree of
the currently-open popup with `[POPUP-ABIERTO]` (resolved from
`aria-expanded`/`aria-controls`, zero extra CDP calls — it's already in the
accessibility tree every snapshot fetches). Prefer `click_popup_option(text)`
over clicking a raw `@eN` for a dropdown option: it resolves the open popup
the same way and clicks inside exactly that subtree in one call, falling back
to a JS-based visible-container search when the site has no ARIA wiring.

### Transient UI feedback (toasts, inline errors) — `act_and_observe`

A toast/snackbar/inline validation message can appear and disappear entirely
within ~2.5-4s — shorter than the round-trip between one tool call that acts
and a separate one that reads the result. `act_and_observe(action, ref=...,
watch_ms=3000)` performs the action and watches the DOM (via the bootstrap's
already-running MutationObserver — no installation delay) and the console
buffer for `watch_ms`, returning a timeline of what appeared/disappeared and
when, plus any new console output, in one call. For a suspected error after
any action, check `console_delta` (or `read_console`) before the DOM — an
error toast fades in seconds, but the `console.error` it usually also fires
persists in the buffer far longer.

### `click` vs `js_click`

`click` dispatches real CDP mouse events only (matches what a user click
fires, including React 17+ delegated handlers) — it does **not** also fire a
JS `element.click()`, which would double-trigger the handler. Use `js_click`
explicitly when CDP mouse events genuinely don't reach a handler.

---

## Performance & Bulk Operations

### The bottleneck is LLM round-trips, not the browser

Each tool call costs one full LLM inference + HTTP round-trip. The browser executes CDP in ~10ms.

| Pattern | Tool calls | Typical wall time |
|---|---|---|
| 20 × (click + fill + click) | 60 | ~10 min |
| 1 × js_eval_loop with 20 items | 1 | ~15 sec |

### Rule: for N > 5 repetitions, use js_eval_loop

```python
# GOOD — 1 tool call for 20 users
js_eval_loop(
    items=users,
    script="""
      document.querySelector('.agregar').click();
      await new Promise(r => setTimeout(r, 400));
      // ... fill logic ...
      document.querySelector('.crear').click();
      return item.user;
    """,
    delay_ms=300,
)
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` (local) or `sse` (Docker/remote) |
| `MCP_HOST` | `0.0.0.0` | SSE bind address |
| `MCP_PORT` | `3067` | SSE port |
| `MCP_API_KEY` | *(empty)* | API key header; empty = no auth |
| `CHROME_PATH` | auto | Explicit path to chrome.exe |
| `CHROME_EXTRA_ARGS` | *(empty)* | Extra Chrome flags |
| `Fast-Browser-MCP_HEADLESS` | `0` | `1` for headless mode |
| `Fast-Browser-MCP_HUMAN_DELAYS`| `1` | `0` removes human-like delays (faster) |

---

## Architecture

```
mcp/         FastMCP server + tool definitions
browser/     BrowserManager — lifecycle, navigate, snapshot, waits
snapshot/    AX tree → compact text with @eN refs (no screenshots)
actions/     mouse, keyboard, forms (ref → coordinates)
cdp/         Raw CDP WebSocket connection + event buffers
chrome/      Chrome launcher, port scan, reattach
```

**Why it's faster than Playwright:**
- No Node.js relay — Python speaks CDP directly.
- Chrome stays alive between calls (daemon) — 0 ms startup per tool call.
- Snapshot = compact AX text, not full YAML or heavy screenshots.
- `@eN` refs = stable `backendNodeId` — no DOM re-query per action.
