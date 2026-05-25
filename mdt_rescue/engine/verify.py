"""
mdt_rescue.engine.verify
========================

Verify a rescued MOV file against the validated GH5S FHD 25p ALL-I
profile.

This module replaces the legacy ``scripts/verify_output.sh`` shell
script. It is a pure-Python, silent engine: it produces structured
data (see :class:`VerifyResult`) and never writes to stdout or
stderr. Stdout reconstruction (the human-readable legacy output) is
the responsibility of the CLI wrapper at ``scripts/verify_output.py``.

API
---

High-level entry point:

* :func:`verify_mov` - compose pre-flight checks, ffprobe probing,
  and plausibility checks into a single :class:`VerifyResult`.

Low-level helpers:

* :func:`probe_streams` - run the 11 separate ffprobe invocations
  and return raw string fields.
* :func:`run_plausibility_checks` - apply the 7 string-vs-string
  equality checks in the legacy order.

Constants:

* :data:`EXPECTATIONS_GH5S_FHD25` - default expectations for the
  validated profile.

Dataclasses (all frozen):

* :class:`VideoStreamInfo`, :class:`AudioStreamInfo`,
  :class:`PlausibilityExpectations`, :class:`CheckResult`,
  :class:`VerifyResult`.

Exceptions:

* :class:`FFprobeNotFoundError` - ffprobe is not in PATH.
* :class:`InputNotFoundError` - the input MOV file does not exist.

Design notes
------------

The legacy shell script runs 11 separate ``ffprobe`` invocations,
each extracting exactly one field. This module replicates that
structure faithfully (rather than consolidating into a single
``-print_format json`` call) for two reasons:

1. **Bit-exact parity** with the legacy script. Each invocation
   succeeds or fails independently; the per-field outcome is
   exactly what the legacy ``|| true`` idiom produces.
2. **Per-field error isolation**. If ffprobe fails on one field
   (e.g. ``profile``), the others still succeed, and the failed
   field becomes an empty string (mirroring the unset variable
   default in bash).

All string comparisons preserve the legacy semantics: width, height,
sample_rate and channels are compared as strings, not integers.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Union


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class FFprobeNotFoundError(Exception):
    """Raised when the ``ffprobe`` executable is not in PATH.

    Mirrors the legacy ``ERROR: ffprobe not found in PATH`` shell
    exit (return code 2 at the wrapper level).
    """


class InputNotFoundError(Exception):
    """Raised when the input MOV file does not exist on disk.

    Mirrors the legacy ``ERROR: file not found: <path>`` shell exit
    (return code 2 at the wrapper level).
    """


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class VideoStreamInfo:
    """Raw ffprobe fields for the first video stream (``v:0``).

    All fields are :class:`str`. An empty string indicates that
    ffprobe failed to return a value for that field (legacy
    ``|| true`` semantics).
    """

    codec_name: str = ""
    width: str = ""
    height: str = ""
    nb_frames: str = ""
    duration: str = ""
    pix_fmt: str = ""
    profile: str = ""


@dataclass(frozen=True)
class AudioStreamInfo:
    """Raw ffprobe fields for the first audio stream (``a:0``).

    All fields are :class:`str`. An empty string indicates that
    ffprobe failed to return a value for that field.
    """

    codec_name: str = ""
    sample_rate: str = ""
    channels: str = ""
    duration: str = ""


@dataclass(frozen=True)
class PlausibilityExpectations:
    """Expected values for the 7 plausibility checks.

    All fields are :class:`str` to preserve the legacy
    string-vs-string equality. The default values target the
    validated GH5S FHD 25p ALL-I profile.

    A richer :class:`Profile` dataclass will subsume this struct in
    phase B.5.
    """

    video_codec: str = "h264"
    width: str = "1920"
    height: str = "1080"
    pix_fmt: str = "yuv422p10le"
    audio_codec: str = "pcm_s16be"
    sample_rate: str = "48000"
    channels: str = "2"


EXPECTATIONS_GH5S_FHD25: PlausibilityExpectations = PlausibilityExpectations()
"""Default expectations for the validated GH5S FHD 25p ALL-I profile."""


@dataclass(frozen=True)
class CheckResult:
    """Result of a single plausibility check.

    Parameters
    ----------
    label:
        Human-readable label as emitted by the legacy script
        (e.g. ``"video codec"``, ``"width"``).
    expected:
        Expected value as a string.
    actual:
        Observed value as a string. May be ``""`` if ffprobe failed
        to obtain it.
    passed:
        ``True`` iff ``actual == expected`` (string equality).
    """

    label: str
    expected: str
    actual: str
    passed: bool


@dataclass(frozen=True)
class VerifyResult:
    """Composite result of a full verify operation."""

    input_path: Path
    video: VideoStreamInfo
    audio: AudioStreamInfo
    checks: Tuple[CheckResult, ...]
    all_passed: bool
    expectations: PlausibilityExpectations


# ---------------------------------------------------------------------
# Internal: single ffprobe field invocation
# ---------------------------------------------------------------------


def _probe_field(input_path: str, stream: str, field_name: str) -> str:
    """Invoke a single ffprobe call and return ``stdout.strip()``.

    Replicates the legacy bash idiom::

        ffprobe -v error -select_streams <stream> \\
            -show_entries stream=<field> \\
            -of default=nw=1:nk=1 <input> || true

    The ``|| true`` semantics are preserved: any failure (non-zero
    exit code, missing field, OS error during subprocess spawn)
    returns ``""``. This function never raises.

    Parameters
    ----------
    input_path:
        Path to the MOV file, as a string.
    stream:
        ffprobe stream selector, typically ``"v:0"`` or ``"a:0"``.
    field_name:
        Name of the field to extract (e.g. ``"codec_name"``,
        ``"width"``).

    Returns
    -------
    str
        The extracted value, stripped of trailing whitespace, or
        ``""`` on any failure.

    Notes
    -----
    Encoding is forced to UTF-8 with ``errors="replace"`` to handle
    any unexpected non-ASCII bytes that ffprobe might emit (e.g. in
    metadata fields). The legacy bash script implicitly relies on
    the shell locale and would not raise on bad bytes either.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", stream,
        "-show_entries", f"stream={field_name}",
        "-of", "default=nw=1:nk=1",
        input_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ---------------------------------------------------------------------
