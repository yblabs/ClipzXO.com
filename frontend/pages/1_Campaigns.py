"""🗂️ Campaigns — submit a campaign's rules, audit every clip against them.

A separate page (Streamlit auto-discovers `frontend/pages/`), so the main
generate-clips page stays untouched:

  1. paste / fill a campaign's submission rules (Whop campaign pages and
     similar clip-payment offers) and save them;
  2. pick any finished job and run the audit — every clip is checked against
     every active rule (title words, hashtags, description, duration, banned
     words, watermark, captions, CTA, language);
  3. get a score, the failing rules ranked, and ready-to-post corrections:
     corrected IG caption + hashtags, YT title and description to copy.
"""
from __future__ import annotations

import os

import httpx
import streamlit as st

DEFAULT_API = os.environ.get("SHORTS_API_URL", "http://127.0.0.1:8000")
PLATFORMS = ["whop", "instagram", "youtube", "tiktok", "other"]

st.set_page_config(page_title="Campaigns — Shorts Studio", page_icon="🗂️", layout="wide")


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


# The rule-text extractor lives in the app package (same repo/venv); it only
# prefills the form below, so a failure here degrades to manual entry.
try:
    from app.pipeline.campaigns import parse_rules_text
except Exception:  # noqa: BLE001 - frontend may run against a remote API
    parse_rules_text = None  # type: ignore[assignment]

# ---------------------------------------------------------------- sidebar ----
with st.sidebar:
    st.title("🗂️ Campaigns")
    st.caption(
        "Save a campaign's submission rules once, then audit every clip of any "
        "job against them before you post to Whop / Instagram / YouTube."
    )
    st.session_state["api_base"] = st.text_input(
        "API base URL", value=st.session_state.get("api_base", DEFAULT_API)
    )
    if call("GET", "/healthz"):
        st.success("API connected")
    else:
        st.warning("API not reachable — start it with `python -m app.main`")

st.header("🗂️ Campaign rules audit")
st.caption(
    "Paste the rules from the campaign page (or fill them in), save, then audit "
    "every clip of a finished job — titles, hashtags, description, duration, "
    "banned words, watermark, captions and CTA are each checked with a fix."
)

# ---------------------------------------------------------------- campaign editor ----
data = call("GET", "/api/campaigns") or {}
campaigns = data.get("campaigns", [])

