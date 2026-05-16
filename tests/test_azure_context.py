"""Pi-side cross-cutting test (Phase 9).

Verifies the Azure context provider returns a JSON-serializable dict with
the expected browser-safe defaults so the Azure relay can hand it straight
to the front end.
"""
from __future__ import annotations

import json

from CTS_Scoreboard import _azure_context_provider, app


def test_azure_context_provider_returns_json_safe_dict():
    with app.app_context():
        ctx = _azure_context_provider()
    assert ctx is not None, "expected build_render_context to succeed at import time"

    # Browser-safe defaults are enforced.
    assert ctx["is_dev_mode"] is False
    assert ctx["serving_context"] == "azure"
    assert ctx["test_background"] is False
    assert ctx["test_event"] is None
    assert ctx["test_heat"] is None

    # Round-trips through JSON for relay transport.
    json.dumps(ctx, default=str)


def test_azure_context_provider_includes_qr_fields():
    with app.app_context():
        ctx = _azure_context_provider()
    assert ctx is not None
    # Always-present so the template can render without conditional guards
    # on missing keys.
    assert "initial_qr_overlay_svg" in ctx
    assert "initial_qr_overlay_corner" in ctx


def test_azure_context_provider_suppresses_qr_overlay():
    """Public Azure-served clients must never see the QR overlay; the QR
    code only makes sense on the LAN scoreboard pointing viewers AT the
    Azure-published URL."""
    with app.app_context():
        ctx = _azure_context_provider()
    assert ctx is not None
    assert ctx["initial_qr_overlay_svg"] == ""
    assert ctx.get("initial_qr_overlay_visibility") == "off"
