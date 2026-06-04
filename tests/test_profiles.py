"""Atomic tests for :mod:`mdt_rescue.profiles`.

Verifies, in this order of priority:

- Concrete profile values for the GH5S FHD25 ALL-I 200 Mbps profile
  match the v0.1 validated values (**P** category).
- Extraction pattern constants match the duplicated constants in
  :mod:`mdt_rescue.engine.audio` (**E** category).
- Derived properties reproduce the hardcoded constants in
  :mod:`mdt_rescue.engine.audio` (**D** category).
- The ``verify_expectations`` property equals
  :data:`mdt_rescue.engine.verify.EXPECTATIONS_GH5S_FHD25`, locking
  backward compatibility with the verify engine (**V** category).
- All four dataclasses are frozen.
- The module's public surface is the expected set.

Test count: 24 atomic tests in 7 classes.
"""

from __future__ import annotations

import dataclasses

import pytest

from mdt_rescue.engine.audio import (
    AUD_ANCHOR as ENGINE_AUDIO_AUD_ANCHOR,
    AUDIO_BYTES_PER_SECOND as ENGINE_AUDIO_BPS,
    EXPECTED_AUDIO_PAYLOAD as ENGINE_AUDIO_PAYLOAD,
    EXPECTED_GAP_SIZE as ENGINE_AUDIO_GAP,
    TIMECODE_SIZE as ENGINE_TIMECODE_SIZE,
)
from mdt_rescue.engine.video import (
    AUD_ANCHOR as ENGINE_VIDEO_AUD_ANCHOR,
)
from mdt_rescue.engine.verify import (
    EXPECTATIONS_GH5S_FHD25,
    PlausibilityExpectations,
)
from mdt_rescue.profiles import (
    AudioSpec,
    ExtractionPattern,
    GH5S_FHD25_ALLI_200M,
    Profile,
    VideoSpec,
)


class TestGH5SProfileVideoValues:
    """P category: GH5S FHD25 video parameters."""

    def test_video_codec_is_h264(self):
        assert GH5S_FHD25_ALLI_200M.video.codec == "h264"

    def test_video_geometry_is_1920x1080(self):
        assert GH5S_FHD25_ALLI_200M.video.width == 1920
        assert GH5S_FHD25_ALLI_200M.video.height == 1080

    def test_video_fps_is_25(self):
        assert GH5S_FHD25_ALLI_200M.video.fps == 25.0

    def test_video_pix_fmt_bitrate_and_codec_profile(self):
        v = GH5S_FHD25_ALLI_200M.video
        assert v.pix_fmt == "yuv422p10le"
        assert v.bitrate_mbps == 200
        assert v.codec_profile == "H.264 High 4:2:2 Intra"


class TestGH5SProfileAudioValues:
    """P category: GH5S FHD25 audio parameters."""

    def test_audio_codec_is_pcm_s16be(self):
        assert GH5S_FHD25_ALLI_200M.audio.codec == "pcm_s16be"

    def test_audio_sample_rate_is_48000_stereo(self):
        a = GH5S_FHD25_ALLI_200M.audio
        assert a.sample_rate == 48000
        assert a.channels == 2

    def test_audio_sample_format_s16be_2_bytes(self):
        a = GH5S_FHD25_ALLI_200M.audio
        assert a.sample_format == "s16be"
        assert a.bytes_per_sample == 2


class TestGH5SExtractionPattern:
    """E.extraction-pattern: extraction parameters match engine.audio."""

    def test_aud_anchor_matches_audio_engine_constant(self):
        assert (
            GH5S_FHD25_ALLI_200M.extraction.aud_anchor
            == ENGINE_AUDIO_AUD_ANCHOR
        )
        assert (
            GH5S_FHD25_ALLI_200M.extraction.aud_anchor
            == b"\x00\x00\x00\x02\x09\x10"
        )

    def test_timecode_size_matches_audio_engine_constant(self):
        assert (
            GH5S_FHD25_ALLI_200M.extraction.timecode_size
            == ENGINE_TIMECODE_SIZE
        )
        assert GH5S_FHD25_ALLI_200M.extraction.timecode_size == 48

    def test_audio_chunk_interval_frames_is_12(self):
        assert (
            GH5S_FHD25_ALLI_200M.extraction.audio_chunk_interval_frames
            == 12
        )


