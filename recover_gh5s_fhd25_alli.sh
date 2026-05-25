#!/usr/bin/env bash
#
# recover_gh5s_fhd25_alli.sh
#
# MDT Rescue Toolkit v0.1.0
# Main orchestration script for the GH5S FHD 25p ALL-I 200M profile.
#
# Usage:
#   ./recover_gh5s_fhd25_alli.sh <broken.mdt> <sane_reference.mov>
#
# This script:
#   1. Validates dependencies (ffmpeg, ffprobe, python3)
#   2. Validates input files exist and are readable
#   3. Checks available disk space (needs ~3x size of broken file)
#   4. Creates output directory
#   5. Extracts SPS/PPS from reference
#   6. Extracts video H.264 NAL units from broken .MDT (streaming)
#   7. Concatenates SPS/PPS + extracted video
#   8. Wraps video into MOV container
#   9. Extracts audio chunks (with silence injection for anomalies)
#   10. Converts raw audio to WAV
#   11. Muxes final video + audio MOV
#   12. Verifies output with ffprobe
#
# The original .MDT file is NEVER modified — only read.

set -e
set -u
set -o pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly VERSION="0.1.0"
readonly PROFILE="GH5S FHD 25p ALL-I 200M"

log() {
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$timestamp] $*"
}

log_to_file() {
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$timestamp] $*" >> "$LOG_FILE"
}

log_both() {
    log "$@"
    log_to_file "$@"
}

die() {
    local exit_code="$1"
    shift
    log "ERROR: $*"
    if [[ -n "${LOG_FILE:-}" ]] && [[ -f "${LOG_FILE:-}" ]]; then
        log_to_file "ERROR: $*"
        log_to_file "Exiting with code $exit_code"
    fi
    exit "$exit_code"
}

print_banner() {
    cat <<EOF

============================================================
MDT Rescue Toolkit v$VERSION
Profile: $PROFILE
============================================================
EOF
}

print_usage() {
    cat <<EOF
Usage: $0 <broken.mdt> <sane_reference.mov>

Arguments:
  broken.mdt           Path to the corrupted .MDT file from Panasonic GH5S
  sane_reference.mov   Path to a healthy .MOV from the same recording session
                       (same camera, same settings)

Example:
  $0 P1194247.mdt P1194245.MOV

The script will create a directory 'recovery_output_<basename>/' next to
the broken file and produce '<basename>_RESCUED.mov' inside it.

The original .MDT file is never modified.
EOF
}

check_dependencies() {
    log "Checking dependencies..."
    local missing=()

    for cmd in ffmpeg ffprobe python3; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "ERROR: Missing required commands: ${missing[*]}"
        echo ""
        echo "On macOS, install with:"
        echo "  brew install ffmpeg python3"
        echo ""
        echo "On Ubuntu/Debian:"
        echo "  sudo apt install ffmpeg python3"
        exit 1
    fi

    log "  ffmpeg:   $(ffmpeg -version 2>&1 | head -1)"
    log "  ffprobe:  $(ffprobe -version 2>&1 | head -1)"
    log "  python3:  $(python3 --version 2>&1)"
}

