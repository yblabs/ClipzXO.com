"""Campaign rules engine: store rule sets, audit every clip, suggest fixes."""
from __future__ import annotations

import json

import pytest

from app import config
from app.pipeline import campaigns, publish
from tests.helpers import make_transcript


@pytest.fixture()
def camp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    return tmp_path / "campaigns"


TRANSCRIPT = make_transcript([
    ("Hey everyone", 0.0, 2.0),
    ("the Chiefs tailgate went completely insane", 2.0, 6.0),
    ("nobody expected the grill to explode like that", 6.0, 11.0),
    ("this was the best tailgate of the year", 11.0, 16.0),
])


def _meta(title="Chiefs tailgate explodes", start=0.0, end=16.0):
    return publish.build_report(TRANSCRIPT.words, start, end, title=title)


def _campaign(**over):
    data = {
        "name": over.pop("name", "Whop \u2014 TradeVault"),
        "brand": over.pop("brand", "tradevault"),
        "platform": over.pop("platform", "whop"),
        "rules": over.pop("rules", {}),
    }
    return campaigns.normalize_campaign(data)


# --------------------------------------------------------------------------- #
# Schema + persistence
# --------------------------------------------------------------------------- #
def test_normalize_campaign_slug_and_platform_fallback():
    c = campaigns.normalize_campaign({"name": "Whop \u2014 TradeVault", "platform": "snapchat"})
    assert c["id"] == "whop-tradevault"
    assert c["platform"] == "other"
    assert c["rules"] == campaigns.RULE_DEFAULTS or all(
        k in c["rules"] for k in campaigns.RULE_DEFAULTS)


def test_normalize_campaign_requires_name():
    with pytest.raises(ValueError):
        campaigns.normalize_campaign({"name": "   "})


def test_normalize_rules_accepts_comma_strings_and_normalises_tags():
    rules = campaigns.normalize_rules({
        "required_title_words": "TradeVault, NFL",
        "required_hashtags": "TradeVault, #Trading",
        "title_max_chars": "100",
        "min_duration": "15",
        "watermark_required": 1,
    })
    assert rules["required_title_words"] == ["tradevault", "nfl"]
    assert rules["required_hashtags"] == ["#tradevault", "#trading"]
    assert rules["title_max_chars"] == 100
    assert rules["min_duration"] == 15.0
    assert rules["watermark_required"] is True


def test_normalize_rules_rejects_unknown_position_and_junk_numbers():
    rules = campaigns.normalize_rules({"watermark_position": "middle", "title_max_chars": "abc"})
    assert rules["watermark_position"] == ""
    assert rules["title_max_chars"] == 0


def test_save_load_list_delete(camp_dir):
    saved = campaigns.save_campaign({"name": "Whop \u2014 TradeVault",
                                     "rules": {"max_duration": 60}})
    assert saved["id"] == "whop-tradevault"
    assert (camp_dir / "whop-tradevault.json").is_file()

    loaded = campaigns.load_campaign("whop-tradevault")
    assert loaded["rules"]["max_duration"] == 60.0

    assert [c["id"] for c in campaigns.list_campaigns()] == ["whop-tradevault"]
    assert campaigns.delete_campaign("whop-tradevault") is True
    assert campaigns.delete_campaign("whop-tradevault") is False
    with pytest.raises(FileNotFoundError):
        campaigns.load_campaign("whop-tradevault")


# --------------------------------------------------------------------------- #
# Free-text parser (paste the campaign page → structured rules)
# --------------------------------------------------------------------------- #
def test_parse_rules_text_recognises_the_common_patterns():
    text = (
        'Title must contain "TradeVault". Max 60 seconds, minimum 15 seconds. '
        "Title under 80 characters. Include #tradevault #trading. Avoid #giveaway. "
        "Do not say guaranteed or risk free. Watermark bottom-right required. "
        "Captions required. CTA asking for a comment."
    )
    out = campaigns.parse_rules_text(text)
    rules = out["rules"]
    assert "tradevault" in rules["required_title_words"]
    assert "#tradevault" in rules["required_hashtags"] and "#trading" in rules["required_hashtags"]
    assert "#giveaway" in rules["banned_hashtags"]
    assert rules["max_duration"] == 60.0 and rules["min_duration"] == 15.0
    assert rules["title_max_chars"] == 80
    assert "guaranteed" in rules["banned_words"] and "risk free" in rules["banned_words"]
    assert rules["watermark_required"] is True
    assert rules["watermark_position"] == "bottom-right"
    assert rules["captions_required"] is True
    assert rules["cta_required"] is True
    assert out["found"]                                  # human-readable notes


