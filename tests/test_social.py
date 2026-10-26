"""Linked social accounts + direct clip posting (YouTube / Instagram)."""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import config, social
from app.main import app, manager

client = TestClient(app)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status_code: int = 200, data: dict | None = None,
                 headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = text
        self.is_success = status_code < 400

    def json(self):
        return self._data


class FakeHTTP:
    """Scripted httpx stand-in: pops responses in call order, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _next(self, url: str, kwargs: dict) -> FakeResp:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected HTTP call: {url}")
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self._next(url, {"method": "GET", **kw})

    def post(self, url, **kw):
        return self._next(url, {"method": "POST", **kw})

    def put(self, url, **kw):
        return self._next(url, {"method": "PUT", **kw})

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


@pytest.fixture()
def social_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_DIR", tmp_path / "social")
    return tmp_path / "social"


@pytest.fixture()
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    return manager.create("upload", "source.mp4")


def _clip_with_metadata(job) -> "tuple[object, dict]":
    path = job.workdir / "clip_01.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake video bytes")
    meta = {
        "instagram": {"title": "IG title line", "caption": "IG caption\n\n#clip",
                      "hashtags": ["#clip"]},
        "youtube": {"title": "YT title that sells the clip", "description": "desc body",
                    "tags": ["clip", "shorts"], "hashtags": ["#shorts"]},
    }
    (job.workdir / "clip_01_publish.json").write_text(json.dumps(meta), encoding="utf-8")
    return path, meta


# --------------------------------------------------------------------------- #
# Account store
# --------------------------------------------------------------------------- #
def test_account_store_roundtrip_hides_credentials(social_dir):
    created = social.add_account(
        "youtube", label="Main", handle="@mainclips",
        credentials={"token": "secret-token"},
    )
    assert created["handle"] == "@mainclips"
    assert "credentials" not in created

    listed = social.list_accounts()
    assert len(listed) == 1
    assert "credentials" not in listed[0]

    full = social.get_account(created["id"])
    assert full["credentials"]["token"] == "secret-token"

    # stored file exists, owner-only, and holds the token
    stored = (social_dir / "accounts.json")
    assert stored.is_file()
    assert "secret-token" in stored.read_text(encoding="utf-8")

    assert social.remove_account(created["id"]) is True
    assert social.list_accounts() == []
    assert social.remove_account(created["id"]) is False
    assert social.get_account(created["id"]) is None


def test_relinking_same_handle_replaces_token(social_dir):
    first = social.add_account("instagram", handle="@one", credentials={"access_token": "A"})
    second = social.add_account("instagram", handle="@one", credentials={"access_token": "B"})
    accounts = social.list_accounts()
    assert len(accounts) == 1
    assert second["id"] != first["id"]
    assert social.get_account(second["id"])["credentials"]["access_token"] == "B"


# --------------------------------------------------------------------------- #
# YouTube — title/tag helpers + resumable upload
# --------------------------------------------------------------------------- #
def test_youtube_title_and_tag_limits():
    assert len(social._yt_title("word " * 40)) <= 100
    assert social._yt_title("A clean short title") == "A clean short title"

    tags = social._yt_tags(["one", "two", "three"])
    assert tags == ["one", "two", "three"]
    # a flood of long tags is trimmed to the 500-char box
    huge = [f"verylongtagnumber{i:02d} paddingwords" for i in range(40)]
    trimmed = social._yt_tags(huge)
    total = sum(len(t) for t in trimmed) + max(0, len(trimmed) - 1)
    assert 0 < total <= 500
    assert trimmed[0] == huge[0]


def test_youtube_upload_resumable_flow(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_yt_access_token", lambda acc: "tok-42")
    fake = FakeHTTP([
        FakeResp(200, headers={"location": "https://upload.example/session/1"}),
        FakeResp(200, {"id": "vid99"}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "put", fake.put)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789" * 10)

    acc = {"id": "yt_x", "credentials": {}}
    out = social.upload_youtube(
        acc, video, title="T" * 150, description="d", tags=["a", "b"],
    )
    assert out["url"] == "https://www.youtube.com/watch?v=vid99"

    init, put = fake.calls
    assert init["method"] == "POST"
    assert init["params"]["uploadType"] == "resumable"
    assert init["params"]["part"] == "snippet,status"
    resource = init["json"]
    assert len(resource["snippet"]["title"]) <= 100
    assert resource["snippet"]["tags"] == ["a", "b"]
    assert resource["status"]["privacyStatus"] in {"public", "unlisted", "private"}
    assert init["headers"]["Authorization"] == "Bearer tok-42"
    assert init["headers"]["X-Upload-Content-Length"] == "100"
    assert put["url"] == "https://upload.example/session/1"
    assert put["content"] == video.read_bytes()


def test_youtube_upload_surfaces_api_error(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_yt_access_token", lambda acc: "tok")
    fake = FakeHTTP([
        FakeResp(400, {"error": {"message": "quotaExceeded: daily upload quota spent"}}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"xx")
    with pytest.raises(RuntimeError, match="quotaExceeded"):
        social.upload_youtube({"id": "yt_x", "credentials": {}}, video, title="t")


# --------------------------------------------------------------------------- #
# YouTube — OAuth states
# --------------------------------------------------------------------------- #
def test_youtube_auth_requires_configuration(social_dir, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET_FILE", "/nonexistent/client.json")
    assert social.youtube_configured() is False
    with pytest.raises(RuntimeError, match="not configured"):
        social.begin_youtube_auth()


def test_youtube_auth_url_and_state_handling(social_dir, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")

    state, url = social.begin_youtube_auth()
    assert state
    assert "accounts.google.com" in url
    assert state in url
    # Google enforces PKCE for new clients: the consent link must carry the
    # S256 challenge, and the stored state must keep the matching verifier
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert isinstance(social._pending[state], dict)
    assert social._pending[state]["verifier"]

    # unknown / empty state is rejected before any token exchange
    with pytest.raises(RuntimeError, match="expired|missing"):
        social.complete_youtube_auth("bogus-state", "code")
    with pytest.raises(RuntimeError, match="missing"):
        social.complete_youtube_auth(state, "")


class _FakeFlowCreds:
    token = "access-tok"

    def to_json(self):
        return json.dumps({"token": "access-tok", "refresh_token": "r",
                           "expiry": "2030-01-01T00:00:00Z"})


class _FakeFlow:
    """Mimics google-auth-oauthlib Flow: auto-generates the PKCE verifier
    in authorization_url and would send it in fetch_token."""

    def __init__(self):
        self.redirect_uri = None
        self.code_verifier = None
        self.fetched = None
        self.credentials = _FakeFlowCreds()

    def authorization_url(self, **kwargs):
        if self.code_verifier is None:
            self.code_verifier = "v" * 64
        return (f"https://accounts.google.com/o/oauth2/auth?state={kwargs.get('state')}",
                kwargs.get("state"))

    def fetch_token(self, **kwargs):
        self.fetched = kwargs


class _BrokenFlow(_FakeFlow):
    def fetch_token(self, **kwargs):
        raise Exception("(invalid_grant) Missing code verifier.")


def test_youtube_pkce_verifier_survives_the_browser_roundtrip(social_dir, monkeypatch):
    """begin and complete use *different* Flow instances — the verifier
    generated for the consent link must be restored for the token exchange,
    or Google rejects with invalid_grant/missing code verifier."""
    flows = []

    def fake_flow():
        f = _FakeFlow()
        flows.append(f)
        return f

    monkeypatch.setattr(social, "_yt_flow", fake_flow)
    monkeypatch.setattr(social, "_yt_channel_info", lambda token: ("@ch", "Ch", ""))

    state, _ = social.begin_youtube_auth()
    assert flows[0].code_verifier  # generated while building the consent link

    account = social.complete_youtube_auth(state, "the-code")
    assert account["handle"] == "@ch"
    assert len(flows) == 2  # a fresh Flow instance handles the exchange…
    assert flows[1].code_verifier == flows[0].code_verifier  # …same verifier
    assert flows[1].fetched == {"code": "the-code"}


def test_youtube_exchange_errors_surface_as_runtime_error(social_dir, monkeypatch):
    calls = []

    def fake_flow():
        f = _BrokenFlow() if calls else _FakeFlow()
        calls.append(f)
        return f

    monkeypatch.setattr(social, "_yt_flow", fake_flow)
    state, _ = social.begin_youtube_auth()
    with pytest.raises(RuntimeError, match="Google could not complete.*Missing code verifier"):
        social.complete_youtube_auth(state, "the-code")


# --------------------------------------------------------------------------- #
# Instagram — connect
# --------------------------------------------------------------------------- #
def test_instagram_connect_validates_and_stores(social_dir, monkeypatch):
    fake = FakeHTTP([FakeResp(200, {"id": "1784140000001", "username": "clipqueen"})])
    monkeypatch.setattr(social.httpx, "get", fake.get)

    acc = social.connect_instagram("EAAG-long-token", "1784140000001", label="")
    assert acc["platform"] == "instagram"
    assert acc["handle"] == "@clipqueen"
    assert social.get_account(acc["id"])["credentials"]["access_token"] == "EAAG-long-token"
    # the Graph call carried the token as a query param for validation
    assert fake.calls[0]["params"]["access_token"] == "EAAG-long-token"


def test_instagram_connect_rejects_bad_token(social_dir, monkeypatch):
    fake = FakeHTTP([FakeResp(400, {"error": {"message": "Invalid OAuth access token",
                                              "code": 190}})])
    monkeypatch.setattr(social.httpx, "get", fake.get)
    with pytest.raises(RuntimeError, match="Invalid OAuth access token"):
        social.connect_instagram("bad", "123")
    with pytest.raises(ValueError, match="required"):
        social.connect_instagram("", "")


# --------------------------------------------------------------------------- #
# Instagram — Reels publish
# --------------------------------------------------------------------------- #
def _ig_account():
    return {"id": "ig_x", "platform": "instagram",
            "credentials": {"access_token": "TOK", "user_id": "1784140000001"}}


def test_instagram_reel_publish_flow(social_dir, tmp_path, monkeypatch):
    fake = FakeHTTP([
        FakeResp(200, {"id": "container7", "uri": "https://rupload.facebook.com/x"}),
        FakeResp(200, {"success": True}),
        FakeResp(200, {"status_code": "IN_PROGRESS"}),
        FakeResp(200, {"status_code": "FINISHED"}),
        FakeResp(200, {"id": "media123"}),
        FakeResp(200, {"permalink": "https://www.instagram.com/reel/media123/"}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)
    monkeypatch.setattr(config, "IG_POLL_SECONDS", 0.0)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video-bytes" * 8)

    out = social.publish_instagram(_ig_account(), video, caption="my caption #clip")
    assert out["id"] == "media123"
    assert out["url"] == "https://www.instagram.com/reel/media123/"

    create = fake.calls[0]
    assert create["method"] == "POST"
    assert create["data"]["media_type"] == "REELS"
    assert create["data"]["upload_type"] == "resumable"
    assert create["data"]["caption"] == "my caption #clip"
    assert create["data"]["share_to_feed"] == "true"
    upload = fake.calls[1]
    assert upload["content"] == video.read_bytes()
    assert upload["headers"]["file_size"] == str(video.stat().st_size)
    publish = fake.calls[4]
    assert publish["url"].endswith("/media_publish")
    assert publish["data"]["creation_id"] == "container7"


def test_instagram_processing_error_surfaces(social_dir, tmp_path, monkeypatch):
    fake = FakeHTTP([
        FakeResp(200, {"id": "c1", "uri": "https://rupload.facebook.com/x"}),
        FakeResp(200, {"success": True}),
        FakeResp(200, {"status_code": "ERROR"}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="processing error"):
        social.publish_instagram(_ig_account(), video, caption="c")


def test_instagram_processing_timeout(social_dir, tmp_path, monkeypatch):
    fake = FakeHTTP([
        FakeResp(200, {"id": "c1", "uri": "https://rupload.facebook.com/x"}),
        FakeResp(200, {"success": True}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)
    monkeypatch.setattr(config, "IG_POLL_TIMEOUT", 0.0)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="still processing"):
        social.publish_instagram(_ig_account(), video, caption="c")


# --------------------------------------------------------------------------- #
# Clip defaults (publish.json → post payload) + dispatcher
# --------------------------------------------------------------------------- #
def test_publish_defaults_reads_sibling_json(job):
    path, meta = _clip_with_metadata(job)
    defaults = social.publish_defaults(path)
    assert defaults["title"] == meta["youtube"]["title"]
    assert defaults["description"] == meta["youtube"]["description"]
    assert defaults["caption"] == meta["instagram"]["caption"]
    assert defaults["tags"] == meta["youtube"]["tags"]
    # the 🪝 hook variant of a clip shares its highlight's metadata
    hook = job.workdir / "clip_01_hook.mp4"
    hook.write_bytes(b"x")
    assert social.publish_defaults(hook)["title"] == meta["youtube"]["title"]
    # no metadata file → empty defaults, never an error
    assert social.publish_defaults(job.workdir / "clip_09.mp4")["title"] == ""


def test_post_clip_unknown_account_or_file(job, social_dir, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(LookupError):
        social.post_clip("yt_missing", video, title="t")
    acc = social.add_account("youtube", handle="@x", credentials={"token": "t"})
    with pytest.raises(FileNotFoundError):
        social.post_clip(acc["id"], tmp_path / "gone.mp4")


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
def test_api_social_accounts_shape_and_secrecy(social_dir):
    social.add_account("instagram", handle="@secret", credentials={"access_token": "TOKEN-XYZ"})

    resp = client.get("/api/social/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"][0]["handle"] == "@secret"
    assert "TOKEN-XYZ" not in resp.text          # never leak credentials
    assert "credentials" not in resp.text
    assert "youtube_configured" in body


def test_api_youtube_auth_url_unconfigured(social_dir, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET_FILE", "/nonexistent/client.json")
    resp = client.get("/api/social/youtube/auth_url")
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]


def test_api_youtube_callback_bad_state(social_dir):
    resp = client.get("/api/social/youtube/callback?code=x&state=bogus")
    assert resp.status_code == 400
    assert "Could not connect" in resp.text
    assert client.get("/api/social/youtube/callback?error=access_denied&state=x").status_code == 400


def test_api_instagram_connect_and_delete(social_dir, monkeypatch):
    fake = FakeHTTP([FakeResp(200, {"id": "42", "username": "myclips"})])
    monkeypatch.setattr(social.httpx, "get", fake.get)

    resp = client.post("/api/social/instagram/connect",
                       json={"access_token": "AAA", "ig_user_id": "42", "label": "main"})
    assert resp.status_code == 200
    account_id = resp.json()["id"]
    assert "AAA" not in resp.text

    listed = client.get("/api/social/accounts").json()["accounts"]
    assert [a["id"] for a in listed] == [account_id]

    assert client.delete(f"/api/social/accounts/{account_id}").json() == {"deleted": account_id}
    assert client.get("/api/social/accounts").json()["accounts"] == []
    assert client.delete(f"/api/social/accounts/{account_id}").status_code == 404


def test_api_post_uses_publish_metadata_defaults(job, social_dir, monkeypatch):
    _clip_with_metadata(job)
    seen = {}

    # real dispatcher (computes defaults from clip_01_publish.json); only the
    # network layer is stubbed
    def fake_upload(acc, path, *, title, description="", tags=None, privacy=""):
        seen.update(account_id=acc["id"], name=path.name, title=title,
                    description=description, tags=tags or [], privacy=privacy)
        return {"id": "z", "url": "https://www.youtube.com/watch?v=z",
                "studio": "https://studio.youtube.com/video/z/edit"}

    monkeypatch.setattr(social, "upload_youtube", fake_upload)
    acc = social.add_account("youtube", handle="@x", credentials={"token": "t"})

    resp = client.post("/api/social/post", json={
        "job_id": job.id, "file": "clip_01.mp4", "account_id": acc["id"],
    })
    assert resp.status_code == 200
    assert resp.json()["url"].endswith("=z")
    assert seen["title"] == "YT title that sells the clip"
    assert seen["description"] == "desc body"
    assert seen["tags"] == ["clip", "shorts"]

    # explicit text overrides the defaults
    client.post("/api/social/post", json={
        "job_id": job.id, "file": "clip_01.mp4", "account_id": acc["id"],
        "title": "My own title",
    })
    assert seen["title"] == "My own title"


def test_api_post_error_paths(job, social_dir, tmp_path):
    _clip_with_metadata(job)

    # unknown job
    resp = client.post("/api/social/post", json={
        "job_id": "nope", "file": "clip_01.mp4", "account_id": "x"})
    assert resp.status_code == 404

    # path traversal in file name
    resp = client.post("/api/social/post", json={
        "job_id": job.id, "file": "../secrets.env", "account_id": "x"})
    assert resp.status_code == 400

    # unknown clip file
    resp = client.post("/api/social/post", json={
        "job_id": job.id, "file": "clip_99.mp4", "account_id": "x"})
    assert resp.status_code == 404

    # unknown account (real dispatcher)
    resp = client.post("/api/social/post", json={
        "job_id": job.id, "file": "clip_01.mp4", "account_id": "yt_ghost"})
    assert resp.status_code == 404
    assert "account not found" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# TikTok — OAuth connect
# --------------------------------------------------------------------------- #
def test_tiktok_auth_requires_configuration(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "")
    assert social.tiktok_configured() is False
    with pytest.raises(RuntimeError, match="not configured"):
        social.begin_tiktok_auth()


def test_tiktok_auth_url_and_state_handling(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "ck-123")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "cs-456")

    state, url = social.begin_tiktok_auth()
    assert state and state in url
    assert url.startswith(social.TT_AUTHORIZE_URL)
    assert "client_key=ck-123" in url
    assert "response_type=code" in url
    assert "video.publish" in url          # direct-post scope is requested
    assert "redirect_uri=" in url
    # TikTok rejects the consent screen without PKCE — the challenge must be
    # in the URL and the matching verifier stored for the token exchange.
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert isinstance(social._pending[state], dict)
    verifier = social._pending[state]["verifier"]
    assert verifier and len(verifier) >= 43   # RFC 7636 length floor
    # the challenge really is S256(verifier), not a random string
    import base64, hashlib as _hashlib
    expect = base64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert f"code_challenge={expect}" in url

    # unknown / empty state or code is rejected before any token exchange
    with pytest.raises(RuntimeError, match="expired|missing"):
        social.complete_tiktok_auth("bogus-state", "code")
    with pytest.raises(RuntimeError, match="missing"):
        social.complete_tiktok_auth(state, "")


def test_tiktok_connect_exchanges_code_and_stores(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "cs")
    state, _ = social.begin_tiktok_auth()
    verifier_expected = social._pending[state]["verifier"]  # PKCE verifier to assert

    fake = FakeHTTP([
        FakeResp(200, {"access_token": "AT", "refresh_token": "RT",
                       "expires_in": 86400, "open_id": "oid1"}),
        FakeResp(200, {"error": {"code": 0, "message": "ok"}, "data": {"user": {
            "open_id": "oid1", "display_name": "Clip Star",
            "avatar_url": "https://a/1.jpg"}}}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)

    acc = social.complete_tiktok_auth(state, "the-code")
    assert acc["platform"] == "tiktok"
    assert acc["id"].startswith("tt_")
    assert acc["handle"] == "Clip Star"

    stored = social.get_account(acc["id"])["credentials"]
    assert stored["access_token"] == "AT"
    assert stored["refresh_token"] == "RT"
    # the exchange carried client_key + secret + code (server-side secret)
    tok = fake.calls[0]
    assert tok["url"] == social.TT_TOKEN_URL
    assert tok["data"]["client_key"] == "ck"
    assert tok["data"]["client_secret"] == "cs"
    assert tok["data"]["code"] == "the-code"
    assert tok["data"]["grant_type"] == "authorization_code"
    # PKCE: the verifier stored at begin must be sent to exchange, or TikTok
    # rejects the consent screen / token swap with "code_challenge".
    assert tok["data"]["code_verifier"] == verifier_expected
    # profile read used the fresh bearer token
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer AT"


def test_tiktok_token_refresh_on_expiry(social_dir, monkeypatch):
    fake = FakeHTTP([
        FakeResp(200, {"access_token": "NEW", "refresh_token": "R2",
                       "expires_in": 3600, "open_id": "oid1"}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)

    acc = social.add_account(
        "tiktok", handle="@tt",
        credentials={"access_token": "OLD", "refresh_token": "R1",
                     "expires_in": 1, "obtained_at": 0},  # expired
    )
    full = social.get_account(acc["id"])
    assert social._tiktok_access_token(full) == "NEW"
    # refreshed token is persisted for next time
    assert social.get_account(acc["id"])["credentials"]["access_token"] == "NEW"
    assert fake.calls[0]["data"]["grant_type"] == "refresh_token"
    assert fake.calls[0]["data"]["refresh_token"] == "R1"


def test_tiktok_expired_without_refresh_token_raises(social_dir):
    acc = social.add_account(
        "tiktok", handle="@tt2",
        credentials={"access_token": "OLD", "expires_in": 1, "obtained_at": 0},
    )
    with pytest.raises(RuntimeError, match="reconnect"):
        social._tiktok_access_token(social.get_account(acc["id"]))


# --------------------------------------------------------------------------- #
# TikTok — Direct Post (FILE_UPLOAD)
# --------------------------------------------------------------------------- #
def _privacy_ok(options=("PUBLIC_TO_EVERYONE", "SELF_ONLY")):
    return FakeResp(200, {"error": {"code": 0, "message": "ok"},
                          "data": {"privacy_level_options": list(options)}})


def test_tiktok_direct_post_file_upload_flow(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_tiktok_access_token", lambda acc: "tt-token")
    monkeypatch.setattr(config, "TIKTOK_POLL_SECONDS", 0.0)

    fake = FakeHTTP([
        _privacy_ok(),
        FakeResp(200, {"error": {"code": 0, "message": "ok"},
                       "publish_id": "PUB42", "upload_url": "https://upload.tt/sess"}),
        FakeResp(200, {}),
        FakeResp(200, {"error": {"code": 0, "message": "ok"},
                       "data": {"status": "PUBLISH_COMPLETE"}}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "put", fake.put)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789" * 12)  # 120 bytes → one chunk
    size = video.stat().st_size

    out = social.upload_tiktok(
        {"id": "tt_x", "credentials": {}}, video,
        title="My caption", description="body", tags=["shorts", "clip"],
    )
    assert out["publish_id"] == "PUB42"
    assert out["id"] == "PUB42"

    creator, init, put, status = fake.calls
    assert creator["url"] == social.TT_CREATOR_INFO_URL
    assert init["url"] == social.TT_VIDEO_INIT_URL
    body = init["json"]
    assert body["source_info"]["source"] == "FILE_UPLOAD"
    assert body["source_info"]["video_size"] == size
    assert body["source_info"]["total_chunk_count"] == 1
    assert body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert "My caption" in body["post_info"]["title"]
    assert "#shorts" in body["post_info"]["title"]        # tags become hashtags
    assert init["headers"]["Authorization"] == "Bearer tt-token"

    assert put["method"] == "PUT"
    assert put["url"] == "https://upload.tt/sess"
    assert put["headers"]["Content-Range"] == f"bytes 0-{size - 1}/{size}"
    assert put["content"] == video.read_bytes()

    assert status["url"] == social.TT_STATUS_URL
    assert status["json"] == {"publish_id": "PUB42"}


def test_tiktok_upload_chunks_reassemble_byte_exact(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_tiktok_access_token", lambda acc: "tok")
    monkeypatch.setattr(config, "TIKTOK_POLL_SECONDS", 0.0)
    monkeypatch.setattr(social, "TT_CHUNK_BYTES", 50)

    payload = bytes(range(256)) * 2  # 512 bytes
    video = tmp_path / "c.mp4"
    video.write_bytes(payload)
    n_chunks = (len(payload) + 50 - 1) // 50

    responses = [
        _privacy_ok(("SELF_ONLY",)),
        FakeResp(200, {"error": {"code": 0}, "publish_id": "P",
                       "upload_url": "https://u"}),
        *[FakeResp(200, {}) for _ in range(n_chunks)],
        FakeResp(200, {"error": {"code": 0}, "data": {"status": "PUBLISH_COMPLETE"}}),
    ]
    fake = FakeHTTP(responses)
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "put", fake.put)

    social.upload_tiktok({"id": "tt_x", "credentials": {}}, video, title="t")

    puts = [c for c in fake.calls if c["method"] == "PUT"]
    assert len(puts) == n_chunks
    joined = b"".join(p["content"] for p in puts)
    assert joined == payload  # byte-exact reassembly
    offset = 0
    for p in puts:
        n = len(p["content"])
        assert p["headers"]["Content-Range"] == f"bytes {offset}-{offset + n - 1}/{len(payload)}"
        offset += n
    assert offset == len(payload)

    init = fake.calls[1]
    assert init["json"]["source_info"]["total_chunk_count"] == n_chunks
    # the account only allows private → we downgrade instead of failing
    assert init["json"]["post_info"]["privacy_level"] == "SELF_ONLY"


def test_tiktok_upload_surfaces_api_error(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_tiktok_access_token", lambda acc: "tok")
    fake = FakeHTTP([
        _privacy_ok(),
        FakeResp(400, {"error": {"code": 10000,
                                 "description": "privacy_level not allowed"}}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)

    video = tmp_path / "c.mp4"
    video.write_bytes(b"xx")
    with pytest.raises(RuntimeError, match="privacy_level not allowed"):
        social.upload_tiktok({"id": "tt_x", "credentials": {}}, video, title="t")


def test_tiktok_publish_failed_status(social_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(social, "_tiktok_access_token", lambda acc: "tok")
    monkeypatch.setattr(config, "TIKTOK_POLL_SECONDS", 0.0)
    fake = FakeHTTP([
        _privacy_ok(),
        FakeResp(200, {"error": {"code": 0}, "publish_id": "P",
                       "upload_url": "https://u"}),
        FakeResp(200, {}),
        FakeResp(200, {"error": {"code": 0}, "data": {"status": "FAILED"}}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "put", fake.put)

    video = tmp_path / "c.mp4"
    video.write_bytes(b"xx")
    with pytest.raises(RuntimeError, match="publish failed"):
        social.upload_tiktok({"id": "tt_x", "credentials": {}}, video, title="t")


def test_post_clip_dispatches_tiktok(job, social_dir, monkeypatch):
    path, meta = _clip_with_metadata(job)
    seen = {}

    def fake_upload(acc, p, *, title, description="", tags=None, privacy="", on_status=None):
        seen.update(title=title, description=description, tags=tags or [], privacy=privacy)
        return {"id": "pub1", "url": ""}

    monkeypatch.setattr(social, "upload_tiktok", fake_upload)
    acc = social.add_account("tiktok", handle="@tt", credentials={"token": "t"})

    out = social.post_clip(acc["id"], path)
    # TikTok's single caption comes from the IG caption (it carries hashtags)
    assert seen["title"] == meta["instagram"]["caption"]
    assert seen["description"] == meta["youtube"]["description"]
    assert seen["tags"] == meta["youtube"]["tags"]
    assert out["platform"] == "tiktok"


# --------------------------------------------------------------------------- #
# TikTok — API endpoints
# --------------------------------------------------------------------------- #
def test_api_accounts_includes_tiktok_configured(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "cs")
    body = client.get("/api/social/accounts").json()
    assert body["tiktok_configured"] is True
    assert "tiktok_redirect_uri" in body


def test_api_tiktok_auth_url_unconfigured(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "")
    resp = client.get("/api/social/tiktok/auth_url")
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]


def test_api_tiktok_auth_url_and_callback(social_dir, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "cs")

    resp = client.get("/api/social/tiktok/auth_url")
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://www.tiktok.com/v2/auth/authorize/")

    # a bad state surfaces a readable page — never a bare 500
    cb = client.get("/api/social/tiktok/callback?code=x&state=bogus")
    assert cb.status_code == 400
    assert "Could not connect" in cb.text
    # TikTok sending an error param is also handled
    assert client.get("/api/social/tiktok/callback?error=access_denied&state=x").status_code == 400


# --------------------------------------------------------------------------- #
# Instagram — Facebook-app OAuth connect + token lifecycle
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ig_oauth(social_dir, monkeypatch):
    monkeypatch.setattr(config, "IG_OAUTH", True)
    monkeypatch.setattr(config, "IG_APP_ID", "fb-app-id")
    monkeypatch.setattr(config, "IG_APP_SECRET", "fb-secret")
    monkeypatch.setattr(
        config, "IG_REDIRECT_URI",
        "http://127.0.0.1:8000/api/social/instagram/callback",
    )
    return social_dir


def test_instagram_oauth_requires_configuration(social_dir, monkeypatch):
    monkeypatch.setattr(config, "IG_OAUTH", True)
    monkeypatch.setattr(config, "IG_APP_ID", "")
    monkeypatch.setattr(config, "IG_APP_SECRET", "")
    assert social.instagram_oauth_configured() is False
    with pytest.raises(RuntimeError, match="not configured"):
        social.begin_instagram_auth()
    # IG_OAUTH=0 hides the one-click flow even with an app configured
    monkeypatch.setattr(config, "IG_APP_ID", "id")
    monkeypatch.setattr(config, "IG_APP_SECRET", "sec")
    monkeypatch.setattr(config, "IG_OAUTH", False)
    assert social.instagram_oauth_configured() is False


def test_instagram_oauth_auth_url_carries_state_and_scopes(ig_oauth):
    state, url = social.begin_instagram_auth()
    assert url.startswith("https://www.facebook.com/")
    assert state in url
    assert "instagram_content_publish" in url          # publishing permission
    query = parse_qs(urlparse(url).query)
    assert query["redirect_uri"][0] == config.IG_REDIRECT_URI  # must match the app
    assert query["scope"][0] == social.IG_OAUTH_SCOPES
    assert isinstance(social._pending[state], dict)

    # unknown / empty state is rejected before any token exchange
    with pytest.raises(RuntimeError, match="expired|missing"):
        social.complete_instagram_auth("bogus-state", "code")
    with pytest.raises(RuntimeError, match="missing"):
        social.complete_instagram_auth(state, "")


def test_instagram_oauth_connect_exchanges_and_links_ig_account(ig_oauth, monkeypatch):
    state, _ = social.begin_instagram_auth()
    fake = FakeHTTP([
        # 1) authorization code -> short-lived token
        FakeResp(200, {"access_token": "SHORT", "token_type": "bearer"}),
        # 2) fb_exchange_token -> long-lived (~60 days)
        FakeResp(200, {"access_token": "LONG", "expires_in": 5184000}),
        # 3) /me/accounts -> the professional IG account on that Page
        FakeResp(200, {"data": [{"id": "page1", "name": "Clips Page",
                                 "instagram_business_account": {
                                     "id": "1784140000001", "username": "clipqueen"}}]}),
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)

    linked = social.complete_instagram_auth(state, "the-code")
    assert len(linked) == 1
    assert linked[0]["platform"] == "instagram"
    assert linked[0]["handle"] == "@clipqueen"

    stored = social.get_account(linked[0]["id"])["credentials"]
    assert stored["access_token"] == "LONG"        # long-lived, not the short one
    assert stored["user_id"] == "1784140000001"
    assert stored["expires_in"] == 5184000
    # the code exchange carried our client id/secret + exact redirect URI
    exchange = fake.calls[0]
    assert exchange["method"] == "POST"
    assert exchange["data"]["code"] == "the-code"
    assert exchange["data"]["client_id"] == "fb-app-id"
    assert exchange["data"]["redirect_uri"] == config.IG_REDIRECT_URI


def test_instagram_oauth_requires_professional_account(ig_oauth, monkeypatch):
    state, _ = social.begin_instagram_auth()
    fake = FakeHTTP([
        FakeResp(200, {"access_token": "SHORT"}),   # code exchange
        FakeResp(400, {}),                           # long-lived exchange fails
        FakeResp(200, {"data": []}),                 # no Page/IG account found
    ])
    monkeypatch.setattr(social.httpx, "post", fake.post)
    monkeypatch.setattr(social.httpx, "get", fake.get)

    with pytest.raises(RuntimeError, match="professional"):
        social.complete_instagram_auth(state, "the-code")


def test_instagram_token_info_statuses(ig_oauth):
    now = time.time()
    ok = {"credentials": {"access_token": "T", "user_id": "1",
                           "obtained_at": now, "expires_in": 5184000}}
    info = social._ig_token_info(ok)
    assert info["status"] == "ok"
    assert info["days_left"] > 50
    assert info["refreshable"] is True
    assert "access_token" not in info               # health only, never the token

    expiring = {"credentials": {"access_token": "T", "user_id": "1",
                                 "obtained_at": now, "expires_in": 3 * 86400}}
    assert social._ig_token_info(expiring)["status"] == "expiring"

    expired = {"credentials": {"access_token": "T", "user_id": "1",
                                "obtained_at": now - 70 * 86400,
                                "expires_in": 5184000}}
    assert social._ig_token_info(expired)["status"] == "expired"

    pasted = {"credentials": {"access_token": "T", "user_id": "1"}}
    assert social._ig_token_info(pasted)["status"] == "unknown"

    assert social._ig_token_info({})["status"] == "missing"


def test_instagram_valid_token_never_triggers_a_refresh(ig_oauth, monkeypatch):
    """A healthy token must be used as-is — no extra HTTP round-trip."""
    def boom(*a, **kw):
        raise AssertionError("refresh should not be attempted for a valid token")

    monkeypatch.setattr(social.httpx, "get", boom)
    monkeypatch.setattr(social.httpx, "post", boom)
    acc = {"id": "ig_x", "platform": "instagram",
           "credentials": {"access_token": "T", "user_id": "1",
                           "obtained_at": time.time(), "expires_in": 5184000}}
    assert social._ig_access_token(acc) == "T"


def test_instagram_publish_refreshes_stale_token_first(ig_oauth, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IG_POLL_SECONDS", 0.0)
    acc = social.add_account(
        "instagram", handle="@clipqueen",
        credentials={"access_token": "OLD", "user_id": "1784140000001",
                     "obtained_at": time.time() - 70 * 86400,
                     "expires_in": 5184000},
    )
    fake = FakeHTTP([
        FakeResp(200, {"access_token": "NEW", "expires_in": 5184000}),  # refresh
        FakeResp(200, {"id": "CNT", "uri": "https://up.example/u"}),   # container
        FakeResp(200, {}),                                                # upload
        FakeResp(200, {"status_code": "FINISHED"}),                       # poll
        FakeResp(200, {"id": "MEDIA"}),                                  # publish
        FakeResp(200, {"permalink": "https://www.instagram.com/reel/M/"}),
    ])
    monkeypatch.setattr(social.httpx, "get", fake.get)
    monkeypatch.setattr(social.httpx, "post", fake.post)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789" * 10)

    full = social.get_account(acc["id"])
    out = social.publish_instagram(full, video, caption="cap")
    assert out["url"] == "https://www.instagram.com/reel/M/"

    # step 1 was the fb_exchange_token refresh of the expired token…
    refresh = fake.calls[0]
    assert refresh["method"] == "GET"
    assert refresh["params"]["grant_type"] == "fb_exchange_token"
    assert refresh["params"]["fb_exchange_token"] == "OLD"
    # …and every subsequent call used the NEW token, persisted to disk
    assert fake.calls[1]["data"]["access_token"] == "NEW"
    stored = social.get_account(acc["id"])["credentials"]
    assert stored["access_token"] == "NEW"
    assert stored["expires_in"] == 5184000


def test_instagram_reconnect_refreshes_token_on_demand(ig_oauth, monkeypatch):
    acc = social.add_account(
        "instagram", handle="@stale",
        credentials={"access_token": "OLD", "user_id": "1",
                     "obtained_at": time.time() - 70 * 86400,
                     "expires_in": 5184000},
    )
    fake = FakeHTTP([FakeResp(200, {"access_token": "NEW", "expires_in": 5184000})])
    monkeypatch.setattr(social.httpx, "get", fake.get)

    info = social.reconnect_instagram(acc["id"])
    assert info["status"] == "ok"
    assert info["days_left"] > 50
    assert social.get_account(acc["id"])["credentials"]["access_token"] == "NEW"

    # unknown account / wrong platform are clean typed errors
    with pytest.raises(LookupError):
        social.reconnect_instagram("ig_missing")
    yt = social.add_account("youtube", handle="@ch", credentials={"token": "t"})
    with pytest.raises(ValueError, match="Instagram"):
        social.reconnect_instagram(yt["id"])


# --------------------------------------------------------------------------- #
# Instagram — API endpoints
# --------------------------------------------------------------------------- #
def test_api_instagram_oauth_endpoints(social_dir, monkeypatch):
    monkeypatch.setattr(config, "IG_OAUTH", True)
    monkeypatch.setattr(config, "IG_APP_ID", "")
    monkeypatch.setattr(config, "IG_APP_SECRET", "")
    body = client.get("/api/social/accounts").json()
    assert body["instagram_configured"] is False
    assert "instagram_redirect_uri" in body
    assert client.get("/api/social/instagram/auth_url").status_code == 400

    monkeypatch.setattr(config, "IG_APP_ID", "fb-id")
    monkeypatch.setattr(config, "IG_APP_SECRET", "fb-sec")
    assert client.get("/api/social/accounts").json()["instagram_configured"] is True

    resp = client.get("/api/social/instagram/auth_url")
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://www.facebook.com/")

    # a bad state surfaces a readable page — never a bare 500
    cb = client.get("/api/social/instagram/callback?code=x&state=bogus")
    assert cb.status_code == 400
    assert "Could not connect" in cb.text
    # Meta sending an error param is also handled
    assert client.get(
        "/api/social/instagram/callback?error=user_denied&state=x"
    ).status_code == 400

    # refreshing a nonexistent account is a clean 404
    r = client.post("/api/social/instagram/refresh", json={"account_id": "nope"})
    assert r.status_code == 404
