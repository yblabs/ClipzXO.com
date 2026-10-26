"""Stationary subtitles: static ASS block + per-clip suggestion + wiring."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import config
from app.pipeline import captions
from tests.helpers import make_transcript

LONG_TEXT = (
    "N3on is taking a break from streaming for his mental health after "
    "his family disowned him for getting tattoos as a muslim"
)


# --------------------------------------------------------------------------- #
# Wrapping
# --------------------------------------------------------------------------- #
def test_wrap_single_short_line():
    assert captions.wrap_static("hello chat") == ["hello chat"]


def test_wrap_breaks_at_box_width():
    lines = captions.wrap_static(LONG_TEXT)
    assert len(lines) <= captions.STATIC_MAX_LINES
    assert all(len(l) <= captions.STATIC_LINE_CHARS + 10 for l in lines)  # +tol for last word
    assert " ".join(lines).replace("…", "").split()[:3] == ["N3on", "is", "taking"]


def test_wrap_caps_lines_with_ellipsis():
    huge = " ".join(f"word{i}" for i in range(200))
    lines = captions.wrap_static(huge)
    assert len(lines) == captions.STATIC_MAX_LINES
    assert lines[-1].endswith("…")


def test_wrap_empty_yields_empty_line():
    assert captions.wrap_static("   ") == [""]


# --------------------------------------------------------------------------- #
# Static ASS builder
# --------------------------------------------------------------------------- #
def test_static_ass_is_one_event_for_the_whole_clip():
    ass = captions.build_static_ass("the chat lost it", 31.9)
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 1
    d = dialogues[0]
    assert ",Sub,," in d                       # stationary style
    assert d.split(",")[1] == "0:00:00.00"     # starts at clip start
    assert d.split(",")[2] == "0:00:31.90"     # ends at clip end
    assert "the chat lost it" in d
    assert "\\pos(" in d                       # pinned in place


def test_static_ass_has_no_animation_or_word_events():
    ass = captions.build_static_ass("the chat lost it", 10.0)
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 1
    assert "\\fscx" not in ass                 # no pop-in
    assert "\\1c" not in ass                   # no per-word highlight sweep
    assert "fad(" in ass                       # only the tiny edge fade


def test_static_ass_wraps_multiline_with_backslash_n():
    ass = captions.build_static_ass(LONG_TEXT, 20.0)
    d = next(l for l in ass.splitlines() if l.startswith("Dialogue:"))
    assert "\\N" in d                          # line break inside one event


def test_static_ass_uses_configured_box_and_text_style():
    ass = captions.build_static_ass("x", 5.0)
    style = next(l for l in ass.splitlines() if l.startswith("Style: Sub,"))
    assert f",{config.STATIC_FONT_SIZE}," in style
    assert "&H00000000" in style               # black text (default)
    assert "&H00FFFFFF" in style               # white box (default)


def test_write_static_ass(tmp_path):
    p = captions.write_static_ass(tmp_path / "c.ass", "hello", 12.0)
    assert p.read_text(encoding="utf-8").startswith("[Script Info]")
    assert "hello" in p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Suggestion analysis
# --------------------------------------------------------------------------- #
def test_suggest_prefers_curated_title():
    t = make_transcript([(LONG_TEXT, 0.0, 30.0)])
    s = captions.suggest_subtitle(t.words, 0.0, 30.0, title=LONG_TEXT)
    assert s.startswith("N3on is taking a break")


def test_suggest_falls_back_to_payoff_when_title_is_fragment():
    t = make_transcript([
        ("the team stood there quietly waiting", 0.0, 15.0),
        ("insane wow never", 15.0, 20.0),
        ("after that everyone went home slowly", 20.0, 30.0),
    ])
    s = captions.suggest_subtitle(t.words, 0.0, 30.0, title="and")
    assert "never" in s                       # payoff marker line


def test_suggest_caps_length_to_the_box():
    t = make_transcript([(LONG_TEXT, 0.0, 30.0)])
    s = captions.suggest_subtitle(t.words, 0.0, 30.0, title=LONG_TEXT * 6)
    assert len(s) <= captions.STATIC_LINE_CHARS * captions.STATIC_MAX_LINES + 1


def test_suggest_without_title_or_words_is_nonempty():
    t = make_transcript([])
    s = captions.suggest_subtitle(t.words, 0.0, 30.0, title="")
    assert s.strip()


# --------------------------------------------------------------------------- #
# run_pipeline wiring
# --------------------------------------------------------------------------- #
def _fake_stages(monkeypatch, tmp_path, transcript):
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
    monkeypatch.setattr(run_mod, "transcribe", lambda *a, **k: transcript)

    hl = SimpleNamespace(
        title="Chiefs tailgate as you can see", start=0.0, end=30.0, duration=30.0,
        model_dump=lambda: {"title": "Chiefs tailgate", "start": 0.0, "end": 30.0},
    )
    monkeypatch.setattr(
        run_mod, "select_highlights",
        lambda *a, **k: SimpleNamespace(provider="heuristic", highlights=[hl]),
    )

    def fake_render(video, start, end, ass, out, **k):
        out.write_bytes(b"\x00")

    monkeypatch.setattr(run_mod.render, "render_clip", fake_render)
    return run_mod


def _transcript():
    return make_transcript([
        ("Hey the Chiefs tailgate was insane", 0.0, 10.0),
        ("everybody screamed when they won", 10.0, 20.0),
        ("and the whole lot went wild again", 20.0, 30.0),
    ])


def _run(run_mod, tmp_path, **kw):
    opts = dict(max_highlights=1, hook=False, hook_playbook=False,
                viral_analysis=False, watermark_on=False)
    opts.update(kw)
    return run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out", **opts,
    )


def test_static_manual_subtitle_burned_on_clip(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path, _transcript())
    result = _run(run_mod, tmp_path, caption_mode="static",
                  subtitle="The chat lost it 😭")
    assert result["caption_mode"] == "static"
    assert result["subtitle"] == "The chat lost it 😭"
    ass = (tmp_path / "out" / "clip_01.ass").read_text(encoding="utf-8")
    d = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(d) == 1
    assert "The chat lost it 😭" in d[0]


def test_static_auto_suggests_from_analysis(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path, _transcript())
    logs: list[str] = []
    result = run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out",
        max_highlights=1, hook=False, hook_playbook=False,
        viral_analysis=False, watermark_on=False,
        caption_mode="static", log=logs.append,
    )
    assert result["subtitle"] == ""           # nothing manual was given
    ass = (tmp_path / "out" / "clip_01.ass").read_text(encoding="utf-8")
    assert "Chiefs tailgate as you can see" in ass   # suggested from title
    assert any("Static subtitle clip_01" in m for m in logs)


def test_animated_mode_keeps_word_events(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path, _transcript())
    result = _run(run_mod, tmp_path, caption_mode="animated")
    assert result["caption_mode"] == "animated"
    assert result["subtitle"] == ""
    ass = (tmp_path / "out" / "clip_01.ass").read_text(encoding="utf-8")
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) > 1                 # one per active word
    assert "\\1c" in ass                      # word highlight sweep present


def test_hook_teaser_cut_gets_the_same_static_text(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path, _transcript())
    recorded: list[tuple[str, str]] = []
    original = run_mod.captions.write_static_ass

    def spy(path, text, duration):
        recorded.append((str(path), text))
        return original(path, text, duration)

    monkeypatch.setattr(run_mod.captions, "write_static_ass", spy)
    monkeypatch.setattr(run_mod.render, "concat_clips",
                        lambda parts, out: out.write_bytes(b"\x00"))
    monkeypatch.setattr(run_mod.teaser, "analyze_audio", lambda *_a, **_k: None)
    _run(run_mod, tmp_path, caption_mode="static", subtitle="Same line here",
         hook=True)
    assert len(recorded) == 2                 # main cut + teaser cut
    assert recorded[0][1] == recorded[1][1] == "Same line here"


def test_unknown_caption_mode_raises(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path, _transcript())
    with pytest.raises(ValueError, match="caption mode"):
        _run(run_mod, tmp_path, caption_mode="karaoke")


def test_config_default_is_animated():
    assert config.CAPTION_MODE in {"animated", "static"}
