"""Optional music + watermark: resolution, filter fragments, render command."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.pipeline import audio_mix, render, watermark
from app.pipeline.render import build_command, build_filter_complex


# --------------------------------------------------------------------------- #
# Music resolution
# --------------------------------------------------------------------------- #
def test_resolve_music_off_by_default():
    assert audio_mix.resolve_music(None) is None
    assert audio_mix.resolve_music("") is None
    assert audio_mix.resolve_music("off") is None


def test_resolve_music_auto_picks_from_library(tmp_path, monkeypatch):
    for name in ("b.mp3", "a.wav"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not audio")
    picked = audio_mix.resolve_music("auto", tmp_path)
    assert picked is not None and picked.suffix in config.MUSIC_SUFFIXES
    # round-robin differs by index
    assert audio_mix.resolve_music("auto", tmp_path, auto_index=1) != picked


def test_resolve_music_auto_empty_library_is_none(tmp_path):
    assert audio_mix.resolve_music("auto", tmp_path) is None


def test_resolve_music_manual_path(tmp_path):
    track = tmp_path / "my_song.mp3"
    track.write_bytes(b"x")
    assert audio_mix.resolve_music(str(track)) == track


def test_resolve_music_manual_library_filename(tmp_path):
    track = tmp_path / "drop.mp3"
    track.write_bytes(b"x")
    assert audio_mix.resolve_music("drop.mp3", tmp_path) == track


def test_resolve_music_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        audio_mix.resolve_music(str(tmp_path / "nope.mp3"))


def test_resolve_music_bad_extension_raises(tmp_path):
    bad = tmp_path / "song.xyz"
    bad.write_bytes(b"x")
    with pytest.raises(ValueError):
        audio_mix.resolve_music(str(bad))


# --------------------------------------------------------------------------- #
# Mix fragment
# --------------------------------------------------------------------------- #
def test_mix_fragment_uses_inputs_and_levels():
    frag = audio_mix.mix_fragment(1, 30.0)
    assert "[1:a]atrim=" in frag          # music is input #1, trimmed to clip
    assert "[0:a]volume=" in frag          # original audio kept (ducked)
    assert "amix=inputs=2:duration=first[aout]" in frag
    assert f"volume={config.MUSIC_VOLUME:.3f}" in frag


def test_mix_fragment_respects_config_levels(monkeypatch):
    monkeypatch.setattr(config, "MUSIC_VOLUME", 0.9)
    monkeypatch.setattr(config, "ORIGINAL_VOLUME", 0.0)
    monkeypatch.setattr(config, "MUSIC_OFFSET", 12.5)
    frag = audio_mix.mix_fragment(1, 20.0)
    assert "volume=0.900" in frag
    assert "volume=0.000" in frag
    assert "start=12.500" in frag


# --------------------------------------------------------------------------- #
# Watermark
# --------------------------------------------------------------------------- #
def test_resolve_watermark_off_and_default(monkeypatch):
    monkeypatch.setattr(config, "WATERMARK_DEFAULT_ON", True)
    assert watermark.resolve_watermark(False) is None


def test_resolve_watermark_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATERMARK_PATH", tmp_path / "absent.png")
    assert watermark.resolve_watermark(True) is None


def test_resolve_watermark_finds_file(tmp_path, monkeypatch):
    wm = tmp_path / "wm.png"
    wm.write_bytes(b"\x89PNG")
    monkeypatch.setattr(config, "WATERMARK_PATH", wm)
    assert watermark.resolve_watermark(None) == wm


def test_overlay_fragment_keeps_watermark_in_safe_zone():
    frag = watermark.overlay_fragment(2)
    expected_w = int(round(config.TARGET_WIDTH * config.WATERMARK_WIDTH_FRAC))
    assert f"[2:v]scale={expected_w}:-1[wm]" in frag
    # centred horizontally, bottom edge above the bottom 15% platform-UI zone
    assert "overlay=x=(W-w)/2" in frag
    assert f"y=H-h-H*{config.WATERMARK_BOTTOM_MARGIN:g}" in frag
    assert frag.endswith("[vwm]")


# --------------------------------------------------------------------------- #
# Render command
# --------------------------------------------------------------------------- #
def _paths(tmp_path: Path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"x")
    ass = tmp_path / "clip.ass"
    ass.write_text("[Events]")
    out = tmp_path / "clip_out.mp4"
    wm = tmp_path / "wm.png"
    wm.write_bytes(b"\x89PNG")
    music = tmp_path / "song.mp3"
    music.write_bytes(b"x")
    return video, ass, out, wm, music


def test_build_command_plain_uses_vf_only(tmp_path):
    video, ass, out, wm, music = _paths(tmp_path)
    cmd = build_command(video, 10, 40, ass, out)
    assert "-filter_complex" not in cmd
    assert "-vf" in cmd
    assert cmd[cmd.index("-ss") + 1] == "10.000"
    assert cmd[cmd.index("-t") + 1] == "30.000"
    assert "-i" not in cmd[cmd.index("-t"):cmd.index("-vf")], "no extra inputs expected"


def test_build_command_with_watermark_uses_filter_complex(tmp_path):
    video, ass, out, wm, music = _paths(tmp_path)
    cmd = build_command(video, 0, 25, ass, out, watermark_path=wm)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "-vf" not in cmd
    assert "[0:v]scale=" in graph and "ass=" in graph
    assert "[3:v]scale=" not in graph  # watermark is input #1 (no music)
    assert "[1:v]scale=" in graph
    assert "[v][wm]overlay=" in graph
    assert cmd[cmd.index("-map") + 1] == "[vwm]"
    # audio must be explicitly mapped too — an explicit -map on video only
    # would silently drop the audio stream.
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[vwm]", "0:a"]
    assert str(wm) in cmd


def test_build_command_with_music_and_watermark_indexes(tmp_path):
    video, ass, out, wm, music = _paths(tmp_path)
    cmd = build_command(video, 5, 35, ass, out, watermark_path=wm, music_path=music)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert str(music) in cmd and str(wm) in cmd
    # music = input #1, watermark = input #2
    assert "[1:a]atrim=" in graph
    assert "[2:v]scale=" in graph
    assert "[v][wm]overlay=" in graph
    assert "[vwm]" in cmd and "[aout]" in cmd
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[vwm]", "[aout]"]


def test_build_command_voice_only_maps_audio_and_ducks(tmp_path):
    """Voice without music/watermark still mixes + maps audio."""
    video, ass, out, wm, music = _paths(tmp_path)
    voice = tmp_path / "tts.m4a"
    voice.write_bytes(b"x")
    cmd = build_command(video, 0, 20, ass, out, voice_path=voice)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert str(voice) in cmd
    assert "[1:a]atrim=" in graph          # voice = input #1
    assert "volume=" in graph
    assert "amix=inputs=2:duration=first[aout]" in graph  # orig + voice
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[v]", "[aout]"]
    # no watermark -> video label is the plain chain output
    assert "overlay" not in graph


def test_build_command_all_extras_index_layout(tmp_path):
    """music=#1, voice=#2, watermark=#3 — indices must line up."""
    video, ass, out, wm, music = _paths(tmp_path)
    voice = tmp_path / "tts.m4a"
    voice.write_bytes(b"x")
    cmd = build_command(
        video, 2, 32, ass, out,
        music_path=music, voice_path=voice, watermark_path=wm,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:a]atrim=" in graph and "[amusic]" in graph
    assert "[2:a]atrim=" in graph and "[avoice]" in graph
    assert "[3:v]scale=" in graph
    assert "amix=inputs=3:duration=first[aout]" in graph
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[vwm]", "[aout]"]


