"""Browser-facing HTTP routes for the relay (Phase 4).

Serves the live scoreboard page at ``/m/{meet_id}`` and the bundled static
assets at ``/m/{meet_id}/static/{bundle_id}/{path}``. The Pi pushes the
template source + assets + initial render context via the Pi namespace; this
module renders that snapshot for any anonymous viewer who knows the meet ID.
"""
from __future__ import annotations

import base64
import re
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import BaseLoader, Environment

from app.state import MeetStateStore

# --- Static asset MIME map (kept tiny on purpose; falls back to octet-stream).
_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

# Hard limits (defense in depth).
_MAX_PATH_LEN = 256
_VALID_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")


def _content_type_for(path: str) -> str:
    dot = path.rfind(".")
    if dot < 0:
        return "application/octet-stream"
    return _MIME.get(path[dot:].lower(), "application/octet-stream")


# Replace the Pi's "io.connect('http://...:port/scoreboard')" wiring with an
# Azure-friendly form that uses the page origin and presents the meet_id auth.
_IO_CONNECT_RE = re.compile(
    r"""io\.connect\(\s*['"]http[s]?://['"]\s*\+\s*document\.domain\s*\+\s*['"]:['"]"""
    r"""\s*\+\s*location\.port\s*\+\s*['"]/scoreboard['"]\s*\)""",
    re.MULTILINE,
)


def _rewrite_io_connect(html: str, meet_id: str) -> str:
    # Force websocket-only transport. We disable HTTP long-polling so we
    # don't need sticky sessions on the load balancer — every reconnect
    # picks a worker fresh and the WS stays pinned to it for its
    # lifetime. Combined with AsyncRedisManager on the server side this
    # lets us scale horizontally across workers and replicas.
    return _IO_CONNECT_RE.sub(
        f"io('/scoreboard', {{auth: {{meet_id: {meet_id!r}}}, transports: ['websocket']}})",
        html,
    )


# Pi templates fetch HTML fragments from absolute paths like
# "/api/qualifying-info" and "/api/message-page/0". On Azure those paths
# are namespaced under /m/{meet_id}/api/... so each meet's fragments stay
# isolated. Rewrite the fetch URL strings before serving the page.
_API_PATH_RE = re.compile(r"""(['"])/api/([A-Za-z0-9_\-/]+)\1""")


def _rewrite_api_paths(html: str, meet_id: str) -> str:
    return _API_PATH_RE.sub(
        lambda m: f"{m.group(1)}/m/{meet_id}/api/{m.group(2)}{m.group(1)}",
        html,
    )


def _make_url_for(meet_id: str, bundle_id: str):
    """Return a Jinja-friendly ``url_for`` shim.

    Only ``url_for('static', filename=...)`` is supported, which is the only
    form the Pi's templates currently use. Anything else returns ``#``.
    """
    base = f"/m/{meet_id}/static/{bundle_id}/"

    def url_for(endpoint: str, **kwargs: Any) -> str:
        if endpoint == "static" and "filename" in kwargs:
            return base + str(kwargs["filename"]).lstrip("/")
        return "#"

    return url_for


