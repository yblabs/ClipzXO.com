"""Stage 3b: pick the flash-forward teaser window for a hook version.

The hook version of a clip cold-opens with a short cut from *later* in the
same highlight — the thriller moment: the crowd roar, the funniest line, the
biggest "no way" beat — then the clip continues from its normal start.
Viewers stay for the moment you teased.

Selection analyses the WHOLE video before choosing inside each clip:
  * audio — `analyze_audio` builds an excitement curve (loudness × broadband
    2–8 kHz "cheer" ratio) over the entire source, so peaks anywhere in the
    video compete equally; a detected peak near the teaser's end extends the
    window a couple of seconds so the roar/payoff actually lands;
  * text  — hook signals from the highlight scorer plus funny/intense
    wording (laughter, superlatives, reaction words).
Candidates are snapped to word boundaries so captions stay coherent. When
the clip is too short to have a "future" part, the tail is used.
"""
from __future__ import annotations

import re
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app import config
from app.pipeline.highlights import _score_text
from app.pipeline.transcribe import Transcript

# extra "this is the funniest / most intense moment" signals layered on top
# of the highlight scorer's hook signals.
_FUNNY_RE = re.compile(
    r"\bhaha+|\b(?:lol|rofl|funny|hilarious|jok(?:e|es)|laugh(?:s|ing|ed)?|dying)\b"
)
_INTENSE_RE = re.compile(
    r"\b(insane|crazy|unbelievable|no way|oh my|omg|shocking|unreal|massive|"
    r"huge|listen|crowd|cheer(?:s|ing)?|roar(?:s|ing)?|knockout|"
    r"finish(?:es|ed)?|stoppage|title)\b"
)


@dataclass(frozen=True)
class TeaserWindow:
    """Absolute source-time window for the teaser cut."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ExcitementCurve:
    """Normalised whole-video excitement: loudness × broadband noise ratio.

    values ~0 (quiet talking) → 1 (loud roar/cheer). `times` are window
    start times in source seconds; `window` is each window's length.
    """

    times: np.ndarray
    values: np.ndarray
    window: float = 0.5

    @property
    def empty(self) -> bool:
        return self.times.size == 0 or self.values.size == 0

    def _mask(self, start: float, end: float) -> np.ndarray:
        return (self.times + self.window > start) & (self.times < end)

    def window_mean(self, start: float, end: float) -> float:
        """Mean excitement over [start, end) (nearest value if outside)."""
        if self.empty:
            return 0.0
        mask = self._mask(start, end)
        if not mask.any():
            idx = int(np.clip(np.searchsorted(self.times, start), 0, self.times.size - 1))
            return float(self.values[idx])
        return float(self.values[mask].mean())

    def peak_in(self, start: float, end: float) -> tuple[float, float]:
        """(time, value) of the highest excitement inside [start, end)."""
        if self.empty:
            return (start, 0.0)
        mask = self._mask(start, end)
        if not mask.any():
            return (start, self.window_mean(start, end))
        idx = int(np.argmax(np.where(mask, self.values, -1.0)))
        return (float(self.times[idx]), float(self.values[idx]))

    @property
    def peak_time(self) -> float:
        """Loudest moment of the whole video (source seconds)."""
        if self.empty:
            return 0.0
        return float(self.times[int(np.argmax(self.values))])


def analyze_audio(
    path: Path | str, *, window: float = 0.5, hop: float = 0.25
) -> ExcitementCurve:
    """Build the excitement curve from the source audio (16-bit PCM WAV).

    Loudness alone tracks shouting commentary; crowd cheers are additionally
    broadband, so the high-band (2–8 kHz) energy ratio lifts roars/applause
    above plain speech. Best-effort: unreadable or odd-format input yields
    an empty curve instead of raising.
    """
    empty = ExcitementCurve(np.array([]), np.array([]), window)
    try:
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001 - analysis must never kill a job
        return empty
    if width != 2 or sr <= 0 or not raw:
        return empty

    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    n = int(sr * window)
    step = max(1, int(sr * hop))
    if n < 16 or x.size < n:
        return empty

    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    hf = (freqs >= 2000) & (freqs <= 8000)
    window_i = np.hanning(n)
    times: list[float] = []
    loud_list: list[float] = []
    hf_list: list[float] = []
    for i in range(0, x.size - n + 1, step):
        seg = x[i : i + n]
        rms = float(np.sqrt(np.mean(seg * seg))) + 1e-12
        spec = np.abs(np.fft.rfft(seg * window_i))
        total = float(np.sum(spec)) + 1e-12
        times.append(i / sr)
        loud_list.append(rms)
        hf_list.append(float(np.sum(spec[hf])) / total)

    loud = np.asarray(loud_list, dtype=np.float64)
    hf_ratio = np.asarray(hf_list, dtype=np.float64)
    ref = max(float(np.percentile(loud, 95)), 1e-9)
    loud_n = np.clip(loud / ref, 0.0, 1.0)
    # speech sits roughly below 0.25 high-band share; roars push well above
    hf_n = np.clip((hf_ratio - 0.25) / 0.5, 0.0, 1.0)
    values = np.clip(loud_n * (0.65 + 0.35 * hf_n), 0.0, 1.0)

    # light smoothing (~1.25s) with edge padding so the ends aren't damped
    if values.size >= 5:
        padded = np.pad(values, 2, mode="edge")
        values = np.convolve(padded, np.ones(5) / 5.0, mode="valid")

    return ExcitementCurve(np.asarray(times, dtype=np.float64), values, window)


def score_moment(text: str) -> float:
    """Hook + funny + intense signals for a candidate teaser's words."""
    low = text.lower()
    score = _score_text(text)
    score += 1.5 * len(_FUNNY_RE.findall(low))
    score += 1.2 * len(_INTENSE_RE.findall(low))
    return score


