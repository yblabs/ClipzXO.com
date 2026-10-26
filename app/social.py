"""Connected social accounts + direct clip publishing.

Link as many YouTube channels as you like (Google OAuth, one click per
account) and as many Instagram professional accounts (one-click "Connect
with Facebook" via your own Facebook app — tokens auto-refresh — or a
pasted Graph API token + IG user id), then post any finished clip straight
from a job — with the title / description / tags the publish-metadata
stage generated.

Credentials live only on disk in ``data/social/accounts.json`` (mode 600)
and are never returned by the API. Publishing is 100 % official API:

  * **YouTube** — Data API v3 resumable upload (``videos.insert``), plain
    REST with google-auth tokens. No google-api-python-client needed.
    Quota: 1 upload unit (default bucket allows ~100 uploads/day).
  * **Instagram** — Graph API ``REELS`` container with resumable byte
    upload of the local file (no public URL required), a processing poll,
    then ``media_publish``. ~50 API posts per account per rolling 24 h.

Both platforms are rate/limit-checked by the API responses themselves;
errors are surfaced verbatim so the UI can show what to fix.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from app import config

_lock = threading.Lock()

# Pending YouTube OAuth states: state -> {"t": created-at epoch,
# "verifier": PKCE code_verifier} (10-minute TTL). The verifier must survive
# the browser round-trip: Google enforces PKCE for new OAuth clients, and the
# token exchange fails with "Missing code verifier" without it.
_pending: dict[str, dict] = {}

YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YT_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"

# TikTok — Login Kit OAuth + Content Posting API (Direct Post, FILE_UPLOAD).
TT_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TT_USER_URL = "https://open.tiktokapis.com/v2/user/info/"
TT_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
TT_VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TT_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TT_SCOPE = "video.upload,video.publish,user.info.basic"
TT_TITLE_MAX = 2200            # TikTok caption hard limit
TT_CHUNK_BYTES = 10_000_000    # FILE_UPLOAD default chunk size
TT_PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY")

# Account-id prefixes per platform (used by add_account).
_ID_PREFIX = {"youtube": "yt", "instagram": "ig", "tiktok": "tt"}

IG_CAPTION_MAX = 2200          # Instagram hard caption limit
# Instagram — Facebook-app OAuth: sign in with Facebook, exchange the code
# for a long-lived Graph token, then discover the professional IG accounts
# linked to that Facebook login's Pages.
IG_AUTH_URL = f"https://www.facebook.com/{config.IG_GRAPH_VERSION}/dialog/oauth"
IG_TOKEN_URL = f"https://graph.facebook.com/{config.IG_GRAPH_VERSION}/oauth/access_token"
IG_OAUTH_SCOPES = "instagram_basic,instagram_content_publish,pages_read_engagement"
IG_TOKEN_WARN_DAYS = 7.0       # auto-refresh when fewer days than this remain
IG_TITLE_MAX = 100             # YouTube hard title limit
YT_TAGS_MAX_TOTAL = 500        # YouTube tag box: total chars incl. commas

HTTP_TIMEOUT = 60.0            # normal API calls
UPLOAD_TIMEOUT = None          # video bytes: no client-side cap


# --------------------------------------------------------------------------- #
# Account store (disk-backed, credential-safe)
# --------------------------------------------------------------------------- #
def _accounts_file() -> Path:
    return config.SOCIAL_DIR / "accounts.json"


def _load() -> list[dict]:
    path = _accounts_file()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    accounts = data.get("accounts")
    return accounts if isinstance(accounts, list) else []


def _save(accounts: list[dict]) -> None:
    path = _accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"accounts": accounts}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)  # credentials file — owner only
    except OSError:  # pragma: no cover - unusual FS
        pass


def public_account(acc: dict) -> dict:
    """API-safe view: everything except the credentials blob."""
    out = {
        "id": acc.get("id", ""),
        "platform": acc.get("platform", ""),
        "label": acc.get("label", ""),
        "handle": acc.get("handle", ""),
        "avatar": acc.get("avatar", ""),
        "created_at": acc.get("created_at", ""),
    }
    if acc.get("platform") == "instagram":
        out["token"] = _ig_token_info(acc)   # health only — never the token
    return out


def list_accounts() -> list[dict]:
    with _lock:
        return [public_account(a) for a in _load()]


def get_account(account_id: str) -> Optional[dict]:
    with _lock:
        for acc in _load():
            if acc.get("id") == account_id:
                return acc
    return None


def add_account(platform: str, label: str = "", handle: str = "", avatar: str = "",
                credentials: Optional[dict] = None) -> dict:
    """Append an account; re-linking the same handle replaces its token."""
    acc = {
        "id": f"{_ID_PREFIX.get(platform, 'ac')}_{uuid.uuid4().hex[:10]}",
        "platform": platform,
        "label": (label or handle or platform).strip(),
        "handle": handle.strip(),
        "avatar": avatar,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "credentials": credentials or {},
    }
    with _lock:
        accounts = _load()
        if handle:
            accounts = [
                a for a in accounts
                if not (a.get("platform") == platform and a.get("handle") == handle)
            ]
        accounts.append(acc)
        _save(accounts)
    return public_account(acc)


def remove_account(account_id: str) -> bool:
    with _lock:
        accounts = _load()
        keep = [a for a in accounts if a.get("id") != account_id]
        if len(keep) == len(accounts):
            return False
        _save(keep)
    return True


def _update_credentials(account_id: str, credentials: dict) -> None:
    with _lock:
        accounts = _load()
        for acc in accounts:
            if acc.get("id") == account_id:
                acc["credentials"] = credentials
        _save(accounts)


# --------------------------------------------------------------------------- #
# YouTube — OAuth connect (multiple channels)
# --------------------------------------------------------------------------- #
def youtube_configured() -> bool:
    if config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET:
        return True
    return Path(config.GOOGLE_CLIENT_SECRET_FILE).is_file()


def _yt_flow():
    from google_auth_oauthlib.flow import Flow

    if config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET:
        client_config = {
            "installed": {
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.SOCIAL_REDIRECT_URI],
            }
        }
    else:
        client_config = str(config.GOOGLE_CLIENT_SECRET_FILE)
    # client type ("installed" vs "web") is inferred from the config key —
    # passing it explicitly leaks into the OAuth2Session constructor.
    flow = Flow.from_client_config(client_config, scopes=[YT_SCOPE, YT_READ_SCOPE])
    flow.redirect_uri = config.SOCIAL_REDIRECT_URI
    return flow


def _pending_age(entry) -> float:
    """Seconds since the state was created (accepts the legacy float form)."""
    created = float(entry.get("t", 0.0)) if isinstance(entry, dict) else float(entry)
    return time.time() - created


def begin_youtube_auth() -> tuple[str, str]:
    """Start an OAuth round: returns (state, consent-screen URL)."""
    if not youtube_configured():
        raise RuntimeError(
            "YouTube OAuth is not configured yet — set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env (or drop client_secret.json in the "
            "project root) and restart the API."
        )
    flow = _yt_flow()
    state = secrets.token_urlsafe(16)
    # authorization_url auto-generates the PKCE code_verifier and puts its
    # S256 challenge in the URL — keep the verifier for the token exchange.
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
        state=state,
    )
    with _lock:
        for old in [s for s, e in list(_pending.items()) if _pending_age(e) > 600]:
            _pending.pop(old, None)
        _pending[state] = {"t": time.time(), "verifier": flow.code_verifier}
    return state, url


def complete_youtube_auth(state: str, code: str) -> dict:
    """Finish the round: exchange the code, read the channel, store it."""
    if not state or not code:
        raise RuntimeError("missing sign-in code — click Connect again")
    with _lock:
        entry = _pending.pop(state, None)
    if entry is None or _pending_age(entry) > 600:
        raise RuntimeError("the sign-in link expired or was already used — click Connect again")
    flow = _yt_flow()
    verifier = entry.get("verifier") if isinstance(entry, dict) else None
    if verifier:
        flow.code_verifier = verifier  # PKCE: must match the challenged one
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - oauthlib raises typed errors
        # (invalid_grant, state mismatch, …) — surface them, never a bare 500
        raise RuntimeError(f"Google could not complete the sign-in: {exc}") from exc
    creds = flow.credentials
    handle, label, avatar = _yt_channel_info(creds.token)
    return add_account(
        "youtube",
        label=label or handle or "YouTube channel",
        handle=handle,
        avatar=avatar,
        credentials=json.loads(creds.to_json()),
    )


def _yt_channel_info(token: str) -> tuple[str, str, str]:
    """(handle, channel title, thumbnail) for the just-authorized channel."""
    try:
        resp = httpx.get(
            YT_CHANNEL_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
        items = resp.json().get("items", []) if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return "", "", ""
    if not items:
        return "", "", ""
    sn = items[0].get("snippet", {})
    thumbs = (sn.get("thumbnails") or {}).get("default") or {}
    return sn.get("customUrl") or "", sn.get("title") or "", thumbs.get("url") or ""


def _yt_access_token(acc: dict) -> str:
    """Valid access token for a stored account, refreshing when expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    info = dict(acc.get("credentials") or {})
    creds = Credentials.from_authorized_user_info(
        info, scopes=[YT_SCOPE, YT_READ_SCOPE]
    )
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise RuntimeError(
                "YouTube sign-in expired — reconnect this account on the Accounts page"
            )
        creds.refresh(Request())
        _update_credentials(acc["id"], json.loads(creds.to_json()))
    if not creds.token:
        raise RuntimeError("YouTube sign-in expired — reconnect this account")
    return creds.token


