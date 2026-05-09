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
    return _rewrite_io_connect(rendered, meet_id)


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

def _state_page(*, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><html><head>"
        "<meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>body{font-family:system-ui,sans-serif;max-width:40rem;"
        "margin:4rem auto;padding:0 1rem;color:#111}"
        "h1{font-size:1.5rem}</style>"
        f"</head><body><h1>{escape(title)}</h1>"
        f"<p>{body}</p></body></html>"
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
            )

        meta = store.get_metadata(meet_id)
        if not meta:
            return _state_page(
                title="No meet found",
                body="That meet ID is unknown or has expired. Ask the host for "
                     "an up-to-date link.",
                status_code=404,
            )
        status = meta.get("status")
        if status == "expired_id_rotated":
            return _state_page(
                title="Link expired",
                body="The host rotated the meet's sharing link. Ask them for "
                     "the new one.",
                status_code=410,
            )
        if status == "closed":
            return _state_page(
                title="Meet closed",
                body=f"<strong>{escape(meta.get('host_team_name', ''))}</strong>'s "
                     "meet has finished. Final results may be available from the host.",
                status_code=200,
            )

        bundle = store.get_current_template(meet_id)
        context = store.get_context(meet_id)
        if not bundle or not context:
            return _state_page(
                title="Meet starting up",
                body="The host's scoreboard hasn't finished publishing yet. "
                     "Refresh in a moment.",
                status_code=503,
            )

        try:
            html = render_meet_page(meet_id=meet_id, bundle=bundle, context=context)
        except Exception as exc:  # pragma: no cover - render errors are caught for safety
            return _state_page(
                title="Render error",
                body=f"Could not render the meet template: {escape(str(exc))}",
                status_code=500,
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

    return router