class TestAudAnchorCrossConsistency:
    """E.extraction-pattern: the AUD anchor is duplicated in three places
    (profiles.py, engine.audio, engine.video). The profile<->engine.audio
    pair is already locked above; this closes the previously unguarded
    third copy in engine.video. Pure consistency guard -- it changes no
    engine behavior. ``==`` is correct here (comparing ``bytes``).
    """

    def test_audio_engine_anchor_matches_profile(self):
        assert (
            ENGINE_AUDIO_AUD_ANCHOR
            == GH5S_FHD25_ALLI_200M.extraction.aud_anchor
        )

    def test_video_engine_anchor_matches_profile(self):
        assert (
            ENGINE_VIDEO_AUD_ANCHOR
            == GH5S_FHD25_ALLI_200M.extraction.aud_anchor
        )

    def test_audio_and_video_engine_anchors_match(self):
        assert ENGINE_AUDIO_AUD_ANCHOR == ENGINE_VIDEO_AUD_ANCHOR


class TestDerivedProperties:
    """D category: derived properties match engine.audio constants.

    These tests are the central regression guard for B.5: they lock the
    derived properties in :class:`Profile` to the hardcoded constants in
    :mod:`mdt_rescue.engine.audio`. If either side is modified without
    the other, these tests fail.
    """

    def test_audio_bytes_per_second_matches_audio_engine_constant(self):
        assert (
            GH5S_FHD25_ALLI_200M.audio_bytes_per_second == ENGINE_AUDIO_BPS
        )
        assert GH5S_FHD25_ALLI_200M.audio_bytes_per_second == 192000

    def test_expected_audio_payload_matches_audio_engine_constant(self):
        assert (
            GH5S_FHD25_ALLI_200M.expected_audio_payload
            == ENGINE_AUDIO_PAYLOAD
        )
        assert GH5S_FHD25_ALLI_200M.expected_audio_payload == 92160

    def test_expected_gap_size_matches_audio_engine_constant(self):
        assert GH5S_FHD25_ALLI_200M.expected_gap_size == ENGINE_AUDIO_GAP
        assert GH5S_FHD25_ALLI_200M.expected_gap_size == 92208


class TestVerifyExpectationsConsistency:
    """V category: verify_expectations equals legacy constant."""

    def test_verify_expectations_returns_PlausibilityExpectations_instance(
        self,
    ):
        assert isinstance(
            GH5S_FHD25_ALLI_200M.verify_expectations,
            PlausibilityExpectations,
        )

    def test_verify_expectations_equals_EXPECTATIONS_GH5S_FHD25(self):
        assert (
            GH5S_FHD25_ALLI_200M.verify_expectations
            == EXPECTATIONS_GH5S_FHD25
        )

    def test_verify_expectations_all_fields_are_str(self):
        ve = GH5S_FHD25_ALLI_200M.verify_expectations
        for f in dataclasses.fields(ve):
            value = getattr(ve, f.name)
            assert isinstance(value, str), (
                f"PlausibilityExpectations.{f.name} should be str, "
                f"got {type(value).__name__}"
            )


class TestFrozenInvariant:
    """Structural: all four dataclasses are frozen."""

    def test_videospec_is_frozen(self):
        vs = GH5S_FHD25_ALLI_200M.video
        with pytest.raises(dataclasses.FrozenInstanceError):
            vs.width = 1280

    def test_audiospec_is_frozen(self):
        a = GH5S_FHD25_ALLI_200M.audio
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.sample_rate = 44100

    def test_extractionpattern_is_frozen(self):
        e = GH5S_FHD25_ALLI_200M.extraction
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.timecode_size = 32

    def test_profile_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            GH5S_FHD25_ALLI_200M.name = "OTHER"


class TestModuleSurface:
    """Structural: module exports the expected public API."""

    def test_module_exports_Profile_class(self):
        from mdt_rescue import profiles

        assert hasattr(profiles, "Profile")
        assert profiles.Profile is Profile

    def test_module_exports_GH5S_FHD25_ALLI_200M_constant(self):
        from mdt_rescue import profiles

        assert hasattr(profiles, "GH5S_FHD25_ALLI_200M")
        assert profiles.GH5S_FHD25_ALLI_200M is GH5S_FHD25_ALLI_200M

    def test_GH5S_FHD25_ALLI_200M_is_Profile_instance(self):
        assert isinstance(GH5S_FHD25_ALLI_200M, Profile)
        assert isinstance(GH5S_FHD25_ALLI_200M.video, VideoSpec)
        assert isinstance(GH5S_FHD25_ALLI_200M.audio, AudioSpec)
        assert isinstance(
            GH5S_FHD25_ALLI_200M.extraction, ExtractionPattern
        )

    def test_module_all_matches_actual_exports(self):
        from mdt_rescue import profiles

        expected = {
            "VideoSpec",
            "AudioSpec",
            "ExtractionPattern",
            "Profile",
            "GH5S_FHD25_ALLI_200M",
            "DEFAULT_PROFILE",
            "PROFILES",
            "get_profile",
            "UnknownProfileError",
        }
        assert set(profiles.__all__) == expected
