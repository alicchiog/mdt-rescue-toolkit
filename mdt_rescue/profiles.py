"""
mdt_rescue.profiles
===================

Recovery profile definitions.

A "profile" bundles the camera-specific binary layout knowledge required
to extract video and audio from an unfinalized .MDT file. It is read by
the engine primitives and the orchestrator (Phase B.6+) to make their
behaviour configurable across camera bodies, framerates, and bitrate
modes.

Phase B.5 introduces:

- :class:`VideoSpec` -- video stream parameters (codec, geometry, fps).
- :class:`AudioSpec` -- audio stream parameters (codec, sample rate,
  channel count, sample format).
- :class:`ExtractionPattern` -- MDT-specific binary layout knowledge
  (AUD anchor, timecode size, audio chunk cadence).
- :class:`Profile` -- top-level recovery profile composing the three.
- :data:`GH5S_FHD25_ALLI_200M` -- the single validated profile carried
  forward from v0.1.0.

The :attr:`Profile.verify_expectations` property bridges the semantic
profile values to the legacy string-based ``PlausibilityExpectations``
used by :func:`mdt_rescue.engine.verify.verify_mov`. The verify engine
itself is unchanged in B.5; a future phase may collapse the two.

Decoupling note
---------------
The ``PlausibilityExpectations`` import is performed *lazily* inside
the body of :attr:`Profile.verify_expectations` (and mirrored at type
checking time via :data:`TYPE_CHECKING`), so that
``import mdt_rescue.profiles`` does not trigger any
``mdt_rescue.engine`` import side effects at module load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mdt_rescue.engine.verify import PlausibilityExpectations


__all__ = [
    "VideoSpec",
    "AudioSpec",
    "ExtractionPattern",
    "Profile",
    "GH5S_FHD25_ALLI_200M",
]


@dataclass(frozen=True)
class VideoSpec:
    """Video stream parameters of a recording profile.

    All fields use native Python types (``int`` for geometry, ``float``
    for fps, ``str`` for codec identifiers). String-typed verify
    expectations are produced separately by
    :attr:`Profile.verify_expectations`.

    ``bitrate_mbps`` and ``codec_profile`` are *documentary* fields:
    they describe the profile but are not verified against the rescued
    MOV in v0.2 (no ffprobe-based check for them). Adding ffprobe
    checks for fps / codec_profile / bitrate is a deferred follow-up
    (B.6/B.7).
    """

    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    bitrate_mbps: int
    codec_profile: str


@dataclass(frozen=True)
class AudioSpec:
    """Audio stream parameters of a recording profile.

    ``sample_format`` and ``bytes_per_sample`` are technically
    derivable from ``codec`` (e.g. ``pcm_s16be`` implies ``s16be`` and
    2 bytes per sample), but are kept as explicit fields in v0.2 to
    avoid introducing a codec lookup table for a single-profile
    codebase.
    """

    codec: str
    sample_rate: int
    channels: int
    sample_format: str
    bytes_per_sample: int


@dataclass(frozen=True)
class ExtractionPattern:
    """MDT-specific binary layout knowledge required by extraction.

    These three values are the irreducible parsing knowledge for the
    GH5S MDT format:

    - ``aud_anchor`` -- 6-byte pattern marking the start of each video
      access unit (length-prefix 2 + AUD NAL header 0x09 + payload
      0x10). Duplicated byte-identically today in
      :mod:`mdt_rescue.engine.video` and
      :mod:`mdt_rescue.engine.audio`; profile-driven extraction
      (Phase B.6+) will consume this single source instead.
    - ``timecode_size`` -- size in bytes of the trailing timecode
      block after each audio chunk in the MDT (48 for GH5S).
    - ``audio_chunk_interval_frames`` -- how many video frames span
      one audio chunk in the GH5S MDT stream (12 for GH5S FHD25).

    See :attr:`Profile.expected_audio_payload` and
    :attr:`Profile.expected_gap_size` for the derived chunk
    arithmetic.
    """

    aud_anchor: bytes
    timecode_size: int
    audio_chunk_interval_frames: int


@dataclass(frozen=True)
class Profile:
    """Top-level recovery profile.

    Composes video, audio and extraction parameters plus identity
    metadata. Derived properties expose the arithmetic relationships
    between profile fields that today live as hardcoded constants in
    :mod:`mdt_rescue.engine.audio`; a regression test in
    ``tests/test_profiles.py`` locks the two sources together until a
    future phase removes the duplication.

    Parameters
    ----------
    name:
        Canonical machine-readable identifier (e.g.
        ``"GH5S_FHD25_ALLI_200M"``). Used for logging and as a
        registry key when multiple profiles will coexist (v0.3+).
    label:
        Human-readable label suitable for UI display.
    camera:
        Camera model string.
    video, audio, extraction:
        Nested sub-dataclasses; see their docstrings.
    """

    name: str
    label: str
    camera: str
    video: VideoSpec
    audio: AudioSpec
    extraction: ExtractionPattern

    @property
    def audio_bytes_per_second(self) -> int:
        """Bytes of raw audio per second.

        Equals ``sample_rate * channels * bytes_per_sample``. For the
        GH5S FHD25 profile this evaluates to ``192000``, matching
        :data:`mdt_rescue.engine.audio.AUDIO_BYTES_PER_SECOND`.
        """
        return (
            self.audio.sample_rate
            * self.audio.channels
            * self.audio.bytes_per_sample
        )

    @property
    def expected_audio_payload(self) -> int:
        """Bytes of PCM audio per MDT audio chunk.

        Equals ``round(audio_bytes_per_second *
        (audio_chunk_interval_frames / video.fps))``. ``round`` (banker's
        rounding) is used instead of ``int`` (floor) to avoid a silent
        rounding-down on fractional-fps profiles a future release may
        add (e.g. 29.97); for the integer-fps GH5S FHD25 profile both
        yield exactly ``92160``, matching
        :data:`mdt_rescue.engine.audio.EXPECTED_AUDIO_PAYLOAD`.
        """
        chunk_seconds = (
            self.extraction.audio_chunk_interval_frames / self.video.fps
        )
        return round(self.audio_bytes_per_second * chunk_seconds)

    @property
    def expected_gap_size(self) -> int:
        """Total bytes of an MDT audio gap (payload + timecode).

        Equals ``expected_audio_payload + extraction.timecode_size``.
        For the GH5S FHD25 profile this evaluates to ``92208``,
        matching :data:`mdt_rescue.engine.audio.EXPECTED_GAP_SIZE`.
        """
        return self.expected_audio_payload + self.extraction.timecode_size

    @property
    def verify_expectations(self) -> PlausibilityExpectations:
        """String-typed expectations for :func:`verify_mov`.

        Converts the semantically-typed profile fields to the legacy
        all-string ``PlausibilityExpectations`` shape expected by the
        verify engine, which compares them directly against ffprobe
        output (also string-typed). Equivalent to
        :data:`mdt_rescue.engine.verify.EXPECTATIONS_GH5S_FHD25` when
        invoked on :data:`GH5S_FHD25_ALLI_200M`.

        The ``PlausibilityExpectations`` import is local to this
        property body to keep :mod:`mdt_rescue.profiles` decoupled
        from :mod:`mdt_rescue.engine.verify` at import time.
        """
        from mdt_rescue.engine.verify import PlausibilityExpectations

        return PlausibilityExpectations(
            video_codec=self.video.codec,
            width=str(self.video.width),
            height=str(self.video.height),
            pix_fmt=self.video.pix_fmt,
            audio_codec=self.audio.codec,
            sample_rate=str(self.audio.sample_rate),
            channels=str(self.audio.channels),
        )


GH5S_FHD25_ALLI_200M: Profile = Profile(
    name="GH5S_FHD25_ALLI_200M",
    label="Panasonic GH5S - FHD 1920x1080 25p ALL-Intra 200 Mbps",
    camera="Panasonic DC-GH5S",
    video=VideoSpec(
        codec="h264",
        width=1920,
        height=1080,
        fps=25.0,
        pix_fmt="yuv422p10le",
        bitrate_mbps=200,
        codec_profile="H.264 High 4:2:2 Intra",
    ),
    audio=AudioSpec(
        codec="pcm_s16be",
        sample_rate=48000,
        channels=2,
        sample_format="s16be",
        bytes_per_sample=2,
    ),
    extraction=ExtractionPattern(
        aud_anchor=b"\x00\x00\x00\x02\x09\x10",
        timecode_size=48,
        audio_chunk_interval_frames=12,
    ),
)
"""The single validated recovery profile carried forward from v0.1.0.

Validated end-to-end on a real 33 GB Panasonic GH5S .MDT file
(P1194247.mdt) in v0.1.0; all extraction constants in
:mod:`mdt_rescue.engine.audio` match the values in this profile.
"""
