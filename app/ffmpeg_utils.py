"""Locate and drive ffmpeg / ffprobe.

Tries the system PATH first, then the `static-ffmpeg` package which downloads
platform binaries on first use, so the app works on machines without a
package manager.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


def _supports_ass(ffmpeg_path: str) -> bool:
    """True if this build includes the libass `ass` filter (needed to burn captions)."""
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return bool(re.search(r"^\s*\S+\s+ass\s+V->V\b", proc.stdout or "", re.MULTILINE))


def _static_binaries() -> tuple[str, str]:
    from static_ffmpeg import run as static_run

    paths = static_run.get_or_fetch_platform_executables_else_raise()
    if not paths or len(paths) < 2 or not paths[0] or not paths[1]:
        raise RuntimeError("static-ffmpeg returned no binaries")
    return str(paths[0]), str(paths[1])


@lru_cache(maxsize=1)
def binaries() -> tuple[str, str]:
    """Return absolute (ffmpeg, ffprobe) paths that support the `ass` filter.

    The system PATH wins, but only if its ffmpeg was built with libass — many
    minimal builds are not. Otherwise the bundled static build is used.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path and _supports_ass(ffmpeg_path):
        return ffmpeg_path, ffprobe_path

    system_unusable = bool(ffmpeg_path) and not _supports_ass(ffmpeg_path)
    try:
        static_ffmpeg, static_ffprobe = _static_binaries()
    except Exception as exc:  # pragma: no cover - depends on network/install
        if system_unusable:
            raise FFmpegError(
                f"{ffmpeg_path} was built without libass (no `ass` filter) and the "
                f"static-ffmpeg fallback failed: {exc}"
            ) from exc
        raise FFmpegError(
            "ffmpeg/ffprobe not found. Install ffmpeg with libass "
            f"(brew install ffmpeg) or `pip install static-ffmpeg`. Underlying error: {exc}"
        ) from exc

    if system_unusable:
        # tell the operator why we are ignoring their PATH ffmpeg
        import logging

        logging.getLogger("shorts").warning(
            "ffmpeg at %s has no libass `ass` filter — using bundled static build instead",
            ffmpeg_path,
        )
    if not _supports_ass(static_ffmpeg):
        raise FFmpegError("neither the system nor the static ffmpeg supports the `ass` filter")
    return static_ffmpeg, static_ffprobe


def ffmpeg() -> str:
    return binaries()[0]


def ffprobe() -> str:
    return binaries()[1]


def run(cmd: list[str], *, cwd: Path | str | None = None, timeout: float | None = None) -> str:
    """Run a command, return stdout, raise FFmpegError with stderr on failure."""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"command timed out after {timeout}s: {' '.join(map(str, cmd))}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-3000:]
        raise FFmpegError(f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n{tail}")
    return proc.stdout


def probe(path: Path | str) -> dict:
    """Return ffprobe JSON (format + streams) for a media file."""
    out = run(
        [
            ffprobe(), "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
    )
    return json.loads(out)


def duration_of(path: Path | str) -> float:
    info = probe(path)
    dur = info.get("format", {}).get("duration")
    if dur is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                dur = stream["duration"]
                break
    if dur is None:
        raise FFmpegError(f"could not determine duration of {path}")
    return float(dur)


def has_audio_stream(path: Path | str) -> bool:
    info = probe(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
