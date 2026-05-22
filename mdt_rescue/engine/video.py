"""mdt_rescue.engine.video
========================

Video NAL extraction engine for Panasonic .MDT files.

Extracts H.264 NAL units from a GH5S .MDT file recorded in FHD
1920x1080 25p ALL-Intra 200M mode and writes them in Annex-B format
ready to be wrapped into a MOV by ffmpeg.

Pipeline strategy
-----------------
1. Find the GH5S AUD anchor pattern (00 00 00 02 09 10): a 4-byte
   length prefix (value 2) followed by an Access Unit Delimiter NAL
   header with primary_pic_type = 0. This marks the start of every
   video frame in the AVC-Intra ALL-I stream.
2. From each AUD anchor, read AVCC length-prefixed NALs until the next
   AUD anchor (or until a non-NAL byte aborts the frame).
3. Convert AVCC to Annex-B by writing a 4-byte start code
   (00 00 00 01) before each NAL payload.
4. Stream the input in chunks (default 256 MB) with overlap-based tail
   handling, so the file never has to be loaded entirely into RAM.

Tail handling guarantee
-----------------------
process_video_buffer always returns the offset of the FIRST BYTE NOT
YET PROCESSED. The main loop in extract_video then sets:

    tail_buf = buf[last_end:]

This guarantees no frame is ever written twice across chunk
boundaries.

Profile validated for
---------------------
- Panasonic DC-GH5S
- 1920x1080, 25 fps PAL
- H.264 High 4:2:2 Intra, ALL-I 200M
- yuv422p10le

This module is the engine layer. Stdout / human-readable logging is
the caller's responsibility; the engine exposes a `progress` callback
so wrappers (and the future orchestrator) can produce their own
output.

Part of MDT Rescue Toolkit -
https://github.com/alicchiog/mdt-rescue-toolkit
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Callable, Mapping

__all__ = [
    "START_CODE",
    "AUD_ANCHOR",
    "VALID_NAL_TYPES",
    "MAX_NAL_SIZE",
    "MIN_NAL_SIZE",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "nal_type_name",
    "is_valid_nal_header",
    "VideoExtractionState",
    "VideoProgressEvent",
    "VideoExtractionResult",
    "process_video_buffer",
    "extract_video",
    "extract_video_to_annexb",
]


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

START_CODE: bytes = b"\x00\x00\x00\x01"
"""H.264 Annex-B start code, 4-byte form."""

AUD_ANCHOR: bytes = b"\x00\x00\x00\x02\x09\x10"
"""GH5S AUD anchor pattern, 6 bytes total:

- 4-byte length prefix = 2 (AVCC framing)
- NAL header byte 0x09 (Access Unit Delimiter,
  forbidden_zero_bit = 0, nal_ref_idc = 0, nal_unit_type = 9)
- payload byte 0x10 (primary_pic_type = 0, with 5 trailing rbsp bits
  set to 10000)

Marks the start of every frame in the GH5S FHD25 ALL-I stream.
"""

VALID_NAL_TYPES: Mapping[int, str] = MappingProxyType({
    1: "slice",
    5: "IDR",
    6: "SEI",
    7: "SPS",
    8: "PPS",
    9: "AUD",
    12: "filler",
})
"""NAL unit types accepted inside a frame. Anything outside this set
aborts the current frame parsing."""

MAX_NAL_SIZE: int = 5 * 1024 * 1024
"""Upper bound on a single NAL unit length (5 MB).

