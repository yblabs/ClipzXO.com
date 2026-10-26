"""Stage 5: render a 9:16 vertical clip with captions burned in (single pass).

Optional extras layered onto the base render (each independently toggleable):
  * watermark — overlay a PNG (default asset, config path, or user-dragged
    file) at one of four corners for the full duration;
  * music     — mix a user/auto-picked track under the original audio;
  * AI voice  — the caption read-aloud track sits on top, original speaker ducked.
When none is used the command is exactly the original `-vf` invocation.

Input layout: #0 source video, then any of: #1 music, #2 AI voice,
#3 watermark — appended only when present (indices tracked, never assumed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app import config
from app.ffmpeg_utils import ffmpeg, run
from app.pipeline import audio_mix, watermark

_FILTER_SPECIALS = "\\' :,[]"

def escape_filter_path(path: Path | str) -> str:
    """Escape a filesystem path for use as a filter option value."""
    out = str(path)
    for ch in _FILTER_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


def build_filter(ass_path: Path | str) -> str:
    """Cover-crop to 9:16 (works for any source aspect) + burn captions."""
    w, h = config.TARGET_WIDTH, config.TARGET_HEIGHT
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"ass={escape_filter_path(ass_path)}"
    )


def build_filter_complex(
    ass_path: Path | str,
    *,
    watermark_path: Optional[Path] = None,
    music_path: Optional[Path] = None,
    voice_path: Optional[Path] = None,
    watermark_position: Optional[str] = None,
    clip_duration: float = 0.0,
) -> tuple[str, str, str]:
    """Build filter_complex: video (+watermark overlay) and audio (+voice/music).

    Returns (graph, video_label, audio_label). Audio label is "aout" when any
    audio mixing happens, else "0:a" (pass-through, used with -map 0:a).
    """
    w, h = config.TARGET_WIDTH, config.TARGET_HEIGHT
    parts: list[str] = []
    # --- video ---------------------------------------------------------------
    parts.append(
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"ass={escape_filter_path(ass_path)}[v]"
    )
    v_label = "v"
    next_index = 1
    if music_path is not None:
        next_index += 1  # music = #1
    if voice_path is not None:
        next_index += 1  # voice = next
    if watermark_path is not None:
        parts.append(
            watermark.overlay_fragment(
                next_index, label_in="v", label_out="vwm",
                position=watermark_position,
            )
        )
        v_label = "vwm"
    # --- audio ---------------------------------------------------------------
    music_idx = 1 if music_path is not None else None
    voice_idx = None
    if voice_path is not None:
        voice_idx = (2 if music_path is not None else 1)
    if music_idx is not None or voice_idx is not None:
        parts.append(
            audio_mix.combined_audio_fragment(
                clip_duration, music_index=music_idx, voice_index=voice_idx
            )
        )
        a_label = "aout"
    else:
        a_label = "0:a"
    return ";".join(parts), v_label, a_label


def build_command(
    video: Path,
    start: float,
    end: float,
    ass_path: Path,
    out_path: Path,
    *,
    watermark_path: Optional[Path] = None,
    music_path: Optional[Path] = None,
    voice_path: Optional[Path] = None,
    watermark_position: Optional[str] = None,
) -> list[str]:
    """Assemble the full ffmpeg argv (pure function — easy to unit test)."""
    duration = max(0.5, end - start)
    cmd: list[str] = [
        ffmpeg(), "-y",
        "-ss", f"{start:.3f}",
        "-i", str(video),
    ]
    if music_path is not None:
        cmd += ["-i", str(music_path)]
    if voice_path is not None:
        cmd += ["-i", str(voice_path)]
    if watermark_path is not None:
        cmd += ["-i", str(watermark_path)]
    # -t after ALL inputs: as an output option it caps the final clip length
    # (placed before an -i it would instead trim that input).
    cmd += ["-t", f"{duration:.3f}"]

    extras = (watermark_path is not None) or (music_path is not None) or (voice_path is not None)
    if not extras:
        # Original, proven invocation: single-pass -vf, no filter_complex.
        cmd += ["-vf", build_filter(ass_path)]
    else:
        graph, v_label, a_label = build_filter_complex(
            ass_path,
            watermark_path=watermark_path,
            music_path=music_path,
            voice_path=voice_path,
            watermark_position=watermark_position,
            clip_duration=duration,
        )
        cmd += ["-filter_complex", graph, "-map", f"[{v_label}]"]
        # ALWAYS map audio — with an explicit -map the default stream
        # selection is disabled, so unmapped audio would be dropped.
        if a_label == "0:a":
            cmd += ["-map", "0:a"]
        else:
            cmd += ["-map", f"[{a_label}]"]

    cmd += [
        "-c:v", "libx264",
        "-preset", config.VIDEO_PRESET,
        "-crf", str(config.VIDEO_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", config.AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def concat_clips(parts: list[Path], out_path: Path) -> Path:
    """Concatenate identically-encoded segments into one file (stream copy).

    Used for hook versions: [flash-forward teaser, full clip]. All parts are
    produced by `render_clip` with identical codec settings, so `-c copy` is
    safe and avoids a second re-encode.
    """
    if not parts:
        raise ValueError("concat_clips needs at least one part")
    listfile = out_path.with_suffix(out_path.suffix + ".list")
    lines = []
    for p in parts:
        resolved = str(Path(p).resolve())
        if "'" in resolved:
            raise ValueError(f"unsupported quote in path for concat: {resolved}")
        lines.append(f"file '{resolved}'\n")
    listfile.write_text("".join(lines), encoding="utf-8")
    cmd = [
        ffmpeg(), "-y",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c", "copy",
        str(out_path),
    ]
    run(cmd, timeout=600)
    return out_path


def render_clip(
    video: Path,
    start: float,
    end: float,
    ass_path: Path,
    out_path: Path,
    *,
    watermark_path: Optional[Path] = None,
    music_path: Optional[Path] = None,
    voice_path: Optional[Path] = None,
    watermark_position: Optional[str] = None,
) -> Path:
    """Re-encode [start, end) of `video` as a captioned vertical short."""
    cmd = build_command(
        video, start, end, ass_path, out_path,
        watermark_path=watermark_path,
        music_path=music_path,
        voice_path=voice_path,
        watermark_position=watermark_position,
    )
    run(cmd, timeout=1800)
    return out_path
