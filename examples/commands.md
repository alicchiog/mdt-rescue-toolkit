# Example Commands

Copy-paste recipes for common scenarios.

---

## Full automated recovery

The simplest use case — let the orchestrator do everything.

```bash
cd mdt-rescue-toolkit
./recover_gh5s_fhd25_alli.sh \
    "/path/to/broken.mdt" \
    "/path/to/reference.mov"
```

Output goes to `recovery_output_<basename>/` next to the broken `.MDT`.

---

## Just verify an already-recovered file

If you already have a rescued `.mov` and want to re-run the verification
without re-doing the whole pipeline:

```bash
./scripts/verify_output.sh /path/to/rescued.mov
```

Exit code 0 = all checks pass. Exit code 1 = at least one check warned.

---

## Just extract the SPS/PPS from a reference

Useful for understanding what your reference contains, or for adapting
the toolkit to a new profile.

```bash
# Step 1: convert reference to Annex B
ffmpeg -y -i reference.mov \
    -map 0:v:0 -c copy \
    -bsf:v h264_mp4toannexb \
    -f h264 ref_annexb.h264

# Step 2: extract the prefix
python3 scripts/extract_sps_pps.py ref_annexb.h264 prefix.h264
```

The script prints offsets and sizes of the SPS and PPS it found.

---

## Just extract the H.264 stream from a `.MDT`

If you want to inspect what the video extractor sees without doing the
full pipeline:

```bash
python3 scripts/extract_video_gh5s_fhd25_alli.py \
    /path/to/broken.mdt \
    /tmp/extracted_video.h264
```

The script prints per-chunk progress and a final summary including:
- Frames extracted
- NAL types found (mostly type 5 IDR and type 9 AUD)
- Bad blocks (should be 0 on a validated-profile file)

---

## Just extract the audio from a `.MDT`

Similar but for audio:

```bash
python3 scripts/extract_audio_gh5s_fhd25_alli.py \
    /path/to/broken.mdt \
    /tmp/extracted_audio.raw

# Then convert raw to WAV for listening:
ffmpeg -y -f s16be -ar 48000 -ac 2 \
    -i /tmp/extracted_audio.raw \
    /tmp/extracted_audio.wav

open /tmp/extracted_audio.wav    # macOS
```

The script's final summary reports:
- Frames scanned
- Normal audio chunks (~92208 bytes each)
- Silence-injected chunks (anomalies on-cycle)
- Unhandled anomalies (off-cycle, logged but not silenced)
- Total audio bytes and duration
- Drift vs. video duration

---

## Inspect the first bytes of a `.MDT`

If you're not sure your file matches the validated profile, look at its
header:

```bash
xxd -l 256 /path/to/broken.mdt | head -20
```

A GH5S FHD 25p ALL-I `.MDT` should show:
- `66 72 65 65` (= "free") near the start, followed by atom headers
- A `6D 64 61 74` ("mdat") atom header
- Then somewhere within the first few hundred bytes after mdat, the
  AUD anchor pattern: `00 00 00 02 09 10`

If you don't see the AUD anchor anywhere within the first KB, your file
is from a different profile.

---

## Probe a reference `.MOV` to confirm it matches

```bash
ffprobe -v error -show_streams /path/to/reference.mov | grep -E \
    'codec_name|profile|width|height|pix_fmt|sample_rate|channels|codec_tag_string'
```

Expected output for a GH5S FHD 25p ALL-I 200M reference:

```
codec_name=h264
codec_tag_string=ai12
profile=High 4:2:2 Intra
width=1920
height=1080
pix_fmt=yuv422p10le
codec_name=pcm_s16be
codec_tag_string=twos
sample_rate=48000
channels=2
```

If any of these differs, the reference is not compatible with this
toolkit version.

---

## Mux only — use existing extracted streams

Sometimes you have a working `<basename>_video_only.mov` and a working
`<basename>_audio.wav` (from a previous run that died at the mux step)
and want to retry only the mux:

```bash
ffmpeg -y \
    -i recovery_output_P1194247/P1194247_video_only.mov \
    -i recovery_output_P1194247/P1194247_audio.wav \
    -map 0:v:0 -map 1:a:0 \
    -c:v copy \
    -c:a pcm_s16be \
    -video_track_timescale 25000 \
    -shortest \
    recovery_output_P1194247/P1194247_RESCUED.mov
```

---

## Clean up intermediate files

The pipeline keeps everything in `recovery_output_<basename>/` for
debugging. Once you've verified that `_RESCUED.mov` is good, you can
safely delete the intermediates:

```bash
cd recovery_output_P1194247/
ls -lh           # check sizes
rm P1194247_video.h264
rm P1194247_video_with_header.h264
rm P1194247_ref_annexb.h264
rm P1194247_audio.raw
# Keep: _video_only.mov (in case you need to re-mux),
#       _audio.wav (in case you need it separately),
#       _RESCUED.mov (the final output),
#       _sps_pps_prefix.h264 (tiny, useful for debugging),
#       recovery_log.txt (always keep)
```

You can also delete `_video_only.mov` and `_audio.wav` if `_RESCUED.mov`
is the only thing you need.

---

## Tune the chunk size

If you're running on a machine with very little RAM (< 1 GB free) or
with a faster disk where I/O isn't the bottleneck, you can edit
`CHUNK_SIZE` and `OVERLAP` at the top of the two Python extractor scripts:

```python
CHUNK_SIZE = 256 * 1024 * 1024   # default 256 MB
OVERLAP    = 10 * 1024 * 1024    # default 10 MB
```

Smaller `CHUNK_SIZE` uses less RAM but issues more disk seeks. Bigger
`CHUNK_SIZE` is faster on SSDs but uses more memory. Do not set
`OVERLAP` below 1 MB or you risk losing frames that straddle a chunk
boundary.

---

## Direct fastest-path for an emergency

If you understand the pipeline and just want to run the whole thing as
fast as possible without the orchestration script:

```bash
B=/path/to/broken.mdt
R=/path/to/reference.mov
O=$(dirname "$B")/recovery_output_$(basename "${B%.*}")
mkdir -p "$O"

# 1. SPS/PPS prefix
ffmpeg -y -i "$R" -map 0:v:0 -c copy -bsf:v h264_mp4toannexb -f h264 "$O/ref.h264"
python3 scripts/extract_sps_pps.py "$O/ref.h264" "$O/prefix.h264"

# 2. video
python3 scripts/extract_video_gh5s_fhd25_alli.py "$B" "$O/v.h264"
cat "$O/prefix.h264" "$O/v.h264" > "$O/vfull.h264"
ffmpeg -y -fflags +genpts -framerate 25 -f h264 -i "$O/vfull.h264" \
    -c copy -video_track_timescale 25000 "$O/v.mov"

# 3. audio
python3 scripts/extract_audio_gh5s_fhd25_alli.py "$B" "$O/a.raw"
ffmpeg -y -f s16be -ar 48000 -ac 2 -i "$O/a.raw" "$O/a.wav"

# 4. mux
ffmpeg -y -i "$O/v.mov" -i "$O/a.wav" \
    -map 0:v:0 -map 1:a:0 \
    -c:v copy -c:a pcm_s16be \
    -video_track_timescale 25000 -shortest \
    "$O/RESCUED.mov"

# 5. verify
./scripts/verify_output.sh "$O/RESCUED.mov"
```