Protects against parsing garbage as huge NALs."""

MIN_NAL_SIZE: int = 1
"""Lower bound on a single NAL unit length (1 byte = header only)."""

DEFAULT_CHUNK_SIZE: int = 256 * 1024 * 1024
"""Default streaming chunk size (256 MB)."""

DEFAULT_OVERLAP: int = 10 * 1024 * 1024
"""Default fallback overlap (10 MB) used when no AUD anchor is found
in the current buffer; kept at the tail for the next iteration."""


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def nal_type_name(t: int) -> str:
    """Return the human-readable name of a NAL type, '?' if unknown."""
    return VALID_NAL_TYPES.get(t, "?")


def is_valid_nal_header(b: int) -> bool:
    """Return True iff the byte is a valid NAL header for our profile.

    forbidden_zero_bit must be 0, and nal_unit_type must belong to
    VALID_NAL_TYPES.
    """
    forbidden = (b >> 7) & 1
    nal_type = b & 0x1F
    return forbidden == 0 and nal_type in VALID_NAL_TYPES


def _read_u32be(buf: bytes, pos: int) -> int:
    """Read 4 big-endian bytes as unsigned 32-bit integer."""
    return struct.unpack(">I", buf[pos:pos + 4])[0]


# ---------------------------------------------------------------------
# State, events, result
# ---------------------------------------------------------------------

@dataclass
class VideoExtractionState:
    """Mutable low-level state used by :func:`process_video_buffer`.

    This mirrors the legacy state dict 1:1 and is primarily intended
    for tests and advanced engine integrations.  End users should rely
    on :func:`extract_video` and the immutable
    :class:`VideoExtractionResult` it returns.
    """

    counts: dict[int, int] = field(default_factory=dict)
    frames: int = 0
    nals: int = 0
    written: int = 0
    bad_blocks: int = 0
    first_sps: bytes | None = None
    first_pps: bytes | None = None


@dataclass(frozen=True)
class VideoProgressEvent:
    """Progress event emitted once per chunk by :func:`extract_video`.

    If ``warning`` is non-None, the consumer MUST emit the warning
    line BEFORE the chunk progress line, to match legacy stdout
    ordering.  The engine itself prints nothing.

    Snapshot fields (``frames``, ``nals``, ``written``,
    ``bad_blocks``) are cumulative across the whole run, not
    per-chunk.
    """

    chunk_num: int
    file_pos: int
    total_size: int
    frames: int
    nals: int
    written: int
    bad_blocks: int
    has_first_sps: bool
    has_first_pps: bool
    tail_size: int
    warning: str | None = None


@dataclass(frozen=True)
class VideoExtractionResult:
    """Immutable result of a video extraction pass.

    ``nal_type_counts`` is a read-only mapping
    (:class:`types.MappingProxyType` wrapping a copy), so mutating the
    underlying :class:`VideoExtractionState` after
    :func:`extract_video` returns cannot affect the result.
    """

    frames: int
    nals: int
    bytes_written: int
    bad_blocks: int
    first_sps: bytes | None
    first_pps: bytes | None
    nal_type_counts: Mapping[int, int]
    input_size: int
    chunks: int
    final_tail_size: int


# ---------------------------------------------------------------------
# Buffer-level processing
# ---------------------------------------------------------------------

def process_video_buffer(
    buf: bytes,
    out_file: BinaryIO,
    state: VideoExtractionState,
    *,
    overlap: int = DEFAULT_OVERLAP,
    max_nal_size: int = MAX_NAL_SIZE,
    min_nal_size: int = MIN_NAL_SIZE,
) -> int:
    """Process one buffer of bytes and write Annex-B NALs to ``out_file``.

    The buffer is typically the concatenation of the previous tail and
    a freshly-read chunk.

    Returns the offset of the FIRST BYTE NOT YET PROCESSED.  Four
    return paths exist, all preserved 1:1 from the legacy
    implementation:

    1. No AUD anchor found in the buffer:
       return ``max(0, len(buf) - overlap)`` so the tail keeps the
       last ``overlap`` bytes in case the anchor straddles the
       boundary.
    2. A frame would overrun the buffer (``had_overrun``):
       return ``frame_start`` so it is reprocessed from the beginning
       in the next chunk.
    3. Inner loop terminated without locating a next AUD anchor:
       return ``last_processed_end`` (just past the last written
       byte).
    4. Outer loop exited via its guard:
       return ``min(pos, len(buf))``.

    The function mutates ``state`` and writes to ``out_file`` as a
    side effect.  No I/O on ``out_file`` happens for frames that span
    a chunk boundary.
    """
    first_aud = buf.find(AUD_ANCHOR)
    if first_aud == -1:
        # Return path 1: no AUD at all in this buffer.
        return max(0, len(buf) - overlap)

    pos = first_aud
    last_processed_end = pos

    while pos >= 0 and pos < len(buf) - 6:
        frame_start = pos
        p = pos
        frame_nals: list[tuple[int, bytes]] = []
        frame_has_vcl = False
        had_overrun = False

        # Walk NALs within this access unit, stopping at next AUD.
        while p + 5 < len(buf):
            if p != frame_start and buf.startswith(AUD_ANCHOR, p):
                break

            if p + 4 > len(buf):
                had_overrun = True
                break

            ln = _read_u32be(buf, p)

            if ln < min_nal_size or ln > max_nal_size:
                break

            if p + 4 + ln > len(buf):
                had_overrun = True
                break

            hdr = buf[p + 4]
            if not is_valid_nal_header(hdr):
                break

            nt = hdr & 0x1F

            # Each frame must begin with an AUD (type 9).
            if len(frame_nals) == 0 and nt != 9:
                break

            payload = buf[p + 4:p + 4 + ln]

            # First SPS/PPS encountered in the .MDT
            # (rare for GH5S 200M, present on other firmwares).
            if nt == 7 and state.first_sps is None:
                state.first_sps = bytes(payload)
            if nt == 8 and state.first_pps is None:
                state.first_pps = bytes(payload)

            frame_nals.append((nt, payload))

            if nt in (1, 5):
                frame_has_vcl = True

            p += 4 + ln

        if had_overrun:
            # Return path 2: leave this frame for the next chunk.
            return frame_start

        # Frame fully inside this buffer.
        if frame_nals and frame_has_vcl:
            for nt, payload in frame_nals:
                out_file.write(START_CODE)
                out_file.write(payload)
                state.nals += 1
                state.written += 4 + len(payload)
                state.counts[nt] = state.counts.get(nt, 0) + 1
            state.frames += 1
        else:
            state.bad_blocks += 1

        # Advance to the next AUD strictly after the current frame.
        search_start = max(p, frame_start + 6)
        next_pos = buf.find(AUD_ANCHOR, search_start)
        last_processed_end = p

        if next_pos == -1:
            # Return path 3: no further AUDs after the last frame.
            return last_processed_end
        if next_pos <= frame_start:
            # Defensive guard from legacy: should not happen given
            # search_start, but kept for behavioural parity.
            return last_processed_end
        pos = next_pos

    # Return path 4: outer loop guard exited.
    return min(pos if pos >= 0 else len(buf), len(buf))


# ---------------------------------------------------------------------
# File-level entry points
# ---------------------------------------------------------------------

def extract_video(
    input_path: Path | str,
    output_path: Path | str,
    *,
    progress: Callable[[VideoProgressEvent], None] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> VideoExtractionResult:
    """Extract H.264 NAL units from a GH5S ``.MDT`` into Annex-B form.

    Streams the input in ``chunk_size`` bytes with overlap-based tail
    handling.  Emits one :class:`VideoProgressEvent` per chunk via
    ``progress`` (if provided).

    The engine prints nothing to stdout.  Human-readable logging is
    the caller's responsibility.

    Parameters
    ----------
    input_path : Path | str
        Source ``.MDT`` file.
    output_path : Path | str
        Destination Annex-B file to write.
    progress : callable, optional
        Called once per chunk with a :class:`VideoProgressEvent`.  If
        the event's ``warning`` field is non-None, consumers should
        emit it BEFORE the chunk progress line to match legacy
        ordering.
    chunk_size : int, default 256 MB
        Number of bytes to read per iteration.
    overlap : int, default 10 MB
        Tail kept across chunks when no AUD anchor is found in the
        current buffer.

    Returns
    -------
    VideoExtractionResult
        Immutable snapshot of the extraction outcome.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    total_size = input_path.stat().st_size
    state = VideoExtractionState()

    chunk_num = 0
    tail_buf = b""

    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        file_pos = 0

        while file_pos < total_size:
            to_read = min(chunk_size, total_size - file_pos)
            # Explicit seek mirrors legacy behaviour (defensive even
            # though the file pointer is already at file_pos).
            fin.seek(file_pos)
            chunk = fin.read(to_read)

            buf = tail_buf + chunk
            chunk_num += 1

            last_end = process_video_buffer(
                buf, fout, state, overlap=overlap
            )

            if last_end >= len(buf):
                tail_buf = b""
            else:
                tail_buf = buf[last_end:]

            warning: str | None = None
            if len(tail_buf) > 2 * overlap:
                # Safety net from the legacy: should never trigger on
                # well-formed input but we preserve the behaviour.
                warning = (
                    f"WARNING: tail size "
                    f"{len(tail_buf) / (1024 ** 2):.0f}MB "
                    f"> 2*overlap, truncating"
                )
                tail_buf = tail_buf[-overlap:]

            file_pos += to_read

            if progress is not None:
                progress(VideoProgressEvent(
                    chunk_num=chunk_num,
                    file_pos=file_pos,
                    total_size=total_size,
                    frames=state.frames,
                    nals=state.nals,
                    written=state.written,
                    bad_blocks=state.bad_blocks,
                    has_first_sps=state.first_sps is not None,
                    has_first_pps=state.first_pps is not None,
                    tail_size=len(tail_buf),
                    warning=warning,
                ))

        final_tail_size = len(tail_buf)

    return VideoExtractionResult(
        frames=state.frames,
        nals=state.nals,
        bytes_written=state.written,
        bad_blocks=state.bad_blocks,
        first_sps=state.first_sps,
        first_pps=state.first_pps,
        nal_type_counts=MappingProxyType(dict(state.counts)),
        input_size=total_size,
        chunks=chunk_num,
        final_tail_size=final_tail_size,
    )


def extract_video_to_annexb(
    input_path: Path | str,
    output_path: Path | str,
    *,
    progress: Callable[[VideoProgressEvent], None] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> VideoExtractionResult:
    """Alias of :func:`extract_video` with a side-effect-explicit name.

    Behaviour is identical.  Use this entry point when the call site
    should make explicit that an Annex-B file is being written to
    disk (parallel to ``extract_and_write_prefix`` in
    :mod:`mdt_rescue.engine.sps_pps`).
    """
    return extract_video(
        input_path,
        output_path,
        progress=progress,
        chunk_size=chunk_size,
        overlap=overlap,
    )
