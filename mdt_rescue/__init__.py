"""
mdt_rescue
==========

Profile-based recovery toolkit for unfinalized Panasonic .MDT video files.

This package will eventually contain three architectural layers:

- :mod:`mdt_rescue.engine` — low-level extraction primitives (video, audio,
  SPS/PPS, ffmpeg wrappers, output verification). Pure Python functions,
  no I/O coupling, importable as a library.
- :mod:`mdt_rescue.orchestrator` — high-level recovery driver. Composes the
  engine primitives into a full ``recover()`` call with progress callbacks
  and a cancel token. Usable from both CLI and GUI.
- :mod:`mdt_rescue.gui` — optional PySide6 GUI layer (installed via the
  ``[gui]`` extra). Thin presentation layer over the orchestrator.

As of v0.2.0a1 the engine primitives and orchestrator are implemented
and validated bit-exact against the v0.1 shell pipeline.  The v0.1
shell orchestrator (recover_gh5s_fhd25_alli.sh) remains in the
repository as the reference implementation and is also fully supported.
The GUI layer is not yet implemented and will follow in a later phase.

Supported recovery profile (v0.1.0, frozen):
    Panasonic DC-GH5S, FHD 1920x1080, 25 fps PAL, AVC-Intra 200 Mbps,
    H.264 High 4:2:2 Intra, yuv422p10le, PCM s16be stereo 48 kHz.

License: MIT
Project: https://github.com/alicchiog/mdt-rescue-toolkit
"""

__version__ = "0.2.0a1"
__author__ = "Gianpiero Alicchio"
__license__ = "MIT"

# Public re-exports will be added here in Phase B/C as the engine and
# orchestrator modules become available. For now we intentionally export
# only the version metadata, so that `import mdt_rescue` succeeds without
# pulling in any unimplemented submodules.

__all__ = [
    "__version__",
    "__author__",
    "__license__",
]
