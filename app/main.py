"""FastAPI backend: submit videos (upload or URL), poll jobs, fetch artifacts."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app import __version__, config, social as socialpub
from app.jobs import JobManager
from app.pipeline import audio_mix, campaigns, ingest, run as pipeline_run

app = FastAPI(title="Shorts Studio API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = JobManager()

# Hard top limit for the "how many clips" option (UI slider + API num_clips).
MAX_CLIPS = 10

MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".json": "application/json",
    ".ass": "text/plain; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
}


class UrlBody(BaseModel):
    url: str
    music: str = ""            # "" (off) | "auto" | library filename | path
    watermark: bool = config.WATERMARK_DEFAULT_ON
    watermark_position: str = ""   # "" = config default; top-left/…/bottom-right
    watermark_file: str = ""       # "" = default asset; else name in WATERMARK_DIR
    voice: str = ""                # "" (off) | voice label ("aria", "guy", …)
    hook: bool | None = None        # None = server default; else flash-forward hook versions
    hook_playbook: bool | None = None  # None = server default; else scored hook-options playbook
    hook_platform: str = ""          # "" = config default; "both" | "shorts" | "reels"
    hook_niche: str = ""              # e.g. "fitness beginners" — sharpens hook relevance
    hook_viewer: str = ""             # target viewer description
    viral_analysis: bool | None = None  # None = server default; else 1M+ pattern checks
    publish_metadata: bool | None = None  # None = server default; else IG + YT metadata per clip
    caption_mode: str = ""            # "" = server default; "animated" | "static" (stationary box)
    subtitle: str = ""                # static mode: your own text ("" = suggest per clip)
    num_clips: int | None = None      # None = server default; else 1..MAX_CLIPS clips to cut


def _launch(
    job, source: str, kind: str, *,
    music: str = "", watermark: bool | None = None,
    watermark_position: str = "", watermark_file: str = "", voice: str = "",
    hook: bool | None = None,
    hook_playbook: bool | None = None, hook_platform: str = "",
    hook_niche: str = "", hook_viewer: str = "",
    viral_analysis: bool | None = None,
    publish_metadata: bool | None = None,
    caption_mode: str = "", subtitle: str = "",
    max_clips: int | None = None,
) -> None:
    def worker(j):
        return pipeline_run.run_pipeline(
            source,
            j.workdir,
            kind=kind,
            progress=lambda p, stage=None: j.update(progress=p, stage=stage),
            log=j.append_log,
            max_highlights=max_clips,
            music=music,
            watermark_on=watermark,
            watermark_position=watermark_position or None,
            watermark_file=watermark_file or None,
            voice=voice or None,
            hook=hook,
            hook_playbook=hook_playbook, hook_platform=hook_platform or None,
            hook_niche=hook_niche, hook_viewer=hook_viewer,
            viral_analysis=viral_analysis,
            publish_metadata=publish_metadata,
            caption_mode=caption_mode or None, subtitle=subtitle,
        )

    manager.start(job, worker)


def _parse_watermark(watermark: str) -> bool:
    return str(watermark).strip().lower() not in {"0", "false", "no", "off"}


def _parse_toggle(value: str) -> bool | None:
    """Form string → tri-state: "" = server default, else True/False."""
    v = str(value).strip().lower()
    if not v:
        return None
    return v not in {"0", "false", "no", "off"}


def _parse_num_clips(value) -> int | None:
    """Clip count → int clamped to 1..MAX_CLIPS; empty/None = server default."""
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        raise HTTPException(400, "num_clips must be a number 1–10")
    return max(1, min(MAX_CLIPS, n))


@app.get("/")
def index() -> dict:
    return {
        "name": "Shorts Studio API",
        "version": __version__,
        "endpoints": [
            "POST /api/jobs/upload  (multipart field: file)",
            "POST /api/jobs/url     (JSON: {url, music, watermark, voice, hook, …})",
            "GET  /api/jobs",
            "GET  /api/jobs/{id}",
            "GET  /api/jobs/{id}/files/{name}",
            "GET  /api/music        (library + trending-sound suggestions)",
            "POST /api/music/upload (add a track to the library)",
            "GET  /api/voices       (AI voice options + watermark positions)",
            "POST /api/watermark/upload (drag-in watermark image)",
            "GET  /api/jobdirs      (finished jobs on disk, for campaign audits)",
            "GET  /api/campaigns    (saved campaign rule sets)",
            "POST /api/campaigns    (create/update a campaign)",
            "POST /api/campaigns/extract (rules file: pdf/txt/md/docx/json → rules)",
            "POST /api/campaigns/{id}/analyze (audit a job's clips against the rules)",
            "GET  /api/campaigns/{id}/reports/{job}?format=json|md",
            "GET  /api/social/accounts  (linked YouTube/Instagram/TikTok accounts)",
            "GET  /api/social/youtube/auth_url  (OAuth link for a YouTube account)",
            "GET  /api/social/tiktok/auth_url  (OAuth link for a TikTok account)",
            "POST /api/social/instagram/connect  (token + IG user id)",
            "POST /api/social/post  (post a job's clip to a linked account)",
        ],
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/jobs/upload")
async def create_upload_job(
    file: UploadFile = File(...),
    music: str = Form(""),
    watermark: str = Form(""),
    watermark_position: str = Form(""),
    watermark_file: str = Form(""),
    voice: str = Form(""),
    hook: str = Form(""),
    hook_playbook: str = Form(""),
    hook_platform: str = Form(""),
    hook_niche: str = Form(""),
    hook_viewer: str = Form(""),
    viral_analysis: str = Form(""),
    publish_metadata: str = Form(""),
    caption_mode: str = Form(""),
    subtitle: str = Form(""),
    num_clips: str = Form(""),
) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ingest.MEDIA_SUFFIXES:
        raise HTTPException(
            400, f"unsupported file type '{suffix}' — expected {sorted(ingest.MEDIA_SUFFIXES)}"
        )

    job = manager.create("upload", file.filename or "upload")
    dest = job.workdir / f"input{suffix}"
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > config.MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "file exceeds the upload size limit")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "empty upload")

    _launch(job, str(dest), "upload", music=music,
            watermark=_parse_watermark(watermark) if watermark.strip() else None,
            watermark_position=watermark_position, watermark_file=watermark_file,
            voice=voice, hook=_parse_toggle(hook),
            hook_playbook=_parse_toggle(hook_playbook) if hook_playbook.strip() else None,
            hook_platform=hook_platform, hook_niche=hook_niche, hook_viewer=hook_viewer,
            viral_analysis=_parse_toggle(viral_analysis) if viral_analysis.strip() else None,
            publish_metadata=_parse_toggle(publish_metadata) if publish_metadata.strip() else None,
            caption_mode=caption_mode, subtitle=subtitle,
            max_clips=_parse_num_clips(num_clips))
    return job.to_model().model_dump()


@app.post("/api/jobs/url")
def create_url_job(body: UrlBody) -> dict:
    url = body.url.strip()
    if not ingest.is_url(url):
        raise HTTPException(400, "expected an http(s) URL")
    job = manager.create("url", url)
    _launch(job, url, "url", music=body.music, watermark=body.watermark,
            watermark_position=body.watermark_position,
            watermark_file=body.watermark_file, voice=body.voice, hook=body.hook,
            hook_playbook=body.hook_playbook, hook_platform=body.hook_platform,
            hook_niche=body.hook_niche, hook_viewer=body.hook_viewer,
            viral_analysis=body.viral_analysis,
            publish_metadata=body.publish_metadata,
            caption_mode=body.caption_mode, subtitle=body.subtitle,
            max_clips=_parse_num_clips(body.num_clips))
    return job.to_model().model_dump()


@app.get("/api/music")
def list_music() -> dict:
    """Local music library + editable trending-sound suggestions."""
    return {
        "dir": str(config.MUSIC_DIR),
        "tracks": [p.name for p in audio_mix.list_library()],
        "trending": list(config.TRENDING_SOUNDS),
    }


@app.post("/api/music/upload")
async def upload_music(file: UploadFile = File(...)) -> dict:
    """Add the user's own audio file to the local music library."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in config.MUSIC_SUFFIXES:
        raise HTTPException(
            400, f"unsupported audio type '{suffix}' — expected {sorted(config.MUSIC_SUFFIXES)}"
        )
    config.MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MUSIC_DIR / f"{Path(file.filename or 'track').stem}{suffix}"
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > config.MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "audio file exceeds the upload size limit")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "empty upload")
    return {"name": dest.name, "dir": str(config.MUSIC_DIR)}


