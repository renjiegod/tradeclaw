"""
Web UI 静态资源单元测试
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_ui_serves_index_from_overridden_dist_dir(monkeypatch, tmp_path: Path):
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)

    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body><div id='root'>Subscription Deck</div></body></html>",
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('ui ok')", encoding="utf-8")

    monkeypatch.setenv("QMT_PROXY_UI_DIST_DIR", str(dist_dir))

    client = TestClient(app)

    index_response = client.get("/ui")
    assert index_response.status_code == 200
    assert "text/html" in index_response.headers.get("content-type", "")
    assert "Subscription Deck" in index_response.text

    asset_response = client.get("/ui/assets/app.js")
    assert asset_response.status_code == 200
    assert "ui ok" in asset_response.text


def test_ui_returns_404_when_build_artifacts_are_missing(monkeypatch, tmp_path: Path):
    missing_dir = tmp_path / "missing-dist"
    monkeypatch.setenv("QMT_PROXY_UI_DIST_DIR", str(missing_dir))

    client = TestClient(app)
    response = client.get("/ui")

    assert response.status_code == 404
    assert "Web UI build artifacts not found" in response.text
