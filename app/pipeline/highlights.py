"""Stage 3: pick the most engaging 30–60s segments.

Two providers:
  * LLM  — GPT-4o-mini or Claude get the timestamped transcript and return a
            JSON array of {start, end, title} highlights (with a robust parser
            and a normaliser that snaps times to word boundaries and enforces
            the 30–60s / non-overlap rules).
  * heuristic — no API key required; scores sentences for "hook" signals
            (questions, exclamation, power words, numbers, speech rate) and
            greedily picks the best non-overlapping windows.
"""
from __future__ import annotations

import json
import os
import re
from bisect import bisect_left
from typing import Callable, Optional

from app import config
from app.models import Highlight, HighlightSelection
from app.pipeline.transcribe import Transcript, Word

LogFn = Callable[[str], None]

SYSTEM_PROMPT = (
    "You are an expert short-form video editor who selects viral-worthy clips "
    "from long-form video transcripts for TikTok, Reels and YouTube Shorts."
)

# Absolute floor: never cut a clip shorter than this, even when the user asks
# for more clips than a 30–60s band can fit (Shorts/Reels live happily on
# ~8–15s clips; below that a clip is just a blip).
# HARD RULE: the number the user picks on the slider is ALWAYS honoured —
# clips shrink to fit, the count is never reduced. This is the absolute
# shortest a clip may get before it stops being a usable Short.
MIN_CLIP_SECONDS = 3.0

# Standard band when the source is long enough for the requested count.
SEP_GAP = 3.0  # required gap between two chosen clips


def adaptive_band(duration: float, count: int) -> tuple[float, float, float, int]:
    """Clip-length band + separation gap + count that actually fit the source.

    The configured 30–60s band is ideal, but a short source asked to produce
    many clips (e.g. an 82s video → 9 clips) can't hold count × 30s without
    overlap. In that case the band shrinks toward (duration − gaps)/count so
    the REQUESTED COUNT is always honoured with shorter clips.

    Returns (min_seconds, max_seconds, gap, count). Long sources keep the
    configured band and the 3s separation unchanged.
    """
    lo = config.MIN_HIGHLIGHT_SECONDS
    hi = config.MAX_HIGHLIGHT_SECONDS
    count = max(1, int(count))
    if not duration or duration >= count * lo:
        return lo, hi, SEP_GAP, count  # plenty of footage — standard band
    # not enough footage: shrink so `count` clips + (count-1) gaps fit.
    # The COUNT is never touched — it is what the user asked for.
    gap = min(SEP_GAP, max(0.35, duration / count * 0.12))
    usable = max(duration - gap * (count - 1), duration * 0.5)
    target = usable / count
    floor = min(MIN_CLIP_SECONDS, duration)
    lo_new = max(floor, min(lo, target * 0.8))
    hi_new = max(lo_new + 1.0, min(hi, target * 1.3))
    return lo_new, hi_new, gap, count


HOOK_WORDS = {
    "secret", "secrets", "mistake", "mistakes", "never", "nobody", "stop", "truth",
    "actually", "imagine", "reason", "biggest", "shocking", "crazy", "insane",
    "amazing", "proven", "hack", "hacks", "trick", "tricks", "tip", "tips",
    "important", "warning", "listen", "free", "money", "best", "worst", "why",
    "how", "what happens", "you need", "you won't", "believe", "insane", "hack",
    "rule", "rules", "change", "changed", "everything", "wrong", "right way",
    "guaranteed", "problems", "problem", "solution", "simple", "easy", "instantly",
}


