"""Hook playbook: payoff detection, 8 archetypes, scoring, beat sheet, markdown."""
from __future__ import annotations

import json

from app.pipeline import hooks
from app.pipeline.transcribe import Word


def _words(text: str, start: float = 0.0, gap: float = 0.35) -> list[Word]:
    """Evenly spaced fake transcript words (0.3s speech + gap each)."""
    words: list[Word] = []
    t = start
    for token in text.split():
        words.append(Word(text=token, start=t, end=t + 0.3))
        t += gap
    return words


LONG_TALK = (
    "so today we are going to talk about training and nobody ever explains "
    "this part properly so you just copy what everyone else does and it never "
    "works and then you quit and honestly that is the biggest mistake people "
    "make because the secret is way simpler than you think and here is the "
    "proof it actually ended the debate forever"
)


# --------------------------------------------------------------------------- #
# Payoff detection
# --------------------------------------------------------------------------- #
def test_detect_payoff_prefers_late_intensifier():
    words = _words(LONG_TALK)
    quote, at = hooks.detect_payoff(words, 0.0, words[-1].end)
    assert quote, "a payoff quote should be found"
    # the strong markers ("secret", "proof", "ended the debate") sit late
    assert at > len(words) * 0.35 * 0.5
    assert any(tok in quote.lower() for tok in ("secret", "proof", "ended", "debate"))


def test_detect_payoff_empty_clip_is_empty():
    assert hooks.detect_payoff([], 0.0, 10.0) == ("", 0.0)


def test_topic_phrase_cleans_sentence_titles():
    words = _words(LONG_TALK)
    t = hooks.topic_phrase("and there is nothing particularly special about it",
                           words, 0.0, 5.0)
    assert not t.lower().startswith("and ")  # leading connective stripped
    assert len(t) <= 35                      # fits on screen at a glance


def test_topic_phrase_prefers_title():
    words = _words(LONG_TALK)
    assert hooks.topic_phrase("Leg day myths", words, 0.0, 5.0) == "Leg day myths"
    assert hooks.topic_phrase("", words, 0.0, 5.0)  # falls back to opening words


# --------------------------------------------------------------------------- #
# Hook generation + scoring
# --------------------------------------------------------------------------- #
def test_generate_hooks_covers_all_eight_archetypes():
    words = _words(LONG_TALK)
    result = hooks.generate_hooks(words, 0.0, words[-1].end, title="Leg day myths")
    assert {h.hook_type for h in result} == {k for k, _, _ in hooks.HOOK_TYPES}
    assert len(result) == 8


def test_hooks_scored_1_to_5_and_ranked():
    words = _words(LONG_TALK)
    result = hooks.generate_hooks(words, 0.0, words[-1].end)
    totals = [h.total for h in result]
    assert totals == sorted(totals, reverse=True)  # ranked best-first
    for h in result:
        assert set(h.scores) == {
            "clarity", "curiosity_gap", "relevance", "works_muted", "honesty"
        }
        assert all(1 <= v <= 5 for v in h.scores.values())
        assert h.spoken_line and h.onscreen_text and h.first_frame_visual


def test_onscreen_text_works_muted_short():
    words = _words(LONG_TALK)
    result = hooks.generate_hooks(words, 0.0, words[-1].end)
    for h in result:
        assert len(h.onscreen_text) <= 64  # readable in a glance, sound off


def test_clickbait_risk_flagged_when_honesty_low():
    words = _words("we are here now so let us begin the session today")
    result = hooks.generate_hooks(words, 0.0, words[-1].end)
    risky = [h for h in result if h.clickbait_risk]
    # a clip with no real payoff should flag the promise-heavy archetypes
    assert risky, "promise-heavy hooks on a payoff-less clip must be flagged"


# --------------------------------------------------------------------------- #
# Beat sheet + report
# --------------------------------------------------------------------------- #
def test_beat_sheet_has_beats_and_silence_cuts():
    words = _words(LONG_TALK)
    end = words[-1].end
    beats = hooks.beat_sheet(words, 0.0, end)
    assert beats
    assert beats[0]["t"] >= 0.0
    ts = [b["t"] for b in beats]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)  # no duplicate beat timestamps
    for b in beats:
        assert b["action"] and b["caption_beat"]


def test_beat_sheet_uses_clip_relative_time():
    """Beats must be timed from the clip start even when it begins at 73.8s
    (regression: absolute word times vs relative interval caused a burst)."""
    start, end = 73.8, 105.2
    words = _words(LONG_TALK, start=start)
    beats = hooks.beat_sheet(words, start, end)
    ts = [b["t"] for b in beats]
    assert 2 <= len(ts) <= 8
    interior = [b2 - b1 for b1, b2 in zip(ts, ts[1:])][:-1]
    assert all(g >= 3.0 for g in interior)   # ~4–8s rhythm between beats
    assert ts[-1] - ts[-2] >= 1.5            # closing beat may finish the sheet
    assert all(0.0 <= t <= end - start for t in ts)


def test_build_report_structure_and_markdown():
    words = _words(LONG_TALK)
    end = words[-1].end
    report = hooks.build_report(words, 0.0, end, title="Leg day myths",
                                platform="shorts", niche="fitness")
    assert len(report["hooks"]) == 8
    assert report["winner"]["hook_type"] in {h["hook_type"] for h in report["hooks"]}
    assert report["test_plan"]["first_test"].startswith("A:")
    md = hooks.render_markdown(report)
    assert "# Hook playbook" in md
    assert report["winner"]["spoken_line"] in md
    # JSON round-trip (this is what gets written to disk)
    assert json.loads(json.dumps(report))["platform"] == "shorts"


def test_write_report_json_and_markdown(tmp_path):
    words = _words(LONG_TALK)
    end = words[-1].end
    report = hooks.build_report(words, 0.0, end)
    jp = hooks.write_report(tmp_path / "h.json", report)
    mp = hooks.write_report(tmp_path / "h.md", report, markdown=True)
    assert json.loads(jp.read_text())["hooks"]
    assert "## Winner" in mp.read_text()
