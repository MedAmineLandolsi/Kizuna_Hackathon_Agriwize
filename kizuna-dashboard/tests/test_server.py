"""The dashboard server serves the pages and tells them where the backend and the analyzer are."""
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from server import WEB, make_server


@pytest.fixture()
def url():
    srv = make_server(0, backend="http://10.0.0.5:8000/", vision="http://10.0.0.5:8100", host="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(u):
    with urllib.request.urlopen(u) as r:
        return r.status, r.headers, r.read().decode("utf-8")


def test_config_js_carries_the_addresses(url):
    status, headers, body = get(url + "/config.js")
    assert status == 200 and "javascript" in headers["Content-Type"]
    assert '"backend": "http://10.0.0.5:8000"' in body and '"vision": "http://10.0.0.5:8100"' in body


def test_default_config_means_same_machine():
    from server import config_js
    assert b'"backend": ""' in config_js("", "")


@pytest.mark.parametrize("page,text", [("/", "Plan de la ferme"), ("/zone.html", "Le système en direct"),
                                       ("/camera.html", "Procédure de repérage")])
def test_pages_are_served_and_load_the_config_first(url, page, text):
    status, _, body = get(url + page)
    assert status == 200 and text in body
    scripts = re.findall(r'<script src="([^"]+)"', body)
    assert scripts[:2] == ["config.js", "common.js"]


def test_every_page_links_the_three_pages():
    for name in ("index.html", "zone.html", "camera.html"):
        html = (WEB / name).read_text(encoding="utf-8")
        for href in ('href="./"', 'href="zone.html"', 'href="camera.html"'):
            assert href in html, (name, href)


def test_pages_only_call_the_backend_through_the_configured_address():
    for js in WEB.glob("*.js"):
        src = js.read_text(encoding="utf-8")
        assert "localhost:8000" not in src and "fetch('/api" not in src and 'fetch("/api' not in src, js.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_syntax():
    for js in WEB.glob("*.js"):
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (js.name, r.stderr)