def build_prompt(
    transcript: Transcript, min_count: int, max_count: int,
    min_seconds: float | None = None, max_seconds: float | None = None,
) -> str:
    """Timestamped transcript + instructions, ready to send to an LLM."""
    min_seconds = min_seconds if min_seconds is not None else config.MIN_HIGHLIGHT_SECONDS
    max_seconds = max_seconds if max_seconds is not None else config.MAX_HIGHLIGHT_SECONDS
    lines: list[str] = []
    buf: list[str] = []
    buf_start: Optional[float] = None
    for seg in transcript.segments:
        if buf_start is None:
            buf_start = seg.start
        buf.append(seg.text)
        if sum(len(b) for b in buf) >= 220 or seg.end - (buf_start or 0) >= 25:
            lines.append(f"[{_stamp(buf_start or 0)}] {' '.join(buf).strip()}")
            buf, buf_start = [], None
    if buf:
        lines.append(f"[{_stamp(buf_start or 0)}] {' '.join(buf).strip()}")

    duration = transcript.duration
    return f"""Below is the full transcript of a {duration:.0f}-second video, chunked with [mm:ss] timestamps.

TASK
Select between {min_count} and {max_count} segments that are the most engaging, viral-worthy,
self-contained moments — the parts a scroller would stop for.

RULES
- Each segment must be between {min_seconds:.0f} and {max_seconds:.0f} seconds long.
- Segments must NOT overlap.
- start/end must be seconds (floats) within 0 and {duration:.1f}, aligned to the timestamps above.
- Prefer a strong hook in the first seconds, a surprising claim, actionable advice,
  an emotional peak, or a complete self-contained story/anecdote.
- The clip must make sense with no prior context.

OUTPUT
Return ONLY a JSON array (no markdown, no commentary) exactly like this:
[{{"start": 45.2, "end": 89.5, "title": "Hook title or summary 1"}},
 {{"start": 120.0, "end": 165.4, "title": "Hook title or summary 2"}}]

TRANSCRIPT
{chr(10).join(lines)}"""


def _stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------- #
# LLM providers
# --------------------------------------------------------------------------- #
def resolve_provider(preference: str | None = None) -> str:
    pref = (preference or config.LLM_PROVIDER or "auto").lower()
    if pref == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else "heuristic"
    if pref == "anthropic":
        return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "heuristic"
    if pref == "heuristic":
        return "heuristic"
    # auto
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "heuristic"


def _call_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        temperature=0.4,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