with st.expander("➕ New / update a campaign", expanded=not campaigns):
    rev = int(st.session_state.get("camp_rev", 0))

    rules_text = st.text_area(
        "Paste the campaign's rules text (optional)",
        placeholder=(
            "e.g. Title must contain \"TradeVault\". Max 60 seconds. Include "
            "#tradevault #trading. No #giveaway. Watermark bottom-right. "
            "Captions required. CTA asking for a comment."
        ),
        key=f"camp_rules_text_{rev}",
    )
    if st.button("🔍 Extract rules from the pasted text", disabled=parse_rules_text is None):
        if not rules_text.strip():
            st.warning("Paste the campaign rules first.")
        elif parse_rules_text is None:
            st.warning("Extractor unavailable — fill the fields manually.")
        else:
            parsed = parse_rules_text(rules_text)
            st.session_state["camp_prefill"] = parsed
            st.session_state["camp_rev"] = rev + 1
            if parsed["found"]:
                st.toast("Found: " + "; ".join(parsed["found"]), icon="✅")
            else:
                st.toast("Nothing recognised — fill the fields manually.", icon="🤷")
            st.rerun()

    rules_file = st.file_uploader(
        "…or upload the rules file — PDF, TXT, MD, DOCX or JSON",
        type=["pdf", "txt", "md", "docx", "json"],
        key=f"camp_rules_file_{rev}",
        help=(
            "The file is read server-side: PDF via pypdf, DOCX via its XML, "
            "a .json with a rules object is imported directly. Recognised "
            "patterns prefill the form below; the raw text is kept in Campaign notes."
        ),
    )
    if st.button("📄 Extract rules from the file", disabled=rules_file is None):
        resp = call(
            "POST", "/api/campaigns/extract",
            files={"file": (rules_file.name, rules_file.getvalue(),
                            rules_file.type or "application/octet-stream")},
        )
        if resp:
            st.session_state["camp_prefill"] = {"rules": resp["rules"]}
            st.session_state["camp_file_text"] = resp.get("text", "")
            st.session_state["camp_rev"] = rev + 1
            if resp["found"]:
                st.toast("Found: " + "; ".join(resp["found"]), icon="✅")
            else:
                st.toast("File read, but no rules recognised — fill in manually.", icon="🤷")
            st.rerun()

    prefill: dict = st.session_state.get("camp_prefill", {}).get("rules", {})
    if prefill:
        st.caption("Form prefilled from the pasted text — adjust anything wrong, then save.")

    def _words(value: str) -> list[str]:
        return [w.strip() for w in value.split(",") if w.strip()]

    with st.form(f"camp-form-{rev}"):
        top = st.columns([3, 2, 2])
        name = top[0].text_input("Campaign name *", value="", placeholder="Whop — TradeVault")
        brand = top[1].text_input("Brand word", value=str(prefill.get("brand", "") or ""),
                                  placeholder="tradevault")
        platform = top[2].selectbox("Platform", PLATFORMS,
                                    index=PLATFORMS.index(
                                        st.session_state.get("camp_platform", "whop"))
                                    if st.session_state.get("camp_platform") in PLATFORMS else 0)
        notes = st.text_area(
            "Campaign notes (free text — kept with the campaign)",
            value=st.session_state.get("camp_file_text", "")[:4000],
            key=f"camp_notes_{rev}",
        )

        st.markdown("**Titles**")
        c1, c2, c3 = st.columns(3)
        req_title = c1.text_input(
            "Title must contain (comma-sep)",
            value=", ".join(prefill.get("required_title_words", [])),
            key=f"camp_req_title_{rev}",
        )
        ban_title = c2.text_input(
            "Title must NOT contain (comma-sep)",
            value=", ".join(prefill.get("banned_title_words", [])),
            key=f"camp_ban_title_{rev}",
        )
        title_max = c3.number_input(
            "Max title chars (0 = platform default)", min_value=0, max_value=2200,
            value=int(prefill.get("title_max_chars", 0) or 0),
            key=f"camp_title_max_{rev}",
        )

        st.markdown("**Hashtags & description**")
        c1, c2, c3 = st.columns(3)
        req_tags = c1.text_input(
            "Required hashtags (comma-sep)",
            value=", ".join(prefill.get("required_hashtags", [])),
            key=f"camp_req_tags_{rev}",
        )
        ban_tags = c2.text_input(
            "Banned hashtags (comma-sep)",
            value=", ".join(prefill.get("banned_hashtags", [])),
            key=f"camp_ban_tags_{rev}",
        )
        max_tags = c3.number_input(
            "Max hashtags (0 = none)", min_value=0, max_value=30,
            value=int(prefill.get("max_hashtags", 0) or 0),
            key=f"camp_max_tags_{rev}",
        )
        req_desc = st.text_input(
            "Description must contain (comma-sep, e.g. tradevault.com)",
            value=", ".join(prefill.get("required_description_words", [])),
            key=f"camp_req_desc_{rev}",
        )

        st.markdown("**Clip requirements**")
        c1, c2, c3, c4 = st.columns(4)
        min_dur = c1.number_input(
            "Min seconds (0 = none)", min_value=0.0, max_value=600.0, step=1.0,
            value=float(prefill.get("min_duration", 0.0) or 0.0),
            key=f"camp_min_dur_{rev}",
        )
        max_dur = c2.number_input(
            "Max seconds (0 = none)", min_value=0.0, max_value=600.0, step=1.0,
            value=float(prefill.get("max_duration", 0.0) or 0.0),
            key=f"camp_max_dur_{rev}",
        )
        ban_words = c3.text_input(
            "Never say (comma-sep)",
            value=", ".join(prefill.get("banned_words", [])),
            key=f"camp_ban_words_{rev}",
        )
        language = c4.text_input(
            "Language (e.g. en, blank = any)",
            value=str(prefill.get("language", "") or ""),
            key=f"camp_lang_{rev}",
        )

        st.markdown("**Rendering requirements**")
        c1, c2, c3, c4 = st.columns(4)
        wm_on = c1.checkbox("Watermark required",
                            value=bool(prefill.get("watermark_required", False)),
                            key=f"camp_wm_{rev}")
        positions = ["any", "top-left", "top-right", "bottom-left", "bottom-right",
                     "bottom-center"]
        wm_pos_default = str(prefill.get("watermark_position", "") or "any")
        wm_pos = c2.selectbox(
            "Watermark position", positions,
            index=positions.index(wm_pos_default) if wm_pos_default in positions else 0,
            key=f"camp_wm_pos_{rev}",
        )
        caps_on = c3.checkbox("Captions required",
                              value=bool(prefill.get("captions_required", False)),
                              key=f"camp_caps_{rev}")
        cta_on = c4.checkbox("CTA / question required",
                             value=bool(prefill.get("cta_required", False)),
                             key=f"camp_cta_{rev}")

        saved = st.form_submit_button("💾 Save campaign", type="primary")

    if saved:
        if not name.strip():
            st.warning("Give the campaign a name first.")
        else:
            st.session_state["camp_platform"] = platform
            campaign = call(
                "POST", "/api/campaigns",
                json={
                    "name": name.strip(),
                    "brand": brand.strip(),
                    "platform": platform,
                    "notes": notes,
                    "rules": {
                        "required_title_words": _words(req_title),
                        "banned_title_words": _words(ban_title),
                        "title_max_chars": int(title_max),
                        "required_hashtags": _words(req_tags),
                        "banned_hashtags": _words(ban_tags),
                        "max_hashtags": int(max_tags),
                        "required_description_words": _words(req_desc),
                        "min_duration": float(min_dur),
                        "max_duration": float(max_dur),
                        "banned_words": _words(ban_words),
                        "watermark_required": bool(wm_on),
                        "watermark_position": "" if wm_pos == "any" else wm_pos,
                        "captions_required": bool(caps_on),
                        "cta_required": bool(cta_on),
                        "language": language.strip(),
                    },
                },
            )
            if campaign:
                st.success(f"Campaign “{campaign['name']}” saved.")
                st.session_state.pop("camp_prefill", None)
                st.rerun()

