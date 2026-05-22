#!/usr/bin/env python3
"""
extract_video_gh5s_fhd25_alli.py
================================

Thin CLI wrapper that delegates video NAL extraction to
:mod:`mdt_rescue.engine.video`.

Preserves the legacy v0.1 contract bit-exact:

- argv: ``<input.mdt> <output.h264>`` (positional, exactly 2 args).
- exit code: ``0`` on success, ``1`` on usage error.
- stdout: identical line-for-line to the original standalone script,
  including the per-chunk progress line, the optional safety-net
  warning (printed BEFORE its chunk line), the DONE footer, the NAL
  types breakdown, and the conditional sidecar message.
- side-effect: when both the first SPS and the first PPS are present
  inside the .MDT (rare for GH5S FHD25 200M), a sidecar prefix file
  ``<output>_sps_pps_from_mdt.h264`` is written alongside the output.

This wrapper is part of Phase B.2 of the MDT Rescue Toolkit refactor:
all extraction logic lives in :mod:`mdt_rescue.engine.video`; this
script is a CLI shim that produces legacy-compatible stdout and
side effects.

Part of MDT Rescue Toolkit -
https://github.com/alicchiog/mdt-rescue-toolkit
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# Fallback import guard: ensure the repo root is on sys.path so this
# script works even when the venv is not active.  Pattern reused from
# scripts/extract_sps_pps.py (B.1.wrapper).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mdt_rescue.engine.video import (  # noqa: E402
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    START_CODE,
    VideoExtractionResult,
    VideoProgressEvent,
    extract_video,
    nal_type_name,
)


def _make_progress_callback() -> Callable[[VideoProgressEvent], None]:
    """Build a progress callback that emits legacy stdout bit-exact.

    The callback handles two stdout blocks per chunk:

    - **Block B-warn** (conditional): if ``event.warning`` is not
      None, print the warning string PREFIXED by the legacy two-space
      indent, then flush.  The engine emits ``event.warning`` without
      any leading indent, so the wrapper adds it here.
    - **Block B**: the per-chunk progress line, with the exact legacy
      format specifiers (5.1f %, 5.2f GB, thousands separators,
      ``MB`` sticky suffix, double-space field separators).

    Both lines flush immediately so they appear in real time when the
    parent shell pipes stdout through ``tee``.
    """

    def cb(event: VideoProgressEvent) -> None:
        if event.warning is not None:
            # Engine warnings carry no leading indent; legacy stdout
            # printed them indented by 2 spaces, BEFORE the chunk
            # progress line.
            print(f"  {event.warning}", flush=True)

        pct = 100.0 * event.file_pos / event.total_size
        gb = event.file_pos / (1024 ** 3)
        sps_flag = "Y" if event.has_first_sps else "N"
        pps_flag = "Y" if event.has_first_pps else "N"
        print(
            f"  chunk#{event.chunk_num} {pct:5.1f}%  ({gb:5.2f} GB read)  "
            f"frames={event.frames:,}  nals={event.nals:,}  "
            f"written={event.written / (1024 ** 2):.0f}MB  "
            f"bad={event.bad_blocks:,}  "
            f"SPS={sps_flag}  "
            f"PPS={pps_flag}  "
            f"tail={event.tail_size / (1024 ** 2):.1f}MB",
            flush=True,
        )

    return cb


def _print_header(
    input_path: str, output_path: str, total_size: int
) -> None:
    """Emit Block A: 4 header lines + 1 blank line.

    The path strings are printed RAW as received from argv (no
    normalisation, no Path conversion), so users who pass relative or
    tilde-prefixed paths see the same string back, matching legacy
    behaviour byte-for-byte.

    Chunk size and overlap come from the engine defaults
    (256 MB / 10 MB), which match the legacy hard-coded values.
    """
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Total size to scan: {total_size / (1024 ** 3):.2f} GB")
    print(
        f"Chunk size: {DEFAULT_CHUNK_SIZE / (1024 ** 2):.0f} MB, "
        f"fallback overlap {DEFAULT_OVERLAP / (1024 ** 2):.0f} MB"
    )
    print()


def _print_footer(result: VideoExtractionResult) -> None:
    """Emit Block C (DONE summary) + Block D (NAL types found).

    Field-width spacing mirrors the legacy script exactly:
    labels are padded so values land at column 24 of the line.
    NAL type entries are sorted ascending by type number; the type
    name is left-aligned in an 8-char field and the count is
    right-aligned in a 10-char field with thousands separator.
    """
    # Block C: DONE summary.
    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Frames with VCL:       {result.frames:,}")
    print(f"NAL units written:     {result.nals:,}")
    print(
        f"Output size:           "
        f"{result.bytes_written / (1024 ** 2):.1f} MB "
        f"({result.bytes_written / (1024 ** 3):.2f} GB)"
    )
    print(f"Bad/empty blocks:      {result.bad_blocks:,}")
    print(f"First SPS in .MDT:     {'YES' if result.first_sps else 'NO'}")
    print(f"First PPS in .MDT:     {'YES' if result.first_pps else 'NO'}")

    # Block D: NAL types found.
    print("\nNAL types found:")
    for k in sorted(result.nal_type_counts):
        count = result.nal_type_counts[k]
        print(
            f"  type {k:2d} ({nal_type_name(k):8s}): "
            f"{count:>10,}"
        )


def _maybe_write_sidecar(
    output_path: str, result: VideoExtractionResult
) -> None:
    """Emit Block E and write the sidecar file IFF both SPS and PPS
    were captured inside the .MDT.

    The sidecar path is derived from ``output_path`` by replacing the
    ``.h264`` suffix with ``_sps_pps_from_mdt.h264``; if the
    replacement does not change the string (i.e. the output has no
    ``.h264`` suffix), the suffix is appended.  The file contains
    ``START_CODE + first_sps`` followed by ``START_CODE + first_pps``
    in two separate writes, matching the legacy script.

    For the GH5S FHD25 200M profile this branch is normally inert:
    the .MDT does not embed SPS/PPS, so neither the file nor the
    stdout block is produced.  Kept here for parity with other
    Panasonic firmwares.
    """
    if not (result.first_sps and result.first_pps):
        return

    prefix_path = output_path.replace('.h264', '_sps_pps_from_mdt.h264')
    if prefix_path == output_path:
        prefix_path = output_path + '_sps_pps_from_mdt.h264'

    with open(prefix_path, 'wb') as f:
        f.write(START_CODE + result.first_sps)
        f.write(START_CODE + result.first_pps)

    print(f"\nSaved SPS+PPS from .MDT to: {prefix_path}")
    print(f"  SPS: {len(result.first_sps)} bytes")
    print(f"  PPS: {len(result.first_pps)} bytes")


def main(argv: list[str]) -> int:
    """CLI entry point.

    Returns the process exit code: ``0`` on success, ``1`` on usage
    error.  Runtime errors (missing input file, write failure, ...)
    propagate as standard Python tracebacks, mirroring the legacy
    script which had no custom error handling.
    """
    if len(argv) != 3:
        print(
            "Usage: python3 extract_video_gh5s_fhd25_alli.py "
            "<input.mdt> <output.h264>"
        )
        return 1

    # Preserve argv strings raw for stdout output and for the engine
    # call.  The engine accepts Path | str and normalises internally.
    input_path = argv[1]
    output_path = argv[2]
    total_size = Path(input_path).stat().st_size

    _print_header(input_path, output_path, total_size)

    result = extract_video(
        input_path,
        output_path,
        progress=_make_progress_callback(),
    )

    _print_footer(result)
    _maybe_write_sidecar(output_path, result)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
