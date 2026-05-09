"""Tests for template_bundle."""
from __future__ import annotations

import os
import shutil

import pytest

from template_bundle import TemplateBundle, build_bundle


@pytest.fixture
def fake_tree(tmp_path):
    """Build a tiny templates/ + static/ tree."""
    troot = tmp_path / "templates"
    sroot = tmp_path / "static"
    (troot / "web").mkdir(parents=True)
    (troot / "partials").mkdir()
    sroot.mkdir()

    (troot / "web" / "home.html").write_text(
        """<!doctype html>
<link rel="stylesheet" href="{{ url_for('static', filename='css/main.css') }}">
{% include 'partials/_qt.html' %}
<img src="{{ url_for('static', filename='ad/logo.png') }}">
"""
    )
    (troot / "partials" / "_qt.html").write_text(
        "<div>{{ url_for('static', filename='js/widget.js') }}</div>"
    )
    (sroot / "css").mkdir()
    (sroot / "css" / "main.css").write_bytes(b"body {}")
    (sroot / "ad").mkdir()
    (sroot / "ad" / "logo.png").write_bytes(b"\x89PNG\r\n")
    (sroot / "js").mkdir()
    (sroot / "js" / "widget.js").write_bytes(b"console.log(1);")
    return str(troot), str(sroot)


def test_bundle_includes_template_text(fake_tree):
    troot, sroot = fake_tree
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert "<!doctype html>" in b.template_text
    assert b.template_path == "web/home.html"


def test_bundle_finds_partials(fake_tree):
    troot, sroot = fake_tree
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert "partials/_qt.html" in b.partial_files
    assert "widget.js" in b.partial_files["partials/_qt.html"]


def test_bundle_finds_static_assets_from_template_and_partials(fake_tree):
    troot, sroot = fake_tree
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert "css/main.css" in b.static_files
    assert "ad/logo.png" in b.static_files
    assert "js/widget.js" in b.static_files
    assert b.static_files["css/main.css"] == b"body {}"


def test_bundle_id_changes_when_template_changes(fake_tree):
    troot, sroot = fake_tree
    b1 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    # Mutate the template.
    with open(os.path.join(troot, "web", "home.html"), "a") as f:
        f.write("\n<!-- changed -->\n")
    b2 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert b1.bundle_id != b2.bundle_id


def test_bundle_id_changes_when_static_changes(fake_tree):
    troot, sroot = fake_tree
    b1 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    with open(os.path.join(sroot, "css", "main.css"), "ab") as f:
        f.write(b"\n.x {}")
    b2 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert b1.bundle_id != b2.bundle_id


def test_bundle_id_stable_for_same_input(fake_tree):
    troot, sroot = fake_tree
    b1 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    b2 = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert b1.bundle_id == b2.bundle_id


def test_bundle_to_dict_is_json_safe(fake_tree):
    import json

    troot, sroot = fake_tree
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    d = b.to_dict()
    json.dumps(d)  # round-trips
    assert d["bundle_id"] == b.bundle_id
    # Static files are base64.
    assert isinstance(d["static_files"]["css/main.css"], str)


def test_missing_static_is_silently_skipped(fake_tree):
    troot, sroot = fake_tree
    # Reference a non-existent file.
    with open(os.path.join(troot, "web", "home.html"), "a") as f:
        f.write("\n{{ url_for('static', filename='nope/missing.png') }}\n")
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    # Existing assets still present.
    assert "css/main.css" in b.static_files
    # Missing one is absent (no exception).
    assert "nope/missing.png" not in b.static_files


def test_bundle_against_real_repo_home_template():
    """Smoke test: real templates/web/home.html should bundle without error."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    troot = os.path.join(repo, "templates")
    sroot = os.path.join(repo, "static")
    if not os.path.exists(os.path.join(troot, "web", "home.html")):
        pytest.skip("real template not available")
    b = build_bundle(template_root=troot, static_root=sroot, template_relpath="web/home.html")
    assert b.bundle_id
    assert "<html" in b.template_text.lower()
    # It references socket.io.4.8.3.min.js as a static file.
    assert any("socket.io" in p for p in b.static_files)
