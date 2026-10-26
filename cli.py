#!/usr/bin/env python3
"""Shorts Studio CLI — run the full pipeline without the API server.

Examples:
    python cli.py talk.mp4
    python cli.py "https://www.youtube.com/watch?v=..." --max-highlights 4
    python cli.py talk.mp4 --provider heuristic --whisper-model small.en
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app import config
from app.pipeline.run import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Turn a long video into captioned vertical shorts.",
    )
    parser.add_argument("source", help="local video path (mp4/mov/mkv/...) or http(s) URL")
    parser.add_argument("--out", default=None, help="work/output directory (default: data/jobs/<ts>)")
    parser.add_argument("--max-highlights", type=int, default=config.MAX_HIGHLIGHTS)
    parser.add_argument(
        "--provider",
        default=None,
        choices=["auto", "openai", "anthropic", "heuristic"],
        help="highlight picker (default: env LLM_PROVIDER / auto)",
    )
    parser.add_argument("--whisper-model", default=None, help="e.g. tiny.en, base.en, small.en, large-v3")
    parser.add_argument(
        "--music",
        default=None,
        help='add background music: "auto" (pick from assets/music), "off", or a path to your own mp3/wav',
    )
    parser.add_argument("--music-volume", type=float, default=None, help="music level, e.g. 0.45")
    parser.add_argument("--original-volume", type=float, default=None, help="original audio level (0 = mute speech)")
    parser.add_argument("--music-offset", type=float, default=None, help="seconds into the music track (put the drop here)")
    wm = parser.add_mutually_exclusive_group()
    wm.add_argument("--watermark", dest="watermark", action="store_true", help="force the watermark on")
    wm.add_argument("--no-watermark", dest="watermark", action="store_false", help="render without the watermark")
    parser.set_defaults(watermark=None)
    parser.add_argument("--watermark-path", default=None, help="path to your own watermark PNG")
    parser.add_argument(
        "--watermark-pos",
        default=None,
        choices=["top-left", "top-right", "bottom-left", "bottom-right", "bottom-center"],
        help="watermark placement (default: config WATERMARK_POSITION)",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help='AI voice that reads the captions: "aria", "jenny", "guy", "andrew", "sonia", "ryan", or off',
    )
    hk = parser.add_mutually_exclusive_group()
    hk.add_argument(
        "--hook", dest="hook", action="store_true",
        help="also render a hook version of each clip (flash-forward cold open)",
    )
    hk.add_argument(
        "--no-hook", dest="hook", action="store_false",
        help="render normal clips only",
    )
    parser.set_defaults(hook=None)
    pb = parser.add_mutually_exclusive_group()
    pb.add_argument(
        "--hook-playbook", dest="hook_playbook", action="store_true",
        help="write 8 scored hook options + a retention beat sheet per clip",
    )
    pb.add_argument(
        "--no-hook-playbook", dest="hook_playbook", action="store_false",
        help="skip the hook playbook",
    )
    parser.set_defaults(hook_playbook=None)
    parser.add_argument(
        "--hook-platform", default=None, choices=["both", "shorts", "reels"],
        help="platform the hooks are tuned for (default: config HOOK_PLATFORM)",
    )
    parser.add_argument("--niche", default="", help="niche, e.g. 'fitness beginners' (sharpens hook relevance)")
    parser.add_argument("--viewer", default="", help="target viewer description")
    cm = parser.add_mutually_exclusive_group()
    cm.add_argument(
        "--static-subtitles", dest="caption_mode", action="store_const", const="static",
        help="stationary subtitle box for the whole clip (like big clip channels)",
    )
    cm.add_argument(
        "--animated-captions", dest="caption_mode", action="store_const", const="animated",
        help="moving word-by-word captions (default)",
    )
    parser.set_defaults(caption_mode=None)
    parser.add_argument(
        "--subtitle", default="",
        help="your own stationary subtitle text (with --static-subtitles; omit to have it suggested per clip)",
    )
    va = parser.add_mutually_exclusive_group()
    va.add_argument(
        "--viral", dest="viral", action="store_true",
        help="write a 0–100 viral analysis per clip (1M+ creator/streamer patterns + fixes)",
    )
    va.add_argument(
        "--no-viral", dest="viral", action="store_false",
        help="skip the viral analysis",
    )
    parser.set_defaults(viral=None)
    pub = parser.add_mutually_exclusive_group()
    pub.add_argument(
        "--publish", dest="publish", action="store_true",
        help="write ready-to-post Instagram caption + YouTube title/tags/description per clip",
    )
    pub.add_argument(
        "--no-publish", dest="publish", action="store_false",
        help="skip the publish metadata",
    )
    parser.set_defaults(publish=None)
    args = parser.parse_args(argv)

    if args.whisper_model:
        config.WHISPER_MODEL = args.whisper_model
    if args.provider:
        config.LLM_PROVIDER = args.provider
    if args.music_volume is not None:
        config.MUSIC_VOLUME = args.music_volume
    if args.original_volume is not None:
        config.ORIGINAL_VOLUME = args.original_volume
    if args.music_offset is not None:
        config.MUSIC_OFFSET = args.music_offset
    if args.watermark_path:
        config.WATERMARK_PATH = Path(args.watermark_path)
    config.MAX_HIGHLIGHTS = args.max_highlights

    if args.out:
        workdir = Path(args.out)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        workdir = config.JOBS_DIR / f"cli-{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)

    last: tuple[float, str] = (0.0, "starting")

    def progress(pct: float, stage: str) -> None:
        nonlocal last
        last = (pct, stage)
        bar = "█" * 30
        filled = int(pct * 30)
        print(f"\r[{bar[:filled]:<30}] {pct * 100:5.1f}%  {stage:<38}", end="", flush=True)

    def log(line: str) -> None:
        print(f"\r{'':78}\r", end="")
        print(f"  · {line}")
        progress(*last)  # redraw the bar after the log line

    try:
        result = run_pipeline(
            args.source, workdir,
            progress=progress, log=log,
            music=args.music, watermark_on=args.watermark,
            watermark_position=args.watermark_pos,
            voice=args.voice, hook=args.hook,
            hook_playbook=args.hook_playbook, hook_platform=args.hook_platform,
            hook_niche=args.niche, hook_viewer=args.viewer,
            viral_analysis=args.viral,
            publish_metadata=args.publish,
            caption_mode=args.caption_mode, subtitle=args.subtitle,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\r{'':70}")
    print(f"\n✔ Done ({result['provider']} highlights, language={result['language']})")
    print(f"  output dir: {workdir}\n")
    print(f"  {'CLIP':<10} {'WINDOW':<18} {'TITLE'}")
    print(f"  {'-'*10} {'-'*18} {'-'*44}")
    for i, hl in enumerate(result["highlights"], 1):
        window = f"{hl['start']:.1f}s – {hl['end']:.1f}s"
        print(f"  clip_{i:02d}.mp4  {window:<18} {hl['title']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
