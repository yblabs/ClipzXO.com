"""Optional watermark: resolve the PNG and build the ffmpeg overlay fragment.

Placement options: top-left, top-right, bottom-left, bottom-right (and the
classic bottom-center kept as the config default). Every placement keeps the
mark out of the platform-UI zones: top 10% and bottom 15% of the frame
(where TikTok / Reels / Shorts put their buttons), ~35% of video width.
The watermark file can be the bundled default, a config path, or a file the
user dragged in via the UI / API (stored in config.WATERMARK_DIR).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app import config

# Aliases so "topleft", "top left", "bottom_right"… all resolve.
_ALIASES = {
    "topleft": "top-left", "top left": "top-left",
    "topright": "top-right", "top right": "top-right",
    "bottomleft": "bottom-left", "bottom left": "bottom-left",
    "bottomright": "bottom-right", "bottom right": "bottom-right",
    "bottomcenter": "bottom-center", "bottom center": "bottom-center",
    "bottom": "bottom-center", "center": "bottom-center",
}


def normalize_position(position: Optional[str]) -> str:
    """Map any user spelling onto a supported position name."""
    if not position:
        return config.WATERMARK_POSITION
    key = str(position).strip().lower()
    key = _ALIASES.get(key.replace("-", " ").replace("_", " ").strip(), _ALIASES.get(key, key))
    if key not in config.WATERMARK_POSITIONS:
        raise ValueError(
            f"unknown watermark position '{position}' — expected one of "
            f"{', '.join(config.WATERMARK_POSITIONS)}"
        )
    return key


def resolve_watermark_file(name_or_path: Optional[str]) -> Optional[Path]:
    """Resolve a user-dragged watermark: a name in WATERMARK_DIR, a path, or None.

    None/"" falls back to the config default (assets/watermark.png).
    Raises FileNotFoundError when the user explicitly named a file that is
    missing — silently dropping their watermark would be wrong.
    """
    if not name_or_path or not str(name_or_path).strip():
        wm = Path(config.WATERMARK_PATH)
        return wm if wm.is_file() else None
    value = str(name_or_path).strip()
    for candidate in (
        Path(value).expanduser(),
        Path(config.WATERMARK_DIR) / value,
        Path(config.WATERMARK_PATH),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"watermark file not found: {value}")


def resolve_watermark(enabled: Optional[bool] = None, path: Optional[Path] = None) -> Optional[Path]:
    """Return the watermark PNG to use, or None when watermarks are off/missing.

    `enabled=None` falls back to config.WATERMARK_DEFAULT_ON.
    """
    if enabled is None:
        enabled = config.WATERMARK_DEFAULT_ON
    if not enabled:
        return None
    wm = Path(path) if path else Path(config.WATERMARK_PATH)
    return wm if wm.is_file() else None


def position_expressions(position: str) -> tuple[str, str]:
    """(x, y) overlay expressions for a placement, honouring the safe margins."""
    pos = normalize_position(position)
    h_margin = f"W*{config.WATERMARK_H_MARGIN:g}"
    top = f"H*{config.WATERMARK_TOP_MARGIN:g}"
    bottom = f"H-h-H*{config.WATERMARK_BOTTOM_MARGIN:g}"
    if pos == "top-left":
        return h_margin, top
    if pos == "top-right":
        return f"W-w-{h_margin}", top
    if pos == "bottom-left":
        return h_margin, bottom
    if pos == "bottom-right":
        return f"W-w-{h_margin}", bottom
    return "(W-w)/2", bottom  # bottom-center


def overlay_fragment(
    wm_index: int,
    label_in: str = "v",
    label_out: str = "vwm",
    position: Optional[str] = None,
) -> str:
    """Semicolon-joined filter_complex fragment overlaying input #wm_index.

    Produces: [N:v]scale=W*frac:-1[wm];[label_in][wm]overlay=...[label_out]
    """
    wm_w = int(round(config.TARGET_WIDTH * config.WATERMARK_WIDTH_FRAC))
    x, y = position_expressions(position)
    return (
        f"[{wm_index}:v]scale={wm_w}:-1[wm];"
        f"[{label_in}][wm]overlay=x={x}:y={y}:eval=init[{label_out}]"
    )
