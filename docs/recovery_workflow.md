# Recovery Workflow

This document explains, step by step, what the `recover_gh5s_fhd25_alli.sh`
script does internally. Useful if you want to understand the pipeline, debug
a failure, or adapt the toolkit to a new profile.

---

## Overview

```
                            Healthy reference .MOV
                                       │
                                       ▼
                          (1) ffmpeg h264_mp4toannexb
                                       │
                                       ▼
                            Annex B H.264 stream
                                       │
                                       ▼
                          (2) extract_sps_pps.py
                                       │
                                       ▼
                              SPS/PPS prefix
                                       │
                                       │     Broken .MDT
                                       │           │
                                       │           ▼
                                       │   (3) extract_video_*.py
                                       │           │
                                       │           ▼
                                       │   Raw H.264 (Annex B)
                                       │           │
                                       ├──────(4) cat ──┐
                                       │                ▼
                                       │     Prefixed Annex B stream
                                       │                │
                                       │                ▼
                                       │     (5) ffmpeg -c copy -> .mov
                                       │                │
                                       │                ▼
                                       │      Video-only MOV
                                       │
                          Broken .MDT  │
                              │        │
                              ▼        │
                  (6) extract_audio_*.py
                              │        │
                              ▼        │
                       Raw PCM s16be   │
                              │        │
                              ▼        │
                    (7) ffmpeg -> WAV  │
                              │        │
                              ▼        │
                          WAV audio    │
                              │        │
                              └─(8) ffmpeg mux -c copy + pcm_s16be
                                       │
                                       ▼
                              Final RESCUED.mov
                                       │
                                       ▼
                          (9) verify_output.sh
```

---

## Step 1 — Reference MOV → Annex B

`ffmpeg` reads the healthy reference MOV and writes its video track out as
raw Annex B H.264, applying the `h264_mp4toannexb` bitstream filter:

```
ffmpeg -y -i reference.mov \
    -map 0:v:0 -c copy \
    -bsf:v h264_mp4toannexb \
    -f h264 reference_annexb.h264
```

The bitstream filter converts the AVCC-framed NAL units inside the MOV
container (length-prefixed) into Annex B framing (start-code-prefixed),
which is what Python parsers expect.

The video stream is **copied**, not re-encoded.

---

## Step 2 — Extract SPS/PPS prefix

`scripts/extract_sps_pps.py` scans the Annex B file, identifies the first
SPS NAL (type 7) and the first PPS NAL (type 8), and writes them — each
preceded by a 4-byte start code — to a small prefix file:

```
[ 00 00 00 01 ] [ SPS NAL bytes ]
[ 00 00 00 01 ] [ PPS NAL bytes ]
```

The script enforces a maximum SPS/PPS size of 200 bytes each, which
prevents the classic "false giant PPS" bug where a naive `data.split()`
implementation accidentally concatenates a PPS payload with downstream
NALs.

The script also handles both 4-byte (`00 00 00 01`) and 3-byte
(`00 00 01`) start codes in the input Annex B stream.

---

## Step 3 — Extract H.264 NAL units from the `.MDT`

`scripts/extract_video_gh5s_fhd25_alli.py` is the heart of the toolkit. It
streams the broken `.MDT` in 256 MB chunks (never loading the whole file
into RAM) and extracts the H.264 NAL units belonging to each video frame.

### How frame boundaries are detected

For each chunk, the script scans for the 6-byte **AUD anchor**:

```
00 00 00 02 09 10
```

This is the AVCC-framed Access Unit Delimiter NAL specific to the GH5S
ALL-I 200M format. Every video access unit in the `.MDT` begins with this
exact byte sequence. Crucially, the script does **not** parse audio or
timecode regions — it only knows where video frames are. This avoids the
false positives that less-targeted scanners produce when interpreting
arbitrary bytes as NAL headers.

### Per-frame extraction

From each AUD anchor, the script reads consecutive AVCC-framed NAL units:

```
[ 4 bytes : NAL length ] [ N bytes : NAL payload ]
[ 4 bytes : NAL length ] [ N bytes : NAL payload ]
...
```

It stops when:
- It encounters the next AUD anchor (= next frame), or
- It encounters a NAL with an invalid header byte (= end of video data
  for this frame, beginning of audio or timecode), or
- A NAL would overrun the current buffer (= frame spans into the next
  chunk; the frame is reprocessed from the start in the next iteration).

Each NAL is written to the output file in Annex B form:

```
[ 00 00 00 01 ] [ NAL payload bytes ]
```

### Tail handling between chunks

After processing each chunk, the script computes how much was actually
consumed and keeps the remaining bytes as a `tail_buf` to be prepended to
the next chunk. The guarantee is that `tail_buf` always contains only
**unprocessed** bytes — never re-written ones — so frames are never
duplicated at chunk boundaries.

If no AUD anchor is found in a chunk, the script falls back to keeping
the last 10 MB of the chunk as `tail_buf` (in case an anchor was split
across the boundary).

