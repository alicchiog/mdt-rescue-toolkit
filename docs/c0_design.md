# C.0 Design — Desktop GUI lightning specification

**Status:** approved (C.0.1 complete, design-frozen for C.0.2)
**Target release:** `v0.3.0-alpha.1`
**Framework:** PySide6
**Distribution:** runs from source on macOS (`python -m mdt_rescue.gui`). Packaging to `.app` deferred to a later phase.

This document is the single source of truth for the layout, behavior, and
threading model of the C.0 GUI. It is intentionally short: implementation
details belong in code, not here. If during implementation a divergence
from this document appears necessary, the document is updated first, then
the code follows.

---

## 1. Layout

A single non-resizable-too-small main window. Four vertical zones, top to
bottom:

```
┌──────────────────────────────────────────────────────┐
│  MDT Rescue Toolkit                       v0.3.0a1   │  (title bar)
├──────────────────────────────────────────────────────┤
│  Profile:  [ GH5S FHD 25p ALL-I 200M       ▾ ]       │  Input zone
│                                                       │
│  Broken .MDT file:                                    │
│    [ /path/to/broken.mdt        ]  [ Browse... ]      │
│                                                       │
│  Reference .MOV file:                                 │
│    [ /path/to/sane_reference.mov ]  [ Browse... ]     │
├──────────────────────────────────────────────────────┤
│              [   Recover   ]    [  Cancel  ]          │  Action zone
├──────────────────────────────────────────────────────┤
│  Progress: STEP 3/7 — Concatenating prefix...         │  Progress zone
│  ████████████░░░░░░░░░░░░░░  43%                      │
├──────────────────────────────────────────────────────┤
│  Output                                               │  Output zone
│  ─────────────────────────────────────────────────    │
│  ✓ Rescued: /tmp/.../test_100mb_RESCUED.mov           │
│  [ Reveal in Finder ]  [ Show log ]                   │
└──────────────────────────────────────────────────────┘
```

Zones are stacked using `QVBoxLayout` with sensible spacers between them.
Minimum window size approximately 640×480.

## 2. Behavior

### 2.1 Profile selector (D2 = b)

`QComboBox` populated from the available `Profile` instances exported by
`mdt_rescue.profiles`. For v0.3.0a1 only `GH5S_FHD25_ALLI_200M` is present,
so the combo box has one entry and is effectively read-only. The widget is
present in the UI as forward-compatible scaffolding; adding a second
profile later means adding one entry to the list, no layout change.

### 2.2 File pickers

Two `QLineEdit` + `QPushButton("Browse...")` pairs. Browse opens
`QFileDialog.getOpenFileName` with filters: `.mdt` for the broken file,
`.mov`/`.mp4`/`.MOV` for the reference. Editing the path manually is
allowed.

### 2.3 Recover button

Enabled iff both paths are non-empty and the referenced files exist on
disk. Click → start recovery worker thread (see §3). While the worker is
running, the Recover button is disabled and the Cancel button is enabled.

### 2.4 Output directory (D3 = a)

Computed automatically from the .mdt path:
`<parent dir of .mdt>/recovery_output_<mdt stem>/`. The user is not asked.
This matches the v0.1 shell pipeline behavior and the
`recover()` default. The chosen directory is shown in the Output zone
once recovery starts.

### 2.5 Cancel button (D4 = c)

Enabled only while a recovery is running. Click → `CancelToken.set()` on
the worker. No confirmation dialog. The orchestrator is cooperative: it
stops at the next stage boundary (typically within 1–2 seconds). Partial
files are left in place in the output directory for inspection.

### 2.6 Progress display

Two widgets in the progress zone:
- A `QLabel` showing the current stage (e.g. `STEP 3/7 — Concatenating
  prefix…`), updated on every `ProgressEvent` with `status == START`.
- A `QProgressBar` whose value advances by completed-stages over total
  stages. With 11 total Stage values (4 preflight + 7 functional), each
  COMPLETE event advances the bar by `100/11 ≈ 9` percentage points. The
  bar is not "smooth" within a stage — sub-stage progress is not currently
  emitted by the orchestrator (see tech debt §8.5). This is acceptable
  for alpha.

### 2.7 Output zone

While idle: empty or shows a placeholder ("No recovery yet").

On success: shows the rescued MOV path with two buttons:
- **Reveal in Finder**: opens the output directory selected in Finder via
  `subprocess.run(["open", "-R", str(rescued_path)])`.
