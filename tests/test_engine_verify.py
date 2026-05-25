"""
Tests for ``mdt_rescue.engine.verify``.

These are unit tests with mocked subprocess and filesystem; no real
ffprobe invocation, no binary fixtures. Behavior is verified against
the legacy ``scripts/verify_output.sh`` semantics.

Organized in eight thematic classes, ~44 atomic tests.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdt_rescue.engine.verify import (
    EXPECTATIONS_GH5S_FHD25,
    AudioStreamInfo,
    CheckResult,
    FFprobeNotFoundError,
    InputNotFoundError,
    PlausibilityExpectations,
    VerifyResult,
    VideoStreamInfo,
    _probe_field,
    probe_streams,
    run_plausibility_checks,
    verify_mov,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _mock_ffprobe_run(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a MagicMock that mimics a single subprocess.run() result."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def _eleven_happy_returns() -> list:
    """11 successful ffprobe results, in the order probe_streams() makes them.

    Order:
        video: codec_name, width, height, nb_frames, duration, pix_fmt, profile
        audio: codec_name, sample_rate, channels, duration
    """
    return [
        _mock_ffprobe_run("h264\n"),
        _mock_ffprobe_run("1920\n"),
        _mock_ffprobe_run("1080\n"),
        _mock_ffprobe_run("100\n"),
        _mock_ffprobe_run("4.000000\n"),
        _mock_ffprobe_run("yuv422p10le\n"),
        _mock_ffprobe_run("High 4:2:2 Intra\n"),
        _mock_ffprobe_run("pcm_s16be\n"),
        _mock_ffprobe_run("48000\n"),
        _mock_ffprobe_run("2\n"),
        _mock_ffprobe_run("4.000000\n"),
    ]


# =====================================================================
# Class 1 - probe_streams
# =====================================================================


class TestProbeStreams:
    """Behavior of the ffprobe probing helper."""

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_happy_path_returns_populated_streams(self, mock_run):
        mock_run.side_effect = _eleven_happy_returns()
        video, audio = probe_streams("/fake/path.mov")
        assert video.codec_name == "h264"
        assert video.width == "1920"
        assert video.height == "1080"
        assert video.nb_frames == "100"
        assert video.duration == "4.000000"
        assert video.pix_fmt == "yuv422p10le"
        assert video.profile == "High 4:2:2 Intra"
        assert audio.codec_name == "pcm_s16be"
        assert audio.sample_rate == "48000"
        assert audio.channels == "2"
        assert audio.duration == "4.000000"

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_trailing_newline_is_stripped(self, mock_run):
        mock_run.side_effect = [_mock_ffprobe_run("h264\n")] + [
            _mock_ffprobe_run("X\n") for _ in range(10)
        ]
        video, _ = probe_streams("/fake.mov")
        assert video.codec_name == "h264"
        assert "\n" not in video.codec_name

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_nonzero_exit_returns_empty_string(self, mock_run):
        mock_run.side_effect = [
            _mock_ffprobe_run("garbage-still-on-stdout\n", returncode=1)
        ] + [_mock_ffprobe_run("X\n") for _ in range(10)]
        video, _ = probe_streams("/fake.mov")
        assert video.codec_name == ""

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_oserror_returns_empty_string(self, mock_run):
        mock_run.side_effect = [FileNotFoundError("ffprobe")] + [
            _mock_ffprobe_run("X\n") for _ in range(10)
        ]
        video, _ = probe_streams("/fake.mov")
        assert video.codec_name == ""

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_exactly_eleven_invocations(self, mock_run):
        mock_run.side_effect = _eleven_happy_returns()
        probe_streams("/fake.mov")
        assert mock_run.call_count == 11

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_video_calls_use_v0_selector(self, mock_run):
        mock_run.side_effect = _eleven_happy_returns()
        probe_streams("/fake.mov")
        for i in range(7):
            cmd = mock_run.call_args_list[i][0][0]
            assert "v:0" in cmd, f"call {i} not selecting v:0: {cmd}"

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_audio_calls_use_a0_selector(self, mock_run):
        mock_run.side_effect = _eleven_happy_returns()
        probe_streams("/fake.mov")
        for i in range(7, 11):
            cmd = mock_run.call_args_list[i][0][0]
            assert "a:0" in cmd, f"call {i} not selecting a:0: {cmd}"

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_accepts_path_object(self, mock_run):
        mock_run.side_effect = _eleven_happy_returns()
        video, _ = probe_streams(Path("/fake/movie.mov"))
        assert video.codec_name == "h264"
        cmd = mock_run.call_args_list[0][0][0]
        assert "/fake/movie.mov" in cmd


# =====================================================================
# Class 2 - _probe_field
# =====================================================================


class TestProbeFieldLowLevel:
    """Behavior of the per-field ffprobe wrapper."""

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_command_shape_matches_legacy(self, mock_run):
        mock_run.return_value = _mock_ffprobe_run("h264\n")
        _probe_field("/x.mov", "v:0", "codec_name")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffprobe"
        assert "-v" in cmd and "error" in cmd
        assert "-select_streams" in cmd and "v:0" in cmd
        assert "-show_entries" in cmd and "stream=codec_name" in cmd
        assert "-of" in cmd and "default=nw=1:nk=1" in cmd
        assert cmd[-1] == "/x.mov"

    @patch("mdt_rescue.engine.verify.subprocess.run")
    def test_subprocess_called_with_utf8_replace(self, mock_run):
        mock_run.return_value = _mock_ffprobe_run("h264\n")
        _probe_field("/x.mov", "v:0", "codec_name")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"


# =====================================================================
# Class 3 - run_plausibility_checks
# =====================================================================


class TestRunPlausibilityChecks:
    """Behavior of the plausibility-check engine."""

    def _good_video(self):
        return VideoStreamInfo(
            codec_name="h264",
            width="1920",
            height="1080",
            nb_frames="100",
            duration="4.0",
            pix_fmt="yuv422p10le",
            profile="High 4:2:2 Intra",
        )

    def _good_audio(self):
        return AudioStreamInfo(
            codec_name="pcm_s16be",
            sample_rate="48000",
            channels="2",
            duration="4.0",
        )

    def test_all_pass_happy_path(self):
        checks = run_plausibility_checks(
            self._good_video(), self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        assert all(c.passed for c in checks)

    def test_width_mismatch_fails_only_that_check(self):
        v = dataclasses.replace(self._good_video(), width="3840")
        checks = run_plausibility_checks(v, self._good_audio(), EXPECTATIONS_GH5S_FHD25)
        by_label = {c.label: c for c in checks}
        assert by_label["width"].passed is False
        assert by_label["width"].actual == "3840"
        assert by_label["width"].expected == "1920"
        for lbl in ("video codec", "height", "pix_fmt", "audio codec", "sample rate", "channels"):
            assert by_label[lbl].passed is True

    def test_audio_codec_mismatch_fails(self):
        a = dataclasses.replace(self._good_audio(), codec_name="aac")
        checks = run_plausibility_checks(self._good_video(), a, EXPECTATIONS_GH5S_FHD25)
        by_label = {c.label: c for c in checks}
        assert by_label["audio codec"].passed is False

    def test_all_fail(self):
        v = VideoStreamInfo()
        a = AudioStreamInfo()
        checks = run_plausibility_checks(v, a, EXPECTATIONS_GH5S_FHD25)
        assert all(c.passed is False for c in checks)
        for c in checks:
            assert c.actual == ""

    def test_empty_actual_fails_check(self):
        v = dataclasses.replace(self._good_video(), pix_fmt="")
        checks = run_plausibility_checks(v, self._good_audio(), EXPECTATIONS_GH5S_FHD25)
        by_label = {c.label: c for c in checks}
        assert by_label["pix_fmt"].passed is False
        assert by_label["pix_fmt"].actual == ""

    def test_custom_expectations(self):
        custom = PlausibilityExpectations(
            video_codec="h265",
            width="3840",
            height="2160",
            pix_fmt="yuv420p10le",
            audio_codec="aac",
            sample_rate="48000",
            channels="2",
        )
        v = VideoStreamInfo(
            codec_name="h265", width="3840", height="2160",
            pix_fmt="yuv420p10le",
        )
        a = AudioStreamInfo(codec_name="aac", sample_rate="48000", channels="2")
        checks = run_plausibility_checks(v, a, custom)
        assert all(c.passed for c in checks)

    def test_returns_exactly_seven_results(self):
        checks = run_plausibility_checks(
            self._good_video(), self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        assert len(checks) == 7

    def test_checks_in_legacy_order(self):
        checks = run_plausibility_checks(
            self._good_video(), self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        labels = [c.label for c in checks]
        assert labels == [
            "video codec",
            "width",
            "height",
            "pix_fmt",
            "audio codec",
            "sample rate",
            "channels",
        ]

    def test_check_labels_are_exact_legacy_strings(self):
        checks = run_plausibility_checks(
            self._good_video(), self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        labels = [c.label for c in checks]
        assert "video codec" in labels
        assert "audio codec" in labels
        assert "sample rate" in labels
        assert "pix_fmt" in labels
        for bad in ("video_codec", "audio_codec", "sample_rate", "videocodec"):
            assert bad not in labels

    def test_string_comparison_no_int_coercion(self):
        v = dataclasses.replace(self._good_video(), width="1920.0")
        checks = run_plausibility_checks(
            v, self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        by_label = {c.label: c for c in checks}
        assert by_label["width"].passed is False
        assert by_label["width"].actual == "1920.0"

    def test_returns_tuple_not_list(self):
        checks = run_plausibility_checks(
            self._good_video(), self._good_audio(), EXPECTATIONS_GH5S_FHD25
        )
        assert isinstance(checks, tuple)


# =====================================================================
# Class 4 - verify_mov (high-level)
# =====================================================================


class TestVerifyMov:
    """End-to-end behavior of verify_mov, with mocked subprocess."""

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_happy_path_all_passed(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffprobe"
        mock_run.side_effect = _eleven_happy_returns()
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake-mov")
        result = verify_mov(mov)
        assert result.all_passed is True
        assert result.video.codec_name == "h264"
        assert result.audio.codec_name == "pcm_s16be"
        assert len(result.checks) == 7

    def test_input_not_found_raises(self, tmp_path):
        ghost = tmp_path / "does-not-exist.mov"
        with pytest.raises(InputNotFoundError) as exc_info:
            verify_mov(ghost)
        assert str(ghost) in str(exc_info.value)

    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_ffprobe_not_in_path_raises(self, mock_which, tmp_path):
        mock_which.return_value = None
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        with pytest.raises(FFprobeNotFoundError):
            verify_mov(mov)

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_default_expectations_used_when_none(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffprobe"
        mock_run.side_effect = _eleven_happy_returns()
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        result = verify_mov(mov, expectations=None)
        assert result.expectations is EXPECTATIONS_GH5S_FHD25

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_custom_expectations_passed_through(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffprobe"
        mock_run.side_effect = _eleven_happy_returns()
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        custom = PlausibilityExpectations(width="9999")
        result = verify_mov(mov, expectations=custom)
        assert result.expectations is custom
        by_label = {c.label: c for c in result.checks}
        assert by_label["width"].passed is False
        assert by_label["width"].expected == "9999"

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_all_passed_false_when_any_check_fails(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffprobe"
        bad = _eleven_happy_returns()
        bad[1] = _mock_ffprobe_run("3840\n")  # width = 3840 instead of 1920
        mock_run.side_effect = bad
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        result = verify_mov(mov)
        assert result.all_passed is False

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_progress_callback_accepted_but_not_called(
        self, mock_which, mock_run, tmp_path
    ):
        mock_which.return_value = "/usr/bin/ffprobe"
        mock_run.side_effect = _eleven_happy_returns()
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        cb = MagicMock()
        verify_mov(mov, progress=cb)
        cb.assert_not_called()

    @patch("mdt_rescue.engine.verify.subprocess.run")
    @patch("mdt_rescue.engine.verify.shutil.which")
    def test_returns_verify_result(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffprobe"
        mock_run.side_effect = _eleven_happy_returns()
        mov = tmp_path / "rescued.mov"
        mov.write_bytes(b"fake")
        result = verify_mov(mov)
        assert isinstance(result, VerifyResult)
        assert isinstance(result.video, VideoStreamInfo)
        assert isinstance(result.audio, AudioStreamInfo)
        assert isinstance(result.checks, tuple)
        assert isinstance(result.input_path, Path)
        assert isinstance(result.expectations, PlausibilityExpectations)


# =====================================================================
# Class 5 - PlausibilityExpectations
# =====================================================================


class TestExpectations:
    """The PlausibilityExpectations dataclass."""

    def test_default_values_match_gh5s_fhd25(self):
        e = PlausibilityExpectations()
        assert e.video_codec == "h264"
        assert e.width == "1920"
        assert e.height == "1080"
        assert e.pix_fmt == "yuv422p10le"
        assert e.audio_codec == "pcm_s16be"
        assert e.sample_rate == "48000"
        assert e.channels == "2"

    def test_frozen(self):
        e = PlausibilityExpectations()
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.width = "3840"

    def test_equality_between_default_instances(self):
        a = PlausibilityExpectations()
        b = PlausibilityExpectations()
        assert a == b

    def test_module_level_constant_is_default_instance(self):
        assert EXPECTATIONS_GH5S_FHD25 == PlausibilityExpectations()


# =====================================================================
# Class 6 - CheckResult
# =====================================================================


class TestCheckResult:
    """The CheckResult dataclass."""

    def test_frozen(self):
        c = CheckResult(label="x", expected="1", actual="1", passed=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.passed = False

    def test_passed_flag(self):
        c_ok = CheckResult(label="x", expected="1", actual="1", passed=True)
        c_bad = CheckResult(label="x", expected="1", actual="2", passed=False)
        assert c_ok.passed is True
        assert c_bad.passed is False

    def test_equality(self):
        c1 = CheckResult(label="x", expected="1", actual="1", passed=True)
        c2 = CheckResult(label="x", expected="1", actual="1", passed=True)
        assert c1 == c2


# =====================================================================
# Class 7 - VideoStreamInfo / AudioStreamInfo
# =====================================================================


class TestStreamInfo:
    """The two raw-ffprobe-data dataclasses."""

    def test_video_stream_info_frozen(self):
        v = VideoStreamInfo()
        with pytest.raises(dataclasses.FrozenInstanceError):
            v.codec_name = "x"

    def test_audio_stream_info_frozen(self):
        a = AudioStreamInfo()
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.codec_name = "x"

    def test_video_stream_info_default_empty_strings(self):
        v = VideoStreamInfo()
        assert v.codec_name == ""
        assert v.width == ""
        assert v.height == ""
        assert v.nb_frames == ""
        assert v.duration == ""
        assert v.pix_fmt == ""
        assert v.profile == ""

    def test_audio_stream_info_default_empty_strings(self):
        a = AudioStreamInfo()
        assert a.codec_name == ""
        assert a.sample_rate == ""
        assert a.channels == ""
        assert a.duration == ""


# =====================================================================
# Class 8 - Exceptions
# =====================================================================


class TestExceptions:
    """The two custom exception classes."""

    def test_ffprobe_not_found_error_is_raisable(self):
        with pytest.raises(FFprobeNotFoundError):
            raise FFprobeNotFoundError("ffprobe")

    def test_input_not_found_error_is_raisable(self):
        with pytest.raises(InputNotFoundError):
            raise InputNotFoundError("/some/path.mov")

    def test_input_not_found_error_carries_path_in_message(self):
        try:
            raise InputNotFoundError("/some/path.mov")
        except InputNotFoundError as e:
            assert "/some/path.mov" in str(e)

    def test_exceptions_do_not_inherit_from_oserror(self):
        assert not issubclass(FFprobeNotFoundError, OSError)
        assert not issubclass(InputNotFoundError, OSError)
        assert issubclass(FFprobeNotFoundError, Exception)
        assert issubclass(InputNotFoundError, Exception)


# =====================================================================
# Class 9 - VerifyResult
# =====================================================================


class TestVerifyResult:
    """The composite VerifyResult dataclass."""

    def _make_result(self, all_passed=True):
        checks = tuple(
            CheckResult(label=f"l{i}", expected="x", actual="x", passed=True)
            for i in range(7)
        )
        return VerifyResult(
            input_path=Path("/x.mov"),
            video=VideoStreamInfo(),
            audio=AudioStreamInfo(),
            checks=checks,
            all_passed=all_passed,
            expectations=EXPECTATIONS_GH5S_FHD25,
        )

    def test_frozen(self):
        r = self._make_result()
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.all_passed = False

    def test_all_passed_true_branch(self):
        r = self._make_result(all_passed=True)
        assert r.all_passed is True

    def test_all_passed_false_branch(self):
        r = self._make_result(all_passed=False)
        assert r.all_passed is False
