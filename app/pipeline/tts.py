"""AI voice: read a clip's captions aloud with a selectable voice.

Voice options live in config.TTS_VOICES (label -> edge-tts voice name); the
CLI/UI pick one by label ("aria", "guy", …) or pass a raw edge-tts voice name.

Engine order:
  1. edge-tts (network) — natural neural voices, needs `edge-tts` installed;
  2. macOS `say` (offline) — falls back to a local voice;
  3. raise — run.py turns the failure into a job log line and still renders.

Output is an m4a/mp3 file of the clip's caption text, mixed into the render
as the dominant track with the original speaker ducked to
config.TTS_ORIGINAL_VOLUME (keeps arena/crowd noise, drops the duplicate
speech).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app import config

AudioSuffix = str


def list_voices() -> dict[str, str]:
    """Selectable voice options: {label: engine voice name}."""
    voices = dict(config.TTS_VOICES)
    if not voices:  # config edited to empty — always offer one usable option
        voices = {"default": "en-US-AriaNeural"}
    return voices


def resolve_voice(voice: Optional[str]) -> str:
    """Map a UI/CLI label onto an engine voice name (labels first, then raw)."""
    if not voice or not str(voice).strip():
        raise ValueError("no voice selected")
    key = str(voice).strip()
    voices = list_voices()
    if key.lower() in voices:
        return voices[key.lower()]
    # accept raw edge-tts names (en-US-AriaNeural) case-insensitively
    lowered = {name.lower(): name for name in voices.values()}
    if key.lower() in lowered:
        return lowered[key.lower()]
    return key  # user passed an edge-tts voice we don't list — try it as-is


def _edge_tts(text: str, voice: str, out_path: Path) -> None:
    import asyncio

    import edge_tts

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=config.TTS_RATE)
        await communicate.save(str(out_path))

    asyncio.run(_run())
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RuntimeError(f"edge-tts produced no audio for voice '{voice}'")


def _say_fallback(text: str, out_path: Path) -> Path:
    """Offline macOS `say` fallback (pick any installed English voice)."""
    say = shutil.which("say")
    if not say:
        raise RuntimeError("no `say` binary available for offline fallback")
    aiff = out_path.with_suffix(".aiff")
    proc = subprocess.run(
        [say, "-v", "Samantha", "-o", str(aiff), text],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0 or not aiff.is_file():
        # try the default voice with no -v
        aiff.unlink(missing_ok=True)
        proc = subprocess.run([say, "-o", str(aiff), text], capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not aiff.is_file():
            raise RuntimeError(f"`say` failed: {proc.stderr.strip()[:300]}")
    return aiff


def synthesize(text: str, voice: str, out_base: Path, log=None) -> Path:
    """Write `text` spoken with `voice` to `out_base` (+ .m4a / .aiff).

    `out_base` is a path *without* a media suffix, e.g. .../clip_01_tts.
    Tries edge-tts first, falls back to `say`; raises RuntimeError on total
    failure (caller decides whether to render without voice).
    """
    def _log(line: str) -> None:
        if log:
            log(line)

    clean = " ".join(str(text).split())
    if not clean:
        raise ValueError("nothing to speak (empty caption text)")
    if len(clean) > 5000:
        clean = clean[:5000]

    voice_name = resolve_voice(voice)
    m4a = out_base.with_suffix(".m4a")
    try:
        mp3 = out_base.with_suffix(".mp3")
        _edge_tts(clean, voice_name, mp3)
        # normalise container to m4a so the mixer sees a predictable format
        from app.ffmpeg_utils import ffmpeg, run

        run([ffmpeg(), "-y", "-i", str(mp3), "-c:a", "aac", "-b:a", "128k", str(m4a)], timeout=120)
        mp3.unlink(missing_ok=True)
        _log(f"AI voice ({voice_name}) rendered via edge-tts")
        return m4a
    except Exception as exc:  # noqa: BLE001 — fall back, then report
        _log(f"edge-tts unavailable ({exc}) — trying offline fallback")
        try:
            aiff = _say_fallback(clean, m4a)
            _log("AI voice rendered offline via `say`")
            return aiff
        except Exception as exc2:  # noqa: BLE001
            raise RuntimeError(
                f"AI voice failed: edge-tts ({exc}); say fallback ({exc2})"
            ) from exc2


def caption_text_for_window(words, start: float, end: float) -> str:
    """Join the transcript words inside [start, end) into speakable text."""
    parts = [w.text for w in words if start - 0.01 <= w.start < end - 0.01]
    return " ".join(parts)
