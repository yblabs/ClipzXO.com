#!/usr/bin/env python3
"""Build a synthetic ~4 minute long-form test video (macOS only).

Uses the built-in `say` command to narrate a script that alternates bland
filler with clearly "hook-y" sentences, then muxes the speech onto an
ffmpeg test pattern so the whole pipeline can be exercised end to end:

    .venv/bin/python scripts/make_demo_video.py [output.mp4]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_utils import duration_of, ffmpeg, run  # noqa: E402

PARAGRAPHS = [
    "Welcome back to the channel everyone. Today we are going to talk about my morning "
    "routine and how I plan my whole week on a Sunday evening.",
    "I usually wake up around seven, make some coffee, and glance at the weather before "
    "I decide what to wear. Nothing dramatic happens in the first hour of my day.",
    "But here is the one secret that nobody tells you about productivity? Your calendar "
    "is lying to you, and the average knowledge worker checks email forty times a day.",
    "After that I commute into the office, sit down, answer a few messages, and skim "
    "through whatever landed in the shared channel overnight.",
    "Some days are simply busy, and there is nothing particularly special happening at "
    "all. We move meetings around and everyone pretends that is normal.",
    "Why do almost eighty percent of new year resolutions fail by February? The answer "
    "is simpler than you think, and it has nothing to do with willpower.",
    "Around noon I take a break, eat something light, and then I like to walk around "
    "the block with my dog before the afternoon session starts.",
    "Nothing exciting happens on that walk, to be honest with you. We just circle the "
    "park and come back, and I stretch a little in the backyard.",
    "Never start your day by opening your phone. That single mistake will cost you the "
    "two most productive hours you will ever have, and you will never get them back.",
    "In the evening I usually cook dinner, reply to the last few messages, and then read "
    "a book for thirty minutes before I start winding down.",
    "How do you actually save ten thousand dollars in a year without even noticing? I "
    "tried three simple tricks, and the last one completely changed everything for me.",
    "Then I watch some television for a while, brush my teeth, and go to bed at a "
    "reasonably early hour so the whole routine can start again tomorrow.",
    "Here is a crazy fact: you will blink ten times more while reading this sentence "
    "than while listening to it. Stop for a second and test it right now.",
    "Isn't that insane? You can count your blinks for the next thirty seconds and you "
    "will see the difference immediately.",
    "Anyway, that is all for today. Thanks for watching, subscribe if this was useful, "
    "and I will see you in the next one.",
]

SCRIPT = "\n".join(PARAGRAPHS)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/demo_input.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        speech = Path(tmp) / "speech.aiff"
        print(f"Narrating {len(SCRIPT.split())} words with `say` …")
        run(["say", "-o", str(speech), SCRIPT], timeout=600)
        speech_duration = duration_of(speech)
        print(f"Speech: {speech_duration:.1f}s")

        print("Muxing onto an ffmpeg test pattern …")
        run(
            [
                ffmpeg(), "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size=1280x720:rate=30:duration={speech_duration:.2f}",
                "-i", str(speech),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "160k",
                "-shortest", "-movflags", "+faststart",
                str(out),
            ],
            timeout=600,
        )

    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {duration_of(out):.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
