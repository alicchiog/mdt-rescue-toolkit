"""Atomic tests for :mod:`mdt_rescue.orchestrator`.

All tests are mock-based and run in well under one second total.  They
exercise:

P1. artifact plan & output_dir convention
P2. order of engine/ffmpeg calls in the happy path
P3. ffmpeg arguments bit-exact vs the legacy ``.sh`` script
P4. progress event emission
P5. cancellation
P6. operational failure paths (preflight + stage errors)

No real ``.mdt`` files, no real ffmpeg, no real engine invocations are
performed.  Bit-exact validation against the B.0a baseline is the job
of B.7 smoke tests (intentionally NOT covered here).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdt_rescue.orchestrator import (
    CancelToken,
    ProgressEvent,
    ProgressStatus,
    RecoveryCancelledError,
    RecoveryPreflightError,
    RecoveryResult,
    RecoveryStageError,
    Stage,
    _ARTIFACT_KEYS,
    _FFMPEG_MUX_AV_ARGS,
    _FFMPEG_RAW_TO_WAV_ARGS,
    _FFMPEG_REF_TO_ANNEXB_ARGS,
    _FFMPEG_WRAP_VIDEO_MOV_ARGS,
    recover,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Create non-empty placeholder MDT + reference files."""
    mdt = tmp_path / "broken.mdt"
    mov = tmp_path / "reference.mov"
    mdt.write_bytes(b"\x00" * 1024)
    mov.write_bytes(b"\x00" * 1024)
    return mdt, mov


def _ok_subprocess(*args, **_kwargs):
    """A stand-in for :func:`subprocess.run` returning a 0 exit code.

    Also creates the ``-y <out>`` output file referenced as the last
    positional argument in the ffmpeg argv, so downstream stages that
    consume it (e.g. CONCAT_PREFIX consumes sane_annexb output,
    MUX_AV consumes video_only_mov output) find the file on disk.
    """
    argv = args[0] if args else []
    if argv and isinstance(argv, list) and argv[0] == "ffmpeg":
        out_path = argv[-1]
        try:
            Path(out_path).write_bytes(b"")
        except OSError:
            pass
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _make_engine_writer(output_kwarg: str):
    """Build a mock engine callable that creates its output file.

    The orchestrator passes ``output_path=...`` to each engine; this
    helper extracts the destination from kwargs and writes an empty
    placeholder there, mimicking real engine I/O without doing actual
    work.
    """

    def _writer(*_args, **kwargs):
        out = kwargs.get(output_kwarg)
        if out is not None:
            try:
                Path(out).write_bytes(b"")
            except OSError:
                pass

    return _writer


def _patch_happy_path():
    """Patch the 4 engine entry points + subprocess + shutil so a full
    ``recover()`` call succeeds without touching the filesystem heavily.

    Engine mocks create their output files on disk so downstream stages
    (CONCAT_PREFIX, ffmpeg wrap/mux) find their inputs.
    """
    return (
        patch("mdt_rescue.orchestrator.shutil.which",
              side_effect=lambda c: f"/usr/bin/{c}"),
        patch("mdt_rescue.orchestrator.shutil.disk_usage",
              return_value=MagicMock(free=10 * 1024 ** 3)),
        patch("mdt_rescue.orchestrator.subprocess.run",
              side_effect=_ok_subprocess),
        patch("mdt_rescue.orchestrator.extract_and_write_prefix",
              side_effect=_make_engine_writer("output_path")),
        patch("mdt_rescue.orchestrator.extract_video",
              side_effect=_make_engine_writer("output_path")),
        patch("mdt_rescue.orchestrator.extract_audio",
              side_effect=_make_engine_writer("output_path")),
        patch("mdt_rescue.orchestrator.verify_mov",
              return_value=MagicMock(name="VerifyResult")),
    )


def _run_happy_path(tmp_path: Path, **kwargs) -> RecoveryResult:
    """Convenience helper running recover() with all engine mocks."""
    mdt, mov = _make_inputs(tmp_path)
    patches = _patch_happy_path()
    started = [p.start() for p in patches]
    try:
        return recover(
            mdt_path=mdt,
            reference_mov_path=mov,
            output_dir=tmp_path / "out",
            **kwargs,
        )
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# P1: artifact plan & output_dir convention
# ---------------------------------------------------------------------------