- **Show log**: opens `recovery_log.txt` in the default text editor via
  `subprocess.run(["open", str(log_path)])`.

On error (D5 = c): shows the failure message inline as red text in the
same Output zone, followed by a **Show log** button. No popup dialog.
Example:

```
✗ Recovery failed: ffmpeg exited with code 5 in stage MUX_AV
  [ Show log ]
```

On cancel: shows `⏹  Recovery cancelled at stage <name>. Partial files in
<output_dir>.` plus a **Show log** button.

## 3. Threading model (D6 = a)

`QThread` subclassing, classic Qt. One worker class:

```python
class RecoveryWorker(QThread):
    progress = Signal(object)      # carries ProgressEvent
    finished_ok = Signal(object)   # carries RecoveryResult
    failed = Signal(str, str)      # message, log_path
    cancelled_at = Signal(str)     # stage name

    def __init__(self, mdt_path, ref_path, profile):
        super().__init__()
        self._mdt_path = mdt_path
        self._ref_path = ref_path
        self._profile = profile
        self._cancel_token = CancelToken()

    def run(self):
        try:
            result = recover(
                mdt_path=self._mdt_path,
                reference_mov_path=self._ref_path,
                profile=self._profile,
                progress=self._on_progress,
                cancel_token=self._cancel_token,
            )
            self.finished_ok.emit(result)
        except RecoveryCancelledError as exc:
            self.cancelled_at.emit(exc.stage.value)
        except RecoveryError as exc:
            self.failed.emit(str(exc), str(exc.log_path) if hasattr(exc, "log_path") else "")

    def _on_progress(self, event):
        self.progress.emit(event)

    def request_cancel(self):
        self._cancel_token.set()
```

Why `QThread` (D6 rationale recap):
- The orchestrator already exposes a callback-based progress and a
  cancel token — adapting them to Qt signals is one closure each.
- `QThread` is the idiomatic Qt path, well documented, well supported
  by `pytest-qt`.
- Cancel is direct: the token flips a flag, the orchestrator checks it
  cooperatively at stage boundaries.

The main window holds a reference to the worker while running. Worker is
created fresh per recovery (no reuse).

## 4. Module layout

```
mdt_rescue/gui/
├── __init__.py        # package marker; minimal
├── __main__.py        # entry point: python -m mdt_rescue.gui
├── app.py             # QApplication setup, main() function
├── main_window.py     # MainWindow class + layout
└── recovery_worker.py # RecoveryWorker(QThread)
```

`pyproject.toml` extras:
```toml
[project.optional-dependencies]
gui = ["PySide6>=6.6"]
```

Install: `pip install -e ".[gui]"`.

## 5. Testing strategy

- **Smoke (always on):** `tests/test_gui_smoke.py` imports the GUI modules
  and instantiates the main window without showing it. Verifies no import
  errors and no constructor crashes. Does NOT require a display server —
  uses `QApplication([])` headless.
- **Worker unit test:** `tests/test_recovery_worker.py` mocks the
  `recover()` function and asserts the worker emits the right signals in
  the right order for success / cancel / error paths.
- **Visual end-to-end:** **not automated for v0.3.0a1.** Manual: run
  `python -m mdt_rescue.gui` and exercise the happy path against
  `test_100mb.mdt` + `sano.mov`.

`pytest-qt` is added as a dev dependency for the worker test.

## 6. Out of scope for v0.3.0a1

The following are explicitly deferred and **not** to be added in C.0:

- Drag-and-drop file inputs.
- Custom output directory selection (D3 chose automatic).
- Multi-profile support in the dropdown (D2 chose forward-compatible
  scaffold only).
- Sub-stage progress percentages (tech debt §8.5).
- Light/dark theme toggle (defer to OS).
- `.app` packaging, codesigning, notarization.
- Auto-update mechanism.
- Settings persistence (remembered last reference MOV, etc.).
- Crash reporting.
- Internationalization.
- Window state restoration.

Each of these can land in a later C.x phase if user feedback warrants it.

## 7. Decisions traceability

| ID | Decision | Choice |
|----|----------|--------|
| D1 | Layout | Single window, 4 vertical zones |
| D2 | Profile UI | Dropdown with 1 entry (forward-compatible) |
| D3 | Output directory | Automatic, no prompt |
| D4 | Cancel button | Immediate, no confirm dialog |
| D5 | Error display | Inline red text in Output zone |
| D6 | Threading | `QThread` subclass |
