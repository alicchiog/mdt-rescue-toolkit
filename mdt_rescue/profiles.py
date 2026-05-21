"""
mdt_rescue.profiles
===================

Recovery profile definitions.

A "profile" bundles the camera-specific binary layout knowledge required
to extract video and audio from an unfinalized .MDT file. It is read by
the engine primitives and the orchestrator to make their behaviour
configurable across camera bodies, framerates, and bitrate modes.

Planned content (Phase B/C):

- ``Profile`` dataclass with fields like ``name``, ``video_codec``,
  ``video_resolution``, ``video_framerate``, ``audio_codec``,
  ``audio_chunk_size``, ``audio_chunk_interval_frames``, ``aud_anchor``,
  etc.
- ``GH5S_FHD25_ALLI_200M`` — the one validated profile carried forward
  from v0.1.0
- (later) additional profiles contributed by users with real test files

At v0.2.0.dev0 this module is an empty placeholder. The single hard-coded
profile parameters are still embedded in the v0.1.0 scripts/ files and
will be extracted into this module as part of Phase B.
"""

__all__ = []
