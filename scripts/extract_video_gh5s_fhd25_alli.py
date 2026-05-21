#!/usr/bin/env python3
"""
extract_video_gh5s_fhd25_alli.py
================================

Extracts H.264 NAL units from a Panasonic .MDT file recorded by a DC-GH5S
in FHD 1920x1080 25p ALL-I 200M mode, and writes them in Annex B format
to be wrapped into a MOV by ffmpeg.

Pipeline strategy
-----------------
1. Find the GH5S AUD anchor pattern (00 00 00 02 09 10): a 4-byte length
   prefix (value 2) followed by a NAL header for an Access Unit Delimiter
   with primary_pic_type = 0. This marks the start of every video frame
   in the AVC-Intra ALL-I stream.
2. From each AUD anchor, read AVCC length-prefixed NALs until the next
   AUD anchor (or until a non-NAL byte aborts the frame).
3. Convert AVCC to Annex B by writing a 4-byte start code (00 00 00 01)
   before each NAL payload.
4. Stream the file in 256 MB chunks with overlap-based tail handling, so
   it never loads the full 33 GB into RAM.

Tail handling guarantee
-----------------------
process_buffer always returns the offset of the first byte that has NOT
been processed/written yet. The main loop then sets:
    tail_buf = buf[last_end:]
This guarantees no frame is ever written twice across chunk boundaries.

Profile validated for
---------------------
- Panasonic DC-GH5S
- 1920x1080, 25 fps PAL
- H.264 High 4:2:2 Intra, ALL-I 200M
- yuv422p10le

Part of MDT Rescue Toolkit - https://github.com/<your-user>/mdt-rescue-toolkit
"""

import sys
import os
import struct

# H.264 markers
START_CODE = b"\x00\x00\x00\x01"
# AUD with primary_pic_type=0, AVCC-framed (length=2 + NAL byte 0x09 + 0x10)
AUD_ANCHOR = b"\x00\x00\x00\x02\x09\x10"

# NAL unit types we accept inside a frame
VALID_TYPES = {
    1: "slice",
    5: "IDR",
    6: "SEI",
    7: "SPS",
    8: "PPS",
    9: "AUD",
    12: "filler",
}

MAX_NAL_SIZE = 5 * 1024 * 1024
MIN_NAL_SIZE = 1

CHUNK_SIZE = 256 * 1024 * 1024   # 256 MB per read
OVERLAP = 10 * 1024 * 1024        # 10 MB fallback if no AUD in buffer


def nal_type_name(t):
    return VALID_TYPES.get(t, "?")


def is_valid_nal_header(b):
    forbidden = (b >> 7) & 1
    nal_type = b & 0x1F
    return forbidden == 0 and nal_type in VALID_TYPES


def read_u32be(buf, pos):
    return struct.unpack(">I", buf[pos:pos + 4])[0]


def process_buffer(buf, out_file, state):
    """
    Process one buffer of bytes (typically tail_buf + freshly-read chunk).

    Returns the offset of the FIRST BYTE NOT YET PROCESSED. Cases:
    - No AUD found in buffer  -> return max(0, len(buf) - OVERLAP)
                                 (keep last OVERLAP bytes for a split AUD)
    - Some frames processed  -> return offset just after last written byte
    - A frame would overrun the buffer (had_overrun)
                             -> return frame_start, so it is reprocessed
                                from the beginning in the next chunk
    """
    first_aud = buf.find(AUD_ANCHOR)
    if first_aud == -1:
        return max(0, len(buf) - OVERLAP)

    pos = first_aud
    last_processed_end = pos

    while pos >= 0 and pos < len(buf) - 6:
        frame_start = pos
        p = pos
        frame_nals = []
        frame_has_vcl = False
        had_overrun = False

        # Walk NALs within this access unit, stopping at next AUD anchor.
        while p + 5 < len(buf):
            if p != frame_start and buf.startswith(AUD_ANCHOR, p):
                break

            if p + 4 > len(buf):
                had_overrun = True
                break

            ln = read_u32be(buf, p)

            if ln < MIN_NAL_SIZE or ln > MAX_NAL_SIZE:
                break

            if p + 4 + ln > len(buf):
                had_overrun = True
                break

            hdr = buf[p + 4]
            if not is_valid_nal_header(hdr):
                break

            nt = hdr & 0x1F

            # Each frame must begin with an AUD (type 9). If not, drop it.
            if len(frame_nals) == 0 and nt != 9:
                break

            payload = buf[p + 4:p + 4 + ln]

            # Stash first SPS/PPS encountered in the .MDT (rarely present).
            if nt == 7 and state['first_sps'] is None:
                state['first_sps'] = bytes(payload)
            if nt == 8 and state['first_pps'] is None:
                state['first_pps'] = bytes(payload)

            frame_nals.append((nt, payload))

            if nt in (1, 5):
                frame_has_vcl = True

            p += 4 + ln

        if had_overrun:
            # Frame spans into the next chunk: don't write anything,
            # leave it to be reprocessed from frame_start.
            return frame_start

        # Frame fully inside this buffer.
        if frame_nals and frame_has_vcl:
            for nt, payload in frame_nals:
                out_file.write(START_CODE)
                out_file.write(payload)
                state['nals'] += 1
                state['written'] += 4 + len(payload)
                state['counts'][nt] = state['counts'].get(nt, 0) + 1
            state['frames'] += 1
        else:
            state['bad_blocks'] += 1

        # Advance to the next AUD strictly after the current frame.
        search_start = max(p, frame_start + 6)
        next_pos = buf.find(AUD_ANCHOR, search_start)
        last_processed_end = p

        if next_pos == -1:
            return last_processed_end
        if next_pos <= frame_start:
            return last_processed_end
        pos = next_pos

    return min(pos if pos >= 0 else len(buf), len(buf))


