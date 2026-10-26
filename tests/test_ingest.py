"""Tests for the source-download fixes: capped format selector + progress hook."""
import pytest

from app.pipeline import ingest


# --------------------------------------------------------------------------- #
# Format selector (DOWNLOAD_MAX_HEIGHT)
# --------------------------------------------------------------------------- #
class TestFormatSelector:
    def test_none_or_zero_is_uncapped(self):
        assert ingest.build_format_selector(None) == ingest.UNCAPPED_FORMAT
        assert ingest.build_format_selector(0) == ingest.UNCAPPED_FORMAT
        assert ingest.build_format_selector(-1) == ingest.UNCAPPED_FORMAT
        assert "height<=" not in ingest.build_format_selector(0)

    def test_default_cap_prefers_landscape_1080p(self):
        sel = ingest.build_format_selector(1080)
        # first choice: best video ≤1080p in mp4 + m4a audio
        assert sel.startswith("bv*[height<=1080][ext=mp4]+ba[ext=m4a]")
        assert "b[height<=1080][ext=mp4]" in sel

    def test_portrait_fallback_doubles_the_cap(self):
        sel = ingest.build_format_selector(1080)
        # 2× cap ⇒ a 9:16 source still reaches a full 1080×1920 frame
        assert "height<=2160" in sel
        # …but only after the landscape tier
        assert sel.index("height<=1080") < sel.index("height<=2160")

    def test_every_ladder_ends_with_last_resort(self):
        sel = ingest.build_format_selector(1080)
        assert sel.endswith("/bv*+ba/b")

    def test_custom_cap_scales_both_tiers(self):
        sel = ingest.build_format_selector(720)
        assert "height<=720" in sel and "height<=1440" in sel
        assert "height<=1080" not in sel

    def test_audio_and_video_stream_tiers_present(self):
        sel = ingest.build_format_selector(1080)
        assert f"bv*[height<=1080]+ba" in sel
        assert f"b[height<=1080]" in sel


# --------------------------------------------------------------------------- #
# yt-dlp progress hook
# --------------------------------------------------------------------------- #
class TestDownloadHook:
    def _capture(self):
        seen: list[tuple[str, float, float]] = []
        return seen, ingest._download_hook(lambda k, d, t: seen.append((k, d, t)))

    def test_video_stream_reported(self):
        seen, hook = self._capture()
        hook({
            "status": "downloading",
            "downloaded_bytes": 500,
            "total_bytes": 1000,
            "info_dict": {"vcodec": "vp9"},
        })
        assert seen == [("video", 500.0, 1000.0)]

    def test_audio_only_stream_detected(self):
        seen, hook = self._capture()
        hook({
            "status": "downloading",
            "downloaded_bytes": 10,
            "total_bytes": 100,
            "info_dict": {"vcodec": "none", "acodec": "mp4a"},
        })
        assert seen == [("audio", 10.0, 100.0)]

    def test_estimate_used_when_exact_total_missing(self):
        seen, hook = self._capture()
        hook({
            "status": "downloading",
            "downloaded_bytes": 42,
            "total_bytes_estimate": 100,
            "info_dict": {},
        })
        assert seen == [("video", 42.0, 100.0)]

    def test_missing_totals_pass_zero(self):
        seen, hook = self._capture()
        hook({"status": "downloading", "downloaded_bytes": 42, "info_dict": {}})
        assert seen == [("video", 42.0, 0.0)]

    def test_non_downloading_statuses_ignored(self):
        seen, hook = self._capture()
        hook({"status": "finished", "info_dict": {}})
        hook({"status": "processing", "info_dict": {}})
        assert seen == []

    def test_missing_info_dict_defaults_to_video(self):
        seen, hook = self._capture()
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
        assert seen == [("video", 1.0, 2.0)]


# --------------------------------------------------------------------------- #
# run_pipeline wiring: on_progress -> stage text + progress bar + milestone log
# --------------------------------------------------------------------------- #
class _PipelineStopped(Exception):
    pass


class TestRunPipelineWiring:
    def test_download_progress_updates_stage_and_bar(self, monkeypatch, tmp_path):
        import app.pipeline.run as run_mod

        calls: list[tuple[float, str]] = []
        logs: list[str] = []

        def fake_stage_source(source, workdir, log, on_progress=None):
            assert on_progress is not None, "run_pipeline must pass a download callback"
            on_progress("video", 500_000_000, 1_000_000_000)  # 50% of 1 GB
            on_progress("audio", 50_000, 100_000)             # 50% of 100 KB
            raise _PipelineStopped

        monkeypatch.setattr(run_mod.ingest, "stage_source", fake_stage_source)

        with pytest.raises(_PipelineStopped):
            run_mod.run_pipeline(
                "https://example.com/v",
                tmp_path,
                progress=lambda p, s: calls.append((p, s)),
                log=logs.append,
            )

        stages = dict((s, p) for p, s in calls)
        assert "Resolving source video" in stages
        video_stage = next(s for s in stages if s.startswith("Downloading video"))
        audio_stage = next(s for s in stages if s.startswith("Downloading audio"))
        assert "50%" in video_stage and "500 MB / 1.00 GB" in video_stage
        assert "50%" in audio_stage
        # video maps into 0.02–0.065, audio into 0.065–0.07 (never backwards)
        assert stages[video_stage] == pytest.approx(0.02 + 0.045 * 0.5)
        assert stages[audio_stage] == pytest.approx(0.065 + 0.005 * 0.5)
        assert stages["Resolving source video"] == 0.02
        # 50% crosses a quarter milestone -> one log line
        assert any(line.startswith("Download 50%") for line in logs)
