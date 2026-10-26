"""🔌 Accounts — link multiple YouTube channels and Instagram accounts.

A separate Streamlit page (auto-discovered in `frontend/pages/`) for
connecting the social accounts clips get posted to:

  * **YouTube** — one click per channel: Google's consent screen, tokens
    stored on disk; repeat to link more channels.
  * **Instagram** — one click per account via your own Facebook app (the
    app exchanges the sign-in for a 60-day token and refreshes it for you),
    or paste an access token + IG user id manually (step-by-step guide on
    the page); repeat for more accounts.

Posting itself happens below every clip on the home page (📣 Post panel).
"""
from __future__ import annotations

import os

import httpx
import streamlit as st

DEFAULT_API = os.environ.get("SHORTS_API_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="Accounts — Shorts Studio", page_icon="🔌", layout="wide")


def api_base() -> str:
    return st.session_state.get("api_base", DEFAULT_API).rstrip("/")


def call(method: str, path: str, **kwargs):
    try:
        resp = httpx.request(method, f"{api_base()}{path}", timeout=120.0, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        st.error(f"API request failed: {exc}")
        return None


def call_detail(method: str, path: str, **kwargs):
    """Like call(), but returns (data, api_error_message) for form feedback."""
    try:
        resp = httpx.request(method, f"{api_base()}{path}", timeout=120.0, **kwargs)
    except httpx.HTTPError as exc:
        return None, f"API request failed: {exc}"
    if resp.is_success:
        try:
            return resp.json(), ""
        except ValueError:
            return {}, ""
    try:
        detail = resp.json().get("detail", resp.reason_phrase)
    except ValueError:
        detail = resp.reason_phrase
    return None, str(detail)


# ---------------------------------------------------------------- sidebar ----
with st.sidebar:
    st.title("🔌 Accounts")
    st.caption(
        "Link the YouTube channels and Instagram accounts your clips get "
        "posted to — then post from the button below any clip."
    )
    st.session_state["api_base"] = st.text_input(
        "API base URL", value=st.session_state.get("api_base", DEFAULT_API)
    )
    if call("GET", "/healthz"):
        st.success("API connected")
    else:
        st.warning("API not reachable — start it with `python -m app.main`")

st.header("🔌 Connected accounts")
st.caption(
    "Link as many accounts as you want. Each clip on the home page gets a "
    "**📣 Post to** panel that posts it directly with the generated title, "
    "description and tags."
)

data = call("GET", "/api/social/accounts") or {}
accounts = data.get("accounts", []) or []
youtube_configured = bool(data.get("youtube_configured"))
tiktok_configured = bool(data.get("tiktok_configured"))

left, mid, right = st.columns(3)

# ------------------------------------------------------------- YouTube ------
with left:
    st.subheader("▶️ YouTube")

    if not youtube_configured:
        with st.expander("⚙️ One-time setup (about 5 minutes)", expanded=True):
            st.markdown(
                """
1. Open [console.cloud.google.com](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → External → fill the app name
   and your email → save. Add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app** → create → copy the Client ID + Secret.
5. Put them in the project's `.env` and restart the API:
   ```bash
   GOOGLE_CLIENT_ID=….apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=…
   ```
   (or save the downloaded JSON as `client_secret.json` in the project root)
"""
            )
        st.info("Set the credentials in .env, restart the API, then reload this page.")
    else:
        auth = call("GET", "/api/social/youtube/auth_url") or {}
        if auth.get("url"):
            st.link_button(
                "🔗 Connect a YouTube account",
                auth["url"],
                type="primary",
                use_container_width=True,
                help="Opens Google's sign-in — pick a channel, then come back to this tab.",
            )
            st.caption(
                "Connecting a different Google account links a different channel — "
                "click the button again for every channel you want."
            )
        else:
            st.caption("…")

    yt_accounts = [a for a in accounts if a["platform"] == "youtube"]
    if yt_accounts:
        st.markdown("**Linked channels**")
        for acc in yt_accounts:
            c1, c2 = st.columns([4, 1])
            with c1:
                avatar = acc.get("avatar") or ""
                if avatar:
                    st.image(avatar, width=32)
                st.markdown(f"**{acc.get('handle') or acc.get('label') or acc['id']}**")
                st.caption(acc.get("label") or "")
            with c2:
                if st.button("Unlink", key=f"un-yt-{acc['id']}"):
                    call("DELETE", f"/api/social/accounts/{acc['id']}")
                    st.rerun()
    else:
        st.caption("No YouTube channel linked yet.")

# ------------------------------------------------------------ Instagram -----
with mid:
    st.subheader("📸 Instagram")
    ig_configured = bool(data.get("instagram_configured"))

    if not ig_configured:
        with st.expander("⚙️ One-time setup (about 5 minutes)", expanded=True):
            st.markdown(
                """
Your Instagram must be a **professional (Business/Creator) account** linked
to a Facebook Page you manage. A free Facebook app gives you a one-click
sign-in **and automatic 60-day token refresh**, so linking stops going stale:

1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps/)
   → **Create App** → type **Business** → create (free, no review needed
   for your own accounts).
2. In the app, add the **Instagram Graph API** product.
3. **App settings → Basic** → copy the **App ID** + **App Secret** into `.env`:
   ```bash
   IG_APP_ID=…
   IG_APP_SECRET=…
   ```
4. Restart the API, reload this page, then click **Connect with Facebook**.

No app? You can still paste a token manually below — those tokens expire
every ~60 days and you renew them yourself with **Refresh token**.
"""
            )
        st.info("Set IG_APP_ID / IG_APP_SECRET in .env, restart the API, then reload this page.")
    else:
        auth = call("GET", "/api/social/instagram/auth_url") or {}
        if auth.get("url"):
            st.link_button(
                "🔗 Connect with Facebook",
                auth["url"],
                type="primary",
                use_container_width=True,
                help="Sign in as the Facebook account that owns your Instagram — "
                     "each click links one Instagram account.",
            )
            st.caption(
                "Each Instagram account sits under a Facebook Page — connect "
                "a different Facebook account to link a different Instagram."
            )
        else:
            st.caption("…")

    with st.expander("…or paste a token manually", expanded=not ig_configured):
        with st.form("ig-connect"):
            st.caption("Paste the access token + user id of a professional account")
            ig_label = st.text_input("Label (optional)", placeholder="Main account")
            ig_token = st.text_input(
                "Access token",
                type="password",
                placeholder="IG… or EAAG… long-lived token",
            )
            ig_user_id = st.text_input("Instagram user id", placeholder="17841400…")
            ig_submitted = st.form_submit_button("🔗 Connect Instagram", type="primary")

        if ig_submitted:
            acc, err = call_detail(
                "POST", "/api/social/instagram/connect",
                json={"access_token": ig_token.strip(), "ig_user_id": ig_user_id.strip(),
                      "label": ig_label.strip()},
            )
            if err:
                st.error(err)
            elif acc:
                st.success(f"Connected {acc.get('handle', '')} 🎉")
                st.rerun()

    with st.expander("⚙️ Where do I get that token?"):
        st.markdown(
            """
Your Instagram must be a **professional (Business/Creator) account**.

1. Make sure the account is professional: Instagram app → Settings →
   Account → **Switch to professional account**, and linked to a Facebook Page.
2. Go to [developers.facebook.com](https://developers.facebook.com/apps/) →
   **Create App** → type **Business** → create (free, no review needed for
   your own accounts).
3. Add the **Instagram Graph API** product to the app.
4. Open **Graph API Explorer** ([link](https://developers.facebook.com/tools/explorer/)):
   select your app → add permissions **`instagram_basic`**,
   **`instagram_content_publish`**, **`pages_read_engagement`** →
   **Generate access token** and approve with the Facebook account that owns
   the Page/Instagram.
5. Copy the token (for a 60-day token call the *Get short-lived token*
   exchange in the Explorer), and get the **Instagram user id** from
   `GET /me/accounts` or the explorer's ID dropdown — paste both above.

Repeat with another token to link more Instagram accounts. Tokens last ~60
days — reconnect when Meta says they expired. Publishing quota: ~50 posts
per account per 24 h (Meta's official limit).
"""
        )

    ig_accounts = [a for a in accounts if a["platform"] == "instagram"]
    if ig_accounts:
        st.markdown("**Linked accounts**")
        for acc in ig_accounts:
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(f"**{acc.get('handle') or acc['label']}**")
                tok = acc.get("token") or {}
                status = tok.get("status", "")
                if status == "ok":
                    st.caption(f"token ok · {tok.get('days_left')} days left")
                elif status == "expiring":
                    st.warning(
                        f"Token expires in {tok.get('days_left')} days — "
                        "hit Refresh to extend it 60 more days"
                    )
                elif status == "expired":
                    st.error("Token expired — hit Refresh, or reconnect the account")
                elif status == "unknown":
                    st.caption(
                        "token lifetime unknown — Refresh extends it 60 more days"
                    )
                else:
                    st.caption(acc.get("label") or "")
            with c2:
                if tok.get("refreshable") and st.button(
                    "Refresh", key=f"rf-ig-{acc['id']}",
                    help="Exchange the stored token for a fresh 60-day one",
                ):
                    res, err = call_detail(
                        "POST", "/api/social/instagram/refresh",
                        json={"account_id": acc["id"]},
                    )
                    if err:
                        st.error(err)
                    else:
                        st.success("Token refreshed — 60 more days ✅")
                        st.rerun()
                if st.button("Unlink", key=f"un-ig-{acc['id']}"):
                    call("DELETE", f"/api/social/accounts/{acc['id']}")
                    st.rerun()
    else:
        st.caption("No Instagram account linked yet.")

# ------------------------------------------------------------- TikTok -------
with right:
    st.subheader("🎵 TikTok")

    if not tiktok_configured:
        with st.expander("⚙️ One-time setup (about 5 minutes)", expanded=False):
            st.markdown(
                """
1. Go to [developers.tiktok.com](https://developers.tiktok.com/apps/) →
   **Manage apps → Create app** → type **Unaudited** → create (free).
2. Add the **Content Posting API** product and enable **Direct post**.
3. Under **Login Kit**, add the redirect URI:
   ```
   http://127.0.0.1:8000/api/social/tiktok/callback
   ```
4. Copy the **Client Key** + **Client Secret** and put them in `.env`:
   ```bash
   TIKTOK_CLIENT_KEY=…
   TIKTOK_CLIENT_SECRET=…
   ```
5. Restart the API, then reload this page and click **Connect**.

> Unaudited apps can only post to your own account as a **private** video
> (`TIKTOK_PRIVACY=SELF_ONLY`). TikTok's audit unlocks public posts.
"""
            )
        st.info("Set TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env, restart the API, then reload.")
    else:
        auth = call("GET", "/api/social/tiktok/auth_url") or {}
        if auth.get("url"):
            st.link_button(
                "🔗 Connect a TikTok account",
                auth["url"],
                type="primary",
                use_container_width=True,
                help="Opens TikTok's sign-in — approve, then come back to this tab.",
            )
            st.caption(
                "Connecting a different TikTok account links a different profile — "
                "click the button again for every account you want."
            )
        else:
            st.caption("…")

    tt_accounts = [a for a in accounts if a["platform"] == "tiktok"]
    if tt_accounts:
        st.markdown("**Linked accounts**")
        for acc in tt_accounts:
            c1, c2 = st.columns([4, 1])
            with c1:
                avatar = acc.get("avatar") or ""
                if avatar:
                    st.image(avatar, width=32)
                st.markdown(f"**{acc.get('handle') or acc.get('label') or acc['id']}**")
                st.caption(acc.get("label") or "")
            with c2:
                if st.button("Unlink", key=f"un-tt-{acc['id']}"):
                    call("DELETE", f"/api/social/accounts/{acc['id']}")
                    st.rerun()
    else:
        st.caption("No TikTok account linked yet.")

st.divider()

# ------------------------------------------------------------ overview ------
if accounts:
    st.subheader(f"✅ {len(accounts)} account(s) ready to post")
    st.markdown(
        "Open the **🎬 home page**, scroll to any clip and use "
        "**📣 Post to** — the title, description and tags the app generated "
        "are filled in for you and stay editable."
    )
    st.table(
        [
            {
                "Platform": a["platform"].title(),
                "Account": a.get("handle") or a.get("label") or a["id"],
                "Label": a.get("label", ""),
                "Linked": (a.get("created_at") or "")[:10],
            }
            for a in accounts
        ]
    )
else:
    st.info("No accounts linked yet — connect your first one above.")
