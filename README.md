# 🎬 Shorts Studio

End-to-end pipeline that turns a long video (MP4/MOV/MKV/… or a YouTube/URL link) into
**vertical 9:16 shorts** with AI-selected highlights and **animated, word-synced captions**
burned in.

```
video / URL ──► audio extract ──► faster-whisper (word timestamps)
                                        │
                                        ▼
                        LLM highlight picker (GPT-4o-mini / Claude,
                        or a built-in heuristic fallback)  ── 3–5 × 30–60 s windows
                                        │
                                        ▼
                        ffmpeg cover-crop to 1080×1920 + ASS captions burn-in
                                        │
                                        ▼
                        clip_01.mp4 … clip_N.mp4  +  .ass  +  transcript/highlights JSON
```

Three ways to use it:

| Interface | Entry point |
|---|---|
| **CLI** | `python cli.py myvideo.mp4` |
| **Web API** | FastAPI (`python -m app.main`) |
| **Web UI** | Streamlit (`streamlit run frontend/streamlit_app.py`) |

---

## Requirements

- **Python 3.11+** (developed on 3.12)
- **ffmpeg with libass** — a `PATH` ffmpeg is used if it has the `ass` filter
  (needed to burn captions); otherwise the bundled `static-ffmpeg` build is used
  automatically, with a warning. No manual install required.
- Whisper model weights download on first run (~74 MB for `base.en`).

## Install

```bash
python3 -m venv .venv          # or: uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt   # optional: pytest
cp .env.example .env           # optional: tune models/colours/limits
```

## Quick start (CLI)

```bash
# local file
.venv/bin/python cli.py data/demo_input.mp4

# YouTube / any http(s) URL (yt-dlp)
.venv/bin/python cli.py "https://www.youtube.com/watch?v=…"

# options
.venv/bin/python cli.py input.mp4 \
    --out out/dir \
    --max-highlights 4 \
    --provider auto \            # auto | openai | anthropic | heuristic
    --whisper-model small.en
```

Outputs land in `data/jobs/cli-<timestamp>/`:

```
input.mp4        # ingested source (or downloaded media)
audio.wav        # extracted mono 16 kHz audio
transcript.json  # word-level transcript (start/end per word)
highlights.json  # selected segments + titles + provider used
clip_01.mp4      # 1080×1920 h264/aac, captions burned in
clip_01.ass      # the caption script for that clip
…
```

## Web API

```bash
.venv/bin/python -m app.main            # http://127.0.0.1:8000
```

| Method | Route | Body |
|---|---|---|
| POST | `/api/jobs/upload` | multipart `file` (mp4/mov/mkv/webm/m4v), optional form fields `music`, `watermark`, `voice`, `hook`, `hook_playbook`, `hook_platform`, `hook_niche`, `hook_viewer` |
| POST | `/api/jobs/url` | `{"url": "https://…", "music": "auto", "watermark": true, "hook_playbook": true}` |
| GET | `/api/jobs` | — |
| GET | `/api/jobs/{id}` | job status, stage, log, highlights, artifacts |
| GET | `/api/jobs/{id}/files/{name}` | artifact download (`video/mp4`, `text/plain`, …) |
| GET | `/api/music` | local music library + trending-sound suggestions |
| POST | `/api/music/upload` | multipart `file` (mp3/wav/m4a/aac/flac/ogg) → adds to the library |
| GET | `/api/voices` | AI voice options + watermark positions |
| POST | `/api/watermark/upload` | multipart `file` (png/jpg/webp) → stores the dragged-in watermark |

```bash
curl -F "file=@video.mp4" -F "music=auto" -F "watermark=on" \
     http://127.0.0.1:8000/api/jobs/upload
curl http://127.0.0.1:8000/api/jobs/<id>          # poll until status == "done"
curl -OJ http://127.0.0.1:8000/api/jobs/<id>/files/clip_01.mp4
```

Jobs run in background threads; progress is reported through `stage` + `progress`
and a timestamped `log`.

## Web UI

```bash
.venv/bin/python -m app.main                 # terminal 1: API
.venv/bin/python -m streamlit run frontend/streamlit_app.py   # terminal 2: UI
```

