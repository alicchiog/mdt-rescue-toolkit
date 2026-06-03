# packaging/mdt_rescue_gui.spec
#
# PyInstaller spec for the MDT Rescue Toolkit desktop GUI (P.2).
#
# Produces a current-architecture .app that launches and renders the GUI.
# Per docs/packaging_design.md §8, P.2's gate is "the .app launches and
# shows the GUI" -- NOT an end-to-end recovery.
#
# ffmpeg/ffprobe are intentionally NOT bundled here; that is P.3. Running a
# real recovery from the frozen P.2 app is out of scope; because
# ffmpeg/ffprobe are not bundled yet, recovery is expected to fail preflight.
# P.3 owns bundled ffmpeg/ffprobe and frozen-app recovery e2e.
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

a = Analysis(
    [ENTRY],
    pathex=[REPO_ROOT],
    binaries=[],          # no ffmpeg/ffprobe yet -- that is P.3
    datas=[],
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
    icon=None,                # app icon is a later polish item
    bundle_identifier="com.alicchiog.mdt-rescue-toolkit",
    version=APP_VERSION,
)
