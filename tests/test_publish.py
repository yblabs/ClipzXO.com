"""Publish metadata: ready-to-post Instagram caption + YouTube title/tags/description."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.pipeline import publish
from tests.helpers import make_transcript


def _transcript():
    return make_transcript([
        ("Hey everyone", 0.0, 2.0),
        ("the Chiefs tailgate went completely insane", 2.0, 6.0),
        ("nobody expected the grill to explode like that", 6.0, 11.0),
        ("this was the best tailgate of the year", 11.0, 16.0),
    ])


# --------------------------------------------------------------------------- #
# Keywords
# --------------------------------------------------------------------------- #
def test_extract_keywords_filters_stopwords_and_promotes_proper_nouns():
    t = _transcript()
    kw = publish.extract_keywords(t.words, 0.0, 16.0)
    assert "chiefs" in kw                      # proper noun promoted
    assert "tailgate" in kw
    for stop in ("the", "everyone", "hey", "this", "was", "of"):
        assert stop not in kw
    assert kw[0] == "chiefs"                   # proper nouns rank first


def test_hashtag_is_lowercase_alnum():
    assert publish._hashtag("Chiefs!") == "#chiefs"
    assert publish._hashtag("a") == ""         # too short to be useful
    assert publish._hashtag("don't") == "#dont"


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #
def test_instagram_caption_has_title_hook_cta_and_hashtags():
    t = _transcript()
    ig = publish.build_instagram(t.words, 0.0, 16.0, title="Chiefs tailgate explodes")
    assert ig["title"] == "Chiefs tailgate explodes"
    assert ig["caption"].startswith("Chiefs tailgate explodes")
    assert "Follow" in ig["cta"] and "?" in ig["cta"]      # honest discussion ask
    assert ig["hashtags"] and all(h.startswith("#") for h in ig["hashtags"])
    assert all(h == h.lower() for h in ig["hashtags"])
    assert len(ig["hashtags"]) <= publish.IG_HASHTAG_TARGET
    assert len(ig["caption"]) <= publish.IG_CAPTION_MAX
    assert "chiefs" in ig["keywords"]


def test_instagram_falls_back_to_payoff_title_without_a_title():
    t = _transcript()
    ig = publish.build_instagram(t.words, 0.0, 16.0, title="")
    assert ig["title"]
    assert len(ig["title"]) <= publish.IG_TITLE_MAX
    assert len(ig["title"].split()) >= 3        # a payoff quote, not a single word


def test_build_hashtags_dedupes_and_appends_niche():
    tags = publish.build_hashtags(["chiefs", "chiefs", "tailgate"], niche="NFL Football")
    assert tags.count("#chiefs") == 1
    assert "#nfl" in tags and "#football" in tags


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def test_youtube_title_respects_platform_cap_with_long_title():
    t = _transcript()
    long_title = "The wildest Chiefs tailgate moment of the entire year explained"
    yt = publish.build_youtube(t.words, 0.0, 16.0, title=long_title)
    assert len(yt["title"]) <= publish.YT_TITLE_MAX
    assert yt["tags"] and len(yt["tags"]) <= publish.YT_TAGS_MAX
    assert "#shorts" in yt["hashtags"]         # Shorts search signal
    assert "#shorts" in yt["description"]
    assert len(yt["description"]) >= publish.YT_DESC_MIN


def test_youtube_title_quotes_the_payoff_without_a_title():
    t = _transcript()
    yt = publish.build_youtube(t.words, 0.0, 16.0, title="")
    assert len(yt["title"]) <= publish.YT_TITLE_MAX
    assert yt["title"][0] in ("\u201c", "T", "T")   # quoted payoff or topic-cased


def test_title_line_trims_on_word_boundary_with_ellipsis():
    out = publish.build_title_line("word " * 40, "", "", [], max_len=100)
    assert len(out) <= 100
    assert out.endswith("\u2026")
    assert " " in out                          # trimmed on a word boundary


# --------------------------------------------------------------------------- #
# Report + markdown
# --------------------------------------------------------------------------- #
def test_build_report_shape_and_self_checks():
    t = _transcript()
    rep = publish.build_report(t.words, 0.0, 16.0, title="Chiefs tailgate explodes", index=3)
    assert rep["clip"] == {"index": 3, "title": "Chiefs tailgate explodes",
                           "start": 0.0, "end": 16.0, "duration": 16.0}
    names = {c["name"] for c in rep["checks"]}
    assert "youtube_title_length" in names and "instagram_hashtag_count" in names
    assert all(c["status"] in {"pass", "watch"} for c in rep["checks"])


def test_render_markdown_has_both_platforms_and_copy_blocks():
    t = _transcript()
    rep = publish.build_report(t.words, 0.0, 16.0, title="Chiefs tailgate explodes")
    md = publish.render_markdown(rep)
    assert "## 📱 Instagram" in md
    assert "## 🖼 YouTube" in md
    assert rep["instagram"]["caption"] in md
    assert rep["youtube"]["title"] in md


# --------------------------------------------------------------------------- #
# run_pipeline wiring (publish files land next to the other reports)
# --------------------------------------------------------------------------- #
def _fake_stages(monkeypatch, tmp_path):
    from app.pipeline import run as run_mod

    src = tmp_path / "input.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(run_mod.ingest, "stage_source", lambda *a, **k: src)
    monkeypatch.setattr(
        run_mod.ingest, "probe_video",
        lambda *_a, **_k: {"duration": 30.0, "width": 1920, "height": 1080, "fps": 30.0},
    )
    monkeypatch.setattr(run_mod.ingest, "extract_audio", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "duration_of", lambda *_a, **_k: 30.0)
    monkeypatch.setattr(run_mod, "save_transcript", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "transcribe", lambda *a, **k: _transcript())

    hl = SimpleNamespace(
        title="Chiefs tailgate explodes", start=0.0, end=16.0, duration=16.0,
        model_dump=lambda: {"title": "Chiefs tailgate explodes", "start": 0.0, "end": 16.0},
    )
    monkeypatch.setattr(
        run_mod, "select_highlights",
        lambda *a, **k: SimpleNamespace(provider="heuristic", highlights=[hl]),
    )

    def fake_render(video, start, end, ass, out, **k):
        out.write_bytes(b"\x00")

    monkeypatch.setattr(run_mod.render, "render_clip", fake_render)
    return run_mod


def _run(run_mod, tmp_path, publish_flag):
    return run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out",
        max_highlights=1, hook=False, hook_playbook=False, viral_analysis=False,
        watermark_on=False, publish_metadata=publish_flag,
    )


def test_run_pipeline_writes_publish_metadata_and_settings(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = _run(run_mod, tmp_path, True)

    assert result["publish_metadata"] is True
    meta = json.loads((out / "clip_01_publish.json").read_text(encoding="utf-8"))
    assert meta["instagram"]["caption"]
    assert meta["youtube"]["title"]
    assert (out / "clip_01_publish.md").is_file()

    # settings.json — what the job rendered WITH (the campaign audit reads it)
    settings = json.loads((out / "settings.json").read_text(encoding="utf-8"))
    assert settings["watermark"] is False
    assert settings["caption_mode"] == "animated"
    assert settings["publish_metadata"] is True


def test_run_pipeline_publish_off_writes_nothing(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = _run(run_mod, tmp_path, False)
    assert result["publish_metadata"] is False
    assert not list(out.glob("*_publish.*"))


def test_run_pipeline_publish_defaults_to_config(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "PUBLISH_METADATA", True)
    run_mod = _fake_stages(monkeypatch, tmp_path)
    result = run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out",
        max_highlights=1, hook=False, hook_playbook=False, viral_analysis=False,
        watermark_on=False,
    )
    assert result["publish_metadata"] is True
    assert (tmp_path / "out" / "clip_01_publish.json").is_file()