# --------------------------------------------------------------------------- #
# YouTube — resumable upload
# --------------------------------------------------------------------------- #
def _yt_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    if len(t) <= IG_TITLE_MAX:
        return t
    return t[: IG_TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def _yt_tags(tags: list[str]) -> list[str]:
    """Trim the tag list until it fits YouTube's 500-char box."""
    out: list[str] = []
    total = 0
    for tag in tags:
        text = str(tag).strip().strip(",").strip()
        if not text or text in out:
            continue
        cost = len(text) + (1 if out else 0)  # comma separator
        if total + cost > YT_TAGS_MAX_TOTAL:
            break
        out.append(text)
        total += cost
    return out


def _error_message(resp, fallback: str) -> str:
    try:
        data = resp.json()
        err = (data.get("error") or {}).get("message")
        if err:
            return err
    except (ValueError, AttributeError):
        pass
    text = (getattr(resp, "text", "") or "").strip()
    return f"{fallback} (HTTP {resp.status_code}{': ' + text[:200] if text else ''})"


def upload_youtube(acc: dict, video_path: Path, *, title: str, description: str = "",
                   tags: Optional[list[str]] = None, privacy: str = "") -> dict:
    """Resumable upload of a local file to one connected channel."""
    token = _yt_access_token(acc)
    size = video_path.stat().st_size
    resource = {
        "snippet": {
            "title": _yt_title(title) or video_path.stem,
            "description": str(description or ""),
            "tags": _yt_tags(list(tags or [])),
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": (privacy or config.YT_PRIVACY_STATUS).lower(),
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }
    init = httpx.post(
        YT_UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=resource,
        timeout=HTTP_TIMEOUT,
    )
    if init.status_code not in (200, 201):
        raise RuntimeError(_error_message(init, "YouTube refused the upload session"))
    session_url = init.headers.get("location") or init.headers.get("Location")
    if not session_url:
        raise RuntimeError("YouTube did not return an upload session URL")

    put = httpx.put(
        session_url,
        content=video_path.read_bytes(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "video/mp4",
        },
        timeout=UPLOAD_TIMEOUT,
    )
    if put.status_code not in (200, 201):
        raise RuntimeError(_error_message(put, "YouTube upload failed"))
    try:
        video_id = put.json().get("id", "")
    except ValueError:
        video_id = ""
    if not video_id:
        raise RuntimeError("YouTube upload finished but returned no video id")
    return {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "studio": f"https://studio.youtube.com/video/{video_id}/edit",
    }


# --------------------------------------------------------------------------- #
# Instagram — connect (one-click Facebook sign-in, or pasted token)
# --------------------------------------------------------------------------- #
def _ig_base() -> str:
    return f"https://graph.facebook.com/{config.IG_GRAPH_VERSION}"


def _ig_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _ig_error(data: dict, fallback: str, status_code: int = 0) -> str:
    err = data.get("error")
    if isinstance(err, dict) and err.get("message"):
        code = err.get("code")
        sub = err.get("error_user_msg") or err.get("error_user_title") or ""
        parts = [str(err["message"])]
        if code:
            parts.append(f"(code {code})")
        if sub:
            parts.append(str(sub))
        return " ".join(parts)
    if data.get("status_code"):
        return f"{fallback}: container status {data['status_code']}"
    return fallback + (f" (HTTP {status_code})" if status_code else "")


def instagram_oauth_configured() -> bool:
    """True when the one-time Facebook app is set up (App ID + Secret)."""
    return bool(config.IG_OAUTH and config.IG_APP_ID and config.IG_APP_SECRET)


def begin_instagram_auth() -> tuple[str, str]:
    """Start a Facebook OAuth round: returns (state, consent-screen URL)."""
    if not instagram_oauth_configured():
        raise RuntimeError(
            "Instagram sign-in is not configured yet — set IG_APP_ID and "
            "IG_APP_SECRET in .env (one-time Facebook app setup, see the "
            "Accounts page) and restart the API."
        )
    state = secrets.token_urlsafe(16)
    url = IG_AUTH_URL + "?" + urlencode({
        "client_id": config.IG_APP_ID,
        "redirect_uri": config.IG_REDIRECT_URI,
        "scope": IG_OAUTH_SCOPES,
        "response_type": "code",
        "state": state,
    })
    with _lock:
        for old in [s for s, e in list(_pending.items()) if _pending_age(e) > 600]:
            _pending.pop(old, None)
        _pending[state] = {"t": time.time()}
    return state, url


def complete_instagram_auth(state: str, code: str) -> list[dict]:
    """Finish the round: exchange the code, link the professional IG accounts.

    The Facebook token belongs to the *user* — the professional Instagram
    accounts are found through the Pages that login manages
    (``/me/accounts`` → ``instagram_business_account``). Every account found
    is linked, so one click can cover several Instagram accounts.
    """
    if not state or not code:
        raise RuntimeError("missing sign-in code — click Connect again")
    with _lock:
        entry = _pending.pop(state, None)
    if entry is None or _pending_age(entry) > 600:
        raise RuntimeError("the sign-in link expired or was already used — click Connect again")
    data = _ig_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.IG_REDIRECT_URI,
    })
    short = str(data.get("access_token") or "")
    if not short:
        raise RuntimeError("Instagram did not return an access token")
    # short-lived (~1 hour) → long-lived (~60 days); degrade to the short
    # one if the exchange fails — the account still links and can refresh.
    long_lived = _ig_long_lived(short)
    token = str(long_lived.get("access_token") or short)
    expires_in = int(long_lived.get("expires_in") or 0)
    found = _ig_discover_accounts(token)
    if not found:
        raise RuntimeError(
            "no professional Instagram account is linked to this Facebook "
            "login — switch the account to Business/Creator and connect it "
            "to a Facebook Page you manage, then try again"
        )
    return [
        add_account(
            "instagram",
            label=(page_name or username or ig_id),
            handle=(f"@{username}" if username else ig_id),
            credentials=_ig_creds(token, ig_id, expires_in),
        )
        for ig_id, username, page_name in found
    ]