Open http://127.0.0.1:8501 — upload or paste a URL, watch live progress, then preview
and download each vertical clip.

## Voice, music & watermark (optional)

All three are independent, opt-in extras layered onto the render — pick any
combination in the UI's 🎛️ panel, via API form fields, or CLI flags.

**🗣️ AI voice (subtitle audio the AI says)** — the AI reads the clip's caption
text aloud with one of several selectable voices (`aria`, `jenny`, `guy`,
`andrew`, `sonia`, `ryan` — edit `TTS_VOICES` to add more). The voice track is
mixed on top; the original speaker is ducked to `TTS_ORIGINAL_VOLUME` (0.15 by
default) so you keep arena/crowd sound without double speech. Uses edge-tts
(network) and falls back to the local macOS `say` voice; a TTS failure logs and
renders without voice rather than failing the job.

```bash
.venv/bin/python cli.py input.mp4 --voice aria      # or jenny / guy / andrew / sonia / ryan
```

**🎵 Popular music** — three modes:

- **Off** (default) — original audio only.
- **AI picks one** — chooses a track from your local library (`assets/music/`,
  round-robin across clips). Drop your own mp3/wav/m4a files there.
- **Upload my own track** — pick a file in the UI (or `--music path/to/song.mp3`
  in the CLI); it is trimmed to the clip, volume-managed, and mixed **under** the
  original audio (ducked, not removed — set `ORIGINAL_VOLUME=0` to mute speech).

  ⚠️ The app never downloads or bundles copyrighted songs — you supply the audio.
  The UI shows trending-sound *suggestions* (`TRENDING_SOUNDS` in config) but you
  must provide the file yourself and make sure you're allowed to use it.

```bash
.venv/bin/python cli.py input.mp4 --music auto                # pick from assets/music/
.venv/bin/python cli.py input.mp4 --music path/to/song.mp3 \
    --music-volume 0.6 --original-volume 0.3 --music-offset 32   # start at the drop
```

**💧 Watermark** — optional overlay. Drag your own image in the UI (PNG/JPG/WEBP
→ stored in `assets/watermarks/`) or use the default `assets/watermark.png`
(`WATERMARK_PATH` / `--watermark-path`). Pick **one of four corners** —
top-left, top-right, bottom-left, bottom-right (bottom-center kept as the
config default). Every placement stays visible for the full clip, is ~35% of
the video width, and clear of the platform-UI zones (top 10% / bottom 15%).

```bash
.venv/bin/python cli.py input.mp4 --no-watermark                 # render without it
.venv/bin/python cli.py input.mp4 --watermark --watermark-pos top-right
.venv/bin/python cli.py input.mp4 --watermark-path my_logo.png --watermark-pos bottom-left
```

## 🪝 Hooks (engagement options)

Two independent, opt-in extras for making clips swipe-stopping:

**Flash-forward hook version** — every clip also gets a `clip_XX_hook.mp4` that
cold-opens with a short teaser cut from later in the same clip (crowd cheer /
funniest / most intense moment, found by analysing the whole video's audio
excitement + transcript), then plays normally. Toggle it in the UI, or
`--hook` / `--no-hook` in the CLI.

**Hook playbook** — a research-grounded report per clip (`clip_XX_hooks.md`
+ `.json`) based on what 1M+ view Shorts/Reels do in the first 1–3 seconds:

- finds the clip's **payoff moment** in the transcript (intensifiers,
  negations, numbers — weighted to the back of the clip),
- writes **8 hook options**, one per winning archetype: open loop, bold /
  contrarian claim, hyper-specific relatable, result-first, pattern interrupt,
  sharable ("send this to…"), mid-action cold start, transformation tease —
  each with a ~12-word spoken line, muted-safe on-screen text, first-frame
  visual and the payoff it promises,
- **scores every hook 1–5** on clarity-in-1-second, curiosity gap, relevance
  to your niche/viewer, works-muted and honesty (low honesty → ⚠️ clickbait
  risk), ranks them and names the top 3,
