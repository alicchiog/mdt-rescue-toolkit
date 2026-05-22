"""
mdt_rescue.engine.sps_pps
=========================

Engine module responsible for locating and extracting the first valid
SPS (Sequence Parameter Set) and PPS (Picture Parameter Set) NAL units
from an Annex B H.264 byte stream, and for producing the small "prefix"
file that the v0.1 pipeline concatenates in front of the recovered raw
H.264 to give ffmpeg the parameter sets it needs when wrapping the
stream into a MOV.

This module is a pure-Python port of the legacy
``scripts/extract_sps_pps.py`` shipped with MDT Rescue Toolkit v0.1.0.
Behaviour is intentionally bit-exact with the legacy script:

* both 4-byte (``00 00 00 01``) and 3-byte (``00 00 01``) start codes
  are recognised; a 4-byte start code is never misreported as a 3-byte
  one at offset+1;
* SPS and PPS are each capped at 200 bytes by default, which prevents
  the classic "false giant PPS" bug where a lookahead-based parser
  accidentally absorbs the next NAL unit into the PPS payload;
* the output prefix is always written with 4-byte start codes, even if
  the input stream used 3-byte start codes;
* the first valid SPS and the first valid PPS are returned; subsequent
  occurrences are ignored.

Public API
----------

``extract_sps_pps(input_path, output_path) -> SpsPpsResult``
    Primary high-level entry point. Reads an Annex B file, locates
    SPS+PPS, writes the prefix file, returns the result. This is the
    function that ``orchestrator.py`` will call.

``extract_and_write_prefix(input_path, output_path, ...) -> SpsPpsResult``
    Alias of ``extract_sps_pps`` that makes the side effect explicit in
    its name. Useful when the caller wants to document intent at the
    call site. Both functions are interchangeable.

``extract_sps_pps_from_bytes(annexb_data, ...) -> SpsPpsResult``
    Pure function: no I/O. Takes bytes, returns a result. Ideal for
    unit testing and for callers that already hold the stream in memory.

``find_next_start_code(data, pos) -> tuple[int, int]``
    Low-level scanner. Returns ``(offset, length)`` of the next start
    code at or after ``pos``, or ``(-1, 0)`` if none is found.

``SpsPpsResult``
    Frozen dataclass holding the extracted SPS/PPS bytes, their offsets
    in the source stream, the ready-to-write Annex B prefix, and the
    size of the source that was scanned.

``SpsPpsNotFoundError``
    Raised when SPS and/or PPS cannot be located. Carries
    ``sps_found`` and ``pps_found`` booleans so callers (e.g. the v0.1
    thin wrapper) can preserve the legacy error message
    ``ERROR: SPS=False, PPS=True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union

__all__ = [
    "DEFAULT_MAX_SPS_SIZE",
    "DEFAULT_MAX_PPS_SIZE",
    "SpsPpsResult",
    "SpsPpsNotFoundError",
    "find_next_start_code",
    "extract_sps_pps_from_bytes",
    "extract_and_write_prefix",
    "extract_sps_pps",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_SPS_SIZE = 200
"""Upper bound on the accepted SPS NAL size, in bytes.

Same value as the legacy ``MAX_SPS_SIZE`` in
``scripts/extract_sps_pps.py``. Protects against the "false giant PPS"
class of parser bugs.
"""

DEFAULT_MAX_PPS_SIZE = 200
"""Upper bound on the accepted PPS NAL size, in bytes.