def test_build_filter_complex_labels_without_watermark(tmp_path):
    video, ass, out, wm, music = _paths(tmp_path)
    graph, v_label, a_label = build_filter_complex(ass, music_path=music, clip_duration=20.0)
    assert v_label == "v"          # no overlay applied -> chain output stays "v"
    assert a_label == "aout"
    assert "[vwm]" not in graph
    assert "[2:v]scale=" not in graph


def test_render_clip_end_to_end_with_music_and_watermark(tmp_path):
    """Actually run ffmpeg on a synthetic clip (skipped if ffmpeg is unavailable)."""
    from app.ffmpeg_utils import FFmpegError, ffmpeg, run

    try:
        ff = ffmpeg()
    except FFmpegError:  # pragma: no cover
        pytest.skip("ffmpeg not available")

    video = tmp_path / "src.mp4"
    try:
        run([ff, "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], timeout=120)
        music = tmp_path / "song.m4a"
        run([ff, "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
             "-c:a", "aac", str(music)], timeout=120)
    except FFmpegError as exc:  # pragma: no cover
        pytest.skip(f"cannot build test media: {exc}")

    from PIL import Image
    wm = tmp_path / "wm.png"
    Image.new("RGBA", (200, 80), (255, 0, 0, 200)).save(wm)

    ass = tmp_path / "c.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,HELLO\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.mp4"
    render.render_clip(video, 1, 5, ass, out, watermark_path=wm, music_path=music)
    assert out.is_file() and out.stat().st_size > 0

    from app.ffmpeg_utils import probe
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (config.TARGET_WIDTH, config.TARGET_HEIGHT)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
