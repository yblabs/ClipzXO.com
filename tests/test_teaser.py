"""Hook versions: flash-forward teaser picking + clip concatenation."""
from __future__ import annotations

import numpy as np
import pytest

from app import config
from app.pipeline import render, teaser
from app.pipeline.teaser import TeaserWindow
from tests.helpers import make_transcript


def _clip_transcript():
    """A 50s talk: bland opening, a hook-y burst with numbers/questions at 26–34s."""
    pieces = [
        ("so um yeah let's just get into it okay", 0, 8),
        ("and then nothing much happens here at all", 8, 18),
        ("nothing to see in this middle part honestly", 18, 26),
        ("why did 90 percent quit right here? shocking right?", 26, 34),
        ("the answer comes later trust me on this one", 34, 42),
        ("and that is exactly how the night ended", 42, 50),
    ]
    return make_transcript(pieces)


# --------------------------------------------------------------------------- #
# Teaser window picking
# --------------------------------------------------------------------------- #
def test_teaser_stays_inside_clip_and_is_short():
    pick = teaser.pick_teaser(_clip_transcript(), 0, 50)
    assert 0 <= pick.start < pick.end <= 50
    assert pick.duration <= config.HOOK_TEASER_SECONDS + 1.0  # word snapping slack
    assert pick.duration >= 1.0


def test_teaser_comes_from_the_future_part():
    # default min gap is 6s — a teaser must not open from the clip start
    pick = teaser.pick_teaser(_clip_transcript(), 0, 50)
    assert pick.start >= config.HOOK_MIN_GAP_SECONDS


def test_teaser_prefers_hooky_late_moment():
    pick = teaser.pick_teaser(_clip_transcript(), 0, 50)
    # the question/number burst lives at 26–34s; bland talk elsewhere
    assert 26.0 <= pick.start < 34.0, f"teaser landed on bland moment {pick.start:.1f}s"


def test_teaser_snaps_to_word_boundaries():
    tr = _clip_transcript()
    pick = teaser.pick_teaser(tr, 0, 50)
    word_starts = {round(w.start, 3) for w in tr.words}
    word_ends = {round(w.end, 3) for w in tr.words}
    assert round(pick.start, 3) in word_starts
    assert round(pick.end, 3) in word_ends or abs(pick.end - 50) < 1e-6


def test_teaser_fallback_tail_when_clip_too_short():
    # 8s clip: no room for min-gap + full teaser -> use the tail
    pick = teaser.pick_teaser(_clip_transcript(), 0, 8)
    assert 0 <= pick.start < pick.end <= 8
    assert abs(pick.end - 8) < 1e-6  # ends exactly at the clip end
    assert pick.start >= 8 - (config.HOOK_TEASER_SECONDS + 1.0)


def test_teaser_respects_custom_duration_and_gap():
    pick = teaser.pick_teaser(_clip_transcript(), 0, 50, duration=3.0, min_gap=20.0)
    assert pick.start >= 20.0
    assert 1.5 <= pick.duration <= 4.5


def test_teaser_clamps_to_transcript_duration():
    pick = teaser.pick_teaser(_clip_transcript(), 40, 999)
    assert pick.end <= 50.0 + 1e-6
    assert pick.start >= 40.0


def test_teaser_empty_transcript_falls_back_to_tail():
    # no word timings at all -> documented fallback: last teaser-length moment
    pick = teaser.pick_teaser(make_transcript([]), 0, 30)
    assert abs(pick.end - 30.0) < 1e-6
    assert abs(pick.duration - config.HOOK_TEASER_SECONDS) < 1e-6


def test_teaser_window_is_immutable_value():
    tw = TeaserWindow(1.0, 3.0)
    assert tw.duration == 2.0
    with pytest.raises(Exception):
        tw.start = 5.0  # frozen dataclass


# --------------------------------------------------------------------------- #
# Whole-video excitement analysis (crowd cheers / energy peaks)
# --------------------------------------------------------------------------- #
def _write_cheer_wav(path, seconds: float = 12.0, sr: int = 16000):
    """Quiet hum, a loud broadband 'cheer' burst at 6–9s, quiet again."""
    import wave

    t = np.arange(int(seconds * sr)) / sr
    hum = 0.04 * np.sin(2 * np.pi * 300 * t)
    rng = np.random.default_rng(11)
    burst = (t >= 6.0) & (t < 9.0)
    x = np.where(burst, 0.55 * rng.standard_normal(t.shape), hum)
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _curve_with_peak(peak_at: float, peak_len: float = 3.0, end: float = 50.0,
                     value: float = 1.0, floor: float = 0.02):
    times = np.arange(0.0, end, 0.25)
    values = np.where((times >= peak_at) & (times < peak_at + peak_len), value, floor)
    return teaser.ExcitementCurve(times, values, 0.5)


def test_analyze_audio_finds_cheer_burst(tmp_path):
    wav = tmp_path / "a.wav"
    _write_cheer_wav(wav)
    curve = teaser.analyze_audio(wav)
    assert not curve.empty
    cheer = curve.window_mean(6.5, 8.5)
    calm = curve.window_mean(1.0, 4.0)
    assert cheer > 3 * max(calm, 1e-3), (cheer, calm)
    assert 5.5 <= curve.peak_time <= 9.5