# Probe streams: 11 separate ffprobe invocations
# ---------------------------------------------------------------------


def probe_streams(
    input_path: Union[Path, str],
) -> Tuple[VideoStreamInfo, AudioStreamInfo]:
    """Probe video (v:0) and audio (a:0) streams via ffprobe.

    Performs 11 separate ffprobe invocations matching the legacy
    ``scripts/verify_output.sh``:

    Video stream (7 fields): ``codec_name``, ``width``, ``height``,
    ``nb_frames``, ``duration``, ``pix_fmt``, ``profile``.

    Audio stream (4 fields): ``codec_name``, ``sample_rate``,
    ``channels``, ``duration``.

    Each field is collected independently. A failure on one field
    does NOT affect the others: that field becomes ``""``, but the
    remaining invocations still execute.

    Parameters
    ----------
    input_path:
        Path to the MOV file. Existence is NOT checked here; the
        caller (:func:`verify_mov`) is responsible for pre-flight
        validation. If the file is missing, each individual ffprobe
        invocation will fail and its field will be ``""``.

    Returns
    -------
    tuple[VideoStreamInfo, AudioStreamInfo]
        Raw string fields, no plausibility checks applied.
    """
    p = str(input_path)
    video = VideoStreamInfo(
        codec_name=_probe_field(p, "v:0", "codec_name"),
        width=_probe_field(p, "v:0", "width"),
        height=_probe_field(p, "v:0", "height"),
        nb_frames=_probe_field(p, "v:0", "nb_frames"),
        duration=_probe_field(p, "v:0", "duration"),
        pix_fmt=_probe_field(p, "v:0", "pix_fmt"),
        profile=_probe_field(p, "v:0", "profile"),
    )
    audio = AudioStreamInfo(
        codec_name=_probe_field(p, "a:0", "codec_name"),
        sample_rate=_probe_field(p, "a:0", "sample_rate"),
        channels=_probe_field(p, "a:0", "channels"),
        duration=_probe_field(p, "a:0", "duration"),
    )
    return video, audio


# ---------------------------------------------------------------------
# Plausibility checks: 7 string-vs-string equalities
# ---------------------------------------------------------------------


