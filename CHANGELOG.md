# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) for
version identifiers.

## [Unreleased]

### Fixed
- The desktop GUI now enables "Show log" on the cancel path: the
  cancellation signal carries the recovery log path, so the log opens
  from a cancelled run just as it does on success and failure.

## [0.3.0a1] - 2026-06-01

Adds a functional desktop GUI (PySide6) over the v0.2 recovery engine. The
GUI produces output byte-identical to the v0.1 shell pipeline, verified by
an end-to-end run against the captured baseline.

### Added
- `mdt_rescue.gui` package: a single-window PySide6 desktop GUI with input,
  action, progress, and output zones (`python -m mdt_rescue.gui`, installed
  via the `[gui]` extra).
- `RecoveryWorker` (a `QThread`) runs `recover()` off the GUI thread,
  relaying progress, success, failure, and cancellation through Qt signals.
- Live progress display, cooperative cancellation, inline error reporting,
  and Reveal in Finder / Show log actions for the completed run.
- A forward-compatible profile selector (one entry for the validated
  GH5S FHD 25p ALL-I 200M profile).
- GUI smoke tests and `RecoveryWorker` unit tests (`pytest-qt`); the suite
  is now 214 passed, 1 skipped.
- Application screenshot in the README.

### Verified
- End-to-end: the GUI's recovery produced a rescued MOV byte-identical to
  the v0.1 baseline (and therefore to the legacy shell pipeline). The GUI,
  the Python smoke test, and the shell pipeline all yield bit-exact output.

### Known limitations
- On the cancel path, the "Show log" action stays disabled (the cancel
  signal carries only the stage name, not the log path). The success and
  failure paths enable it. To be addressed in a later release.

### Notes
- Requires Python 3.11+ and the `[gui]` extra (PySide6) for the GUI.
- The v0.1 shell pipeline and the v0.2 Python API remain unchanged.

## [0.2.0a1] - 2026-05-28

First alpha of the v0.2 line. The v0.1 shell pipeline is preserved unchanged
and remains the reference implementation; v0.2 adds a fully tested Python
layer that reproduces its output bit-exact.

### Added
- `mdt_rescue` Python package with a three-layer architecture: engine
  primitives, a recovery orchestrator, and (planned) a GUI layer.
- `mdt_rescue.engine` modules porting the v0.1 extraction logic into pure,
  testable functions: `sps_pps`, `video`, `audio`, and `verify`.
- `mdt_rescue.profiles` with frozen `Profile`, `VideoSpec`, `AudioSpec`, and
  `ExtractionPattern` dataclasses, plus the validated `GH5S_FHD25_ALLI_200M`
  profile.
- `mdt_rescue.orchestrator.recover()` public API that composes the engine
  primitives into a full recovery run, with progress callbacks, a cancel
  token, and a structured `RecoveryResult`.
- Thin CLI wrappers in `scripts/` delegating to the engine while preserving
  the v0.1 command-line behavior.
- End-to-end smoke test (`tests/test_smoke_e2e.py`) that runs the real
  `recover()` pipeline on actual input files and asserts the produced
  artifacts are byte-identical to the captured v0.1 baseline. Gated behind
  the `slow` marker and the `MDT_RUN_SMOKE=1` environment variable.
- Captured v0.1 deep smoke baseline under `tests/baselines/v0.1-smoke/`.

### Fixed
- Final audio/video mux now produces a MOV containing both streams. The
  orchestrator's mux step previously passed the raw PCM file to ffmpeg
  without format hints, which caused ffmpeg to misdetect the input and
  silently drop the audio stream, yielding a video-only "rescued" file.
  The mux command was realigned with the validated v0.1 invocation (mux
  from the WAV file, explicit stream mapping, video track timescale, and
  shortest-stream truncation). Detected by the new end-to-end smoke test.

### Notes
- The v0.1 shell orchestrator (`recover_gh5s_fhd25_alli.sh`) and its
  reference scripts remain in place and unchanged.
- The Python `recover()` path and the shell path produce byte-identical
  output on all eight deterministic artifacts for the validated smoke
  baseline.
- Python 3.11+ is now required for the Python API (uses `StrEnum`).

## [0.1.0] - 2026

Initial release, as shipped.

### Added
- Shell-based recovery pipeline (`recover_gh5s_fhd25_alli.sh`) for
  unfinalized Panasonic GH5S `.MDT` files in the FHD 25p ALL-I 200M profile.
- Standalone extraction scripts for video NAL units, audio chunks, and
  SPS/PPS prefix.
- ffprobe-based output verification.
- Validated end-to-end on a real 33 GB `.MDT` file, producing a clean MOV
  byte-for-byte equivalent to a finalized camera recording.

[0.3.0a1]: https://github.com/alicchiog/mdt-rescue-toolkit/releases/tag/v0.3.0-alpha.1
[0.2.0a1]: https://github.com/alicchiog/mdt-rescue-toolkit/releases/tag/v0.2.0-alpha.1
[0.1.0]: https://github.com/alicchiog/mdt-rescue-toolkit/releases/tag/v0.1.0