- the winner gets a **retention beat sheet** (a new visual beat every ~4–8s,
  cut points at real silences, word-by-word captions, loop-friendly ending)
  and an **A/B test plan** where only the hook changes between versions.

```bash
.venv/bin/python cli.py input.mp4 --hook-playbook --hook-platform shorts \
    --niche "fitness beginners" --viewer "new gym-goers"
```

In the UI it's the **🎯 Hook playbook** toggle in the 🎛️ panel (platform,
niche and target viewer sharpen the scores). Config: `HOOK_PLAYBOOK` (default
on), `HOOK_PLATFORM` (`both` / `shorts` / `reels`).

## 🚀 Viral analysis (1M+ creator/streamer patterns)

A second research pass, running beside the hook playbook: every clip gets a
`clip_XX_viral.md` + `.json` report that scores it **0–100** against the
cutting patterns of 1M+ view creator/streamer Shorts (MrBeast-style openings,
xQc / Kai Cenat / iShowSpeed clip channels, professional clip editors and
published retention analyses). Eight checks run against the clip's own words
and timing:

| Check | What it verifies (with a timestamped fix when it fails) |
|-------|----------------------------------------------------------|
| Open on the moment | no greeting / filler / mid-thought start — the swipe decision lands in the first ~1–3s |
| Length for the arc | single-beat moments hold ~18–35s; past 45s is a retention cliff |
| Payoff lands & loops | a real peak exists, sits late enough to build, and has ~2s of room to land |
| Cut dead air | pauses ≥0.8s listed with timestamps as jump-cut candidates |
| Ending rewards rewatch | no post-payoff chatter trailing past the landing |
| Captions readable muted | words-per-second stays inside a readable-on-silent band |
| Cold-viewer context | a cold viewer gets who/what from the opening or an on-screen line |
| Honest discussion ask | a genuine question to pin — never a manufactured cut-off |

Each check reports **evidence + a concrete fix**, fixes are prioritised by
impact, and every pattern carries its source URL (in the `.md` report under
"What 1M+ creator/streamer Shorts do"). It reuses the hook module's payoff
detection, so the playbook and the viral analysis always agree on where the
payoff sits. Deterministic — no LLM key needed.

```bash
.venv/bin/python cli.py input.mp4 --viral          # force on
.venv/bin/python cli.py input.mp4 --no-viral       # skip
```

In the UI it's the **🚀 Viral analysis** toggle in the 🎛️ panel, beside the
hook toggles. Config: `VIRAL_ANALYSIS` (default on).

## 📱 Publish metadata (Instagram + YouTube) + 🗂️ Campaign rules (Whop)

Every clip also gets **ready-to-post metadata** (`clip_XX_publish.md` +
`.json`), generated deterministically from the clip's own transcript —
keywords ranked by frequency with proper nouns promoted, and the same payoff
detection the hook playbook / viral analysis use, so all reports agree on the
moment that sells the clip:

- **Instagram** — a copy-paste caption (title line + payoff hook + cold-viewer
  context + CTA) and a hashtag set sized inside platform limits;
- **YouTube** — a ≤100-char title, a tag list (keywords + niche + format tags)
  and a description with the hook, context, CTA and `#shorts`.

The caption/title/tag choices are then **audited against campaign rules**.
Whop (and every clip-payment platform) runs campaigns with per-offer
submission rules — a brand word that must appear in the title, required and
banned hashtags, a watermark in a certain corner, duration caps, words that
may never be spoken. The **🗂️ Campaigns page** (a separate Streamlit page)
turns those rules into a structured campaign, then audits **every clip of any
finished job** — from its files on disk, so it works after a restart:

