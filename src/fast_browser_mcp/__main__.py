"""Entry point: run the MCP server over stdio (default) or SSE (remote/Docker).

Usage:
  stdio (Claude Code local):
      python -m fast_browser_mcp

  SSE (Docker / remote server):
      python -m fast_browser_mcp --transport sse --host 0.0.0.0 --port 8000

  SSE with API key auth:
      MCP_API_KEY=secret python -m fast_browser_mcp --transport sse
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast-Browser-MCP Navigator MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="stdio = local Claude Code; sse = Docker/remote (default: stdio)",
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "3067")))
    args = parser.parse_args()

    # Import here so tool decorators register after argparse (avoids side-effects on --help)
    from .mcp.server import mcp  # noqa: PLC0415

    if args.transport == "sse":
        _run_sse(mcp, args.host, args.port)
    else:
        mcp.run(transport="stdio")


# ---------------------------------------------------------------------------
# Pure ASGI middlewares — BaseHTTPMiddleware buffers the response body and
# breaks SSE streaming.  These intercept at the ASGI scope level instead.
# ---------------------------------------------------------------------------

class _HealthMiddleware:
    """Serve GET /health without touching the SSE stream."""

    _BODY = b'{"status":"ok"}'
    _HEADERS = [
        (b"content-type", b"application/json"),
        (b"content-length", b"15"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/health":
            await send({"type": "http.response.start", "status": 200, "headers": self._HEADERS})
            await send({"type": "http.response.body", "body": self._BODY})
            return
        await self.app(scope, receive, send)


class _ApiKeyMiddleware:
    """Reject requests missing X-API-Key header (except /health and /sse OPTIONS)."""

    _UNAUTH = b"Unauthorized"
    _UNAUTH_HEADERS = [
        (b"content-type", b"text/plain"),
        (b"content-length", b"12"),
    ]

    def __init__(self, app, api_key: str):
        self.app = app
        self._key = api_key

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        key = headers.get(b"x-api-key", b"").decode()
        if key != self._key:
            await send({"type": "http.response.start", "status": 401, "headers": self._UNAUTH_HEADERS})
            await send({"type": "http.response.body", "body": self._UNAUTH})
            return
        await self.app(scope, receive, send)


def _run_sse(mcp, host: str, port: int) -> None:
    """Run with SSE transport, optionally gated by MCP_API_KEY env var."""
    import uvicorn

    api_key = os.getenv("MCP_API_KEY", "")

    ts = getattr(mcp.settings, "transport_security", None)
    if ts is not None:
        # FastMCP's own defaults already allow 127.0.0.1/localhost/[::1] on
        # any port — host.docker.internal (how the container's host machine
        # reaches it) is just missing from that allowlist, so add it the same
        # way instead of disabling DNS rebinding protection outright (which
        # would open the SSE endpoint, default host 0.0.0.0, to rebinding
        # attacks from ANY origin, not just Docker's internal host).
        ts.allowed_hosts = list(dict.fromkeys([*ts.allowed_hosts, "host.docker.internal:*"]))
        # allowed_origins only matters for requests that DO send an Origin
        # header (absent Origin always passes, e.g. a bare SSE client).
        ts.allowed_origins = list(
            dict.fromkeys([*ts.allowed_origins, "http://host.docker.internal:*"])
        )

    app = mcp.sse_app()

    # Wrap with pure-ASGI middlewares (innermost first)
    app = _HealthMiddleware(app)
    if api_key:
        app = _ApiKeyMiddleware(app, api_key)
        print(f"Fast-Browser-MCP Navigator — SSE on http://{host}:{port}/sse (API key auth ON)")
    else:
        print(f"Fast-Browser-MCP Navigator — SSE on http://{host}:{port}/sse (no auth)")
        print("  Set MCP_API_KEY env var to enable API key protection.")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
