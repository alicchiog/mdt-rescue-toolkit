# Packaging

Build the MDT Rescue Toolkit desktop GUI into a macOS `.app` with PyInstaller.

## Build (current architecture)

From the repo root:

```sh
pip install -e ".[packaging]"
pyinstaller packaging/mdt_rescue_gui.spec
```

Output: `dist/MDT Rescue Toolkit.app` (plus PyInstaller's `build/` working
directory). Both `build/` and `dist/` are gitignored — build artifacts are
never committed. Building from the explicit spec does not emit a stray
top-level `.spec`.

## Scope (P.2)

This produces a **current-architecture** app that launches and renders the
GUI. It does **not** bundle `ffmpeg`/`ffprobe` yet — that is P.3.

Running a real recovery from the frozen P.2 app is out of scope; because
`ffmpeg`/`ffprobe` are not bundled yet, recovery is expected to fail
preflight. P.3 owns bundled `ffmpeg`/`ffprobe` and frozen-app recovery e2e.
The Universal2 build and the unsigned `.dmg` (with the Gatekeeper
first-launch UX) are the P.4 release target.

See [`docs/packaging_design.md`](../docs/packaging_design.md) for the full
design and the P.1–P.4 phase plan.