def test_analyze_audio_unreadable_file_is_empty(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not a wav file")
    curve = teaser.analyze_audio(bad)
    assert curve.empty
    assert curve.window_mean(0.0, 5.0) == 0.0
    assert curve.peak_time == 0.0


def test_uniform_text_teaser_follows_audio_peak():
    # same bland wording everywhere -> only the cheer can decide
    words = [("the camera keeps rolling quietly along the empty aisle today", i * 10, (i + 1) * 10)
             for i in range(5)]
    tr = make_transcript(words)
    audio = _curve_with_peak(30.0, 3.0)
    pick = teaser.pick_teaser(tr, 0, 50, audio=audio)
    assert pick.start < 33.0 and pick.end > 30.0, pick
    assert audio.window_mean(pick.start, pick.end) > 0.5


def test_audio_weight_zero_matches_text_only_pick():
    tr = _clip_transcript()
    audio = _curve_with_peak(30.0, 3.0)
    with_audio = teaser.pick_teaser(tr, 0, 50, audio=audio, audio_weight=0.0)
    without = teaser.pick_teaser(tr, 0, 50, audio=None)
    assert with_audio.start == without.start


def test_cheer_gate_outranks_early_text_hook():
    # an early question hook vs a later textually-bland crowd roar:
    # the roar must win when it is a standout energetic window
    pieces = [
        ("so what is the big question everyone keeps asking here now?", 0, 12),
        ("the camera keeps rolling quietly along the empty aisle today", 12, 26),
        ("nothing much is being said at this part honestly okay then", 26, 40),
        ("neutral talk continues along the quiet corridor here now", 40, 50),
    ]
    tr = make_transcript(pieces)
    audio = _curve_with_peak(28.0, 6.0)  # broad, strong roar at 28–34s
    pick = teaser.pick_teaser(tr, 0, 50, audio=audio)
    assert 26.0 <= pick.start <= 33.0, f"cheer lost to early text hook: {pick.start:.1f}s"


def test_flat_audio_gate_keeps_text_ordering():
    tr = _clip_transcript()
    times = np.arange(0.0, 50.0, 0.25)
    flat = teaser.ExcitementCurve(times, np.full_like(times, 0.1), 0.5)
    with_audio = teaser.pick_teaser(tr, 0, 50, audio=flat)
    without = teaser.pick_teaser(tr, 0, 50, audio=None)
    assert with_audio.start == without.start


def test_teaser_prefers_funniest_segment():
    pieces = [
        ("so yeah the camera keeps rolling quietly along here", 0, 10),
        ("nothing much is being said at this part honestly", 10, 20),
        ("the setup keeps going with neutral talk right now", 20, 30),
        ("that joke was so funny everyone died laughing hahaha", 30, 40),
        ("and then we wrapped up the whole thing quietly", 40, 50),
    ]
    tr = make_transcript(pieces)
    pick = teaser.pick_teaser(tr, 0, 50)
    assert 30.0 <= pick.start < 40.0, f"missed the funny segment: {pick.start:.1f}s"


# --------------------------------------------------------------------------- #
# Peak extension ("extend few more seconds" so the cheer lands)
# --------------------------------------------------------------------------- #
def test_peak_extension_stretches_teaser_past_cheer():
    audio = _curve_with_peak(13.0, 2.0, end=30.0)
    win = TeaserWindow(10.0, 14.0)
    out = teaser._extend_for_peak(win, 30.0, 4.0, audio)
    assert out.start == 10.0
    # peak at 13.0 + HOOK_PEAK_TAIL_SECONDS after it
    assert out.end == pytest.approx(13.0 + config.HOOK_PEAK_TAIL_SECONDS)


def test_peak_extension_ignores_weak_or_already_covered_peak():
    weak = teaser._extend_for_peak(
        TeaserWindow(10.0, 14.0), 30.0, 4.0, _curve_with_peak(13.0, 2.0, end=30.0, value=0.3)
    )
    assert weak == TeaserWindow(10.0, 14.0)  # below the 0.6 strength bar
    early = teaser._extend_for_peak(
        TeaserWindow(10.0, 14.0), 30.0, 4.0, _curve_with_peak(10.5, 2.0, end=30.0)
    )
    assert early == TeaserWindow(10.0, 14.0)  # peak + tail already inside


def test_peak_extension_never_passes_clip_end():
    audio = _curve_with_peak(29.0, 1.0, end=30.0)
    out = teaser._extend_for_peak(TeaserWindow(25.0, 29.5), 30.0, 4.0, audio)
    assert out.end <= 30.0


def test_peak_extension_without_audio_is_identity():
    win = TeaserWindow(10.0, 14.0)
    assert teaser._extend_for_peak(win, 30.0, 4.0, None) == win


# --------------------------------------------------------------------------- #
# Concatenation (real ffmpeg — both parts are identically encoded)
# --------------------------------------------------------------------------- #
def _make_part(path, seconds: float):
    from app.ffmpeg_utils import ffmpeg, run

    run([
        ffmpeg(), "-y",
        "-f", "lavfi", "-i", f"testsrc=size=128x128:rate=10:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ])


def test_concat_clips_joins_parts_end_to_end(tmp_path):
    from app.ffmpeg_utils import duration_of, ffmpeg

    try:
        a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
        _make_part(a, 1.0)
        _make_part(b, 2.0)
    except FileNotFoundError:
        pytest.skip("ffmpeg not available")
    out = tmp_path / "out.mp4"
    render.concat_clips([a, b], out)
    assert out.stat().st_size > 0
    assert abs(duration_of(out) - 3.0) < 0.25


def test_concat_clips_rejects_empty_list(tmp_path):
    with pytest.raises(ValueError):
        render.concat_clips([], tmp_path / "out.mp4")