@app.get("/api/voices")
def list_voices() -> dict:
    """Selectable AI voice options (label -> engine voice name)."""
    from app.pipeline import tts

    return {"voices": tts.list_voices(), "positions": list(config.WATERMARK_POSITIONS)}


@app.post("/api/watermark/upload")
async def upload_watermark(file: UploadFile = File(...)) -> dict:
    """Accept the watermark image the user dragged in (PNG/JPG/WEBP)."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, f"unsupported image type '{suffix}' — expected png/jpg/webp")
    config.WATERMARK_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.WATERMARK_DIR / f"{Path(file.filename or 'mark').stem}{suffix}"
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > 20 * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "watermark image exceeds 20 MB")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "empty upload")
    return {"name": dest.name, "dir": str(config.WATERMARK_DIR)}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return [j.to_model().model_dump() for j in manager.list()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_model().model_dump()


@app.get("/api/jobs/{job_id}/files/{name}")
def get_job_file(job_id: str, name: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise HTTPException(400, "invalid file name")
    path = (job.workdir / name).resolve()
    if not path.is_relative_to(job.workdir.resolve()) or not path.is_file():
        raise HTTPException(404, "file not found")
    media = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


# --------------------------------------------------------------------------- #
# Campaign rules (Whop & clip-payment campaigns): store rule sets, audit every
# clip of a finished job against them, keep the report on disk.
# --------------------------------------------------------------------------- #
class CampaignBody(BaseModel):
    name: str
    brand: str = ""
    platform: str = "whop"          # whop | instagram | youtube | tiktok | other
    notes: str = ""
    rules: dict = {}                # validated/normalised by campaigns.normalize_rules


class AnalyzeBody(BaseModel):
    job_id: str


@app.get("/api/jobdirs")
def list_jobdirs() -> list[dict]:
    """Finished jobs on disk (campaign audits survive API restarts)."""
    return campaigns.list_jobdirs()


@app.get("/api/campaigns")
def list_campaigns_endpoint() -> dict:
    return {"campaigns": campaigns.list_campaigns()}


@app.post("/api/campaigns/extract")
async def extract_campaign_rules(file: UploadFile = File(...)) -> dict:
    """Pull structured rules out of an uploaded rules file
    (PDF / TXT / MD / DOCX / JSON) — used by the Campaigns page."""
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "rules file exceeds 20 MB")
    try:
        return campaigns.read_rules_file(file.filename or "rules.txt", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - corrupt/encrypted PDF etc.
        raise HTTPException(422, f"could not read {file.filename}: {exc}")


@app.post("/api/campaigns")
def create_campaign(body: CampaignBody) -> dict:
    try:
        return campaigns.save_campaign(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/campaigns/{cid}")
def get_campaign(cid: str) -> dict:
    try:
        return campaigns.load_campaign(cid)
    except FileNotFoundError:
        raise HTTPException(404, "campaign not found")


@app.delete("/api/campaigns/{cid}")
def remove_campaign(cid: str) -> dict:
    if not campaigns.delete_campaign(cid):
        raise HTTPException(404, "campaign not found")
    return {"deleted": cid}


@app.post("/api/campaigns/{cid}/analyze")
def analyze_campaign(cid: str, body: AnalyzeBody) -> dict:
    """Audit every clip of a job against the campaign's rules (fast, text-only)."""
    try:
        campaign = campaigns.load_campaign(cid)
        workdir = campaigns.job_workdir(body.job_id)
    except FileNotFoundError:
        raise HTTPException(404, "campaign not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        return campaigns.analyze_job(campaign, workdir)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/campaigns/{cid}/reports/{job_id}")
