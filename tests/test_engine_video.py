"""Unit tests for mdt_rescue.engine.video.

All tests operate on synthetic byte buffers built inline; no binary
fixtures are committed to the repository.  File-level tests use
pytest's ``tmp_path`` fixture for ephemeral input/output paths.

Test layout
-----------
- TestHelpers                      (3 tests)
- TestProcessBufferReturnPaths     (5 tests)
- TestProcessBufferHappyPath       (6 tests)
- TestProcessBufferDropPaths       (5 tests)
- TestStateTracking                (4 tests)
- TestExtractVideoFile             (6 tests)
- TestVideoExtractionResult        (4 tests)

Total: 33 atomic tests.
"""

from __future__ import annotations

import io
import struct
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from mdt_rescue.engine.video import (
    AUD_ANCHOR,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    MAX_NAL_SIZE,
    MIN_NAL_SIZE,
    START_CODE,
    VALID_NAL_TYPES,
    VideoExtractionResult,
    VideoExtractionState,
    VideoProgressEvent,
    extract_video,
    extract_video_to_annexb,
    is_valid_nal_header,
    nal_type_name,
    process_video_buffer,
)


# ---------------------------------------------------------------------
# Inline buffer builders (synthetic AVCC NALs)
# ---------------------------------------------------------------------

def _make_avcc_nal(nal_type: int, body: bytes = b"") -> bytes:
    """Build an AVCC length-prefixed NAL.

    Layout: 4-byte BE length + 1-byte header + body.
    The header has forbidden_zero_bit = 0, nal_ref_idc = 0, and
    nal_unit_type = ``nal_type``.
    """
    assert 0 <= nal_type <= 0x1F, "nal_type must fit in 5 bits"
    header = bytes([nal_type & 0x1F])
    nal = header + body
    return struct.pack(">I", len(nal)) + nal


def _make_aud_avcc() -> bytes:
    """Build the canonical GH5S AUD anchor as an AVCC NAL.

    Returns exactly ``AUD_ANCHOR`` (6 bytes).
    """
    return _make_avcc_nal(9, b"\x10")


# Sanity: our helper must reproduce AUD_ANCHOR byte-for-byte.
assert _make_aud_avcc() == AUD_ANCHOR


# ---------------------------------------------------------------------
# TestHelpers
# ---------------------------------------------------------------------

class TestHelpers:
    def test_nal_type_name_known(self):
        assert nal_type_name(1) == "slice"
        assert nal_type_name(5) == "IDR"
        assert nal_type_name(6) == "SEI"
        assert nal_type_name(7) == "SPS"
        assert nal_type_name(8) == "PPS"
        assert nal_type_name(9) == "AUD"
        assert nal_type_name(12) == "filler"

    def test_nal_type_name_unknown(self):
        assert nal_type_name(0) == "?"
        assert nal_type_name(2) == "?"
        assert nal_type_name(10) == "?"
        assert nal_type_name(31) == "?"

    def test_is_valid_nal_header_cases(self):
        # Valid: forbidden=0, type in VALID_NAL_TYPES
        assert is_valid_nal_header(0x01) is True   # slice
        assert is_valid_nal_header(0x05) is True   # IDR
        assert is_valid_nal_header(0x07) is True   # SPS
        assert is_valid_nal_header(0x08) is True   # PPS
        assert is_valid_nal_header(0x09) is True   # AUD
        # Invalid: forbidden_zero_bit set
        assert is_valid_nal_header(0xff) is False  # forbidden=1, type=31
        assert is_valid_nal_header(0x80) is False  # forbidden=1, type=0
        assert is_valid_nal_header(0x89) is False  # forbidden=1, type=9
        # Invalid: type not in set
        assert is_valid_nal_header(0x00) is False  # type=0
        assert is_valid_nal_header(0x02) is False  # type=2
        assert is_valid_nal_header(0x0a) is False  # type=10
        assert is_valid_nal_header(0x1f) is False  # type=31


# ---------------------------------------------------------------------
# TestProcessBufferReturnPaths
# ---------------------------------------------------------------------

