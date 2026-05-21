"""
mdt_rescue.engine
=================

Low-level extraction primitives for .MDT file recovery.

This subpackage will host the migrated logic from the v0.1.0 ``scripts/``
directory, refactored as pure, importable Python functions:

- ``extract_sps_pps`` — parse SPS/PPS NAL units from a healthy reference MOV
- ``extract_video`` — extract H.264 access units from a .MDT, streaming
- ``extract_audio`` — extract PCM s16be chunks from a .MDT with silence
  injection for structural anomalies
- ``ffmpeg_wrappers`` — thin Python wrappers around ffmpeg/ffprobe subprocess
- ``verify`` — final ffprobe-based validation of the recovered MOV

At v0.2.0.dev0 this subpackage is intentionally empty. Each component will
be added in Phase B as a small, independent refactor with its own commit
and tests.
"""

__all__ = []