def render_meet_page(
    *,
    meet_id: str,
    bundle: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """Render the cached template into HTML the browser can consume.

    ``bundle`` shape mirrors :class:`template_bundle.TemplateBundle.to_dict`.
    """
    bundle_id = str(bundle["bundle_id"])
    partials = bundle.get("partial_files") or {}
    template_text = bundle["template_text"]

    env = Environment(
        loader=BaseLoader(),
        autoescape=True,
    )
    # Map partial paths to their source so {% include 'partials/x.html' %} works.
    env.loader = _DictLoader(partials | {"__entry__": template_text})

    template = env.get_template("__entry__")
    rendered = template.render(
        url_for=_make_url_for(meet_id, bundle_id),
        meet_id=meet_id,
        **context,
    )
    return _rewrite_for_meet(rendered, meet_id)


def _rewrite_for_meet(html: str, meet_id: str) -> str:
    """Apply all per-meet HTML rewrites the browser needs."""
    return _rewrite_api_paths(_rewrite_io_connect(html, meet_id), meet_id)


class _DictLoader(BaseLoader):
    """Tiny in-memory Jinja loader keyed by template name."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get_source(self, environment, template):
        if template not in self._m:
            from jinja2 import TemplateNotFound

            raise TemplateNotFound(template)
        source = self._m[template]
        return source, None, lambda: True


# --- "no live meet" / "closed" / "unknown" small pages ---------------

# Reusable styling for the small status pages. Mirrors the visual language of
# the Pi's settings.html (system fonts, soft-grey background, rounded card,
# Bootstrap-3 alert-style colors) so the cloud-side pages don't look like a
# bare 404 next to the live scoreboard.
_STATE_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
    color: #333;
    background: #f5f7fa;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    min-height: 100vh;
}
.state-card {
    background: #fff;
    border: 1px solid #e5e5e5;
    border-radius: 6px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    max-width: 32rem;
    width: 100%;
    padding: 28px 32px 24px;
    text-align: center;
}
.state-icon {
    width: 56px; height: 56px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 14px;
    font-size: 28px;
    line-height: 1;
}
.state-icon.info    { background: #d9edf7; color: #31708f; }
.state-icon.warning { background: #fcf8e3; color: #8a6d3b; }
.state-icon.danger  { background: #f2dede; color: #a94442; }
.state-icon.success { background: #dff0d8; color: #3c763d; }
.state-card h1 {
    font-size: 20px;
    font-weight: 600;
    margin: 0 0 10px;
    color: #333;
}
.state-card p {
    font-size: 14px;
    color: #555;
    margin: 0 0 12px;
    line-height: 1.5;
}
.state-card p:last-child { margin-bottom: 0; }
.state-card .muted {
    font-size: 12px;
    color: #999;
    margin-top: 18px;
}
.state-card strong { color: #333; }
"""

# kind -> (css class, glyph). Kept ASCII / unicode-safe for simple SVG-free render.
_STATE_KINDS = {
    "info":    ("info",    "&#x24D8;"),   # circled i
    "warning": ("warning", "&#x26A0;"),   # warning sign
    "danger":  ("danger",  "&#x2715;"),   # ballot x
    "success": ("success", "&#x2713;"),   # check
}


def _state_page(
    *,
    title: str,
    body: str,
    status_code: int = 200,
    kind: str = "info",
    footer: str = "",
) -> HTMLResponse:
    icon_class, glyph = _STATE_KINDS.get(kind, _STATE_KINDS["info"])
    footer_html = f'<p class="muted">{footer}</p>' if footer else ""
    html = (
        "<!doctype html><html><head>"
        "<meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{_STATE_CSS}</style>"
        "</head><body>"
        "<div class=\"state-card\">"
        f"<div class=\"state-icon {icon_class}\" aria-hidden=\"true\">{glyph}</div>"
        f"<h1>{escape(title)}</h1>"
        f"<p>{body}</p>"
        f"{footer_html}"
        "</div></body></html>"
    )
    return HTMLResponse(html, status_code=status_code)


# ---------------------------------------------------------------------------

def build_router(*, store: MeetStateStore) -> APIRouter:
    """Construct the browser-facing router bound to a state store."""
    router = APIRouter()

    @router.get("/m/{meet_id}", response_class=HTMLResponse)
    async def meet_page(meet_id: str, request: Request) -> HTMLResponse:
        # Defensive validation: meet_ids are short and alphanumeric.
        if not meet_id.isalnum() or len(meet_id) > 64:
            return _state_page(
                title="Invalid meet ID",
                body="The link you followed is malformed.",
                status_code=400,
                kind="danger",
                footer="Double-check the URL and try again.",
            )

        meta = store.get_metadata(meet_id)
        if not meta:
            return _state_page(
                title="No meet found",
                body="That meet ID is unknown or has expired. If you scanned a "
                     "printed QR code, the host may not have set up the "
                     "scoreboard yet &mdash; try again closer to meet time, or "
                     "ask the host for an up-to-date link.",
                status_code=404,
                kind="warning",
            )
        status = meta.get("status")
        if status == "expired_id_rotated":
            return _state_page(
                title="Link expired",
                body="The host updated this meet&rsquo;s sharing link, so the "
                     "old URL no longer works. Ask the host for the new link "
                     "or QR code.",
                status_code=410,
                kind="warning",
            )
        if status == "closed":
            host = escape(meta.get("host_team_name", "")) or "the host"
            return _state_page(
                title="No meet in session",
                body=f"There&rsquo;s no live meet right now from "
                     f"<strong>{host}</strong>. The next meet will appear here "
                     "automatically &mdash; this link is good all season, so "
                     "feel free to bookmark it or save the QR code.",
                status_code=200,
                kind="info",
                footer="Check back at the next scheduled meet.",
            )

        bundle = store.get_current_template(meet_id)
        context = store.get_context(meet_id)
        if not bundle or not context:
            host = escape(meta.get("host_team_name", "")) or "the host"
            return _state_page(
                title="Connecting to the scoreboard",
                body=f"<strong>{host}</strong> is online but hasn&rsquo;t "
                     "published the first event yet. Results will appear here "
                     "automatically as soon as the meet starts &mdash; no need "
                     "to refresh.",
                status_code=503,
                kind="info",
            )

        try:
            html = render_meet_page(meet_id=meet_id, bundle=bundle, context=context)
        except Exception as exc:  # pragma: no cover - render errors are caught for safety
            return _state_page(
                title="Render error",
                body=f"Could not render the meet template: {escape(str(exc))}",
                status_code=500,
                kind="danger",
            )
        # If the meet is degraded, leave it to the in-page Socket.IO client to
        # surface the banner via the "feed_status" event we already emit.
        return HTMLResponse(html)

    @router.get("/m/{meet_id}/static/{bundle_id}/{path:path}")
    async def meet_static(meet_id: str, bundle_id: str, path: str) -> Response:
        if (
            len(path) > _MAX_PATH_LEN
            or ".." in path
            or path.startswith("/")
            or not _VALID_PATH_RE.match(path)
        ):
            return Response(status_code=400)
        if not meet_id.isalnum() or not bundle_id.isalnum():
            return Response(status_code=400)

        # Look up the cached bundle. Note: we honor the bundle_id from the URL
        # (not the current bundle), so old browsers with a cached page can keep
        # loading their pinned assets while a new bundle rolls out.
        from app.state import MeetKeys

        raw = store._r.get(MeetKeys(meet_id).template(bundle_id))
        if not raw:
            return Response(status_code=404)
        import json

        try:
            bundle = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(status_code=500)

        files = bundle.get("static_files") or {}
        b64 = files.get(path)
        if b64 is None:
            return Response(status_code=404)
        try:
            data = base64.b64decode(b64)
        except (ValueError, TypeError):
            return Response(status_code=500)
        return Response(
            data,
            media_type=_content_type_for(path),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    def _serve_fragment(meet_id: str, name: str, request: Request) -> Response:
        if not meet_id.isalnum() or len(meet_id) > 64:
            return Response(status_code=400)
        got = store.get_fragment(meet_id, name)
        if not got:
            # Match the Pi behavior: empty 200 (template treats this as "no
            # content yet" and renders nothing).
            return Response(b"", media_type="text/html; charset=utf-8")
        key, html = got
        etag = f'"{key}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304)
        return Response(
            html.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"ETag": etag, "Cache-Control": "public, max-age=60"},
        )

    @router.get("/m/{meet_id}/api/qualifying-info")
    async def meet_qualifying_info(meet_id: str, request: Request) -> Response:
        return _serve_fragment(meet_id, "qualifying_info", request)

    @router.get("/m/{meet_id}/api/message-page/{index}")
    async def meet_message_page(meet_id: str, index: int, request: Request) -> Response:
        return _serve_fragment(meet_id, f"message_page_{index}", request)

    return router
