"""QR-code helper for the scoreboard.

Wraps :mod:`segno` to produce inline SVG (no PIL required) at a couple of
sizes used by the project. Outputs are deliberately minimal SVG strings that
can be embedded directly in HTML.

Two usages:

- Inline ``[[QR]]`` token substitution inside message-page markdown.
- An optional small corner overlay shown on top of the live scoreboard.

The QR target URL is built from the Azure relay URL plus the active meet ID;
when either is missing, we render nothing (the caller should treat that as
"feature disabled").
"""
from __future__ import annotations

import io
from typing import Final

import segno

# Token recognised in user-authored markdown. Case-sensitive on purpose so
# casual prose containing "qr" doesn't accidentally trigger substitution.
QR_TOKEN: Final = "[[QR]]"

# Default rendering knobs. ``scale`` is the segno scale (each module = N px);
# ``border`` is in modules.
_DEFAULT_INLINE_SCALE = 6
_DEFAULT_OVERLAY_SCALE = 4
_DEFAULT_BORDER = 2


def render_qr_svg(
    text: str,
    *,
    scale: int = _DEFAULT_INLINE_SCALE,
    border: int = _DEFAULT_BORDER,
    dark: str = "#000",
    light: str | None = None,
) -> str:
    """Return a self-contained inline SVG string for ``text``.

    Returns the empty string if ``text`` is empty (so callers can treat
    "missing target URL" as a no-op).
    """
    if not text:
        return ""
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(
        buf,
        kind="svg",
        scale=scale,
        border=border,
        dark=dark,
        light=light,
        xmldecl=False,
        svgns=True,
        omitsize=False,
    )
    svg = buf.getvalue().decode("utf-8")
    # segno emits explicit width/height but no viewBox, so when CSS resizes
    # the <svg> the inner path (which uses absolute module coordinates) stays
    # anchored to its native pixel size and ends up in the upper-left corner.
    # Inject a matching viewBox so the QR scales to fill its rendered box.
    if "viewBox" not in svg:
        modules, _ = qr.symbol_size(scale=scale, border=border)
        svg = svg.replace(
            "<svg ",
            f'<svg viewBox="0 0 {modules} {modules}" preserveAspectRatio="xMidYMid meet" ',
            1,
        )
    return svg


def build_meet_url(*, public_base: str, meet_id: str) -> str:
    """Compose the public per-meet URL used for QR targets.

    Returns "" if either input is missing/empty.
    """
    base = (public_base or "").rstrip("/")
    if not base or not meet_id:
        return ""
    return f"{base}/m/{meet_id}"


def substitute_qr_tokens(
    html: str,
    *,
    target_url: str,
    scale: int = _DEFAULT_INLINE_SCALE,
) -> str:
    """Replace every ``[[QR]]`` occurrence in ``html`` with an inline QR SVG.

    If ``target_url`` is empty, the token is replaced with an empty string so
    user-authored placeholders disappear instead of leaking through.
    """
    if QR_TOKEN not in html:
        return html
    svg = render_qr_svg(target_url, scale=scale) if target_url else ""
    wrapped = f'<span class="qr-inline">{svg}</span>' if svg else ""
    return html.replace(QR_TOKEN, wrapped)


def render_overlay_svg(target_url: str) -> str:
    """Render the small corner overlay QR (smaller scale, transparent bg).

    Uses a zero-module border because the on-screen overlay container
    already provides a generous white quiet zone via CSS padding; baking
    additional whitespace into the SVG just shrinks the visible modules.
    """
    return render_qr_svg(
        target_url, scale=_DEFAULT_OVERLAY_SCALE, border=0, light=None
    )


def render_qr_png(
    target_url: str,
    *,
    target_px: int = 1000,
    border: int = _DEFAULT_BORDER,
) -> bytes:
    """Render a QR code as PNG bytes, sized to roughly ``target_px`` pixels.

    Used for the "Download QR" button, which expects a 4" × 4" image at
    250 dpi (1000 × 1000 px). Segno emits PNGs whose pixel dimensions are
    ``(modules + 2 * border) * scale``, so the scale is chosen so the result
    is the largest size that does not exceed ``target_px``. Returns ``b""``
    when ``target_url`` is empty.
    """
    if not target_url:
        return b""
    qr = segno.make(target_url, error="m")
    # qr.symbol_size returns (px, px) for a given scale+border. Compute the
    # largest integer scale where the image fits within target_px.
    modules = qr.symbol_size(scale=1, border=border)[0]  # = modules + 2*border
    scale = max(1, target_px // modules)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border, dark="#000", light="#fff")
    return buf.getvalue()
