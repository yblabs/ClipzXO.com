"""Caption generation: chunking rules, ASS structure, per-word highlight events."""
from __future__ import annotations

from app import config
from app.pipeline.captions import build_ass, group_words, _fmt_time
from app.pipeline.transcribe import Word
from tests.helpers import make_transcript


def _words(n=10, start=0.0, step=0.4):
    return [Word(f"word{i}", start + i * step, start + (i + 1) * step) for i in range(n)]


def _dialogue_lines(ass: str) -> list[str]:
    return [l for l in ass.splitlines() if l.startswith("Dialogue:")]


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #
def test_group_words_respects_max_words_and_chars():
    chunks = group_words(_words(10), clip_start=0.0, duration=10.0, max_words=3, max_chars=100)
    assert all(len(c) <= 3 for c in chunks)
    assert sum(len(c) for c in chunks) == 10


def test_group_words_starts_new_line_after_pause():
    words = [
        Word("hello", 0.0, 0.4),
        Word("there", 0.4, 0.8),
        Word("after", 5.0, 5.4),  # 4.2s silence -> new chunk
        Word("pause", 5.4, 5.8),
    ]
    chunks = group_words(words, clip_start=0.0, duration=10.0)
    assert len(chunks) == 2
    assert [w.text for w in chunks[0]] == ["HELLO", "THERE"]  # uppercase style


def test_group_words_clips_to_window():
    words = [
        Word("before", -2.0, -1.0),   # entirely before clip
        Word("inside", 1.0, 1.5),
        Word("after", 9.5, 12.0),     # entirely after clip end
    ]
    chunks = group_words(words, clip_start=0.0, duration=5.0)
    flat = [w for c in chunks for w in c]
    assert [w.text for w in flat] == ["INSIDE"]


# --------------------------------------------------------------------------- #
# ASS structure
# --------------------------------------------------------------------------- #
def test_ass_header_uses_target_resolution_and_style():
    ass = build_ass(_words(6), clip_start=0.0, duration=6.0)
    assert f"PlayResX: {config.TARGET_WIDTH}" in ass
    assert f"PlayResY: {config.TARGET_HEIGHT}" in ass
    assert "Style: Caption," in ass
    assert "[Events]" in ass


def _colour_words(text: str) -> list[tuple[str, str]]:
    """[(colour, word), ...] parsed from an event's inline \1c tags."""
    out = []
    for part in text.split("{\\1c")[1:]:
        colour, rest = part.split("}", 1)
        out.append((colour, rest.strip()))
    return out


def test_one_event_per_active_word_with_single_accent():
    words = _words(3)  # fits in one caption line
    ass = build_ass(words, clip_start=0.0, duration=3.0)
    lines = _dialogue_lines(ass)
    assert len(lines) == 3  # one event per active word
    for i, line in enumerate(lines):
        text = line.rsplit(",,", 1)[-1]
        parsed = _colour_words(text)
        assert [w for _, w in parsed] == ["WORD0", "WORD1", "WORD2"], (
            "identical layout in every event"
        )
        expected = [
            config.CAPTION_HIGHLIGHT if j == i else config.CAPTION_PRIMARY
            for j in range(3)
        ]
        for c, _ in parsed:
            # \1c override tags require the trailing & (libass may otherwise
            # drop the colour change silently)
            assert c.startswith("&H") and c.endswith("&"), c
        assert [c.rstrip("&") for c, _ in parsed] == [
            e.rstrip("&") for e in expected
        ], f"accent must sit on word {i}"


def test_events_are_contiguous_within_a_line_and_in_bounds():
    words = _words(4, start=2.0, step=0.5)
    duration = 6.0
    ass = build_ass(words, clip_start=1.0, duration=duration)
    spans = []
    for line in _dialogue_lines(ass):
        _, start, end, *_ = line.split(",", 4)
        spans.append((_to_sec(start), _to_sec(end)))
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert abs(e1 - s2) < 1e-6 or e1 <= s2 + 0.06, "no overlapping highlights"
    for s, e in spans:
        assert 0 <= s < e <= duration + 1e-6


def _to_sec(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    s, cs = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100


def test_first_event_pops_in_last_event_fades_out():
    words = _words(3)
    ass = build_ass(words, clip_start=0.0, duration=3.0)
    lines = _dialogue_lines(ass)
    assert "\\t(0," in lines[0] and "\\fad(" in lines[0]
    assert "\\fad(0," in lines[-1]
    assert "\\t(0," not in lines[-1] or len(lines) == 1


def test_words_are_uppercased_when_enabled():
    monkey_enabled = config.CAPTION_UPPERCASE
    try:
        config.CAPTION_UPPERCASE = True
        ass = build_ass([Word("Hello", 0.0, 0.5)], clip_start=0.0, duration=2.0)
        assert "HELLO" in ass
        config.CAPTION_UPPERCASE = False
        ass = build_ass([Word("Hello", 0.0, 0.5)], clip_start=0.0, duration=2.0)
        assert "Hello" in ass
    finally:
        config.CAPTION_UPPERCASE = monkey_enabled


def test_fmt_time_carries_over():
    assert _fmt_time(0) == "0:00:00.00"
    assert _fmt_time(1.5) == "0:00:01.50"
    assert _fmt_time(61.05) == "0:01:01.05"
    assert _fmt_time(59.999) == "0:01:00.00"  # rounds instead of emitting 60.99


def test_empty_word_list_still_yields_header_only():
    ass = build_ass([], clip_start=0.0, duration=30.0)
    assert "[Events]" in ass
    assert not _dialogue_lines(ass)


def test_full_transcript_words_render_for_real_clip_window():
    tr = make_transcript(
        [("one two three four five six", 0.0, 6.0), ("seven eight nine ten", 6.0, 11.0)]
    )
    clip_start, clip_end = 3.0, 9.0
    ass = build_ass(tr.words, clip_start, clip_end - clip_start)
    lines = _dialogue_lines(ass)
    assert lines, "expected captions inside the window"
    for line in lines:
        s, e = _to_sec(line.split(",")[1]), _to_sec(line.split(",")[2])
        assert 0 <= s < e <= clip_end - clip_start
