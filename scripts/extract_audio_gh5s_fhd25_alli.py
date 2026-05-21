#!/usr/bin/env python3
"""
extract_audio_gh5s_fhd25_alli.py
================================

Extracts PCM s16be 48kHz stereo audio from a Panasonic .MDT file
recorded by a DC-GH5S in FHD 1920x1080 25p ALL-I 200M mode.

Audio layout in the GH5S .MDT (validated empirically on one file)
-----------------------------------------------------------------
- Between consecutive video AUD anchors there is a "gap".
- Most gaps are zero bytes (no audio inline at that frame).
- Every ~12 video frames a gap of about 92208 bytes appears:
    [ 92160 bytes of PCM s16be stereo 48kHz ] + [ 48 bytes timecode ]
  i.e. 480 ms of audio + 12 uint32-BE incrementing counters.
- Three "fat" anomalous gaps were observed in the validated 33 GB file,
  positioned exactly 12 frames after the previous audio chunk. These
  are handled by injecting 92160 bytes of silence (0x00) to keep the
  audio timeline aligned with video. Unhandled anomalies (gaps that
  don't fit the cycle) are logged but NOT silenced.

The script streams the input in 256 MB chunks; the .MDT is never loaded
fully into RAM.

Part of MDT Rescue Toolkit - https://github.com/<your-user>/mdt-rescue-toolkit
"""

import sys
import os
import struct

AUD_ANCHOR = b"\x00\x00\x00\x02\x09\x10"
VALID_TYPES = {1, 5, 6, 7, 8, 9, 12}
MAX_NAL_SIZE = 5 * 1024 * 1024
MIN_NAL_SIZE = 1

EXPECTED_AUDIO_PAYLOAD = 92160
TIMECODE_SIZE = 48
EXPECTED_GAP_SIZE = EXPECTED_AUDIO_PAYLOAD + TIMECODE_SIZE  # 92208
SILENCE_BLOCK = b"\x00" * EXPECTED_AUDIO_PAYLOAD

CHUNK_SIZE = 256 * 1024 * 1024
OVERLAP = 10 * 1024 * 1024


def is_valid_nal_header(b):
    forbidden = (b >> 7) & 1
    nal_type = b & 0x1F
    return forbidden == 0 and nal_type in VALID_TYPES


def read_u32be(buf, pos):
    return struct.unpack(">I", buf[pos:pos + 4])[0]


def find_frame_end(buf, frame_start):
    """
    Walks NAL units within a video access unit and returns the offset
    just after the last NAL of that frame (i.e. the start of the next
    gap region). Returns None if the frame would overrun the buffer.
    """
    p = frame_start
    while p + 5 < len(buf):
        if p != frame_start and buf.startswith(AUD_ANCHOR, p):
            return p
        if p + 4 > len(buf):
            return None
        ln = read_u32be(buf, p)
        if ln < MIN_NAL_SIZE or ln > MAX_NAL_SIZE:
            return p
        if p + 4 + ln > len(buf):
            return None
        hdr = buf[p + 4]
        if not is_valid_nal_header(hdr):
            return p
        nt = hdr & 0x1F
        if p == frame_start and nt != 9:
            return None
        p += 4 + ln
    return None


def process_buffer(buf, buf_file_offset, out_file, state):
    first_aud = buf.find(AUD_ANCHOR)
    if first_aud == -1:
        return max(0, len(buf) - OVERLAP)

    pos = first_aud
    last_processed_end = pos

    while pos >= 0 and pos < len(buf) - 6:
        frame_start = pos

        frame_end = find_frame_end(buf, frame_start)
        if frame_end is None:
            return frame_start

        search_start = max(frame_end, frame_start + 6)
        next_pos = buf.find(AUD_ANCHOR, search_start)
        if next_pos == -1:
            return frame_start

        gap_size = next_pos - frame_end

        if gap_size == 0:
            state['gap_zero'] += 1

        elif 90000 <= gap_size <= 95000:
            # Normal audio chunk: keep first 92160 bytes, drop last 48.
            audio_start = frame_end
            audio_end = next_pos - TIMECODE_SIZE
            audio_data = buf[audio_start:audio_end]
            out_file.write(audio_data)
            state['audio_bytes'] += len(audio_data)
            state['normal_chunks'] += 1
            state['last_audio_at_frame'] = state['frames']

        else:
            # Anomalous gap.
            frames_since_last_audio = state['frames'] - state['last_audio_at_frame']
            file_offset = buf_file_offset + frame_end

            if frames_since_last_audio == 12:
                # Anomaly hits exactly where an audio chunk was expected.
                # Inject silence to keep audio timeline aligned with video.
                out_file.write(SILENCE_BLOCK)
                state['audio_bytes'] += EXPECTED_AUDIO_PAYLOAD
                state['silence_inserted'] += 1
                state['last_audio_at_frame'] = state['frames']
                state['handled_anomalies'].append({
                    'frame_idx': state['frames'],
                    'gap_size': gap_size,
                    'file_offset': file_offset,
                })
            else:
                # Anomaly off-cycle: log only, do not silence-pad blindly.
                state['unhandled_anomalies'].append({
                    'frame_idx': state['frames'],
                    'gap_size': gap_size,
                    'file_offset': file_offset,
                    'frames_since_last_audio': frames_since_last_audio,
                })

        state['frames'] += 1
        last_processed_end = next_pos

        if next_pos <= frame_start:
            return last_processed_end
        pos = next_pos

    return last_processed_end


