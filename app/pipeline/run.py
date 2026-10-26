"""Pipeline orchestrator: ingest → audio → transcript → highlights → clips."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from app import config
from app.ffmpeg_utils import duration_of
from app.pipeline import (
    audio_mix, captions, hooks, ingest, publish, render, teaser, tts, viral, watermark,
)
from app.pipeline.highlights import select_highlights
from app.pipeline.transcribe import save as save_transcript, transcribe

ProgressFn = Callable[[float, str], None]
LogFn = Callable[[str], None]


def _noop_progress(_: float, __: str) -> None:  # pragma: no cover
    pass


def _noop_log(_: str) -> None:  # pragma: no cover
    pass


def _fmt_bytes(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def run_pipeline(
    source: str,
    workdir: Path,
    *,
    kind: str = "upload",
    progress: ProgressFn = _noop_progress,
    log: LogFn = _noop_log,
    max_highlights: Optional[int] = None,
    provider: Optional[str] = None,
    music: Optional[str] = None,
    watermark_on: Optional[bool] = None,
    watermark_position: Optional[str] = None,
    watermark_file: Optional[str] = None,
    voice: Optional[str] = None,
    hook: Optional[bool] = None,
    hook_playbook: Optional[bool] = None,
    hook_platform: Optional[str] = None,
    hook_niche: str = "",
    hook_viewer: str = "",
    viral_analysis: Optional[bool] = None,
    publish_metadata: Optional[bool] = None,
    caption_mode: Optional[str] = None,
    subtitle: str = "",
    auto_music_index: int = 0,
) -> dict:
    """Run every stage for one input; returns the result payload for the job.

    `music` — ""/None (off), "auto"/"ai" (pick from config.MUSIC_DIR), or a
    path to the user's own audio file.
    `watermark_on` — None = config default, True/False = force on/off.
    `watermark_position` — top-left / top-right / bottom-left / bottom-right /
    bottom-center (validated; errors on unknown values).
    `watermark_file` — user-dragged PNG name/path (None = config default).
    `voice` — ""/None (off), a config.TTS_VOICES label ("aria", "guy", …),
    or a raw edge-tts voice name.
    `hook` — None = config default (HOOK_VERSIONS), True/False = force the
    flash-forward "hook version" of every clip on/off.
    `hook_playbook` — None = config default (HOOK_PLAYBOOK); True/False forces
    the scored hook-options playbook (+ retention beat sheet) per clip.
    `hook_platform` — "both" | "shorts" | "reels" (labels the playbook).
    `viral_analysis` — None = config default (VIRAL_ANALYSIS); True/False
    forces the 1M+ creator/streamer pattern checks per clip on/off.
    `publish_metadata` — None = config default (PUBLISH_METADATA); True/False
    forces the ready-to-post Instagram + YouTube metadata per clip on/off.
    `caption_mode` — None = config default (CAPTION_MODE): "animated" (the
    moving word-sync captions) or "static" (one stationary box for the whole
    clip).
    `subtitle` — text for static mode; empty = suggest one per clip from its
    analysis (highlight title, else detected payoff line). Ignored when
    captions are animated.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    count = max_highlights or config.MAX_HIGHLIGHTS

    # --- optional extras resolved once, before rendering --------------------
    # In auto mode each clip gets the next library track (round-robin);
    # an explicit file/"off" is resolved once for the whole job.
    auto_mode = (music or "").strip().lower() in {"auto", "ai"}
    music_file = audio_mix.resolve_music(music, auto_index=auto_music_index)
    # validate the position early (raises on typos) and resolve the file
    if watermark_on is not False:
        wm_pos = watermark.normalize_position(watermark_position)
        wm_file = (
            watermark.resolve_watermark_file(watermark_file)
            if watermark_file
            else watermark.resolve_watermark(watermark_on)
        )
    else:
        wm_pos, wm_file = None, None
    voice_on = bool(voice and str(voice).strip()
                    and str(voice).strip().lower() not in {"off", "none", "false"})
    hook_on = config.HOOK_VERSIONS if hook is None else bool(hook)
    playbook_on = config.HOOK_PLAYBOOK if hook_playbook is None else bool(hook_playbook)
    viral_on = config.VIRAL_ANALYSIS if viral_analysis is None else bool(viral_analysis)
    publish_on = (
        config.PUBLISH_METADATA if publish_metadata is None else bool(publish_metadata)
    )
    platform = (hook_platform or config.HOOK_PLATFORM).strip().lower()
    if platform not in {"both", "shorts", "reels"}:
        raise ValueError(f"unknown hook platform {platform!r} — use both/shorts/reels")
    mode = (caption_mode or config.CAPTION_MODE).strip().lower()
    if mode not in {"animated", "static"}:
        raise ValueError(f"unknown caption mode {mode!r} — use animated/static")
    static_text = str(subtitle or "").strip()

    # --- 1. source + audio ---------------------------------------------------
    progress(0.02, "Resolving source video")

    # URL downloads report live byte counts (yt-dlp hooks); local files skip
    # straight through. Video stream maps to 0.02–0.065, audio to 0.065–0.07
    # so the bar never jumps backwards when the second stream starts.
    dl_state = {"milestone": 0}

    def download_progress(kind: str, done: float, total: float) -> None:
        frac = min(done / total, 1.0) if total > 0 else 0.0
        shown = f"{_fmt_bytes(done)}" + (
            f" / {_fmt_bytes(total)} ({frac * 100:.0f}%)" if total > 0 else ""
        )
        stage = f"Downloading {kind} — {shown}"
        if kind == "audio":
            progress(0.065 + 0.005 * frac, stage)
            return
        progress(0.02 + 0.045 * frac, stage)
        mark = int(frac * 4)  # log at 25 / 50 / 75 %
        if total > 0 and 0 < mark < 4 and mark > dl_state["milestone"]:
            dl_state["milestone"] = mark
            log(f"Download {mark * 25}% — {shown}")

    video = ingest.stage_source(source, workdir, log, on_progress=download_progress)
    progress(0.08, "Probing video")
    meta = ingest.probe_video(video)
    duration = meta["duration"]
    log(
        f"Source: {duration:.1f}s, {meta['width']}x{meta['height']}"
        + (f" @ {meta['fps']:.2f}fps" if meta["fps"] else "")
    )

    wav = workdir / "audio.wav"
    ingest.extract_audio(video, wav, log)
    progress(0.14, "Transcribing speech (Whisper)")
    log(f"Whisper model: {config.WHISPER_MODEL}")

    def transcribe_progress(frac: float) -> None:
        progress(0.14 + 0.34 * frac, "Transcribing speech (Whisper)")

    transcript = transcribe(wav, duration_of(wav), on_progress=transcribe_progress)
    save_transcript(transcript, workdir / "transcript.json")
    log(f"Transcribed {len(transcript.words)} words ({transcript.language})")

    # --- whole-video excitement analysis (feeds hook teaser picking) --------
    # Loudness × broadband "cheer" ratio across the ENTIRE source, so the
    # hook can target where the crowd roars / energy peaks. Best-effort:
    # a failure downgrades hook picking to text-only, never kills the job.
    excitement: Optional[teaser.ExcitementCurve] = None
    if hook_on:
        try:
            excitement = teaser.analyze_audio(wav)
            if not excitement.empty:
                log(
                    f"Whole-video excitement: loudest moment at "
                    f"{excitement.peak_time:.1f}s (crowd/energy peak)"
                )
        except Exception as exc:  # noqa: BLE001
            log(f"Audio excitement analysis unavailable ({exc}) — hook uses text only")

    # --- 2. highlights -------------------------------------------------------
    progress(0.50, "Selecting highlights")
    selection = select_highlights(transcript, provider=provider, count=count, log=log)
    highlights = selection.highlights
    if not highlights:
        raise RuntimeError("no usable highlights could be selected for this video")
    (workdir / "highlights.json").write_text(
        json.dumps(
            {"provider": selection.provider, "highlights": [h.model_dump() for h in highlights]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for i, hl in enumerate(highlights, 1):
        log(f"Highlight {i}: {hl.start:.1f}s–{hl.end:.1f}s  « {hl.title} »")

    # --- 3. render clips with burned-in captions -----------------------------
    artifacts: list[dict] = []
    total = len(highlights)
    for i, hl in enumerate(highlights, 1):
        tag = f"clip_{i:02d}"
        progress(
            0.55 + 0.42 * (i - 1) / total,
            f"Rendering vertical clip {i}/{total}",
        )
        ass_path = workdir / f"{tag}.ass"
        # Static mode: resolve ONE text per clip (manual, or suggested from
        # the clip's analysis) and reuse it for the hook teaser cut too, so
        # the block reads the same across both versions.
        clip_subtitle = ""
        if mode == "static":
            clip_subtitle = static_text or captions.suggest_subtitle(
                transcript.words, hl.start, hl.end, title=hl.title,
            )
            captions.write_static_ass(ass_path, clip_subtitle, hl.duration)
            log(f"Static subtitle {tag}: « {clip_subtitle} »")
        else:
            captions.write_ass(ass_path, transcript.words, hl.start, hl.duration)
        out_path = workdir / f"{tag}.mp4"
        if auto_mode:
            # round-robin: clip i gets library track (auto_music_index + i - 1)
            clip_music = audio_mix.resolve_music(
                music, auto_index=auto_music_index + i - 1
            )
        else:
            clip_music = music_file
        # AI voice: read this clip's captions aloud (best-effort — a TTS
        # failure logs and renders without voice rather than killing the job).
        clip_voice = None
        if voice_on:
            text = tts.caption_text_for_window(transcript.words, hl.start, hl.end)
            try:
                clip_voice = tts.synthesize(
                    text, str(voice), workdir / f"{tag}_tts", log=log
                )
            except Exception as exc:  # noqa: BLE001
                log(f"AI voice for {tag} failed ({exc}) — rendering without it")
        render.render_clip(
            video, hl.start, hl.end, ass_path, out_path,
            watermark_path=wm_file, music_path=clip_music,
            voice_path=clip_voice, watermark_position=wm_pos,
        )
        size_mb = out_path.stat().st_size / 1e6
        extras = []
        if wm_file is not None:
            extras.append(f"watermark:{wm_pos}")
        if clip_music is not None:
            extras.append(f"music:{clip_music.name}")
        if clip_voice is not None:
            extras.append(f"voice:{voice}")
        log(f"Rendered {out_path.name} — {hl.duration:.1f}s, {size_mb:.1f} MB"
            + (" [" + ", ".join(extras) + "]" if extras else ""))
        artifacts.append(
            {
                "name": out_path.name,
                "kind": "video",
                "title": hl.title or f"Clip {i}",
                "start": hl.start,
                "end": hl.end,
            }
        )
        artifacts.append({"name": ass_path.name, "kind": "text", "title": f"Captions for clip {i}"})

        # --- hook version: cold-open with a flash-forward teaser ------------
        # Same highlight, but it starts with a few-second cut chosen by
        # analysing the whole video (crowd cheer / funniest / most intense
        # moment, extended a few seconds so the peak lands), then the full
        # clip continues. Same captions, watermark, music and voice treatment.
        if hook_on and hl.duration >= config.HOOK_MIN_CLIP_SECONDS:
            tw = teaser.pick_teaser(transcript, hl.start, hl.end, audio=excitement)
            t_tag = f"{tag}_teaser"
            t_ass = workdir / f"{t_tag}.ass"
            if mode == "static":
                captions.write_static_ass(t_ass, clip_subtitle, tw.duration)
            else:
                captions.write_ass(t_ass, transcript.words, tw.start, tw.duration)
            t_part = workdir / f"{t_tag}.mp4"
            render.render_clip(
                video, tw.start, tw.end, t_ass, t_part,
                watermark_path=wm_file, music_path=clip_music,
                voice_path=clip_voice, watermark_position=wm_pos,
            )
            hook_path = workdir / f"{tag}_hook.mp4"
            render.concat_clips([t_part, out_path], hook_path)
            t_part.unlink(missing_ok=True)
            t_ass.unlink(missing_ok=True)
            artifacts.append(
                {
                    "name": hook_path.name,
                    "kind": "video",
                    "title": f"{hl.title or f'Clip {i}'} 🪝",
                    "start": hl.start,
                    "end": hl.end,
                }
            )
            log(
                f"Hook version {hook_path.name} — {tw.duration:.1f}s flash-forward "
                f"from {tw.start:.1f}s, then the clip continues"
            )

        # --- hook playbook: 8 scored hook options + retention beat sheet ---
        # Research-grounded (1M+ view Shorts/Reels): open loop, bold claim,
        # relatable, result-first, pattern interrupt, shareable, mid-action,
        # transformation — scored on clarity/curiosity/relevance/muted/honesty.
        if playbook_on:
            report = hooks.build_report(
                transcript.words, hl.start, hl.end,
                title=hl.title, platform=platform,
                niche=hook_niche, viewer=hook_viewer,
            )
            hooks.write_report(workdir / f"{tag}_hooks.json", report)
            hooks.write_report(workdir / f"{tag}_hooks.md", report, markdown=True)
            artifacts.append(
                {"name": f"{tag}_hooks.json", "kind": "json",
                 "title": f"Hook playbook for clip {i}"}
            )
            artifacts.append(
                {"name": f"{tag}_hooks.md", "kind": "text",
                 "title": f"Hook playbook (readable) for clip {i}"}
            )
            w = report["winner"]
            log(
                f"Hook playbook {tag}: winner « {w['spoken_line']} » "
                f"({w['name']}, {w['total']:.1f}/5) — see {tag}_hooks.md"
            )

        # --- viral analysis: what makes 1M+ creator/streamer Shorts work -----
        # Same pattern checks every time (open on the moment, length band,
        # payoff placement, dead air, loop ending, muted captions, context,
        # honest ask) scored against THIS clip's words, with prioritized fixes.
        if viral_on:
            vreport = viral.build_report(
                transcript.words, hl.start, hl.end, title=hl.title,
            )
            viral.write_report(workdir / f"{tag}_viral.json", vreport)
            viral.write_report(workdir / f"{tag}_viral.md", vreport, markdown=True)
            artifacts.append(
                {"name": f"{tag}_viral.json", "kind": "json",
                 "title": f"Viral analysis for clip {i}"}
            )
            artifacts.append(
                {"name": f"{tag}_viral.md", "kind": "text",
                 "title": f"Viral analysis (readable) for clip {i}"}
            )
            log(
                f"Viral analysis {tag}: {vreport['score']}/100 "
                f"({vreport['grade']}) — {len(vreport['prioritized_fixes'])} fix(es) "
                f"in {tag}_viral.md"
            )

        # --- publish metadata: ready-to-post IG caption + YT title/tags/desc --
        # Deterministic from the clip's own words: keywords + payoff line.
        # The campaign audit (campaigns.py) reuses these as its baseline.
        if publish_on:
            preport = publish.build_report(
                transcript.words, hl.start, hl.end, title=hl.title, index=i,
            )
            publish.write_report(workdir / f"{tag}_publish.json", preport)
            publish.write_report(workdir / f"{tag}_publish.md", preport, markdown=True)
            artifacts.append(
                {"name": f"{tag}_publish.json", "kind": "json",
                 "title": f"Publish metadata for clip {i}"}
            )
            artifacts.append(
                {"name": f"{tag}_publish.md", "kind": "text",
                 "title": f"Publish metadata (readable) for clip {i}"}
            )
            log(
                f"Publish metadata {tag}: IG « {preport['instagram']['title'][:60]} » "
                f"/ YT « {preport['youtube']['title'][:60]} »"
            )

    artifacts.append({"name": "transcript.json", "kind": "json", "title": "Word-level transcript"})
    artifacts.append({"name": "highlights.json", "kind": "json", "title": "Selected highlights"})

    # settings.json — what this job rendered WITH. The campaign audit reads
    # it (from disk, so it works after restarts) to verify watermark / caption
    # rules without re-rendering anything.
    settings = {
        "music": music_file.name if music_file else None,
        "watermark": wm_file is not None,
        "watermark_position": wm_pos if wm_file is not None else None,
        "voice": str(voice) if voice_on else None,
        "caption_mode": mode,
        "subtitle": static_text if mode == "static" else "",
        "hook": hook_on,
        "hook_playbook": playbook_on,
        "viral_analysis": viral_on,
        "publish_metadata": publish_on,
    }
    (workdir / "settings.json").write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    artifacts.append({"name": "settings.json", "kind": "json", "title": "Job render settings"})
    progress(1.0, "Done")

    return {
        "duration": duration,
        "language": transcript.language,
        "provider": selection.provider,
        "highlights": [h.model_dump() for h in highlights],
        "artifacts": artifacts,
        "music": music_file.name if music_file else None,
        "hook": hook_on,
        "hook_playbook": playbook_on,
        "hook_platform": platform,
        "viral_analysis": viral_on,
        "publish_metadata": publish_on,
        "caption_mode": mode,
        "subtitle": static_text if mode == "static" else "",
        "watermark": wm_file is not None,
        "watermark_position": wm_pos if wm_file is not None else None,
        "voice": str(voice) if voice_on else None,
    }