def _ig_token_request(params: dict) -> dict:
    """POST to the Graph OAuth token endpoint (code exchange)."""
    payload = {
        "client_id": config.IG_APP_ID,
        "client_secret": config.IG_APP_SECRET,
        **params,
    }
    try:
        resp = httpx.post(IG_TOKEN_URL, data=payload, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Instagram: {exc}") from exc
    data = _ig_json(resp)
    if resp.status_code != 200 or not data.get("access_token"):
        raise RuntimeError(_ig_error(
            data, "Instagram could not complete the sign-in", resp.status_code
        ))
    return data


def _ig_long_lived(token: str) -> dict:
    """``fb_exchange_token``: any valid token → a fresh long-lived (~60 d) one."""
    if not token or not (config.IG_APP_ID and config.IG_APP_SECRET):
        return {}
    try:
        resp = httpx.get(
            IG_TOKEN_URL,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": config.IG_APP_ID,
                "client_secret": config.IG_APP_SECRET,
                "fb_exchange_token": token,
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        return {}
    data = _ig_json(resp)
    if resp.status_code != 200 or not data.get("access_token"):
        return {}
    return {
        "access_token": str(data["access_token"]),
        "expires_in": int(data.get("expires_in") or 0),
    }


def _ig_discover_accounts(token: str) -> list[tuple[str, str, str]]:
    """``[(ig_user_id, username, page_name)]`` this login can publish reels to."""
    try:
        resp = httpx.get(
            f"{_ig_base()}/me/accounts",
            params={
                "fields": "name,instagram_business_account{id,username}",
                "access_token": token,
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        return []
    found: list[tuple[str, str, str]] = []
    for page in _ig_json(resp).get("data") or []:
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            found.append((
                str(ig["id"]),
                str(ig.get("username") or ""),
                str(page.get("name") or ""),
            ))
    return found


def _ig_creds(token: str, user_id: str, expires_in: int = 0) -> dict:
    return {
        "access_token": token,
        "user_id": user_id,
        "obtained_at": time.time(),
        # 0 = lifetime unknown (e.g. a pasted token) — only an actual
        # expires_in from Meta enables the automatic refresh below.
        "expires_in": int(expires_in or 0),
    }


def _ig_token_info(acc: dict) -> dict:
    """Token-health view for the UI — statuses only, never token values."""
    creds = acc.get("credentials") or {}
    token = str(creds.get("access_token") or "")
    obtained = float(creds.get("obtained_at") or 0.0)
    expires_in = int(creds.get("expires_in") or 0)
    info = {
        "status": "missing",      # missing | unknown | ok | expiring | expired
        "days_left": -1.0,
        "expires_at": "",
        "refreshable": bool(config.IG_APP_ID and config.IG_APP_SECRET),
    }
    if not token:
        return info
    if obtained <= 0 or expires_in <= 0:
        info["status"] = "unknown"   # pasted token — Meta will say on post
        return info
    expires_at = obtained + expires_in
    days = (expires_at - time.time()) / 86400.0
    info["days_left"] = round(days, 1)
    info["expires_at"] = datetime.fromtimestamp(
        expires_at, timezone.utc
    ).isoformat(timespec="seconds")
    if days <= 0:
        info["status"] = "expired"
    elif days < IG_TOKEN_WARN_DAYS:
        info["status"] = "expiring"
    else:
        info["status"] = "ok"
    return info


def reconnect_instagram(account_id: str) -> dict:
    """Exchange the stored token for a fresh 60-day one; returns its health."""
    acc = get_account(account_id)
    if acc is None:
        raise LookupError("account not found — link it again on the Accounts page")
    if acc.get("platform") != "instagram":
        raise ValueError("only Instagram accounts carry a token to refresh")
    creds = dict(acc.get("credentials") or {})
    fresh = _ig_long_lived(str(creds.get("access_token") or ""))
    if not fresh:
        raise RuntimeError(
            "Instagram could not refresh this token — set IG_APP_ID / "
            "IG_APP_SECRET in .env (one-time Facebook app setup) or paste "
            "a new token"
        )
    merged = {**creds, **fresh, "obtained_at": time.time()}
    _update_credentials(account_id, merged)
    return _ig_token_info({"credentials": merged})


def _ig_access_token(acc: dict) -> str:
    """Valid access token for a stored account; refreshes when it's stale."""
    creds = dict(acc.get("credentials") or {})
    token = str(creds.get("access_token") or "")
    if not token:
        raise RuntimeError("Instagram account is missing its token — reconnect it")
    status = _ig_token_info(acc)["status"]
    if status in ("ok", "unknown"):
        return token
    if status == "missing":
        raise RuntimeError("Instagram account is missing its token — reconnect it")
    # expiring (≤7 days left) or already expired — exchange for a fresh one
    fresh = _ig_long_lived(token)
    if fresh:
        _update_credentials(acc["id"], {**creds, **fresh, "obtained_at": time.time()})
        return str(fresh["access_token"])
    if status == "expired":
        raise RuntimeError(
            "Instagram sign-in expired — reconnect this account on the Accounts page"
        )
    return token


def connect_instagram(access_token: str, ig_user_id: str, *, label: str = "") -> dict:
    """Validate a pasted token against the Graph API, then store the account."""
    access_token = str(access_token or "").strip()
    ig_user_id = str(ig_user_id or "").strip()
    if not access_token or not ig_user_id:
        raise ValueError("both an access token and the Instagram user id are required")
    try:
        resp = httpx.get(
            f"{_ig_base()}/{ig_user_id}",
            params={"fields": "id,username,name", "access_token": access_token},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Instagram: {exc}") from exc
    data = _ig_json(resp)
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(_ig_error(
            data, "Instagram rejected the token — check it has "
                  "instagram_content_publish and the account is professional", resp.status_code
        ))
    username = str(data.get("username") or ig_user_id)
    return add_account(
        "instagram",
        label=(label or data.get("name") or username).strip(),
        handle=f"@{username}",
        # lifetime unknown for pasted tokens (0) — extendable with the
        # "Refresh token" button once IG_APP_ID / IG_APP_SECRET are set.
        credentials=_ig_creds(access_token, ig_user_id, 0),
    )


# --------------------------------------------------------------------------- #
# Instagram — REELS publish (local bytes, no public URL needed)
# --------------------------------------------------------------------------- #
def publish_instagram(acc: dict, video_path: Path, *, caption: str,
                      on_status=None) -> dict:
    creds = acc.get("credentials") or {}
    user_id = str(creds.get("user_id") or "")
    if not user_id:
        raise RuntimeError("Instagram account is missing its user id — reconnect it")
    token = _ig_access_token(acc)   # auto-refreshes first when stale/expired
    caption = str(caption or "")[:IG_CAPTION_MAX]
    size = video_path.stat().st_size

    # 1) create a resumable REELS container
    try:
        resp = httpx.post(
            f"{_ig_base()}/{user_id}/media",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true",
                "access_token": token,
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Instagram upload start failed: {exc}") from exc
    data = _ig_json(resp)
    container, upload_uri = data.get("id"), data.get("uri")
    if resp.status_code != 200 or not container or not upload_uri:
        raise RuntimeError(_ig_error(data, "Instagram refused the upload session", resp.status_code))

    # 2) upload the file bytes straight from disk
    try:
        up = httpx.post(
            upload_uri,
            content=video_path.read_bytes(),
            headers={
                "Authorization": f"Bearer {token}",
                "offset": "0",
                "file_size": str(size),
            },
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Instagram upload failed: {exc}") from exc
    if up.status_code not in (200, 201):
        raise RuntimeError(_ig_error(_ig_json(up), "Instagram upload failed", up.status_code))

    # 3) wait for Meta to finish processing the container
    if on_status:
        on_status("waiting for Instagram to process the reel…")
    deadline = time.time() + config.IG_POLL_TIMEOUT
    while time.time() < deadline:
        try:
            st = _ig_json(httpx.get(
                f"{_ig_base()}/{container}",
                params={"fields": "status_code", "access_token": token},
                timeout=HTTP_TIMEOUT,
            ))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Instagram status check failed: {exc}") from exc
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                _ig_error(st, f"Instagram processing {str(code).lower()}")
            )
        time.sleep(config.IG_POLL_SECONDS)
    else:
        raise RuntimeError(
            f"Instagram was still processing after {int(config.IG_POLL_TIMEOUT)}s — "
            "try posting again (the container may still publish from a retry)"
        )

    # 4) publish the container
    try:
        pub = _ig_json(httpx.post(
            f"{_ig_base()}/{user_id}/media_publish",
            data={"creation_id": container, "access_token": token},
            timeout=HTTP_TIMEOUT,
        ))
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Instagram publish failed: {exc}") from exc
    media_id = pub.get("id")
    if not media_id:
        raise RuntimeError(_ig_error(pub, "Instagram publish failed", 400))

    permalink = _ig_permalink(str(media_id), token)
    if on_status:
        on_status("published")
    return {"id": str(media_id), "url": permalink, "container": str(container)}


def _ig_permalink(media_id: str, token: str) -> str:
    fallback = f"https://www.instagram.com/reel/{media_id}/"
    try:
        resp = httpx.get(
            f"{_ig_base()}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=HTTP_TIMEOUT,
        )
        link = _ig_json(resp).get("permalink")
        return str(link) if link else fallback
    except (httpx.HTTPError, ValueError):
        return fallback


# --------------------------------------------------------------------------- #
# TikTok — OAuth connect (Login Kit, multiple accounts)
# --------------------------------------------------------------------------- #
def tiktok_configured() -> bool:
    return bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET)


def begin_tiktok_auth() -> tuple[str, str]:
    """Start a Login Kit round: returns (state, consent-screen URL)."""
    if not tiktok_configured():
        raise RuntimeError(
            "TikTok OAuth is not configured yet — set TIKTOK_CLIENT_KEY and "
            "TIKTOK_CLIENT_SECRET in .env and restart the API."
        )
    state = secrets.token_urlsafe(16)
    # PKCE (RFC 7636): TikTok rejects the consent screen without a
    # code_challenge, and the token exchange must send the matching
    # code_verifier — so keep the verifier alongside the pending state.
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    url = TT_AUTHORIZE_URL + "?" + urlencode({
        "client_key": config.TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": TT_SCOPE,
        "redirect_uri": config.TIKTOK_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    with _lock:
        for old in [s for s, e in list(_pending.items()) if _pending_age(e) > 600]:
            _pending.pop(old, None)
        _pending[state] = {"t": time.time(), "verifier": verifier}
    return state, url


def complete_tiktok_auth(state: str, code: str) -> dict:
    """Finish the round: exchange the code, read the profile, store it."""
    if not state or not code:
        raise RuntimeError("missing sign-in code — click Connect again")
    with _lock:
        entry = _pending.pop(state, None)
    if entry is None or _pending_age(entry) > 600:
        raise RuntimeError("the sign-in link expired or was already used — click Connect again")
    fields = {"grant_type": "authorization_code", "code": code}
    verifier = entry.get("verifier") if isinstance(entry, dict) else None
    if verifier:
        fields["code_verifier"] = verifier  # PKCE: must match code_challenge
    data = _tt_exchange(fields)
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError("TikTok did not return an access token")
    handle, label, avatar = _tt_user_info(token)
    return add_account(
        "tiktok",
        label=label or handle or "TikTok account",
        handle=handle,
        avatar=avatar,
        credentials=_tt_creds(data),
    )


def _tt_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _tt_ok(data: dict) -> bool:
    """TikTok wraps errors as {"error": {code, description}} — code 0 = ok."""
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("code") in (None, "", 0, "0")
    return data.get("error_code") in (None, "", 0, "0", "")


def _tt_error(data: dict, fallback: str, status_code: int = 0) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        msg = str(err.get("description") or err.get("message") or "").strip()
        code = err.get("code")
        if msg and code not in (None, "", 0, "0"):
            return f"{msg} (code {code})"
        if msg:
            return msg
        if code not in (None, "", 0, "0"):
            return f"{fallback} (code {code})"
    if data.get("error_code") not in (None, "", 0, "0"):
        detail = data.get("error_msg") or data.get("error_description") or ""
        return f"{fallback}: {detail}".rstrip()
    return fallback + (f" (HTTP {status_code})" if status_code else "")


def _tt_exchange(fields: dict) -> dict:
    """POST to TikTok's token endpoint (code exchange / refresh)."""
    payload = {
        "client_key": config.TIKTOK_CLIENT_KEY,
        "client_secret": config.TIKTOK_CLIENT_SECRET,
        **fields,
    }
    try:
        resp = httpx.post(TT_TOKEN_URL, data=payload, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach TikTok: {exc}") from exc
    data = _tt_json(resp)
    if resp.status_code != 200 or not _tt_ok(data):
        raise RuntimeError(_tt_error(
            data, "TikTok could not complete the sign-in", resp.status_code
        ))
    return data


def _tt_creds(token_data: dict) -> dict:
    return {
        "access_token": str(token_data.get("access_token") or ""),
        "refresh_token": str(token_data.get("refresh_token") or ""),
        "expires_in": int(token_data.get("expires_in") or 0),
        "obtained_at": time.time(),
        "open_id": str(token_data.get("open_id") or ""),
        "scope": str(token_data.get("scope") or ""),
    }


def _tt_user_info(token: str) -> tuple[str, str, str]:
    """(handle, label, thumbnail) for the just-authorized TikTok account."""
    try:
        resp = httpx.get(
            TT_USER_URL,
            params={"fields": "open_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    except (httpx.HTTPError, ValueError):
        return "", "", ""
    data = _tt_json(resp)
    if not _tt_ok(data):
        return "", "", ""
    user = (data.get("data") or {}).get("user") or {}
    name = str(user.get("display_name") or "").strip()
    avatar = str(user.get("avatar_url") or "").strip()
    # TikTok's basic info has no @username — fall back to the display name.
    return name, name, avatar


def _tiktok_access_token(acc: dict) -> str:
    """Valid access token for a stored account, refreshing when expired."""
    creds = dict(acc.get("credentials") or {})
    token = str(creds.get("access_token") or "")
    obtained = float(creds.get("obtained_at") or 0.0)
    expires_in = int(creds.get("expires_in") or 0)
    if token and (expires_in <= 0 or time.time() < obtained + expires_in - 60):
        return token
    refresh = str(creds.get("refresh_token") or "")
    if not refresh:
        raise RuntimeError(
            "TikTok sign-in expired — reconnect this account on the Accounts page"
        )
    data = _tt_exchange({"grant_type": "refresh_token", "refresh_token": refresh})
    merged = {**creds, **_tt_creds(data)}
    if not merged.get("access_token"):
        raise RuntimeError("TikTok sign-in expired — reconnect this account")
    _update_credentials(acc["id"], merged)
    return merged["access_token"]


# --------------------------------------------------------------------------- #
# TikTok — Direct Post (FILE_UPLOAD)
# --------------------------------------------------------------------------- #
def _tt_title(title: str, description: str = "", tags=None) -> str:
    """TikTok has one caption field: title + description + #hashtags, ≤2200."""
    parts = [" ".join(str(title or "").split())]
    desc = " ".join(str(description or "").split())
    if desc and desc.lower() not in parts[0].lower():
        parts.append(desc)
    hash_tags: list[str] = []
    for tag in list(tags or []):
        word = str(tag).strip().strip(",").strip()
        if not word:
            continue
        cleaned = "".join(ch for ch in word if ch.isalnum() or ch in "_ ").replace(" ", "")
        if cleaned and f"#{cleaned}" not in hash_tags:
            hash_tags.append(f"#{cleaned}")
    if hash_tags:
        parts.append(" ".join(hash_tags))
    text = "\n\n".join(p for p in parts if p)
    if len(text) <= TT_TITLE_MAX:
        return text
    return text[: TT_TITLE_MAX - 1].rstrip() + "…"


def _tt_best_privacy(token: str, wanted: str) -> str:
    """Pick a privacy level this account actually allows.

    An unaudited TikTok app can only post to its own account as a private
    draft (SELF_ONLY); asking creator_info for the allowed list lets us
    downgrade instead of having the upload rejected outright.
    """
    try:
        resp = httpx.post(
            TT_CREATOR_INFO_URL,
            json={},
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
        data = _tt_json(resp)
        if resp.status_code != 200 or not _tt_ok(data):
            return wanted
        allowed = [
            str(p) for p in ((data.get("data") or {}).get("privacy_level_options") or [])
        ]
        if not allowed:
            return wanted
        if wanted in allowed:
            return wanted
        for pref in TT_PRIVACY_LEVELS:
            if pref in allowed:
                return pref
        return allowed[0]
    except (httpx.HTTPError, ValueError):
        return wanted


def upload_tiktok(acc: dict, video_path: Path, *, title: str, description: str = "",
                  tags: Optional[list[str]] = None, privacy: str = "",
                  on_status=None) -> dict:
    """Direct Post a local clip to a connected TikTok account."""
    token = _tiktok_access_token(acc)
    size = video_path.stat().st_size
    if size <= 0:
        raise RuntimeError(f"clip file is empty: {video_path.name}")
    caption = _tt_title(title or video_path.stem, description, tags)
    privacy_level = (privacy or config.TIKTOK_PRIVACY or "PUBLIC_TO_EVERYONE").upper()
    if privacy_level not in TT_PRIVACY_LEVELS:
        privacy_level = "PUBLIC_TO_EVERYONE"
    privacy_level = _tt_best_privacy(token, privacy_level)

    chunk = min(TT_CHUNK_BYTES, size)
    total_chunks = (size + chunk - 1) // chunk
    init = _tt_post_json(TT_VIDEO_INIT_URL, {
        "post_info": {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk,
            "total_chunk_count": total_chunks,
        },
    }, token)
    publish_id = str(init.get("publish_id") or "")
    upload_url = str(init.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise RuntimeError(_tt_error(init, "TikTok did not return an upload session", 400))

    # Upload the bytes in chunks with a Content-Range header per chunk.
    if on_status:
        on_status("uploading to TikTok…")
    try:
        with open(video_path, "rb") as fh:
            offset = 0
            for _ in range(total_chunks):
                data = fh.read(chunk)
                if not data:
                    break
                end = offset + len(data) - 1
                put = httpx.put(
                    upload_url,
                    content=data,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    timeout=UPLOAD_TIMEOUT,
                )
                if put.status_code not in (200, 201, 206):
                    raise RuntimeError(_tt_error(
                        _tt_json(put), "TikTok upload failed", put.status_code
                    ))
                offset = end + 1
    except httpx.HTTPError as exc:
        raise RuntimeError(f"TikTok upload failed: {exc}") from exc

    # Poll until TikTok finishes publishing (init alone does not mean live).
    if on_status:
        on_status("waiting for TikTok to publish…")
    deadline = time.time() + config.TIKTOK_POLL_TIMEOUT
    while time.time() < deadline:
        st = _tt_post_json(TT_STATUS_URL, {"publish_id": publish_id}, token)
        status = str((st.get("data") or {}).get("status") or st.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            break
        if status == "FAILED":
            raise RuntimeError(_tt_error(st, "TikTok publish failed"))
        time.sleep(config.TIKTOK_POLL_SECONDS)
    else:
        raise RuntimeError(
            f"TikTok was still processing after {int(config.TIKTOK_POLL_TIMEOUT)}s — "
            "check your TikTok uploads (the video may still publish from a retry)"
        )

    if on_status:
        on_status("published")
    return {
        "id": publish_id,
        "publish_id": publish_id,
        "url": "",
        "privacy_level": privacy_level,
    }


def _tt_post_json(url: str, body: dict, token: str = "") -> dict:
    """POST JSON to a TikTok v2 endpoint; raises on transport / API errors."""
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach TikTok: {exc}") from exc
    data = _tt_json(resp)
    if resp.status_code != 200 or not _tt_ok(data):
        raise RuntimeError(_tt_error(data, "TikTok request failed", resp.status_code))
    return data


# --------------------------------------------------------------------------- #
# Clip defaults + dispatcher
# --------------------------------------------------------------------------- #
def publish_defaults(video_path: Path) -> dict:
    """Title/description/caption/tags for a clip from its own publish JSON.

    ``clip_01.mp4`` / ``clip_01_hook.mp4`` → ``clip_01_publish.json``.
    """
    stem = video_path.stem
    if stem.endswith("_hook"):
        stem = stem[: -len("_hook")]
    path = video_path.parent / f"{stem}_publish.json"
    empty = {"title": "", "description": "", "caption": "", "tags": []}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    ig = data.get("instagram") or {}
    yt = data.get("youtube") or {}
    return {
        "title": str(yt.get("title") or ig.get("title") or ""),
        "description": str(yt.get("description") or ""),
        "caption": str(ig.get("caption") or ""),
        "tags": [str(t) for t in (yt.get("tags") or [])],
    }


def post_clip(account_id: str, video_path: Path, *, title: str = "",
              description: str = "", caption: str = "",
              tags: Optional[list[str]] = None, privacy: str = "") -> dict:
    """Post one clip file to one connected account (platform dispatcher)."""
    acc = get_account(account_id)
    if acc is None:
        raise LookupError("account not found — link it again on the Accounts page")
    if not video_path.is_file():
        raise FileNotFoundError(f"clip file not found: {video_path.name}")

    defaults = publish_defaults(video_path)
    title = title or defaults["title"]
    description = description or defaults["description"]
    caption = caption or defaults["caption"]
    tags = list(tags) if tags else defaults["tags"]

    if acc["platform"] == "youtube":
        result = upload_youtube(
            acc, video_path, title=title, description=description,
            tags=tags, privacy=privacy,
        )
    elif acc["platform"] == "instagram":
        result = publish_instagram(acc, video_path, caption=caption or title)
    elif acc["platform"] == "tiktok":
        # TikTok has one caption field — prefer the IG caption (it already
        # carries hashtags), else the title; fold in description + tags.
        result = upload_tiktok(
            acc, video_path, title=caption or title, description=description,
            tags=tags, privacy=privacy,
        )
    else:  # pragma: no cover - store only accepts the three platforms
        raise RuntimeError(f"unsupported platform: {acc['platform']}")
    return {"account": public_account(acc), "platform": acc["platform"], **result}
