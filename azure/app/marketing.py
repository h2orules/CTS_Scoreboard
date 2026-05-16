"""Public marketing pages (homepage + Terms + Privacy).

These pages live outside the ``/m/{meet_id}`` per-meet surface and are
served to any visitor to the relay's root. They're static and stateless
&mdash; no Redis lookups, no per-meet context &mdash; so the routes are
kept here rather than in :mod:`app.routes` to keep concerns separated.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def build_marketing_router(
    templates: Jinja2Templates | None = None,
) -> APIRouter:
    """Construct the public landing-page router.

    ``templates`` is optional so tests can inject a custom instance; in
    prod the default points at ``azure/app/templates/``.
    """
    if templates is None:
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    router = APIRouter()
    # Use a fixed string here (rather than e.g. datetime.now()) so the
    # rendered HTML is byte-stable across requests and friendly to CDN
    # caching. Bump manually whenever the policy text changes.
    last_updated = date(2026, 5, 16).strftime("%B %d, %Y")

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "home.html", {})

    @router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
    async def terms(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "terms.html", {"updated": last_updated}
        )

    @router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    async def privacy(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "privacy.html", {"updated": last_updated}
        )

    return router
