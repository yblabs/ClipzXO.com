"""Stage 4: captions — animated word-sync (default) or a stationary block.

Design note: instead of ASS karaoke (``\\k`` — which is a no-op in some libass
builds, verified empirically), we emit **one event per active word**. Every
event contains the *same* full caption line with the *same* styling, so the
text layout is byte-for-byte identical across events — the only difference is
which word carries the accent colour. Result: exactly one word is highlighted
at any instant, with zero layout jitter, plus a pop-in animation on the first
event of each caption line and soft fades at the edges.

Optional **static mode** (``CAPTION_MODE=static`` / the UI's Stationary
option): one fixed text block — written manually per job, or suggested from
the clip's own analysis — rendered as a single Dialogue event in an opaque
box for the whole clip, matching the white-box style used by big streamer
clip channels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.pipeline.transcribe import Word

MAX_SPAN_SECONDS = 5.0   # never let one caption line linger longer than this
PAUSE_GAP_SECONDS = 0.8  # a silence this long always starts a new line

# Static (stationary) subtitle: one text block fixed for the whole clip, in
# the white-box / black-text style used by 1M+ streamer clip channels.
STATIC_LINE_CHARS = 36   # wrapped line width before breaking to the next line
STATIC_MAX_LINES = 4     # longer text is cut with an ellipsis
STATIC_PAD = 10          # opaque-box padding around the text
STATIC_FADE_MS = 120     # barely-there fade so the block doesn't hard-pop


@dataclass
class Piece:
    """A word, timed relative to the clip start."""

    text: str
    start: float
    end: float


def _uppercase_enabled() -> bool:
    return str(getattr(config, "CAPTION_UPPERCASE", "1")).lower() not in {"0", "false", "no"}


def group_words(
    words: list[Word],
    clip_start: float,
    duration: float,
    *,
    max_words: int | None = None,
    max_chars: int | None = None,
) -> list[list[Piece]]:
    """Bucket word timestamps (absolute) into caption lines inside [0, duration]."""
    max_words = max_words or config.CAPTION_MAX_WORDS
    max_chars = max_chars or config.CAPTION_MAX_CHARS
    upper = _uppercase_enabled()

    pieces: list[Piece] = []
    for w in words:
        start = w.start - clip_start
        end = w.end - clip_start
        if end <= 0.0 or start >= duration:
            continue
        pieces.append(
            Piece(
                text=w.text.upper() if upper else w.text,
                start=max(0.0, start),
                end=min(duration, end),
            )
        )

    chunks: list[list[Piece]] = []
    current: list[Piece] = []
    for piece in pieces:
        if current:
            gap = piece.start - current[-1].end
            span = piece.end - current[0].start
            chars = sum(len(p.text) + 1 for p in current) + len(piece.text)
            if (
                len(current) >= max_words
                or chars > max_chars
                or span > MAX_SPAN_SECONDS
                or gap > PAUSE_GAP_SECONDS
            ):
                chunks.append(current)
                current = []
        current.append(piece)
    if current:
        chunks.append(current)
    return chunks


def _fmt_time(seconds: float) -> str:
    """ASS timestamps are h:mm:ss.cc (centiseconds), computed in integer cs."""
    cs = max(0, int(round(seconds * 100)))
    hours, rem = divmod(cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, csec = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{csec:02d}"


HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{secondary},{outline},&H80000000,-1,0,0,0,100,100,{spacing},0,1,{outline_w},0,{align},{ml},{mr},{mv},1
Style: Sub,{font},{sub_size},{sub_text},{sub_text},{sub_box},&H80000000,-1,0,0,0,100,100,0,0,3,{sub_pad},0,{align},{ml},{mr},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_colour(value: str) -> str:
    """Canonical ASS colour literal: ``&HAABBGGRR&`` (trailing ``&`` required
    inside override tags; harmless and spec-correct in style lines)."""
    v = str(value).strip()
    if v[:2].lower() != "&h":
        v = "&H" + v.lstrip("&")
    return v.rstrip("&") + "&"


def _header() -> str:
    return HEADER.format(
        w=config.TARGET_WIDTH,
        h=config.TARGET_HEIGHT,
        font=config.CAPTION_FONT,
        size=config.CAPTION_FONT_SIZE,
        primary=_ass_colour(config.CAPTION_PRIMARY),
        secondary=_ass_colour(config.CAPTION_HIGHLIGHT),
        outline=_ass_colour(config.CAPTION_OUTLINE),
        spacing=getattr(config, "CAPTION_LETTER_SPACING", 0),
        outline_w=int(config.CAPTION_OUTLINE_WIDTH),
        align=5,
        ml=40,
        mr=40,
        mv=config.CAPTION_POS_Y,
        sub_size=getattr(config, "STATIC_FONT_SIZE", 64),
        sub_text=_ass_colour(getattr(config, "STATIC_TEXT_COLOUR", "&H00000000")),
        sub_box=_ass_colour(getattr(config, "STATIC_BOX_COLOUR", "&H00FFFFFF")),
        sub_pad=STATIC_PAD,
    )


def build_ass(words: list[Word], clip_start: float, duration: float) -> str:
    """Full .ass script with word-highlight events for a clip."""
    lines = [_header()]
    chunks = group_words(words, clip_start, duration)

    for chunk in chunks:
        for idx, piece in enumerate(chunk):
            start = piece.start
            nxt = chunk[idx + 1].start if idx + 1 < len(chunk) else piece.end
            end = min(duration, max(nxt, start + 0.05))
            if start >= duration - 0.02:
                break

            is_first = idx == 0
            is_last = idx == len(chunk) - 1
            tags = [f"{{\\an5\\pos({config.CAPTION_POS_X},{config.CAPTION_POS_Y})}}"]
            if is_first:
                tags.append("{\\fad(%d,0)}" % config.CAPTION_FADE_IN)
                tags.append(
                    "{\\fscx%d\\fscy%d\\t(0,%d,\\fscx100\\fscy100)}"
                    % (config.CAPTION_POP_FROM, config.CAPTION_POP_FROM, config.CAPTION_POP_MS)
                )
            if is_last:
                tags.append("{\\fad(0,%d)}" % config.CAPTION_FADE_OUT)

            body: list[str] = []
            for j, p in enumerate(chunk):
                colour = config.CAPTION_HIGHLIGHT if j == idx else config.CAPTION_PRIMARY
                space = " " if j < len(chunk) - 1 else ""
                body.append("{\\1c" + _ass_colour(colour) + "}" + p.text + space)

            text = "".join(tags) + "".join(body)
            lines.append(
                f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},Caption,,0,0,0,,{text}"
            )
    return "\n".join(lines) + "\n"


def write_ass(path: Path, words: list[Word], clip_start: float, duration: float) -> Path:
    path.write_text(build_ass(words, clip_start, duration), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Static (stationary) subtitle — fixed text block for the whole clip
# --------------------------------------------------------------------------- #
def wrap_static(text: str, *, max_chars: int = STATIC_LINE_CHARS,
                max_lines: int = STATIC_MAX_LINES) -> list[str]:
    """Wrap one subtitle into short box lines; cut the tail with an ellipsis."""
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if cur and len(cand) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = cand
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur:  # overflow: the last line gets an ellipsis
        joined = lines[-1]
        lines[-1] = joined.rsplit(" ", 1)[0].rstrip() + "…"
    if not lines:
        lines = [""]
    return lines


def build_static_ass(text: str, duration: float) -> str:
    """One Dialogue event covering [0, duration] — no word events, no pop,
    no highlight sweep: the block just sits there, like the clip channels do."""
    lines = wrap_static(text)
    body = "\\N".join(lines)
    tags = (
        f"{{\\an5\\pos({config.CAPTION_POS_X},{config.CAPTION_POS_Y})"
        f"\\fad({STATIC_FADE_MS},{STATIC_FADE_MS})}}"
    )
    dialogue = (
        f"Dialogue: 0,{_fmt_time(0.0)},{_fmt_time(max(0.0, duration))},"
        f"Sub,,0,0,0,,{tags}{body}"
    )
    return "\n".join([_header(), dialogue]) + "\n"


def write_static_ass(path: Path, text: str, duration: float) -> Path:
    path.write_text(build_static_ass(text, duration), encoding="utf-8")
    return path


def suggest_subtitle(words: list[Word], clip_start: float, end: float,
                     *, title: str = "") -> str:
    """Analyse the clip and propose one stationary line.

    Priority: the curated highlight title (the analysis product — an LLM
    provider writes real summaries, the heuristic extracts a salient
    sentence), else the detected payoff quote, else the opening words.
    Trimmed so it fits the box.
    """
    from app.pipeline.hooks import detect_payoff, topic_phrase

    t = re.sub(r"\s+", " ", str(title or "")).strip(" .!?,;:")
    if len(t.split()) >= 3:
        candidate = t
    else:
        quote, _ = detect_payoff(words, clip_start, end)
        candidate = quote.strip() or topic_phrase(title, words, clip_start, end)
    cap = STATIC_LINE_CHARS * STATIC_MAX_LINES
    if len(candidate) > cap:
        candidate = candidate[:cap].rsplit(" ", 1)[0].rstrip() + "…"
    return candidate or "Watch this"
