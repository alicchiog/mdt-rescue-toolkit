"""
mdt_rescue.orchestrator
=======================

High-level recovery driver.  Composes the four engine primitives
(:mod:`mdt_rescue.engine.sps_pps`, :mod:`mdt_rescue.engine.video`,
:mod:`mdt_rescue.engine.audio`, :mod:`mdt_rescue.engine.verify`) plus
four ffmpeg invocations into a complete recovery flow, usable by both
the legacy CLI and the future GUI.

Public API
----------
- :func:`recover` -- main entry point.
- :class:`Stage` -- enum of 11 stages (4 preflight + 7 functional).
- :class:`ProgressStatus` -- enum of per-stage progress phases.
- :class:`ProgressEvent` -- frozen dataclass emitted to the progress
  callback.
- :class:`RecoveryResult` -- frozen dataclass holding the terminal
  state of a recovery run.
- :class:`CancelToken` -- cooperative cancellation signal backed by
  :class:`threading.Event` (thread-safe for the future GUI worker).
- :class:`RecoveryError` and three sub-exceptions
  (:class:`RecoveryPreflightError`, :class:`RecoveryStageError`,
  :class:`RecoveryCancelledError`) for *expected* operational errors.

Design notes (Phase B.6)
------------------------
- The 7 functional stages map 1:1 to the ``STEP X/7`` banners of
  :file:`recover_gh5s_fhd25_alli.sh` and emit them into the log file
  for backward-compatible semantic verification (Phase B.7 baseline
  smoke).
- ffmpeg arguments are hardcoded as tuples matching the legacy ``.sh``
  byte-for-byte.  Profile-driven extraction of these args is deferred
  to a post-B.7 commit, once B.7 has confirmed bit-exact equivalence.
- :func:`recover` catches only :class:`RecoveryError` and subclasses.
  Programming bugs (``TypeError``, ``AttributeError``, etc.) propagate.
- :func:`_run_concat_prefix` uses :func:`shutil.copyfileobj` to stream
  the concatenation, yielding the same bytes as ``cat prefix video``
  without loading either file into RAM.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, TYPE_CHECKING

from mdt_rescue.engine.audio import extract_audio
from mdt_rescue.engine.sps_pps import extract_and_write_prefix
from mdt_rescue.engine.verify import verify_mov
from mdt_rescue.engine.video import extract_video
from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M, Profile

if TYPE_CHECKING:
    from mdt_rescue.engine.verify import VerifyResult


__all__ = [
    "recover",
    "ProgressEvent",
    "ProgressStatus",
    "RecoveryResult",
    "CancelToken",
    "Stage",
    "RecoveryError",
    "RecoveryPreflightError",
    "RecoveryStageError",
    "RecoveryCancelledError",
]


# ---------------------------------------------------------------------------
# Enums and public dataclasses
# ---------------------------------------------------------------------------


class Stage(StrEnum):
    """The 11 stages of the recovery pipeline.

    The first 4 are preflight checks (no banner in the log).  The last
    7 are functional stages and emit a ``STEP X/7`` banner matching the
    legacy ``.sh`` script for backward-compatible log verification.
    """

    PREFLIGHT_DEPS = "preflight_deps"
    PREFLIGHT_INPUTS = "preflight_inputs"
    PREFLIGHT_DISK = "preflight_disk"
    PREFLIGHT_OUTPUT_DIR = "preflight_output_dir"
    EXTRACT_SPS_PPS = "extract_sps_pps"
    EXTRACT_VIDEO = "extract_video"
    CONCAT_PREFIX = "concat_prefix"
    WRAP_VIDEO_MOV = "wrap_video_mov"
    EXTRACT_AUDIO = "extract_audio"
    MUX_AV = "mux_av"
    VERIFY = "verify"


class ProgressStatus(StrEnum):
    """Per-stage progress phases."""

    START = "start"
    PROGRESS = "progress"
    COMPLETE = "complete"
    ERROR = "error"


#: Mapping from functional Stage to its (step_number, human_label) used
#: when emitting the ``STEP X/7`` banner to the log file.  Preflight
#: stages are absent from this mapping.
_STEP_BANNERS: Mapping[Stage, tuple[int, str]] = MappingProxyType(
    {
        Stage.EXTRACT_SPS_PPS: (1, "Extracting SPS/PPS from reference"),
        Stage.EXTRACT_VIDEO: (
            2,
            "Extracting video NAL units from .MDT (streaming)",
        ),
        Stage.CONCAT_PREFIX: (3, "Concatenating SPS/PPS prefix + video"),
        Stage.WRAP_VIDEO_MOV: (4, "Wrapping video into MOV container"),
        Stage.EXTRACT_AUDIO: (
            5,
            "Extracting audio chunks from .MDT (streaming)",
        ),
        Stage.MUX_AV: (6, "Muxing final video + audio"),
        Stage.VERIFY: (7, "Verifying output"),
    }
)


#: Canonical artifact keys (8 entries, matching the legacy ``.sh``
#: output basenames).
_ARTIFACT_KEYS: tuple[str, ...] = (
    "sane_annexb",
    "sps_pps_prefix",
    "video_h264",
    "video_with_header",
    "video_only_mov",
    "audio_raw",
    "audio_wav",
    "rescued_mov",
)


#: Suffix per canonical artifact key.  Combined with ``<basename>_`` to
#: form the actual filename inside :attr:`RecoveryResult.output_dir`.
_ARTIFACT_SUFFIXES: Mapping[str, str] = MappingProxyType(
    {
        "sane_annexb": "_sane_annexb.h264",
        "sps_pps_prefix": "_sps_pps_prefix.h264",
        "video_h264": "_video.h264",
        "video_with_header": "_video_with_header.h264",
        "video_only_mov": "_video_only.mov",
        "audio_raw": "_audio.raw",
        "audio_wav": "_audio.wav",
        "rescued_mov": "_RESCUED.mov",
    }
)


@dataclass(frozen=True)
class ProgressEvent:
    """Structured progress notification emitted by :func:`recover`.

    Parameters
    ----------
    stage:
        Which of the 11 stages the event refers to.
    status:
        Whether the stage is starting, progressing, completing or has
        errored.
    message:
        Human-readable description.
    timestamp:
        Timezone-aware UTC timestamp at emission.
    percent:
        Optional progress percentage (0.0--100.0) when the underlying
        engine reports it; ``None`` otherwise.
    detail:
        Optional immutable mapping carrying stage-specific data
        (filenames, byte counts, etc.).
    """

    stage: Stage
    status: ProgressStatus
    message: str
    timestamp: datetime
    percent: Optional[float] = None
    detail: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class RecoveryResult:
    """Terminal state of a :func:`recover` invocation.

    All paths point to the user's filesystem regardless of whether the
    file actually exists; :attr:`artifacts` is populated eagerly with
    the 8 canonical paths right after
    :data:`Stage.PREFLIGHT_OUTPUT_DIR`, so callers (e.g. the GUI) know
    where files will appear before the pipeline finishes.

    ``success`` and ``cancelled`` are mutually informative:

    - ``success=True, cancelled=False`` -- pipeline completed.
    - ``success=False, cancelled=True`` -- user cancelled.
    - ``success=False, cancelled=False`` -- preflight or stage failure.
    """

    success: bool
    cancelled: bool
    output_dir: Path
    log_path: Path
    rescued_mov_path: Optional[Path]
    artifacts: Mapping[str, Path]
    stages_completed: tuple[Stage, ...]
    stage_failed: Optional[Stage]
    error: Optional["RecoveryError"]
    elapsed_seconds: float
    verify_result: Optional["VerifyResult"]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancelToken:
    """Cooperative cancellation signal backed by :class:`threading.Event`.

    The orchestrator checks :meth:`raise_if_set` between stages; engines
    are not aware of cancellation and complete the in-flight stage
    before the next check.  ``threading.Event`` is used (instead of a
    plain :class:`bool`) so that the future GUI worker thread can flip
    the token from a different thread without races.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_set(self) -> bool:
        """Whether :meth:`cancel` has been called."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Set the cancellation flag.  Idempotent."""
        self._event.set()

    def raise_if_set(self, stage: Optional[Stage] = None) -> None:
        """Raise :class:`RecoveryCancelledError` if cancelled."""
        if self._event.is_set():
            raise RecoveryCancelledError(stage=stage, detail="cancelled")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class RecoveryError(Exception):
    """Base for all *expected* operational errors raised inside
    :func:`recover`.

    Subclasses carry an optional :attr:`stage` field for the orchestrator
    main frame to populate :attr:`RecoveryResult.stage_failed`.
    """

    def __init__(
        self,
        detail: str,
        stage: Optional[Stage] = None,
        original: Optional[BaseException] = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.stage = stage
        self.original = original


class RecoveryPreflightError(RecoveryError):
    """Preflight check failure: missing deps / input / disk / output dir."""


class RecoveryStageError(RecoveryError):
    """Functional stage failure: engine exception or ffmpeg non-zero exit."""


class RecoveryCancelledError(RecoveryError):
    """Raised by :meth:`CancelToken.raise_if_set` when cancelled."""


# ---------------------------------------------------------------------------
# ffmpeg argument templates (bit-exact vs recover_gh5s_fhd25_alli.sh)
# ---------------------------------------------------------------------------
#
# Templates use ``{tokens}`` substituted at call time.  Order, flag
# values and presence MUST match the legacy ``.sh`` (audit B.6.AUDIT.4)
# to preserve bit-exact output.  Profile-driven extraction is deferred
# to a post-B.7 commit; see notes-for-future in the commit message.


_FFMPEG_REF_TO_ANNEXB_ARGS: tuple[str, ...] = (
    "-y",
    "-i", "{ref}",
    "-map", "0:v:0",
    "-c", "copy",
    "-bsf:v", "h264_mp4toannexb",
    "-f", "h264",
    "{out}",
)

_FFMPEG_WRAP_VIDEO_MOV_ARGS: tuple[str, ...] = (
    "-y",
    "-fflags", "+genpts",
    "-framerate", "25",
    "-f", "h264",
    "-i", "{video_with_header}",
    "-c", "copy",
    "-video_track_timescale", "25000",
    "{out}",
)

_FFMPEG_RAW_TO_WAV_ARGS: tuple[str, ...] = (
    "-y",
    "-f", "s16be",
    "-ar", "48000",
    "-ac", "2",
    "-i", "{audio_raw}",
    "{out}",
)

_FFMPEG_MUX_AV_ARGS: tuple[str, ...] = (
    "-y",
    "-i", "{video_only_mov}",
    "-i", "{audio_raw}",
    "-c:v", "copy",
    "-c:a", "pcm_s16be",
    "{out}",
)


def _format_ffmpeg_args(
    template: tuple[str, ...],
    **substitutions: str,
) -> list[str]:
    """Substitute ``{token}`` placeholders in the template.

    Tokens are matched literally (no Python format-spec).  Non-token
    arguments pass through unchanged.
    """
    result: list[str] = []
    for arg in template:
        if arg.startswith("{") and arg.endswith("}"):
            key = arg[1:-1]
            if key not in substitutions:
                raise KeyError(
                    f"ffmpeg template token {{{key}}} not provided"
                )
            result.append(substitutions[key])
        else:
            result.append(arg)
    return result


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _log_timestamp() -> str:
    """Return a ``YYYY-MM-DD HH:MM:SS`` timestamp in local time, matching
    the legacy ``.sh`` ``date '+%Y-%m-%d %H:%M:%S'`` format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_line(log_path: Optional[Path], message: str) -> None:
    """Append a single ``[ts] message`` line to the log file.

    No-op if ``log_path`` is ``None`` (i.e. before
    :data:`Stage.PREFLIGHT_OUTPUT_DIR` succeeds).
    """
    if log_path is None:
        return
    line = f"[{_log_timestamp()}] {message}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def _emit_step_banner(stage: Stage, log_path: Optional[Path]) -> None:
    """Emit ``STEP X/7: <label>`` to the log file for backward-compat."""
    if stage not in _STEP_BANNERS:
        return
    step_num, label = _STEP_BANNERS[stage]
    _log_line(log_path, f"STEP {step_num}/7: {label}")


def _emit(
    progress: Callable[[ProgressEvent], None],
    stage: Stage,
    status: ProgressStatus,
    message: str,
    log_path: Optional[Path] = None,
    percent: Optional[float] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit a :class:`ProgressEvent` and mirror it to the log file."""
    event = ProgressEvent(
        stage=stage,
        status=status,
        message=message,
        timestamp=_utc_now(),
        percent=percent,
        detail=MappingProxyType(dict(detail)) if detail is not None else None,
    )
    progress(event)
    _log_line(log_path, f"[{stage}/{status}] {message}")


# ---------------------------------------------------------------------------
# Preflight stages
# ---------------------------------------------------------------------------


def _run_preflight_deps(
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """Stage 1/4: verify ffmpeg / ffprobe / python3 are on PATH."""
    token.raise_if_set(stage=Stage.PREFLIGHT_DEPS)
    _emit(progress, Stage.PREFLIGHT_DEPS, ProgressStatus.START,
          "Checking dependencies")
    missing: list[str] = [
        c for c in ("ffmpeg", "ffprobe", "python3")
        if shutil.which(c) is None
    ]
    if missing:
        raise RecoveryPreflightError(
            detail=f"Missing dependencies: {', '.join(missing)}",
            stage=Stage.PREFLIGHT_DEPS,
        )
    _emit(progress, Stage.PREFLIGHT_DEPS, ProgressStatus.COMPLETE,
          "Dependencies OK")


def _run_preflight_inputs(
    mdt_path: Path | str,
    reference_mov_path: Path | str,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> tuple[Path, Path]:
    """Stage 2/4: validate that input files exist and are readable."""
    token.raise_if_set(stage=Stage.PREFLIGHT_INPUTS)
    _emit(progress, Stage.PREFLIGHT_INPUTS, ProgressStatus.START,
          "Validating inputs")
    mdt = Path(mdt_path)
    ref = Path(reference_mov_path)
    if not mdt.is_file():
        raise RecoveryPreflightError(
            detail=f"Broken .MDT file not found: {mdt}",
            stage=Stage.PREFLIGHT_INPUTS,
        )
    if not ref.is_file():
        raise RecoveryPreflightError(
            detail=f"Sane reference .MOV file not found: {ref}",
            stage=Stage.PREFLIGHT_INPUTS,
        )
    _emit(progress, Stage.PREFLIGHT_INPUTS, ProgressStatus.COMPLETE,
          f"Inputs validated: broken={mdt.name}, reference={ref.name}")
    return mdt, ref


def _run_preflight_disk(
    mdt: Path,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """Stage 3/4: verify at least ~3x the MDT size is free on output FS."""
    token.raise_if_set(stage=Stage.PREFLIGHT_DISK)
    _emit(progress, Stage.PREFLIGHT_DISK, ProgressStatus.START,
          "Checking disk space")
    try:
        mdt_size = mdt.stat().st_size
        usage = shutil.disk_usage(mdt.parent)
    except OSError as exc:
        raise RecoveryPreflightError(
            detail=f"Cannot stat input or output filesystem: {exc}",
            stage=Stage.PREFLIGHT_DISK,
            original=exc,
        ) from exc
    required = mdt_size * 3
    if usage.free < required:
        required_gb = required / (1024 ** 3)
        available_gb = usage.free / (1024 ** 3)
        raise RecoveryPreflightError(
            detail=(
                f"Insufficient disk space. Need at least "
                f"{required_gb:.2f} GB, have {available_gb:.2f} GB."
            ),
            stage=Stage.PREFLIGHT_DISK,
        )
    _emit(progress, Stage.PREFLIGHT_DISK, ProgressStatus.COMPLETE,
          "Disk space OK")


def _run_preflight_output_dir(
    mdt: Path,
    output_dir: Path | str | None,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> tuple[Path, Path, dict[str, Path]]:
    """Stage 4/4: create the output directory, derive log path and
    populate the artifacts dict eagerly.

    Returns
    -------
    out_dir, log_path, artifacts
    """
    token.raise_if_set(stage=Stage.PREFLIGHT_OUTPUT_DIR)
    _emit(progress, Stage.PREFLIGHT_OUTPUT_DIR, ProgressStatus.START,
          "Preparing output directory")

    basename = mdt.stem
    if output_dir is None:
        out_dir = mdt.parent / f"recovery_output_{basename}"
    else:
        out_dir = Path(output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecoveryPreflightError(
            detail=f"Cannot create output directory {out_dir}: {exc}",
            stage=Stage.PREFLIGHT_OUTPUT_DIR,
            original=exc,
        ) from exc

    log_path = out_dir / "recovery_log.txt"
    log_path.touch(exist_ok=True)

    artifacts: dict[str, Path] = {
        key: out_dir / f"{basename}{_ARTIFACT_SUFFIXES[key]}"
        for key in _ARTIFACT_KEYS
    }

    _emit(progress, Stage.PREFLIGHT_OUTPUT_DIR, ProgressStatus.COMPLETE,
          f"Output directory: {out_dir}", log_path=log_path)
    return out_dir, log_path, artifacts


# ---------------------------------------------------------------------------
# Functional stages
# ---------------------------------------------------------------------------


def _run_subprocess(
    args: list[str],
    stage: Stage,
    log_path: Path,
) -> None:
    """Run a subprocess, append stderr to the log, raise on non-zero."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RecoveryStageError(
            detail=f"Failed to launch {args[0]}: {exc}",
            stage=stage,
            original=exc,
        ) from exc
    if result.stderr:
        _log_line(log_path, result.stderr.rstrip("\n"))
    if result.stdout:
        _log_line(log_path, result.stdout.rstrip("\n"))
    if result.returncode != 0:
        raise RecoveryStageError(
            detail=(
                f"{args[0]} exited with code {result.returncode}: "
                f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no stderr'}"
            ),
            stage=stage,
        )


def _run_extract_sps_pps(
    ref: Path,
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 1/7: convert reference MOV to Annex-B and extract SPS/PPS."""
    token.raise_if_set(stage=Stage.EXTRACT_SPS_PPS)
    _emit_step_banner(Stage.EXTRACT_SPS_PPS, log_path)
    _emit(progress, Stage.EXTRACT_SPS_PPS, ProgressStatus.START,
          "Extracting SPS/PPS from reference", log_path=log_path)

    _emit(progress, Stage.EXTRACT_SPS_PPS, ProgressStatus.PROGRESS,
          "Converting reference MOV to Annex-B", log_path=log_path)
    ffmpeg_args = ["ffmpeg", *_format_ffmpeg_args(
        _FFMPEG_REF_TO_ANNEXB_ARGS,
        ref=str(ref),
        out=str(artifacts["sane_annexb"]),
    )]
    _run_subprocess(ffmpeg_args, Stage.EXTRACT_SPS_PPS, log_path)

    _emit(progress, Stage.EXTRACT_SPS_PPS, ProgressStatus.PROGRESS,
          "Parsing SPS/PPS NAL units", log_path=log_path)
    try:
        extract_and_write_prefix(
            input_path=artifacts["sane_annexb"],
            output_path=artifacts["sps_pps_prefix"],
        )
    except Exception as exc:
        raise RecoveryStageError(
            detail=f"SPS/PPS extraction failed: {exc}",
            stage=Stage.EXTRACT_SPS_PPS,
            original=exc,
        ) from exc

    _emit(progress, Stage.EXTRACT_SPS_PPS, ProgressStatus.COMPLETE,
          "SPS/PPS prefix written", log_path=log_path)


def _run_extract_video(
    mdt: Path,
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 2/7: extract H.264 NAL units from the broken .MDT."""
    token.raise_if_set(stage=Stage.EXTRACT_VIDEO)
    _emit_step_banner(Stage.EXTRACT_VIDEO, log_path)
    _emit(progress, Stage.EXTRACT_VIDEO, ProgressStatus.START,
          "Extracting video NAL units from .MDT", log_path=log_path)
    try:
        extract_video(
            input_path=mdt,
            output_path=artifacts["video_h264"],
        )
    except Exception as exc:
        raise RecoveryStageError(
            detail=f"Video extraction failed: {exc}",
            stage=Stage.EXTRACT_VIDEO,
            original=exc,
        ) from exc
    _emit(progress, Stage.EXTRACT_VIDEO, ProgressStatus.COMPLETE,
          "Video Annex-B written", log_path=log_path)


def _run_concat_prefix(
    artifacts: dict[str, Path],
    log_path: Path,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 3/7: concatenate SPS/PPS prefix + video into one Annex-B file.

    Streamed via :func:`shutil.copyfileobj` so output bytes are
    identical to ``cat prefix video`` without loading either file into
    RAM.
    """
    token.raise_if_set(stage=Stage.CONCAT_PREFIX)
    _emit_step_banner(Stage.CONCAT_PREFIX, log_path)
    _emit(progress, Stage.CONCAT_PREFIX, ProgressStatus.START,
          "Concatenating SPS/PPS prefix + video", log_path=log_path)
    try:
        with open(artifacts["video_with_header"], "wb") as dst:
            for src in (artifacts["sps_pps_prefix"], artifacts["video_h264"]):
                with open(src, "rb") as s:
                    shutil.copyfileobj(s, dst)
    except OSError as exc:
        raise RecoveryStageError(
            detail=f"Concatenation failed: {exc}",
            stage=Stage.CONCAT_PREFIX,
            original=exc,
        ) from exc
    _emit(progress, Stage.CONCAT_PREFIX, ProgressStatus.COMPLETE,
          "Video with header written", log_path=log_path)


def _run_wrap_video_mov(
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 4/7: wrap Annex-B into a MOV container via ffmpeg."""
    token.raise_if_set(stage=Stage.WRAP_VIDEO_MOV)
    _emit_step_banner(Stage.WRAP_VIDEO_MOV, log_path)
    _emit(progress, Stage.WRAP_VIDEO_MOV, ProgressStatus.START,
          "Wrapping video into MOV container", log_path=log_path)
    ffmpeg_args = ["ffmpeg", *_format_ffmpeg_args(
        _FFMPEG_WRAP_VIDEO_MOV_ARGS,
        video_with_header=str(artifacts["video_with_header"]),
        out=str(artifacts["video_only_mov"]),
    )]
    _run_subprocess(ffmpeg_args, Stage.WRAP_VIDEO_MOV, log_path)
    _emit(progress, Stage.WRAP_VIDEO_MOV, ProgressStatus.COMPLETE,
          "Video-only MOV written", log_path=log_path)


def _run_extract_audio(
    mdt: Path,
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 5/7: extract audio chunks and convert raw -> wav via ffmpeg."""
    token.raise_if_set(stage=Stage.EXTRACT_AUDIO)
    _emit_step_banner(Stage.EXTRACT_AUDIO, log_path)
    _emit(progress, Stage.EXTRACT_AUDIO, ProgressStatus.START,
          "Extracting audio chunks from .MDT", log_path=log_path)
    try:
        extract_audio(
            input_path=mdt,
            output_path=artifacts["audio_raw"],
        )
    except Exception as exc:
        raise RecoveryStageError(
            detail=f"Audio extraction failed: {exc}",
            stage=Stage.EXTRACT_AUDIO,
            original=exc,
        ) from exc

    _emit(progress, Stage.EXTRACT_AUDIO, ProgressStatus.PROGRESS,
          "Converting raw audio to WAV", log_path=log_path)
    ffmpeg_args = ["ffmpeg", *_format_ffmpeg_args(
        _FFMPEG_RAW_TO_WAV_ARGS,
        audio_raw=str(artifacts["audio_raw"]),
        out=str(artifacts["audio_wav"]),
    )]
    _run_subprocess(ffmpeg_args, Stage.EXTRACT_AUDIO, log_path)
    _emit(progress, Stage.EXTRACT_AUDIO, ProgressStatus.COMPLETE,
          "Audio raw + WAV written", log_path=log_path)


def _run_mux_av(
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> None:
    """STEP 6/7: mux final video + audio MOV via ffmpeg."""
    token.raise_if_set(stage=Stage.MUX_AV)
    _emit_step_banner(Stage.MUX_AV, log_path)
    _emit(progress, Stage.MUX_AV, ProgressStatus.START,
          "Muxing final video + audio", log_path=log_path)
    ffmpeg_args = ["ffmpeg", *_format_ffmpeg_args(
        _FFMPEG_MUX_AV_ARGS,
        video_only_mov=str(artifacts["video_only_mov"]),
        audio_raw=str(artifacts["audio_raw"]),
        out=str(artifacts["rescued_mov"]),
    )]
    _run_subprocess(ffmpeg_args, Stage.MUX_AV, log_path)
    _emit(progress, Stage.MUX_AV, ProgressStatus.COMPLETE,
          "Rescued MOV written", log_path=log_path)


def _run_verify(
    artifacts: dict[str, Path],
    log_path: Path,
    profile: Profile,
    progress: Callable[[ProgressEvent], None],
    token: CancelToken,
) -> "VerifyResult":
    """STEP 7/7: ffprobe-based plausibility checks on the rescued MOV."""
    token.raise_if_set(stage=Stage.VERIFY)
    _emit_step_banner(Stage.VERIFY, log_path)
    _emit(progress, Stage.VERIFY, ProgressStatus.START,
          "Verifying output", log_path=log_path)
    try:
        result = verify_mov(
            input_path=artifacts["rescued_mov"],
            expectations=profile.verify_expectations,
        )
    except Exception as exc:
        raise RecoveryStageError(
            detail=f"Verification failed: {exc}",
            stage=Stage.VERIFY,
            original=exc,
        ) from exc
    _emit(progress, Stage.VERIFY, ProgressStatus.COMPLETE,
          "Verification finished", log_path=log_path)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def recover(
    mdt_path: Path | str,
    reference_mov_path: Path | str,
    profile: Profile = GH5S_FHD25_ALLI_200M,
    output_dir: Path | str | None = None,
    progress: Optional[Callable[[ProgressEvent], None]] = None,
    cancel_token: Optional[CancelToken] = None,
) -> RecoveryResult:
    """Run the full GH5S MDT recovery pipeline.

    Composes the four engine primitives and four ffmpeg invocations,
    producing 8 deterministic artifacts in :attr:`output_dir`.  Only
    :class:`RecoveryError` (and subclasses) are caught and translated
    into :class:`RecoveryResult` with ``success=False``; programming
    bugs (``TypeError``, ``AttributeError``, etc.) propagate.

    Parameters
    ----------
    mdt_path:
        Path to the broken Panasonic ``.MDT`` file.  Never modified.
    reference_mov_path:
        Path to a healthy ``.MOV`` file from the same recording session
        (used to extract SPS/PPS for the rescued video).
    profile:
        Recovery profile.  Defaults to
        :data:`mdt_rescue.profiles.GH5S_FHD25_ALLI_200M`.
    output_dir:
        Where to write the 8 artifacts and the log file.  Default:
        ``<mdt_parent>/recovery_output_<mdt_stem>/``.
    progress:
        Optional callback receiving a :class:`ProgressEvent` for each
        stage transition.
    cancel_token:
        Optional :class:`CancelToken` for cooperative cancellation.

    Returns
    -------
    A :class:`RecoveryResult` describing the terminal state.
    """
    start = time.monotonic()
    token = cancel_token if cancel_token is not None else CancelToken()
    emit: Callable[[ProgressEvent], None] = (
        progress if progress is not None else (lambda _e: None)
    )

    stages_completed: list[Stage] = []
    artifacts: dict[str, Path] = {}
    log_path: Optional[Path] = None
    out_dir: Optional[Path] = None
    verify_result: Optional["VerifyResult"] = None

    try:
        _run_preflight_deps(emit, token)
        stages_completed.append(Stage.PREFLIGHT_DEPS)

        mdt, ref = _run_preflight_inputs(
            mdt_path, reference_mov_path, emit, token,
        )
        stages_completed.append(Stage.PREFLIGHT_INPUTS)

        _run_preflight_disk(mdt, emit, token)
        stages_completed.append(Stage.PREFLIGHT_DISK)

        out_dir, log_path, artifacts = _run_preflight_output_dir(
            mdt, output_dir, emit, token,
        )
        stages_completed.append(Stage.PREFLIGHT_OUTPUT_DIR)

        _run_extract_sps_pps(
            ref, artifacts, log_path, profile, emit, token,
        )
        stages_completed.append(Stage.EXTRACT_SPS_PPS)

        _run_extract_video(
            mdt, artifacts, log_path, profile, emit, token,
        )
        stages_completed.append(Stage.EXTRACT_VIDEO)

        _run_concat_prefix(artifacts, log_path, emit, token)
        stages_completed.append(Stage.CONCAT_PREFIX)

        _run_wrap_video_mov(
            artifacts, log_path, profile, emit, token,
        )
        stages_completed.append(Stage.WRAP_VIDEO_MOV)

        _run_extract_audio(
            mdt, artifacts, log_path, profile, emit, token,
        )
        stages_completed.append(Stage.EXTRACT_AUDIO)

        _run_mux_av(artifacts, log_path, profile, emit, token)
        stages_completed.append(Stage.MUX_AV)

        verify_result = _run_verify(
            artifacts, log_path, profile, emit, token,
        )
        stages_completed.append(Stage.VERIFY)

        _log_line(log_path, "")
        _log_line(log_path, "=" * 60)
        _log_line(log_path, "RECOVERY COMPLETED")
        _log_line(log_path, "=" * 60)

        return RecoveryResult(
            success=True,
            cancelled=False,
            output_dir=out_dir,
            log_path=log_path,
            rescued_mov_path=artifacts["rescued_mov"],
            artifacts=MappingProxyType(dict(artifacts)),
            stages_completed=tuple(stages_completed),
            stage_failed=None,
            error=None,
            elapsed_seconds=time.monotonic() - start,
            verify_result=verify_result,
        )

    except RecoveryCancelledError as exc:
        return _failure_result(
            cancelled=True,
            out_dir=out_dir,
            log_path=log_path,
            artifacts=artifacts,
            stages_completed=stages_completed,
            error=exc,
            elapsed=time.monotonic() - start,
            verify_result=verify_result,
        )
    except RecoveryError as exc:
        if log_path is not None:
            _log_line(log_path, f"ERROR ({exc.stage}): {exc.detail}")
        return _failure_result(
            cancelled=False,
            out_dir=out_dir,
            log_path=log_path,
            artifacts=artifacts,
            stages_completed=stages_completed,
            error=exc,
            elapsed=time.monotonic() - start,
            verify_result=verify_result,
        )


def _failure_result(
    cancelled: bool,
    out_dir: Optional[Path],
    log_path: Optional[Path],
    artifacts: dict[str, Path],
    stages_completed: list[Stage],
    error: "RecoveryError",
    elapsed: float,
    verify_result: Optional["VerifyResult"],
) -> RecoveryResult:
    """Build a ``success=False`` :class:`RecoveryResult`.

    Used for both cancellation and operational error paths.
    """
    return RecoveryResult(
        success=False,
        cancelled=cancelled,
        output_dir=out_dir if out_dir is not None else Path("."),
        log_path=log_path if log_path is not None else Path("."),
        rescued_mov_path=None,
        artifacts=MappingProxyType(dict(artifacts)) if artifacts else MappingProxyType({}),
        stages_completed=tuple(stages_completed),
        stage_failed=error.stage,
        error=error,
        elapsed_seconds=elapsed,
        verify_result=verify_result,
    )
