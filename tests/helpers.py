"""Shared fixtures: build synthetic Whisper transcripts from timed sentences."""
from __future__ import annotations

from app.pipeline.transcribe import Segment, Transcript, Word


def make_segment(text: str, start: float, end: float) -> Segment:
    tokens = text.split()
    step = (end - start) / max(1, len(tokens))
    words = [
        Word(tok, start + i * step, start + (i + 1) * step) for i, tok in enumerate(tokens)
    ]
    return Segment(start=start, end=end, text=text, words=words)


def make_transcript(pieces: list[tuple[str, float, float]], language: str = "en") -> Transcript:
    """pieces: [(text, start, end), ...]"""
    segments = [make_segment(text, start, end) for text, start, end in pieces]
    words = [w for s in segments for w in s.words]
    duration = max((end for _, _, end in pieces), default=0.0)
    return Transcript(
        language=language,
        duration=duration,
        text=" ".join(s.text for s in segments),
        segments=segments,
        words=words,
    )
