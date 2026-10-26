"""Campaign rules engine — audit every clip against a campaign's rules.

Clip-payment platforms (Whop campaigns and similar) accept submissions under
per-campaign rules: the brand word must appear in the title, specific
hashtags are required, others are banned, a watermark must sit in a certain
corner, clips must run between N and M seconds, certain words may never be
spoken… This module stores a campaign as structured rules, audits each clip's
generated publish metadata (app/pipeline/publish.py) plus the render settings
against EVERY rule, and returns pass/fail verdicts with concrete fixes —
including corrected titles, captions and hashtag sets ready to paste.

Everything runs from the job's files on disk (`transcript.json`,
`highlights.json`, `settings.json`), so audits work on any finished job even
after a server restart.

Campaign JSON (stored under config.CAMPAIGNS_DIR):

    {
      "id": "tradevault",
      "name": "Whop — TradeVault",
      "platform": "whop",                 # whop | instagram | youtube | tiktok | other
      "brand": "tradevault",
      "notes": "free text from the campaign page",
      "rules": {
        "required_title_words": ["tradevault"],
        "banned_title_words": [],
        "title_max_chars": 100,
        "required_hashtags": ["#tradevault"],
        "banned_hashtags": ["#giveaway"],
        "max_hashtags": 8,
        "required_description_words": ["tradevault.com"],
        "min_duration": 15.0,
        "max_duration": 60.0,
        "banned_words": ["guaranteed"],
        "watermark_required": true,
        "watermark_position": "bottom-right",
        "captions_required": true,
        "cta_required": true,
        "language": ""
      }
    }
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app import config
from app.pipeline import publish
from app.pipeline.hooks import words_in
from app.pipeline.transcribe import load as load_transcript

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
PLATFORMS = ("whop", "instagram", "youtube", "tiktok", "other")

RULE_DEFAULTS: dict = {
    "required_title_words": [],      # must appear in the IG title line + YT title
    "banned_title_words": [],        # must NOT appear in titles
    "title_max_chars": 0,            # 0 = no campaign limit (YT still ≤100)
    "required_hashtags": [],         # must appear in the IG caption + YT description
    "banned_hashtags": [],           # must NOT appear anywhere
    "max_hashtags": 0,               # 0 = no campaign limit
    "required_description_words": [],  # must appear in the YT description
    "min_duration": 0.0,             # seconds (0 = no limit)
    "max_duration": 0.0,             # seconds (0 = no limit)
    "banned_words": [],              # must not be SPOKEN in the clip
    "watermark_required": False,
    "watermark_position": "",        # "" = any corner
    "captions_required": False,
    "cta_required": False,
    "language": "",                  # "" = any (e.g. "en")
}

GRADES = ((85, "ready to submit"), (65, "close — small fixes"), (0, "fix first"))

# rule key -> check name when one check covers several keys
_CHECK_NAME = {"watermark_required": "watermark", "watermark_position": "watermark"}


def _norm_words(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        w = re.sub(r"\s+", " ", str(v)).strip(" .!?,;:#").lower()
        if w and w not in out:
            out.append(w)
    return out


def _norm_tags(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        t = re.sub(r"[^A-Za-z0-9_#]", "", str(v))
        if not t:
            continue
        tag = t if t.startswith("#") else f"#{t}"
        tag = tag.lower()
        if tag not in out:
            out.append(tag)
    return out


def normalize_rules(rules: dict | None) -> dict:
    r = dict(RULE_DEFAULTS)
    for k, v in (rules or {}).items():
        if k not in RULE_DEFAULTS:
            continue
        if k in {"required_title_words", "banned_title_words", "banned_words",
                 "required_description_words"}:
            r[k] = _norm_words(v if isinstance(v, list) else str(v).split(","))
        elif k in {"required_hashtags", "banned_hashtags"}:
            r[k] = _norm_tags(v if isinstance(v, list) else str(v).split(","))
        elif k in {"title_max_chars", "max_hashtags"}:
            try:
                r[k] = max(0, int(v))
            except (TypeError, ValueError):
                r[k] = 0
        elif k in {"min_duration", "max_duration"}:
            try:
                r[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                r[k] = 0.0
        elif k in {"watermark_required", "captions_required", "cta_required"}:
            r[k] = bool(v)
        elif k in {"watermark_position", "language"}:
            r[k] = str(v or "").strip().lower()
    if r["watermark_position"] and r["watermark_position"] not in config.WATERMARK_POSITIONS:
        r["watermark_position"] = ""
    return r


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:60] or "campaign"


def normalize_campaign(data: dict) -> dict:
    name = re.sub(r"\s+", " ", str(data.get("name", "") or "")).strip()
    if not name:
        raise ValueError("campaign needs a name")
    platform = str(data.get("platform", "whop") or "whop").strip().lower()
    if platform not in PLATFORMS:
        platform = "other"
    return {
        "id": _slug(data.get("id") or name),
        "name": name,
        "platform": platform,
        "brand": str(data.get("brand", "") or "").strip(),
        "notes": str(data.get("notes", "") or "").strip(),
        "rules": normalize_rules(data.get("rules")),
    }


# --------------------------------------------------------------------------- #
# Persistence (data/campaigns/*.json — survives restarts, unlike in-memory jobs)
# --------------------------------------------------------------------------- #
def _dir() -> Path:
    config.CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    return config.CAMPAIGNS_DIR


def save_campaign(data: dict) -> dict:
    campaign = normalize_campaign(data)
    (_dir() / f"{campaign['id']}.json").write_text(
        json.dumps(campaign, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return campaign


def load_campaign(cid: str) -> dict:
    path = _dir() / f"{_slug(cid)}.json"
    if not path.is_file():
        raise FileNotFoundError(f"campaign {cid!r} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def list_campaigns() -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(out, key=lambda c: c.get("name", ""))


def delete_campaign(cid: str) -> bool:
    path = _dir() / f"{_slug(cid)}.json"
    if path.is_file():
        path.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# Free-text rules → structured rules (paste the campaign page, get a draft)
# --------------------------------------------------------------------------- #
_HASHTAG_RE = re.compile(r"#\w+")
_CONTAIN_RE = re.compile(
    r"\b(?:contain|contains|include|includes|mention|mentions|must have|needs? to have"
    r"|with the (?:word|name|title))\s+(?:the\s+word\s+)?[\"'\u201c']?([A-Za-z0-9@.'\-]{2,40})",
    re.I,
)
_BAN_RE = re.compile(
    r"\b(?:no|banned|avoid|never|do not use|don't use|do not say|don't say"
    r"|cannot say|can't say|exclude|without|remove)\s+"
    r"[\"'\u201c']?(#?[A-Za-z0-9@.'\-]{2,40})",
    re.I,
)
_RANGE_RE = re.compile(r"(\d+)\s*(?:-|\u2013|to|and)\s*(\d+)\s*(?:s\b|sec|secs|second|seconds)\b", re.I)
_MAX_T_RE = re.compile(r"(?:max|maximum|under|below|less than|no more than|up to)\s*(\d+)\s*(?:char|character)s?", re.I)
_MAX_D_RE = re.compile(r"(?:max|maximum|under|below|less than|no more than|up to|within)\s*(\d+)\s*(?:s\b|sec|secs|second|seconds)\b", re.I)
_MIN_D_RE = re.compile(r"(?:min|minimum|at least|over|above|more than)\s*(\d+)\s*(?:s\b|sec|secs|second|seconds)\b", re.I)
_POSITION_RE = re.compile(
    r"(top|bottom)[\s\-]*(left|right|center|centre)|\bcenter(?:ed)? bottom\b", re.I
)


def parse_rules_text(text: str) -> dict:
    """Best-effort extraction of rules from pasted campaign text.

    Returns {"rules": {...}, "found": ["human note", ...]} — conservative on
    purpose: it only picks up patterns it recognises, never invents rules.
    """
    src = str(text or "")
    rules: dict = {}
    found: list[str] = []

    tags = [t.lower() for t in _HASHTAG_RE.findall(src)]
    tags = [t for t in tags if len(t) > 1]
    if tags:
        # hashtags that appear next to a ban word are banned, the rest required
        banned_tags: list[str] = []
        required_tags: list[str] = []
        for m in _BAN_RE.finditer(src):
            tok = m.group(1).strip(".,;:!?")   # the class swallows sentence dots
            if tok.startswith("#"):
                banned_tags.append(tok.lower())
        required_tags = [t for t in tags if t not in banned_tags]
        if required_tags:
            rules["required_hashtags"] = required_tags
            found.append(f"required hashtags: {' '.join(required_tags)}")
        if banned_tags:
            rules["banned_hashtags"] = _norm_tags(banned_tags)
            found.append(f"banned hashtags: {' '.join(rules['banned_hashtags'])}")

    contains = [_tidy_lower(m.group(1)) for m in _CONTAIN_RE.finditer(src)]
    contains = [c for c in contains if c]
    if contains:
        rules["required_title_words"] = contains
        found.append(f"title must contain: {', '.join(contains)}")

    banned: list[str] = []
    for phrase in _banned_phrases(src):
        if not phrase.startswith("#") and phrase not in banned:
            banned.append(phrase)
    if banned:
        rules["banned_words"] = banned
        found.append(f"banned words: {', '.join(banned)}")

    m = _RANGE_RE.search(src)
    if m:
        rules["min_duration"], rules["max_duration"] = float(m.group(1)), float(m.group(2))
        found.append(f"duration {m.group(1)}–{m.group(2)}s")
    else:
        m = _MAX_D_RE.search(src)
        if m:
            rules["max_duration"] = float(m.group(1))
            found.append(f"max duration {m.group(1)}s")
        m = _MIN_D_RE.search(src)
        if m:
            rules["min_duration"] = float(m.group(1))
            found.append(f"min duration {m.group(1)}s")

    m = _MAX_T_RE.search(src)
    if m:
        rules["title_max_chars"] = int(m.group(1))
        found.append(f"title ≤ {m.group(1)} chars")

    if re.search(r"\bwatermark\b", src, re.I):
        rules["watermark_required"] = True
        found.append("watermark required")
        m = _POSITION_RE.search(src)
        if m and m.group(1) and m.group(2):
            pos = f"{m.group(1).lower()}-{m.group(2).lower()}"
            if pos in config.WATERMARK_POSITIONS:
                rules["watermark_position"] = pos
                found.append(f"watermark position: {pos}")

    if re.search(r"\b(captions?|subtitles?)\b", src, re.I):
        rules["captions_required"] = True
        found.append("captions required")

    if re.search(r"\bcta\b|call[\s\-]to[\s\-]action|ask\s+(?:for\s+)?a\s+(?:question|comment)", src, re.I):
        rules["cta_required"] = True
        found.append("CTA / question required")

    return {"rules": normalize_rules(rules), "found": found}


def _banned_phrases(src: str) -> list[str]:
    """Banned words/phrases after a ban keyword, including 'X or Y' runs.

    "Do not say guaranteed or risk free" → ["guaranteed", "risk free"],
    while "Avoid #giveaway posts" only yields the hashtag (handled separately).
    Each phrase is capped at 3 words so a trailing sentence never leaks in.
    """
    out: list[str] = []
    for m in _BAN_RE.finditer(src):
        clause = re.split(r"[.!?;\n]|,", src[m.end():])[0]
        parts = re.split(r"\s+(?:or|and)\s+", clause, flags=re.I)
        pieces = [m.group(1)] + parts[1:]
        for piece in pieces:
            words = piece.strip(" \"'\u201c\u201d.,;:!?()[]").split()[:3]
            phrase = " ".join(words)
            if phrase and phrase.lower() not in [p.lower() for p in out]:
                out.append(phrase)
    return out


def _tidy_lower(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip(" .!?,;:#\"'\u201c\u201d").lower()


# --------------------------------------------------------------------------- #
# Rules from FILES (PDF / TXT / MD / DOCX / JSON) — upload → structured rules
# --------------------------------------------------------------------------- #
RULE_FILE_SUFFIXES = (".pdf", ".txt", ".md", ".json", ".docx")


def _read_pdf_text(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _read_docx_text(data: bytes) -> str:
    """Plain-text extraction from a .docx (a zip of XML) with no extra deps."""
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    text = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", text)
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))


def read_rules_file(filename: str, data: bytes) -> dict:
    """Extract campaign rules from an uploaded rules file.

    Returns {"kind", "text", "rules", "found"}:
      * a .json with a "rules" object is imported directly (still normalised);
      * .pdf → pypdf text, .docx → stdlib XML strip, .txt/.md → decoded text;
      * every text form then runs through the conservative parse_rules_text.
    """
    name = str(filename or "rules.txt")
    suffix = Path(name).suffix.lower()
    if suffix not in RULE_FILE_SUFFIXES:
        raise ValueError(
            f"unsupported file type '{suffix}' "
            f"— expected {'/'.join(RULE_FILE_SUFFIXES)}"
        )
    if not data:
        raise ValueError("empty file")

    if suffix == ".json":
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("rules"), dict):
            return {
                "kind": "json",
                "text": json.dumps(payload, indent=2, ensure_ascii=False)[:8000],
                "rules": normalize_rules(payload["rules"]),
                "found": [f"imported structured rules from {name}"],
            }

    if suffix == ".pdf":
        text = _read_pdf_text(data)
    elif suffix == ".docx":
        text = _read_docx_text(data)
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        raise ValueError(f"no readable text in {name}")
    parsed = parse_rules_text(text)
    return {"kind": suffix.lstrip("."), "text": text[:8000],
            "rules": parsed["rules"], "found": parsed["found"]}


# --------------------------------------------------------------------------- #
# Individual rule checks — each returns {rule, status, evidence, suggestion}
# --------------------------------------------------------------------------- #
def check_title_words(rule_words: list[str], meta: dict, brand: str) -> dict:
    ig_title = meta["instagram"]["title"]
    yt_title = meta["youtube"]["title"]
    missing = [w for w in rule_words
               if w not in ig_title.lower() and w not in yt_title.lower()]
    if not rule_words:
        return {"rule": "required_title_words", "status": "pass",
                "evidence": "no title-word rule", "suggestion": ""}
    if not missing:
        return {"rule": "required_title_words", "status": "pass",
                "evidence": f"{', '.join(rule_words)} present in the titles",
                "suggestion": ""}
    stem = brand or missing[0]
    return {
        "rule": "required_title_words",
        "status": "fail",
        "evidence": f"missing from both titles: {', '.join(missing)}",
        "suggestion": (
            f"Prefix both titles with the brand, e.g. "
            f"\u201c{publish._capitalise(stem)} \u2014 {ig_title[:80]}\u201d"
        ),
    }


def check_banned_title_words(rule_words: list[str], meta: dict) -> dict:
    if not rule_words:
        return {"rule": "banned_title_words", "status": "pass",
                "evidence": "no banned-title-word rule", "suggestion": ""}
    texts = {"instagram title": meta["instagram"]["title"].lower(),
             "youtube title": meta["youtube"]["title"].lower()}
    hits = [(name, w) for name, t in texts.items() for w in rule_words if w in t]
    if not hits:
        return {"rule": "banned_title_words", "status": "pass",
                "evidence": "titles clean", "suggestion": ""}
    return {
        "rule": "banned_title_words",
        "status": "fail",
        "evidence": "; ".join(f"\u201c{w}\u201d in the {name}" for name, w in hits),
        "suggestion": "Remove the banned word(s) from the affected titles.",
    }


def check_title_length(limit: int, meta: dict, platform: str) -> dict:
    hard = publish.YT_TITLE_MAX
    yt_len = len(meta["youtube"]["title"])
    ig_len = len(meta["instagram"]["title"])
    if limit <= 0:
        if yt_len > hard:
            return {"rule": "title_max_chars", "status": "watch",
                    "evidence": f"YouTube title {yt_len} chars (platform cap {hard})",
                    "suggestion": f"Shorten the YouTube title to ≤{hard} chars."}
        return {"rule": "title_max_chars", "status": "pass",
                "evidence": f"titles {ig_len}/{yt_len} chars", "suggestion": ""}
    over = []
    if yt_len > limit:
        over.append(f"youtube {yt_len}")
    if (platform == "instagram" or limit < hard) and ig_len > limit:
        over.append(f"instagram {ig_len}")
    if not over:
        return {"rule": "title_max_chars", "status": "pass",
                "evidence": f"titles within {limit} chars", "suggestion": ""}
    return {
        "rule": "title_max_chars",
        "status": "fail",
        "evidence": f"over the campaign cap ({limit}): {', '.join(over)} chars",
        "suggestion": f"Trim the affected title(s) to ≤{limit} chars.",
    }


def check_hashtags(rules: dict, meta: dict) -> list[dict]:
    out: list[dict] = []
    ig = set(meta["instagram"]["hashtags"])
    yt = set(meta["youtube"]["hashtags"])
    desc_tags = set(re.findall(r"#[a-z0-9_]+", meta["youtube"]["description"].lower()))

    required = rules["required_hashtags"]
    if required:
        missing = [t for t in required if t not in ig and (t not in yt and t not in desc_tags)]
        out.append({
            "rule": "required_hashtags",
            "status": "pass" if not missing else "fail",
            "evidence": "all required hashtags present" if not missing
                        else f"missing: {' '.join(missing)}",
            "suggestion": "" if not missing else
                          f"Append to the Instagram caption & YouTube description: {' '.join(required)}",
        })
    banned = rules["banned_hashtags"]
    if banned:
        bad = [t for t in banned if t in ig or t in yt or t in desc_tags]
        out.append({
            "rule": "banned_hashtags",
            "status": "pass" if not bad else "fail",
            "evidence": "no banned hashtags" if not bad else f"present: {' '.join(bad)}",
            "suggestion": "" if not bad else f"Delete {' '.join(bad)} before posting.",
        })
    cap = rules["max_hashtags"]
    if cap > 0:
        n = len(meta["instagram"]["hashtags"])
        out.append({
            "rule": "max_hashtags",
            "status": "pass" if n <= cap else "fail",
            "evidence": f"{n} hashtags (cap {cap})",
            "suggestion": "" if n <= cap else
                          f"Keep only the first {cap}: "
                          f"{' '.join(meta['instagram']['hashtags'][:cap])}",
        })
    return out


def check_description(rules: dict, meta: dict) -> list[dict]:
    out: list[dict] = []
    desc = meta["youtube"]["description"].lower()
    required = rules["required_description_words"]
    if required:
        missing = [w for w in required if w not in desc]
        out.append({
            "rule": "required_description_words",
            "status": "pass" if not missing else "fail",
            "evidence": "description has the required text" if not missing
                        else f"missing from the description: {', '.join(missing)}",
            "suggestion": "" if not missing else
                          f"Add a line to the YouTube description: \u201c{' '.join(missing)}\u201d",
        })
    if rules["cta_required"]:
        has_cta = bool(re.search(r"[?]|\b(follow|subscribe|comment|join|share)\b", desc, re.I))
        out.append({
            "rule": "cta_required",
            "status": "pass" if has_cta else "fail",
            "evidence": "CTA present" if has_cta else "no call to action found",
            "suggestion": "" if has_cta else
                          f"End both captions with a CTA, e.g. \u201c{publish.build_cta('', 'instagram')}\u201d",
        })
    return out


def check_duration(rules: dict, duration: float) -> list[dict]:
    out: list[dict] = []
    lo, hi = rules["min_duration"], rules["max_duration"]
    if lo > 0 and duration < lo:
        out.append({"rule": "min_duration", "status": "fail",
                    "evidence": f"clip is {duration:.1f}s (min {lo:.0f}s)",
                    "suggestion": f"Extend the highlight window by ~{lo - duration:.1f}s."})
    elif lo > 0:
        out.append({"rule": "min_duration", "status": "pass",
                    "evidence": f"{duration:.1f}s ≥ {lo:.0f}s", "suggestion": ""})
    if hi > 0 and duration > hi:
        out.append({"rule": "max_duration", "status": "fail",
                    "evidence": f"clip is {duration:.1f}s (max {hi:.0f}s)",
                    "suggestion": f"Trim {duration - hi:.1f}s — cut the dead air the viral report lists."})
    elif hi > 0:
        out.append({"rule": "max_duration", "status": "pass",
                    "evidence": f"{duration:.1f}s ≤ {hi:.0f}s", "suggestion": ""})
    return out


def check_banned_words(rule_words: list[str], words, start: float, end: float) -> dict:
    if not rule_words:
        return {"rule": "banned_words", "status": "pass",
                "evidence": "no banned-word rule", "suggestion": ""}
    inside = words_in(words, start, end)
    hits: list[tuple[str, float]] = []
    for w in inside:
        t = _tidy_lower(w.text)
        for bad in rule_words:
            if t == bad or (len(bad.split()) == 1 and bad in t.split()):
                hits.append((bad, w.start - start))
                break
    if not hits:
        return {"rule": "banned_words", "status": "pass",
                "evidence": "no banned words spoken", "suggestion": ""}
    spots = ", ".join(f"\u201c{b}\u201d at +{t:.1f}s" for b, t in hits[:4])
    return {
        "rule": "banned_words",
        "status": "fail",
        "evidence": spots + (" …" if len(hits) > 4 else ""),
        "suggestion": "Re-cut around those lines (or bleep them) before submitting.",
    }


def check_watermark(rules: dict, settings: dict | None) -> list[dict]:
    out: list[dict] = []
    required = rules["watermark_required"]
    position = rules["watermark_position"]
    if not required and not position:
        return out
    if settings is None:
        out.append({"rule": "watermark", "status": "watch",
                    "evidence": "this job predates settings persistence — cannot verify",
                    "suggestion": "Re-render with the watermark on"
                                  + (f" at {position}" if position else "")
                                  + " to be safe."})
        return out
    on = bool(settings.get("watermark"))
    pos = str(settings.get("watermark_position") or "")
    if required and not on:
        out.append({"rule": "watermark", "status": "fail",
                    "evidence": "rendered without a watermark",
                    "suggestion": "Re-render with the watermark enabled"
                                  + (f" at {position}" if position else "") + "."})
    elif position and pos and pos != position:
        out.append({"rule": "watermark", "status": "fail",
                    "evidence": f"watermark is at {pos}, campaign wants {position}",
                    "suggestion": f"Re-render with the watermark at {position}."})
    else:
        out.append({"rule": "watermark", "status": "pass",
                    "evidence": f"watermark on" + (f" ({pos})" if pos else ""),
                    "suggestion": ""})
    return out


def check_captions(rules: dict, settings: dict | None) -> list[dict]:
    if not rules["captions_required"]:
        return []
    if settings is None or not settings.get("caption_mode"):
        return [{"rule": "captions_required", "status": "watch",
                 "evidence": "cannot verify caption mode for this job",
                 "suggestion": "Re-render with captions burned in (animated or stationary)."}]
    mode = settings["caption_mode"]
    return [{"rule": "captions_required", "status": "pass",
             "evidence": f"{mode} captions burned in", "suggestion": ""}]


def check_language(rules: dict, language: str) -> list[dict]:
    want = rules["language"]
    if not want:
        return []
    got = (language or "").lower()
    ok = got == want or got.startswith(want + "-")
    return [{"rule": "language", "status": "pass" if ok else "fail",
             "evidence": f"clip language {got or 'unknown'} (campaign wants {want})",
             "suggestion": "" if ok else
                           f"Pick a {want} source video — this campaign only accepts {want} clips."}]


# --------------------------------------------------------------------------- #
# Per-clip audit + corrected metadata
# --------------------------------------------------------------------------- #
def _apply_fixes(meta: dict, rules: dict, brand: str) -> dict:
    """Build corrected, paste-ready metadata when any title/hashtag rule failed."""
    ig_title = meta["instagram"]["title"]
    yt_title = meta["youtube"]["title"]
    stem = (brand or (rules["required_title_words"] or [""])[0]).strip()
    missing_ig = [w for w in rules["required_title_words"]
                  if w and w not in ig_title.lower()]
    missing_yt = [w for w in rules["required_title_words"]
                  if w and w not in yt_title.lower()]
    changed_title = False
    if missing_ig and stem:
        ig_title = f"{publish._capitalise(stem)} \u2014 {ig_title}"
        changed_title = True
    if missing_yt and stem:
        cand = f"{publish._capitalise(stem)} \u2014 {yt_title}"
        if len(cand) <= publish.YT_TITLE_MAX:
            yt_title = cand
    for w in rules["banned_title_words"]:
        ig_title = re.sub(rf"\b{re.escape(w)}\b", "", ig_title, flags=re.I)
        yt_title = re.sub(rf"\b{re.escape(w)}\b", "", yt_title, flags=re.I)
    cap = rules["title_max_chars"] or publish.YT_TITLE_MAX
    yt_title = publish._trim(yt_title, min(cap, publish.YT_TITLE_MAX))

    hashtags = list(meta["instagram"]["hashtags"])
    for t in rules["required_hashtags"]:
        if t not in hashtags:
            hashtags.append(t)
    hashtags = [t for t in hashtags if t not in set(rules["banned_hashtags"])]
    if rules["max_hashtags"] > 0:
        hashtags = hashtags[: rules["max_hashtags"]]

    ig_caption = meta["instagram"]["caption"]
    if changed_title or hashtags != meta["instagram"]["hashtags"]:
        head = ig_caption.split("\n\n")[0]
        rest = ig_caption.split("\n\n")[1:]
        # replace the trailing hashtag line, keep body lines
        body = [p for p in rest if not p.startswith("#")]
        ig_caption = "\n\n".join([ig_title] + body + [" ".join(hashtags)])

    yt_desc = meta["youtube"]["description"]
    desc_tags = " ".join(rules["required_hashtags"])
    if desc_tags and desc_tags.lower() not in yt_desc.lower():
        yt_desc = f"{yt_desc}\n\n{desc_tags}"
    for w in rules["required_description_words"]:
        if w and w.lower() not in yt_desc.lower():
            yt_desc = f"{yt_desc}\n{w}"

    return {
        "instagram_caption": ig_caption,
        "instagram_hashtags": hashtags,
        "youtube_title": yt_title,
        "youtube_description": yt_desc,
    }


def audit_clip(campaign: dict, *, meta: dict, words, start: float, end: float,
               language: str = "", settings: dict | None = None) -> dict:
    rules = campaign["rules"]
    duration = float(end - start)
    checks: list[dict] = [
        check_title_words(rules["required_title_words"], meta, campaign.get("brand", "")),
        check_banned_title_words(rules["banned_title_words"], meta),
        check_title_length(rules["title_max_chars"], meta, campaign.get("platform", "other")),
    ]
    checks += check_hashtags(rules, meta)
    checks += check_description(rules, meta)
    checks += check_duration(rules, duration)
    checks.append(check_banned_words(rules["banned_words"], words, start, end))
    checks += check_watermark(rules, settings)
    checks += check_captions(rules, settings)
    checks += check_language(rules, language)
    # drop placeholder "no X rule" checks so they don't pad the score
    checks = [c for c in checks
              if not re.match(r"^no [a-z\- ]+ rule$", c["evidence"])]

    score = round(
        100 * sum(1.0 if c["status"] == "pass" else 0.5 if c["status"] == "watch" else 0.0
                  for c in checks) / max(1, len(checks))
    )
    return {
        "clip": meta["clip"],
        "score": score,
        "checks": checks,
        "suggested_fixes": _apply_fixes(meta, rules, campaign.get("brand", "")),
        "publish": {"instagram": meta["instagram"], "youtube": meta["youtube"]},
    }


# --------------------------------------------------------------------------- #
# Job-level analysis (runs from disk — no in-memory job needed)
# --------------------------------------------------------------------------- #
def analyze_job(campaign: dict, workdir: Path) -> dict:
    workdir = Path(workdir)
    tr_path, hl_path = workdir / "transcript.json", workdir / "highlights.json"
    if not tr_path.is_file() or not hl_path.is_file():
        raise FileNotFoundError(
            f"{workdir.name} has no transcript/highlights — run the pipeline on it first"
        )
    transcript = load_transcript(tr_path)
    highlights = json.loads(hl_path.read_text(encoding="utf-8")).get("highlights", [])
    settings: dict | None = None
    s_path = workdir / "settings.json"
    if s_path.is_file():
        try:
            settings = json.loads(s_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = None

    clips: list[dict] = []
    for i, hl in enumerate(highlights, 1):
        meta = publish.build_report(
            transcript.words, float(hl["start"]), float(hl["end"]),
            title=hl.get("title", ""), index=i,
        )
        clip = audit_clip(
            campaign, meta=meta, words=transcript.words,
            start=float(hl["start"]), end=float(hl["end"]),
            language=transcript.language, settings=settings,
        )
        clips.append(clip)

    active = [k for k, v in campaign["rules"].items() if v not in ("", 0, 0.0, False, [])]
    # some checks cover several rule keys at once (watermark required + position)
    active_names = {_CHECK_NAME.get(k, k) for k in active}
    all_checks = [c for clip in clips for c in clip["checks"]]
    scored = [c for c in all_checks if c["rule"] in active_names]
    score = round(100 * sum(1.0 if c["status"] == "pass" else 0.5 if c["status"] == "watch" else 0.0
                            for c in scored) / max(1, len(scored)))
    grade = next(label for cutoff, label in GRADES if score >= cutoff)

    rollup: dict[str, dict] = {}
    for c in scored:
        if c["status"] == "fail":
            entry = rollup.setdefault(c["rule"], {"rule": c["rule"], "fails": 0, "clips": []})
            entry["fails"] += 1
            entry["clips"].append(clip_index_label(clips, c))

    report = {
        "campaign": campaign,
        "job": workdir.name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "clips_audited": len(clips),
        "active_rules": active,
        "score": score,
        "grade": grade,
        "failing_rules": sorted(rollup.values(), key=lambda r: -r["fails"]),
        "clips": clips,
    }
    write_report(workdir / f"campaign_{campaign['id']}_report.json", report)
    write_report(workdir / f"campaign_{campaign['id']}_report.md", report, markdown=True)
    return report


def clip_index_label(clips: list[dict], check: dict) -> str:
    # identity, not equality: identical failing checks repeat across clips
    for clip in clips:
        if any(check is c for c in clip["checks"]):
            return f"clip_{clip['clip']['index']:02d}"
    return "?"


# --------------------------------------------------------------------------- #
# Report IO
# --------------------------------------------------------------------------- #
def render_markdown(report: dict) -> str:
    camp = report["campaign"]
    lines = [
        f"# Campaign audit \u2014 {camp['name']} \u00d7 job {report['job']}",
        "",
        f"**Score: {report['score']}/100 \u2014 {report['grade']}**  ",
        f"**{report['clips_audited']} clips** audited against "
        f"{len(report['active_rules'])} active rules ({camp['platform']})",
        "",
    ]
    if report["failing_rules"]:
        lines += ["## Rules failing the most", "",
                  "| Rule | Failing clips |", "|------|---------------|"]
        for r in report["failing_rules"]:
            lines.append(f"| {r['rule']} | {', '.join(r['clips'])} |")
        lines.append("")
    icon = {"pass": "\u2705", "watch": "👀", "fail": "\u274c"}
    for clip in report["clips"]:
        c = clip["clip"]
        lines += [
            f"## Clip {c['index']:02d} \u2014 {clip['score']}/100"
            + (f" (\u201c{c['title']}\u201d)" if c["title"] else "")
            + f"  \n*{c['start']:.1f}s\u2013{c['end']:.1f}s \u00b7 {c['duration']:.1f}s*",
            "",
            "| Rule | Status | Evidence | Suggestion |",
            "|------|--------|----------|------------|",
        ]
        for ch in clip["checks"]:
            lines.append(
                f"| {ch['rule']} | {icon[ch['status']]} {ch['status']} "
                f"| {ch['evidence']} | {ch['suggestion'] or '\u2014'} |"
            )
        fixes = clip["suggested_fixes"]
        lines += ["", "**Ready-to-post corrections:**", ""]
        lines += [
            "```text",
            "IG caption:",
            fixes["instagram_caption"],
            "",
            "YT title:",
            fixes["youtube_title"],
            "",
            "YT description:",
            fixes["youtube_description"],
            "```",
            "",
        ]
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: dict, *, markdown: bool = False) -> Path:
    if markdown:
        path.write_text(render_markdown(report), encoding="utf-8")
    else:
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Job directories on disk (campaign audits don't need the live job list)
# --------------------------------------------------------------------------- #
def list_jobdirs() -> list[dict]:
    out: list[dict] = []
    if not config.JOBS_DIR.is_dir():
        return out
    for d in config.JOBS_DIR.iterdir():
        if not d.is_dir() or not (d / "highlights.json").is_file():
            continue
        try:
            highlights = json.loads((d / "highlights.json").read_text(encoding="utf-8"))
            n = len(highlights.get("highlights", []))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": d.name,
            "clips": n,
            "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
            "has_settings": (d / "settings.json").is_file(),
        })
    return sorted(out, key=lambda j: j["created"], reverse=True)


def job_workdir(job_id: str) -> Path:
    """Validate a job id and return its workdir (rejects traversal)."""
    name = str(job_id or "")
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("invalid job id")
    workdir = (config.JOBS_DIR / name).resolve()
    if not workdir.is_relative_to(config.JOBS_DIR.resolve()):
        raise ValueError("invalid job id")
    return workdir


def report_path(cid: str, job_id: str, *, markdown: bool = False) -> Path:
    """Where an audit report lives for (campaign, job) — validated job id."""
    ext = "md" if markdown else "json"
    return job_workdir(job_id) / f"campaign_{_slug(cid)}_report.{ext}"


# --------------------------------------------------------------------------- #
# CLI: python -m app.pipeline.campaigns <jobdir> --campaign <id-or-file>
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    p = argparse.ArgumentParser(description="Audit a finished job against campaign rules")
    p.add_argument("job", nargs="?", help="job id (folder under data/jobs)")
    p.add_argument("--campaign", help="campaign id or path to its JSON")
    p.add_argument("--extract", metavar="FILE",
                   help="print the rules parsed from a rules file "
                        "(PDF/TXT/MD/DOCX/JSON) and exit")
    args = p.parse_args(argv)
    if args.extract:
        path = Path(args.extract)
        out = read_rules_file(path.name, path.read_bytes())
        print(json.dumps({"found": out["found"], "rules": out["rules"]},
                         indent=2, ensure_ascii=False))
        return 0
    if not args.job or not args.campaign:
        p.error("job id and --campaign are required (or use --extract FILE)")
    path = Path(args.campaign)
    campaign = (json.loads(path.read_text(encoding="utf-8")) if path.is_file()
                else load_campaign(args.campaign))
    report = analyze_job(campaign, job_workdir(args.job))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