# ---------------------------------------------------------------- pick + audit ----
data = call("GET", "/api/campaigns") or {}
campaigns = data.get("campaigns", [])
if not campaigns:
    st.info("Save a campaign first — the audit runs against its rules.")
    st.stop()

jobdirs = call("GET", "/api/jobdirs") or []
st.divider()
left, right = st.columns(2)
with left:
    chosen = st.selectbox(
        "Campaign",
        campaigns,
        format_func=lambda c: f"{c['name']} · {c['platform']}"
        + (f" · @{c['brand']}" if c.get("brand") else ""),
    )
with right:
    if not jobdirs:
        job_label = "— no finished jobs on disk —"
    else:
        job_label = st.selectbox(
            "Job to audit",
            jobdirs,
            format_func=lambda j: f"{j['id']} · {j['clips']} clips · {j['created']}"
            + ("" if j["has_settings"] else " · (no settings — watermark checks are guesses)"),
        )

with st.expander("Rules that will be checked"):
    st.json(chosen.get("rules", {}))
    if chosen.get("notes"):
        st.caption(chosen["notes"])
    if st.button("🗑 Delete this campaign"):
        if call("DELETE", f"/api/campaigns/{chosen['id']}"):
            st.session_state.pop("camp_report", None)
            st.rerun()

if not jobdirs:
    st.warning("No finished jobs on disk — generate shorts on the home page first.")
    st.stop()

audit_clicked = st.button("🔎 Audit every clip against these rules", type="primary",
                          use_container_width=True)
if audit_clicked:
    report = call("POST", f"/api/campaigns/{chosen['id']}/analyze",
                  json={"job_id": job_label["id"]})
    if report:
        st.session_state["camp_report"] = report

# ---------------------------------------------------------------- report ----
report = st.session_state.get("camp_report")
if not report:
    st.info("Run the audit to see per-clip verdicts and ready-to-post fixes.")
    st.stop()

camp = report["campaign"]
st.divider()
st.subheader(f"Audit — {camp['name']} × `{report['job']}`")
m = st.columns(4)
m[0].metric("Score", f"{report['score']}/100")
m[1].metric("Verdict", report["grade"])
m[2].metric("Clips audited", report["clips_audited"])
m[3].metric("Rules checked", len(report["active_rules"]))

if report["failing_rules"]:
    st.markdown("**Rules failing the most:**")
    st.table(
        [{"Rule": r["rule"], "Failing clips": ", ".join(r["clips"]), "Fails": r["fails"]}
         for r in report["failing_rules"]]
    )

ICON = {"pass": "✅", "watch": "👀", "fail": "❌"}
for clip in report["clips"]:
    c = clip["clip"]
    title = f" — « {c['title']} »" if c["title"] else ""
    with st.expander(f"Clip {c['index']:02d}{title} · {clip['score']}/100 · {c['duration']:.0f}s"):
        st.dataframe(
            [
                {"Rule": ch["rule"], "": ICON[ch["status"]], "Evidence": ch["evidence"],
                 "Fix": ch["suggestion"]}
                for ch in clip["checks"]
            ],
            hide_index=True, use_container_width=True,
        )
        fixes = clip["suggested_fixes"]
        st.markdown("**Ready-to-post corrections** (apply these and the clip is compliant):")
        f1, f2 = st.columns(2)
        f1.code(fixes["instagram_caption"], language=None)
        f2.code(fixes["youtube_title"], language=None)
        st.code(fixes["youtube_description"], language=None)

# The markdown report is served as text/plain — fetch it raw, not as JSON.
try:
    md_resp = httpx.get(
        f"{api_base()}/api/campaigns/{camp['id']}/reports/{report['job']}",
        params={"format": "md"}, timeout=60.0,
    )
except httpx.HTTPError:
    md_resp = None
if md_resp is not None and md_resp.status_code == 200:
    st.download_button(
        "⬇ Download the full audit report (markdown)",
        data=md_resp.content,
        file_name=f"campaign_{camp['id']}_{report['job']}.md",
        mime="text/markdown",
        use_container_width=True,
    )
