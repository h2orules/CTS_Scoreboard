"""Template bundling for the Azure relay.

Builds a self-contained bundle of the Pi's scoreboard template plus every
static asset it references, keyed by SHA-256 of the bundle contents. The Pi
pushes this bundle to Azure on connect and on changes; Azure stores it under
``meet:{id}:template:{bundle_id}`` and serves it from cache without needing to
fetch from the Pi.

Phase-3 scope:
- Discover one HTML template plus referenced static files via regex.
- Read each file as bytes; compute combined SHA-256.
- Return a JSON-serializable bundle dict.

Future enhancements (Phase 4+): full Jinja2 AST walk for partials/macros, MIME
detection, asset minification.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Final

# Matches {{ url_for('static', filename='...') }} with various quotings.
_STATIC_URL_RE: Final = re.compile(
    r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# Matches {% include 'partials/x.html' %} or {% extends '...' %}.
_INCLUDE_RE: Final = re.compile(
    r"""\{%\s*(?:include|extends)\s+['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TemplateBundle:
    """Versioned bundle of template + static assets."""

    bundle_id: str
    template_path: str
    template_text: str
    static_files: dict[str, bytes]   # logical_path -> raw bytes
    partial_files: dict[str, str]    # template_name -> source

    def to_dict(self) -> dict:
        return {
            "bundle_id": self.bundle_id,
            "template_path": self.template_path,
            "template_text": self.template_text,
            "static_files": {
                p: base64.b64encode(b).decode("ascii") for p, b in self.static_files.items()
            },
            "partial_files": dict(self.partial_files),
        }


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _hash_bundle(template_text: str, static_files: dict[str, bytes], partials: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(template_text.encode("utf-8"))
    for name in sorted(partials):
        h.update(b"\x00partial:")
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(partials[name].encode("utf-8"))
    for name in sorted(static_files):
        h.update(b"\x00static:")
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(static_files[name])
    return h.hexdigest()[:16]


def build_bundle(
    *,
    template_root: str,
    static_root: str,
    template_relpath: str,
) -> TemplateBundle:
    """Build a bundle for the named template (relative to ``template_root``).

    ``template_relpath`` example: ``"web/home.html"``.

    Walks ``{% include %}`` / ``{% extends %}`` references one level deep and
    enumerates every ``url_for('static', filename='...')`` literal. Missing
    static files are silently skipped (the browser will 404 on them).
    """
    template_full = os.path.join(template_root, template_relpath)
    template_text = _read_text(template_full)

    # --- partials ---
    partials: dict[str, str] = {}
    for m in _INCLUDE_RE.finditer(template_text):
        rel = m.group(1)
        full = os.path.join(template_root, rel)
        if os.path.exists(full):
            partials[rel] = _read_text(full)

    # --- static assets ---
    static_files: dict[str, bytes] = {}
    seen: set[str] = set()
    sources = [template_text, *partials.values()]
    for src in sources:
        for m in _STATIC_URL_RE.finditer(src):
            rel = m.group(1)
            if rel in seen:
                continue
            seen.add(rel)
            full = os.path.join(static_root, rel)
            if os.path.isfile(full):
                static_files[rel] = _read_bytes(full)

    bundle_id = _hash_bundle(template_text, static_files, partials)
    return TemplateBundle(
        bundle_id=bundle_id,
        template_path=template_relpath,
        template_text=template_text,
        static_files=static_files,
        partial_files=partials,
    )
