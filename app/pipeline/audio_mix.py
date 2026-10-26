"""Audio mixing: background music + AI voice + original speech in one graph.

Three optional sources layered over the clip's own audio (input #0):
  * music    — user file or auto-pick from config.MUSIC_DIR (mix_fragment);
  * AI voice — caption read-aloud from app.pipeline.tts (voice_fragment);
Both end in the "aout" label. Original audio is always kept, ducked to
config.ORIGINAL_VOLUME — or config.TTS_ORIGINAL_VOLUME when the AI voice is
speaking, so the two speech tracks don't fight.

The app never downloads or bundles copyrighted songs — the user supplies the
files (or lets "AI picks one" choose from their local library).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app import config


def list_library(music_dir: Optional[Path] = None) -> list[Path]:
    """Audio files the user dropped into the local music library, sorted."""
    root = Path(music_dir) if music_dir else Path(config.MUSIC_DIR)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in config.MUSIC_SUFFIXES
    )


def pick_track(music_dir: Optional[Path] = None, *, offset: int = 0) -> Optional[Path]:
    """Auto mode: choose a track from the local library (round-robin by offset).

    Returns None when the library is empty — callers should fall back to no
    music rather than fail the render.
    """
    tracks = list_library(music_dir)
    if not tracks:
        return None
    return tracks[offset % len(tracks)]


def resolve_music(music: Optional[str], music_dir: Optional[Path] = None,
                  *, auto_index: int = 0) -> Optional[Path]:
    """Resolve the `music` option to a concrete audio file (or None).

    Accepted values:
      "" / None / "off"  -> no music
      "auto" / "ai"      -> pick from the local library
      any other string   -> a path to the user's own audio file, or a bare
                             filename inside the local library (must exist)
    Raises FileNotFoundError for an explicit track that doesn't exist — the
    user asked for that track, so silently dropping it would be wrong.
    """
    if not music:
        return None
    value = str(music).strip()
    if not value or value.lower() in {"off", "none", "false"}:
        return None
    if value.lower() in {"auto", "ai"}:
        return pick_track(music_dir, offset=auto_index)
    path = Path(value).expanduser()
    if not path.is_file():
        # also accept a bare filename from the local library
        lib_candidate = (Path(music_dir) if music_dir else Path(config.MUSIC_DIR)) / value
        if lib_candidate.is_file():
            path = lib_candidate
    if not path.is_file():
        raise FileNotFoundError(f"music file not found: {path}")
    if path.suffix.lower() not in config.MUSIC_SUFFIXES:
        raise ValueError(
            f"unsupported music type '{path.suffix}' — expected {sorted(config.MUSIC_SUFFIXES)}"
        )
    return path


def mix_fragment(music_index: int, clip_duration: float) -> str:
    """Fragment mixing input #music_index under the clip audio -> label "aout".

    Kept for the music-only path: trim the track to the clip length, apply
    volume, a short fade-out, then amix with the original audio (input 0).
    `MUSIC_OFFSET` lets the user start the track at the drop.
    """
    vol = max(0.0, float(config.MUSIC_VOLUME))
    orig = max(0.0, float(config.ORIGINAL_VOLUME))
    offset = max(0.0, float(config.MUSIC_OFFSET))
    dur = max(0.1, float(clip_duration))
    fade = min(1.5, dur / 4)
    music_chain = (
        f"[{music_index}:a]atrim=start={offset:.3f}:duration={dur:.3f},"
        f"asetpts=PTS-STARTPTS,volume={vol:.3f},"
        f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}[amusic]"
    )
    orig_chain = f"[0:a]volume={orig:.3f}[aorig]"
    # duration=first ends the mix with the (full-length) original track.
    return f"{music_chain};{orig_chain};[aorig][amusic]amix=inputs=2:duration=first[aout]"


def voice_fragment(voice_index: int, clip_duration: float) -> str:
    """Fragment for input #voice_index (AI read-aloud) -> label "avoice".

    Trimmed to the clip length so a slightly longer TTS render can't extend
    the mix past the video.
    """
    dur = max(0.1, float(clip_duration))
    vol = max(0.0, float(config.TTS_VOLUME))
    fade = min(1.0, dur / 6)
    return (
        f"[{voice_index}:a]atrim=start=0:duration={dur:.3f},"
        f"asetpts=PTS-STARTPTS,volume={vol:.3f},"
        f"afade=t=in:d=0.10,afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}[avoice]"
    )


def combined_audio_fragment(
    clip_duration: float,
    *,
    music_index: Optional[int] = None,
    voice_index: Optional[int] = None,
) -> str:
    """Full audio graph: original (ducked) + optional AI voice + optional music.

    Order of layers in the amix: [aorig] first (duration anchor), then voice,
    then music — so the AI voice sits on top and music sits at the bottom.
    When the AI voice is present the original speaker ducks further
    (TTS_ORIGINAL_VOLUME) to avoid double speech.
    """
    dur = max(0.1, float(clip_duration))
    parts: list[str] = []
    inputs: list[str] = []

    orig_vol = (
        config.TTS_ORIGINAL_VOLUME if voice_index is not None else config.ORIGINAL_VOLUME
    )
    parts.append(f"[0:a]volume={max(0.0, float(orig_vol)):.3f}[aorig]")
    inputs.append("[aorig]")

    if voice_index is not None:
        parts.append(voice_fragment(voice_index, dur))
        inputs.append("[avoice]")
    if music_index is not None:
        vol = max(0.0, float(config.MUSIC_VOLUME))
        offset = max(0.0, float(config.MUSIC_OFFSET))
        fade = min(1.5, dur / 4)
        parts.append(
            f"[{music_index}:a]atrim=start={offset:.3f}:duration={dur:.3f},"
            f"asetpts=PTS-STARTPTS,volume={vol:.3f},"
            f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}[amusic]"
        )
        inputs.append("[amusic]")

    if len(inputs) == 1:
        parts.append("[aorig]acopy[aout]")
    else:
        parts.append(
            "".join(inputs) + f"amix=inputs={len(inputs)}:duration=first[aout]"
        )
    return ";".join(parts)
