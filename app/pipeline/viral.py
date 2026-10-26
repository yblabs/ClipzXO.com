"""Stage 5c: VIRAL ANALYSIS — how 1M+ creator/streamer Shorts are cut.

A research pass over high-performing creator & streamer Shorts (clip channels
in the xQc / Kai Cenat / iShowSpeed / MrBeast orbit, professional clip editors'
workflows, and published retention analyses) distilled into checks this
pipeline can actually run against a clip's word transcript and timing.

The shared lesson of that footage:

  * open on the moment, never on a greeting — the swipe decision lands in the
    first ~1–3 seconds;
  * a single-beat reaction/comedy moment holds attention best around 18–30s;
    past ~45s every beat must earn itself;
  * the payoff must exist, land late enough to build, and get room to react;
  * cut dead air — pauses that don't carry tension only invite the swipe;
  * the ending should reward a rewatch (echo/open loop), not trail off;
  * captions must make it comprehensible with sound OFF;
  * a cold viewer needs one line of who/what context in frame one;
  * discussion invites must be genuine — no manufactured cut-offs.

Honesty note: the numeric benchmarks (length bands, first-second figures) are
testable defaults sourced in RESEARCH below — not guaranteed formulas. Every
check reports evidence and a concrete, timestamped fix, and the per-clip score
is just the weighted sum of those checks.

Deterministic (no LLM): reuses the hook module's payoff detection so the hook
playbook and the viral analysis always agree on where the payoff sits.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.pipeline.hooks import (
    detect_payoff,
    payoff_strength,
    topic_phrase,
    words_in,
)

# --------------------------------------------------------------------------- #
# Research knowledge base (the "trained" patterns + where they come from)
# --------------------------------------------------------------------------- #
RESEARCH: list[dict] = [
    {
        "pattern": "open_on_the_moment",
        "what": "First frame = the strongest reaction/claim; no greeting, no intro, no logo.",
        "why": "The swipe decision lands in the first ~1–3s; 50–60% of drop-offs happen in the first three seconds.",
        "source": "https://www.marketingbrew.com/stories/2022/11/02/how-mrbeast-builds-amazing-social-media-hooks",
    },
    {
        "pattern": "length_for_the_arc",
        "what": "Single-beat reaction/comedy moment: start by testing ~18–30s; >45s only if every beat advances the story.",
        "why": "15–30s is a high-retention range (often >80%); clips over 45s tend to drop off unless the content sustains interest.",
        "source": "https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention",
    },
    {
        "pattern": "payoff_lands_and_loops",
        "what": "Build to a real payoff, hold ~1s for the reaction, end cleanly or echo the opening so a replay feels natural.",
        "why": "Rewatch-friendly endings (callback/loop) earn replays; a payoff that arrives with no room to land wastes the build.",
        "source": "https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention",
    },
    {
        "pattern": "captions_for_muted",
        "what": "Burned-in captions, short high-contrast lines, key words emphasised; keep them off the face.",
        "why": "A large share of viewers watch with sound off and rely on captions (up to ~69% in public per the Verizon/Publicis survey); bad captions raise cognitive load.",
        "source": "https://pmc.ncbi.nlm.nih.gov/articles/PMC11458047/",
    },
    {
        "pattern": "one_line_of_context",
        "what": "Frame one answers who/what/why-now for a cold viewer — one short on-screen line or the visible evidence itself.",
        "why": "Stream-clip viewers weren't in the chat; YouTube's own Shorts tools push text/voiceover precisely to add missing context fast.",
        "source": "https://support.google.com/youtube/answer/13380879?hl=en",
    },
    {
        "pattern": "cut_dead_air",
        "what": "Remove greetings, waiting, repeated explanations and pauses that carry no tension; keep pauses that do.",
        "why": "Professional clip editors screen VODs for the peak then trim slow intros/dead air — forward motion is the baseline.",
        "source": "https://clippingagency.co/streamer-clipping-service/",
    },
    {
        "pattern": "honest_bait",
        "what": "Titles and discussion asks are accurate and specific; never cut off the payoff to force 'part 2'.",
        "why": "YouTube's policy warns against misleading metadata/deceptive engagement; Shorts already get fewer comments per view.",
        "source": "https://support.google.com/youtube/answer/6390658?hl=en",
    },
    {
        "pattern": "measure_and_iterate",
        "what": "Judge clips by 'chose to view' and retention curves in YouTube Analytics; compare similar clips, not one outlier.",
        "why": "YouTube explicitly recommends finding patterns across your better and worse Shorts instead of chasing a formula.",
        "source": "https://support.google.com/youtube/answer/12942217?hl=en",
    },
]
SOURCE_BY_KEY = {p["pattern"]: p["source"] for p in RESEARCH}

# --------------------------------------------------------------------------- #
# Check constants
# --------------------------------------------------------------------------- #
GREETING_TOKENS = {
    "hey", "hi", "hello", "hiya", "heya", "yo", "sup", "hola",
    "welcome", "greetings", "what's", "whats",
}
FILLER_TOKENS = {"um", "uh", "erm", "hmm", "ah"}
LEAD_CONNECTIVES = {"so", "well", "okay", "ok", "and", "but"}

DEAD_AIR_GAP_S = 0.8        # a pause this long is a jump-cut candidate
DEAD_AIR_FIX_PCT = 0.12     # ≥12% of the clip in dead air → fix
DEAD_AIR_WATCH_PCT = 0.05   # ≥5% → watch
TRAILING_FIX_S = 6.0        # more than 6s after the payoff → trim
DENSITY_FIX = 4.5           # caption words per second ceiling


def _clean(token: str) -> str:
    return re.sub(r"[^\w']", "", token).lower()


# --------------------------------------------------------------------------- #
# Individual checks — each returns {status, evidence, fix, deduction}
# --------------------------------------------------------------------------- #
def check_open(words, start: float, end: float) -> dict:
    """Open on the moment, not a greeting (first ~3 words)."""
    inside = words_in(words, start, end)
    if not inside:
        return {"status": "watch", "evidence": "no transcript words in window",
                "fix": "verify the first spoken line starts on the moment", "deduction": 6}
    toks = [_clean(w.text) for w in inside[:3]]
    if any(t in GREETING_TOKENS for t in toks):
        first_sub = next(
            (w.text.strip() for w in inside[3:] if _clean(w.text) not in GREETING_TOKENS
             and _clean(w.text) not in FILLER_TOKENS),
            None,
        )
        return {
            "status": "fix",
            "evidence": f"clip opens on a greeting: « {' '.join(w.text.strip() for w in inside[:3])} »",
            "fix": ("cut straight to the first substantive line"
                    + (f" (« {first_sub[:40]} »)" if first_sub else "")
                    + " — greetings cost the swipe"),
            "deduction": 15,
        }
    if toks and toks[0] in FILLER_TOKENS:
        return {
            "status": "fix",
            "evidence": f"clip opens on filler: « {inside[0].text.strip()} »",
            "fix": "trim the filler — start on the first word that carries meaning",
            "deduction": 12,
        }
    if toks and toks[0] in LEAD_CONNECTIVES:
        return {
            "status": "watch",
            "evidence": f"clip opens mid-thought: « {inside[0].text.strip()} … »",
            "fix": "start one beat later so the first line lands on its own",
            "deduction": 8,
        }
    return {"status": "pass", "evidence": "opens directly on content",
            "fix": "", "deduction": 0}


def check_length(duration: float) -> dict:
    """Length sweet spot for a single creator/streamer moment."""
    if duration <= 35:
        return {"status": "pass", "evidence": f"{duration:.1f}s — inside the 18–35s test band",
                "fix": "", "deduction": 0}
    if duration <= 45:
        return {
            "status": "watch",
            "evidence": f"{duration:.1f}s — past the ~35s comfort zone",
            "fix": "trim setup/padding toward ~30–35s; keep a beat only if it advances the arc",
            "deduction": 8,
        }
    return {
        "status": "fix",
        "evidence": f"{duration:.1f}s — well past the ~45s retention cliff",
        "fix": "split into two Shorts or cut hard to the peak; a single moment should hold ~18–35s",
        "deduction": 15,
    }


def check_payoff(words, start: float, end: float) -> dict:
    """Payoff exists, lands late enough, and leaves room for the reaction."""
    duration = max(0.0, end - start)
    strength = payoff_strength(words, start, end)
    quote, payoff_t = detect_payoff(words, start, end)
    if strength == 0 or len(quote.split()) < 2:
        return {
            "status": "fix",
            "evidence": "no measurable peak (no reaction/number/exclamation markers)",
            "fix": "pick a moment with a reaction, a number or a punchline — this window has nothing to build to",
            "deduction": 25,
        }
    rel = max(0.0, payoff_t - start)
    if duration and rel > duration - 2.0:
        return {
            "status": "fix",
            "evidence": f"payoff « {quote[:40]} » sits at +{rel:.1f}s of {duration:.1f}s — no room to land",
            "fix": "extend the clip 2–3s past the payoff so the reaction finishes",
            "deduction": 15,
        }
    if duration and rel < 0.35 * duration:
        return {
            "status": "watch",
            "evidence": f"payoff at +{rel:.1f}s of {duration:.1f}s — arrives early",
            "fix": "withhold more: start later or front-load context so tension builds first",
            "deduction": 10,
        }
    return {
        "status": "pass",
        "evidence": f"payoff « {quote[:40] } » at +{rel:.1f}s ({rel / duration * 100:.0f}% in)" if duration
        else "payoff detected",
        "fix": "", "deduction": 0,
    }


def check_dead_air(words, start: float, end: float) -> dict:
    """Pauses ≥0.8s inside the clip are jump-cut candidates."""
    inside = words_in(words, start, end)
    duration = max(0.0, end - start)
    if not inside or not duration:
        return {"status": "watch", "evidence": "no word timings to measure",
                "fix": "review for dead air manually", "deduction": 6}
    gaps: list[tuple[float, float]] = []  # (relative start, length)
    cursor = start
    for w in inside:
        if w.start - cursor >= DEAD_AIR_GAP_S:
            gaps.append((cursor - start, w.start - cursor))
        cursor = max(cursor, w.end)
    total = sum(g for _, g in gaps)
    share = total / duration
    if share >= DEAD_AIR_FIX_PCT:
        listed = ", ".join(f"+{t:.1f}s ({g:.1f}s)" for t, g in sorted(gaps, key=lambda x: -x[1])[:3])
        return {
            "status": "fix",
            "evidence": f"{total:.1f}s of dead air across {len(gaps)} pause(s) ({share * 100:.0f}% of clip): {listed}",
            "fix": f"jump-cut the pauses at {', '.join(f'+{t:.1f}s' for t, _ in sorted(gaps, key=lambda x: x[0])[:4])}",
            "deduction": 15,
        }
    if share >= DEAD_AIR_WATCH_PCT:
        return {
            "status": "watch",
            "evidence": f"{total:.1f}s of pauses ({share * 100:.0f}% of clip)",
            "fix": "tighten the longest pause (keep any that carry tension)",
            "deduction": 8,
        }
    return {"status": "pass", "evidence": f"only {total:.1f}s of pauses ({share * 100:.0f}%)",
            "fix": "", "deduction": 0}


def check_ending(words, start: float, end: float) -> dict:
    """End near the payoff — no post-payoff chatter that kills the rewatch."""
    duration = max(0.0, end - start)
    _, payoff_t = detect_payoff(words, start, end)
    rel = max(0.0, payoff_t - start)
    trailing = max(0.0, duration - rel)
    if trailing > TRAILING_FIX_S:
        return {
            "status": "fix",
            "evidence": f"{trailing:.1f}s of talk after the payoff",
            "fix": f"end ~1s after the reaction — trim {trailing - 1:.0f}s of post-payoff chatter for a rewatch-friendly finish",
            "deduction": 12,
        }
    return {
        "status": "pass",
        "evidence": f"ends {trailing:.1f}s after the payoff — close to the landing",
        "fix": "", "deduction": 0,
    }


def check_caption_density(words, start: float, end: float) -> dict:
    """Muted viewing: caption lines must stay readable at speaking pace."""
    inside = words_in(words, start, end)
    duration = max(0.0, end - start)
    if not inside or not duration:
        return {"status": "watch", "evidence": "no word timings",
                "fix": "check caption readability", "deduction": 4}
    wps = len(inside) / duration
    if wps > DENSITY_FIX:
        return {
            "status": "watch",
            "evidence": f"{wps:.1f} caption words/second — dense to read muted",
            "fix": "trim caption lines to fewer words on screen at once (keep every word in the audio)",
            "deduction": 8,
        }
    return {
        "status": "pass",
        "evidence": f"{wps:.1f} words/second — readable with sound off (captions are burned in)",
        "fix": "", "deduction": 0,
    }


def check_cold_context(words, start: float, end: float, *, title: str = "") -> dict:
    """A cold viewer needs who/what in the opening — card or visible evidence."""
    inside = words_in(words, start, end)
    head = inside[:8]
    named = any(w.text[:1].isupper() or any(ch.isdigit() for ch in w.text) for w in head)
    if (title or "").strip() or named:
        topic = topic_phrase(title, words, start, end)
        return {
            "status": "pass",
            "evidence": f"context available from « {topic} » / opening words",
            "fix": "", "deduction": 0,
        }
    topic = topic_phrase(title, words, start, end)
    return {
        "status": "watch",
        "evidence": "no title, name or number in the opening — a cold viewer lacks who/what",
        "fix": f"add one on-screen context line in frame 1, e.g. « {topic.upper()} »",
        "deduction": 8,
    }


def check_discussion(words, start: float, end: float) -> dict:
    """Invite genuine discussion; never manufacture a cut-off."""
    inside = words_in(words, start, end)
    has_q = any("?" in w.text for w in inside)
    if has_q:
        return {
            "status": "pass",
            "evidence": "clip contains a real question",
            "fix": "pin that question as the pinned comment to host the discussion",
            "deduction": 0,
        }
    return {
        "status": "watch",
        "evidence": "no discussion prompt in the clip",
        "fix": "add one honest, specific question to the caption (never cut the payoff short)",
        "deduction": 5,
    }


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
STATUS_ORDER = {"fix": 0, "watch": 1, "pass": 2}


def _check(key: str, name: str, result: dict) -> dict:
    return {
        "pattern": key,
        "name": name,
        "status": result["status"],
        "evidence": result["evidence"],
        "fix": result["fix"],
        "deduction": result["deduction"],
        "source": SOURCE_BY_KEY.get(key, ""),
    }


def grade(score: int) -> str:
    if score >= 85:
        return "strong — publish-ready"
    if score >= 70:
        return "solid — one or two fixes away"
    return "needs work — apply the fixes below first"


def build_report(
    words,
    start: float,
    end: float,
    *,
    title: str = "",
) -> dict:
    """Viral analysis for one clip: pattern checks + prioritized fixes."""
    duration = max(0.0, end - start)
    checks = [
        _check("open_on_the_moment", "Open on the moment", check_open(words, start, end)),
        _check("length_for_the_arc", "Length for the arc", check_length(duration)),
        _check("payoff_lands_and_loops", "Payoff lands & loops", check_payoff(words, start, end)),
        _check("cut_dead_air", "Cut dead air", check_dead_air(words, start, end)),
        _check("payoff_lands_and_loops", "Ending rewards rewatch", check_ending(words, start, end)),
        _check("captions_for_muted", "Captions readable muted", check_caption_density(words, start, end)),
        _check("one_line_of_context", "Cold-viewer context", check_cold_context(words, start, end, title=title)),
        _check("honest_bait", "Honest discussion ask", check_discussion(words, start, end)),
    ]
    score = max(0, 100 - sum(c["deduction"] for c in checks))
    fixes = [
        {"impact": c["deduction"], "pattern": c["name"], "action": c["fix"]}
        for c in checks if c["status"] == "fix" and c["fix"]
    ] + [
        {"impact": c["deduction"], "pattern": c["name"], "action": c["fix"]}
        for c in checks if c["status"] == "watch" and c["fix"]
    ]
    fixes.sort(key=lambda f: -f["impact"])
    payoff, payoff_t = detect_payoff(words, start, end)
    return {
        "clip": {"start": round(start, 2), "end": round(end, 2),
                 "duration_s": round(duration, 2), "title": title},
        "score": score,
        "grade": grade(score),
        "best_moment": {"quote": payoff, "at": round(payoff_t, 2),
                        "rel": round(max(0.0, payoff_t - start), 2)},
        "patterns": checks,
        "prioritized_fixes": fixes,
        "research": RESEARCH,
    }


def render_markdown(report: dict) -> str:
    clip = report["clip"]
    lines = [
        f"# Viral analysis — {clip['title'] or 'clip'}",
        "",
        f"**Score: {report['score']}/100 — {report['grade']}**  ",
        f"**Payoff:** « {report['best_moment']['quote'] or '—'} » "
        f"at +{report['best_moment']['rel']:.1f}s",
        "",
    ]
    if report["prioritized_fixes"]:
        lines += ["## Fix first (highest impact)", "",
                  "| Impact | Area | Action |", "|--------|------|--------|"]
        for f in report["prioritized_fixes"]:
            lines.append(f"| -{f['impact']} | {f['pattern']} | {f['action']} |")
        lines.append("")
    lines += [
        "## Pattern checks",
        "",
        "| Pattern | Status | Evidence | Fix |",
        "|---------|--------|----------|-----|",
    ]
    icon = {"pass": "✅", "watch": "👀", "fix": "🔧"}
    for c in report["patterns"]:
        lines.append(
            f"| {c['name']} | {icon[c['status']]} {c['status']} "
            f"| {c['evidence']} | {c['fix'] or '—'} |"
        )
    lines += ["", "## What 1M+ creator/streamer Shorts do", ""]
    for p in report["research"]:
        lines += [f"- **{p['pattern']}** — {p['what']} *({p['why']})* "
                  f"<{p['source']}>"]
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: dict, *, markdown: bool = False) -> Path:
    if markdown:
        path.write_text(render_markdown(report), encoding="utf-8")
    else:
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
