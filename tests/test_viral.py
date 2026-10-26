"""Viral analysis: 1M+ creator/streamer Shorts pattern checks per clip."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import config
from app.pipeline import viral
from tests.helpers import make_transcript

# --------------------------------------------------------------------------- #
# Open on the moment
# --------------------------------------------------------------------------- #
def test_open_greeting_is_a_fix():
    t = make_transcript([("Hey everyone welcome back to the channel", 0.0, 4.0)])
    r = viral.check_open(t.words, 0.0, 4.0)
    assert r["status"] == "fix"
    assert "greeting" in r["evidence"]
    assert "Hey everyone" in r["evidence"] or "substantive line" in r["fix"]
    assert r["deduction"] == 15


def test_open_filler_is_a_fix():
    t = make_transcript([("Um let me tell you what happened", 0.0, 4.0)])
    r = viral.check_open(t.words, 0.0, 4.0)
    assert r["status"] == "fix"
    assert "filler" in r["evidence"]
    assert r["deduction"] == 12


def test_open_mid_thought_is_watch():
    t = make_transcript([("So there we were waiting", 0.0, 4.0)])
    r = viral.check_open(t.words, 0.0, 4.0)
    assert r["status"] == "watch"
    assert "mid-thought" in r["evidence"]


def test_open_on_content_passes():
    t = make_transcript([("The quarterback threw a pick six", 0.0, 4.0)])
    r = viral.check_open(t.words, 0.0, 4.0)
    assert r["status"] == "pass"
    assert r["deduction"] == 0


def test_open_empty_window_is_watch():
    t = make_transcript([])
    r = viral.check_open(t.words, 100.0, 110.0)
    assert r["status"] == "watch"


# --------------------------------------------------------------------------- #
# Length band
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "status"),
    [(30.0, "pass"), (35.0, "pass"), (40.0, "watch"), (45.0, "watch"), (52.0, "fix")],
)
def test_length_bands(seconds, status):
    r = viral.check_length(seconds)
    assert r["status"] == status
    if status != "pass":
        assert r["fix"]


# --------------------------------------------------------------------------- #
# Payoff placement
# --------------------------------------------------------------------------- #
PLAIN = "the team walked onto the field and stood there quietly in the rain"


def test_payoff_missing_is_big_fix():
    t = make_transcript([(PLAIN, 0.0, 20.0)])
    r = viral.check_payoff(t.words, 0.0, 20.0)
    assert r["status"] == "fix"
    assert r["deduction"] == 25


def test_payoff_at_very_end_has_no_room():
    t = make_transcript([
        ("the team walked onto the field and stood there quietly in the rain waiting",
         0.0, 17.0),
        ("insane wow never", 17.0, 20.0),
    ])
    r = viral.check_payoff(t.words, 0.0, 20.0)
    assert r["status"] == "fix"
    assert "no room to land" in r["evidence"]


def test_payoff_too_early_is_watch():
    t = make_transcript([
        ("insane wow never seen that crazy", 0.0, 5.0),
        (PLAIN + " slowly and carefully today", 5.0, 30.0),
    ])
    r = viral.check_payoff(t.words, 0.0, 30.0)
    assert r["status"] == "watch"
    assert "arrives early" in r["evidence"]


def test_payoff_well_placed_passes():
    t = make_transcript([
        ("the team walked onto the field and stood there quietly", 0.0, 12.0),
        ("insane wow never", 12.0, 18.0),
        (PLAIN, 18.0, 30.0),
    ])
    r = viral.check_payoff(t.words, 0.0, 30.0)
    assert r["status"] == "pass"
    assert "never" in r["evidence"]  # the detected marker quote


# --------------------------------------------------------------------------- #
# Dead air
# --------------------------------------------------------------------------- #
def test_dead_air_long_pause_is_fix_with_timestamps():
    t = make_transcript([
        ("one two three four five six seven eight nine ten", 0.0, 10.0),
        ("and then the whole thing happened again later", 15.5, 30.0),
    ])
    r = viral.check_dead_air(t.words, 0.0, 30.0)
    assert r["status"] == "fix"
    assert "+10.0s" in r["evidence"]          # timestamped
    assert "jump-cut" in r["fix"]


def test_dead_air_small_pause_is_watch():
    t = make_transcript([
        ("one two three four five six seven eight nine ten", 0.0, 15.0),
        ("and then it happened", 18.2, 40.0),
    ])
    r = viral.check_dead_air(t.words, 0.0, 40.0)
    assert r["status"] == "watch"


def test_dead_air_continuous_speech_passes():
    t = make_transcript([(PLAIN, 0.0, 30.0)])
    r = viral.check_dead_air(t.words, 0.0, 30.0)
    assert r["status"] == "pass"


# --------------------------------------------------------------------------- #
# Ending / rewatch
# --------------------------------------------------------------------------- #
def test_ending_with_post_payoff_chatter_is_fix():
    t = make_transcript([
        ("the team walked onto the field and stood there quietly", 0.0, 12.0),
        ("insane wow never", 12.0, 15.0),
        ("and then they talked about it for a long time afterwards", 15.0, 25.0),
    ])
    r = viral.check_ending(t.words, 0.0, 25.0)
    assert r["status"] == "fix"
    assert "rewatch" in r["fix"]


def test_ending_close_to_payoff_passes():
    t = make_transcript([
        ("the team walked onto the field and stood there quietly", 0.0, 15.0),
        ("insane wow never", 15.0, 17.0),
        ("done now", 17.0, 20.0),
    ])
    r = viral.check_ending(t.words, 0.0, 20.0)
    assert r["status"] == "pass"


# --------------------------------------------------------------------------- #
# Captions readable muted
# --------------------------------------------------------------------------- #
def test_caption_density_too_fast_is_watch():
    words = " ".join(f"w{i}" for i in range(30))
    t = make_transcript([(words, 0.0, 5.0)])
    r = viral.check_caption_density(t.words, 0.0, 5.0)
    assert r["status"] == "watch"
    assert "muted" in r["evidence"]


def test_caption_density_normal_passes():
    t = make_transcript([(PLAIN, 0.0, 30.0)])
    r = viral.check_caption_density(t.words, 0.0, 30.0)
    assert r["status"] == "pass"


# --------------------------------------------------------------------------- #
# Cold-viewer context
# --------------------------------------------------------------------------- #
def test_context_passes_with_title():
    t = make_transcript([(PLAIN, 0.0, 20.0)])
    r = viral.check_cold_context(t.words, 0.0, 20.0, title="Chiefs tailgate")
    assert r["status"] == "pass"


def test_context_passes_with_name_or_number_in_opening():
    t = make_transcript([("Marcus threw the ball from the 40 yard line", 0.0, 6.0)])
    r = viral.check_cold_context(t.words, 0.0, 6.0)
    assert r["status"] == "pass"


def test_context_without_any_marker_is_watch_with_suggestion():
    t = make_transcript([("the guy just stood there and kept talking slowly", 0.0, 20.0)])
    r = viral.check_cold_context(t.words, 0.0, 20.0)
    assert r["status"] == "watch"
    assert "context line" in r["fix"]
    assert "«" in r["fix"]


# --------------------------------------------------------------------------- #
# Honest discussion ask
# --------------------------------------------------------------------------- #
def test_discussion_with_real_question_passes():
    t = make_transcript([("would you have kicked the field goal why?", 0.0, 10.0)])
    r = viral.check_discussion(t.words, 0.0, 10.0)
    assert r["status"] == "pass"
    assert "pinned" in r["fix"]


def test_discussion_without_question_is_watch():
    t = make_transcript([(PLAIN, 0.0, 20.0)])
    r = viral.check_discussion(t.words, 0.0, 20.0)
    assert r["status"] == "watch"
    assert r["deduction"] == 5


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def _messy_clip():
    """A clip with real problems: greeting, a dead pause, post-payoff chatter."""
    return make_transcript([
        ("Hey everyone welcome back", 0.0, 4.0),
        ("the team walked onto the field and stood there quietly", 4.0, 12.0),
        ("insane wow never", 12.0, 15.0),
        ("and then they talked about it for a long time afterwards", 21.0, 30.0),
    ])


def test_build_report_score_is_weighted_sum():
    t = _messy_clip()
    rep = viral.build_report(t.words, 0.0, 30.0, title="Chiefs tailgate")
    assert rep["score"] == max(0, 100 - sum(c["deduction"] for c in rep["patterns"]))
    assert 0 <= rep["score"] <= 100
    assert rep["grade"] == viral.grade(rep["score"])


def test_build_report_fixes_prioritized_by_impact():
    t = _messy_clip()
    rep = viral.build_report(t.words, 0.0, 30.0)
    fixes = rep["prioritized_fixes"]
    assert fixes, "a messy clip must produce fixes"
    impacts = [f["impact"] for f in fixes]
    assert impacts == sorted(impacts, reverse=True)
    assert all(f["action"] for f in fixes)
    # only fix/watch statuses surface as fixes
    statuses = {c["name"]: c["status"] for c in rep["patterns"]}
    assert all(statuses[f["pattern"]] in {"fix", "watch"} for f in fixes)


def test_build_report_best_moment_and_research():
    t = _messy_clip()
    rep = viral.build_report(t.words, 0.0, 30.0)
    assert "never" in rep["best_moment"]["quote"]  # a payoff marker from the cluster
    assert rep["best_moment"]["rel"] > 0
    assert len(rep["research"]) == 8
    assert all(p["source"].startswith("https://") for p in rep["research"])


@pytest.mark.parametrize(
    ("score", "needle"), [(90, "strong"), (75, "solid"), (40, "needs work")]
)
def test_grade_bands(score, needle):
    assert needle in viral.grade(score)


def test_research_keys_are_unique_and_mapped():
    keys = [p["pattern"] for p in viral.RESEARCH]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(viral.SOURCE_BY_KEY)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_markdown_has_score_table_and_sources():
    t = _messy_clip()
    rep = viral.build_report(t.words, 0.0, 30.0, title="Chiefs tailgate")
    md = viral.render_markdown(rep)
    assert md.startswith("# Viral analysis")
    assert f"**Score: {rep['score']}/100" in md
    assert "## Pattern checks" in md
    assert "## What 1M+ creator/streamer Shorts do" in md
    for p in viral.RESEARCH:
        assert p["source"] in md


def test_write_report_json_and_markdown(tmp_path):
    t = _messy_clip()
    rep = viral.build_report(t.words, 0.0, 30.0)
    jp = viral.write_report(tmp_path / "clip_01_viral.json", rep)
    mp = viral.write_report(tmp_path / "clip_01_viral.md", rep, markdown=True)
    assert json.loads(jp.read_text(encoding="utf-8"))["score"] == rep["score"]
    assert mp.read_text(encoding="utf-8").startswith("# Viral analysis")


# --------------------------------------------------------------------------- #
# Wiring through run_pipeline (viral files land next to the hook playbook)
# --------------------------------------------------------------------------- #
def _fake_stages(monkeypatch, tmp_path):
    """Stub every ffmpeg/whisper/render stage; keep viral analysis real."""
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

    t = _messy_clip()
    monkeypatch.setattr(run_mod, "transcribe", lambda *a, **k: t)

    hl = SimpleNamespace(
        title="Chiefs tailgate", start=0.0, end=30.0, duration=30.0,
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


def _run(run_mod, tmp_path, viral_flag):
    return run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out",
        max_highlights=1, hook=False, hook_playbook=False,
        watermark_on=False, viral_analysis=viral_flag,
    )


def test_run_pipeline_writes_viral_reports(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = _run(run_mod, tmp_path, True)

    assert result["viral_analysis"] is True
    jp, mp = out / "clip_01_viral.json", out / "clip_01_viral.md"
    assert jp.is_file() and mp.is_file()
    assert 0 <= json.loads(jp.read_text(encoding="utf-8"))["score"] <= 100
    assert mp.read_text(encoding="utf-8").startswith("# Viral analysis")
    kinds = [a["name"] for a in result["artifacts"]]
    assert "clip_01_viral.json" in kinds and "clip_01_viral.md" in kinds
    # beside the hook playbook files (playbook off here, so none expected)
    assert not list(out.glob("*_hooks.*"))


def test_run_pipeline_viral_off_writes_nothing(tmp_path, monkeypatch):
    run_mod = _fake_stages(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = _run(run_mod, tmp_path, False)
    assert result["viral_analysis"] is False
    assert not list(out.glob("*_viral.*"))


def test_run_pipeline_defaults_to_config_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIRAL_ANALYSIS", True)
    run_mod = _fake_stages(monkeypatch, tmp_path)
    result = run_mod.run_pipeline(
        str(tmp_path / "input.mp4"), tmp_path / "out",
        max_highlights=1, hook=False, hook_playbook=False, watermark_on=False,
    )
    assert result["viral_analysis"] is True
