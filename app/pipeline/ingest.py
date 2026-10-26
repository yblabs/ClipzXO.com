"""Stage 1: get a source video on disk (upload or YouTube URL) + extract audio."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from app import config
from app.ffmpeg_utils import FFmpegError, duration_of, ffmpeg, has_audio_stream, is_url, probe, run

LogFn = Callable[[str], None]

MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".mts", ".wmv", ".flv"}
_KEEP = shutil.ignore_patterns("*.part", "*.ytdl", "*.temp.*")

# kind = "video" | "audio", done/total in bytes (total=0 when unknown)
DownloadProgressFn = Callable[[str, float, float], None]

UNCAPPED_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"


def build_format_selector(max_height: int | None) -> str:
    """yt-dlp format ladder capped to `max_height` (None/0 = uncapped).

    The first tier keeps landscape sources at ≤ max_height (16:9 1080p for
    the default). The second tier doubles the cap so portrait 9:16 sources
    still get a full 1080×1920 frame instead of being squeezed by the
    landscape cap. The final fallback grabs anything, so odd sites without
    height metadata still resolve.
    """
    if not max_height or max_height <= 0:
        return UNCAPPED_FORMAT
    tiers: list[str] = []
    for cap in (max_height, max_height * 2):
        tiers += [
            f"bv*[height<={cap}][ext=mp4]+ba[ext=m4a]",
            f"b[height<={cap}][ext=mp4]",
            f"bv*[height<={cap}]+ba",
            f"b[height<={cap}]",
        ]
    tiers.append("bv*+ba/b")
    return "/".join(tiers)


def _download_hook(on_progress: DownloadProgressFn) -> Callable[[dict], None]:
    """yt-dlp progress hook: normalise hook dicts to (kind, done, total)."""

    def _hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        info = d.get("info_dict") or {}
        kind = "audio" if info.get("vcodec") == "none" else "video"
        total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        done = float(d.get("downloaded_bytes") or 0)
        on_progress(kind, done, total)

    return _hook


def _noop(_: str) -> None:  # pragma: no cover
    pass


def download_video(
    url: str,
    workdir: Path,
    log: LogFn = _noop,
    on_progress: DownloadProgressFn | None = None,
) -> Path:
    """Download a YouTube (or yt-dlp supported) URL as an mp4 into `workdir`."""
    import yt_dlp

    log(f"Downloading video from {url}")
    opts = {
        "format": build_format_selector(config.DOWNLOAD_MAX_HEIGHT),
        "outtmpl": str(workdir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "overwrites": True,
    }
    if on_progress is not None:
        opts["progress_hooks"] = [_download_hook(on_progress)]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        raise RuntimeError(f"yt-dlp failed for {url}: {exc}") from exc

    candidates = [
        p
        for p in sorted(workdir.glob("source.*"))
        if p.suffix.lower() in MEDIA_SUFFIXES and p.is_file()
    ]
    if not candidates:
        raise RuntimeError(f"yt-dlp finished but no video file found in {workdir}")
    downloaded = candidates[0]
    log(f"Downloaded {downloaded.name} ({downloaded.stat().st_size / 1e6:.1f} MB)")
    return downloaded


def stage_source(
    source: str,
    workdir: Path,
    log: LogFn = _noop,
    on_progress: DownloadProgressFn | None = None,
) -> Path:
    """Resolve `source` (a local path or an http(s) URL) to a local video file."""
    if is_url(source):
        return download_video(source, workdir, log, on_progress=on_progress)

    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    if path.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError(
            f"unsupported file type '{path.suffix}' — expected one of {sorted(MEDIA_SUFFIXES)}"
        )
    local = workdir / f"input{path.suffix.lower()}"
    if local.resolve() != path:
        shutil.copy2(path, local)
    log(f"Using local file {path.name} ({local.stat().st_size / 1e6:.1f} MB)")
    return local


def extract_audio(video: Path, wav: Path, log: LogFn = _noop) -> Path:
    """Extract mono 16 kHz WAV — the exact format Whisper expects."""
    if not has_audio_stream(video):
        raise FFmpegError(f"no audio stream found in {video.name}; cannot transcribe")
    log("Extracting audio track (16 kHz mono WAV)")
    run(
        [
            ffmpeg(), "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(wav),
        ]
    )
    return wav


def probe_video(video: Path) -> dict:
    info = probe(video)
    duration = duration_of(video)
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    return {
        "duration": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": _parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
    }


def _parse_rate(value: str | None) -> float:
    if not value or "/" not in value:
        try:
            return float(value) if value else 0.0
        except ValueError:
            return 0.0
    num, den = value.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0