| Rule | What the audit does when it fails |
|------|-----------------------------------|
| Title must contain / not contain | flags the title and prints a corrected version with the brand word injected |
| Max title chars | trims the suggestion to the cap (YouTube's 100 always applies) |
| Required / banned hashtags | lists the exact tags to add, and the ones to delete, in IG caption + YT description |
| Description must contain | tells you the exact line to append (e.g. the campaign URL) |
| Min / max seconds | computes exactly how much to trim or extend the window |
| Never-say words | scans the transcript and lists each hit with a `+timestamp` |
| Watermark on / position | verifies the job's render settings; tells you to re-render at the right corner when wrong |
| Captions required | verifies captions were burned in (animated or stationary) |
| CTA required | appends a suggested question/CTA line when missing |
| Language | catches clips that don't match the campaign's language |

Each check returns **pass 👀/❌ with evidence and a concrete fix**, every clip
gets a **0–100 compliance score**, failing rules are ranked across the job,
and the report ships **ready-to-post corrections**: the corrected Instagram
caption + hashtag line, YouTube title and description to paste straight into
the platform. Rules can be typed into a form, **extracted from pasted
campaign text**, or **uploaded as the rules file itself — PDF, TXT, MD, DOCX
or JSON** (PDF text via `pypdf`, DOCX from its XML, a `.json` with a rules
object imports directly); the raw text is kept in the campaign's notes.

```bash
.venv/bin/python cli.py input.mp4 --no-publish             # skip the metadata
.venv/bin/python -m app.pipeline.campaigns <job-id> --campaign <campaign-id-or-file.json>
```

In the UI: the **📱 Publish metadata** toggle on the home page, and the
**🗂️ Campaigns** page in the sidebar for the audit. Campaigns are stored in
`data/campaigns/`; audits write `campaign_<id>_report.md`/`.json` into the
job's folder. Config: `PUBLISH_METADATA` (default on).

## 🔌 Connected accounts — post clips directly (YouTube + Instagram + TikTok)

Link your accounts once on the **🔌 Accounts** page, and every clip on the
home page gets a **📣 Post to** panel below the video:
pick the account, the title/description/caption/tags are pre-filled from the
clip's own publish metadata (editable), and **🚀 Post now** uploads the clip
straight to that account.

- **YouTube (multiple channels)** — click *Connect a YouTube account*, pick
  the Google account on Google's consent screen, done; repeat for every
  channel. One-time setup in Google Cloud (free, ~5 min): enable **YouTube
  Data API v3**, create an **OAuth client ID of type Desktop app**, put
  `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env` (or save the
  downloaded JSON as `client_secret.json`). No app review is needed to post
  to your own channels; quota allows ~100 uploads/day by default. Uploads use
  the official resumable protocol; vertical clips show up as Shorts.
- **Instagram (multiple accounts)** — paste an access token + IG user id of
  a **professional (Business/Creator) account** (the Accounts page has the
  step-by-step Meta flow: create a free Business app → add the Instagram
  Graph API → generate a token with `instagram_basic`,
  `instagram_content_publish`, `pages_read_engagement`); repeat per account.
  Reels are uploaded with Meta's resumable byte upload (no public URL
  needed), processed, then published with the generated caption + hashtags.
  Meta's limit: ~50 API posts per account per 24 h; tokens last ~60 days.
- **TikTok (multiple accounts)** — click *Connect a TikTok account*, approve
  on TikTok's consent screen, done; repeat per profile. One-time setup on
  [developers.tiktok.com](https://developers.tiktok.com/apps/) (free):
  create an app, add the **Content Posting API** with **Direct post** and the
  **Login Kit**, register the redirect URI
  `http://127.0.0.1:8000/api/social/tiktok/callback`, put
  `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` in `.env`. Videos go up with
  the official **Content Posting API** (chunked `FILE_UPLOAD` + status poll),
  one caption field filled from the clip's publish metadata. **Note:** TikTok
  only allows **private** posts from an unaudited app — set
  `TIKTOK_PRIVACY=SELF_ONLY` until your app passes TikTok's review, which is
  what unlocks `PUBLIC_TO_EVERYONE`.

Credentials are stored only in `data/social/accounts.json` (file mode 600)
and are never returned by the API. Posting falls back to each clip's
`clip_XX_publish.json` when fields are left empty.

```bash
curl -s localhost:8000/api/social/accounts                      # linked accounts
curl -s localhost:8000/api/social/youtube/auth_url              # OAuth link to open
curl -s localhost:8000/api/social/tiktok/auth_url               # TikTok OAuth link
curl -s -X POST localhost:8000/api/social/post -H 'content-type: application/json' \
  -d '{"job_id":"…","file":"clip_01.mp4","account_id":"…"}'
```

Config: `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (or
`GOOGLE_CLIENT_SECRET_FILE`), `SOCIAL_REDIRECT_URI` (default
`http://127.0.0.1:8000/api/social/youtube/callback` — must match the OAuth
client), `YT_PRIVACY_STATUS` (`public`), `IG_GRAPH_VERSION` (`v21.0`),
`IG_POLL_SECONDS`/`IG_POLL_TIMEOUT`, `TIKTOK_CLIENT_KEY`/
`TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI` (default
`http://127.0.0.1:8000/api/social/tiktok/callback`), `TIKTOK_PRIVACY`
(`PUBLIC_TO_EVERYONE`), `TIKTOK_POLL_SECONDS`/`TIKTOK_POLL_TIMEOUT`.

