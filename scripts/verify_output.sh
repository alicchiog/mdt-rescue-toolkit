#!/usr/bin/env bash
# verify_output.sh
# ================
# Runs ffprobe checks on a rescued MOV file and prints a human-readable
# summary. Returns 0 if both video and audio streams look plausible for
# the GH5S FHD 25p ALL-I profile, 1 otherwise.
#
# Usage:
#   ./verify_output.sh path/to/rescued.mov
#
# Part of MDT Rescue Toolkit.

set -u

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <rescued.mov>" >&2
    exit 2
fi

INPUT="$1"

if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: file not found: $INPUT" >&2
    exit 2
fi

if ! command -v ffprobe >/dev/null 2>&1; then
    echo "ERROR: ffprobe not found in PATH" >&2
    exit 2
fi

echo "============================================================"
echo "Verifying: $INPUT"
echo "============================================================"

# Basic stream info
VIDEO_CODEC=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_W=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_H=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=height -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_FRAMES=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=nb_frames -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_DUR=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=duration -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_PIX=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=pix_fmt -of default=nw=1:nk=1 "$INPUT" || true)
VIDEO_PROFILE=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=profile -of default=nw=1:nk=1 "$INPUT" || true)

AUDIO_CODEC=$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=codec_name -of default=nw=1:nk=1 "$INPUT" || true)
AUDIO_SR=$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=sample_rate -of default=nw=1:nk=1 "$INPUT" || true)
AUDIO_CH=$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=channels -of default=nw=1:nk=1 "$INPUT" || true)
AUDIO_DUR=$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=duration -of default=nw=1:nk=1 "$INPUT" || true)

echo ""
echo "VIDEO STREAM"
echo "  codec:        ${VIDEO_CODEC:-?}"
echo "  profile:      ${VIDEO_PROFILE:-?}"
echo "  resolution:   ${VIDEO_W:-?}x${VIDEO_H:-?}"
echo "  pixel format: ${VIDEO_PIX:-?}"
echo "  frames:       ${VIDEO_FRAMES:-?}"
echo "  duration:     ${VIDEO_DUR:-?} s"
echo ""
echo "AUDIO STREAM"
echo "  codec:        ${AUDIO_CODEC:-?}"
echo "  sample rate:  ${AUDIO_SR:-?} Hz"
echo "  channels:     ${AUDIO_CH:-?}"
echo "  duration:     ${AUDIO_DUR:-?} s"
echo ""

# Plausibility checks for the validated GH5S FHD 25p profile.
OK=1

check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "  [OK]   $label: $actual"
    else
        echo "  [WARN] $label: expected '$expected', got '$actual'"
        OK=0
    fi
}

echo "PLAUSIBILITY CHECKS (against validated GH5S FHD 25p profile)"
check "video codec"  "h264"        "$VIDEO_CODEC"
check "width"        "1920"        "$VIDEO_W"
check "height"       "1080"        "$VIDEO_H"
check "pix_fmt"      "yuv422p10le" "$VIDEO_PIX"
check "audio codec"  "pcm_s16be"   "$AUDIO_CODEC"
check "sample rate"  "48000"       "$AUDIO_SR"
check "channels"     "2"           "$AUDIO_CH"

echo ""
if [[ $OK -eq 1 ]]; then
    echo "Verification: PASSED"
    exit 0
else
    echo "Verification: WARNINGS (file may still work, inspect manually)"
    exit 1
fi
