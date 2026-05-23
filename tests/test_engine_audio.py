"""
tests/test_engine_audio.py
==========================

Unit tests for :mod:`mdt_rescue.engine.audio`.

These tests use synthetic byte sequences (constructed inline) and
:class:`io.BytesIO` / ``tmp_path`` for I/O.  No binary fixtures are
checked into the repository.

The synthetic ``.MDT`` shape used here is::

    AUD_ANCHOR (6 B)  ||  filler (gap_size bytes of zeros)  ||  AUD_ANCHOR (6 B)

This is the minimum viable input that exercises every branch of
:func:`mdt_rescue.engine.audio.process_audio_buffer`.  Real GH5S frames
contain additional NAL units after the AUD, but ``find_frame_end``
terminates at the first byte that does not look like a valid
length-prefixed NAL, so a zero-filled gap is a faithful stand-in.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError

import pytest

from mdt_rescue.engine.audio import (
    AUD_ANCHOR,
    AUDIO_BYTES_PER_SECOND,
    AudioExtractionResult,
    AudioExtractionState,
    AudioProgressEvent,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    EXPECTED_AUDIO_PAYLOAD,
    EXPECTED_GAP_SIZE,
    HandledAnomaly,
    MAX_NAL_SIZE,
    MIN_NAL_SIZE,
    NORMAL_GAP_RANGE,
    TIMECODE_SIZE,
    UnhandledAnomaly,
    VALID_NAL_TYPES,
    VIDEO_FPS_FOR_DRIFT,
    _state_to_result,
    extract_audio,
    extract_audio_to_raw,
    find_frame_end,
    is_valid_nal_header,
    process_audio_buffer,
)


# ---------------------------------------------------------------------
# Synthetic-buffer helpers
# ---------------------------------------------------------------------

def _aud() -> bytes:
    """Return the 6-byte AUD anchor (length=2, header=0x09, payload=0x10)."""
    return AUD_ANCHOR


def _build_two_frames(gap_size: int, filler: bytes = b"\x00") -> bytes:
    """Build ``AUD || (filler * gap_size) || AUD``.

    This represents two consecutive video frames separated by a gap of
    exactly ``gap_size`` bytes.
    """
    return _aud() + (filler * gap_size) + _aud()


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

class TestConstants:
    def test_aud_anchor_value(self):
        assert AUD_ANCHOR == b"\x00\x00\x00\x02\x09\x10"
        assert len(AUD_ANCHOR) == 6

    def test_valid_nal_types(self):
        assert VALID_NAL_TYPES == frozenset({1, 5, 6, 7, 8, 9, 12})
        # frozenset to guarantee immutability
        with pytest.raises(AttributeError):
            VALID_NAL_TYPES.add(99)  # type: ignore[attr-defined]

    def test_audio_layout_constants(self):
        assert EXPECTED_AUDIO_PAYLOAD == 92160
        assert TIMECODE_SIZE == 48
        assert EXPECTED_GAP_SIZE == 92208
        assert EXPECTED_AUDIO_PAYLOAD + TIMECODE_SIZE == EXPECTED_GAP_SIZE

    def test_nal_size_bounds(self):
        assert MIN_NAL_SIZE == 1
        assert MAX_NAL_SIZE == 5 * 1024 * 1024

    def test_streaming_constants(self):
        assert DEFAULT_CHUNK_SIZE == 256 * 1024 * 1024
        assert DEFAULT_OVERLAP == 10 * 1024 * 1024

    def test_drift_constants(self):
        # FHD 25p
        assert VIDEO_FPS_FOR_DRIFT == 25.0
        # 48000 Hz * 2 channels * 2 bytes/sample = 192000 B/s
        assert AUDIO_BYTES_PER_SECOND == 192000

    def test_normal_gap_range(self):
        assert NORMAL_GAP_RANGE == (90000, 95000)
        # Expected gap lies strictly inside the range.
        assert NORMAL_GAP_RANGE[0] < EXPECTED_GAP_SIZE < NORMAL_GAP_RANGE[1]


# ---------------------------------------------------------------------
# is_valid_nal_header
# ---------------------------------------------------------------------

class TestIsValidNalHeader:
    def test_aud_header_is_valid(self):
        # 0x09: forbidden bit 0, type 9 (AUD).
        assert is_valid_nal_header(0x09) is True

    def test_idr_slice_is_valid(self):
        # 0x65: forbidden bit 0, type 5 (IDR slice).
        assert is_valid_nal_header(0x65) is True

    def test_all_valid_types_pass(self):
        for nt in VALID_NAL_TYPES:
            # forbidden bit clear, low 5 bits set to nt
            assert is_valid_nal_header(nt) is True, f"type {nt} should be valid"

    def test_forbidden_bit_rejects(self):
        # 0x89: forbidden bit set, type 9.  Must reject.
        assert is_valid_nal_header(0x89) is False

    def test_invalid_type_rejects(self):
        # type 10 is not in VALID_NAL_TYPES.
        assert is_valid_nal_header(0x0A) is False
        # type 0 either.
        assert is_valid_nal_header(0x00) is False


# ---------------------------------------------------------------------
# find_frame_end
# ---------------------------------------------------------------------

class TestFindFrameEnd:
    def test_returns_offset_of_next_aud(self):
        buf = _build_two_frames(gap_size=100)
        # Frame starts at offset 0 (AUD).  After the 6-byte AUD NAL the
        # gap is zero bytes, so find_frame_end should detect the start
        # of the next AUD chunk at offset 6 ... but since the gap is
        # zero-filled (not raw AUD), find_frame_end terminates at the
        # first byte that fails the NAL-length check, which is the very
        # first zero of the filler at offset 6.
        assert find_frame_end(buf, 0) == 6

    def test_returns_offset_when_filler_is_invalid_length(self):
        # Filler of zeros: read_u32be returns 0 < MIN_NAL_SIZE -> return p.
        buf = _aud() + b"\x00" * 100 + _aud()
        assert find_frame_end(buf, 0) == 6

    def test_returns_none_when_buffer_too_short(self):
        # AUD only, no trailing bytes.  Loop condition fails.
        buf = _aud()
        assert find_frame_end(buf, 0) is None

    def test_returns_none_when_frame_start_is_not_aud(self):
        # Build a length-prefixed NAL of type 5 (IDR) at offset 0.
        # find_frame_end starts at frame_start, sees a valid NAL but
        # nt != 9, returns None.
        nal_body = b"\x65\x88\x88\x88\x88"  # 5 bytes: header 0x65 + 4 byte payload
        ln = len(nal_body)
        buf = ln.to_bytes(4, "big") + nal_body + _aud() + b"\x00" * 200
        assert find_frame_end(buf, 0) is None

    def test_returns_offset_on_invalid_header(self):
        # Length-prefixed NAL with forbidden bit set in the header.
        # find_frame_end returns p (the offset of the invalid NAL).
        nal_body = b"\x89\xAA\xAA\xAA\xAA"  # header 0x89: forbidden bit
        ln = len(nal_body)
        # Prepend a valid AUD so we can frame_start at 0.
        buf = _aud() + ln.to_bytes(4, "big") + nal_body + b"\x00" * 200
        # frame_start=0: AUD parses, p advances to 6.  At p=6 we have
        # a length-prefixed NAL whose header has the forbidden bit set
        # -> return p = 6.
        assert find_frame_end(buf, 0) == 6


# ---------------------------------------------------------------------
# process_audio_buffer
# ---------------------------------------------------------------------

class TestProcessAudioBuffer:
    def test_no_aud_anchor_returns_tail_offset(self):
        buf = b"\x00" * 1000
        out = io.BytesIO()
        state = AudioExtractionState()
        end = process_audio_buffer(buf, 0, out, state, overlap=200)
        # max(0, 1000 - 200) = 800
        assert end == 800
        assert state.frames == 0
        assert out.getvalue() == b""

    def test_no_aud_anchor_small_buffer_returns_zero(self):
        # When len(buf) < overlap, max(0, ...) clamps to 0.
        buf = b"\x00" * 50
        out = io.BytesIO()
        state = AudioExtractionState()
        end = process_audio_buffer(buf, 0, out, state, overlap=200)
        assert end == 0

    def test_gap_zero_counted_no_output(self):
        # AUD || AUD: gap is zero, no audio written.
        buf = _build_two_frames(gap_size=0)
        out = io.BytesIO()
        state = AudioExtractionState()
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        assert state.gap_zero == 1
        assert state.frames == 1
        assert state.audio_bytes == 0
        assert state.normal_chunks == 0
        assert out.getvalue() == b""

    def test_normal_nominal_gap_writes_92160(self):
        # gap_size == 92208 (EXPECTED_GAP_SIZE) -> write 92160 bytes.
        buf = _build_two_frames(gap_size=EXPECTED_GAP_SIZE)
        out = io.BytesIO()
        state = AudioExtractionState()
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        assert state.normal_chunks == 1
        assert state.audio_bytes == EXPECTED_AUDIO_PAYLOAD
        assert state.frames == 1
        assert len(out.getvalue()) == EXPECTED_AUDIO_PAYLOAD

    def test_normal_off_nominal_gap_writes_gap_minus_48(self):
        # INVARIANT: for gap inside NORMAL_GAP_RANGE but != 92208,
        # the engine writes (gap_size - TIMECODE_SIZE) bytes,
        # NOT a fixed 92160.  This guards against a tempting "fix".
        gap = 93000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        state = AudioExtractionState()
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        expected_written = gap - TIMECODE_SIZE  # 92952, NOT 92160
        assert state.audio_bytes == expected_written
        assert len(out.getvalue()) == expected_written
        assert state.normal_chunks == 1

    def test_normal_gap_at_lower_bound(self):
        gap = NORMAL_GAP_RANGE[0]  # 90000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        state = AudioExtractionState()
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        assert state.audio_bytes == gap - TIMECODE_SIZE
        assert state.normal_chunks == 1

    def test_normal_gap_at_upper_bound(self):
        gap = NORMAL_GAP_RANGE[1]  # 95000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        state = AudioExtractionState()
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        assert state.audio_bytes == gap - TIMECODE_SIZE
        assert state.normal_chunks == 1

    def test_handled_anomaly_at_12_frames(self):
        # Anomalous gap (50000, out of NORMAL_GAP_RANGE) when
        # frames_since_last_audio == 12 -> handled, silence injected.
        gap = 50000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        state = AudioExtractionState(frames=12, last_audio_at_frame=0)
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        # frames_since_last_audio = 12 - 0 = 12 -> handled.
        assert state.silence_inserted == 1
        assert state.audio_bytes == EXPECTED_AUDIO_PAYLOAD
        assert state.last_audio_at_frame == 12  # updated to current frame
        assert len(state.handled_anomalies) == 1
        anomaly = state.handled_anomalies[0]
        assert isinstance(anomaly, HandledAnomaly)
        assert anomaly.frame_idx == 12
        assert anomaly.gap_size == gap
        # File offset = buf_file_offset (0) + frame_end (6).
        assert anomaly.file_offset == 6
        # Output is exactly the silence block.
        assert out.getvalue() == b"\x00" * EXPECTED_AUDIO_PAYLOAD
        assert len(state.unhandled_anomalies) == 0

    def test_unhandled_anomaly_off_cycle(self):
        # Same anomalous gap, but frames_since_last_audio != 12 -> unhandled.
        gap = 50000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        # frames=5, last_audio_at_frame=0 -> frames_since_last_audio = 5
        state = AudioExtractionState(frames=5, last_audio_at_frame=0)
        process_audio_buffer(buf, 0, out, state, overlap=DEFAULT_OVERLAP)
        assert state.silence_inserted == 0
        assert state.audio_bytes == 0
        assert state.last_audio_at_frame == 0  # NOT updated
        assert len(state.handled_anomalies) == 0
        assert len(state.unhandled_anomalies) == 1
        anomaly = state.unhandled_anomalies[0]
        assert isinstance(anomaly, UnhandledAnomaly)
        assert anomaly.frame_idx == 5
        assert anomaly.gap_size == gap
        assert anomaly.frames_since_last_audio == 5
        assert anomaly.file_offset == 6
        # No data written.
        assert out.getvalue() == b""

    def test_file_offset_uses_buf_file_offset(self):
        # buf_file_offset is the file position of buf[0].  Anomaly
        # offsets must be reported as (buf_file_offset + frame_end).
        gap = 50000
        buf = _build_two_frames(gap_size=gap)
        out = io.BytesIO()
        state = AudioExtractionState(frames=12, last_audio_at_frame=0)
        process_audio_buffer(
            buf, 1_000_000, out, state, overlap=DEFAULT_OVERLAP,
        )
        assert state.handled_anomalies[0].file_offset == 1_000_006


# ---------------------------------------------------------------------
# AudioExtractionResult
# ---------------------------------------------------------------------

class TestAudioExtractionResult:
    def _make_result(self, **overrides) -> AudioExtractionResult:
        defaults = dict(
            frames=100,
            normal_chunks=8,
            silence_inserted=0,
            audio_bytes=8 * EXPECTED_AUDIO_PAYLOAD,
            gap_zero=92,
            unhandled_count=0,
            handled_anomalies=(),
            unhandled_anomalies=(),
            video_duration_sec=4.0,
            audio_duration_sec=3.84,
            drift_sec=-0.16,
            input_size=1_000_000,
            chunks=1,
            final_tail_size=0,
        )
        defaults.update(overrides)
        return AudioExtractionResult(**defaults)

    def test_result_is_frozen(self):
        result = self._make_result()
        with pytest.raises(FrozenInstanceError):
            result.frames = 999  # type: ignore[misc]

    def test_result_equality_on_fields(self):
        a = self._make_result()
        b = self._make_result()
        assert a == b
        c = self._make_result(frames=200)
        assert a != c

    def test_total_effective_chunks(self):
        result = self._make_result(normal_chunks=10, silence_inserted=3)
        assert result.total_effective_chunks == 13

    def test_audio_bytes_mod_4(self):
        # Nominal: 8 * 92160 = 737280, divisible by 4.
        result = self._make_result(audio_bytes=8 * EXPECTED_AUDIO_PAYLOAD)
        assert result.audio_bytes_mod_4 == 0
        # Pathological off-by-one.
        result = self._make_result(audio_bytes=12345)
        assert result.audio_bytes_mod_4 == 1

    def test_anomaly_tuples_are_immutable(self):
        result = self._make_result(
            handled_anomalies=(HandledAnomaly(1, 50000, 100),),
        )
        # Tuples cannot grow.
        with pytest.raises(AttributeError):
            result.handled_anomalies.append(  # type: ignore[attr-defined]
                HandledAnomaly(2, 50001, 200)
            )

    def test_result_decoupled_from_mutable_state(self):
        # Build a state, snapshot it, then mutate the state and verify
        # the snapshot is unaffected.
        state = AudioExtractionState(
            frames=5,
            audio_bytes=100,
        )
        state.handled_anomalies.append(HandledAnomaly(1, 50000, 0))
        state.unhandled_anomalies.append(
            UnhandledAnomaly(2, 51000, 10, frames_since_last_audio=7)
        )

        result = _state_to_result(
            state, input_size=1000, chunks=1, final_tail_size=0,
        )

        # Mutate the state AFTER snapshot.
        state.frames = 999
        state.audio_bytes = 99_999
        state.handled_anomalies.append(HandledAnomaly(99, 99, 99))
        state.unhandled_anomalies.append(
            UnhandledAnomaly(99, 99, 99, frames_since_last_audio=99)
        )

        # The snapshot must remain untouched.
        assert result.frames == 5
        assert result.audio_bytes == 100
        assert len(result.handled_anomalies) == 1
        assert result.handled_anomalies[0].frame_idx == 1
        assert len(result.unhandled_anomalies) == 1
        assert result.unhandled_anomalies[0].frame_idx == 2
        assert isinstance(result.handled_anomalies, tuple)
        assert isinstance(result.unhandled_anomalies, tuple)


# ---------------------------------------------------------------------
# AudioExtractionState
# ---------------------------------------------------------------------

class TestAudioExtractionState:
    def test_state_is_mutable(self):
        state = AudioExtractionState()
        state.frames = 42
        state.audio_bytes = 1024
        state.handled_anomalies.append(HandledAnomaly(0, 0, 0))
        assert state.frames == 42
        assert state.audio_bytes == 1024
        assert len(state.handled_anomalies) == 1

    def test_state_defaults(self):
        state = AudioExtractionState()
        assert state.frames == 0
        assert state.normal_chunks == 0
        assert state.silence_inserted == 0
        assert state.audio_bytes == 0
        assert state.gap_zero == 0
        # Sentinel for "no audio seen yet".
        assert state.last_audio_at_frame == -1
        assert state.handled_anomalies == []
        assert state.unhandled_anomalies == []


# ---------------------------------------------------------------------
# extract_audio (file I/O on tmp_path)
# ---------------------------------------------------------------------

class TestExtractAudio:
    def test_happy_path_writes_expected_audio_bytes(self, tmp_path):
        # One frame with a nominal gap.
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        mdt.write_bytes(_build_two_frames(gap_size=EXPECTED_GAP_SIZE))

        result = extract_audio(mdt, out)

        assert out.read_bytes() == b"\x00" * EXPECTED_AUDIO_PAYLOAD
        assert result.audio_bytes == EXPECTED_AUDIO_PAYLOAD
        assert result.normal_chunks == 1
        assert result.frames == 1
        assert result.gap_zero == 0
        assert result.silence_inserted == 0
        assert result.unhandled_count == 0
        assert result.handled_anomalies == ()
        assert result.unhandled_anomalies == ()
        assert result.input_size == EXPECTED_GAP_SIZE + 12  # 2 * AUD + gap
        assert result.chunks == 1

    def test_drift_computed_from_counters(self, tmp_path):
        # One frame, one nominal audio chunk.
        # video_duration = 1 / 25  = 0.04 s
        # audio_duration = 92160 / 192000 = 0.48 s
        # drift = 0.48 - 0.04 = 0.44 s
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        mdt.write_bytes(_build_two_frames(gap_size=EXPECTED_GAP_SIZE))

        result = extract_audio(mdt, out)
        assert result.video_duration_sec == pytest.approx(0.04)
        assert result.audio_duration_sec == pytest.approx(0.48)
        assert result.drift_sec == pytest.approx(0.44)

    def test_zero_frames_yields_zero_durations(self, tmp_path):
        # Buffer with no AUD anchor -> 0 frames -> 0 durations, 0 drift.
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        mdt.write_bytes(b"\x00" * 1024)

        result = extract_audio(mdt, out)
        assert result.frames == 0
        assert result.audio_bytes == 0
        assert result.video_duration_sec == 0.0
        assert result.audio_duration_sec == 0.0
        assert result.drift_sec == 0.0

    def test_input_size_recorded(self, tmp_path):
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        payload = _build_two_frames(gap_size=0)
        mdt.write_bytes(payload)
        result = extract_audio(mdt, out)
        assert result.input_size == len(payload)


# ---------------------------------------------------------------------
# extract_audio_to_raw alias
# ---------------------------------------------------------------------

class TestExtractAudioAlias:
    def test_alias_produces_identical_bytes(self, tmp_path):
        mdt = tmp_path / "synth.mdt"
        out_a = tmp_path / "via_primary.raw"
        out_b = tmp_path / "via_alias.raw"
        mdt.write_bytes(_build_two_frames(gap_size=EXPECTED_GAP_SIZE))

        result_primary = extract_audio(mdt, out_a)
        result_alias = extract_audio_to_raw(mdt, out_b)

        assert out_a.read_bytes() == out_b.read_bytes()
        assert result_primary == result_alias


# ---------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------

class TestProgressCallback:
    def test_callback_called_once_per_chunk(self, tmp_path):
        # Small chunk_size forces multiple chunks.
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        # Build a 4096-byte file of mostly filler with a single AUD pair
        # at the start to make the data realistic.
        payload = _build_two_frames(gap_size=0)
        payload = payload + b"\x00" * (4096 - len(payload))
        mdt.write_bytes(payload)

        events: list[AudioProgressEvent] = []
        # chunk_size=1024 -> 4 chunks for a 4096-byte input.
        extract_audio(
            mdt, out, progress=events.append, chunk_size=1024, overlap=128,
        )
        assert len(events) == 4
        assert [e.chunk_num for e in events] == [1, 2, 3, 4]

    def test_callback_event_is_frozen_and_carries_raw_fields(self, tmp_path):
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        mdt.write_bytes(_build_two_frames(gap_size=EXPECTED_GAP_SIZE))

        events: list[AudioProgressEvent] = []
        result = extract_audio(mdt, out, progress=events.append)

        assert len(events) >= 1
        final = events[-1]
        assert isinstance(final, AudioProgressEvent)
        with pytest.raises(FrozenInstanceError):
            final.chunk_num = 999  # type: ignore[misc]
        # Final event mirrors the final state.
        assert final.bytes_read == result.input_size
        assert final.total_bytes == result.input_size
        assert final.frames == result.frames
        assert final.audio_bytes == result.audio_bytes

    def test_callback_none_does_not_raise(self, tmp_path):
        mdt = tmp_path / "synth.mdt"
        out = tmp_path / "out.raw"
        mdt.write_bytes(_build_two_frames(gap_size=EXPECTED_GAP_SIZE))
        # Explicitly passing progress=None should be a no-op.
        result = extract_audio(mdt, out, progress=None)
        assert result.normal_chunks == 1