def _call_anthropic(prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic()
    message = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1500,
        temperature=0.4,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def extract_json_array(text: str) -> list:
    """Pull a JSON array of dicts out of an LLM response, however it is wrapped."""
    candidates: list[str] = []
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    candidates.extend(f.strip() for f in fenced)
    candidates.append(text.strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for opener in ("[", "{"):
            idx = candidate.find(opener)
            while idx != -1:
                try:
                    obj, _ = decoder.raw_decode(candidate[idx:])
                except ValueError:
                    idx = candidate.find(opener, idx + 1)
                    continue
                if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                    return obj
                if isinstance(obj, dict):
                    for value in obj.values():
                        if isinstance(value, list) and value and isinstance(value[0], dict) and "start" in value[0]:
                            return value
                break
    raise ValueError("no JSON array of highlights found in model response")


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _nearest(values: list[float], t: float) -> float:
    """Closest value to `t` (ties go forward); returns a real boundary, never `t`."""
    if not values:
        return t
    i = bisect_left(values, t)
    if i <= 0:
        return values[0]
    if i >= len(values):
        return values[-1]
    fwd, bwd = values[i], values[i - 1]
    return fwd if abs(fwd - t) <= abs(t - bwd) else bwd


def _snap_start(words: list[Word], t: float) -> float:
    return _nearest([w.start for w in words], t)


def _snap_end(words: list[Word], t: float) -> float:
    return _nearest([w.end for w in words], t)


def normalize_highlights(
    items: list[dict],
    transcript: Transcript,
    *,
    min_seconds: float | None = None,
    max_seconds: float | None = None,
    limit: int | None = None,
) -> list[Highlight]:
    """Clamp, snap to word boundaries, enforce 30–60s, non-overlap and count."""
    duration = transcript.duration or 0.0
    words = transcript.words
    if min_seconds is None or max_seconds is None:
        dmin, dmax, _gap, _count = adaptive_band(duration, limit or config.MAX_HIGHLIGHTS)
        min_seconds = min_seconds if min_seconds is not None else dmin
        max_seconds = max_seconds if max_seconds is not None else dmax
    limit = limit or config.MAX_HIGHLIGHTS
    if duration and duration < min_seconds:
        min_seconds = duration  # shorter source than a full highlight

    parsed: list[Highlight] = []
    for item in items:
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        if duration:
            start = min(max(start, 0.0), duration)
            end = min(max(end, 0.0), duration)
        else:
            start = max(start, 0.0)
        if end - start < 1.0:
            continue
        title = str(item.get("title") or "").strip().replace("\n", " ")
        parsed.append(Highlight(start=start, end=end, title=title))

    parsed.sort(key=lambda h: h.start)

    result: list[Highlight] = []
    for hl in parsed:
        if duration:
            hl.start = min(_snap_start(words, hl.start), duration)
            hl.end = min(_snap_end(words, hl.end), duration)
            # enforce minimum length by pushing the end out to a word boundary
            if hl.end - hl.start < min_seconds - 0.25:
                target = hl.start + min_seconds
                snapped = _snap_end(words, target)
                hl.end = min(duration, max(snapped, target))
                if hl.end - hl.start < min_seconds - 1.0 and duration >= min_seconds:
                    continue
            # enforce maximum length
            if hl.end - hl.start > max_seconds:
                hl.end = min(_snap_end(words, hl.start + max_seconds), hl.end)
        if result:
            prev = result[-1]
            if hl.start < prev.end:  # overlap -> push forward
                hl.start = min(_snap_start(words, prev.end), hl.end)
                if hl.end - hl.start < min_seconds * 0.75:
                    continue
            if hl.start - prev.start < min(10.0, min_seconds):  # near-duplicate
                continue
        if hl.end <= hl.start:
            continue
        if not hl.title:
            hl.title = _title_for_window(transcript, hl.start, hl.end)
        result.append(hl)
        if len(result) >= limit:
            break
    return result


def _title_for_window(transcript: Transcript, start: float, end: float) -> str:
    best = ""
    for seg in transcript.segments:
        if seg.start >= start and seg.end <= end and len(seg.text) > len(best):
            best = seg.text
    best = re.sub(r"\s+", " ", best).strip(" .,;:-")
    if len(best) > 70:
        best = best[:67].rsplit(" ", 1)[0] + "…"
    return best or "Highlight"


# --------------------------------------------------------------------------- #
# Heuristic fallback (no API key required)
# --------------------------------------------------------------------------- #
def _score_text(text: str) -> float:
    low = text.lower()
    score = 0.0
    score += 2.5 * low.count("?")
    score += 1.0 * low.count("!")
    words_found = re.findall(r"[a-z']+", low)
    if words_found:
        hits = sum(1 for w in words_found if w in HOOK_WORDS)
        score += 2.0 * hits
    score += 1.5 * len(re.findall(r"\d+", low))  # numbers / stats / money
    if re.search(r"\b(you|your)\b", low):
        score += 1.0
    if re.search(r"\b(how|why|what|never|nobody|everyone|mistake|secret)\b", low):
        score += 1.5
    return score


def heuristic_highlights(
    transcript: Transcript,
    count: int,
    *,
    band: tuple[float, float, float, int] | None = None,
) -> list[Highlight]:
    """Score sentence ranges and greedily take the best non-overlapping ones.

    `band` — optional (min_seconds, max_seconds, gap, count) from
    `adaptive_band`, so short sources honour the requested clip count with
    shorter clips instead of silently returning fewer.
    """
    segments = [s for s in transcript.segments if s.text.strip()]
    if not segments:
        return []
    duration = transcript.duration or segments[-1].end
    if band is None:
        band = adaptive_band(duration, count)
    min_len, max_len, gap, count = band
    if duration < min_len:
        min_len = duration

    starts = [s.start for s in segments]
    ends = [s.end for s in segments]
    seg_scores = [_score_text(s.text) for s in segments]

    candidates: list[tuple[float, float, float, str]] = []  # (score, start, end, title)
    n = len(segments)
    for i in range(n):
        for j in range(i + 1, n + 1):
            length = ends[j - 1] - starts[i]
            if length > max_len:
                break
            if length < min_len and j < n:
                continue
            if length < min_len - 1.0:
                break
            score = sum(seg_scores[i:j])
            # prefer dense, hook-heavy windows over long rambling ones
            score = score / max(12.0, length) * 45.0
            if length >= 38:
                score += 0.8
            window_end = min(duration, ends[j - 1])
            title = _title_for_window(transcript, starts[i], window_end)
            candidates.append((score, starts[i], window_end, title))

    candidates.sort(key=lambda c: c[0], reverse=True)

    # Shrunken band (short source, many clips requested): score-greedy can't
    # tile a packed timeline, so partition the video into `count` equal slots
    # and take the best hook-y window inside each — guaranteeing the count.
    if max_len < config.MAX_HIGHLIGHT_SECONDS - 1e-6:
        return _select_in_slots(
            transcript, segments, seg_scores, count, min_len, gap, duration
        )

    chosen: list[Highlight] = []
    for score, start, end, title in candidates:
        if len(chosen) >= count:
            break
        if any(start < c.end + gap and end > c.start - gap for c in chosen):
            continue
        if score <= 0:
            break
        chosen.append(Highlight(start=round(start, 2), end=round(end, 2), title=title))

    chosen.sort(key=lambda h: h.start)
    return chosen


def _select_in_slots(
    transcript: Transcript,
    segments: list,
    seg_scores: list[float],
    count: int,
    min_len: float,
    gap: float,
    duration: float,
) -> list[Highlight]:
    """Fill `count` equal slots across a short source, one clip per slot.

    Each slot holds the highest-scoring word-snapped window that fits inside
    it; when speech doesn't provide one, an evenly placed window is used. Slots
    are disjoint by construction, so the requested count is always delivered
    (each clip lands around duration/count seconds — never below
    MIN_CLIP_SECONDS, since `count` was capped by `adaptive_band`).
    """
    words = transcript.words
    word_starts = [w.start for w in words]
    word_ends = [w.end for w in words]
    target = (duration - gap * (count - 1)) / count if count else duration
    pitch = target + gap
    floor_len = max(min_len * 0.8, target * 0.8)

    def _near(values: list[float], t: float, tol: float = 1.2) -> float:
        """Word-boundary snap, but only when it stays close to `t` — sparse
        dialogue would otherwise drag a window seconds off its slot."""
        v = _nearest(values, t)
        return v if abs(v - t) <= tol else t

    # candidate windows at the slot's target length, starting at each segment
    sized: list[tuple[float, float, float, str]] = []
    for seg, score in zip(segments, seg_scores):
        end = _near(word_ends, seg.start + target)
        if end - seg.start < floor_len:
            end = min(duration, seg.start + target)  # snap cost too much — keep the raw length
        start = _near(word_starts, seg.start)
        if end - start < floor_len:
            continue
        sized.append(
            (score / max(12.0, end - start) * 45.0, start, end,
             _title_for_window(transcript, start, end))
        )

    chosen: list[Highlight] = []
    prev_end = 0.0
    for k in range(count):
        slot_start = k * pitch
        slot_end = min(duration, slot_start + pitch)
        lo = max(slot_start, prev_end)   # never start before the previous clip ends
        if slot_end - lo < 1.0:
            break                        # timeline exhausted — deliver what fits
        in_slot = [
            c for c in sized if c[1] >= lo - 1e-6 and c[2] <= slot_end + 1e-6
        ]
        if in_slot:
            best = max(in_slot, key=lambda c: (c[0], -c[1]))
            start, end, title = max(best[1], lo), min(best[2], slot_end), best[3]
        else:
            start = max(_near(word_starts, lo), lo)  # clamp: no backward snap bleed
            raw_end = start + target
            end = _near(word_ends, min(raw_end, slot_end))
            if end > slot_end or end - start < floor_len:
                end = min(slot_end, raw_end)
            title = _title_for_window(transcript, start, end)
        if end - start < 1.0:
            continue
        chosen.append(Highlight(start=round(start, 2), end=round(end, 2), title=title))
        prev_end = end

    chosen.sort(key=lambda h: h.start)
    return chosen


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def select_highlights(
    transcript: Transcript,
    *,
    provider: str | None = None,
    count: int | None = None,
    log: LogFn = lambda _: None,
) -> HighlightSelection:
    count = count or config.MAX_HIGHLIGHTS
    resolved = resolve_provider(provider)
    band = adaptive_band(transcript.duration or 0.0, count)
    min_seconds, max_seconds, gap, _band_count = band
    if max_seconds < config.MAX_HIGHLIGHT_SECONDS:
        log(
            f"Short source ({transcript.duration or 0:.0f}s): shrinking clips to "
            f"{min_seconds:.0f}–{max_seconds:.0f}s so all {count} requested clips fit"
        )

    if resolved == "heuristic":
        log("No LLM API key found — using the built-in hook heuristic to pick highlights")
        highlights = heuristic_highlights(transcript, count, band=band)
        # always top up to the exact count the user asked for
        if len(highlights) < count:
            highlights = _pad_with_even_spacing(transcript, highlights, count, band=band)
        highlights = highlights[:count]
        return HighlightSelection(highlights=highlights, provider="heuristic")

    prompt = build_prompt(transcript, min_count, count, min_seconds, max_seconds)
    log(f"Ask{'ing' if resolved == 'openai' else 'ing'} {resolved} to find {min_count}–{count} highlights…")
    try:
        raw = _call_openai(prompt) if resolved == "openai" else _call_anthropic(prompt)
        items = extract_json_array(raw)
        highlights = normalize_highlights(
            items, transcript, min_seconds=min_seconds, max_seconds=max_seconds,
            limit=count,
        )
        if not highlights:
            raise ValueError("model returned highlights that were all invalid")
        highlights = _top_up(transcript, highlights, count, band)
        return HighlightSelection(highlights=highlights, provider=resolved, raw_response=raw)
    except Exception as exc:  # network / quota / parse problems -> never fail the job
        log(f"LLM highlight selection failed ({exc}); falling back to the heuristic picker")
        highlights = heuristic_highlights(transcript, count, band=band)
        highlights = _top_up(transcript, highlights, count, band)
        return HighlightSelection(highlights=highlights, provider="heuristic (LLM fallback)")


def _top_up(
    transcript: Transcript, highlights: list[Highlight], count: int,
    band: tuple[float, float, float, int],
) -> list[Highlight]:
    """Guarantee EXACTLY `count` clips: pad any shortfall, then trim any excess.

    The user picked a number on the slider — that number is the contract. A
    picker that finds fewer (sparse dialogue) gets topped up with evenly
    spaced windows; one that finds more gets trimmed to the count.
    """
    if len(highlights) < count:
        highlights = _pad_with_even_spacing(transcript, highlights, count, band=band)
    return highlights[:count]


def _pad_with_even_spacing(
    transcript: Transcript, existing: list[Highlight], count: int,
    *,
    band: tuple[float, float, float, int] | None = None,
) -> list[Highlight]:
    """Guarantee we return something usable even for tiny/degenerate transcripts."""
    duration = transcript.duration or 0.0
    if duration <= 5:
        return existing
    if band is None:
        band = adaptive_band(duration, count)
    min_seconds, max_seconds, gap, count = band
    span = min(max_seconds, duration)
    step = span + gap
    out = list(existing)
    t = 0.0
    while len(out) < count and t + min_seconds <= duration + 1e-6:
        candidate = Highlight(
            start=round(t, 2),
            end=round(min(duration, t + span), 2),
            title=_title_for_window(transcript, t, min(duration, t + span)),
        )
        if all(candidate.start >= o.end + gap or candidate.end <= o.start - gap for o in out):
            out.append(candidate)
        t += step
    out.sort(key=lambda h: h.start)
    return out
