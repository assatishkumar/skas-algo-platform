"""The /mobile static mount: the mobile companion webapp is served from the SAME origin as
the API (a phone browser over Tailscale needs no native shell), it never shadows the
desktop SPA's catch-all, and a missing build is a quiet skip — not an error."""

from __future__ import annotations

from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.config import get_settings


def _dists(tmp_path):
    desktop = tmp_path / "web-dist"
    (desktop / "assets").mkdir(parents=True)
    (desktop / "index.html").write_text("<html>DESKTOP</html>")
    mobile = tmp_path / "mobile-dist"
    mobile.mkdir()
    (mobile / "index.html").write_text("<html>MOBILE</html>")
    return desktop, mobile


def test_mobile_mount_serves_alongside_desktop_spa(tmp_path, monkeypatch):
    desktop, mobile = _dists(tmp_path)
    s = get_settings()
    monkeypatch.setattr(s, "serve_webapp", True)
    monkeypatch.setattr(s, "webapp_dist", str(desktop))
    monkeypatch.setattr(s, "mobile_dist", str(mobile))
    client = TestClient(create_app())

    assert "MOBILE" in client.get("/mobile/").text            # the mount, html=True index
    assert "DESKTOP" in client.get("/").text                  # desktop shell untouched
    assert "DESKTOP" in client.get("/live").text              # deep link → desktop fallback
    # /mobile redirects to /mobile/ (Starlette mount slash handling) instead of falling
    # through to the desktop catch-all.
    assert "MOBILE" in client.get("/mobile", follow_redirects=True).text


def test_missing_mobile_build_is_a_quiet_skip(tmp_path, monkeypatch):
    desktop, _ = _dists(tmp_path)
    s = get_settings()
    monkeypatch.setattr(s, "serve_webapp", True)
    monkeypatch.setattr(s, "webapp_dist", str(desktop))
    monkeypatch.setattr(s, "mobile_dist", str(tmp_path / "nope"))
    client = TestClient(create_app())

    assert "DESKTOP" in client.get("/").text
    # no /mobile mount → the path falls back to the desktop shell (client-side 404 there)
    assert "DESKTOP" in client.get("/mobile/").text
