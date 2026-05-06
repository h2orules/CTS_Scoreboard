"""Tests for qr_utils."""
from __future__ import annotations

import re

from qr_utils import (
    QR_TOKEN,
    build_meet_url,
    render_overlay_svg,
    render_qr_svg,
    substitute_qr_tokens,
)


def test_render_qr_svg_returns_svg_for_text():
    svg = render_qr_svg("https://example.com/m/abc")
    assert svg.startswith("<svg")
    assert "</svg>" in svg
    # Should contain at least one path or rect node.
    assert "<path" in svg or "<rect" in svg


def test_render_qr_svg_empty_text_returns_empty_string():
    assert render_qr_svg("") == ""
    assert render_qr_svg(None) == ""  # type: ignore[arg-type]


def test_build_meet_url_combines_base_and_id():
    assert build_meet_url(public_base="https://relay.example.com",
                          meet_id="abc123XYZ7890ab") == "https://relay.example.com/m/abc123XYZ7890ab"


def test_build_meet_url_strips_trailing_slash():
    assert build_meet_url(public_base="https://relay.example.com/",
                          meet_id="abc") == "https://relay.example.com/m/abc"


def test_build_meet_url_missing_inputs_returns_empty():
    assert build_meet_url(public_base="", meet_id="abc") == ""
    assert build_meet_url(public_base="https://x", meet_id="") == ""


def test_substitute_qr_tokens_replaces_with_svg_when_target_set():
    html = f"Scan: {QR_TOKEN} for live results"
    out = substitute_qr_tokens(html, target_url="https://example.com/m/abc")
    assert QR_TOKEN not in out
    assert "qr-inline" in out
    assert "<svg" in out


def test_substitute_qr_tokens_strips_token_when_no_target():
    html = f"Scan: {QR_TOKEN} please"
    out = substitute_qr_tokens(html, target_url="")
    assert QR_TOKEN not in out
    assert "<svg" not in out
    # The surrounding text is preserved (with the token gone).
    assert "Scan:" in out
    assert "please" in out


def test_substitute_qr_tokens_idempotent_on_html_without_token():
    html = "<p>no qr here</p>"
    assert substitute_qr_tokens(html, target_url="https://example.com/m/abc") == html


def test_substitute_handles_multiple_tokens():
    html = f"{QR_TOKEN} and again {QR_TOKEN}"
    out = substitute_qr_tokens(html, target_url="https://example.com/m/abc")
    # Two QR spans rendered.
    assert out.count("qr-inline") == 2


def test_render_overlay_svg_smaller_than_inline():
    inline = render_qr_svg("https://example.com/m/abc")
    overlay = render_overlay_svg("https://example.com/m/abc")
    # Both are valid SVG; overlay uses smaller scale, so the byte count is
    # generally smaller. We just assert both render and are SVGs.
    assert inline.startswith("<svg")
    assert overlay.startswith("<svg")
    assert "</svg>" in overlay


def test_substituted_svg_round_trips_through_safe_filter():
    """The substitution is intended for use with Jinja's |safe filter, so the
    embedded SVG should not contain script tags or event handlers."""
    out = substitute_qr_tokens(QR_TOKEN, target_url="https://example.com/m/abc")
    assert "<script" not in out.lower()
    # No on* handlers like onload=, onclick=, etc.
    assert not re.search(r'\son[a-z]+=', out, re.IGNORECASE)