---

## Step 4 — Concatenate prefix + raw H.264

```
cat sps_pps_prefix.h264 video.h264 > video_with_header.h264
```

This produces a single Annex B file that begins with SPS+PPS, then the
extracted video stream. `ffmpeg` needs the parameter sets at (or near)
the start of the stream in order to know the resolution, profile, and
pixel format when it starts writing the MOV.

---

## Step 5 — Wrap into video-only MOV

```
ffmpeg -y -fflags +genpts -framerate 25 \
    -f h264 -i video_with_header.h264 \
    -c copy \
    -video_track_timescale 25000 \
    video_only.mov
```

Notes:
- `-c copy` means no re-encoding. The output MOV contains the same H.264
  bytes as the input.
- `-framerate 25` and `-video_track_timescale 25000` produce clean
  timestamps in the MOV: 25 fps progressive with a timescale of 25000
  ticks per second (1000 ticks per frame).
- ffmpeg may print a small number of "non-existing PPS X referenced"
  warnings if the .MDT extraction included a handful of false-positive
  NAL units. These are scattered, do not corrupt the visible output, and
  are decoded as "no frame!" by the decoder.

---

## Step 6 — Extract audio from `.MDT`

`scripts/extract_audio_gh5s_fhd25_alli.py` streams the `.MDT` again,
using the same AUD-anchor strategy as the video extractor, but this time
collecting the bytes between the end of one frame and the next AUD anchor.
These "gaps" are where audio and timecode live.

### Decision logic per gap

- **Gap size == 0** → no audio at this video frame, skip.
- **Gap size 90000–95000** → normal audio chunk:
  - Write the **first 92160 bytes** to the output (PCM payload).
  - Discard the **last 48 bytes** (timecode counters).
- **Gap size anything else** → anomalous:
  - If `frames_since_last_audio == 12` → the anomaly hits exactly where
    an audio chunk was due. Inject **92160 bytes of silence** to keep
    A/V sync. Log the event.
  - Otherwise → log the anomaly but **do not** invent data. The output
    will drift by 0.48 s for each unhandled anomaly, which is the correct
    behavior when we don't know what to do.

### Output

A single raw PCM file: `audio.raw`, big-endian 16-bit stereo at 48 kHz.

---

## Step 7 — Convert PCM to WAV

```
ffmpeg -y \
    -f s16be -ar 48000 -ac 2 -i audio.raw \
    audio.wav
```

This is purely a format change. Inside the WAV the audio is little-endian
(WAV's native format), so the final mux step will convert it back to
`pcm_s16be` for the MOV.

---

## Step 8 — Mux video + audio into final MOV

```
ffmpeg -y \
    -i video_only.mov \
    -i audio.wav \
    -map 0:v:0 -map 1:a:0 \
    -c:v copy \
    -c:a pcm_s16be \
    -video_track_timescale 25000 \
    -shortest \
    RESCUED.mov
```

Notes:
- `-c:v copy` again: the video is not re-encoded.
- `-c:a pcm_s16be` writes the audio back as big-endian PCM, which is the
  native GH5S `twos` audio format. The resulting MOV is byte-equivalent
  in audio format to what the camera would have written natively.
- `-shortest` ends the output at the shorter of the two streams. On the
  validated file, this resulted in a 0.4 s audio truncation, well within
  acceptable A/V sync tolerance for a 23-minute clip.

---

## Step 9 — Verify

`scripts/verify_output.sh` runs `ffprobe` against the rescued file and
prints a one-page summary plus a series of pass/warn checks:

- video codec must be `h264`
- resolution must be `1920 × 1080`
- pixel format must be `yuv422p10le`
- audio codec must be `pcm_s16be`
- audio sample rate must be `48000`
- audio channels must be `2`

If any check warns, the script exits with code 1 (file may still be
usable, but the user should inspect it manually).

---

## Notes for adaptation to other profiles

If you want to adapt this toolkit to a different camera or recording
mode, the key things you'll need to determine empirically are:

1. **The AUD anchor pattern.** This may be the same `00 00 00 02 09 10`
   on other Panasonic bodies, but it's not guaranteed.
2. **The audio chunk size and cycle.** GH5S 25p FHD uses 92160 bytes
   every 12 frames. GH5S 50p would use 46080 bytes every 24 frames if
   the encoder maintains 480 ms chunks. 4K modes may differ. The chunk
   size in bytes is always `sample_rate × channels × 2 × chunk_duration`.
3. **The timecode trailer size.** 48 bytes (12 × uint32 BE) on the
   validated profile. Other profiles may use different counter widths
   or no trailer at all.
4. **The SPS/PPS source.** If the broken `.MDT` happens to contain
   inline SPS/PPS NALs (some firmware versions do), you can use them
   directly and skip the reference MOV step. The video extractor saves
   any inline SPS/PPS it finds to a sidecar file, which the user can
   inspect.

Until each of these is validated against a real failing file from the
target profile, the toolkit should not advertise support for it.