def test_parse_rules_text_recognises_nothing():
    out = campaigns.parse_rules_text("just some words about a video")
    assert out["found"] == []
    assert out["rules"] == campaigns.normalize_rules({})


def test_banned_hashtag_never_lands_in_required():
    # regression: the ban regex used to swallow the sentence period
    # ("#giveaway."), so the clean tag slipped past the required/banned filter
    out = campaigns.parse_rules_text("Include #tradevault. Avoid #giveaway.")
    assert out["rules"]["banned_hashtags"] == ["#giveaway"]
    assert "#giveaway" not in out["rules"]["required_hashtags"]
    assert out["rules"]["required_hashtags"] == ["#tradevault"]


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def test_required_title_word_missing_fails_with_brand_fix():
    meta = _meta()
    bad = campaigns.check_title_words(["tradevault"], meta, "tradevault")
    assert bad["status"] == "fail"
    assert "tradevault" in bad["suggestion"].lower()

    ok = campaigns.check_title_words(["chiefs"], meta, "")
    assert ok["status"] == "pass"


def test_banned_title_words_flagged():
    meta = _meta(title="Chiefs tailgate guaranteed insane")
    r = campaigns.check_banned_title_words(["guaranteed"], meta)
    assert r["status"] == "fail"
    assert "guaranteed" in r["evidence"]
    assert campaigns.check_banned_title_words(["zzz"], meta)["status"] == "pass"


def test_title_length_campaign_cap_vs_platform_cap():
    meta = _meta(title="x " * 60)                    # 119 chars
    assert campaigns.check_title_length(0, meta, "whop")["status"] == "pass"   # ≤ YT cap
    assert campaigns.check_title_length(100, meta, "whop")["status"] == "pass"
    r = campaigns.check_title_length(60, meta, "whop")
    assert r["status"] == "fail"
    assert "60" in r["suggestion"]


def test_hashtag_rules():
    meta = _meta()
    rules = campaigns.normalize_rules({"required_hashtags": ["#tradevault"]})
    r = campaigns.check_hashtags(rules, meta)[0]
    assert r["status"] == "fail"
    assert "#tradevault" in r["suggestion"]

    rules = campaigns.normalize_rules({"banned_hashtags": ["#insane"]})
    r = campaigns.check_hashtags(rules, meta)[0]
    assert r["status"] == "fail"                     # "insane" is a spoken keyword
    assert "#insane" in r["suggestion"]

    rules = campaigns.normalize_rules({"max_hashtags": 2})
    r = campaigns.check_hashtags(rules, meta)[0]
    assert r["status"] == "fail"
    assert r["suggestion"].count("#") == 2           # exact trimmed set to keep


def test_description_word_and_cta_rules():
    meta = _meta()
    rules = campaigns.normalize_rules({"required_description_words": ["tradevault.com"]})
    r = campaigns.check_description(rules, meta)[0]
    assert r["status"] == "fail"
    assert "tradevault.com" in r["suggestion"]

    # CTA always present in generated metadata (the build_cta line)
    rules = campaigns.normalize_rules({"cta_required": True})
    assert campaigns.check_description(rules, meta)[0]["status"] == "pass"


def test_duration_rules_compute_exact_trims():
    rules = campaigns.normalize_rules({"min_duration": 30.0, "max_duration": 60.0})
    rs = campaigns.check_duration(rules, 16.0)
    assert rs[0]["status"] == "fail" and "14.0s" in rs[0]["suggestion"]

    rules = campaigns.normalize_rules({"max_duration": 12.0})
    r = campaigns.check_duration(rules, 16.0)[0]
    assert r["status"] == "fail" and "4.0s" in r["suggestion"]