Same value as the legacy ``MAX_PPS_SIZE`` in
``scripts/extract_sps_pps.py``.
"""

_START_CODE_4 = b"\x00\x00\x00\x01"


# ---------------------------------------------------------------------------
# Result type and error type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpsPpsResult:
    """Immutable result of a successful SPS/PPS extraction.

    Attributes
    ----------
    sps_bytes:
        Raw SPS NAL payload (NAL header included, no start code).
    pps_bytes:
        Raw PPS NAL payload (NAL header included, no start code).
    sps_offset:
        Offset in the source stream where the SPS start code begins.
    pps_offset:
        Offset in the source stream where the PPS start code begins.
    prefix_annexb:
        Ready-to-write Annex B prefix:
        ``00 00 00 01 || SPS || 00 00 00 01 || PPS``.
    source_size:
        Size in bytes of the input that was scanned. Used by the v0.1
        thin wrapper to reproduce the legacy ``Input size: X.X MB``
        message.
    """

    sps_bytes: bytes
    pps_bytes: bytes
    sps_offset: int
    pps_offset: int
    prefix_annexb: bytes
    source_size: int


class SpsPpsNotFoundError(Exception):
    """Raised when SPS and/or PPS cannot be located in the input stream.

    The booleans ``sps_found`` and ``pps_found`` describe which of the
    two parameter sets was located before the scan ended. The v0.1
    thin wrapper uses them to reproduce the legacy stderr line
    ``ERROR: SPS=False, PPS=True``.
    """

    def __init__(self, sps_found: bool, pps_found: bool) -> None:
        self.sps_found = sps_found
        self.pps_found = pps_found
        super().__init__(
            f"SPS/PPS not found (sps_found={sps_found}, pps_found={pps_found})"
        )


# ---------------------------------------------------------------------------
# Low-level: start code scanner
# ---------------------------------------------------------------------------


def find_next_start_code(data: bytes, pos: int) -> tuple[int, int]:
    """Find the next H.264 Annex B start code at or after ``pos``.

    A 4-byte start code (``00 00 00 01``) is reported at its own
    offset; a 3-byte start code (``00 00 01``) is never reported when
    it is in fact the tail of a 4-byte one. This matches the legacy
    behaviour exactly.

    Parameters
    ----------
    data:
        Annex B byte stream.
    pos:
        Offset to start the search from (inclusive).

    Returns
    -------
    tuple[int, int]
        ``(offset, length)`` where ``length`` is 3 or 4, or
        ``(-1, 0)`` if no start code is found.
    """
    n = len(data)
    p = pos
    while p < n - 2:
        if data[p] == 0 and data[p + 1] == 0:
            if p + 3 < n and data[p + 2] == 0 and data[p + 3] == 1:
                return p, 4
            if data[p + 2] == 1:
                return p, 3
        p += 1
    return -1, 0


# ---------------------------------------------------------------------------
# Mid-level: pure extraction from bytes
# ---------------------------------------------------------------------------


def extract_sps_pps_from_bytes(
    annexb_data: bytes,
    max_sps_size: int = DEFAULT_MAX_SPS_SIZE,
    max_pps_size: int = DEFAULT_MAX_PPS_SIZE,
) -> SpsPpsResult:
    """Extract the first valid SPS and PPS from an Annex B byte stream.

    The scan walks NAL units in order, looks at the NAL header to
    classify each unit, and accepts the first SPS (``nal_type == 7``)
    and the first PPS (``nal_type == 8``) whose payloads fit within
    ``max_sps_size`` / ``max_pps_size`` bytes and whose
    ``forbidden_zero_bit`` is 0.

    Parameters
    ----------
    annexb_data:
        Annex B H.264 byte stream.
    max_sps_size:
        Upper bound on the accepted SPS NAL size. Defaults to
        :data:`DEFAULT_MAX_SPS_SIZE`.
    max_pps_size:
        Upper bound on the accepted PPS NAL size. Defaults to
        :data:`DEFAULT_MAX_PPS_SIZE`.

    Returns
    -------
    SpsPpsResult
        On success.

    Raises
    ------
    SpsPpsNotFoundError
        If SPS, PPS, or both cannot be located. The exception carries
        ``sps_found`` and ``pps_found`` flags describing partial success.
    """
    sps: bytes | None = None
    pps: bytes | None = None
    sps_offset: int = -1
    pps_offset: int = -1

    pos = 0
    n = len(annexb_data)

    while pos < n and (sps is None or pps is None):
        sc_off, sc_len = find_next_start_code(annexb_data, pos)
        if sc_off == -1:
            break

        nal_start = sc_off + sc_len
        next_sc, _ = find_next_start_code(annexb_data, nal_start)
        nal_end = next_sc if next_sc != -1 else n

        nal_size = nal_end - nal_start
        if nal_size < 1:
            pos = nal_start + 1
            continue

        nh = annexb_data[nal_start]
        forbidden = (nh >> 7) & 1
        nal_type = nh & 0x1F

        if forbidden == 0:
            if nal_type == 7 and sps is None and nal_size <= max_sps_size:
                sps = annexb_data[nal_start:nal_end]
                sps_offset = sc_off
            elif nal_type == 8 and pps is None and nal_size <= max_pps_size:
                pps = annexb_data[nal_start:nal_end]
                pps_offset = sc_off

        pos = nal_end

    if sps is None or pps is None:
        raise SpsPpsNotFoundError(sps_found=sps is not None, pps_found=pps is not None)

    prefix = _START_CODE_4 + sps + _START_CODE_4 + pps

    return SpsPpsResult(
        sps_bytes=sps,
        pps_bytes=pps,
        sps_offset=sps_offset,
        pps_offset=pps_offset,
        prefix_annexb=prefix,
        source_size=n,
    )


# ---------------------------------------------------------------------------
# High-level: file in, file out
# ---------------------------------------------------------------------------


PathLike = Union[str, Path]


def extract_and_write_prefix(
    input_path: PathLike,
    output_path: PathLike,
    max_sps_size: int = DEFAULT_MAX_SPS_SIZE,
    max_pps_size: int = DEFAULT_MAX_PPS_SIZE,
) -> SpsPpsResult:
    """Read an Annex B file, extract SPS/PPS, write the prefix to disk.

    This is the function that the v0.1 thin CLI wrapper delegates to.
    The output file is always written with 4-byte start codes, matching
    the legacy ``scripts/extract_sps_pps.py`` byte-for-byte.

    Parameters
    ----------
    input_path:
        Path to an Annex B H.264 file (the "sane" reference, already
        converted from MOV by ``ffmpeg -bsf:v h264_mp4toannexb``).
    output_path:
        Path where the SPS/PPS prefix will be written.
    max_sps_size, max_pps_size:
        See :func:`extract_sps_pps_from_bytes`.

    Returns
    -------
    SpsPpsResult
        On success.

    Raises
    ------
    SpsPpsNotFoundError
        Propagated from :func:`extract_sps_pps_from_bytes`.
    OSError
        If either file cannot be opened.
    """
    in_path = Path(input_path)
    out_path = Path(output_path)

    data = in_path.read_bytes()
    result = extract_sps_pps_from_bytes(
        data,
        max_sps_size=max_sps_size,
        max_pps_size=max_pps_size,
    )
    out_path.write_bytes(result.prefix_annexb)
    return result


def extract_sps_pps(
    input_path: PathLike,
    output_path: PathLike,
    max_sps_size: int = DEFAULT_MAX_SPS_SIZE,
    max_pps_size: int = DEFAULT_MAX_PPS_SIZE,
) -> SpsPpsResult:
    """Primary public entry point. Alias of :func:`extract_and_write_prefix`.

    This is the name that ``orchestrator.py`` will use. Kept distinct
    from :func:`extract_and_write_prefix` so call sites can pick the
    name that best documents intent: ``extract_sps_pps`` reads as a
    domain operation, ``extract_and_write_prefix`` makes the side
    effect explicit.
    """
    return extract_and_write_prefix(
        input_path,
        output_path,
        max_sps_size=max_sps_size,
        max_pps_size=max_pps_size,
    )
