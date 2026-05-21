# Troubleshooting

Common failure modes for `MDT Rescue Toolkit v0.1`, in roughly the order
you'll hit them while running the pipeline. Each section explains:

- What the symptom looks like
- The most likely cause
- What to try

If none of these fits, please open an issue with your `recovery_log.txt`.

---

## "command not found: ffmpeg" / "command not found: python3"

**Symptom.** The script exits immediately with one of these errors.

**Cause.** Required dependencies are not in your `PATH`.

**Fix.**
- On macOS:
  ```
  brew install ffmpeg python3
  ```
- On Linux (Debian/Ubuntu):
  ```
  sudo apt install ffmpeg python3
  ```

Verify with:
```
ffmpeg -version
ffprobe -version
python3 --version
```

---

## "input .MDT not found" / "reference .MOV not found"

**Symptom.** The script exits with an error mentioning your input path.

**Cause.** Paths with spaces or special characters need to be quoted.

**Fix.** Always wrap arguments in double quotes:

```
./recover_gh5s_fhd25_alli.sh \
    "/Volumes/My Drive/footage/P1194247.mdt" \
    "/Volumes/My Drive/footage/P1194245.mov"
```

---

## "not enough free space on output volume"

**Symptom.** The script exits before doing any extraction.

**Cause.** The toolkit needs about **twice** the size of the broken `.MDT`
available on the volume where you're writing the output.

**Fix.** Either:
- Free up space on that volume, or
- Move the broken `.MDT` to a volume with more room. (Remember: work on a
  copy, never the original.)

---

## "SPS/PPS extraction failed"

**Symptom.** Step 2 fails. Log says `ERROR: SPS=False, PPS=False`.

**Cause.** The reference `.MOV` does not contain valid H.264 SPS/PPS in
its `avcC` box. Most likely:
- It's not actually an H.264 file (e.g. it's a ProRes proxy).
- It's a `.MDT` itself, not a finalized `.MOV`.

**Fix.** Run `ffprobe reference.mov` and confirm that the video stream is
`h264`. If it's not, find a different reference that is.

---

## Video extraction finishes but the file looks too small

**Symptom.** Step 3 runs to completion but the output `.h264` is much
smaller than the input `.MDT` (e.g. 200 MB out of 33 GB).

**Cause.** The `.MDT` does not match the validated profile. The AUD
anchor `00 00 00 02 09 10` was rarely or never found, so only a tiny
fraction of the file was interpreted as video.

**Fix.** Confirm your file matches the validated profile (see
[`profiles/gh5s_fhd25_alli_200m.md`](../profiles/gh5s_fhd25_alli_200m.md))
by running:

```
xxd -l 256 your_file.mdt | head -20
```

You should see the AUD anchor pattern `00 00 00 02 09 10` somewhere
within the first few hundred bytes after the `mdat` atom header. If it's
missing, your file is from a different profile and this toolkit is not
the right tool for it (yet).

---

## ffmpeg complains about "dimensions not set"

**Symptom.** Step 5 (video MOV wrap) prints `dimensions not set` or
`Could not write header (incorrect codec parameters ?)`.

**Cause.** The SPS/PPS prefix was not properly concatenated to the H.264
stream. ffmpeg cannot determine resolution/profile.

**Fix.**
1. Verify that `<basename>_sps_pps_prefix.h264` exists and is between 50
   and 200 bytes in size.
2. Verify that `<basename>_video_with_header.h264` is approximately the
   same size as `<basename>_video.h264` plus the prefix.
3. Re-run the pipeline. If the same error persists, the reference MOV's
   SPS/PPS are likely incompatible with the broken file's H.264 stream.
   Try a different reference recorded with **identical** camera settings.

---

## ffmpeg prints `non-existing PPS X referenced` warnings

**Symptom.** During step 5 (wrap) or step 8 (mux), ffmpeg prints a few
warnings like:
```
non-existing PPS 4 referenced
non-existing PPS 205 referenced
pps_id 399 out of range
```

**Cause.** A small number of false-positive NAL units (a few out of
hundreds of thousands) were accidentally included by the extractor.
These have invalid header bytes that get interpreted by ffmpeg as
references to nonexistent parameter sets.