def test_banned_words_found_with_timestamps():
    rules = campaigns.normalize_rules({"banned_words": ["insane"]})
    r = campaigns.check_banned_words(rules["banned_words"], TRANSCRIPT.words, 0.0, 16.0)
    assert r["status"] == "fail"
    assert "+5.3s" in r["evidence"]
    clean = campaigns.check_banned_words(["zzzz"], TRANSCRIPT.words, 0.0, 16.0)
    assert clean["status"] == "pass"


def test_watermark_checks_read_render_settings():
    rules = campaigns.normalize_rules({"watermark_required": True,
                                       "watermark_position": "bottom-right"})
    assert campaigns.check_watermark(rules, None)[0]["status"] == "watch"
    assert campaigns.check_watermark(rules, {"watermark": False})[0]["status"] == "fail"
    wrong = campaigns.check_watermark(
        rules, {"watermark": True, "watermark_position": "top-left"})[0]
    assert wrong["status"] == "fail" and "bottom-right" in wrong["suggestion"]
    ok = campaigns.check_watermark(
        rules, {"watermark": True, "watermark_position": "bottom-right"})[0]
    assert ok["status"] == "pass"


def test_captions_and_language_checks():
    rules = campaigns.normalize_rules({"captions_required": True})
    assert campaigns.check_captions(rules, None)[0]["status"] == "watch"
    assert campaigns.check_captions(rules, {"caption_mode": "static"})[0]["status"] == "pass"

    rules = campaigns.normalize_rules({"language": "en"})
    assert campaigns.check_language(rules, "en")[0]["status"] == "pass"
    assert campaigns.check_language(rules, "hi")[0]["status"] == "fail"


# --------------------------------------------------------------------------- #
# Per-clip audit + fixes
# --------------------------------------------------------------------------- #
def test_audit_clip_all_rules_pass_scores_100():
    campaign = _campaign(rules={
        "required_title_words": ["chiefs"], "required_hashtags": ["#chiefs"],
        "banned_hashtags": ["#giveaway"], "required_description_words": ["chiefs"],
        "min_duration": 10.0, "max_duration": 30.0,
        "watermark_required": True, "watermark_position": "bottom-right",
        "captions_required": True, "cta_required": True, "language": "en",
        "title_max_chars": 100,
    })
    meta = _meta()
    clip = campaigns.audit_clip(
        campaign, meta=meta, words=TRANSCRIPT.words, start=0.0, end=16.0,
        language="en", settings={"watermark": True, "watermark_position": "bottom-right",
                                 "caption_mode": "static"},
    )
    assert clip["score"] == 100
    assert all(c["status"] == "pass" for c in clip["checks"])


def test_audit_clip_suggestions_produce_compliant_metadata():
    campaign = _campaign(rules={
        "required_title_words": ["tradevault"],
        "required_hashtags": ["#tradevault"],
        "required_description_words": ["tradevault.com"],
        "banned_hashtags": ["#insane"],
        "banned_words": ["insane"],
    })
    meta = _meta()
    clip = campaigns.audit_clip(
        campaign, meta=meta, words=TRANSCRIPT.words, start=0.0, end=16.0,
        language="en", settings=None,
    )
    assert clip["score"] < 100
    fixes = clip["suggested_fixes"]
    assert fixes["instagram_caption"].startswith("Tradevault \u2014")
    assert "#tradevault" in fixes["instagram_hashtags"]
    assert "#insane" not in fixes["instagram_hashtags"]      # banned tag stripped
    assert "tradevault.com" in fixes["youtube_description"]
    assert len(fixes["youtube_title"]) <= publish.YT_TITLE_MAX


# --------------------------------------------------------------------------- #
# Rules from FILES (PDF / TXT / DOCX / JSON)
# --------------------------------------------------------------------------- #
def _minimal_pdf(text: str) -> bytes:
    """A tiny valid single-page PDF whose content stream says `text`."""
    safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    content = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    return bytes(out)


def test_read_rules_file_pdf():
    pdf = _minimal_pdf(
        'Title must contain "TradeVault". Max 60 seconds. Include #tradevault.'
    )
    out = campaigns.read_rules_file("campaign.pdf", pdf)
    assert out["kind"] == "pdf"
    assert "tradevault" in out["rules"]["required_title_words"]
    assert out["rules"]["max_duration"] == 60.0
    assert "#tradevault" in out["rules"]["required_hashtags"]
    assert out["text"].strip()


