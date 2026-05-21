# MDT Rescue Toolkit

> A profile-based recovery toolkit for unfinalized `.MDT` video files.

**Version:** 0.1.0
**Status:** Experimental
**Supported profile:** Panasonic GH5S, FHD 1920×1080, 25 fps PAL, H.264 High 4:2:2 Intra, ALL-I 200M

---

## Born from a real recovery case

This toolkit was built during a single weekend to rescue a 33 GB `.MDT` file produced by a Panasonic DC-GH5S whose recording was interrupted by the well-known *"Battery Grip of Death"* event during a student podcast (`Fill the Blanc`, Luiss Business School).

The file was abandoned by the camera in an unfinalized state: no `moov` atom, no SPS/PPS in headers, just raw AVC-Intra video and PCM audio chunks interleaved with timecodes. Untrunc failed. Commercial tools either didn't recognize the format (`.MDT`) or asked 89€ for a one-shot recovery.

After hours of binary analysis, scripts iterated through several versions, structured tests on 100 MB → 1 GB → full file, we ended up with a complete recovered MOV: **2h 17m... well, actually 23m 33s** of pristine 4:2:2 10-bit video plus its original PCM audio, byte-for-byte identical to what the camera would have written if finalization had succeeded.

This toolkit packages what we learned. It is **not a generic `.MDT` recovery tool** — it works only on the exact profile we validated. If your situation matches, it will probably help. If not, it provides a template for analysis.

---

## What this toolkit does

Given:
- A corrupted `.MDT` file from a Panasonic GH5S in FHD 25p ALL-I 200M
- A "sane" reference `.MOV` recorded in the same session with the same settings

The toolkit produces:
- A clean `.MOV` file with video + audio properly interleaved
- The same codec, color space, bit depth, and bitrate as the camera's native output
- An ffprobe-verifiable file that imports natively into DaVinci Resolve, Final Cut, and Premiere

---

## Supported profile (v0.1)

This release supports **only one validated profile**:

| Parameter | Value |
|---|---|
| Camera model | Panasonic DC-GH5S |
| Container | `.MDT` (unfinalized MOV variant) |
| Video codec | H.264 High 4:2:2 Intra (AVC-Intra) |
| Profile | ALL-Intra 200 Mbps |
| Resolution | 1920 × 1080 |
| Frame rate | 25 fps PAL |
| Chroma | 4:2:2 |
| Bit depth | 10-bit |
| Audio codec | PCM s16 big-endian (`twos`) |
| Audio sample rate | 48000 Hz |
| Audio channels | 2 (stereo) |

**If your file does not match this profile, the toolkit will likely fail or produce garbage.** Future versions may add support for other profiles (FHD 50p, 4K, GH5 variants), but only based on validated test cases.

---

## Requirements

- macOS or Linux (tested on macOS Sequoia)
- Python 3.8+
- `ffmpeg` and `ffprobe` (8.0+ recommended)
- A "sane" reference `.MOV` from the same recording session (or another GH5S file with identical settings)
- Free disk space: **at least 3× the size of your corrupted `.MDT`** (working files + final output)

