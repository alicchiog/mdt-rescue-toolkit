"""
mdt_rescue.runtime
==================

Runtime resolution of the external ffmpeg/ffprobe executables.

This is the single seam (designed in docs/packaging_design.md §4) that lets
the packaged macOS app use bundled binaries while development keeps using
the binaries on ``PATH``:

- **Frozen** (a packaged app; ``sys.frozen`` is truthy): return the absolute
  path to the binary bundled at the freeze root (``sys._MEIPASS`` under
  PyInstaller). The bundle layout -- ``<_MEIPASS>/<tool>`` -- is a CONTRACT
  with the packaging step (P.3 ``--add-binary``): P.3 must place ``ffmpeg``
  and ``ffprobe`` at the freeze root so this resolves correctly.
- **Development** (not frozen): return the bare tool name so the OS resolves
  it via ``PATH`` at spawn time, exactly as before this seam existed.
  Returning the bare name (not an absolute ``shutil.which`` path) keeps
  ``argv[0]`` stable and dev behavior byte-identical.
"""

from __future__ import annotations

import os
import sys

_ALLOWED_TOOLS = ("ffmpeg", "ffprobe")


def resolve_ffmpeg_binary(tool: str) -> str:
    """Return the executable to invoke for ``tool`` ("ffmpeg" or "ffprobe").

    Frozen build: absolute path ``<sys._MEIPASS>/<tool>``. Development: the
    bare ``tool`` name, resolved via ``PATH`` at subprocess spawn.

    Raises
    ------
    ValueError
        If ``tool`` is not ``"ffmpeg"`` or ``"ffprobe"``.
    RuntimeError
        If the build is frozen but ``sys._MEIPASS`` is absent.
    """
    if tool not in _ALLOWED_TOOLS:
        raise ValueError(
            f"resolve_ffmpeg_binary: unsupported tool {tool!r}; "
            f"expected one of {_ALLOWED_TOOLS}"
        )

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is None:
            raise RuntimeError(
                "Frozen build is missing sys._MEIPASS; cannot locate the "
                f"bundled {tool!r} binary."
            )
        return os.path.join(meipass, tool)

    return tool