**Impact.** Negligible. The decoder marks the affected frames as
"no frame!" and continues. Visible artifacts on the validated test case
were zero.

**Fix.** Not required. If you have a use case where even a 0.001% NAL
contamination matters, please open an issue describing the use case.

---

## Audio has clicks every ~0.48 seconds

**Symptom.** The recovered audio sounds OK but with a "metronome" click
every half second.

**Cause.** The 48 bytes of timecode at the end of each audio chunk were
not stripped — they're being interpreted as PCM samples.

**Fix.** Re-run the pipeline. If the symptom persists, the audio chunk
layout on your file differs from the validated profile (timecode might
be at the start of the chunk rather than the end, or have a different
length). Inspect a chunk manually:

```
# Read 96 bytes starting at the offset of the first non-zero audio gap
# (you'll find this in recovery_log.txt under "audio gaps":
xxd -s <offset> -l 96 your_file.mdt
```

Compare to the layout described in
[`profiles/gh5s_fhd25_alli_200m.md`](../profiles/gh5s_fhd25_alli_200m.md).

---

## A/V drift at the end of the clip

**Symptom.** Lip sync is correct at the start but drifts apart by some
fraction of a second by the end of the clip.

**Cause.** The audio extractor injected silence for in-cycle anomalies
that were actually fat "phase-change" blocks of real audio. The silence
restores the timeline but discards some audio content.

**Impact.** On the validated 33 GB / 23-minute file, the drift was
+0.4 s end-to-end after silence injection. Imperceptible during normal
viewing.

**Fix.** If your project requires sample-accurate sync, use the recovered
WAV as a sync reference and replace it with cleanly-recorded audio (e.g.
from an external recorder) in your NLE.

---

## ffprobe reports `nb_frames` < expected

**Symptom.** The verification step prints a frame count noticeably lower
than what you expected from the recording duration.

**Cause.** The `.MDT` was truncated. The camera stopped writing video
NALs at some point, but the file kept growing because the OS hadn't yet
flushed the truncation length. In practice, the toolkit recovered all
the video that was actually written.

**Fix.** This is not a tool bug — the data was never written to the file.
The original transcript of this project's first successful recovery
expected 2h 17m of video but found only 23 m 33 s. The remaining time
was simply never captured by the camera.

---

## Final MOV plays in DaVinci but not in VLC

**Symptom.** The rescued file plays cleanly in DaVinci Resolve, Final
Cut, Premiere, and QuickTime, but VLC shows a black screen with audio
only (or refuses to open it).

**Cause.** VLC has a quirk with the timescale of MOV containers that
contain AVC-Intra `ai12` streams produced from raw H.264. ffmpeg's
default timebase (`1/1200000` or similar) confuses VLC's demuxer.

**Impact.** Cosmetic. Every professional NLE handles the file correctly.

**Fix.** The `recover_gh5s_fhd25_alli.sh` pipeline already uses
`-video_track_timescale 25000` to mitigate this. If you still have
trouble with VLC specifically, re-mux explicitly:

```
ffmpeg -i RESCUED.mov \
    -map 0:v:0 -map 0:a:0 \
    -c copy \
    -video_track_timescale 25000 \
    RESCUED_for_vlc.mov
```

---

## "Conversion failed!" with no other detail

**Symptom.** Some ffmpeg step exits non-zero without a clear message.

**Fix.** Open `recovery_log.txt` and scroll up to find the last ffmpeg
invocation. Copy the exact ffmpeg command and run it manually with
`-loglevel debug` (replace `-loglevel warning`) to see the full trace.

---

## Still stuck?

If your situation isn't covered here, please open a GitHub issue with:

- Your camera model and firmware version
- The recording settings (resolution, framerate, codec, bitrate)
- The output of `ffprobe -v error -show_format your_reference.mov`
- The output of `xxd -l 256 your_broken.mdt | head -20`
- The full contents of `recovery_log.txt`
- A description of what failed and at which step

Please **do not** attach the broken `.MDT` itself (issue attachments are
limited to small files and recovery `.MDT` files are typically tens of
gigabytes). Instead, describe its size and the first 256 bytes' hex dump
as above.