Install ffmpeg on macOS:
```bash
brew install ffmpeg
```

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/mdt-rescue-toolkit.git
cd mdt-rescue-toolkit
chmod +x recover_gh5s_fhd25_alli.sh scripts/verify_output.sh
```

No other dependencies. The Python scripts use only the standard library.

---

## Usage

```bash
./recover_gh5s_fhd25_alli.sh /path/to/broken.mdt /path/to/sane_reference.mov
```

The script will:
1. Check that `ffmpeg` and `ffprobe` are available
2. Verify both input files exist
3. Check available disk space
4. Create an output directory `recovery_output_<basename>/`
5. Run the full recovery pipeline (extraction + mux + verification)
6. Log every step to `recovery_log.txt`
7. Produce `<basename>_RESCUED.mov` as the final result

**The original `.MDT` file is never modified.** The toolkit only reads from it.

### Example

```bash
./recover_gh5s_fhd25_alli.sh P1194247.mdt P1194245.MOV
```

After ~30-40 minutes (HDD speed dependent), you will get:

```
recovery_output_P1194247/
├── P1194247_video.h264              # extracted H.264 stream
├── P1194247_video_with_header.h264  # with SPS/PPS prepended
├── P1194247_video_only.mov          # video-only MOV
├── P1194247_audio.raw               # raw PCM s16be
├── P1194247_audio.wav               # WAV
├── P1194247_RESCUED.mov             # final muxed video+audio
└── recovery_log.txt                 # log of the run
```

---

## What if it doesn't work?

The toolkit may fail or produce a broken file if:

- Your file is not from a GH5S, or not in FHD 25p ALL-I 200M
- The sane reference doesn't match the broken file's encoding parameters
- The corruption is too severe (file truncated near the start, before any video frames)
- The audio interleaving pattern doesn't match the validated model

In these cases, options:

1. **Try a commercial recovery service.** [Aeroquartet Treasured](https://www.aeroquartet.com/) handled our exact case and would have worked. Around 89 EUR. They offer a free trial that shows you the recovered content before you pay.
2. **Try [GRAU Video Repair](https://www.video-repair.com/).** Another commercial option for video repair.
3. **Try [Untrunc](https://github.com/anthwlock/untrunc).** Open source. It did not work for our specific case but may work for other situations.
4. **Inspect your `.MDT`** with `xxd` and compare to the validated pattern in `profiles/gh5s_fhd25_alli_200m.md`. If the structure differs significantly, this toolkit is not for you.

---

## Warning and disclaimer

This tool is **experimental**. It is not affiliated with Panasonic. It was built from a single real recovery case and currently supports only one validated profile. **Always work on copies** of your corrupted file. **No recovery is guaranteed.**

The authors and contributors of this toolkit accept no liability for any data loss, time loss, or other damages arising from its use. By using this software you agree that you do so at your own risk.

If your footage is irreplaceable and time-critical, consider a paid recovery service as a parallel option. This toolkit is a free alternative, not a replacement for professional recovery in high-stakes scenarios.

---

## Project structure

```
mdt-rescue-toolkit/
├── README.md                              # this file
├── LICENSE                                # MIT
├── .gitignore
├── recover_gh5s_fhd25_alli.sh             # main orchestration script
├── scripts/
│   ├── extract_video_gh5s_fhd25_alli.py   # video NAL extraction (streaming/chunked)
│   ├── extract_audio_gh5s_fhd25_alli.py   # audio chunk extraction with silence injection
│   ├── extract_sps_pps.py                 # SPS/PPS extraction from reference
│   └── verify_output.sh                   # final ffprobe verification
├── profiles/
│   └── gh5s_fhd25_alli_200m.md            # detailed format description
├── docs/
│   ├── recovery_workflow.md               # step-by-step pipeline explanation
│   ├── troubleshooting.md                 # common failure modes
│   └── limitations.md                     # what this toolkit does NOT do
└── examples/
    └── commands.md                        # example invocations
```

---

## Contributing

If you have a `.MDT` file that matches the supported profile and the toolkit fails on it, please open an issue with:
- Your camera model and firmware version
- The recording settings (resolution, framerate, codec)
- Output of `xxd -l 256 your_file.mdt | head -20`
- The exact error from `recovery_log.txt`

If you have a `.MDT` from a different profile and want to extend the toolkit, please open a discussion before submitting a PR. We want to keep the project conservative and well-tested.

---

## License

MIT. See `LICENSE`.

---

## Acknowledgments

This toolkit exists thanks to a stubborn refusal to lose a podcast pilot recording. Thanks to all student podcasts everywhere — your "I have a bad feeling about this" moments are why we write tools like this.
