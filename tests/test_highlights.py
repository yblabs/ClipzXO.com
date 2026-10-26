"""Highlight selection: LLM JSON parsing, normalisation rules, heuristic picker."""
from __future__ import annotations

import pytest

from app.models import Highlight
from app.pipeline.highlights import (
    MIN_CLIP_SECONDS,
    adaptive_band,
    extract_json_array,
    heuristic_highlights,
    normalize_highlights,
    resolve_provider,
    select_highlights,
)
from tests.helpers import make_transcript


def _rich_transcript():
    """A 5-minute talk: bland narration punctuated by clearly hook-y lines."""
    pieces = [
        ("Welcome back everyone today we are going to talk about my morning routine.", 0, 12),
        ("I usually wake up early and make some coffee and check the weather outside.", 12, 27),
        ("But here is the one secret that nobody tells you about productivity?", 27, 40),
        ("Your calendar is lying to you and you are checking email forty times a day.", 40, 55),
        ("Then I commute into the office and I sit down and I answer a few messages.", 55, 72),
        ("Some days are just busy and there is nothing special happening at all.", 72, 85),
        ("Why do almost eighty percent of new year resolutions fail by February?", 85, 98),
        ("The answer is simpler than you think and it has nothing to do with willpower.", 98, 113),
        ("After lunch I like to go for a walk around the neighborhood with my dog.", 113, 128),
        ("Nothing particularly exciting happens on that walk to be honest with you.", 128, 141),
        ("Never start your day by opening your phone that single mistake will cost you everything.", 141, 157),
        ("In the evening I usually cook dinner and then I read a book for a while.", 157, 172),
        ("How do you actually save ten thousand dollars in a year without even noticing?", 172, 186),
        ("I tried three simple tricks and the last one completely changed everything for me.", 186, 201),
        ("Then I watch some television and I go to bed at a reasonable hour.", 201, 214),
        ("Here is a crazy fact you will blink ten times more while reading this sentence.", 214, 229),
        ("Isn't that insane and you can test it right now by counting your blinks.", 229, 242),
        ("Anyway that is all for today thanks for watching and see you next time.", 242, 256),
    ]
    return make_transcript(pieces)


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #
def test_extract_json_from_markdown_fence_with_prose():
    raw = """Sure! Here are the best moments:
```json
[{"start": 45.2, "end": 89.5, "title": "The calendar lie"}]
```
Hope that helps!"""
    out = extract_json_array(raw)
    assert out == [{"start": 45.2, "end": 89.5, "title": "The calendar lie"}]


def test_extract_json_array_bare_with_trailing_text():
    raw = '[{"start": 0, "end": 40, "title": "Hook"}] — let me know if you need more!'
    assert extract_json_array(raw)[0]["title"] == "Hook"


def test_extract_json_from_wrapped_object():
    raw = '{"clips": [{"start": 10.0, "end": 55.0, "title": "x"}]}'
    assert extract_json_array(raw)[0]["start"] == 10.0


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json_array("I could not find any highlights in this transcript.")


# --------------------------------------------------------------------------- #
# Normalisation: clamping, 30–60s, snapping, overlap
# --------------------------------------------------------------------------- #
def test_normalize_clamps_and_enforces_duration_bounds():
    tr = _rich_transcript()
    items = [
        {"start": -50, "end": 10_000, "title": "crazy wide"},
        {"start": 60, "end": 65, "title": "too short"},
        {"start": 120, "end": 300, "title": "too long"},
    ]
    out = normalize_highlights(items, tr, limit=5)
    assert out
    for hl in out:
        assert 0 <= hl.start < hl.end <= tr.duration
        assert 29.0 <= hl.duration <= 60.5, hl


def test_normalize_removes_overlaps_and_duplicates():
    tr = _rich_transcript()
    items = [
        {"start": 40, "end": 90, "title": "a"},
        {"start": 60, "end": 110, "title": "overlaps a"},
        {"start": 41, "end": 91, "title": "duplicate of a"},
        {"start": 150, "end": 200, "title": "b"},
    ]
    out = normalize_highlights(items, tr, limit=5)
    assert len(out) >= 2
    for prev, cur in zip(out, out[1:]):
        assert cur.start >= prev.end - 0.01
    titles = [h.title for h in out]
    assert "duplicate of a" not in titles[1:] or len(titles) == 1


