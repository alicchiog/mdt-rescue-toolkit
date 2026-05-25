#!/usr/bin/env python3
"""
extract_audio_gh5s_fhd25_alli.py
================================

Thin CLI wrapper around :mod:`mdt_rescue.engine.audio`.

Delegates audio extraction to the engine and reproduces the legacy
stdout byte-for-byte.  The engine performs all I/O and counter
bookkeeping; this wrapper is responsible only for argv parsing, exit
codes, and presentation of the legacy progress/footer/drift/anomaly
output consumed by ``recover_gh5s_fhd25_alli.sh``.

Legacy contract preserved:

* argv: ``<input.mdt> <output.raw>`` (exactly 2 positional arguments)
* exit 0 on success
* exit 1 on usage error, with the legacy usage message printed to stdout
* stdout layout: header (block A), per-chunk progress (block B),
  separator + DONE + counter footer (block C), drift summary (block D),
  handled anomalies list with trailing blank (block E, conditional),
  unhandled anomalies list with no trailing blank (block F, conditional)

The original audio-extraction logic now lives in
``mdt_rescue/engine/audio.py``.  This script remains the v0.1 CLI entry
point and is exercised by the v0.1 recovery pipeline.

Part of MDT Rescue Toolkit - https://github.com/alicchiog/mdt-rescue-toolkit
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------
# Fallback import guard
# ---------------------------------------------------------------------
#
# Allows running this script without an active virtualenv: if the
# ``mdt_rescue`` package cannot be found on ``sys.path``, prepend the
# repository root (one level above ``scripts/``) and retry.

try:
    from mdt_rescue.engine.audio import (
        AudioExtractionResult,
        AudioProgressEvent,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_OVERLAP,
        extract_audio,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mdt_rescue.engine.audio import (
        AudioExtractionResult,
        AudioProgressEvent,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_OVERLAP,
        extract_audio,
    )


# ---------------------------------------------------------------------
# Helpers — legacy stdout reconstruction
# ---------------------------------------------------------------------

def _make_progress_callback():
    """Build the inline callback that prints block B (per-chunk progress).

    Mirrors the legacy ``print(...)`` invocation at lines 206-212 of
    the pre-refactor script.  Format quirks preserved:

    * 2-space indent before ``chunk#``
    * percentage with width 5, one decimal (``{:5.1f}``)
    * GB read with width 5, two decimals (``{:5.2f}``)
    * ``frames`` and ``normal`` use the thousands separator
    * ``silence`` and ``unhandled`` do NOT use the thousands separator
    * ``audio={V:.1f}MB`` has no space between the value and ``MB``
    * ``flush=True`` to keep the progress visible under tee/log pipes
    """

    def _callback(event: AudioProgressEvent) -> None:
        pct = 100.0 * event.bytes_read / event.total_bytes
        gb = event.bytes_read / (1024 ** 3)
        audio_mb = event.audio_bytes / (1024 ** 2)
        print(
            f"  chunk#{event.chunk_num} {pct:5.1f}%  ({gb:5.2f} GB read)  "
            f"frames={event.frames:,}  "
            f"normal={event.normal_chunks:,}  "
            f"silence={event.silence_inserted}  "
            f"unhandled={event.unhandled_count}  "
            f"audio={audio_mb:.1f}MB",
            flush=True,
        )

    return _callback


def _print_header(input_path: str, output_path: str, input_size: int) -> None:
    """Block A: header + blank line.

    Matches legacy lines 157-162.  Quirks preserved:

    * ``Input:`` is followed by TWO spaces (visual alignment with
      ``Output:`` which has ONE space)
    * ``Total size to scan:`` uses ``.2f`` precision in GB
    * ``Chunk size:`` and ``fallback overlap`` both use ``.0f`` in MB
    """
    gb = input_size / (1024 ** 3)
    chunk_mb = DEFAULT_CHUNK_SIZE / (1024 ** 2)
    overlap_mb = DEFAULT_OVERLAP / (1024 ** 2)
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Total size to scan: {gb:.2f} GB")
    print(
        f"Chunk size: {chunk_mb:.0f} MB, "
        f"fallback overlap {overlap_mb:.0f} MB"
    )
    print()


def _print_footer(result: AudioExtractionResult) -> None:
    """Block C: separator + DONE + counter summary + blank.

    Matches legacy lines 214-227.  Quirks preserved:

    * Separator is ``=`` repeated **70** times (NOT 60 like video)
    * Leading ``\\n`` before the first separator: the per-chunk
      progress block does not emit its own trailing blank, so the
      footer adds one for visual separation
    * Most labels are padded to 31 characters, but
      ``Total audio chunks (effective):`` is already 31 chars then
      one space, so it totals 32 (legacy asymmetry, preserved)
    * ``Frames scanned``, ``Zero gaps``, ``Normal audio chunks``,
      ``Audio bytes``, and ``Total audio chunks (effective)`` use the
      thousands separator
    * ``Silence chunks inserted`` and ``Unhandled anomalies`` do NOT
      use the thousands separator
    * ``Audio bytes`` uses ``.2f`` MB precision (NOT ``.1f`` like the
      progress line)
    """
    audio_mb = result.audio_bytes / (1024 ** 2)
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    print(f"Frames scanned:                {result.frames:,}")
    print(f"Zero gaps (no audio):          {result.gap_zero:,}")
    print(f"Normal audio chunks:           {result.normal_chunks:,}")
    print(f"Silence chunks inserted:       {result.silence_inserted}")
    print(f"Unhandled anomalies:           {result.unhandled_count}")
    print(
        f"Total audio chunks (effective): "
        f"{result.total_effective_chunks:,}"
    )
    print(
        f"Audio bytes:                   {result.audio_bytes:,} "
        f"({audio_mb:.2f} MB)"
    )
    print(f"audio_bytes % 4 = {result.audio_bytes_mod_4} (must be 0)")
    print()


def _print_drift(result: AudioExtractionResult) -> None:
    """Block D: drift diagnostic + blank.

    Matches legacy lines 232-235.  Quirks preserved:

    * Labels padded to 23 characters (NOT 31 like the footer)
    * Durations use ``.3f`` precision in seconds
    * Drift uses ``:+.3f`` to ALWAYS show an explicit sign
      (drift = 0.0 prints as ``+0.000``, not ``0.000``)
    """
    print(f"Video duration:        {result.video_duration_sec:.3f} sec")
    print(f"Audio duration:        {result.audio_duration_sec:.3f} sec")
    print(f"Drift (audio - video): {result.drift_sec:+.3f} sec")
    print()


def _print_anomalies(result: AudioExtractionResult) -> None:
    """Blocks E and F: anomaly lists, both conditional.

    Matches legacy lines 237-249.  Quirks preserved:

    * Block E (handled): header + N rows + **trailing blank line**
    * Block F (unhandled): header + N rows + **NO trailing blank**
      (legacy asymmetry — F is the last output of the program)
    * Row format for both:
        - 2-space indent
        - ``frame {N:>6,}`` right-aligned width 6 with thousands sep
        - ``gap_size={N:>9,}`` right-aligned width 9 with thousands sep
        - ``offset=0x{N:X}`` uppercase hex, no width padding
    * Block F adds ``frames_since_last_audio={N}`` (no padding, no
      thousands separator) between gap_size and offset
    """
    if result.handled_anomalies:
        print("--- HANDLED ANOMALIES (silence injected) ---")
        for a in result.handled_anomalies:
            print(
                f"  frame {a.frame_idx:>6,}  "
                f"gap_size={a.gap_size:>9,}  "
                f"offset=0x{a.file_offset:X}"
            )
        print()
    if result.unhandled_anomalies:
        print("--- UNHANDLED ANOMALIES (NOT injected, investigate) ---")
        for a in result.unhandled_anomalies:
            print(
                f"  frame {a.frame_idx:>6,}  "
                f"gap_size={a.gap_size:>9,}  "
                f"frames_since_last_audio={a.frames_since_last_audio}  "
                f"offset=0x{a.file_offset:X}"
            )


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    """Legacy CLI entry point.

    Returns 0 on success, 1 on usage error.  The legacy script printed
    its usage message to stdout (not stderr); this is preserved here.
    """
    if len(argv) != 3:
        print(
            "Usage: python3 extract_audio_gh5s_fhd25_alli.py "
            "<input.mdt> <output.raw>"
        )
        return 1

    input_path = argv[1]
    output_path = argv[2]
    input_size = os.path.getsize(input_path)

    _print_header(input_path, output_path, input_size)
    result = extract_audio(
        input_path,
        output_path,
        progress=_make_progress_callback(),
    )
    _print_footer(result)
    _print_drift(result)
    _print_anomalies(result)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
