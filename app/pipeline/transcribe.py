"""Stage 2: speech-to-text with word-level timestamps (faster-whisper)."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app import config

ProgressFn = Callable[[float], None]


@dataclass
class Word:
    text: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class Transcript:
    language: str
    duration: float
    text: str
    segments: list[Segment] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "duration": round(self.duration, 3),
            "text": self.text,
            "segments": [s.to_dict() for s in self.segments],
            "words": [w.to_dict() for w in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        segments = [
            Segment(
                start=float(s["start"]),
                end=float(s["end"]),
                text=s["text"],
                words=[Word(w["text"], float(w["start"]), float(w["end"])) for w in s.get("words", [])],
            )
            for s in data.get("segments", [])
        ]
        words = [Word(w["text"], float(w["start"]), float(w["end"])) for w in data.get("words", [])]
        if not words:
            words = [w for s in segments for w in s.words]
        return cls(
            language=data.get("language", ""),
            duration=float(data.get("duration", 0.0)),
            text=data.get("text", ""),
            segments=segments,
            words=words,
        )


_model = None
_model_lock = threading.Lock()


def get_model():
    """Lazily load the shared Whisper model (singleton, thread-safe)."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
            )
        return _model


def transcribe(
    audio_path: Path,
    duration: float,
    on_progress: Optional[ProgressFn] = None,
) -> Transcript:
    """Transcribe a WAV file, returning word-level timestamps."""
    model = get_model()
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=config.WHISPER_LANGUAGE,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
    )

    segments: list[Segment] = []
    words: list[Word] = []
    for seg in segments_iter:
        seg_words = [
            Word(text=w.word.strip(), start=float(w.start), end=float(w.end))
            for w in (seg.words or [])
            if w.word and w.word.strip()
        ]
        segments.append(
            Segment(start=float(seg.start), end=float(seg.end), text=seg.text.strip(), words=seg_words)
        )
        words.extend(seg_words)
        if on_progress and duration > 0:
            on_progress(min(0.99, float(seg.end) / duration))

    if duration <= 0 and segments:
        duration = segments[-1].end

    full_text = " ".join(s.text for s in segments).strip()
    return Transcript(
        language=getattr(info, "language", "") or "",
        duration=float(duration),
        text=full_text,
        segments=segments,
        words=words,
    )


def save(transcript: Transcript, path: Path) -> None:
    path.write_text(json.dumps(transcript.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> Transcript:
    return Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))