def extract(input_path, output_raw):
    total_size = os.path.getsize(input_path)

    print(f"Input:  {input_path}")
    print(f"Output: {output_raw}")
    print(f"Total size to scan: {total_size / (1024 ** 3):.2f} GB")
    print(f"Chunk size: {CHUNK_SIZE / (1024 ** 2):.0f} MB, "
          f"fallback overlap {OVERLAP / (1024 ** 2):.0f} MB")
    print()

    state = {
        'frames': 0,
        'normal_chunks': 0,
        'silence_inserted': 0,
        'audio_bytes': 0,
        'gap_zero': 0,
        'last_audio_at_frame': -1,
        'handled_anomalies': [],
        'unhandled_anomalies': [],
    }

    with open(input_path, "rb") as fin, open(output_raw, "wb") as fout:
        file_pos = 0
        tail_buf = b""
        tail_file_offset = 0
        chunk_num = 0

        while file_pos < total_size:
            to_read = min(CHUNK_SIZE, total_size - file_pos)
            fin.seek(file_pos)
            chunk = fin.read(to_read)

            buf_file_offset = tail_file_offset if tail_buf else file_pos
            buf = tail_buf + chunk
            chunk_num += 1

            last_end = process_buffer(buf, buf_file_offset, fout, state)

            if last_end >= len(buf):
                tail_buf = b""
            else:
                tail_buf = buf[last_end:]
                tail_file_offset = buf_file_offset + last_end

            if len(tail_buf) > 2 * OVERLAP:
                tail_buf = tail_buf[-OVERLAP:]
                tail_file_offset = buf_file_offset + (len(buf) - len(tail_buf))

            file_pos += to_read

            pct = 100.0 * file_pos / total_size
            gb = file_pos / (1024 ** 3)
            print(f"  chunk#{chunk_num} {pct:5.1f}%  ({gb:5.2f} GB read)  "
                  f"frames={state['frames']:,}  "
                  f"normal={state['normal_chunks']:,}  "
                  f"silence={state['silence_inserted']}  "
                  f"unhandled={len(state['unhandled_anomalies'])}  "
                  f"audio={state['audio_bytes'] / (1024 ** 2):.1f}MB",
                  flush=True)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    total_chunks = state['normal_chunks'] + state['silence_inserted']
    print(f"Frames scanned:                {state['frames']:,}")
    print(f"Zero gaps (no audio):          {state['gap_zero']:,}")
    print(f"Normal audio chunks:           {state['normal_chunks']:,}")
    print(f"Silence chunks inserted:       {state['silence_inserted']}")
    print(f"Unhandled anomalies:           {len(state['unhandled_anomalies'])}")
    print(f"Total audio chunks (effective): {total_chunks:,}")
    print(f"Audio bytes:                   {state['audio_bytes']:,} "
          f"({state['audio_bytes'] / (1024 ** 2):.2f} MB)")
    print(f"audio_bytes % 4 = {state['audio_bytes'] % 4} (must be 0)")
    print()

    video_duration = state['frames'] / 25.0
    audio_duration = state['audio_bytes'] / 192000
    drift = audio_duration - video_duration
    print(f"Video duration:        {video_duration:.3f} sec")
    print(f"Audio duration:        {audio_duration:.3f} sec")
    print(f"Drift (audio - video): {drift:+.3f} sec")
    print()

    if state['handled_anomalies']:
        print("--- HANDLED ANOMALIES (silence injected) ---")
        for a in state['handled_anomalies']:
            print(f"  frame {a['frame_idx']:>6,}  gap_size={a['gap_size']:>9,}  "
                  f"offset=0x{a['file_offset']:X}")
        print()

    if state['unhandled_anomalies']:
        print("--- UNHANDLED ANOMALIES (NOT injected, investigate) ---")
        for a in state['unhandled_anomalies']:
            print(f"  frame {a['frame_idx']:>6,}  gap_size={a['gap_size']:>9,}  "
                  f"frames_since_last_audio={a['frames_since_last_audio']}  "
                  f"offset=0x{a['file_offset']:X}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 extract_audio_gh5s_fhd25_alli.py "
              "<input.mdt> <output.raw>")
        sys.exit(1)

    extract(sys.argv[1], sys.argv[2])
