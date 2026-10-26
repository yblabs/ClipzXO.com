"""Watermark placement, user-dragged watermark files, and the AI voice (TTS)."""
from __future__ import annotations

import pytest

from app import config
from app.pipeline import tts, watermark
from app.pipeline.audio_mix import combined_audio_fragment, voice_fragment
from tests.helpers import make_transcript


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def test_normalize_position_aliases_and_default():
    assert watermark.normalize_position(None) == config.WATERMARK_POSITION
    assert watermark.normalize_position("top left") == "top-left"
    assert watermark.normalize_position("TOP-LEFT") == "top-left"
    assert watermark.normalize_position("bottom_right") == "bottom-right"
    with pytest.raises(ValueError):
        watermark.normalize_position("middle")


@pytest.mark.parametrize(
    "pos,x,y",
    [
        ("top-left", "W*0.04", "H*0.1"),
        ("top-right", "W-w-W*0.04", "H*0.1"),
        ("bottom-left", "W*0.04", "H-h-H*0.15"),
        ("bottom-right", "W-w-W*0.04", "H-h-H*0.15"),
        ("bottom-center", "(W-w)/2", "H-h-H*0.15"),
    ],
)
def test_position_expressions_keep_safe_margins(pos, x, y):
    gx, gy = watermark.position_expressions(pos)
    assert gx == x
    assert gy == y


def test_overlay_fragment_uses_requested_corner():
    frag = watermark.overlay_fragment(2, position="top-right")
    assert "x=W-w-W*0.04" in frag
    assert "y=H*0.1" in frag
    assert frag.endswith("[vwm]")


def test_top_positions_sit_below_platform_header():
    """Top placements must not enter the top 10% where Reels UI sits."""
    for pos in ("top-left", "top-right"):
        _, gy = watermark.position_expressions(pos)
        assert gy == f"H*{config.WATERMARK_TOP_MARGIN:g}"


def test_bottom_positions_clear_bottom_15pct():
    for pos in ("bottom-left", "bottom-right", "bottom-center"):
        _, gy = watermark.position_expressions(pos)
        assert gy == f"H-h-H*{config.WATERMARK_BOTTOM_MARGIN:g}"


# --------------------------------------------------------------------------- #
# User-dragged watermark file
# --------------------------------------------------------------------------- #
def test_resolve_watermark_file_empty_uses_default(tmp_path, monkeypatch):
    default = tmp_path / "watermark.png"
    default.write_bytes(b"\x89PNG")
    monkeypatch.setattr(config, "WATERMARK_PATH", default)
    assert watermark.resolve_watermark_file("") == default
    assert watermark.resolve_watermark_file(None) == default


def test_resolve_watermark_file_finds_uploaded_name(tmp_path, monkeypatch):
    up = tmp_path / "mylogo.png"
    up.write_bytes(b"\x89PNG")
    monkeypatch.setattr(config, "WATERMARK_DIR", tmp_path)
    assert watermark.resolve_watermark_file("mylogo.png") == up


def test_resolve_watermark_file_accepts_absolute_path(tmp_path):
    up = tmp_path / "brand.png"
    up.write_bytes(b"\x89PNG")
    assert watermark.resolve_watermark_file(str(up)) == up


def test_resolve_watermark_file_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATERMARK_DIR", tmp_path)
    monkeypatch.setattr(config, "WATERMARK_PATH", tmp_path / "absent.png")
    with pytest.raises(FileNotFoundError):
        watermark.resolve_watermark_file("nope.png")


# --------------------------------------------------------------------------- #
# AI voice
# --------------------------------------------------------------------------- #
def test_list_voices_multiple_options():
    voices = tts.list_voices()
    assert len(voices) >= 2, "user asked for multiple voice options"
    # labels are UI-friendly, values are engine voice names
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in voices.items())
    assert any("aria" == k for k in voices)


def test_resolve_voice_label_and_raw():
    label = next(iter(tts.list_voices()))
    assert tts.resolve_voice(label) == tts.list_voices()[label]
    assert tts.resolve_voice(label.upper()) == tts.list_voices()[label]
    # raw engine name passes through
    raw = "en-US-AriaNeural"
    assert tts.resolve_voice(raw) == tts.list_voices().get("aria", raw)
    with pytest.raises(ValueError):
        tts.resolve_voice("  ")


def test_caption_text_for_window_joins_words():
    tr = make_transcript([("hello brave new world", 10.0, 14.0)])
    text = tts.caption_text_for_window(tr.words, 10.0, 14.0)
    assert text == "hello brave new world"
    assert tts.caption_text_for_window(tr.words, 50.0, 60.0) == ""


def test_synthesize_empty_text_raises(tmp_path):
    with pytest.raises(ValueError):
        tts.synthesize("   ", "aria", tmp_path / "tts")


def test_synthesize_offline_fallback_or_success(tmp_path, monkeypatch):
    """Happy path: edge-tts if network, else say; both unavailable -> RuntimeError.

    Force the offline path deterministically so the test doesn't depend on
    the network: make edge-tts raise, keep `say` (macOS) if present.
    """
    import app.pipeline.tts as tts_mod

    def _boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(tts_mod, "_edge_tts", _boom)
    has_say = any(p.name == "say" for p in __import__("pathlib").Path("/usr/bin").glob("say"))
    if not has_say:
        with pytest.raises(RuntimeError, match="AI voice failed"):
            tts.synthesize("hello there", "aria", tmp_path / "tts")
        return
    out = tts.synthesize("hello there world", "aria", tmp_path / "tts")
    assert out.is_file() and out.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Combined audio graph
# --------------------------------------------------------------------------- #
def test_voice_fragment_trims_and_labels():
    frag = voice_fragment(2, 30.0)
    assert "[2:a]atrim=start=0:duration=30.000" in frag
    assert frag.endswith("[avoice]")
    assert "afade=t=in:d=0.10" in frag


def test_combined_graph_ducks_original_when_voice_present():
    frag = combined_audio_fragment(30.0, voice_index=1)
    assert f"volume={config.TTS_ORIGINAL_VOLUME:.3f}" in frag
    assert "amix=inputs=2:duration=first[aout]" in frag


def test_combined_graph_three_layers():
    frag = combined_audio_fragment(25.0, music_index=1, voice_index=2)
    assert "amix=inputs=3:duration=first[aout]" in frag
    assert "[aorig]" in frag and "[amusic]" in frag and "[avoice]" in frag
    # music level still config-driven
    assert f"volume={config.MUSIC_VOLUME:.3f}" in frag


def test_combined_graph_original_only_copies():
    frag = combined_audio_fragment(20.0)
    assert frag.endswith("[aout]")
    assert "acopy" in frag
