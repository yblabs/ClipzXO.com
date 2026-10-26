"""Streamlit frontend for the Shorts Studio API.

Run the API first (`python -m app.main`), then:
    streamlit run frontend/streamlit_app.py
"""
from __future__ import annotations

import os
import time

import httpx
import streamlit as st

DEFAULT_API = os.environ.get("SHORTS_API_URL", "http://127.0.0.1:8000")
VIDEO_EXTS = ("mp4", "mov", "mkv", "webm", "m4v")

st.set_page_config(page_title="Shorts Studio", page_icon="🎬", layout="wide")


def api_base() -> str:
    return st.session_state.get("api_base", DEFAULT_API).rstrip("/")


def call(method: str, path: str, *, timeout: float = 120.0, **kwargs):
    try:
        resp = httpx.request(method, f"{api_base()}{path}", timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        # surface the API's own error message (e.g. what YouTube/Instagram said)
        detail = ""
        try:
            detail = str(exc.response.json().get("detail", ""))
        except (ValueError, AttributeError):
            pass
        st.error(detail or f"API request failed: {exc}")
        return None
    except httpx.HTTPError as exc:
        st.error(f"API request failed: {exc}")
        return None


# ---------------------------------------------------------------- sidebar ----
with st.sidebar:
    st.title("🎬 Shorts Studio")
    st.caption("Long video → AI-selected highlights → vertical clips with animated captions.")
    st.session_state["api_base"] = st.text_input("API base URL", value=DEFAULT_API)
    health = call("GET", "/healthz")
    if health:
        st.success("API connected")
    else:
        st.warning("API not reachable — start it with `python -m app.main`")

    jobs = call("GET", "/api/jobs") or []
    if jobs:
        st.divider()
        st.subheader("Recent jobs")
        for job in jobs[:8]:
            icon = {"done": "✅", "error": "❌"}.get(job["status"], "⏳")
            label = f"{icon} {job['id']} · {(job['source'] or '')[:34]}"
            if st.button(label, key=f"job-{job['id']}", use_container_width=True):
                st.session_state["job_id"] = job["id"]
                st.rerun()

# ------------------------------------------------------------------ input -----
st.header("Turn long videos into viral shorts")

mode = st.radio("Input", ["Upload a file", "YouTube / URL"], horizontal=True)

# --------------------------------- AI voice + music + watermark options ------
with st.expander("🎛️ Voice, music & watermark (optional)", expanded=True):
    options_info = call("GET", "/api/voices") or {}
    voices: dict = options_info.get("voices", {}) or {}
    positions: list = options_info.get("positions", []) or []

    st.markdown("**🪝 Engaging hook versions**")
    hook_on = st.toggle(
        "Add a hook version to every clip",
        value=True,
        help=(
            "Every clip also gets a 🪝 version that cold-opens with a short "
            "flash-forward from later in the same clip — picked by analysing "
            "the whole video for where the crowd cheers, the funniest line or "
            "the most intense moment, extended a few seconds so the peak lands "
            "— then plays normally. Viewers stay for what you teased."
        ),
        key="hook_toggle",
    )

    st.markdown("**🎯 Hook playbook (scored hook options)**")
    playbook_on = st.toggle(
        "Write a hook playbook for every clip",
        value=True,
        help=(
            "Each clip gets `clip_XX_hooks.md`/`.json`: the 8 hook archetypes "
            "that win on Shorts/Reels (open loop, bold claim, relatable, "
            "result-first, pattern interrupt, shareable, mid-action, "
            "transformation), scored 1–5 on clarity, curiosity, relevance, "
            "works-muted and honesty — plus the winning hook script and a "
            "retention beat sheet (a new visual beat every ~4–8s)."
        ),
        key="playbook_toggle",
    )
    hook_platform = "both"
    hook_niche = ""
    hook_viewer = ""
    if playbook_on:
        hook_platform = st.radio(
            "Platform",
            ["both", "shorts", "reels"],
            horizontal=True,
            key="hook_platform",
            help="Tunes the playbook framing (YouTube Shorts vs Instagram Reels).",
        )
        hcols = st.columns(2)
        hook_niche = hcols[0].text_input(
            "Niche", placeholder="fitness beginners", key="hook_niche",
            help="Sharpens how relevant each hook scores for your audience.",
        )
        hook_viewer = hcols[1].text_input(
            "Target viewer", placeholder="new gym-goers", key="hook_viewer",
        )

    st.markdown("**🚀 Viral analysis (1M+ creator/streamer patterns)**")
    viral_on = st.toggle(
        "Score every clip like a 1M+ Shorts channel",
        value=True,
        help=(
            "Each clip gets `clip_XX_viral.md`/`.json`: pattern checks distilled "
            "from 1M+ view creator/streamer Shorts (MrBeast-style openings, "
            "xQc/Kai Cenat/Speed clip channels, published retention data) — "
            "open on the moment, ~18–35s length band, payoff placement, dead "
            "air, rewatch-friendly ending, muted captions, cold-viewer context, "
            "honest discussion ask — scored 0–100 with prioritized, "
            "timestamped fixes."
        ),
        key="viral_toggle",
    )

    st.markdown("**📱 Publish metadata (Instagram + YouTube)**")
    publish_on = st.toggle(
        "Write ready-to-post titles, tags & descriptions for every clip",
        value=True,
        help=(
            "Each clip gets `clip_XX_publish.md`/`.json`: a copy-paste "
            "Instagram caption (title line + payoff hook + CTA) with a hashtag "
            "set, plus a ≤100-char YouTube title, tag list and description with "
            "#shorts — all generated from the clip's own transcript. The "
            "🗂️ Campaigns page audits these against campaign rules."
        ),
        key="publish_toggle",
    )

    st.markdown("**✍️ Subtitles**")
    caption_choice = st.radio(
        "Caption style",
        ["Animated (moving)", "Stationary — suggest", "Stationary — my text"],
        horizontal=True,
        key="caption_style",
        help=(
            "Animated = the default word-by-word moving captions. Stationary = "
            "ONE text block in a white box, fixed for the whole clip (like the "
            "big streamer clip channels) — either suggested from the clip's "
            "own analysis (highlight title / payoff line) or written by you."
        ),
    )
    if caption_choice == "Animated (moving)":
        caption_mode, subtitle_value = "animated", ""
    elif caption_choice == "Stationary — suggest":
        caption_mode, subtitle_value = "static", ""
    else:
        caption_mode = "static"
        subtitle_value = st.text_input(
            "Stationary subtitle",
            placeholder="e.g. N3on is taking a break from streaming for his mental health…",
            key="subtitle_text",
            help="Shown in the box on every clip of this job, unchanged.",
        )

    st.markdown("**🗣️ AI voice (reads the captions aloud)**")
    voice_choice = st.radio(
        "Subtitle audio",
        ["Off", *list(voices.keys())],
        horizontal=True,
        key="voice_radio",
        help="The AI speaks this clip's caption text on top of the video; the original speaker is ducked, arena sound kept.",
    )
    voice_value = "" if voice_choice == "Off" else voice_choice

    st.markdown("**🎵 Background audio**")
    music_info = call("GET", "/api/music") or {}
    music_choice = st.radio(
        "Popular music",
        ["Off", "AI picks one", "Upload my own track"],
        horizontal=True,
        help="⚠️ Supply your own audio file — never use copyrighted songs you don't have rights to.",
    )
    music_file = None
    if music_choice == "Upload my own track":
        music_file = st.file_uploader(
            "Audio file", type=["mp3", "wav", "m4a", "aac", "flac", "ogg"]
        )
    elif music_choice == "AI picks one":
        tracks = music_info.get("tracks", [])
        if tracks:
            st.caption(f"Library ({music_info.get('dir', '')}): {', '.join(tracks)}")
        else:
            st.warning("Your music library is empty — drop mp3/wav files into assets/music, or upload a track.")
    trending = music_info.get("trending", [])
    if trending:
        st.caption("Trending-sound ideas (supply the audio yourself): " + " · ".join(trending))

    st.markdown("**💧 Watermark**")
    add_watermark = st.checkbox(
        "Add watermark to clips",
        value=True,
        help="Optional. Overlay stays out of platform-UI zones (top 10% / bottom 15%), ~35%% of video width.",
    )
    wm_position = ""
    wm_uploaded_name = ""
    if add_watermark:
        default_pos = "bottom-right" if "bottom-right" in positions else (positions[0] if positions else "bottom-right")
        pos_index = positions.index(default_pos) if default_pos in positions else 0
        wm_position = st.radio(
            "Position",
            positions or ["bottom-right"],
            index=pos_index,
            horizontal=True,
            key="wm_pos",
            help="Pick the corner — all four corners are kept clear of platform UI.",
        )
        wm_file = st.file_uploader(
            "Your watermark image (drag & drop — optional, uses the default if empty)",
            type=["png", "jpg", "jpeg", "webp"],
            key="wm_upload",
        )
        if wm_file is not None:
            uploaded_wm = call(
                "POST", "/api/watermark/upload",
                files={"file": (wm_file.name, wm_file.getvalue(), wm_file.type or "image/png")},
            )
            if uploaded_wm:
                wm_uploaded_name = uploaded_wm["name"]
                st.caption(f"Using your watermark: {wm_uploaded_name}")
            else:
                st.stop()  # upload failed — error already shown by call()

    st.markdown("**🎞️ Number of clips**")
    num_clips = st.slider(
        "How many clips to create",
        min_value=1, max_value=10, value=10,
        key="num_clips",
        help=(
            "How many shorts to cut from this video (max 10). On short sources "
            "the clips automatically shrink (down to ~8s) so you still get the "
            "number you asked for; only a very short video yields fewer."
        ),
    )

with st.form("job-form", clear_on_submit=False):
    uploaded = None
    if mode == "Upload a file":
        uploaded = st.file_uploader("Video file", type=list(VIDEO_EXTS))
    else:
        url = st.text_input("Video URL", placeholder="https://www.youtube.com/watch?v=…")
    cols = st.columns([1, 3])
    submitted = cols[0].form_submit_button("✨ Generate shorts", type="primary", use_container_width=True)

if submitted:
    # resolve the music option into an API-friendly value
    music_value = ""
    if music_choice == "AI picks one":
        music_value = "auto"
    elif music_choice == "Upload my own track":
        if music_file is None:
            st.warning("Choose an audio file first (or switch music to Off).")
            st.stop()
        uploaded_track = call(
            "POST", "/api/music/upload",
            files={"file": (music_file.name, music_file.getvalue(), music_file.type or "audio/mpeg")},
        )
        if not uploaded_track:
            st.stop()
        music_value = uploaded_track["name"]
        st.session_state["last_track"] = uploaded_track["name"]
    watermark_on = add_watermark

    if mode == "Upload a file":
        if uploaded is None:
            st.warning("Choose a video file first.")
        else:
            data = {"file": (uploaded.name, uploaded.getvalue(), uploaded.type or "video/mp4")}
            job = call(
                "POST", "/api/jobs/upload", files=data,
                data={
                    "music": music_value,
                    "watermark": "on" if watermark_on else "off",
                    "watermark_position": wm_position if watermark_on else "",
                    "watermark_file": wm_uploaded_name if watermark_on else "",
                    "voice": voice_value,
                    "hook": "on" if hook_on else "off",
                    "hook_playbook": "on" if playbook_on else "off",
                    "hook_platform": hook_platform if playbook_on else "",
                    "hook_niche": hook_niche,
                    "hook_viewer": hook_viewer,
                    "viral_analysis": "on" if viral_on else "off",
                    "publish_metadata": "on" if publish_on else "off",
                    "caption_mode": caption_mode,
                    "subtitle": subtitle_value,
                    "num_clips": str(num_clips),
                },
            )
    else:
        if not url.strip():
            st.warning("Paste a URL first.")
        else:
            job = call(
                "POST", "/api/jobs/url",
                json={
                    "url": url.strip(),
                    "music": music_value,
                    "watermark": watermark_on,
                    "watermark_position": wm_position if watermark_on else "",
                    "watermark_file": wm_uploaded_name if watermark_on else "",
                    "voice": voice_value,
                    "hook": hook_on,
                    "hook_playbook": playbook_on,
                    "hook_platform": hook_platform if playbook_on else "",
                    "hook_niche": hook_niche,
                    "hook_viewer": hook_viewer,
                    "viral_analysis": viral_on,
                    "publish_metadata": publish_on,
                    "caption_mode": caption_mode,
                    "subtitle": subtitle_value,
                    "num_clips": num_clips,
                },
            )

    if job:
        st.session_state["job_id"] = job["id"]
        st.rerun()

# ---------------------------------------------------------------- status ------
job_id = st.session_state.get("job_id")
if not job_id:
    st.info("Submit a video to get started.")
    st.stop()

# Jobs live in the API's memory only — a server restart (or uvicorn --reload
# after editing app code) forgets them, so a stored id can go stale. Handle
# that with a clear message instead of a raw 404 on every rerun.
try:
    resp = httpx.get(f"{api_base()}/api/jobs/{job_id}", timeout=120.0)
except httpx.HTTPError as exc:
    st.error(f"API request failed: {exc}")
    st.stop()
if resp.status_code == 404:
    st.session_state.pop("job_id", None)
    st.warning(
        f"Job `{job_id}` no longer exists — the API server was restarted "
        "(jobs are kept in memory only). Submit your video again."
    )
    time.sleep(4)
    st.rerun()
if not resp.is_success:
    st.error(f"API request failed: {resp.status_code} {resp.reason_phrase}")
    st.stop()
job = resp.json()

if job["status"] == "error":
    st.error(f"Job failed: {job.get('error') or 'unknown error'}")
    with st.expander("Log"):
        st.code("\n".join(job.get("log", [])))
    if st.button("Clear"):
        st.session_state.pop("job_id", None)
        st.rerun()
    st.stop()

if job["status"] in ("queued", "running"):
    st.subheader(f"Job `{job['id']}` — {job['stage']}")
    st.progress(job["progress"], text=f"{job['progress'] * 100:.0f}% · {job['stage']}")
    tail = job.get("log", [])[-8:]
    if tail:
        st.code("\n".join(tail), language="text")
    time.sleep(2)
    st.rerun()

# ---------------------------------------------------------------- results -----
st.subheader(f"Your shorts — `{job['id']}`")
meta_cols = st.columns(4)
meta_cols[0].metric("Highlights", len(job["highlights"]))
meta_cols[1].metric("Source", f"{job.get('duration') or 0:.0f}s")
meta_cols[2].metric("Language", (job.get("language") or "—").upper())
meta_cols[3].metric("Selector", job.get("provider") or "—")

clips = [a for a in job["artifacts"] if a["kind"] == "video"]
if not clips:
    st.warning("No clips were produced.")
    st.stop()

if not any(a["name"].endswith("_publish.json") for a in job["artifacts"]):
    st.caption(
        "💡 No IG/YT suggestions for this job — it ran with the 📱 Publish "
        "metadata toggle off (or before it existed). New jobs include them by default."
    )

# Group each clip's variants (normal + 🪝 hook) into one card with a switch.
variants: dict = {}
for clip in clips:
    slot = variants.setdefault((clip.get("start"), clip.get("end")), {})
    slot["hook" if clip["name"].endswith("_hook.mp4") else "normal"] = clip
cards = list(variants.values())

# 📱 Publish metadata (when the toggle was on): fetch each clip's suggested
# Instagram + YouTube text once, so every card can show it below the video.
publish_meta: dict = {}
for a in job["artifacts"]:
    if not a["name"].endswith("_publish.json"):
        continue
    try:
        resp = httpx.get(f"{api_base()}{a['url']}", timeout=60.0)
        if resp.status_code == 200:
            publish_meta[a["name"][: -len("_publish.json")]] = resp.json()
    except httpx.HTTPError:
        pass

# 🔌 Linked social accounts (YouTube/Instagram) — read once per render so
# every clip card's 📣 Post panel can list them.
social_accounts: list = (call("GET", "/api/social/accounts") or {}).get("accounts", []) or []


def _clip_tag(name: str) -> str:
    """clip_01.mp4 / clip_01_hook.mp4 → clip_01 (the publish file's stem)."""
    tag = name[:-4] if name.endswith(".mp4") else name
    return tag[:-5] if tag.endswith("_hook") else tag


def _render_publish_suggestions(pub: dict) -> None:
    """Ready-to-post IG + YT text below a clip, copy-ready."""
    ig = pub.get("instagram") or {}
    yt = pub.get("youtube") or {}
    if not ig or not yt:
        return
    st.markdown("**📱 Suggested captions & titles**")
    left, right = st.columns(2)
    with left:
        st.caption("Instagram title")
        st.code(str(ig.get("title", "")), language=None)
        st.caption("Instagram tags")
        st.code(" ".join(ig.get("hashtags", [])), language=None)
    with right:
        yt_title = str(yt.get("title", ""))
        st.caption(f"YouTube title ({len(yt_title)}/100)")
        st.code(yt_title, language=None)
        st.caption("YouTube tags")
        tags = yt.get("tags", [])
        st.code(", ".join(tags[:12]) + (" …" if len(tags) > 12 else ""), language=None)
    st.caption("YouTube description")
    st.code(str(yt.get("description", "")), language=None)


def _render_post_panel(base: dict, chosen: dict, pub: dict | None) -> None:
    """📣 Post this clip directly to a linked YouTube / Instagram / TikTok
    account, pre-filling the title/description/caption/tags the app generated."""
    if not social_accounts:
        st.caption(
            "📣 Post to YouTube / Instagram / TikTok — link your accounts on "
            "the 🔌 Accounts page and this button appears here."
        )
        return
    with st.expander("📣 Post to YouTube / Instagram / TikTok", expanded=False):
        labels = {
            f"{a['platform'].title()} · {a.get('handle') or a.get('label') or a['id']}": a
            for a in social_accounts
        }
        pick = st.selectbox(
            "Post to", list(labels), key=f"p-acc-{job['id']}-{base['name']}"
        )
        acc = labels[pick]
        pub = pub or {}
        suffix = f"{job['id']}-{base['name']}-{acc['id']}"
        payload: dict = {
            "job_id": job["id"],
            "file": chosen["name"],
            "account_id": acc["id"],
        }
        if acc["platform"] == "youtube":
            yt = pub.get("youtube") or {}
            title = st.text_input(
                "Title", key=f"p-t-{suffix}",
                value=str(yt.get("title") or base.get("title") or ""),
            )
            description = st.text_area(
                "Description", key=f"p-d-{suffix}",
                value=str(yt.get("description") or ""), height=170,
            )
            tags_text = st.text_input(
                "Tags (comma separated)", key=f"p-g-{suffix}",
                value=", ".join(str(t) for t in (yt.get("tags") or [])),
            )
            payload.update(
                title=title, description=description,
                tags=[t.strip() for t in tags_text.split(",") if t.strip()],
            )
            st.caption(
                f"{len(title)}/100 title chars · vertical uploads show up as Shorts"
            )
        elif acc["platform"] == "tiktok":
            ig = pub.get("instagram") or {}
            yt = pub.get("youtube") or {}
            default_caption = str(ig.get("caption") or yt.get("title") or "")
            caption = st.text_area(
                "Caption", key=f"p-tt-{suffix}",
                value=default_caption, height=170,
            )
            payload.update(
                caption=caption,
                title=str(ig.get("title") or yt.get("title") or ""),
            )
            st.caption(f"{len(caption)}/2200 caption chars · one caption field on TikTok")
        else:
            ig = pub.get("instagram") or {}
            caption = st.text_area(
                "Caption", key=f"p-c-{suffix}",
                value=str(ig.get("caption") or ""), height=170,
            )
            payload.update(caption=caption, title=str(ig.get("title") or ""))
            st.caption(f"{len(caption)}/2200 caption chars · hashtags included")
        if st.button(
            "🚀 Post now", type="primary", key=f"p-b-{suffix}",
            use_container_width=True,
        ):
            with st.spinner(
                f"Uploading to {acc['platform'].title()} — big clips can take a few minutes…"
            ):
                result = call("POST", "/api/social/post", json=payload, timeout=3600.0)
            if result:
                acct = result.get("account") or {}
                who = acct.get("handle") or acct.get("label") or ""
                st.success(f"✅ Posted to {who or result.get('platform', '')}")
                if result.get("url"):
                    st.markdown(
                        f"[Open on {str(result.get('platform', '')).title()} →]({result['url']})"
                    )


def _render_clip_card(card: dict) -> None:
    normal, hook = card.get("normal"), card.get("hook")
    base = normal or hook
    title = (normal or {}).get("title") or base.get("title") or "Clip"
    st.markdown(f"**{title}**")
    chosen = base
    if normal and hook:
        version = st.segmented_control(
            "Version", ["Normal", "🪝 Hook"],
            key=f"ver-{job['id']}-{base['name']}",
            default="Normal",
        )
        chosen = hook if version == "🪝 Hook" else normal
    st.caption(
        f"{chosen['start']:.1f}s – {chosen['end']:.1f}s · {chosen['size'] / 1e6:.1f} MB"
        + (" · 🪝 flash-forward cold open" if chosen is hook else "")
    )
    st.video(f"{api_base()}{chosen['url']}")  # absolute URL, not a local path
    blob = httpx.get(f"{api_base()}{chosen['url']}", timeout=120.0)
    if blob.status_code == 200:
        st.download_button(
            "⬇ Download",
            data=blob.content,
            file_name=chosen["name"],
            mime="video/mp4",
            key=f"dl-{job['id']}-{chosen['name']}",
            use_container_width=True,
        )
    other = hook if chosen is normal else normal
    if other:
        st.caption(f"Other version: [{other['name']}]({api_base()}{other['url']})")
    pub = publish_meta.get(_clip_tag(base["name"]))
    if pub:
        _render_publish_suggestions(pub)
    _render_post_panel(base, chosen, pub)


for row in range(0, len(cards), 2):
    cols = st.columns(2)
    for col, card in zip(cols, cards[row : row + 2]):
        with col:
            _render_clip_card(card)

with st.expander("Highlights + transcript artifacts"):
    st.table(
        [
            {"#": i, "start": h["start"], "end": h["end"], "title": h["title"]}
            for i, h in enumerate(job["highlights"], 1)
        ]
    )
    for a in job["artifacts"]:
        if a["kind"] != "video":
            st.markdown(f"[{a['name']}]({api_base()}{a['url']}) — {a['title']}")

with st.expander("Job log"):
    st.code("\n".join(job.get("log", [])), language="text")
