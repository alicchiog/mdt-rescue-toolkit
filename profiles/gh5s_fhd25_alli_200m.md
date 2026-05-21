# Profile: GH5S FHD 25p ALL-I 200M

> Validated profile for `MDT Rescue Toolkit v0.1`.

This document describes, in technical detail, the only profile this toolkit
has been validated against. If your `.MDT` file does not match all of the
parameters below, the toolkit will most likely fail or produce a damaged
output.

---

## Camera and recording mode

| Parameter            | Value                                |
|----------------------|--------------------------------------|
| Camera body          | Panasonic DC-GH5S                    |
| Recording mode       | AVC-Intra 200 Mbps (ALL-I)           |
| Container (target)   | `.MOV` (QuickTime, finalized)        |
| Container (recovered)| `.MDT` (intermediate, unfinalized)   |

The `.MDT` is the temporary file the camera writes while a clip is being
recorded. Under normal conditions, when the user stops recording, the camera
finalizes the file and renames it to `.MOV`, writing the `moov` atom at the
end and updating the `ftyp` / `mdat` headers. If recording is interrupted
abnormally (battery removal, power failure, "Battery Grip of Death" on the
GH5S), the file remains a `.MDT` with valid `mdat` payload but no `moov`.

---

## Video stream

| Parameter         | Value                              |
|-------------------|------------------------------------|
| Codec             | H.264 (AVC)                        |
| Profile           | High 4:2:2 Intra                   |
| Level             | 4.1                                |
| 4cc codec_tag     | `ai12` (AVC-Intra Class 100M)      |
| Resolution        | 1920 × 1080                        |
| Frame rate        | 25 fps (PAL, progressive)          |
| Pixel format      | yuv422p10le (4:2:2, 10-bit)        |
| Bit rate (nominal)| 200 Mbps                           |
| GOP structure     | ALL-Intra (every frame is an IDR)  |
| Color space       | BT.709                             |
| Field order       | Progressive                        |

Each video frame is encoded as one access unit. An access unit contains:
- 1 × AUD NAL (type 9), AVCC length-prefixed: 4 bytes length + 1 byte 0x09 + 1 byte 0x10
- 0 or more SEI NALs (type 6)
- 1 or more IDR slice NALs (type 5), one per slice tile

SPS (type 7) and PPS (type 8) NAL units are **not present** inside the `.MDT`
itself. They are usually carried in the `moov.trak.mdia.minf.stbl.stsd.avcC`
box of a finalized MOV. The toolkit therefore extracts SPS+PPS from a healthy
reference `.MOV` provided by the user and prepends them to the recovered
H.264 stream before wrapping.

### AUD anchor pattern

Each access unit in the `.MDT` begins with the exact byte sequence:

```
00 00 00 02 09 10
```

This is the AVCC-framed AUD NAL with primary_pic_type = 0. The toolkit uses
this sequence as a synchronization anchor when scanning the file, which
allows it to skip past the interleaved audio and timecode blocks without
having to parse them as H.264.

---

## Audio stream

| Parameter            | Value           |
|----------------------|-----------------|
| Codec                | PCM             |
| Sample format        | s16be (`twos`)  |
| Sample rate          | 48000 Hz        |
| Channels             | 2 (stereo)      |
| Bytes per sample     | 2 (16-bit)      |
| Bytes per audio frame| 4 (stereo)      |
| Nominal bitrate      | 1536 kbps       |

### Interleaving pattern (validated)

Audio is interleaved between video access units in the `.MDT`. The pattern
observed in the validated 33 GB test file is:

```
[AUD][video NALs] [AUD][video NALs] ... [AUD][video NALs]
                                          ^
                                          every ~12 video frames:
                                          92208 bytes of audio + timecode
                                          appear in the gap between
                                          consecutive AUDs.
```

A "normal" audio gap is exactly:

```
[ 92160 bytes : PCM s16be stereo 48 kHz, ~480 ms ]
[ 48 bytes    : 12 × uint32 big-endian timecode counters ]
```

`92160 = 48000 Hz × 2 channels × 2 bytes × 0.48 s`.

`48 = 12 frames × 4 bytes`, monotonically increasing counters.

### Cycle phase

The cycle is **12 video frames per audio chunk**. However, the phase
(`frame_index % 12` at which the audio chunk appears) is not constant
throughout the file. On the validated 33 GB recording, the phase changed
three times:

- ~53% of audio chunks at phase 11
- ~31% at phase 7
- ~16% at phase 2

Each phase change coincided with an "anomalous" giant gap whose size was
several hundred kilobytes — likely the camera flushing a buffer and
re-aligning the audio stream. The toolkit detects these anomalies by
checking `frames_since_last_audio == 12` instead of relying on
`frame_index % 12`, so phase changes are tolerated.

### Anomaly handling

The toolkit categorizes anomalous gaps (size outside 90000–95000 bytes) as:

- **Handled (silence-injected)** — gap occurs exactly 12 frames after the
  previous valid audio chunk. The toolkit writes 92160 bytes of zero PCM
  to preserve sync. Three such events were present in the validated file,
  yielding a total of 1.44 seconds of injected silence on a 23-minute
  clip. The resulting A/V drift is +0.4 s overall.

- **Unhandled (logged only)** — gap of unexpected size that does not match
  the cycle. The toolkit does not invent data for these; it logs the frame
  index and offset and continues. Zero such events were observed on the
  validated file.

---

## File header (`.MDT` byte layout, first 24 bytes)

The validated file begins with:

```
Offset  Bytes                                       Meaning
0x00    00 00 00 0C 66 72 65 65                     free atom (12 bytes)
0x08    00 00 00 00
0x0C    [larger atom header]                        mdat begins here
0x18    [first AVCC-framed NAL = AUD anchor]        first video frame
```

Subsequent bytes follow the AVCC + audio + timecode interleaving pattern
described above until end of file.

This toolkit does not actually parse the atom headers at the start of the
file. It simply locates the first AUD anchor (`00 00 00 02 09 10`) anywhere
in the file and starts decoding from there.

---

## Reference MOV requirements

The healthy reference `.MOV` provided as second argument to the toolkit
must contain a video track encoded with **the same SPS and PPS** as the
broken `.MDT`. In practice this means:

- Same camera body (GH5S preferred, GH5 untested)
- Same recording mode (ALL-Intra 200M)
- Same resolution (1920 × 1080)
- Same frame rate (25 fps PAL)
- Same chroma subsampling and bit depth (4:2:2 10-bit)
- Same color space (BT.709)

A `.MOV` recorded in a different session (different day, different shoot)
should work as long as the camera settings match exactly. A `.MOV` from a
different camera model is **not guaranteed** to produce compatible SPS/PPS.

---

## Source of truth

The values in this document were extracted by direct binary inspection of:

- `P1194247.mdt` — a 33 GB unfinalized GH5S file from a real recovery case.
- `P1194245.MOV` — a healthy 1.1 GB reference file from the same session.

If you have a different `.MDT` that you believe matches this profile but
the toolkit fails on it, please file an issue with the output of
`xxd -l 256 your_file.mdt` and your `recovery_log.txt`.
