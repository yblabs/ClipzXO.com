"""num_clips — how many clips a job makes (Upload + YouTube/URL endpoints)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import MAX_CLIPS, app, manager

client = TestClient(app)

FAKE_VIDEO = b"\x00\x00\x00\x18ftypmp42 fake video bytes"


@pytest.fixture()
def capture(monkeypatch, tmp_path):
    """Run jobs synchronously and capture the pipeline's kwargs."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    seen: dict = {}

    def fake_run(source, workdir, **kwargs):
        seen.update(kwargs)
        return {"provider": "test", "language": "en", "duration": 12.0,
                "highlights": [], "artifacts": []}

    monkeypatch.setattr("app.main.pipeline_run.run_pipeline", fake_run)
    monkeypatch.setattr(manager, "start", lambda job, fn: fn(job))
    return seen


def _upload(num_clips: str):
    return client.post(
        "/api/jobs/upload",
        files={"file": ("source.mp4", FAKE_VIDEO, "video/mp4")},
        data={"num_clips": num_clips},
    )


# --------------------------------------------------------------------------- #
# Pass-through (both input modes)
# --------------------------------------------------------------------------- #
def test_upload_num_clips_reaches_pipeline(capture):
    resp = _upload("7")
    assert resp.status_code == 200
    assert capture["max_highlights"] == 7


def test_url_num_clips_reaches_pipeline(capture):
    resp = client.post(
        "/api/jobs/url", json={"url": "https://youtu.be/abc123", "num_clips": 3}
    )
    assert resp.status_code == 200
    assert capture["max_highlights"] == 3


# --------------------------------------------------------------------------- #
# Default (absent/empty → server default = config.MAX_HIGHLIGHTS)
# --------------------------------------------------------------------------- #
def test_num_clips_defaults_to_server_setting(capture):
    client.post("/api/jobs/url", json={"url": "https://youtu.be/abc123"})
    assert capture["max_highlights"] is None

    _upload("")
    assert capture["max_highlights"] is None


# --------------------------------------------------------------------------- #
# Clamping to the 1–10 band
# --------------------------------------------------------------------------- #
def test_num_clips_clamped_to_top_limit(capture):
    assert MAX_CLIPS == 10
    client.post("/api/jobs/url", json={"url": "https://youtu.be/abc123", "num_clips": 99})
    assert capture["max_highlights"] == 10

    _upload("25")
    assert capture["max_highlights"] == 10


def test_num_clips_clamped_to_one(capture):
    client.post("/api/jobs/url", json={"url": "https://youtu.be/abc123", "num_clips": 0})
    assert capture["max_highlights"] == 1

    _upload("-4")
    assert capture["max_highlights"] == 1


def test_num_clips_rejects_garbage_in_form():
    assert _upload("many").status_code == 400