def test_read_rules_file_txt():
    out = campaigns.read_rules_file(
        "rules.txt", b"Watermark bottom-right required. Captions required."
    )
    assert out["rules"]["watermark_required"] is True
    assert out["rules"]["watermark_position"] == "bottom-right"
    assert out["rules"]["captions_required"] is True
    assert "watermark required" in out["found"]


def test_read_rules_file_docx():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>"
            "Max 45 seconds. Avoid #giveaway."
            "</w:t></w:r></w:p></w:body></w:document>",
        )
    out = campaigns.read_rules_file("brief.docx", buf.getvalue())
    assert out["kind"] == "docx"
    assert out["rules"]["max_duration"] == 45.0
    assert "#giveaway" in out["rules"]["banned_hashtags"]


def test_read_rules_file_json_structured():
    payload = {"name": "X", "rules": {"required_title_words": ["acme"],
                                        "max_duration": 30}}
    out = campaigns.read_rules_file("camp.json", json.dumps(payload).encode())
    assert out["kind"] == "json"
    assert out["rules"]["required_title_words"] == ["acme"]
    assert out["rules"]["max_duration"] == 30.0
    assert out["found"]


def test_read_rules_file_rejects_bad_input():
    with pytest.raises(ValueError):
        campaigns.read_rules_file("evil.exe", b"whatever")
    with pytest.raises(ValueError):
        campaigns.read_rules_file("empty.txt", b"")


# --------------------------------------------------------------------------- #
# Job-level analysis (from disk) + jobdir helpers
# --------------------------------------------------------------------------- #
def _workdir(tmp_path, *, settings=True):
    wd = tmp_path / "jobs" / "job123"
    wd.mkdir(parents=True)
    (wd / "transcript.json").write_text(
        json.dumps(TRANSCRIPT.to_dict()), encoding="utf-8")
    (wd / "highlights.json").write_text(json.dumps({
        "provider": "heuristic",
        "highlights": [{"start": 0.0, "end": 16.0, "title": "Chiefs tailgate explodes"}],
    }), encoding="utf-8")
    if settings:
        (wd / "settings.json").write_text(json.dumps({
            "watermark": True, "watermark_position": "bottom-right",
            "caption_mode": "static",
        }), encoding="utf-8")
    return wd


def test_analyze_job_writes_both_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    wd = _workdir(tmp_path)
    campaign = _campaign(rules={"required_title_words": ["tradevault"],
                                "watermark_required": True})
    report = campaigns.analyze_job(campaign, wd)

    assert report["clips_audited"] == 1
    assert 0 <= report["score"] <= 100
    assert report["failing_rules"] and report["failing_rules"][0]["rule"] == "required_title_words"
    assert (wd / "campaign_whop-tradevault_report.json").is_file()
    md = (wd / "campaign_whop-tradevault_report.md").read_text(encoding="utf-8")
    assert "Ready-to-post corrections" in md and "Tradevault" in md


def test_analyze_job_works_without_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    wd = _workdir(tmp_path, settings=False)
    campaign = _campaign(rules={"watermark_required": True})
    report = campaigns.analyze_job(campaign, wd)
    statuses = [c["status"] for c in report["clips"][0]["checks"]
                if c["rule"] == "watermark"]
    assert statuses == ["watch"]                       # can't verify old jobs


def test_analyze_job_requires_a_finished_job(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        campaigns.analyze_job(_campaign(), empty)


def test_job_workdir_rejects_traversal():
    for bad in ("", "..", "../evil", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            campaigns.job_workdir(bad)


def test_report_path_and_list_jobdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    wd = _workdir(tmp_path)
    (tmp_path / "jobs" / "halfdone").mkdir()
    (tmp_path / "jobs" / "halfdone" / "transcript.json").write_text("{}")

    assert campaigns.report_path("whop-tradevault", "job123").name == \
        "campaign_whop-tradevault_report.json"
    assert campaigns.report_path("whop-tradevault", "job123", markdown=True) == \
        wd / "campaign_whop-tradevault_report.md"

    dirs = campaigns.list_jobdirs()
    assert [j["id"] for j in dirs] == ["job123"]       # halfdone has no highlights
    assert dirs[0]["clips"] == 1 and dirs[0]["has_settings"] is True