class TestProcessBufferReturnPaths:
    def test_path1_no_aud_returns_overlap_offset(self):
        """Return path 1: buf without AUD anchor -> len(buf)-overlap."""
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = b"\xff" * 100
        result = process_video_buffer(buf, out, state, overlap=10)
        assert result == 90
        assert out.getvalue() == b""

    def test_path1_no_aud_short_buf_returns_zero(self):
        """Return path 1 edge case: buf shorter than overlap -> 0."""
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = b"\xff" * 5
        result = process_video_buffer(buf, out, state, overlap=10)
        assert result == 0
        assert out.getvalue() == b""

    def test_path2_had_overrun_returns_frame_start(self):
        """Return path 2: a NAL declares more bytes than the buffer
        holds -> return frame_start so it is reprocessed."""
        state = VideoExtractionState()
        out = io.BytesIO()
        # AUD at offset 0; then a NAL claiming length=1000 but only a
        # few bytes of payload available -> had_overrun.
        buf = AUD_ANCHOR + struct.pack(">I", 1000) + b"\x01" + b"\x00" * 5
        # frame_start = 0
        result = process_video_buffer(buf, out, state, overlap=5)
        assert result == 0
        assert out.getvalue() == b""
        assert state.frames == 0
        assert state.bad_blocks == 0

    def test_path3_loop_end_returns_last_processed_end(self):
        """Return path 3: a complete frame parses, but no further AUD
        is found in the buffer -> return last_processed_end."""
        state = VideoExtractionState()
        out = io.BytesIO()
        slice_nal = _make_avcc_nal(1, b"\x00" * 8)  # 13 bytes
        buf = AUD_ANCHOR + slice_nal + b"\xff" * 20  # 39 bytes
        result = process_video_buffer(buf, out, state, overlap=5)
        # AUD (6) + slice (13) = 19 bytes consumed.
        assert result == 19
        assert state.frames == 1
        assert state.nals == 2

    def test_path4_outer_loop_end_returns_min_pos_len(self):
        """Return path 4: pos lands at len(buf)-6 exactly -> outer
        loop guard exits -> return min(pos, len(buf))."""
        state = VideoExtractionState()
        out = io.BytesIO()
        slice_nal = _make_avcc_nal(1, b"\x00" * 8)  # 13 bytes
        buf = AUD_ANCHOR + slice_nal + AUD_ANCHOR  # 6 + 13 + 6 = 25
        result = process_video_buffer(buf, out, state, overlap=5)
        # First frame written, pos advances to 19 = next AUD offset.
        # Outer guard: 19 < 25 - 6 = 19 is False -> exit.
        # min(19, 25) = 19.
        assert result == 19
        assert state.frames == 1


# ---------------------------------------------------------------------
# TestProcessBufferHappyPath
# ---------------------------------------------------------------------

