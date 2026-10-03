"""Render from the exact local spec without hydrating a mismatched SSR tree."""
from importlib import import_module
import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_api_html_self_hosts_matching_react_and_style_runtime(tmp_path):
    try:
        module = import_module("scripts.docs_ai.build_api")
    except ModuleNotFoundError:
        pytest.fail("Consistent local Redocly compiler is missing")
    if not (ROOT / "scripts/docs_ai/node_modules/.bin/redocly").exists():
        pytest.skip("Run npm ci --prefix scripts/docs_ai for the real Redocly build")
    (tmp_path / "openapi.json").write_text(json.dumps({"openapi": "3.1.0", "info": {"title": "Synthetic API", "version": "1"}, "paths": {}}))
    (tmp_path / "provenance.json").write_text(json.dumps({"source_sha": "1" * 40, "source_tree_dirty": False}))
    module.build(tmp_path)
    html = (tmp_path / "api.html").read_text()
    assert 'src="redoc.standalone.js"' in html
    assert not re.search(r'<script[^>]+src="https?://', html)
    assert "fonts.googleapis.com" not in html
    assert "Redoc.init('openapi.json'" in html
    assert "Redoc.hydrate" not in html
    assert "__redoc_state" not in html
    assert "uncommitted" not in html
    bundle = (tmp_path / "redoc.standalone.js").read_text()
    assert 'data-styled-version="5.3.11"' in bundle
    assert "18.3.1" in bundle
    for name in ("react", "react-dom"):
        assert json.loads((ROOT / "scripts/docs_ai/node_modules" / name / "package.json").read_text())["version"] == "18.3.1"
    assert (tmp_path / "redoc.standalone.js.LICENSE.txt").exists()
