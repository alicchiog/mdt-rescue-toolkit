"""
mdt_rescue.engine.audio
=======================

Audio extraction engine for Panasonic GH5S ``.MDT`` files recorded in
FHD 1920x1080 25p ALL-Intra 200 Mbps mode.

This module is the pure-engine port of
``scripts/extract_audio_gh5s_fhd25_alli.py``.  The engine is silent: it
produces a result object and emits structured progress events through an
optional callback, but never writes to stdout.  The legacy CLI behaviour
(banner, per-chunk progress lines, footer, drift summary, anomaly lists)
is preserved by the thin wrapper script that drives this engine.

Audio layout in the GH5S .MDT
-----------------------------
- Between consecutive video AUD anchors there is a "gap" of N bytes.
- Most gaps are 0 (no audio inline at that frame).
- Every ~12 video frames a gap of about 92208 bytes appears:
    ``[ 92160 bytes of PCM s16be stereo 48 kHz ] + [ 48 bytes timecode ]``
  i.e. 480 ms of audio + 12 uint32-BE incrementing counters.
- Anomalous "fat" gaps that fall exactly 12 frames after the previous
  audio chunk are silenced (90 KB of zeros are injected) to keep the
  audio timeline aligned with video.  Off-cycle anomalies are logged
  but not silenced.

The engine streams the input in chunks (default 256 MB) with
overlap-based tail handling, so the ``.MDT`` is never fully loaded
into RAM.

Bit-exact invariant
-------------------
For gaps inside :data:`NORMAL_GAP_RANGE`, the engine writes
``gap_size - TIMECODE_SIZE`` bytes -- NOT a fixed 92160.  This preserves
byte-for-byte equivalence with the legacy script for non-nominal but
in-range gaps.

Part of MDT Rescue Toolkit - https://github.com/alicchiog/mdt-rescue-toolkit
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable


# ---------------------------------------------------------------------
# Constants (public)
# ---------------------------------------------------------------------

#: Access Unit Delimiter anchor (NAL type 9, length-prefixed) marking a
#: video frame boundary.  Duplicated from :mod:`mdt_rescue.engine.video`
#: deliberately: engine modules in B-phase keep zero coupling.
#: Consolidation into :mod:`mdt_rescue.profiles` is planned for B.5.
AUD_ANCHOR: bytes = b"\x00\x00\x00\x02\x09\x10"

#: Valid H.264 NAL unit types observed in the GH5S FHD25 ALL-Intra profile.
VALID_NAL_TYPES: frozenset[int] = frozenset({1, 5, 6, 7, 8, 9, 12})

#: Maximum plausible NAL unit size (5 MB).  Anti-bug guard.
MAX_NAL_SIZE: int = 5 * 1024 * 1024

#: Minimum plausible NAL unit size (1 byte).
MIN_NAL_SIZE: int = 1

#: Size of one PCM audio chunk (s16be stereo 48 kHz, 480 ms).
EXPECTED_AUDIO_PAYLOAD: int = 92160

#: Size of the timecode trailer appended to each audio chunk.
TIMECODE_SIZE: int = 48

#: Total expected size of a normal audio gap (payload + timecode).
EXPECTED_GAP_SIZE: int = EXPECTED_AUDIO_PAYLOAD + TIMECODE_SIZE  # 92208

#: Inclusive bounds for detecting a "normal" audio gap.  Gaps inside
#: this range are treated as containing an audio chunk; the engine
#: writes ``gap_size - TIMECODE_SIZE`` bytes (not a fixed 92160) to
#: preserve bit-exact behaviour of the legacy script.
NORMAL_GAP_RANGE: tuple[int, int] = (90000, 95000)

#: Default streaming chunk size (256 MB).
DEFAULT_CHUNK_SIZE: int = 256 * 1024 * 1024

#: Default tail overlap (10 MB).
DEFAULT_OVERLAP: int = 10 * 1024 * 1024

#: Reference video frame rate used to compute audio/video drift (FHD25).
VIDEO_FPS_FOR_DRIFT: float = 25.0

#: Bytes per second of audio (s16be stereo 48 kHz = 48000 * 2 ch * 2 B).
AUDIO_BYTES_PER_SECOND: int = 192000


# Private silence block: 90 KB of zeros, injected when an on-cycle
# anomaly replaces an expected audio chunk.
_SILENCE_BLOCK: bytes = b"\x00" * EXPECTED_AUDIO_PAYLOAD


# ---------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------

def is_valid_nal_header(b: int) -> bool:
    """Return ``True`` if byte ``b`` is a valid H.264 NAL unit header.

    A valid header has its forbidden bit cleared and a NAL type that
    appears in :data:`VALID_NAL_TYPES`.
    """
    forbidden = (b >> 7) & 1
    nal_type = b & 0x1F
    return forbidden == 0 and nal_type in VALID_NAL_TYPES


def _read_u32be(buf: bytes, pos: int) -> int:
    """Read a 4-byte big-endian unsigned integer from ``buf`` at ``pos``."""
    return struct.unpack(">I", buf[pos:pos + 4])[0]


def find_frame_end(buf: bytes, frame_start: int) -> int | None:
    """Locate the end of a video access unit starting at ``frame_start``.

    Walks NAL units (each preceded by a 4-byte big-endian length) starting
    from the byte after the AUD anchor.  Returns the offset just after
    the last NAL of the frame -- which is the start of the audio gap
    region for that frame.

    Returns ``None`` if the buffer is too short to validate the frame or
    if the frame does not begin with NAL type 9 (AUD).
    """
    p = frame_start
    while p + 5 < len(buf):
        if p != frame_start and buf.startswith(AUD_ANCHOR, p):
            return p
        if p + 4 > len(buf):
            return None
        ln = _read_u32be(buf, p)
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


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class HandledAnomaly:
    """An on-cycle gap anomaly where silence was injected.

    These anomalies appear exactly 12 frames after the previous audio
    chunk but their gap size is outside the normal range.  The engine
    writes 90 KB of silence to keep the audio timeline aligned with
    video.
    """
    frame_idx: int
    gap_size: int
    file_offset: int


@dataclass(frozen=True)
class UnhandledAnomaly:
    """An off-cycle gap anomaly: logged but NOT silenced.

    These anomalies have unexpected size and do not occur exactly 12
    frames after the previous audio chunk.  Silence is not injected:
    the engine records the event for diagnostic review only.
    """
    frame_idx: int
    gap_size: int
    file_offset: int
    frames_since_last_audio: int


@dataclass(frozen=True)
class AudioProgressEvent:
    """Per-chunk progress event emitted by :func:`extract_audio`.

    All fields are raw numeric values.  Display formatting (percentages,
    units conversion, thousands separators) is the responsibility of
    the caller (typically the CLI wrapper).
    """
    chunk_num: int
    bytes_read: int
    total_bytes: int
    frames: int
    normal_chunks: int
    silence_inserted: int
    unhandled_count: int
    audio_bytes: int


@dataclass(frozen=True)
class AudioExtractionResult:
    """Immutable result of an audio extraction pass.

    Anomaly tuples are snapshots: mutating the underlying
    :class:`AudioExtractionState` after :func:`extract_audio` returns
    cannot affect the result.

    Field names follow the legacy state-dict keys (``frames``,
    ``audio_bytes``, ``gap_zero``) so the CLI wrapper can reconstruct
    legacy stdout without renames.
    """
    frames: int
    normal_chunks: int
    silence_inserted: int
    audio_bytes: int
    gap_zero: int
    unhandled_count: int
    handled_anomalies: tuple[HandledAnomaly, ...]
    unhandled_anomalies: tuple[UnhandledAnomaly, ...]
    video_duration_sec: float
    audio_duration_sec: float
    drift_sec: float
    input_size: int
    chunks: int
    final_tail_size: int

    @property
    def total_effective_chunks(self) -> int:
        """Sum of normal audio chunks and silence-injected chunks."""
        return self.normal_chunks + self.silence_inserted

    @property
    def audio_bytes_mod_4(self) -> int:
        """``audio_bytes % 4``; must be 0 for s16 stereo alignment."""
        return self.audio_bytes % 4


@dataclass
class AudioExtractionState:
    """Mutable state tracked during streaming audio extraction.

    This is a low-level type exposed for callers driving the buffer
    processing loop manually.  Most users should call
    :func:`extract_audio` instead.
    """
    frames: int = 0
    normal_chunks: int = 0
    silence_inserted: int = 0
    audio_bytes: int = 0
    gap_zero: int = 0
    last_audio_at_frame: int = -1
    handled_anomalies: list[HandledAnomaly] = field(default_factory=list)
    unhandled_anomalies: list[UnhandledAnomaly] = field(default_factory=list)


# ---------------------------------------------------------------------
# Buffer-level processing
# ---------------------------------------------------------------------

def process_audio_buffer(
    buf: bytes,
    buf_file_offset: int,
    out_file: BinaryIO,
    state: AudioExtractionState,
    *,
    overlap: int = DEFAULT_OVERLAP,
) -> int:
    """Process one streaming buffer, writing detected audio chunks.

    Walks AUD anchors in ``buf``, computes the gap between consecutive
    anchors, and classifies each gap:

    * ``gap_size == 0``: no inline audio at this frame; counted.
    * ``gap_size`` inside :data:`NORMAL_GAP_RANGE`: write
      ``gap_size - TIMECODE_SIZE`` bytes (NOT a fixed 92160).
    * other ``gap_size``: anomaly.  If it occurs exactly 12 frames
      after the previous audio chunk, inject 90 KB of silence and
      record as :class:`HandledAnomaly`; otherwise record as
      :class:`UnhandledAnomaly` and write nothing.

    Returns ``last_processed_end``: the offset up to which the buffer
    has been consumed.  The caller uses this to manage the tail buffer
    between chunks.
    """
    first_aud = buf.find(AUD_ANCHOR)
    if first_aud == -1:
        return max(0, len(buf) - overlap)

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
            state.gap_zero += 1

        elif NORMAL_GAP_RANGE[0] <= gap_size <= NORMAL_GAP_RANGE[1]:
            # Normal audio chunk: keep (gap_size - 48) bytes, drop the
            # trailing 48-byte timecode.  NOT a fixed 92160.
            audio_start = frame_end
            audio_end = next_pos - TIMECODE_SIZE
            audio_data = buf[audio_start:audio_end]
            out_file.write(audio_data)
            state.audio_bytes += len(audio_data)
            state.normal_chunks += 1
            state.last_audio_at_frame = state.frames

        else:
            # Anomalous gap.
            frames_since_last_audio = state.frames - state.last_audio_at_frame
            file_offset = buf_file_offset + frame_end

            if frames_since_last_audio == 12:
                # Anomaly hits exactly where an audio chunk was expected.
                # Inject silence to keep audio timeline aligned with video.
                out_file.write(_SILENCE_BLOCK)
                state.audio_bytes += EXPECTED_AUDIO_PAYLOAD
                state.silence_inserted += 1
                state.last_audio_at_frame = state.frames
                state.handled_anomalies.append(HandledAnomaly(
                    frame_idx=state.frames,
                    gap_size=gap_size,
                    file_offset=file_offset,
                ))
            else:
                # Anomaly off-cycle: log only, do not silence-pad blindly.
                state.unhandled_anomalies.append(UnhandledAnomaly(
                    frame_idx=state.frames,
                    gap_size=gap_size,
                    file_offset=file_offset,
                    frames_since_last_audio=frames_since_last_audio,
                ))

        state.frames += 1
        last_processed_end = next_pos

        if next_pos <= frame_start:
            return last_processed_end
        pos = next_pos

    return last_processed_end


# ---------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------

def _state_to_result(
    state: AudioExtractionState,
    *,
    input_size: int,
    chunks: int,
    final_tail_size: int,
) -> AudioExtractionResult:
    """Snapshot ``state`` into an immutable :class:`AudioExtractionResult`.

    Computes video/audio durations and drift from the final counters.
    Anomaly lists are copied into tuples so subsequent mutation of
    ``state`` cannot affect the returned result.
    """
    video_duration_sec = state.frames / VIDEO_FPS_FOR_DRIFT
    audio_duration_sec = state.audio_bytes / AUDIO_BYTES_PER_SECOND
    drift_sec = audio_duration_sec - video_duration_sec
    return AudioExtractionResult(
        frames=state.frames,
        normal_chunks=state.normal_chunks,
        silence_inserted=state.silence_inserted,
        audio_bytes=state.audio_bytes,
        gap_zero=state.gap_zero,
        unhandled_count=len(state.unhandled_anomalies),
        handled_anomalies=tuple(state.handled_anomalies),
        unhandled_anomalies=tuple(state.unhandled_anomalies),
        video_duration_sec=video_duration_sec,
        audio_duration_sec=audio_duration_sec,
        drift_sec=drift_sec,
        input_size=input_size,
        chunks=chunks,
        final_tail_size=final_tail_size,
    )


def extract_audio(
    input_path: Path | str,
    output_path: Path | str,
    *,
    progress: Callable[[AudioProgressEvent], None] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> AudioExtractionResult:
    """Extract PCM s16be stereo 48 kHz audio from a GH5S ``.MDT`` file.

    Streams ``input_path`` in ``chunk_size`` bytes with overlap-based
    tail handling, detects audio chunks between video AUD anchors, and
    writes the raw PCM bytes to ``output_path``.  Returns an immutable
    :class:`AudioExtractionResult` summarising counters and drift.

    Parameters
    ----------
    input_path:
        Path to the broken ``.MDT`` file.
    output_path:
        Path where the ``.raw`` PCM output is written (overwritten if
        already present).
    progress:
        Optional callback invoked once per chunk with an
        :class:`AudioProgressEvent`.  The engine is otherwise silent.
    chunk_size:
        Streaming chunk size in bytes.  Defaults to 256 MB.
    overlap:
        Tail overlap in bytes.  Defaults to 10 MB.

    Notes
    -----
    The engine writes ``gap_size - 48`` bytes for each normal audio
    gap (NOT a fixed 92160).  This preserves bit-exact behaviour of
    the legacy script for non-nominal gap sizes inside
    :data:`NORMAL_GAP_RANGE`.
    """
    input_path = os.fspath(input_path)
    output_path = os.fspath(output_path)
    total_size = os.path.getsize(input_path)

    state = AudioExtractionState()
    chunk_num = 0
    final_tail_size = 0

    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        file_pos = 0
        tail_buf = b""
        tail_file_offset = 0

        while file_pos < total_size:
            to_read = min(chunk_size, total_size - file_pos)
            fin.seek(file_pos)
            chunk = fin.read(to_read)

            buf_file_offset = tail_file_offset if tail_buf else file_pos
            buf = tail_buf + chunk
            chunk_num += 1

            last_end = process_audio_buffer(
                buf, buf_file_offset, fout, state, overlap=overlap,
            )

            if last_end >= len(buf):
                tail_buf = b""
            else:
                tail_buf = buf[last_end:]
                tail_file_offset = buf_file_offset + last_end

            if len(tail_buf) > 2 * overlap:
                tail_buf = tail_buf[-overlap:]
                tail_file_offset = buf_file_offset + (len(buf) - len(tail_buf))

            file_pos += to_read
            final_tail_size = len(tail_buf)

            if progress is not None:
                progress(AudioProgressEvent(
                    chunk_num=chunk_num,
                    bytes_read=file_pos,
                    total_bytes=total_size,
                    frames=state.frames,
                    normal_chunks=state.normal_chunks,
                    silence_inserted=state.silence_inserted,
                    unhandled_count=len(state.unhandled_anomalies),
                    audio_bytes=state.audio_bytes,
                ))

    return _state_to_result(
        state,
        input_size=total_size,
        chunks=chunk_num,
        final_tail_size=final_tail_size,
    )


def extract_audio_to_raw(
    input_path: Path | str,
    output_path: Path | str,
    *,
    progress: Callable[[AudioProgressEvent], None] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> AudioExtractionResult:
    """Alias of :func:`extract_audio` with a side-effect-explicit name.

    Behaviour is identical to :func:`extract_audio`.  Use this entry
    point when the call site benefits from naming the output format
    explicitly (raw PCM s16be).
    """
    return extract_audio(
        input_path,
        output_path,
        progress=progress,
        chunk_size=chunk_size,
        overlap=overlap,
    )