class TestArtifactPlanAndOutputDir:
    def test_output_dir_default_convention_with_no_timestamp(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        patches = _patch_happy_path()
        for p in patches:
            p.start()
        try:
            result = recover(mdt_path=mdt, reference_mov_path=mov)
        finally:
            for p in patches:
                p.stop()
        assert result.output_dir == tmp_path / "recovery_output_broken"
        assert "_timestamp" not in str(result.output_dir)

    def test_artifact_paths_match_sh_basename_pattern(self, tmp_path):
        result = _run_happy_path(tmp_path)
        out = tmp_path / "out"
        assert result.artifacts["sane_annexb"] == out / "broken_sane_annexb.h264"
        assert result.artifacts["sps_pps_prefix"] == out / "broken_sps_pps_prefix.h264"
        assert result.artifacts["video_h264"] == out / "broken_video.h264"
        assert result.artifacts["video_with_header"] == out / "broken_video_with_header.h264"
        assert result.artifacts["video_only_mov"] == out / "broken_video_only.mov"
        assert result.artifacts["audio_raw"] == out / "broken_audio.raw"
        assert result.artifacts["audio_wav"] == out / "broken_audio.wav"
        assert result.artifacts["rescued_mov"] == out / "broken_RESCUED.mov"

    def test_artifact_keys_are_the_8_canonical_names(self, tmp_path):
        result = _run_happy_path(tmp_path)
        assert set(result.artifacts.keys()) == set(_ARTIFACT_KEYS)
        assert len(result.artifacts) == 8

    def test_explicit_output_dir_overrides_default(self, tmp_path):
        result = _run_happy_path(tmp_path)
        assert result.output_dir == tmp_path / "out"
        assert result.log_path == tmp_path / "out" / "recovery_log.txt"


# ---------------------------------------------------------------------------
# P2: order of engine/ffmpeg calls
# ---------------------------------------------------------------------------


class TestEngineAndFfmpegCallOrder:
    def test_happy_path_calls_engine_sps_pps_extract(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")) as m_sps, \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_audio",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            recover(mdt_path=mdt, reference_mov_path=mov,
                    output_dir=tmp_path / "out")
        m_sps.assert_called_once()

    def test_happy_path_calls_engine_video_extract(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=_make_engine_writer("output_path")) as m_video, \
             patch("mdt_rescue.orchestrator.extract_audio",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            recover(mdt_path=mdt, reference_mov_path=mov,
                    output_dir=tmp_path / "out")
        m_video.assert_called_once()

    def test_happy_path_calls_engine_audio_extract(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_audio",
                   side_effect=_make_engine_writer("output_path")) as m_audio, \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            recover(mdt_path=mdt, reference_mov_path=mov,
                    output_dir=tmp_path / "out")
        m_audio.assert_called_once()

    def test_happy_path_calls_engine_verify(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_audio",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()) as m_verify:
            recover(mdt_path=mdt, reference_mov_path=mov,
                    output_dir=tmp_path / "out")
        m_verify.assert_called_once()

    def test_happy_path_calls_subprocess_run_ffmpeg_4_times(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess) as m_sub, \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_audio",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            recover(mdt_path=mdt, reference_mov_path=mov,
                    output_dir=tmp_path / "out")
        assert m_sub.call_count == 4
        for call in m_sub.call_args_list:
            args = call.args[0]
            assert args[0] == "ffmpeg"


# ---------------------------------------------------------------------------
# P3: ffmpeg args bit-exact vs the legacy .sh
# ---------------------------------------------------------------------------


class TestFfmpegArgsBitExact:
    """Each test asserts a specific ffmpeg invocation has the exact
    arguments documented in B.6.AUDIT.4 of the legacy script."""

    def test_ref_to_annexb_args_match_sh(self):
        # recover_gh5s_fhd25_alli.sh line 224:
        #   ffmpeg -y -i "$SANE_MOV" -map 0:v:0 -c copy
        #     -bsf:v h264_mp4toannexb -f h264 "$sane_annexb"
        assert _FFMPEG_REF_TO_ANNEXB_ARGS == (
            "-y", "-i", "{ref}",
            "-map", "0:v:0", "-c", "copy",
            "-bsf:v", "h264_mp4toannexb", "-f", "h264",
            "{out}",
        )

    def test_wrap_video_mov_args_match_sh(self):
        # recover_gh5s_fhd25_alli.sh lines 274-280:
        #   ffmpeg -y -fflags +genpts -framerate 25
        #     -f h264 -i "$VIDEO_WITH_HEADER" -c copy
        #     -video_track_timescale 25000 "$video_only_mov"
        assert _FFMPEG_WRAP_VIDEO_MOV_ARGS == (
            "-y", "-fflags", "+genpts",
            "-framerate", "25",
            "-f", "h264", "-i", "{video_with_header}",
            "-c", "copy",
            "-video_track_timescale", "25000",
            "{out}",
        )

    def test_raw_to_wav_args_match_sh(self):
        # recover_gh5s_fhd25_alli.sh line 300:
        #   ffmpeg -y -f s16be -ar 48000 -ac 2 -i "$audio_raw" "$audio_wav"
        assert _FFMPEG_RAW_TO_WAV_ARGS == (
            "-y", "-f", "s16be",
            "-ar", "48000", "-ac", "2",
            "-i", "{audio_raw}",
            "{out}",
        )

    def test_mux_av_args_match_sh(self):
        # recover_gh5s_fhd25_alli.sh lines 315-321:
        #   ffmpeg -y -i "$video_only_mov" -i "$audio_raw"
        #     -c:v copy -c:a pcm_s16be "$final_mov"
        assert _FFMPEG_MUX_AV_ARGS == (
            "-y", "-i", "{video_only_mov}",
            "-i", "{audio_raw}",
            "-c:v", "copy", "-c:a", "pcm_s16be",
            "{out}",
        )


# ---------------------------------------------------------------------------
# P4: progress event emission
# ---------------------------------------------------------------------------


class TestProgressEmission:
    def test_progress_emits_11_stages_in_order(self, tmp_path):
        events: list[ProgressEvent] = []
        _run_happy_path(tmp_path, progress=events.append)
        completed = [
            e.stage for e in events
            if e.status == ProgressStatus.COMPLETE
        ]
        assert completed == [
            Stage.PREFLIGHT_DEPS,
            Stage.PREFLIGHT_INPUTS,
            Stage.PREFLIGHT_DISK,
            Stage.PREFLIGHT_OUTPUT_DIR,
            Stage.EXTRACT_SPS_PPS,
            Stage.EXTRACT_VIDEO,
            Stage.CONCAT_PREFIX,
            Stage.WRAP_VIDEO_MOV,
            Stage.EXTRACT_AUDIO,
            Stage.MUX_AV,
            Stage.VERIFY,
        ]

    def test_progress_emits_start_and_complete_for_each_stage(self, tmp_path):
        events: list[ProgressEvent] = []
        _run_happy_path(tmp_path, progress=events.append)
        for stage in Stage:
            statuses = [
                e.status for e in events if e.stage == stage
            ]
            assert ProgressStatus.START in statuses, (
                f"missing START for {stage}"
            )
            assert ProgressStatus.COMPLETE in statuses, (
                f"missing COMPLETE for {stage}"
            )

    def test_progress_event_fields_populated(self, tmp_path):
        events: list[ProgressEvent] = []
        _run_happy_path(tmp_path, progress=events.append)
        for ev in events:
            assert isinstance(ev, ProgressEvent)
            assert isinstance(ev.stage, Stage)
            assert isinstance(ev.status, ProgressStatus)
            assert ev.timestamp is not None
            assert ev.timestamp.tzinfo is not None
            assert isinstance(ev.message, str) and ev.message

    def test_no_progress_callback_does_not_crash(self, tmp_path):
        result = _run_happy_path(tmp_path, progress=None)
        assert result.success is True


# ---------------------------------------------------------------------------
# P5: cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_before_recover_returns_cancelled_result(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        token = CancelToken()
        token.cancel()
        with patch("mdt_rescue.orchestrator.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            result = recover(
                mdt_path=mdt, reference_mov_path=mov,
                output_dir=tmp_path / "out",
                cancel_token=token,
            )
        assert result.success is False
        assert result.cancelled is True
        assert isinstance(result.error, RecoveryCancelledError)

    def test_cancel_mid_pipeline_stops_at_next_stage(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        token = CancelToken()
        sps_writer = _make_engine_writer("output_path")

        def cancel_after_video_extract(*args, **kwargs):
            sps_writer(*args, **kwargs)
            token.cancel()

        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=sps_writer), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=cancel_after_video_extract), \
             patch("mdt_rescue.orchestrator.extract_audio") as m_audio, \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            result = recover(
                mdt_path=mdt, reference_mov_path=mov,
                output_dir=tmp_path / "out",
                cancel_token=token,
            )
        assert result.success is False
        assert result.cancelled is True
        assert m_audio.call_count == 0
        assert Stage.EXTRACT_VIDEO in result.stages_completed

    def test_cancel_token_idempotent(self):
        token = CancelToken()
        assert token.is_set is False
        token.cancel()
        token.cancel()
        token.cancel()
        assert token.is_set is True
        with pytest.raises(RecoveryCancelledError):
            token.raise_if_set()


# ---------------------------------------------------------------------------
# P6.a: preflight failures
# ---------------------------------------------------------------------------


class TestPreflightFailures:
    def test_missing_mdt_returns_recovery_preflight_error(self, tmp_path):
        mov = tmp_path / "reference.mov"
        mov.write_bytes(b"\x00" * 1024)
        nonexistent_mdt = tmp_path / "does_not_exist.mdt"
        with patch("mdt_rescue.orchestrator.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            result = recover(
                mdt_path=nonexistent_mdt,
                reference_mov_path=mov,
                output_dir=tmp_path / "out",
            )
        assert result.success is False
        assert result.cancelled is False
        assert isinstance(result.error, RecoveryPreflightError)
        assert result.stage_failed == Stage.PREFLIGHT_INPUTS

    def test_missing_reference_returns_recovery_preflight_error(self, tmp_path):
        mdt = tmp_path / "broken.mdt"
        mdt.write_bytes(b"\x00" * 1024)
        nonexistent_mov = tmp_path / "does_not_exist.mov"
        with patch("mdt_rescue.orchestrator.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            result = recover(
                mdt_path=mdt,
                reference_mov_path=nonexistent_mov,
                output_dir=tmp_path / "out",
            )
        assert result.success is False
        assert isinstance(result.error, RecoveryPreflightError)
        assert result.stage_failed == Stage.PREFLIGHT_INPUTS

    def test_insufficient_disk_returns_recovery_preflight_error(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage",
                   return_value=MagicMock(free=0)):
            result = recover(
                mdt_path=mdt,
                reference_mov_path=mov,
                output_dir=tmp_path / "out",
            )
        assert result.success is False
        assert isinstance(result.error, RecoveryPreflightError)
        assert result.stage_failed == Stage.PREFLIGHT_DISK


# ---------------------------------------------------------------------------
# P6.b: stage failures (engine exception, ffmpeg non-zero exit)
# ---------------------------------------------------------------------------


class TestStageFailures:
    def test_engine_video_exception_returns_recovery_stage_error(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)
        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=_ok_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix",
                   side_effect=_make_engine_writer("output_path")), \
             patch("mdt_rescue.orchestrator.extract_video",
                   side_effect=RuntimeError("synthetic engine failure")), \
             patch("mdt_rescue.orchestrator.extract_audio"), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            result = recover(
                mdt_path=mdt, reference_mov_path=mov,
                output_dir=tmp_path / "out",
            )
        assert result.success is False
        assert result.cancelled is False
        assert isinstance(result.error, RecoveryStageError)
        assert result.stage_failed == Stage.EXTRACT_VIDEO
        assert "synthetic engine failure" in result.error.detail

    def test_ffmpeg_nonzero_exit_returns_recovery_stage_error(self, tmp_path):
        mdt, mov = _make_inputs(tmp_path)

        def failing_subprocess(*_args, **_kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "ffmpeg synthetic failure"
            return result

        with patch("mdt_rescue.orchestrator.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"), \
             patch("mdt_rescue.orchestrator.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)), \
             patch("mdt_rescue.orchestrator.subprocess.run", side_effect=failing_subprocess), \
             patch("mdt_rescue.orchestrator.extract_and_write_prefix"), \
             patch("mdt_rescue.orchestrator.extract_video"), \
             patch("mdt_rescue.orchestrator.extract_audio"), \
             patch("mdt_rescue.orchestrator.verify_mov", return_value=MagicMock()):
            result = recover(
                mdt_path=mdt, reference_mov_path=mov,
                output_dir=tmp_path / "out",
            )
        assert result.success is False
        assert isinstance(result.error, RecoveryStageError)
        assert result.stage_failed == Stage.EXTRACT_SPS_PPS
