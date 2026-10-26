"""Stage 5b: the HOOK playbook — scored hook options + a retention beat sheet.

Grounded in a research pass over high-performing (1M+ view) YouTube Shorts and
Instagram Reels. The pattern they share: the first 1–3 seconds decide the
swipe, and the winning openings are almost always one of eight archetypes:

  1. open_loop       information gap — name the payoff, withhold one detail
  2. bold_claim      contrarian / "stop doing X" claim
  3. relatable       hyper-specific moment the target viewer recognises
  4. result_first    show the outcome, then rewind to how
  5. pattern_interrupt  unexpected visual/sound in frame 1
  6. shareable       "send this to someone who…" framing
  7. mid_action      cut the intro; start inside the moment
  8. transformation  before/after promise

This module is deterministic (no LLM call): it detects the clip's payoff
moment from the word transcript (intensifiers, negations, numbers, exclamations
— weighted toward the later part of the clip, where payoffs live), then writes
one hook option per archetype. Each option carries a spoken line (~12 words),
on-screen text that must work muted, the recommended first-frame visual and the
payoff it promises. Every hook is scored 1–5 on clarity, curiosity gap,
relevance, works-muted and honesty; anything that outruns the actual payoff is
flagged `clickbait_risk`. The winner gets a beat sheet (a new visual beat
every ~4–8s, cut points at real silences in the transcript) and an A/B test
plan where only the hook changes between versions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app import config
from app.pipeline.transcribe import Word

MAX_QUOTE_WORDS = 6      # payoff quote length
MAX_TOPIC_WORDS = 4      # topic phrase length when no highlight title exists
BEAT_INTERVAL = 6.0      # seconds between retention beats (4–8s window)
MIN_GAP_FOR_CUT = 0.45   # a real silence here = natural cut point

# Words that make a moment feel like a payoff (intensifiers, negations,
# stakes). Scored over a rolling window ending at each word.
PAYOFF_MARKERS = {
    "never", "nobody", "noone", "stop", "don't", "dont", "cannot", "can't",
    "cant", "insane", "crazy", "wild", "insane", "unbelievable", "impossible",
    "secret", "actually", "finally", "wait", "best", "worst", "biggest",
    "first", "last", "only", "why", "how", "what", "wow", "omg", "literally",
    "actually", "ended", "destroyed", "obliterated", "record", "million",
    "beat", "won", "lost", "shocked", "screaming", "everybody", "everyone",
}

# Archetype table: (key, display name, base scores) — base scores are
# [clarity, curiosity, relevance, muted, honesty] before content modifiers.
HOOK_TYPES: list[tuple[str, str, tuple[int, int, int, int, int]]] = [
    ("open_loop", "Open loop", (4, 5, 3, 4, 4)),
    ("bold_claim", "Bold / contrarian claim", (5, 4, 3, 5, 3)),
    ("relatable", "Hyper-specific relatable moment", (4, 3, 5, 4, 5)),
    ("result_first", "Result first, then rewind", (5, 4, 4, 4, 4)),
    ("pattern_interrupt", "Pattern interrupt", (3, 5, 3, 3, 3)),
    ("shareable", "Sharable — 'send this to…'", (4, 3, 4, 5, 5)),
    ("mid_action", "Mid-action cold start", (4, 4, 4, 3, 5)),
    ("transformation", "Transformation tease (before/after)", (4, 4, 4, 4, 4)),
]

BEAT_ACTIONS = [
    "punch zoom-in on the speaker's face",
    "cut/B-roll illustrating what the captions just said",
    "big text card restating the key phrase",
    "speed ramp or jump-cut to remove the pause",
    "reaction shot / crowd shot for energy",
    "hold on the speaker — let the line land",
]


@dataclass
class HookOption:
    hook_type: str
    name: str
    spoken_line: str        # ~12 words, spoken in the first 1–3 seconds
    onscreen_text: str      # must work with sound OFF
    first_frame_visual: str
    payoff_promise: str
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.scores.values()) / max(1, len(self.scores))

    @property
    def clickbait_risk(self) -> bool:
        return self.scores.get("honesty", 5) < 4

    def to_dict(self) -> dict:
        return {
            "hook_type": self.hook_type,
            "name": self.name,
            "spoken_line": self.spoken_line,
            "onscreen_text": self.onscreen_text,
            "first_frame_visual": self.first_frame_visual,
            "payoff_promise": self.payoff_promise,
            "scores": dict(self.scores),
            "total": round(self.total, 2),
            "clickbait_risk": self.clickbait_risk,
        }


# --------------------------------------------------------------------------- #
# Transcript helpers
# --------------------------------------------------------------------------- #
def words_in(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w.end > start and w.start < end]


def _clean(token: str) -> str:
    return re.sub(r"[^\w']", "", token).lower()


def detect_payoff(words: list[Word], start: float, end: float) -> tuple[str, float]:
    """Most surprising/emotional/useful moment in the clip.

    Returns (quote, absolute_timestamp). Payoffs live late — words in the back
    half of the clip get a recency bonus. Falls back to the clip's final words.
    """
    inside = words_in(words, start, end)
    if not inside:
        return "", start
    n = len(inside)
    best_i, best_score = n - 1, -1.0
    for i, w in enumerate(inside):
        window = inside[max(0, i - 5): i + 1]
        hits = sum(1 for v in window if _clean(v.text) in PAYOFF_MARKERS)
        hits += sum(1 for v in window if any(c in v.text for c in "?!"))
        hits += sum(1 for v in window if any(ch.isdigit() for ch in v.text))
        recency = (i / max(1, n - 1)) * 2.0
        score = hits + recency
        if score > best_score:
            best_i, best_score = i, score
    lo = max(0, best_i - MAX_QUOTE_WORDS // 2)
    hi = min(n, lo + MAX_QUOTE_WORDS)
    quote = " ".join(w.text.strip() for w in inside[lo:hi]).strip(" ,.!?;:")
    return quote, inside[best_i].start


def payoff_strength(words: list[Word], start: float, end: float) -> int:
    """How much genuine payoff evidence the clip contains (marker hits)."""
    return sum(
        1
        for w in words_in(words, start, end)
        if _clean(w.text) in PAYOFF_MARKERS
        or any(c in w.text for c in "?!")
        or any(ch.isdigit() for ch in w.text)
    )


# Leading connectives the heuristic/LLM highlight titles often start with —
# they read badly inside a hook line.
_CONNECTIVES = {
    "and", "but", "so", "then", "because", "now", "which", "when", "that",
    "if", "like", "just", "actually", "honestly", "well", "okay", "ok",
}


def topic_phrase(title: str, words: list[Word], start: float, end: float) -> str:
    """Short topic phrase: cleaned highlight title, else the clip's opening words.

    Titles are often sentence fragments ("and there is nothing particularly
    special") — strip leading connectives, cut at clause breaks, cap length,
    so the phrase reads inside a hook line and on screen.
    """
    t = re.sub(r"\s+", " ", (title or "").strip()).strip(" .!?,;:")
    if t:
        toks = t.split(" ")
        while toks and toks[0].lower().rstrip(",") in _CONNECTIVES:
            toks.pop(0)
        t = " ".join(toks)
        for sep in (",", " —", " --"):
            if sep in t:
                t = t.split(sep, 1)[0]
        t = " ".join(t.split(" ")[:MAX_TOPIC_WORDS]).strip()
        if t:
            return t if len(t) <= 34 else t[:34].rsplit(" ", 1)[0].rstrip() + "…"
    inside = words_in(words, start, end)[:MAX_TOPIC_WORDS]
    return " ".join(w.text.strip(" ,.!?;:") for w in inside) or "this"


# --------------------------------------------------------------------------- #
# Hook generation
# --------------------------------------------------------------------------- #
def generate_hooks(
    words: list[Word],
    start: float,
    end: float,
    *,
    title: str = "",
    platform: str = "both",
    niche: str = "",
    viewer: str = "",
) -> list[HookOption]:
    """One scored option per archetype, built from this clip's own words."""
    topic = topic_phrase(title, words, start, end)
    payoff, payoff_t = detect_payoff(words, start, end)
    payoff = payoff or topic
    payoff_rel = max(0.0, payoff_t - start)
    payoff_frame = (
        f"Frame 1 = the payoff shot at +{payoff_rel:.1f}s "
        f"(« {payoff[:40]} ») played cold, no intro"
    )

    options: list[HookOption] = [
        HookOption(
            "open_loop", "Open loop",
            "Watch till the end — nobody warns you about this part coming next",
            f"{topic.upper()} — WAIT FOR IT…",
            "Speaker mid-sentence, tight crop, one line of OST naming the topic",
            f"Delivers « {payoff[:50]} » at +{payoff_rel:.1f}s",
        ),
        HookOption(
            "bold_claim", "Bold / contrarian claim",
            "Stop scrolling — everything you assume about this is wrong",
            f"STOP SCROLLING 🛑 {topic.upper()}",
            "Hard cut on a pointed finger / direct-to-camera stare",
            f"Proves it with « {payoff[:50]} »",
        ),
        HookOption(
            "relatable", "Hyper-specific relatable moment",
            "If you've ever dealt with exactly this, you're not the only one",
            f"YOU DO THIS TOO — {topic.upper()}",
            "Deadpan close-up of the relatable moment, paused",
            f"The « {payoff[:50]} » everyone recognises",
        ),
        HookOption(
            "result_first", "Result first, then rewind",
            f"Here's how it ends: “{payoff[:30]}” — now here's how it happened",
            f"THE ENDING FIRST 👀 {topic.upper()}",
            payoff_frame,
            "The whole clip, rewound from the outcome",
        ),
        HookOption(
            "pattern_interrupt", "Pattern interrupt",
            f"Wait — “{payoff[:24]}”? That changes everything",
            f"WAIT… WHAT?! 😳 {topic.upper()}",
            payoff_frame,
            f"The context behind « {payoff[:50]} »",
        ),
        HookOption(
            "shareable", "Sharable — 'send this to…'",
            "Send this to someone who needs to see it today",
            f"SEND THIS TO SOMEONE WHO… {topic.upper()}",
            "Speaker holds eye contact, points at camera",
            f"« {payoff[:50]} » — the reason to share",
        ),
        HookOption(
            "mid_action", "Mid-action cold start",
            (f"“{payoff}” — you need the context" if payoff
             else "…it happens mid-sentence, no intro"),
            f"STARTED IN THE MIDDLE OF {topic.upper()}",
            "Cut in on motion — no title, no greeting",
            "The payoff IS the opening; the clip explains it",
        ),
        HookOption(
            "transformation", "Transformation tease",
            "From the very start to the moment everybody repeats — watch it all",
            f"BEFORE ➜ AFTER: {topic.upper()}",
            "Split-screen or hard cut: setup frame → payoff frame",
            f"The full arc ending on « {payoff[:50]} »",
        ),
    ]

    # No real payoff detected → say so: honesty drops on every promise.
    weak_payoff = payoff_strength(words, start, end) == 0 or len(payoff.split()) < 2
    table = {key: scores for key, _, scores in HOOK_TYPES}
    for opt in options:
        clarity, curiosity, relevance, muted, honesty = table[opt.hook_type]
        # content modifiers: shorter OST reads faster muted; explicit stakes help
        if len(opt.onscreen_text) <= 28:
            muted += 1
        if len(opt.onscreen_text) > 44:
            muted -= 1
        if opt.hook_type in {"shareable", "relatable"}:
            relevance += 1 if (niche or viewer) else 0
        if opt.hook_type in {"result_first", "mid_action", "pattern_interrupt"} \
                and weak_payoff:
            honesty = min(honesty, 3)   # promises a payoff the clip may lack
        elif opt.hook_type in {"bold_claim", "pattern_interrupt"} and not weak_payoff:
            honesty = min(5, honesty + 1)  # the clip genuinely delivers
        opt.scores = {
            "clarity": max(1, min(5, clarity)),
            "curiosity_gap": max(1, min(5, curiosity)),
            "relevance": max(1, min(5, relevance)),
            "works_muted": max(1, min(5, muted)),
            "honesty": max(1, min(5, honesty)),
        }
    # stable rank: score desc, then archetype order
    order = {key: i for i, (key, _, _) in enumerate(HOOK_TYPES)}
    options.sort(key=lambda o: (-o.total, order[o.hook_type]))
    return options


# --------------------------------------------------------------------------- #
# Retention edit plan
# --------------------------------------------------------------------------- #
def beat_sheet(words: list[Word], start: float, end: float) -> list[dict]:
    """A new visual/story beat every ~4–8s, cut points at real silences."""
    inside = words_in(words, start, end)
    duration = max(0.0, end - start)
    if not inside:
        return []
    beats: list[dict] = []
    # candidate cut points: silences between words, inside the clip
    cuts: list[float] = []
    prev_end = inside[0].end
    for w in inside[1:]:
        if w.start - prev_end >= MIN_GAP_FOR_CUT:
            cuts.append(w.start)
        prev_end = w.end

    next_beat = BEAT_INTERVAL
    last_t = -1.0
    for i, w in enumerate(inside):
        rel = w.start - start  # seconds from clip start
        if rel >= next_beat or i == len(inside) - 1:
            # nearest silence to the target beat, else the word itself — but
            # never a beat that doesn't advance past the previous one
            cut_rel = (
                min(cuts, key=lambda c: abs(c - w.start)) - start if cuts else rel
            )
            cand = cut_rel if cut_rel > last_t + 2.0 else rel
            t = round(min(max(cand, last_t + 0.5), duration), 2)
            if t > last_t and t <= duration:
                line = " ".join(v.text for v in inside[i:i + 4])
                beats.append({
                    "t": t,
                    "action": BEAT_ACTIONS[len(beats) % len(BEAT_ACTIONS)],
                    "caption_beat": line[:48],
                })
                last_t = t
            next_beat += BEAT_INTERVAL
            if last_t >= duration:
                break
    return beats


def test_plan(winner: HookOption, runner_up: HookOption) -> dict:
    """A/B plan: change ONLY the hook between versions."""
    return {
        "first_test": f"A: « {winner.spoken_line} » vs B: « {runner_up.spoken_line} »",
        "rule": "Change ONLY the hook between versions — same cut, captions and music.",
        "weak_viewed_vs_swiped": f"Fix the hook: swap in B ({runner_up.name}) or try another archetype.",
        "mid_video_drop": "Early retention fine but a drop mid-video → fix pacing/clarity at that timestamp (see beat sheet).",
    }


def build_report(
    words: list[Word],
    start: float,
    end: float,
    *,
    title: str = "",
    platform: str = "both",
    niche: str = "",
    viewer: str = "",
) -> dict:
    """Full hook playbook for one clip, as a JSON-serialisable dict."""
    hooks = generate_hooks(
        words, start, end, title=title, platform=platform, niche=niche, viewer=viewer
    )
    payoff, payoff_t = detect_payoff(words, start, end)
    top3 = hooks[:3]
    winner, runner = top3[0], top3[1]
    return {
        "platform": platform or "both",
        "niche": niche,
        "viewer": viewer,
        "clip": {"start": round(start, 2), "end": round(end, 2), "title": title},
        "best_moment": {"quote": payoff, "at": round(payoff_t, 2),
                        "rel": round(max(0.0, payoff_t - start), 2)},
        "hooks": [h.to_dict() for h in hooks],
        "top3": [h.hook_type for h in top3],
        "winner": winner.to_dict(),
        "retention": {
            "recommended_length_s": 35 if (end - start) <= 45 else 45,
            "beat_sheet": beat_sheet(words, start, end),
            "captions": "word-by-word, high contrast, large",
            "ending": "loop-friendly last line/frame + a comment/share trigger that fits the content",
        },
        "test_plan": test_plan(winner, runner),
        "assumptions": (
            ["No distinctive payoff detected — hooks promise less; consider a stronger clip."]
            if len(payoff.split()) < 2 else []
        ),
    }


def render_markdown(report: dict) -> str:
    """Human-readable playbook (same structure as the strategist output)."""
    w = report["winner"]
    lines = [
        f"# Hook playbook — {report['clip']['title'] or 'clip'} "
        f"({report['platform']})",
        "",
        f"**Payoff moment:** « {report['best_moment']['quote'] or '—'} » "
        f"at +{report['best_moment']['rel']:.1f}s",
        "",
        "| # | Type | Spoken line | OST (muted-safe) | Clarity | Curiosity | Relevance | Muted | Honest | Risk |",
        "|---|------|-------------|------------------|---------|-----------|-----------|-------|--------|------|",
    ]
    for i, h in enumerate(report["hooks"], 1):
        s = h["scores"]
        lines.append(
            f"| {i} | {h['name']} | {h['spoken_line']} | {h['onscreen_text']} "
            f"| {s['clarity']} | {s['curiosity_gap']} | {s['relevance']} "
            f"| {s['works_muted']} | {s['honesty']} "
            f"| {'⚠️ clickbait' if h['clickbait_risk'] else '—'} |"
        )
    lines += [
        "",
        f"## Winner: {w['name']}  (score {w['total']:.1f}/5)",
        f"- **Say:** {w['spoken_line']}",
        f"- **On screen:** {w['onscreen_text']}",
        f"- **First frame:** {w['first_frame_visual']}",
        f"- **Promises:** {w['payoff_promise']}",
        "",
        "## Retention edit plan",
        f"- Length: ~{report['retention']['recommended_length_s']}s",
        f"- Captions: {report['retention']['captions']}",
        f"- Ending: {report['retention']['ending']}",
        "",
        "| Beat (s) | Edit | Caption focus |",
        "|----------|------|---------------|",
    ]
    for b in report["retention"]["beat_sheet"]:
        lines.append(f"| {b['t']:.1f} | {b['action']} | {b['caption_beat']} |")
    tp = report["test_plan"]
    lines += [
        "",
        "## A/B test",
        f"- First test: {tp['first_test']}",
        f"- Rule: {tp['rule']}",
        f"- Weak viewed-vs-swiped: {tp['weak_viewed_vs_swiped']}",
        f"- Mid-video drop: {tp['mid_video_drop']}",
    ]
    for note in report.get("assumptions", []):
        lines += ["", f"**Assumption:** {note}"]
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: dict, *, markdown: bool = False) -> Path:
    if markdown:
        path.write_text(render_markdown(report), encoding="utf-8")
    else:
        import json
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
