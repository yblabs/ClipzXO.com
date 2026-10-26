"""Central configuration. Everything is overridable through environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

try:  # optional: pick up a project .env file when python-dotenv is available
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(ROOT_DIR / ".env", override=False)
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


# --- storage -----------------------------------------------------------------
DATA_DIR = Path(_env("SHORTS_DATA_DIR", str(ROOT_DIR / "data")))
JOBS_DIR = DATA_DIR / "jobs"

# --- source download (yt-dlp) -------------------------------------------------
# Cap the height of URL downloads. YouTube's "best video" is often 4K —
# multiple GB and minutes of waiting for a clip rendered at 1080x1920.
# 1080 keeps jobs fast; the selector falls back to 2x for portrait sources.
# 0 = uncapped (whatever "best" means — usually 4K).
DOWNLOAD_MAX_HEIGHT = _env_int("DOWNLOAD_MAX_HEIGHT", 1080)

# --- transcription -----------------------------------------------------------
WHISPER_MODEL = _env("WHISPER_MODEL", "base.en")  # tiny.en | base.en | small.en | medium.en | large-v3
WHISPER_DEVICE = _env("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = _env("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "") or None  # None => auto-detect

# --- highlight selection -----------------------------------------------------
LLM_PROVIDER = _env("LLM_PROVIDER", "auto")  # auto | openai | anthropic | heuristic
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
MAX_HIGHLIGHTS = _env_int("MAX_HIGHLIGHTS", 10)   # clips to make per uploaded video
MIN_HIGHLIGHTS = _env_int("MIN_HIGHLIGHTS", 3)
MIN_HIGHLIGHT_SECONDS = _env_float("MIN_HIGHLIGHT_SECONDS", 30.0)
MAX_HIGHLIGHT_SECONDS = _env_float("MAX_HIGHLIGHT_SECONDS", 60.0)

# --- rendering ---------------------------------------------------------------
TARGET_WIDTH = _env_int("TARGET_WIDTH", 1080)
TARGET_HEIGHT = _env_int("TARGET_HEIGHT", 1920)
VIDEO_CRF = _env_int("VIDEO_CRF", 20)
VIDEO_PRESET = _env("VIDEO_PRESET", "veryfast")
AUDIO_BITRATE = _env("AUDIO_BITRATE", "160k")

# --- captions ----------------------------------------------------------------
CAPTION_FONT = _env("CAPTION_FONT", "Arial")
CAPTION_FONT_SIZE = _env_int("CAPTION_FONT_SIZE", 92)
CAPTION_MAX_WORDS = _env_int("CAPTION_MAX_WORDS", 4)
CAPTION_MAX_CHARS = _env_int("CAPTION_MAX_CHARS", 20)
CAPTION_POS_X = _env_int("CAPTION_POS_X", TARGET_WIDTH // 2)
CAPTION_POS_Y = _env_int("CAPTION_POS_Y", 1310)
# ASS colours are &HAABBGGRR& (alpha, blue, green, red).
CAPTION_PRIMARY = _env("CAPTION_PRIMARY", "&H00FFFFFF")  # colour of words not yet spoken
CAPTION_HIGHLIGHT = _env("CAPTION_HIGHLIGHT", "&H0000D4FF")  # accent while a word is being spoken
CAPTION_OUTLINE = _env("CAPTION_OUTLINE", "&H00101010")
CAPTION_OUTLINE_WIDTH = _env_float("CAPTION_OUTLINE_WIDTH", 9.0)
CAPTION_UPPERCASE = _env("CAPTION_UPPERCASE", "1").lower() not in {"0", "false", "no"}
CAPTION_LETTER_SPACING = _env_int("CAPTION_LETTER_SPACING", 0)
CAPTION_FADE_IN = _env_int("CAPTION_FADE_IN", 50)
CAPTION_FADE_OUT = _env_int("CAPTION_FADE_OUT", 70)
CAPTION_POP_FROM = _env_int("CAPTION_POP_FROM", 86)  # scale % the line starts at
CAPTION_POP_MS = _env_int("CAPTION_POP_MS", 90)

# --- caption mode: animated (default) or stationary subtitle -----------------
# animated = the moving word-by-word captions above.
# static   = ONE text block fixed in an opaque box for the whole clip, like
#            the big streamer clip channels. The text is either written
#            manually (subtitle=…) or suggested per clip from the title /
#            detected payoff moment (subtitle empty).
CAPTION_MODE = _env("CAPTION_MODE", "animated")  # animated | static
STATIC_FONT_SIZE = _env_int("STATIC_FONT_SIZE", 64)
STATIC_TEXT_COLOUR = _env("STATIC_TEXT_COLOUR", "&H00000000")  # black text
STATIC_BOX_COLOUR = _env("STATIC_BOX_COLOUR", "&H00FFFFFF")    # white box

# --- music (optional background track) ---------------------------------------
MUSIC_DIR = Path(_env("SHORTS_MUSIC_DIR", str(ROOT_DIR / "assets" / "music")))
MUSIC_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MUSIC_VOLUME = _env_float("MUSIC_VOLUME", 0.45)   # level of the added track
MUSIC_OFFSET = _env_float("MUSIC_OFFSET", 0.0)    # seconds into the track (put the drop here)
ORIGINAL_VOLUME = _env_float("ORIGINAL_VOLUME", 1.0)  # original clip audio (0 = muted)
# Suggestions only — the app never downloads or bundles copyrighted songs;
# the user supplies their own audio files. Edit freely.
TRENDING_SOUNDS = [
    "\"Strange\" — Vincemp3",
    "\"Abbi\" — Vincemp3",
    "\"Boyna Galava\" — Gio Pika",
    "Other Gio Pika / Russian-rap sounds trending in MMA edits",
]

# --- watermark (optional) -----------------------------------------------------
WATERMARK_PATH = Path(_env("WATERMARK_PATH", str(ROOT_DIR / "assets" / "watermark.png")))
WATERMARK_DIR = Path(_env("WATERMARK_DIR", str(ROOT_DIR / "assets" / "watermarks")))  # user-uploaded files
WATERMARK_DEFAULT_ON = _env("WATERMARK_DEFAULT_ON", "1").lower() not in {"0", "false", "no"}
WATERMARK_WIDTH_FRAC = _env_float("WATERMARK_WIDTH_FRAC", 0.35)   # ~35% of video width
WATERMARK_BOTTOM_MARGIN = _env_float("WATERMARK_BOTTOM_MARGIN", 0.15)  # stay out of bottom 15%
WATERMARK_TOP_MARGIN = _env_float("WATERMARK_TOP_MARGIN", 0.10)   # stay out of top 10%
WATERMARK_H_MARGIN = _env_float("WATERMARK_H_MARGIN", 0.04)       # side gap (fraction of width)
# Placement options: the four corners the UI offers, plus the classic centred
# bottom position kept for config/CLI users.
WATERMARK_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "bottom-center")
WATERMARK_POSITION = _env("WATERMARK_POSITION", "bottom-center")

# --- hook versions (flash-forward cold open) ---------------------------------
# Each clip also gets a "hook" cut: it opens with a short teaser taken from
# later in the same highlight (the payoff moment), then plays normally.
# Teaser selection analyses the whole video: audio excitement (crowd cheers)
# + funny/intense text, and stretches a few seconds past a detected peak.
HOOK_VERSIONS = _env("HOOK_VERSIONS", "1").lower() not in {"0", "false", "no"}
HOOK_TEASER_SECONDS = _env_float("HOOK_TEASER_SECONDS", 4.0)   # teaser length (a few seconds so the peak lands)
HOOK_MIN_GAP_SECONDS = _env_float("HOOK_MIN_GAP_SECONDS", 6.0)  # teaser must be at least this far after the clip start
HOOK_MIN_CLIP_SECONDS = _env_float("HOOK_MIN_CLIP_SECONDS", 8.0)  # skip hook cuts for clips shorter than this
HOOK_AUDIO_WEIGHT = _env_float("HOOK_AUDIO_WEIGHT", 5.0)   # whole-video excitement influence vs text
HOOK_AUDIO_PROMINENCE = _env_float("HOOK_AUDIO_PROMINENCE", 0.85)  # candidates must reach this fraction of the clip's most energetic window (a real cheer outranks text hooks)
HOOK_PEAK_TAIL_SECONDS = _env_float("HOOK_PEAK_TAIL_SECONDS", 1.5)  # seconds kept after a detected peak (the cheer finishes)

# --- hook playbook (scored hook options per clip) ----------------------------
# Research pass over 1M+ view Shorts/Reels: the first 1–3s decide the swipe,
# and winning openings are one of eight archetypes (open loop, bold claim,
# relatable, result-first, pattern interrupt, shareable, mid-action,
# transformation). When on, every clip gets `clip_XX_hooks.json`/`.md` with
# 8 scored options, the winning hook script and a retention beat sheet.
HOOK_PLAYBOOK = _env("HOOK_PLAYBOOK", "1").lower() not in {"0", "false", "no"}
HOOK_PLATFORM = _env("HOOK_PLATFORM", "both")  # "both" | "shorts" | "reels"

# --- viral analysis (1M+ creator/streamer Shorts patterns) -------------------
# Research-backed checks per clip — open on the moment, length band, payoff
# placement, dead air, loop-friendly ending, muted captions, cold-viewer
# context, honest discussion ask — scored 0–100 with prioritized fixes.
# Pattern sources live in app/pipeline/viral.py (RESEARCH).
VIRAL_ANALYSIS = _env("VIRAL_ANALYSIS", "1").lower() not in {"0", "false", "no"}

# --- publish metadata (Instagram + YouTube titles/tags/descriptions) --------
# Every clip also gets clip_XX_publish.json/.md: a copy-paste Instagram
# caption + hashtag set and a YouTube title/tags/description, generated
# deterministically from the clip's own transcript (keywords + payoff line).
# The campaign audit (app/pipeline/campaigns.py) runs on top of these.
PUBLISH_METADATA = _env("PUBLISH_METADATA", "1").lower() not in {"0", "false", "no"}

# --- campaign rules (Whop & clip-payment campaigns) -------------------------
# Structured submission rules (brand words, hashtags, watermark, duration…)
# are stored here and audited against every clip's publish metadata +
# render settings. See app/pipeline/campaigns.py.
CAMPAIGNS_DIR = DATA_DIR / "campaigns"

# --- AI voice (reads the captions aloud) -------------------------------------
# Multiple voice options; keys are UI/CLI labels, values are edge-tts voice names.
# The app uses your network for synthesis (edge-tts); on failure it falls back
# to the local macOS `say` voice, and reports the error if that fails too.
TTS_VOICES = {
    "aria": "en-US-AriaNeural",
    "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural",
    "andrew": "en-US-AndrewNeural",
    "sonia": "en-GB-SoniaNeural",
    "ryan": "en-GB-RyanNeural",
}
TTS_RATE = _env("TTS_RATE", "+8%")          # slightly punchy delivery
TTS_VOLUME = _env_float("TTS_VOLUME", 1.0)  # level of the AI voice
# Original speech volume while the AI voice reads (0 = mute the speaker,
# keeps crowd/arena noise above 0).
TTS_ORIGINAL_VOLUME = _env_float("TTS_ORIGINAL_VOLUME", 0.15)

# --- misc --------------------------------------------------------------------
MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024)  # 4 GiB
API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = _env_int("API_PORT", 8000)

# --- social publishing (link YouTube / Instagram / TikTok accounts) -----------
# YouTube: create a "Desktop app" OAuth client in Google Cloud Console
# (enable YouTube Data API v3, then APIs & Services → Credentials → Create
# credentials → OAuth client ID → Desktop app) and put its id/secret here,
# or drop the downloaded JSON as client_secret.json in the project root.
# Instagram: paste an access token + IG user id of your professional account
# on the 🔌 Accounts page (step-by-step guide there).
# TikTok: register an app on developers.tiktok.com (enable the Content
# Posting API + Login Kit, add the redirect URI below), put its client key /
# secret here, then click Connect on the 🔌 Accounts page.
SOCIAL_DIR = DATA_DIR / "social"                    # linked accounts (credentials, mode 600)
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET", "")
GOOGLE_CLIENT_SECRET_FILE = _env("GOOGLE_CLIENT_SECRET_FILE", str(ROOT_DIR / "client_secret.json"))
SOCIAL_REDIRECT_URI = _env("SOCIAL_REDIRECT_URI", f"http://127.0.0.1:{API_PORT}/api/social/youtube/callback")
YT_PRIVACY_STATUS = _env("YT_PRIVACY_STATUS", "public")   # public | unlisted | private
IG_GRAPH_VERSION = _env("IG_GRAPH_VERSION", "v21.0")      # Meta Graph API version
IG_POLL_SECONDS = _env_float("IG_POLL_SECONDS", 8.0)      # reel-processing poll interval
IG_POLL_TIMEOUT = _env_float("IG_POLL_TIMEOUT", 300.0)     # give up after this many seconds
# Instagram one-click sign-in (recommended): a free Business-type Facebook
# app (developers.facebook.com → Create App → add the Instagram Graph API
# product) gives a "Connect with Facebook" button and lets the app exchange
# the sign-in for a 60-day token it refreshes automatically — linking stops
# going stale. Without it you can still paste a token + IG user id manually.
IG_OAUTH = _env("IG_OAUTH", "1").lower() not in {"0", "off", "no", "manual"}  # set 0 to hide one-click sign-in
IG_APP_ID = _env("IG_APP_ID", "")
IG_APP_SECRET = _env("IG_APP_SECRET", "")
IG_REDIRECT_URI = _env("IG_REDIRECT_URI", f"http://127.0.0.1:{API_PORT}/api/social/instagram/callback")
# TikTok (Content Posting API / Direct Post): create an app on
# developers.tiktok.com, enable Content Posting, add the redirect URI below.
# An unaudited app can only post to your own account as a private draft —
# set TIKTOK_PRIVACY=SELF_ONLY for that, PUBLIC_TO_EVERYONE once approved.
TIKTOK_CLIENT_KEY = _env("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = _env("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = _env("TIKTOK_REDIRECT_URI", f"http://127.0.0.1:{API_PORT}/api/social/tiktok/callback")
TIKTOK_PRIVACY = _env("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE")  # SELF_ONLY | PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS
TIKTOK_POLL_SECONDS = _env_float("TIKTOK_POLL_SECONDS", 6.0)   # publish-status poll interval
TIKTOK_POLL_TIMEOUT = _env_float("TIKTOK_POLL_TIMEOUT", 300.0)  # give up after this many seconds
