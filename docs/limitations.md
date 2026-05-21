# Limitations

This document is the most important page in the repository. It exists to
make sure nobody runs the toolkit on a file it can't help with, then loses
time discovering that the hard way.

The first release of `MDT Rescue Toolkit` is **intentionally narrow**.
What follows is the full list of things it does not handle.

---

## Camera models

Validated on: **Panasonic DC-GH5S only.**

Untested on:
- Panasonic GH5 (the original, non-S model)
- Panasonic GH5 II, GH6, S1, S1H, BS1H, BGH1
- Panasonic AG-CX series (AG-CX350, AG-CX10, etc.)
- Any other manufacturer's `.MDT` files (Sony FX series, Canon C-series,
  Blackmagic, Atomos recordings, etc.)

The AUD anchor pattern and audio interleaving structure are not
guaranteed to be identical across bodies. Even within the Panasonic
GH-series, firmware versions may differ. The toolkit does not refuse to
run on files from other cameras — it just won't produce correct output
on them.

---

## Resolutions

Validated on: **1920 × 1080 only.**

Untested on:
- 4096 × 2160 (DCI 4K)
- 3840 × 2160 (UHD 4K)
- 1920 × 1080 anamorphic
- 640 × 480 proxy modes

A 4K file's audio chunk size will be the same in bytes (audio is
resolution-independent), but the video frames will be much larger,
which means the per-chunk interleaving cycle may not be 12 frames.
The toolkit assumes 12 frames per audio chunk and will misalign sync
on resolutions that interleave differently.

---

## Frame rates

Validated on: **25 fps PAL only.**

Untested on:
- 23.976 fps NTSC film
- 24 fps cinema
- 29.97 fps NTSC
- 30 fps
- 50 fps PAL
- 59.94 fps NTSC
- 60 fps

On other framerates, the **audio chunk size will be different** because
the chunk represents a fixed duration in seconds (480 ms on the validated
profile) and the duration is tied to the number of frames per chunk.
- 25 fps: 12 frames × 40 ms/frame = 480 ms = 92 160 bytes
- 50 fps: 12 frames × 20 ms/frame = 240 ms = 46 080 bytes (hypothesis,
  unvalidated)
- 24 fps: 12 frames × 41.67 ms/frame = 500 ms = 96 000 bytes (hypothesis)

These are guesses. Do not trust them.

---

## Codecs

Validated on: **H.264 High 4:2:2 Intra, ALL-I 200 Mbps.**

Untested on:
- H.264 4:2:0 Long-GOP modes (IPB, ALL-IPB)
- H.264 4:2:0 ALL-I (e.g. 100 Mbps)
- H.265 / HEVC
- Apple ProRes
- Blackmagic RAW
- Sony XAVC-S / XAVC-I

The toolkit explicitly uses an AUD anchor pattern derived from AVC-Intra
ALL-I streams. Long-GOP H.264 has different AUD framing and a different
GOP structure (one IDR every N frames, with P/B frames in between), so
the per-frame extraction strategy doesn't apply.

---

## Bit depths and chroma

Validated on: **10-bit 4:2:2 (yuv422p10le).**

Untested on:
- 8-bit 4:2:0
- 8-bit 4:2:2
- 10-bit 4:2:0
- 12-bit anything

A different bit depth produces a different SPS (the SPS encodes
`bit_depth_luma_minus8` and `bit_depth_chroma_minus8`). The reference
MOV's SPS must match the broken `.MDT`'s actual encoding exactly.

---

## Audio formats

Validated on: **PCM s16be stereo 48 kHz.**

Untested on:
- 24-bit PCM (s24be)
- 96 kHz audio
- Mono audio
- 4-channel audio (e.g. GH5 with XLR adapter)

The audio extractor's chunk size (92 160 bytes) is hard-coded for the
validated profile. Different bit depth or sample rate or channel count
would require a different chunk size, and the silence-injection size
would also change.

---

## File state

Validated on: **`.MDT` truncated at the end** (no `moov` atom, payload
ends cleanly).

Untested on:
- Files truncated near the start (no `mdat` header, no first AUD)
- Files where `mdat` itself is corrupted (bad bytes scattered through
  the payload)
- Files with partial overwrites (e.g. another recording started on top
  of the broken one)
- Multi-segment recordings split across multiple `.MDT` files

The toolkit can survive a small amount of internal corruption (it skips
NALs whose header bytes are invalid), but it does not perform error
correction or "best-effort reconstruction" of severely damaged data.

---

## Output quality

The toolkit **does not re-encode** anything in the video path. Video
quality at the output is bit-identical to the source. So:

- Lost frames cannot be recovered (the data was never written by the
  camera).
- Frame artifacts inside an IDR cannot be repaired.
- A glitchy frame at frame N in the `.MDT` will be a glitchy frame at
  frame N in the rescued MOV.

The same is largely true for audio, except for the silence-injected
intervals (typically 480 ms each, a handful of times per 30-minute
clip) where original audio is replaced with digital silence.

---

## Sync accuracy

On the validated test file (33 GB, 23 minutes, 23 seconds), the
end-to-end A/V drift after recovery was **+0.4 seconds** of audio over
video, caused by silence injection at three "phase-change" anomalies.

On a different file with different anomaly counts, the drift will be
different. The toolkit reports the exact drift in the recovery log so
that the user can compensate in their NLE if needed.

The toolkit does **not** automatically stretch audio to match video.
That decision belongs to the user.

---

## What "validated" means in this document

"Validated" means: at least one real broken `.MDT` matching that exact
parameter has been processed end-to-end with this toolkit, and the
resulting MOV was visually and aurally verified across the entire
duration in DaVinci Resolve.

"Untested" does **not** mean "doesn't work" — it means nobody has tried
yet, or nobody has reported back. The toolkit is structured so that
adding a new validated profile is a matter of (a) acquiring one real
failing file matching that profile, (b) reverse-engineering the
anchor/chunk pattern for it, (c) adding a new wrapper script.

If you have a broken `.MDT` that doesn't match the validated profile
but you want to attempt recovery anyway: the scripts inside `scripts/`
can be invoked directly with custom arguments. They will warn you, but
they won't refuse. Read the source code first.

---

## When to use a commercial tool instead

This toolkit is a free alternative for one specific failure mode. There
are situations where paying for commercial recovery software is the
right answer:

- Your `.MDT` does not match the validated profile and you have a
  deadline.
- Your footage is irreplaceable (wedding, one-time event, paid client
  delivery).
- You are not comfortable running scripts from the command line.

Two professional tools that handle Panasonic `.MDT` recovery and have
real customer support:

- **Aeroquartet Treasured** — offers a free trial that recovers the
  full file and watermarks the preview, so you can verify the result
  before paying. The team responds to support emails in hours.
- **GRAU Video Repair** — broader format support, focused on broken
  MOV/MP4 video.

Neither is endorsed by this toolkit, but both have helped people in
the same situation that motivated this project. Compare your time and
risk against their fees, and choose the right tool.