## ✍️ Stationary subtitles (optional)

Besides the default **animated word-by-word captions**, every job can opt into
a **stationary subtitle**: one text block in an opaque box, fixed in the same
spot for the whole clip — the style the big streamer clip channels use (e.g.
the n3on-style context card). Two ways to fill it:

- **Stationary — my text**: you write the line; it's burned onto every clip
  of that job unchanged (`--static-subtitles --subtitle "…"`),
- **Stationary — suggest**: the app analyses each clip and proposes one —
  the curated highlight title when there is one (real summaries come from an
  LLM provider), otherwise the detected payoff line, trimmed to fit the box.

The hook's flash-forward cut gets the same block, so both versions of a clip
read consistently. Wrap, font size and colours are config (`STATIC_FONT_SIZE`,
`STATIC_TEXT_COLOUR`, `STATIC_BOX_COLOUR`); the default remains animated
(`CAPTION_MODE=animated`) so nothing changes unless you pick it.

```bash
.venv/bin/python cli.py input.mp4 --static-subtitles                     # suggested per clip
.venv/bin/python cli.py input.mp4 --static-subtitles --subtitle "The chat lost it 😭"
```

In the UI it's the **✍️ Subtitles** radio in the 🎛️ panel: *Animated
(moving)* / *Stationary — suggest* / *Stationary — my text*.

## Configuration

Everything is environment-driven (see [`.env.example`](.env.example) for the full list):

