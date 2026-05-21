#!/usr/bin/env python3
"""
extract_sps_pps.py
==================

Extracts the first valid SPS and PPS NAL units from a healthy reference
H.264 Annex B stream. The output is a small binary "prefix" file that
gets concatenated in front of the recovered raw H.264 to give ffmpeg
the parameter sets it needs to wrap the stream into a MOV.

Handles both 4-byte (00 00 00 01) and 3-byte (00 00 01) start codes.
Strictly limits SPS/PPS to plausible sizes (<= 200 bytes each), which
prevents the classic "false giant PPS" bug where lookahead-based parsers
accidentally absorb subsequent NAL units into the PPS payload.

Part of MDT Rescue Toolkit - https://github.com/<your-user>/mdt-rescue-toolkit
"""

import sys

MAX_SPS_SIZE = 200
MAX_PPS_SIZE = 200


def find_next_start_code(data, pos):
    """
    Scans `data` starting at `pos` for the next H.264 start code.
    Returns (offset, length) where length is 3 or 4, or (-1, 0) if none found.

    A 4-byte start code (00 00 00 01) is preferred at its own offset;
    we never report a 3-byte code that is actually the tail of a 4-byte one.
    """
    n = len(data)
    p = pos
    while p < n - 2:
        if data[p] == 0 and data[p + 1] == 0:
            if p + 3 < n and data[p + 2] == 0 and data[p + 3] == 1:
                return p, 4
            if data[p + 2] == 1:
                return p, 3
        p += 1
    return -1, 0


def extract(input_path, output_path):
    """
    Reads an Annex B H.264 file, finds the first valid SPS and PPS NAL units,
    and writes them (with 4-byte start codes) to output_path.

    Exits with status 1 if SPS or PPS cannot be found.
    """
    with open(input_path, "rb") as f:
        data = f.read()

    print(f"Input size: {len(data) / (1024 ** 2):.1f} MB")

    sps = None
    pps = None
    pos = 0

    while pos < len(data) and (sps is None or pps is None):
        sc_off, sc_len = find_next_start_code(data, pos)
        if sc_off == -1:
            break

        nal_start = sc_off + sc_len
        next_sc, _ = find_next_start_code(data, nal_start)
        nal_end = next_sc if next_sc != -1 else len(data)

        nal_size = nal_end - nal_start
        if nal_size < 1:
            pos = nal_start + 1
            continue

        nh = data[nal_start]
        forbidden = (nh >> 7) & 1
        nal_type = nh & 0x1F

        if forbidden == 0:
            if nal_type == 7 and sps is None and nal_size <= MAX_SPS_SIZE:
                sps = data[nal_start:nal_end]
                print(f"  Found SPS at offset 0x{sc_off:X}, size {len(sps)} bytes")
            elif nal_type == 8 and pps is None and nal_size <= MAX_PPS_SIZE:
                pps = data[nal_start:nal_end]
                print(f"  Found PPS at offset 0x{sc_off:X}, size {len(pps)} bytes")

        pos = nal_end

    if sps is None or pps is None:
        print(f"ERROR: SPS={sps is not None}, PPS={pps is not None}")
        sys.exit(1)

    start_code = b"\x00\x00\x00\x01"
    with open(output_path, "wb") as f:
        f.write(start_code + sps)
        f.write(start_code + pps)

    total = len(sps) + len(pps) + 8
    print(f"\nWrote prefix file: {output_path}")
    print(f"Total size: {total} bytes "
          f"(SPS {len(sps)} + PPS {len(pps)} + 2x 4-byte start code)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 extract_sps_pps.py <input_annexb.h264> <output_prefix.h264>")
        sys.exit(1)
    extract(sys.argv[1], sys.argv[2])