class TestProcessBufferHappyPath:
    def test_single_frame_aud_plus_slice_written(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        slice_nal = _make_avcc_nal(1, b"\xab\xcd")
        buf = AUD_ANCHOR + slice_nal
        process_video_buffer(buf, out, state, overlap=5)
        expected = (
            START_CODE + b"\x09\x10" +
            START_CODE + b"\x01\xab\xcd"
        )
        assert out.getvalue() == expected
        assert state.frames == 1
        assert state.nals == 2

    def test_two_consecutive_frames_both_written(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        s1 = _make_avcc_nal(1, b"\xab")
        s2 = _make_avcc_nal(5, b"\xcd")
        # Trailing garbage forces path 3 exit, doesn't affect writes.
        buf = AUD_ANCHOR + s1 + AUD_ANCHOR + s2 + b"\xff" * 20
        process_video_buffer(buf, out, state, overlap=5)
        expected = (
            START_CODE + b"\x09\x10" +
            START_CODE + b"\x01\xab" +
            START_CODE + b"\x09\x10" +
            START_CODE + b"\x05\xcd"
        )
        assert out.getvalue() == expected
        assert state.frames == 2
        assert state.nals == 4

    def test_frame_with_sps_pps_slice(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        sps_body = b"\xde\xad"
        pps_body = b"\xbe\xef"
        slice_body = b"\x00\x01\x02"
        buf = (
            AUD_ANCHOR
            + _make_avcc_nal(7, sps_body)
            + _make_avcc_nal(8, pps_body)
            + _make_avcc_nal(1, slice_body)
        )
        process_video_buffer(buf, out, state, overlap=5)
        # first_sps and first_pps include the header byte.
        assert state.first_sps == b"\x07" + sps_body
        assert state.first_pps == b"\x08" + pps_body
        assert state.frames == 1
        assert state.nals == 4

    def test_avc_to_annexb_conversion_is_byte_exact(self):
        """AVCC length prefix is REPLACED by Annex-B start code,
        producing exactly: start_code || header || body for each NAL.
        """
        state = VideoExtractionState()
        out = io.BytesIO()
        slice_body = b"\xaa\xbb\xcc\xdd"
        buf = AUD_ANCHOR + _make_avcc_nal(1, slice_body)
        process_video_buffer(buf, out, state, overlap=5)
        expected = (
            START_CODE + b"\x09\x10" +
            START_CODE + b"\x01" + slice_body
        )
        assert out.getvalue() == expected
        # The AVCC length prefix of the slice (b"\x00\x00\x00\x05")
        # must NOT appear at the position of the start code.
        written = out.getvalue()
        assert written.startswith(START_CODE)
        # No raw AVCC length=5 followed by NAL header in output
        # (the only \x00\x00\x00\x05 would belong to a length prefix,
        # which we have removed).
        assert b"\x00\x00\x00\x05\x01" not in written

    def test_counts_incremented_per_nal_type(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = (
            AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
            + AUD_ANCHOR + _make_avcc_nal(5, b"\x00")
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.counts[9] == 3
        assert state.counts[1] == 2
        assert state.counts[5] == 1
        assert state.frames == 3
        assert state.nals == 6

    def test_aud_only_frame_is_bad_block_then_next_frame_written(self):
        """An AUD followed immediately by another AUD is a frame with
        no VCL -> bad_blocks++.  The next AUD+slice is a normal write.
        """
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = (
            AUD_ANCHOR
            + AUD_ANCHOR
            + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.bad_blocks == 1
        assert state.frames == 1
        assert state.nals == 2


# ---------------------------------------------------------------------
# TestProcessBufferDropPaths
# ---------------------------------------------------------------------

class TestProcessBufferDropPaths:
    def test_invalid_nal_forbidden_bit_breaks_frame(self):
        """forbidden_zero_bit=1 aborts the frame; frame has only AUD
        -> bad_block.  Next frame still parses normally."""
        state = VideoExtractionState()
        out = io.BytesIO()
        # length=2, header=0xff (forbidden=1) -> invalid
        invalid_nal = b"\x00\x00\x00\x02\xff\x00"
        buf = (
            AUD_ANCHOR + invalid_nal
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.bad_blocks == 1
        assert state.frames == 1

    def test_unknown_nal_type_breaks_frame(self):
        """NAL type not in VALID_NAL_TYPES aborts the frame."""
        state = VideoExtractionState()
        out = io.BytesIO()
        # length=2, header=0x0f (type=15, forbidden=0) -> not in set
        unknown_nal = b"\x00\x00\x00\x02\x0f\x00"
        buf = (
            AUD_ANCHOR + unknown_nal
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.bad_blocks == 1
        assert state.frames == 1

    def test_oversize_length_breaks_frame(self):
        """NAL length > max_nal_size aborts the frame."""
        state = VideoExtractionState()
        out = io.BytesIO()
        # Use a small max_nal_size for the test (10 bytes) to keep
        # the buffer tiny while still exercising the > max guard.
        oversize_prefix = struct.pack(">I", 1000)
        # Build a buffer large enough that the oversize check fires
        # BEFORE the overrun check (which needs p+4+ln <= len(buf)).
        # Here ln=1000 with max_nal_size=10 -> rejected.
        buf = (
            AUD_ANCHOR + oversize_prefix + b"\x01"
            + b"\x00" * 50
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5, max_nal_size=10)
        assert state.bad_blocks == 1
        assert state.frames == 1

    def test_undersize_length_breaks_frame(self):
        """NAL length < min_nal_size aborts the frame."""
        state = VideoExtractionState()
        out = io.BytesIO()
        # length=0 (< MIN_NAL_SIZE=1)
        undersize = b"\x00\x00\x00\x00\x01\x00"
        buf = (
            AUD_ANCHOR + undersize
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.bad_blocks == 1
        assert state.frames == 1

    def test_no_vcl_frame_is_bad_block_but_sps_pps_still_captured(self):
        """Frame with AUD+SPS+PPS but no VCL is bad_block; the
        first_sps and first_pps stash MUST still be populated."""
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = (
            AUD_ANCHOR
            + _make_avcc_nal(7, b"\xaa")
            + _make_avcc_nal(8, b"\xbb")
            + AUD_ANCHOR
            + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.bad_blocks == 1
        assert state.frames == 1
        assert state.first_sps == b"\x07\xaa"
        assert state.first_pps == b"\x08\xbb"


# ---------------------------------------------------------------------
# TestStateTracking
# ---------------------------------------------------------------------

class TestStateTracking:
    def test_frames_nals_written_match_output(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = AUD_ANCHOR + _make_avcc_nal(1, b"\xab" * 3)
        process_video_buffer(buf, out, state, overlap=5)
        assert state.frames == 1
        assert state.nals == 2
        # state.written must equal the number of bytes written.
        assert state.written == len(out.getvalue())

    def test_first_sps_only_captured_once(self):
        """first_sps must NOT be overwritten by a later SPS."""
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = (
            AUD_ANCHOR
            + _make_avcc_nal(7, b"\xde\xad")
            + _make_avcc_nal(1, b"\x00")
            + AUD_ANCHOR
            + _make_avcc_nal(7, b"\xbe\xef")
            + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.first_sps == b"\x07\xde\xad"

    def test_first_pps_only_captured_once(self):
        """first_pps must NOT be overwritten by a later PPS."""
        state = VideoExtractionState()
        out = io.BytesIO()
        buf = (
            AUD_ANCHOR
            + _make_avcc_nal(8, b"\xde\xad")
            + _make_avcc_nal(1, b"\x00")
            + AUD_ANCHOR
            + _make_avcc_nal(8, b"\xbe\xef")
            + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.first_pps == b"\x08\xde\xad"

    def test_counts_dict_per_nal_type(self):
        state = VideoExtractionState()
        out = io.BytesIO()
        # 3 frames: each AUD + slice; one of them also has SPS+PPS
        buf = (
            AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
            + AUD_ANCHOR
            + _make_avcc_nal(7, b"\xaa")
            + _make_avcc_nal(8, b"\xbb")
            + _make_avcc_nal(5, b"\x00")
            + AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        process_video_buffer(buf, out, state, overlap=5)
        assert state.counts[9] == 3
        assert state.counts[1] == 2
        assert state.counts[5] == 1
        assert state.counts[7] == 1
        assert state.counts[8] == 1
        assert state.frames == 3
        assert state.nals == 8


# ---------------------------------------------------------------------
# TestExtractVideoFile
# ---------------------------------------------------------------------

class TestExtractVideoFile:
    def test_extract_creates_output_file_and_returns_result(self, tmp_path):
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        input_path.write_bytes(
            AUD_ANCHOR + _make_avcc_nal(1, b"\xab" * 100)
        )
        result = extract_video(input_path, output_path)
        assert output_path.exists()
        assert isinstance(result, VideoExtractionResult)
        assert result.frames == 1
        assert result.nals == 2
        assert result.input_size == input_path.stat().st_size
        assert result.bytes_written == output_path.stat().st_size

    def test_extract_video_to_annexb_is_equivalent_alias(self, tmp_path):
        input_path = tmp_path / "input.mdt"
        out1 = tmp_path / "out1.h264"
        out2 = tmp_path / "out2.h264"
        input_path.write_bytes(
            AUD_ANCHOR + _make_avcc_nal(1, b"\xab" * 100)
            + AUD_ANCHOR + _make_avcc_nal(5, b"\xcd" * 50)
        )
        r1 = extract_video(input_path, out1)
        r2 = extract_video_to_annexb(input_path, out2)
        # Semantic equivalence on every field of the result.
        assert r1.frames == r2.frames
        assert r1.nals == r2.nals
        assert r1.bytes_written == r2.bytes_written
        assert r1.bad_blocks == r2.bad_blocks
        assert r1.first_sps == r2.first_sps
        assert r1.first_pps == r2.first_pps
        assert dict(r1.nal_type_counts) == dict(r2.nal_type_counts)
        # Byte-exact output equivalence.
        assert out1.read_bytes() == out2.read_bytes()

    def test_extract_accepts_str_paths(self, tmp_path):
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        input_path.write_bytes(
            AUD_ANCHOR + _make_avcc_nal(1, b"\xab" * 50)
        )
        result = extract_video(str(input_path), str(output_path))
        assert result.frames == 1
        assert output_path.exists()

    def test_progress_callback_called_per_chunk(self, tmp_path):
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        frame = AUD_ANCHOR + _make_avcc_nal(1, b"\xab" * 100)
        # Repeat the frame to span multiple small chunks.
        input_path.write_bytes(frame * 5)
        events: list[VideoProgressEvent] = []
        result = extract_video(
            input_path, output_path,
            progress=events.append,
            chunk_size=200,
            overlap=50,
        )
        assert len(events) >= 1
        # chunk_num starts at 1 and is monotonically increasing.
        for i, ev in enumerate(events, start=1):
            assert ev.chunk_num == i
        # The last event's cumulative totals match the final result.
        assert events[-1].frames == result.frames
        assert events[-1].nals == result.nals
        # total_size is constant across events.
        for ev in events:
            assert ev.total_size == input_path.stat().st_size

    def test_default_chunk_size_and_overlap_are_used(self, tmp_path):
        """When chunk_size / overlap are not specified, the defaults
        from the module are applied.  On a tiny file the result is
        a single chunk."""
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        input_path.write_bytes(
            AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        # Verify defaults are reasonable constants (regression guard).
        assert DEFAULT_CHUNK_SIZE == 256 * 1024 * 1024
        assert DEFAULT_OVERLAP == 10 * 1024 * 1024
        result = extract_video(input_path, output_path)
        assert result.chunks == 1
        assert result.frames == 1

    def test_safety_net_truncation_emits_warning(self, tmp_path):
        """Force tail_buf > 2*overlap to trigger the safety-net
        truncation path.  The first chunk's progress event must carry
        a warning string; final_tail_size must end up <= overlap."""
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        # Each 11-byte segment: AUD (6) + length prefix declaring 100
        # bytes (4) + one body byte (1).  ln=100 is < default
        # max_nal_size, valid, BUT the buffer never contains 100 bytes
        # after the prefix -> had_overrun -> process returns
        # frame_start=0 -> tail = whole buf.
        overrun_seg = AUD_ANCHOR + struct.pack(">I", 100) + b"\x01"
        input_path.write_bytes(overrun_seg * 3)  # 33 bytes
        events: list[VideoProgressEvent] = []
        result = extract_video(
            input_path, output_path,
            progress=events.append,
            chunk_size=15,
            overlap=2,
        )
        warned = [e for e in events if e.warning is not None]
        assert len(warned) >= 1
        assert "WARNING" in warned[0].warning
        assert "truncating" in warned[0].warning
        # After truncation the tail must be <= overlap.
        assert result.final_tail_size <= 2


# ---------------------------------------------------------------------
# TestVideoExtractionResult
# ---------------------------------------------------------------------

class TestVideoExtractionResult:
    def _build_result(self, **overrides) -> VideoExtractionResult:
        defaults = dict(
            frames=0, nals=0, bytes_written=0, bad_blocks=0,
            first_sps=None, first_pps=None,
            nal_type_counts=MappingProxyType({}),
            input_size=0, chunks=0, final_tail_size=0,
        )
        defaults.update(overrides)
        return VideoExtractionResult(**defaults)

    def test_result_is_frozen(self):
        r = self._build_result()
        with pytest.raises(FrozenInstanceError):
            r.frames = 1  # type: ignore[misc]

    def test_result_shape_has_all_fields(self):
        r = self._build_result(
            frames=1, nals=2, bytes_written=10, bad_blocks=3,
            first_sps=b"\x07", first_pps=b"\x08",
            nal_type_counts=MappingProxyType({1: 1}),
            input_size=100, chunks=1, final_tail_size=0,
        )
        assert r.frames == 1
        assert r.nals == 2
        assert r.bytes_written == 10
        assert r.bad_blocks == 3
        assert r.first_sps == b"\x07"
        assert r.first_pps == b"\x08"
        assert r.nal_type_counts == {1: 1}
        assert r.input_size == 100
        assert r.chunks == 1
        assert r.final_tail_size == 0

    def test_result_equality_on_fields(self):
        r1 = self._build_result(frames=1, nals=2, bytes_written=10)
        r2 = self._build_result(frames=1, nals=2, bytes_written=10)
        assert r1 == r2

    def test_result_decoupled_from_mutable_state(self, tmp_path):
        """nal_type_counts must be a read-only MappingProxyType
        wrapping a copy.  Mutating attempts must raise; the result
        must remain stable even if the underlying state is mutated
        afterwards (which is not directly observable through the
        public API, but we verify the proxy semantics)."""
        input_path = tmp_path / "input.mdt"
        output_path = tmp_path / "output.h264"
        input_path.write_bytes(
            AUD_ANCHOR + _make_avcc_nal(1, b"\x00")
        )
        result = extract_video(input_path, output_path)
        # The mapping must be a read-only proxy.
        assert isinstance(result.nal_type_counts, MappingProxyType)
        with pytest.raises(TypeError):
            result.nal_type_counts[999] = 99  # type: ignore[index]
        # A copy taken now must equal a copy taken later.
        snapshot1 = dict(result.nal_type_counts)
        snapshot2 = dict(result.nal_type_counts)
        assert snapshot1 == snapshot2
