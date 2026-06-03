# packaging/mdt_rescue_gui.spec
#
# PyInstaller spec for the MDT Rescue Toolkit desktop GUI.
#
# Produces a current-architecture .app with bundled ffmpeg/ffprobe that
# recovers a file end-to-end with no system ffmpeg required.
#
# P.3 bundles LGPL ffmpeg/ffprobe (built from source, no GPL / no libx264;
# see packaging/licenses/NOTICE.txt) at the freeze root, matching the seam
# contract mdt_rescue.runtime.resolve_ffmpeg_binary -> <sys._MEIPASS>/<tool>.
# The binaries must exist at packaging/bin/ before building (built per
# packaging/README.md; gitignored, never committed).
#
# Build (from the repo root):
#     pip install -e ".[packaging]"
#     pyinstaller packaging/mdt_rescue_gui.spec
#
# Output: dist/MDT Rescue Toolkit.app  (build/ and dist/ are gitignored)
#
# Architecture: current arch only (target_arch=None). Universal2 is the
# P.4 release target.

import os

from mdt_rescue import __version__ as APP_VERSION

REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ENTRY = os.path.join(REPO_ROOT, "mdt_rescue", "gui", "__main__.py")
BIN = os.path.join(SPECPATH, "bin")           # built ffmpeg/ffprobe (gitignored)
LIC = os.path.join(SPECPATH, "licenses")      # LGPL text + NOTICE (committed)
ICON = os.path.join(SPECPATH, "assets", "app_icon.icns")  # app icon (committed)

a = Analysis(
    [ENTRY],
    pathex=[REPO_ROOT],
    binaries=[
        # Bundle at the freeze root (dest ".") so resolve_ffmpeg_binary()
        # finds them at <sys._MEIPASS>/<tool> in the frozen app.
        (os.path.join(BIN, "ffmpeg"), "."),
        (os.path.join(BIN, "ffprobe"), "."),
    ],
    datas=[
        (os.path.join(LIC, "COPYING.LGPLv2.1"), "licenses"),
        (os.path.join(LIC, "NOTICE.txt"), "licenses"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MDT Rescue Toolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed GUI app (no terminal)
    disable_windowed_traceback=False,
    argv_emulation=False,     # not needed (Browse buttons, not Finder-open)
    target_arch=None,         # current arch (x86_64 here); Universal2 is P.4
    codesign_identity=None,   # unsigned (P.4 documents Gatekeeper UX)
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MDT Rescue Toolkit",
)

app = BUNDLE(
    coll,
    name="MDT Rescue Toolkit.app",
    icon=ICON,                # placeholder app icon (packaging/assets/app_icon.icns)
    bundle_identifier="com.alicchiog.mdt-rescue-toolkit",
    version=APP_VERSION,
)
