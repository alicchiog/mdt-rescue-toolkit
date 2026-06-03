# Packaging

Build the MDT Rescue Toolkit desktop GUI into a self-contained macOS `.app`
with PyInstaller, bundling LGPL `ffmpeg`/`ffprobe` so no system ffmpeg is
required.

## Build

From the repo root, with the bundled binaries already in `packaging/bin/`
(see "Bundled ffmpeg/ffprobe" below):

```sh
pip install -e ".[packaging]"
pyinstaller packaging/mdt_rescue_gui.spec
```

Output: `dist/MDT Rescue Toolkit.app` (plus PyInstaller's `build/` working
directory). `build/`, `dist/`, and `packaging/bin/` are gitignored — build
artifacts and the large binaries are never committed. Building from the
explicit spec does not emit a stray top-level `.spec`.

## Bundled ffmpeg/ffprobe (build from source, LGPL)

The `.app` bundles `ffmpeg` and `ffprobe` at the freeze root, where the
runtime seam (`mdt_rescue.runtime.resolve_ffmpeg_binary`) finds them as
`<sys._MEIPASS>/<tool>` in the frozen app. The binaries are **not** committed
(~23 MB each); build them from source and place them in `packaging/bin/`
before running PyInstaller.

**Version match matters.** Recovery output is validated byte-for-byte against
a baseline, and the MOV muxer embeds the libavformat version, so the bundled
ffmpeg must be the **same version** as the baseline-producing one (currently
**8.1.1**). GPL vs LGPL does not affect the output — the pipeline only
copies/remuxes video and writes PCM audio, never encoding H.264 — but the
version does.

Build (LGPL, no GPL / no libx264; system-only deps for clean-Mac portability):

```sh
curl -fL -o ffmpeg-8.1.1.tar.xz https://ffmpeg.org/releases/ffmpeg-8.1.1.tar.xz
tar xf ffmpeg-8.1.1.tar.xz && cd ffmpeg-8.1.1
./configure --cc=clang --arch=x86_64 \
  --disable-gpl --disable-nonfree \
  --disable-shared --enable-static \
  --disable-doc --disable-ffplay --disable-x86asm \
  --disable-xlib --disable-libxcb --disable-lzma \
  --prefix="$PWD/_install"
make -j"$(sysctl -n hw.ncpu)"
cp ffmpeg ffprobe /path/to/repo/packaging/bin/
```

Verify before bundling:
- `packaging/bin/ffmpeg -version` → `ffmpeg version 8.1.1`, and the
  `configuration` line shows none of `--enable-gpl` / `--enable-libx264` /
  `--enable-nonfree`.
- `otool -L packaging/bin/ffmpeg` (and `ffprobe`) → only
  `/System/Library/Frameworks/*` and `/usr/lib/*` deps (no `/usr/local/...`),
  so the `.app` runs on a clean Mac.

The bundled binaries stay under their own LGPL v2.1 license; see
`packaging/licenses/COPYING.LGPLv2.1` and `packaging/licenses/NOTICE.txt`
(both shipped inside the `.app` under `licenses/`).

## Scope / phases

P.3 delivers the self-contained `.app` (bundled ffmpeg, frozen-app recovery
byte-identical to the baseline), current-architecture (x86_64). The
Universal2 build and the unsigned `.dmg` with the Gatekeeper first-launch UX
are the P.4 release target.

See [`docs/packaging_design.md`](../docs/packaging_design.md) for the full
design and the P.1–P.4 phase plan.