_CHECK_SPEC: Tuple[Tuple[str, str, str, str], ...] = (
    # (label,           expectations attr,  stream-info attr, source)
    ("video codec",     "video_codec",      "codec_name",     "video"),
    ("width",           "width",            "width",          "video"),
    ("height",          "height",           "height",         "video"),
    ("pix_fmt",         "pix_fmt",          "pix_fmt",        "video"),
    ("audio codec",     "audio_codec",      "codec_name",     "audio"),
    ("sample rate",     "sample_rate",      "sample_rate",    "audio"),
    ("channels",        "channels",         "channels",       "audio"),
)
"""Specification of the 7 plausibility checks in legacy order.

Each tuple is ``(label, expectations_attr, stream_info_attr,
source)`` where ``source`` is either ``"video"`` or ``"audio"``.
The order of this tuple drives the order of
:func:`run_plausibility_checks` output, which in turn drives the
order of the ``[OK]`` / ``[WARN]`` lines emitted by the wrapper.
Do not reorder.
"""


def run_plausibility_checks(
    video_info: VideoStreamInfo,
    audio_info: AudioStreamInfo,
    expectations: PlausibilityExpectations,
) -> Tuple[CheckResult, ...]:
    """Run the 7 plausibility checks in the legacy order.

    Each check compares an expected value (from ``expectations``)
    against an actual value (from ``video_info`` or ``audio_info``)
    using string equality. No type coercion is performed: a width
    of ``"1920"`` against an expected ``"1920"`` passes, but
    ``"1920.0"`` against ``"1920"`` fails.

    Parameters
    ----------
    video_info:
        Raw video stream info as produced by :func:`probe_streams`.
    audio_info:
        Raw audio stream info as produced by :func:`probe_streams`.
    expectations:
        Profile expectations to check against.

    Returns
    -------
    tuple[CheckResult, ...]
        Seven results in legacy order: ``"video codec"``,
        ``"width"``, ``"height"``, ``"pix_fmt"``, ``"audio codec"``,
        ``"sample rate"``, ``"channels"``.
    """
    results = []
    for label, exp_attr, info_attr, source in _CHECK_SPEC:
        expected = getattr(expectations, exp_attr)
        if source == "video":
            actual = getattr(video_info, info_attr)
        else:
            actual = getattr(audio_info, info_attr)
        results.append(
            CheckResult(
                label=label,
                expected=expected,
                actual=actual,
                passed=(actual == expected),
            )
        )
    return tuple(results)


# ---------------------------------------------------------------------
# High-level: verify_mov
# ---------------------------------------------------------------------


def verify_mov(
    input_path: Union[Path, str],
    expectations: Optional[PlausibilityExpectations] = None,
    progress: Optional[Callable[[Any], None]] = None,
) -> VerifyResult:
    """Verify a rescued MOV file against the expected profile.

    Performs the following steps, in order:

    1. Validate that ``input_path`` exists on disk
       (:class:`InputNotFoundError` otherwise).
    2. Validate that ``ffprobe`` is available in ``PATH``
       (:class:`FFprobeNotFoundError` otherwise).
    3. Probe the video and audio streams (11 ffprobe invocations).
    4. Run the 7 plausibility checks.
    5. Return a :class:`VerifyResult` with structured data.

    The function never prints. The legacy stdout output is the
    responsibility of the CLI wrapper.

    Parameters
    ----------
    input_path:
        Path to the MOV file to verify.
    expectations:
        Profile expectations. If ``None`` (default), uses
        :data:`EXPECTATIONS_GH5S_FHD25`.
    progress:
        Reserved for future use (B.6 orchestrator). Currently
        accepted for interface symmetry with the other engine
        entry points (:func:`mdt_rescue.engine.video.extract_video`,
        :func:`mdt_rescue.engine.audio.extract_audio`) but never
        invoked. Pass ``None`` or omit.

    Returns
    -------
    VerifyResult
        Structured result with stream info, per-check outcomes, and
        an overall pass/fail flag.

    Raises
    ------
    InputNotFoundError
        If ``input_path`` does not point to an existing regular file.
    FFprobeNotFoundError
        If ``ffprobe`` is not in PATH.
    """
    if expectations is None:
        expectations = EXPECTATIONS_GH5S_FHD25

    path = Path(input_path)
    if not path.is_file():
        raise InputNotFoundError(str(input_path))

    if shutil.which("ffprobe") is None:
        raise FFprobeNotFoundError("ffprobe")

    video, audio = probe_streams(path)
    checks = run_plausibility_checks(video, audio, expectations)
    all_passed = all(c.passed for c in checks)

    # ``progress`` is reserved for B.6 orchestrator; never invoked
    # in B.4. Reference once to silence unused-argument linters
    # without emitting any side effect.
    if progress is not None:
        pass

    return VerifyResult(
        input_path=path,
        video=video,
        audio=audio,
        checks=checks,
        all_passed=all_passed,
        expectations=expectations,
    )