def _extend_for_peak(
    window: TeaserWindow,
    clip_end: float,
    duration: float,
    audio: ExcitementCurve | None,
) -> TeaserWindow:
    """Extend a few seconds past a strong nearby peak so the cheer lands.

    Looks a little beyond the window's original end (bounded by the clip and
    by `duration + a couple of seconds`); when a loud peak sits close to the
    end, the teaser is stretched to keep ~HOOK_PEAK_TAIL_SECONDS after it.
    """
    if audio is None or audio.empty:
        return window
    extension_room = min(
        clip_end, window.start + duration + config.HOOK_PEAK_TAIL_SECONDS + 1.0
    )
    if extension_room <= window.end:
        return window
    peak_t, peak_v = audio.peak_in(window.start, extension_room)
    if peak_v < 0.6:
        return window
    wanted = min(extension_room, peak_t + config.HOOK_PEAK_TAIL_SECONDS)
    if wanted > window.end:
        return TeaserWindow(window.start, wanted)
    return window


def pick_teaser(
    transcript: Transcript,
    clip_start: float,
    clip_end: float,
    *,
    duration: float | None = None,
    min_gap: float | None = None,
    audio: ExcitementCurve | None = None,
    audio_weight: float | None = None,
) -> TeaserWindow:
    """Choose a flash-forward window inside [clip_start, clip_end).

    `duration` — target teaser length (default config.HOOK_TEASER_SECONDS).
    `min_gap` — how far after clip_start the teaser must come from
    (default config.HOOK_MIN_GAP_SECONDS).
    `audio` — whole-video excitement curve (cheer/energy) if available.
    `audio_weight` — influence of audio excitement vs text signals.
    """
    duration = float(duration if duration is not None else config.HOOK_TEASER_SECONDS)
    min_gap = float(min_gap if min_gap is not None else config.HOOK_MIN_GAP_SECONDS)
    weight = float(audio_weight if audio_weight is not None else config.HOOK_AUDIO_WEIGHT)
    # weight <= 0 means "ignore audio entirely" (text-only picking, no extension)
    if audio is not None and (audio.empty or weight <= 0):
        audio = None
    transcript_duration = float(transcript.duration or 0.0)
    if transcript_duration > 0:
        # never let the teaser escape the actual source material
        clip_end = min(float(clip_end), transcript_duration)
    clip_len = max(0.0, clip_end - clip_start)
    if clip_len <= 0:
        return TeaserWindow(clip_start, clip_start)

    words = [w for w in transcript.words if w.start >= clip_start and w.end <= clip_end]

    def _tail() -> TeaserWindow:
        start = max(clip_start, clip_end - duration)
        return TeaserWindow(start, clip_end)

    if not words:
        return _tail()

    # candidate starts: word starts in the "future" part of the clip, leaving
    # room for the full teaser before (a little before) the clip's end.
    latest_start = clip_end - duration - 0.2
    # each candidate: (start, snapped_end, words_text, position_bonus)
    candidates: list[tuple[float, float, str, float]] = []

    for i, w in enumerate(words):
        if w.start < clip_start + min_gap or w.start > latest_start:
            continue
        window_words = [
            x for x in words[i:] if x.start <= w.start + duration + 0.25
        ]
        if not window_words:
            continue
        text = " ".join(x.text for x in window_words)
        position_bonus = 0.25 * ((w.start - clip_start) / clip_len)
        # snap the end to the last word that fits
        end = min(clip_end, max(window_words[-1].end, w.start + duration * 0.6))
        candidates.append((w.start, end, text, position_bonus))

    if not candidates:
        return _tail()

    def _audio_mean(start: float) -> float:
        return audio.window_mean(start, start + duration) if audio is not None else 0.0

    # Cheer prominence gate: when this clip has a standout energetic window
    # (crowd roar, big reaction), the teaser opens there — a real cheer
    # outranks even strongly hooky wording. Flat/quiet audio falls through to
    # pure text scoring (all candidates clear the bar equally).
    eligible = candidates
    if audio is not None:
        clip_max = max(_audio_mean(c[0]) for c in candidates)
        bar = clip_max * config.HOOK_AUDIO_PROMINENCE
        gated = [c for c in candidates if _audio_mean(c[0]) >= bar]
        if gated:
            eligible = gated

    best = max(
        eligible,
        key=lambda c: (
            score_moment(c[2]) + c[3] + weight * _audio_mean(c[0]),
            c[0],  # tie-break: later moment wins
        ),
    )
    chosen = TeaserWindow(best[0], best[1])
    if audio is not None:
        chosen = _extend_for_peak(chosen, clip_end, duration, audio)
    return chosen
