"""
Unit tests for mdt_rescue.engine.sps_pps.

All tests work on synthetic Annex B byte streams built inline. No
binary fixtures, no I/O against real .mov or .h264 files. The
bit-exact behaviour against the real v0.1 pipeline is verified
separately by the full-pipeline smoke test in commit B.1.wrapper.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mdt_rescue.engine.sps_pps import (
    DEFAULT_MAX_PPS_SIZE,
    DEFAULT_MAX_SPS_SIZE,
    SpsPpsNotFoundError,
    SpsPpsResult,
    extract_and_write_prefix,
    extract_sps_pps,
    extract_sps_pps_from_bytes,
    find_next_start_code,
)

# ---------------------------------------------------------------------------
# Helpers: build tiny synthetic Annex B streams
# ---------------------------------------------------------------------------

SC4 = b"\x00\x00\x00\x01"
SC3 = b"\x00\x00\x01"

# NAL header byte: forbidden_zero_bit (1) | nal_ref_idc (2) | nal_type (5)
NAL_HEADER_SPS = bytes([0x67])  # 0b0_11_00111 -> nal_type 7
NAL_HEADER_PPS = bytes([0x68])  # 0b0_11_01000 -> nal_type 8
NAL_HEADER_IDR = bytes([0x65])  # 0b0_11_00101 -> nal_type 5 (IDR slice)
NAL_HEADER_AUD = bytes([0x09])  # 0b0_00_01001 -> nal_type 9 (AUD)
NAL_HEADER_FORBIDDEN_SPS = bytes([0xE7])  # forbidden bit = 1, would be SPS otherwise


def _sps(payload: bytes = b"\xAA" * 20) -> bytes:
    """A synthetic SPS NAL: header + payload (no start code)."""
    return NAL_HEADER_SPS + payload


def _pps(payload: bytes = b"\xBB" * 8) -> bytes:
    """A synthetic PPS NAL: header + payload (no start code)."""
    return NAL_HEADER_PPS + payload


def _idr(payload: bytes = b"\xCC" * 32) -> bytes:
    """A synthetic IDR slice NAL: header + payload (no start code)."""
    return NAL_HEADER_IDR + payload


# ---------------------------------------------------------------------------
# find_next_start_code
# ---------------------------------------------------------------------------


class TestFindNextStartCode:
    def test_4byte_start_code_at_zero(self):
        data = SC4 + b"\x67\xAA\xBB"
        assert find_next_start_code(data, 0) == (0, 4)

    def test_3byte_start_code_at_zero(self):
        data = SC3 + b"\x67\xAA\xBB"
        assert find_next_start_code(data, 0) == (0, 3)

    def test_prefers_4byte_over_3byte_when_both_match(self):
        # 00 00 00 01 starts at offset 0; a naive parser might also see
        # 00 00 01 at offset 1, but find_next_start_code must report
        # (0, 4) and never (1, 3).
        data = SC4 + b"\x67\xAA"
        off, length = find_next_start_code(data, 0)
        assert (off, length) == (0, 4)

    def test_skips_to_next_start_code(self):
        data = b"\xDE\xAD\xBE\xEF" + SC4 + b"\x67"
        assert find_next_start_code(data, 0) == (4, 4)

    def test_search_starts_at_pos(self):
        # Two start codes; pos=4 must find the second one, not the first.
        data = SC4 + b"\x67\xAA\xBB" + SC4 + b"\x68\xCC"
        first = find_next_start_code(data, 0)
        second = find_next_start_code(data, first[0] + first[1])
        assert first == (0, 4)
        assert second == (7, 4)

    def test_not_found_returns_minus_one_zero(self):
        assert find_next_start_code(b"\xDE\xAD\xBE\xEF\xCA\xFE", 0) == (-1, 0)

    def test_empty_input_returns_minus_one_zero(self):
        assert find_next_start_code(b"", 0) == (-1, 0)

    def test_too_short_for_start_code_returns_minus_one_zero(self):
        assert find_next_start_code(b"\x00\x00", 0) == (-1, 0)


# ---------------------------------------------------------------------------
# extract_sps_pps_from_bytes - happy paths
# ---------------------------------------------------------------------------


class TestExtractFromBytesHappyPath:
    def test_minimal_sps_then_pps(self):
        sps = _sps()
        pps = _pps()
        stream = SC4 + sps + SC4 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert isinstance(result, SpsPpsResult)
        assert result.sps_bytes == sps
        assert result.pps_bytes == pps
        assert result.sps_offset == 0
        assert result.pps_offset == 4 + len(sps)
        assert result.source_size == len(stream)
        assert result.prefix_annexb == SC4 + sps + SC4 + pps

    def test_pps_before_sps_is_still_extracted_correctly(self):
        # The legacy scanner walks in order and accepts the first valid
        # SPS and the first valid PPS independently. PPS appearing
        # before SPS in the stream must still produce a valid result.
        sps = _sps()
        pps = _pps()
        stream = SC4 + pps + SC4 + sps

        result = extract_sps_pps_from_bytes(stream)

        assert result.sps_bytes == sps
        assert result.pps_bytes == pps
        assert result.pps_offset == 0
        assert result.sps_offset == 4 + len(pps)

    def test_other_nal_types_are_ignored(self):
        sps = _sps()
        pps = _pps()
        stream = SC4 + bytes([0x09]) + b"\x10" + SC4 + sps + SC4 + _idr() + SC4 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.sps_bytes == sps
        assert result.pps_bytes == pps

    def test_output_prefix_always_uses_4byte_start_codes(self):
        # Input uses 3-byte start codes; output must still be 4-byte.
        sps = _sps()
        pps = _pps()
        stream = SC3 + sps + SC3 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.prefix_annexb.startswith(SC4)
        assert result.prefix_annexb == SC4 + sps + SC4 + pps

    def test_first_sps_wins_subsequent_sps_ignored(self):
        sps1 = _sps(b"\x11" * 10)
        sps2 = _sps(b"\x22" * 10)
        pps = _pps()
        stream = SC4 + sps1 + SC4 + sps2 + SC4 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.sps_bytes == sps1

    def test_first_pps_wins_subsequent_pps_ignored(self):
        sps = _sps()
        pps1 = _pps(b"\x33" * 5)
        pps2 = _pps(b"\x44" * 5)
        stream = SC4 + sps + SC4 + pps1 + SC4 + pps2

        result = extract_sps_pps_from_bytes(stream)

        assert result.pps_bytes == pps1


# ---------------------------------------------------------------------------
# extract_sps_pps_from_bytes - size caps
# ---------------------------------------------------------------------------


class TestExtractFromBytesSizeCaps:
    def test_oversized_sps_is_skipped_then_valid_sps_accepted(self):
        # An SPS whose payload exceeds the cap is silently skipped;
        # the scanner keeps looking until it finds a SPS that fits.
        big_sps = _sps(b"\x55" * (DEFAULT_MAX_SPS_SIZE + 50))
        good_sps = _sps(b"\x66" * 20)
        pps = _pps()
        stream = SC4 + big_sps + SC4 + good_sps + SC4 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.sps_bytes == good_sps

    def test_oversized_pps_is_skipped_then_valid_pps_accepted(self):
        sps = _sps()
        big_pps = _pps(b"\x77" * (DEFAULT_MAX_PPS_SIZE + 50))
        good_pps = _pps(b"\x88" * 10)
        stream = SC4 + sps + SC4 + big_pps + SC4 + good_pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.pps_bytes == good_pps

    def test_custom_max_sps_size_is_respected(self):
        # With a tight cap, even a small SPS can be rejected.
        sps = _sps(b"\x99" * 50)
        pps = _pps()
        stream = SC4 + sps + SC4 + pps

        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(stream, max_sps_size=10)

        assert exc.value.sps_found is False
        assert exc.value.pps_found is True


# ---------------------------------------------------------------------------
# extract_sps_pps_from_bytes - error paths
# ---------------------------------------------------------------------------


class TestExtractFromBytesErrorPaths:
    def test_empty_input_raises_with_both_false(self):
        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(b"")

        assert exc.value.sps_found is False
        assert exc.value.pps_found is False

    def test_no_start_codes_at_all_raises_with_both_false(self):
        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(b"\xDE\xAD\xBE\xEF" * 8)

        assert exc.value.sps_found is False
        assert exc.value.pps_found is False

    def test_only_sps_present_raises_with_sps_true_pps_false(self):
        stream = SC4 + _sps()

        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(stream)

        assert exc.value.sps_found is True
        assert exc.value.pps_found is False

    def test_only_pps_present_raises_with_sps_false_pps_true(self):
        stream = SC4 + _pps()

        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(stream)

        assert exc.value.sps_found is False
        assert exc.value.pps_found is True

    def test_forbidden_zero_bit_set_causes_unit_to_be_ignored(self):
        # A NAL with forbidden_zero_bit = 1 must NOT be accepted, even
        # if its nal_type field equals 7 (SPS).
        forbidden_sps_payload = NAL_HEADER_FORBIDDEN_SPS + b"\xAA" * 10
        stream = SC4 + forbidden_sps_payload + SC4 + _pps()

        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps_from_bytes(stream)

        assert exc.value.sps_found is False
        assert exc.value.pps_found is True


# ---------------------------------------------------------------------------
# SpsPpsResult: immutability and shape
# ---------------------------------------------------------------------------


class TestSpsPpsResult:
    def test_result_is_frozen(self):
        result = extract_sps_pps_from_bytes(SC4 + _sps() + SC4 + _pps())
        with pytest.raises(FrozenInstanceError):
            result.sps_bytes = b""  # type: ignore[misc]

    def test_prefix_is_concatenation_of_start_codes_and_nals(self):
        sps = _sps(b"\x01" * 15)
        pps = _pps(b"\x02" * 7)
        stream = SC4 + sps + SC4 + pps

        result = extract_sps_pps_from_bytes(stream)

        assert result.prefix_annexb == SC4 + sps + SC4 + pps
        # Total prefix size: 2 start codes (4+4) + sps + pps
        assert len(result.prefix_annexb) == 8 + len(sps) + len(pps)


# ---------------------------------------------------------------------------
# SpsPpsNotFoundError: shape
# ---------------------------------------------------------------------------


class TestSpsPpsNotFoundError:
    def test_exception_carries_both_flags(self):
        exc = SpsPpsNotFoundError(sps_found=False, pps_found=True)
        assert exc.sps_found is False
        assert exc.pps_found is True

    def test_exception_message_includes_flags(self):
        exc = SpsPpsNotFoundError(sps_found=False, pps_found=True)
        assert "sps_found=False" in str(exc)
        assert "pps_found=True" in str(exc)


# ---------------------------------------------------------------------------
# File-level wrappers: extract_and_write_prefix and extract_sps_pps alias
# ---------------------------------------------------------------------------


class TestFileWrappers:
    def test_extract_and_write_prefix_writes_expected_bytes(self, tmp_path):
        sps = _sps()
        pps = _pps()
        stream = SC4 + sps + SC4 + pps

        in_path = tmp_path / "in.h264"
        out_path = tmp_path / "out_prefix.h264"
        in_path.write_bytes(stream)

        result = extract_and_write_prefix(in_path, out_path)

        assert out_path.read_bytes() == SC4 + sps + SC4 + pps
        assert result.source_size == len(stream)

    def test_extract_sps_pps_alias_is_equivalent(self, tmp_path):
        sps = _sps()
        pps = _pps()
        stream = SC4 + sps + SC4 + pps

        in_path = tmp_path / "in.h264"
        out_a = tmp_path / "out_a.h264"
        out_b = tmp_path / "out_b.h264"
        in_path.write_bytes(stream)

        result_a = extract_and_write_prefix(in_path, out_a)
        result_b = extract_sps_pps(in_path, out_b)

        # Compare on semantic fields, not the whole dataclass: this stays
        # green if future evolutions add side-effect fields (e.g.
        # output_path, bytes_written) that legitimately differ between
        # two separate calls writing to different paths.
        assert out_a.read_bytes() == out_b.read_bytes()
        assert result_a.prefix_annexb == result_b.prefix_annexb
        assert result_a.sps_bytes == result_b.sps_bytes
        assert result_a.pps_bytes == result_b.pps_bytes

    def test_extract_sps_pps_accepts_str_paths(self, tmp_path):
        stream = SC4 + _sps() + SC4 + _pps()
        in_path = tmp_path / "in.h264"
        out_path = tmp_path / "out.h264"
        in_path.write_bytes(stream)

        # Passing plain strings, not Path objects.
        result = extract_sps_pps(str(in_path), str(out_path))

        assert out_path.read_bytes() == result.prefix_annexb

    def test_extract_sps_pps_propagates_not_found_error(self, tmp_path):
        in_path = tmp_path / "empty.h264"
        out_path = tmp_path / "should_not_exist.h264"
        in_path.write_bytes(b"")

        with pytest.raises(SpsPpsNotFoundError) as exc:
            extract_sps_pps(in_path, out_path)

        assert exc.value.sps_found is False
        assert exc.value.pps_found is False
        assert not out_path.exists()