def get_campaign_report(cid: str, job_id: str, format: str = "json"):
    """Re-read a stored audit report (format=json|md)."""
    try:
        path = campaigns.report_path(cid, job_id, markdown=(format == "md"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not path.is_file():
        raise HTTPException(404, "report not found — run the analysis first")
    media = "text/plain; charset=utf-8" if path.suffix == ".md" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)


# --------------------------------------------------------------------------- #
# Social publishing: link multiple YouTube / Instagram accounts, post clips
# with the generated title/description straight from a job's files.
# --------------------------------------------------------------------------- #
class InstagramConnectBody(BaseModel):
    access_token: str
    ig_user_id: str
    label: str = ""


class InstagramRefreshBody(BaseModel):
    account_id: str


class PostBody(BaseModel):
    job_id: str
    file: str                    # clip filename inside the job dir, e.g. clip_01.mp4
    account_id: str
    title: str = ""              # empty = defaults from clip_XX_publish.json
    description: str = ""
    caption: str = ""
    tags: list[str] = []
    privacy: str = ""            # YT only: "" = config default (public)


@app.get("/api/social/accounts")
def list_social_accounts() -> dict:
    return {
        "accounts": socialpub.list_accounts(),
        "youtube_configured": socialpub.youtube_configured(),
        "youtube_redirect_uri": config.SOCIAL_REDIRECT_URI,
        "tiktok_configured": socialpub.tiktok_configured(),
        "tiktok_redirect_uri": config.TIKTOK_REDIRECT_URI,
        "instagram_configured": socialpub.instagram_oauth_configured(),
        "instagram_redirect_uri": config.IG_REDIRECT_URI,
    }


@app.delete("/api/social/accounts/{account_id}")
def delete_social_account(account_id: str) -> dict:
    if not socialpub.remove_account(account_id):
        raise HTTPException(404, "account not found")
    return {"deleted": account_id}


@app.get("/api/social/youtube/auth_url")
def youtube_auth_url() -> dict:
    """Start the Google OAuth round (opens in the user's browser)."""
    try:
        state, url = socialpub.begin_youtube_auth()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "state": state, "redirect_uri": config.SOCIAL_REDIRECT_URI}