def extract(input_path, output_path):
    total_size = os.path.getsize(input_path)

    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Total size to scan: {total_size / (1024 ** 3):.2f} GB")
    print(f"Chunk size: {CHUNK_SIZE / (1024 ** 2):.0f} MB, "
          f"fallback overlap {OVERLAP / (1024 ** 2):.0f} MB")
    print()

    state = {
        'counts': {},
        'frames': 0,
        'nals': 0,
        'written': 0,
        'bad_blocks': 0,
        'first_sps': None,
        'first_pps': None,
    }

    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        file_pos = 0
        tail_buf = b""
        chunk_num = 0

        while file_pos < total_size:
            to_read = min(CHUNK_SIZE, total_size - file_pos)
            fin.seek(file_pos)
            chunk = fin.read(to_read)

            buf = tail_buf + chunk
            chunk_num += 1

            last_end = process_buffer(buf, fout, state)

            if last_end >= len(buf):
                tail_buf = b""
            else:
                tail_buf = buf[last_end:]

            if len(tail_buf) > 2 * OVERLAP:
                # Safety net: should never trigger, but truncate if it does.
                print(f"  WARNING: tail size {len(tail_buf) / (1024 ** 2):.0f}MB "
                      f"> 2*overlap, truncating", flush=True)
                tail_buf = tail_buf[-OVERLAP:]

            file_pos += to_read

            pct = 100.0 * file_pos / total_size
            gb = file_pos / (1024 ** 3)
            print(f"  chunk#{chunk_num} {pct:5.1f}%  ({gb:5.2f} GB read)  "
                  f"frames={state['frames']:,}  nals={state['nals']:,}  "
                  f"written={state['written'] / (1024 ** 2):.0f}MB  "
                  f"bad={state['bad_blocks']:,}  "
                  f"SPS={'Y' if state['first_sps'] else 'N'}  "
                  f"PPS={'Y' if state['first_pps'] else 'N'}  "
                  f"tail={len(tail_buf) / (1024 ** 2):.1f}MB",
                  flush=True)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Frames with VCL:       {state['frames']:,}")
    print(f"NAL units written:     {state['nals']:,}")
    print(f"Output size:           {state['written'] / (1024 ** 2):.1f} MB "
          f"({state['written'] / (1024 ** 3):.2f} GB)")
    print(f"Bad/empty blocks:      {state['bad_blocks']:,}")
    print(f"First SPS in .MDT:     {'YES' if state['first_sps'] else 'NO'}")
    print(f"First PPS in .MDT:     {'YES' if state['first_pps'] else 'NO'}")
    print("\nNAL types found:")
    for k in sorted(state['counts']):
        print(f"  type {k:2d} ({nal_type_name(k):8s}): "
              f"{state['counts'][k]:>10,}")

    # If SPS/PPS were found inside the .MDT, save them as a sidecar prefix.
    # In the GH5S case validated so far they are NOT present, but other
    # firmware versions may include them.
    if state['first_sps'] and state['first_pps']:
        prefix_path = output_path.replace('.h264', '_sps_pps_from_mdt.h264')
        if prefix_path == output_path:
            prefix_path = output_path + '_sps_pps_from_mdt.h264'
        with open(prefix_path, 'wb') as f:
            f.write(START_CODE + state['first_sps'])
            f.write(START_CODE + state['first_pps'])
        print(f"\nSaved SPS+PPS from .MDT to: {prefix_path}")
        print(f"  SPS: {len(state['first_sps'])} bytes")
        print(f"  PPS: {len(state['first_pps'])} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 extract_video_gh5s_fhd25_alli.py "
              "<input.mdt> <output.h264>")
        sys.exit(1)

    extract(sys.argv[1], sys.argv[2])