- **Transcription** — `WHISPER_MODEL` (`base.en` default), `WHISPER_DEVICE`, `WHISPER_LANGUAGE`
- **Highlights** — `LLM_PROVIDER`, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`,
  `MIN/MAX_HIGHLIGHT_SECONDS` (30/60), `MIN/MAX_HIGHLIGHTS` (3/10).
  With no API key the built-in **heuristic** picks engaging windows from the
  transcript (hook density, question/exclamation cues, self-contained boundaries),
  snapped to word boundaries.
- **Source download** — `DOWNLOAD_MAX_HEIGHT` (1080): caps yt-dlp so URL jobs don't
  pull multi-GB 4K video (`0` = uncapped). Downloads report live progress
  (bytes / % per stream) in the job stage and log.
- **Rendering** — `TARGET_WIDTH/HEIGHT` (1080×1920), `VIDEO_CRF`, `VIDEO_PRESET`
- **Music** — `SHORTS_MUSIC_DIR` (library folder, default `assets/music`),
  `MUSIC_VOLUME` (0.45), `MUSIC_OFFSET` (0.0 — put the drop here),
  `ORIGINAL_VOLUME` (1.0; 0 mutes speech), `TRENDING_SOUNDS` (UI suggestions list)
- **Watermark** — `WATERMARK_PATH` (default `assets/watermark.png`),
  `WATERMARK_DIR` (drag-in store), `WATERMARK_DEFAULT_ON` (1),
  `WATERMARK_WIDTH_FRAC` (0.35), `WATERMARK_POSITION` (bottom-center; the UI
  offers top-left/top-right/bottom-left/bottom-right),
  `WATERMARK_TOP_MARGIN` / `WATERMARK_BOTTOM_MARGIN` (0.10 / 0.15),
  `WATERMARK_H_MARGIN` (0.04)
- **AI voice** — `TTS_VOICES` (label → edge-tts name map), `TTS_RATE` (+8%),
  `TTS_VOLUME` (1.0), `TTS_ORIGINAL_VOLUME` (0.15 — original speaker ducked)
- **Hooks & viral** — `HOOK_VERSIONS` (1), `HOOK_PLAYBOOK` (1), `HOOK_PLATFORM`
  (both), `VIRAL_ANALYSIS` (1 — per-clip 0–100 score vs 1M+ Shorts patterns)
- **Subtitles** — `CAPTION_MODE` (`animated` default; `static` = one
  stationary box per clip, text manual or suggested), `STATIC_FONT_SIZE` (64),
  `STATIC_TEXT_COLOUR` / `STATIC_BOX_COLOUR` (black on white)
- **Captions** — `CAPTION_FONT_SIZE`, `CAPTION_MAX_WORDS`/`MAX_CHARS` (line chunking),
  `CAPTION_UPPERCASE`, `CAPTION_POS_Y`, `CAPTION_PRIMARY` / `CAPTION_HIGHLIGHT` /
  `CAPTION_OUTLINE` (ASS `&HAABBGGRR&` colours), pop-in/fade timings
- **Social publishing** — `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (YouTube
  OAuth client), `SOCIAL_REDIRECT_URI`, `YT_PRIVACY_STATUS` (`public`),
  `IG_GRAPH_VERSION` (`v21.0`), `IG_POLL_SECONDS`/`IG_POLL_TIMEOUT` (reel
  processing poll); accounts live in `data/social/accounts.json`
- **Server** — `API_HOST`, `API_PORT`, `SHORTS_DATA_DIR`, `MAX_UPLOAD_BYTES`

## How the captions work

Each caption line is rendered as **one ASS dialogue event per active word**, all with
identical layout, so the only thing that changes between events is which word carries
the accent colour — zero layout jitter while the highlight marches through the line.
Events get a `\fad` in/out and the first word gets a small `\t` scale pop-in.

Design notes (empirically verified against the bundled libass):

- Karaoke `\k` is a no-op in some libass builds, so per-word `{\1c&H…&}` override
  tags are used instead of karaoke wipes.
- Colour overrides **must** end with `&` (`{\1c&H00D4FF&}`); without it libass may
  silently drop the colour change. `captions._ass_colour()` canonicalises every
  colour for you.
- The ffmpeg resolver refuses a `PATH` ffmpeg built without the `ass` filter and
  falls back to `static-ffmpeg` instead of failing mid-render.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q     # 65 tests: highlights, captions, chunking, providers,
                                        # music mixing, watermark positions, AI voice, render commands
.venv/bin/python scripts/make_demo_video.py   # regenerate the synthetic narrated demo video
```

## Project layout

```
app/
  config.py            # env-driven settings
  ffmpeg_utils.py      # ffmpeg/ffprobe resolver (libass check + static fallback)
  jobs.py              # in-memory job manager (background threads, log, progress)
  main.py              # FastAPI app
  models.py            # pydantic job/highlight/artifact schemas
  pipeline/
    ingest.py          # file or yt-dlp URL → input media + audio.wav
    transcribe.py      # faster-whisper word-level transcript
    highlights.py      # LLM JSON selection + heuristic fallback, word-boundary snapping
    captions.py        # word-grouping + ASS script builder
    render.py          # 9:16 cover-crop + caption burn-in (single ffmpeg pass)
    audio_mix.py       # optional music + AI voice: library + ffmpeg audio graph
    tts.py             # AI voice: caption read-aloud (edge-tts, `say` fallback)
    watermark.py       # optional watermark: 4-corner placement + uploaded files
    run.py             # stage orchestration for one job
assets/
  watermark.png       # default watermark overlay (replace with your own)
  music/              # your music library for "AI picks one"
frontend/streamlit_app.py
cli.py
scripts/make_demo_video.py
tests/
```