def test_normalize_snaps_to_word_boundaries():
    tr = _rich_transcript()
    # land slightly inside a word: segment 1 starts at 27.0 (first word start 27.0)
    out = normalize_highlights([{"start": 27.4, "end": 87.4, "title": "x"}], tr, limit=1)
    hl = out[0]
    word_starts = {round(w.start, 3) for w in tr.words}
    word_ends = {round(w.end, 3) for w in tr.words}
    assert round(hl.start, 3) in word_starts
    assert round(hl.end, 3) in word_ends


def test_normalize_respects_limit_and_titles():
    tr = _rich_transcript()
    items = [{"start": s, "end": s + 40, "title": f"t{i}"} for i, s in enumerate(range(0, 220, 45))]
    out = normalize_highlights(items, tr, limit=3)
    assert len(out) <= 3


# --------------------------------------------------------------------------- #
# Heuristic picker
# --------------------------------------------------------------------------- #
def test_heuristic_returns_valid_windows():
    tr = _rich_transcript()
    out = heuristic_highlights(tr, count=4)
    assert 1 <= len(out) <= 4
    for hl in out:
        assert 0 <= hl.start < hl.end <= tr.duration
        assert hl.duration >= 25
        assert hl.title
    for prev, cur in zip(out, out[1:]):
        assert cur.start >= prev.end


def test_heuristic_prefers_hook_dense_regions():
    tr = _rich_transcript()
    out = heuristic_highlights(tr, count=1)
    best = out[0]
    # the two strongest hook clusters are ~27–113s and ~141–201s
    overlaps_hook_cluster = (
        best.start < 113 and best.end > 27 or best.start < 201 and best.end > 141
    )
    assert overlaps_hook_cluster, f"picked bland window {best.start}-{best.end}"


def test_provider_falls_back_to_heuristic_without_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_provider("auto") == "heuristic"
    assert resolve_provider(None) == "heuristic"


def test_select_highlights_end_to_end_heuristic():
    tr = _rich_transcript()
    selection = select_highlights(tr, provider="heuristic", count=4)
    assert selection.provider == "heuristic"
    assert selection.highlights
    assert all(isinstance(h, Highlight) for h in selection.highlights)


# --------------------------------------------------------------------------- #
# Adaptive band: short sources honour the requested clip COUNT with shorter
# clips (the "82s video asked for 8 clips" case), instead of returning 2.
# --------------------------------------------------------------------------- #
def _short_transcript(duration: float = 81.7):
    """A ~82s clip with speech spread across the whole thing."""
    pieces = []
    t = 0.0
    i = 0
    while t < duration - 1:
        end = min(duration, t + 6.5)
        pieces.append((f"Line {i} with a question? And some words.", t, end))
        t = end
        i += 1
    return make_transcript(pieces)


def test_adaptive_band_keeps_standard_band_for_long_sources():
    lo, hi, gap, count = adaptive_band(duration=600.0, count=8)
    assert (lo, hi, gap, count) == (30.0, 60.0, 3.0, 8)


def test_adaptive_band_shrinks_for_short_source():
    lo, hi, gap, count = adaptive_band(duration=81.7, count=8)
    assert count == 8                       # requested count is honoured
    assert lo <= hi <= 60.0
    assert lo >= MIN_CLIP_SECONDS
    # the band + gaps must actually fit the source
    assert count * lo + (count - 1) * gap <= 81.7 + 1e-6


def test_adaptive_band_caps_count_at_what_fits():
    # 30s source can't hold 8 clips even at the 8s floor — fewer, longer ones
    lo, hi, gap, count = adaptive_band(duration=30.0, count=8)
    assert count < 8
    assert count >= 1
    assert count * lo + (count - 1) * gap <= 30.0 + 1e-6


def test_short_source_yields_requested_clip_count():
    """Regression: an 82s video asked for 8 clips used to return 2."""
    tr = _short_transcript(81.7)
    selection = select_highlights(tr, provider="heuristic", count=8)
    out = selection.highlights
    assert len(out) == 8, f"expected 8 clips, got {len(out)}: {[(h.start, h.end) for h in out]}"
    for hl in out:
        assert hl.duration >= MIN_CLIP_SECONDS - 0.5, hl
        assert 0 <= hl.start < hl.end <= tr.duration
    for prev, cur in zip(out, out[1:]):
        assert cur.start >= prev.end, "clips must not overlap"
