#!/usr/bin/env python3
"""
extract_sps_pps.py - v0.1-compatible CLI wrapper
=================================================

Thin CLI wrapper that delegates to mdt_rescue.engine.sps_pps and
reproduces the v0.1.0 stdout messages and exit codes byte-for-byte.

This file used to contain the full SPS/PPS extraction logic (108
lines, MDT Rescue Toolkit v0.1.0). As of v0.2 the logic lives in
mdt_rescue.engine.sps_pps; this wrapper exists only to keep the
v0.1 bash pipeline (recover_gh5s_fhd25_alli.sh) working unchanged.

Usage (unchanged from v0.1.0):
    python3 extract_sps_pps.py <input_annexb.h264> <output_prefix.h264>

Exit codes (unchanged from v0.1.0):
    0  Success
    1  Wrong number of arguments, or SPS/PPS not found

Part of MDT Rescue Toolkit - https://github.com/alicchiog/mdt-rescue-toolkit
"""

import sys
from pathlib import Path

# Make the mdt_rescue package importable when this script is invoked
# directly (e.g. by recover_gh5s_fhd25_alli.sh) without the venv being
# active. The package lives at repo root / mdt_rescue / ...; this file
# lives at repo root / scripts / extract_sps_pps.py, so parents[1] is
# the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mdt_rescue.engine.sps_pps import (  # noqa: E402  (after sys.path tweak)
    SpsPpsNotFoundError,
    extract_sps_pps,
)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python3 extract_sps_pps.py <input_annexb.h264> <output_prefix.h264>")
        return 1

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    try:
        result = extract_sps_pps(input_path, output_path)
    except SpsPpsNotFoundError as e:
        print(f"ERROR: SPS={e.sps_found}, PPS={e.pps_found}")
        return 1

    # Reproduce v0.1.0 stdout messages bit-exact.
    print(f"Input size: {result.source_size / (1024 ** 2):.1f} MB")
    print(f"  Found SPS at offset 0x{result.sps_offset:X}, size {len(result.sps_bytes)} bytes")
    print(f"  Found PPS at offset 0x{result.pps_offset:X}, size {len(result.pps_bytes)} bytes")

    total = len(result.sps_bytes) + len(result.pps_bytes) + 8
    print(f"\nWrote prefix file: {output_path}")
    print(
        f"Total size: {total} bytes "
        f"(SPS {len(result.sps_bytes)} + PPS {len(result.pps_bytes)} + 2x 4-byte start code)"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