validate_inputs() {
    if [[ $# -lt 2 ]]; then
        print_usage
        exit 2
    fi

    BROKEN_MDT="$1"
    SANE_MOV="$2"

    if [[ ! -f "$BROKEN_MDT" ]]; then
        die 3 "Broken .MDT file not found: $BROKEN_MDT"
    fi

    if [[ ! -r "$BROKEN_MDT" ]]; then
        die 3 "Broken .MDT file is not readable: $BROKEN_MDT"
    fi

    if [[ ! -f "$SANE_MOV" ]]; then
        die 3 "Sane reference .MOV file not found: $SANE_MOV"
    fi

    if [[ ! -r "$SANE_MOV" ]]; then
        die 3 "Sane reference .MOV file is not readable: $SANE_MOV"
    fi

    BROKEN_MDT="$(cd "$(dirname "$BROKEN_MDT")" && pwd)/$(basename "$BROKEN_MDT")"
    SANE_MOV="$(cd "$(dirname "$SANE_MOV")" && pwd)/$(basename "$SANE_MOV")"

    log "Inputs validated:"
    log "  Broken:    $BROKEN_MDT"
    log "  Reference: $SANE_MOV"
}

check_disk_space() {
    local broken_size_bytes
    if [[ "$(uname)" == "Darwin" ]]; then
        broken_size_bytes=$(stat -f%z "$BROKEN_MDT")
    else
        broken_size_bytes=$(stat -c%s "$BROKEN_MDT")
    fi

    local broken_size_gb=$((broken_size_bytes / 1024 / 1024 / 1024))
    local required_gb=$((broken_size_gb * 3))

    local output_parent
    output_parent="$(dirname "$BROKEN_MDT")"

    local available_kb
    available_kb=$(df -k "$output_parent" | awk 'NR==2 {print $4}')
    local available_gb=$((available_kb / 1024 / 1024))

    log "Disk space check:"
    log "  Broken file size: ${broken_size_gb} GB"
    log "  Required (3x):    ${required_gb} GB"
    log "  Available:        ${available_gb} GB"

    if [[ $available_gb -lt $required_gb ]]; then
        die 4 "Insufficient disk space. Need at least ${required_gb} GB, have ${available_gb} GB."
    fi
}

setup_output_dir() {
    local basename
    basename="$(basename "$BROKEN_MDT")"
    basename="${basename%.*}"

    local parent_dir
    parent_dir="$(dirname "$BROKEN_MDT")"

    OUTPUT_DIR="$parent_dir/recovery_output_$basename"
    BASENAME="$basename"

    if [[ -d "$OUTPUT_DIR" ]]; then
        log "WARNING: Output directory already exists: $OUTPUT_DIR"
        log "Existing files inside will be overwritten if necessary."
    else
        mkdir -p "$OUTPUT_DIR"
    fi

    LOG_FILE="$OUTPUT_DIR/recovery_log.txt"

    {
        echo "============================================================"
        echo "MDT Rescue Toolkit v$VERSION"
        echo "Profile: $PROFILE"
        echo "Run started at: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Broken file:    $BROKEN_MDT"
        echo "Reference file: $SANE_MOV"
        echo "Output dir:     $OUTPUT_DIR"
        echo "============================================================"
        echo ""
    } > "$LOG_FILE"

    log "Output directory: $OUTPUT_DIR"
    log "Log file:         $LOG_FILE"
}

step_extract_sps_pps() {
    log_both ""
    log_both "STEP 1/7: Extracting SPS/PPS from reference"
    log_both "-----------------------------------------------------------"

    local sane_annexb="$OUTPUT_DIR/${BASENAME}_sane_annexb.h264"
    local sps_pps_prefix="$OUTPUT_DIR/${BASENAME}_sps_pps_prefix.h264"

    log_both "Converting reference MOV to Annex B..."
    ffmpeg -y -i "$SANE_MOV" -map 0:v:0 -c copy -bsf:v h264_mp4toannexb -f h264 \
        "$sane_annexb" >> "$LOG_FILE" 2>&1 \
        || die 5 "Failed to convert reference MOV to Annex B"

    log_both "Extracting SPS and PPS NAL units..."
    python3 "$SCRIPT_DIR/scripts/extract_sps_pps.py" \
        "$sane_annexb" "$sps_pps_prefix" >> "$LOG_FILE" 2>&1 \
        || die 5 "Failed to extract SPS/PPS from reference"

    SPS_PPS_PREFIX="$sps_pps_prefix"
    log_both "SPS/PPS prefix written: $sps_pps_prefix"
}

step_extract_video() {
    log_both ""
    log_both "STEP 2/7: Extracting video NAL units from .MDT (streaming)"
    log_both "-----------------------------------------------------------"
    log_both "This may take 20-40 minutes depending on disk speed."

    local video_h264="$OUTPUT_DIR/${BASENAME}_video.h264"

    python3 "$SCRIPT_DIR/scripts/extract_video_gh5s_fhd25_alli.py" \
        "$BROKEN_MDT" "$video_h264" 2>&1 | tee -a "$LOG_FILE" \
        || die 5 "Video extraction failed"

    VIDEO_H264="$video_h264"
    log_both "Video H.264 written: $video_h264"
}

step_concatenate() {
    log_both ""
    log_both "STEP 3/7: Concatenating SPS/PPS prefix + video"
    log_both "-----------------------------------------------------------"

    local video_with_header="$OUTPUT_DIR/${BASENAME}_video_with_header.h264"

    cat "$SPS_PPS_PREFIX" "$VIDEO_H264" > "$video_with_header" \
        || die 5 "Concatenation failed"

    VIDEO_WITH_HEADER="$video_with_header"
    log_both "Concatenated: $video_with_header"
}

step_wrap_video_mov() {
    log_both ""
    log_both "STEP 4/7: Wrapping video into MOV container"
    log_both "-----------------------------------------------------------"

    local video_only_mov="$OUTPUT_DIR/${BASENAME}_video_only.mov"

    ffmpeg -y \
        -fflags +genpts \
        -framerate 25 \
        -f h264 -i "$VIDEO_WITH_HEADER" \
        -c copy \
        -video_track_timescale 25000 \
        "$video_only_mov" >> "$LOG_FILE" 2>&1 \
        || die 5 "Video MOV wrap failed"

    VIDEO_ONLY_MOV="$video_only_mov"
    log_both "Video-only MOV written: $video_only_mov"
}

step_extract_audio() {
    log_both ""
    log_both "STEP 5/7: Extracting audio chunks from .MDT (streaming)"
    log_both "-----------------------------------------------------------"

    local audio_raw="$OUTPUT_DIR/${BASENAME}_audio.raw"
    local audio_wav="$OUTPUT_DIR/${BASENAME}_audio.wav"

    python3 "$SCRIPT_DIR/scripts/extract_audio_gh5s_fhd25_alli.py" \
        "$BROKEN_MDT" "$audio_raw" 2>&1 | tee -a "$LOG_FILE" \
        || die 5 "Audio extraction failed"

    log_both "Converting raw PCM to WAV..."
    ffmpeg -y -f s16be -ar 48000 -ac 2 -i "$audio_raw" "$audio_wav" \
        >> "$LOG_FILE" 2>&1 \
        || die 5 "Audio WAV conversion failed"

    AUDIO_WAV="$audio_wav"
    log_both "Audio WAV written: $audio_wav"
}

step_mux_final() {
    log_both ""
    log_both "STEP 6/7: Muxing final video + audio"
    log_both "-----------------------------------------------------------"

    local final_mov="$OUTPUT_DIR/${BASENAME}_RESCUED.mov"

    ffmpeg -y \
        -i "$VIDEO_ONLY_MOV" \
        -i "$AUDIO_WAV" \
        -map 0:v:0 \
        -map 1:a:0 \
        -c:v copy \
        -c:a pcm_s16be \
        -video_track_timescale 25000 \
        -shortest \
        "$final_mov" >> "$LOG_FILE" 2>&1 \
        || die 5 "Final mux failed"

    FINAL_MOV="$final_mov"
    log_both "FINAL RESCUED FILE: $final_mov"
}

step_verify() {
    log_both ""
    log_both "STEP 7/7: Verifying output"
    log_both "-----------------------------------------------------------"

    python3 "$SCRIPT_DIR/scripts/verify_output.py" "$FINAL_MOV" 2>&1 | tee -a "$LOG_FILE" \
        || log_both "WARNING: Verification reported issues. Inspect the file manually."
}

main() {
    print_banner

    LOG_FILE=""

    check_dependencies
    validate_inputs "$@"
    check_disk_space
    setup_output_dir

    local start_time
    start_time=$(date +%s)

    step_extract_sps_pps
    step_extract_video
    step_concatenate
    step_wrap_video_mov
    step_extract_audio
    step_mux_final
    step_verify

    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))
    local minutes=$((elapsed / 60))
    local seconds=$((elapsed % 60))

    log_both ""
    log_both "============================================================"
    log_both "RECOVERY COMPLETED"
    log_both "============================================================"
    log_both "Elapsed: ${minutes}m ${seconds}s"
    log_both "Final file: $FINAL_MOV"
    log_both ""
    log_both "Intermediate files have been kept in $OUTPUT_DIR."
    log_both "You may delete them once you've verified the rescued MOV works."
    log_both "============================================================"
}

main "$@"
