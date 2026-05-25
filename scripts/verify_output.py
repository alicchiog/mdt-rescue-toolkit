#!/usr/bin/env python3
"""
verify_output.py
================

Thin CLI wrapper around ``mdt_rescue.engine.verify``.

Runs ffprobe checks on a rescued MOV file and prints a human-readable
summary. Returns 0 if both video and audio streams look plausible for
the GH5S FHD 25p ALL-I profile, 1 otherwise. Returns 2 for usage
errors, missing input file, or missing ffprobe.

Usage
-----

    python3 scripts/verify_output.py path/to/rescued.mov

This wrapper replaces the legacy ``scripts/verify_output.sh`` (which
is kept in the repository as a reference). The bit-exact stdout
format and the 0/1/2 exit-code policy of the legacy script are
preserved.

Part of MDT Rescue Toolkit, phase B.4.wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ----------------------------------------------------------------------
# Fallback import guard
# ----------------------------------------------------------------------
#
# Allow this script to be invoked without an active venv, as long as
# the repository layout is intact and the script is executed from
# anywhere on the filesystem (``$REPO_ROOT/scripts/verify_output.py``).
# Same pattern as B.1.wrapper, B.2.wrapper, B.3.wrapper.

try:
    from mdt_rescue.engine.verify import (  # noqa: E402
        FFprobeNotFoundError,
        InputNotFoundError,
        VerifyResult,
        verify_mov,
    )
except ImportError:  # pragma: no cover - env-dependent
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_REPO_ROOT))
    from mdt_rescue.engine.verify import (  # noqa: E402
        FFprobeNotFoundError,
        InputNotFoundError,
        VerifyResult,
        verify_mov,
    )


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

_HEADER_RULE = "=" * 60
"""Legacy header rule: 60 ``=`` characters."""


# ----------------------------------------------------------------------
# Stdout helpers (legacy bit-exact rendering)
# ----------------------------------------------------------------------
#
# Each helper produces a specific block of the legacy stdout. Spacing,
# punctuation, and quoting are hardcoded as data, not computed at
# runtime. Do not refactor toward column-width helpers: the legacy
# layout uses ad-hoc per-line spacing.


def _print_header(input_path) -> None:
    """Emit lines 1-4 of the legacy output: header rule, Verifying:, header rule, blank.

    The ``Verifying:`` line stamps ``input_path`` verbatim - no
    normalization, no realpath, no surrounding quotes. Matches the
    legacy ``echo "Verifying: $INPUT"`` with ``$INPUT`` as received
    in ``$1``.
    """
    print(_HEADER_RULE)
    print(f"Verifying: {input_path}")
    print(_HEADER_RULE)
    print("")


def _print_video_stream(video) -> None:
    """Emit lines 5-12: VIDEO STREAM block + trailing blank line.

    Fields use the ``value or "?"`` fallback pattern, matching the
    legacy ``${VAR:-?}`` idiom for unset / empty bash variables.
    """
    print("VIDEO STREAM")
    print(f"  codec:        {video.codec_name or '?'}")
    print(f"  profile:      {video.profile or '?'}")
    print(f"  resolution:   {video.width or '?'}x{video.height or '?'}")
    print(f"  pixel format: {video.pix_fmt or '?'}")
    print(f"  frames:       {video.nb_frames or '?'}")
    print(f"  duration:     {video.duration or '?'} s")
    print("")


def _print_audio_stream(audio) -> None:
    """Emit lines 13-18: AUDIO STREAM block + trailing blank line."""
    print("AUDIO STREAM")
    print(f"  codec:        {audio.codec_name or '?'}")
    print(f"  sample rate:  {audio.sample_rate or '?'} Hz")
    print(f"  channels:     {audio.channels or '?'}")
    print(f"  duration:     {audio.duration or '?'} s")
    print("")


def _format_check_line(check) -> str:
    """Format a single check result as the legacy [OK]/[WARN] line.

    Returns the line without trailing newline. The caller is
    responsible for printing it.

    Branch templates (literal, do not refactor):
        passed=True:
            "  [OK]   {label}: {actual}"
        passed=False:
            "  [WARN] {label}: expected '{expected}', got '{actual}'"

    The ``[OK]`` marker (4 chars) is followed by 3 spaces; the
    ``[WARN]`` marker (6 chars) is followed by 1 space. This keeps
    the label column aligned across both branches. Do not adjust
    spacing.

    Single quotes around ``expected`` and ``actual`` are literal
    characters in the legacy output (matching ``'$expected'`` /
    ``'$actual'`` in bash). They are NOT Python repr quotes; an empty
    ``actual`` is rendered as ``got ''`` (two literal apostrophes),
    not ``got ?``.
    """
    if check.passed:
        return f"  [OK]   {check.label}: {check.actual}"
    return (
        f"  [WARN] {check.label}: "
        f"expected '{check.expected}', got '{check.actual}'"
    )


def _print_checks(checks) -> None:
    """Emit lines 19-27: PLAUSIBILITY CHECKS header + 7 check lines + blank.

    The 7 check lines are emitted in the order returned by the
    engine (which is the legacy order: video codec, width, height,
    pix_fmt, audio codec, sample rate, channels).
    """
    print(
        "PLAUSIBILITY CHECKS (against validated GH5S FHD 25p profile)"
    )
    for check in checks:
        print(_format_check_line(check))
    print("")


def _print_verdict(all_passed: bool) -> None:
    """Emit line 28: the verdict.

    No trailing blank line; the legacy script terminates immediately
    after this with ``exit 0`` or ``exit 1``.

    Templates (literal):
        all_passed=True:
            "Verification: PASSED"
        all_passed=False:
            "Verification: WARNINGS (file may still work, inspect manually)"

    Note: the failure branch is ``WARNINGS`` (plural, with the
    parenthetical), NOT ``FAILED``. Earlier phase notes referred to
    a ``Verification: FAILED`` literal which does not exist in the
    legacy script.
    """
    if all_passed:
        print("Verification: PASSED")
    else:
        print(
            "Verification: WARNINGS "
            "(file may still work, inspect manually)"
        )


def _render_verify_result(result: VerifyResult, input_path) -> None:
    """Render the full legacy stdout block from a VerifyResult.

    Calls H1 through H5 in legacy order. ``input_path`` is passed
    separately (rather than read from ``result.input_path``) so that
    the path printed in the header matches argv verbatim, without
    the ``Path()`` normalization the engine applies internally.
    """
    _print_header(input_path)
    _print_video_stream(result.video)
    _print_audio_stream(result.audio)
    _print_checks(result.checks)
    _print_verdict(result.all_passed)


# ----------------------------------------------------------------------
# Stderr error handlers
# ----------------------------------------------------------------------
#
# Three dedicated functions for the three legacy error messages.
# Each writes to stderr and returns nothing; the caller (``main``)
# is responsible for the matching exit code (always 2 for these).


def _print_usage_error(prog: str) -> None:
    """Emit the legacy usage error to stderr.

    Mirrors ``echo "Usage: $0 <rescued.mov>" >&2`` in
    ``scripts/verify_output.sh``.
    """
    print(f"Usage: {prog} <rescued.mov>", file=sys.stderr)


def _print_input_not_found_error(path: str) -> None:
    """Emit the legacy 'file not found' error to stderr.

    Mirrors ``echo "ERROR: file not found: $INPUT" >&2``.
    """
    print(f"ERROR: file not found: {path}", file=sys.stderr)


def _print_ffprobe_not_found_error() -> None:
    """Emit the legacy 'ffprobe not found' error to stderr.

    Mirrors ``echo "ERROR: ffprobe not found in PATH" >&2``.
    """
    print("ERROR: ffprobe not found in PATH", file=sys.stderr)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main() -> int:
    """CLI entry point.

    Exit codes (matching the legacy ``scripts/verify_output.sh``):

    * **0** - all 7 plausibility checks passed; verdict ``PASSED``.
    * **1** - at least one check raised a warning; verdict
      ``WARNINGS (file may still work, inspect manually)``.
    * **2** - usage error, missing input file, or missing ffprobe.

    Returns
    -------
    int
        Exit code; the entry-point block at the bottom of the file
        passes this through to ``sys.exit``.
    """
    if len(sys.argv) != 2:
        _print_usage_error(sys.argv[0])
        return 2

    input_arg = sys.argv[1]

    try:
        result = verify_mov(input_arg)
    except InputNotFoundError:
        _print_input_not_found_error(input_arg)
        return 2
    except FFprobeNotFoundError:
        _print_ffprobe_not_found_error()
        return 2

    _render_verify_result(result, input_arg)
    return 0 if result.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