@app.get("/api/social/youtube/callback")
def youtube_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Google redirects here after consent — store the channel, show a page."""
    try:
        if error:
            raise RuntimeError(f"Google said: {error}")
        account = socialpub.complete_youtube_auth(state, code)
    except (RuntimeError, ValueError) as exc:
        return HTMLResponse(
            "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
            f"<h3>❌ Could not connect</h3><p>{exc}</p>"
            "<p>Close this tab and try again from Shorts Studio.</p></body>",
            status_code=400,
        )
    name = account.get("handle") or account.get("label") or "your channel"
    return HTMLResponse(
        "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
        f"<h3>✅ Connected {name}</h3>"
        "<p>This account is now available for one-click clip posting in "
        "Shorts Studio. Close this tab and come back to the app.</p></body>"
    )


@app.get("/api/social/tiktok/auth_url")
def tiktok_auth_url() -> dict:
    """Start the TikTok Login Kit round (opens in the user's browser)."""
    try:
        state, url = socialpub.begin_tiktok_auth()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "state": state, "redirect_uri": config.TIKTOK_REDIRECT_URI}


@app.get("/api/social/tiktok/callback")
def tiktok_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """TikTok redirects here after consent — store the account, show a page."""
    try:
        if error:
            raise RuntimeError(f"TikTok said: {error}")
        account = socialpub.complete_tiktok_auth(state, code)
    except (RuntimeError, ValueError) as exc:
        return HTMLResponse(
            "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
            f"<h3>❌ Could not connect</h3><p>{exc}</p>"
            "<p>Close this tab and try again from Shorts Studio.</p></body>",
            status_code=400,
        )
    name = account.get("handle") or account.get("label") or "your TikTok"
    return HTMLResponse(
        "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
        f"<h3>✅ Connected {name}</h3>"
        "<p>This TikTok account is now available for one-click clip posting in "
        "Shorts Studio. Close this tab and come back to the app.</p></body>"
    )


@app.get("/api/social/instagram/auth_url")
def instagram_auth_url() -> dict:
    """Start the Facebook OAuth round (opens in the user's browser)."""
    try:
        state, url = socialpub.begin_instagram_auth()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "state": state, "redirect_uri": config.IG_REDIRECT_URI}


@app.get("/api/social/instagram/callback")
def instagram_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Meta redirects here after consent — store the account(s), show a page."""
    try:
        if error:
            raise RuntimeError(f"Facebook said: {error}")
        linked = socialpub.complete_instagram_auth(state, code)
    except (RuntimeError, ValueError) as exc:
        return HTMLResponse(
            "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
            f"<h3>❌ Could not connect</h3><p>{exc}</p>"
            "<p>Close this tab and try again from Shorts Studio.</p></body>",
            status_code=400,
        )
    names = ", ".join(
        str(a.get("handle") or a.get("label") or "account") for a in linked
    )
    extra = f" (+{len(linked) - 1} more)" if len(linked) > 1 else ""
    return HTMLResponse(
        "<body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
        f"<h3>✅ Connected {names}{extra}</h3>"
        "<p>This Instagram account is now available for one-click clip posting "
        "in Shorts Studio. Close this tab and come back to the app.</p></body>"
    )


@app.post("/api/social/instagram/refresh")
def refresh_instagram_endpoint(body: InstagramRefreshBody) -> dict:
    """Exchange a linked account's token for a fresh 60-day one."""
    try:
        info = socialpub.reconnect_instagram(body.account_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return {"account": body.account_id, "token": info}


@app.post("/api/social/instagram/connect")
def connect_instagram_endpoint(body: InstagramConnectBody) -> dict:
    try:
        return socialpub.connect_instagram(
            body.access_token, body.ig_user_id, label=body.label
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/social/post")
def post_to_social(body: PostBody) -> dict:
    """Upload one clip of a finished job to one linked account."""
    job = manager.get(body.job_id)
    if job is None:
        raise HTTPException(404, "job not found — jobs live in memory; resubmit it")
    name = Path(body.file).name
    if not name or name != body.file or "/" in body.file or "\\\\" in body.file:
        raise HTTPException(400, "invalid file name")
    path = (job.workdir / name).resolve()
    if not path.is_relative_to(job.workdir.resolve()) or not path.is_file():
        raise HTTPException(404, "clip file not found")
    try:
        return socialpub.post_clip(
            body.account_id, path,
            title=body.title, description=body.description,
            caption=body.caption, tags=body.tags, privacy=body.privacy,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(502, str(exc))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=config.API_HOST, port=config.API_PORT, reload=False)
