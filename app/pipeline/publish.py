"""Stage 5d: PUBLISH METADATA — ready-to-post titles, tags & descriptions.

Every clip also gets `clip_XX_publish.json`/`.md` with copy-paste metadata
for the two platforms clip submissions actually go to:

  * Instagram — a caption (title line + payoff hook + context + CTA) and a
    hashtag set sized inside the platform's limits;
  * YouTube — a ≤100-char title, a tag list and a description built from the
    hook, a context line, a CTA and #shorts.

Deterministic (no LLM): keywords come from the clip's own transcript —
content words ranked by frequency with proper nouns promoted — and the hook
line reuses the payoff detection the hook playbook and the viral analysis
use, so all reports agree on the moment that sells the clip.

The campaign audit (app/pipeline/campaigns.py) runs on top of this: a
campaign's rules check these generated titles/tags/descriptions plus the
render settings, and suggest exact corrections.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.pipeline.hooks import detect_payoff, topic_phrase, words_in

# --------------------------------------------------------------------------- #
# Platform limits (hard) + practical targets
# --------------------------------------------------------------------------- #
IG_CAPTION_MAX = 2200      # Instagram caption hard limit
IG_TITLE_MAX = 150         # keep the first line a scannable title
IG_HASHTAG_MAX = 30        # Instagram ignores hashtags past 30
IG_HASHTAG_TARGET = 8      # what actually works on Reels
YT_TITLE_MAX = 100         # YouTube title hard limit
YT_TAGS_MAX = 30           # practical tag count
YT_DESC_MIN = 100          # a description shorter than this barely helps search

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "so", "to", "of", "in", "on",
    "at", "for", "from", "with", "into", "out", "up", "down", "over", "under",
    "is", "am", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "have", "has", "had", "will", "would", "can", "could", "should",
    "you", "your", "yours", "we", "our", "us", "they", "them", "their", "he",
    "him", "his", "she", "her", "it", "its", "i", "me", "my", "mine", "this",
    "that", "these", "those", "there", "here", "when", "what", "which", "who",
    "whom", "how", "why", "where", "not", "no", "yes", "just", "really",
    "very", "too", "also", "then", "than", "now", "get", "got", "go", "went",
    "come", "came", "see", "saw", "look", "looked", "know", "knew", "think",
    "thought", "want", "wanted", "say", "said", "tell", "told", "ask", "asked",
    "let", "lets", "make", "made", "take", "took", "give", "gave", "put",
    "keep", "kept", "right", "okay", "ok", "yeah", "gonna", "wanna", "gotta",
    "like", "about", "actually", "maybe", "thing", "things", "stuff", "one",
    "two", "three", "going", "doing", "being", "has", "don't", "dont",
    "didn't", "didnt", "won't", "wont", "can't", "cant", "isn't", "isnt",
    "because", "been", "every", "all", "any", "some", "more", "most", "much",
    "many", "even", "ever", "back", "again", "still", "here", "well", "way",
    # greetings / engagement filler — never topic keywords, junk as hashtags
    "hey", "hello", "hiya", "heya", "everyone", "everybody", "guys", "folks",
    "welcome", "please", "subscribe", "subscriber", "comment", "follow",
    "like", "share", "followers", "honestly", "literally", "seriously",
    "totally", "basically", "exactly", "obviously",
}

# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #
def _clean(token: str) -> str:
    return re.sub(r"[^\w']", "", token).lower()


def _tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t.strip(" .!?,;:—-")


def _trim(text: str, max_len: int) -> str:
    """Trim on a word boundary with an ellipsis; never cut mid-word."""
    t = _tidy(text)
    if len(t) <= max_len:
        return t
    cut = t[: max_len - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or t[: max_len - 1]) + "…"


def _capitalise(text: str) -> str:
    t = _tidy(text)
    if not t:
        return t
    return t[0].upper() + t[1:]


# --------------------------------------------------------------------------- #
# Keywords from the clip's own words
# --------------------------------------------------------------------------- #
def extract_keywords(words, start: float, end: float, *, limit: int = 12) -> list[str]:
    """Content words inside the clip window, ranked by frequency.

    Proper nouns (capitalised tokens that aren't sentence-initial) are
    promoted — names are what people actually search.
    """
    inside = words_in(words, start, end)
    freq: dict[str, int] = {}
    proper: set[str] = set()
    for i, w in enumerate(inside):
        t = _clean(w.text)
        # contractions ("it's", "we're") are function words — never keywords
        if len(t) < 3 or t in STOPWORDS or t.isdigit() or "'" in t:
            continue
        freq[t] = freq.get(t, 0) + 1
        raw = w.text.strip()
        if i > 0 and raw[:1].isupper() and raw.isupper() is False and t not in STOPWORDS:
            proper.add(t)
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    out = [t for t, _ in ranked if t in proper]
    out += [t for t, _ in ranked if t not in proper]
    return out[:limit]


def _hashtag(word: str) -> str:
    """Turn a keyword into a safe hashtag: lowercase, no punctuation."""
    w = re.sub(r"[^A-Za-z0-9]", "", word)
    return f"#{w.lower()}" if len(w) >= 3 else ""


# --------------------------------------------------------------------------- #
# Lines
# --------------------------------------------------------------------------- #
def build_title_line(title: str, quote: str, topic: str, keywords: list[str],
                     *, max_len: int) -> str:
    """One scannable title: curated title first, then the payoff quote."""
    candidates: list[str] = []
    t = _tidy(title)
    if len(t.split()) >= 3:
        candidates.append(t)
    q = _tidy(quote)
    if len(q.split()) >= 3:
        candidates.append(f"\u201c{_trim(q, max_len - 3)}\u201d")
    if topic:
        candidates.append(_capitalise(_tidy(topic)))
    if keywords:
        candidates.append(" ".join(keywords[:5]).title())
    for cand in candidates:
        if len(cand) <= max_len:
            return cand
    base = candidates[0] if candidates else "Watch this moment"
    return _trim(base, max_len)


def build_hashtags(keywords: list[str], *, niche: str = "", limit: int = IG_HASHTAG_TARGET,
                   with_platform: bool = False) -> list[str]:
    tags: list[str] = []
    for kw in keywords:
        tag = _hashtag(kw)
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= limit:
            break
    for word in str(niche or "").split():
        tag = _hashtag(word)
        if tag and tag not in tags:
            tags.append(tag)
    if with_platform and "#shorts" not in tags:
        tags.append("#shorts")
    return tags[: max(limit, IG_HASHTAG_MAX)]


def build_cta(niche: str, platform: str) -> str:
    topic = _tidy(niche)
    if platform == "instagram":
        return f"Follow for more {topic} clips \u2014 which moment hit hardest? 👇" if topic \
            else "Follow for daily clips \u2014 which moment hit hardest? 👇"
    return f"More {topic} shorts daily \u2014 subscribe \U0001f4cc" if topic \
        else "Full stream moments daily \u2014 subscribe \U0001f4cc"


def build_context_line(words, start: float, end: float) -> str:
    """One line of cold-viewer context taken from how the clip actually opens."""
    inside = words_in(words, start, end)
    opening = " ".join(w.text for w in inside[:12]).strip()
    opening = re.sub(r"\s+", " ", opening)
    if not opening:
        return ""
    return _capitalise(_trim(opening, 120))


# --------------------------------------------------------------------------- #
# Per-platform metadata
# --------------------------------------------------------------------------- #
def build_instagram(words, start: float, end: float, *, title: str = "",
                    niche: str = "") -> dict:
    inside = words_in(words, start, end)
    quote, _ = detect_payoff(words, start, end)
    topic = topic_phrase(title, words, start, end)
    keywords = extract_keywords(words, start, end)
    title_line = build_title_line(title, quote, topic, keywords, max_len=IG_TITLE_MAX)
    context = build_context_line(words, start, end)
    cta = build_cta(niche, "instagram")
    hashtags = build_hashtags(keywords, niche=niche)

    parts = [title_line]
    q = _tidy(quote)
    if q and _tidy(q).lower() not in _tidy(title_line).lower():
        parts.append(_capitalise(_trim(q, 200)))
    if context and context.lower() not in _tidy(title_line).lower():
        parts.append(context)
    parts.append(cta)
    parts.append(" ".join(hashtags))
    caption = "\n\n".join(p for p in parts if p)
    return {
        "title": title_line,
        "caption": caption,
        "cta": cta,
        "hashtags": hashtags,
        "keywords": keywords,
    }


def build_youtube(words, start: float, end: float, *, title: str = "",
                  niche: str = "") -> dict:
    inside = words_in(words, start, end)
    quote, _ = detect_payoff(words, start, end)
    topic = topic_phrase(title, words, start, end)
    keywords = extract_keywords(words, start, end)
    yt_title = build_title_line(title, quote, topic, keywords, max_len=YT_TITLE_MAX)
    context = build_context_line(words, start, end)
    cta = build_cta(niche, "youtube")
    hashtags = build_hashtags(keywords, niche=niche, limit=IG_HASHTAG_TARGET,
                              with_platform=True)
    # Tags (not hashtags) for YouTube's tag box: keywords + niche + format tags
    tags: list[str] = []
    for kw in keywords:
        if kw not in tags:
            tags.append(kw)
        if len(tags) >= YT_TAGS_MAX - 4:
            break
    for word in str(niche or "").split():
        w = _clean(word)
        if w and w not in tags:
            tags.append(w)
    for generic in ("shorts", "short", "clip", "viral"):
        if generic not in tags:
            tags.append(generic)

    q = _tidy(quote)
    desc_parts = []
    if q:
        desc_parts.append(_capitalise(_trim(q, 200)))
    if context:
        desc_parts.append(context)
    desc_parts.append(cta)
    desc_parts.append(" ".join(hashtags))
    description = "\n\n".join(p for p in desc_parts if p)
    return {
        "title": yt_title,
        "description": description,
        "tags": tags[:YT_TAGS_MAX],
        "hashtags": hashtags,
        "cta": cta,
        "keywords": keywords,
    }


# --------------------------------------------------------------------------- #
# Self-checks (length/limit sanity before anything else audits the metadata)
# --------------------------------------------------------------------------- #
def self_checks(ig: dict, yt: dict) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, evidence: str, fix: str = "") -> None:
        checks.append({"name": name, "status": "pass" if ok else "watch",
                       "evidence": evidence, "fix": "" if ok else fix})

    add("youtube_title_length", len(yt["title"]) <= YT_TITLE_MAX,
        f"{len(yt['title'])}/{YT_TITLE_MAX} chars",
        "shorten the YouTube title to ≤100 chars")
    add("instagram_caption_length", len(ig["caption"]) <= IG_CAPTION_MAX,
        f"{len(ig['caption'])}/{IG_CAPTION_MAX} chars",
        "shorten the Instagram caption")
    add("instagram_hashtag_count", len(ig["hashtags"]) <= IG_HASHTAG_MAX,
        f"{len(ig['hashtags'])} hashtags (Instagram uses the first {IG_HASHTAG_MAX})",
        f"cut to {IG_HASHTAG_MAX} hashtags or fewer")
    add("youtube_description_length", len(yt["description"]) >= YT_DESC_MIN,
        f"{len(yt['description'])} chars",
        "expand the description (context + CTA) past ~100 chars")
    add("hashtags_present", bool(ig["hashtags"]) and bool(yt["hashtags"]),
        f"{len(ig['hashtags'])} IG / {len(yt['hashtags'])} YT hashtags",
        "add topic hashtags so the clip is findable")
    return checks


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(words, start: float, end: float, *, title: str = "",
                 niche: str = "", index: int = 1) -> dict:
    ig = build_instagram(words, start, end, title=title, niche=niche)
    yt = build_youtube(words, start, end, title=title, niche=niche)
    return {
        "clip": {
            "index": index,
            "title": _tidy(title),
            "start": round(float(start), 2),
            "end": round(float(end), 2),
            "duration": round(float(end - start), 2),
        },
        "instagram": ig,
        "youtube": yt,
        "checks": self_checks(ig, yt),
    }


def render_markdown(report: dict) -> str:
    clip = report["clip"]
    ig = report["instagram"]
    yt = report["youtube"]
    lines = [
        f"# Publish metadata \u2014 clip {clip['index']:02d}"
        + (f" ({clip['title']})" if clip["title"] else ""),
        "",
        "## 📱 Instagram",
        "",
        "**Caption** (copy-paste):",
        "",
        "```text",
        ig["caption"],
        "```",
        "",
        f"**Hashtags:** {' '.join(ig['hashtags']) or '\u2014'}",
        "",
        "## 🖼 YouTube",
        "",
        f"**Title** ({len(yt['title'])}/{YT_TITLE_MAX} chars):",
        "",
        yt["title"],
        "",
        "**Description** (copy-paste):",
        "",
        "```text",
        yt["description"],
        "```",
        "",
        "**Tags:** " + ", ".join(yt["tags"]),
        "",
        "## Sanity checks",
        "",
        "| Check | Status | Evidence |",
        "|-------|--------|----------|",
    ]
    icon = {"pass": "\u2705", "watch": "👀"}
    for c in report["checks"]:
        lines.append(f"| {c['name']} | {icon[c['status']]} {c['status']} | {c['evidence']} |")
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: dict, *, markdown: bool = False) -> Path:
    if markdown:
        path.write_text(render_markdown(report), encoding="utf-8")
    else:
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
