# P.0 Design — macOS app packaging

**Status:** approved (P.0.1 complete, design-frozen for P.1)
**Target:** a downloadable, double-clickable macOS app of the desktop GUI
with `ffmpeg`/`ffprobe` bundled — no Python, Homebrew, or ffmpeg install
required by the end user.
**Distribution:** unsigned, non-notarized `.dmg` (free path; no Apple
Developer account).

This document is the single source of truth for *how* the GUI will be
packaged. It is design only: P.0.1 writes this doc and nothing else — no
packaging code, no build, no new dependencies, no changes to existing
code. Implementation lands in the gated phases P.1–P.4 (§8). Like
`docs/c0_design.md` preceded the GUI, this precedes the packaging work.

---

## 1. Goal & constraints

Settled decisions (from prior discussion — treat as fixed for P.0):

- **Audience.** A non-developer GH5S videomaker who has a corrupted `.MDT`
  and a sane reference `.MOV`. They should download one file, open it, and
  recover — without a terminal, Python, Homebrew, or a separate ffmpeg
  install.
- **ffmpeg/ffprobe are bundled** inside the app. No separate install for
  the end user. A heavier bundle is explicitly accepted.
- **The `.dmg` is unsigned and not notarized.** No Apple Developer account,
  no 99 USD/year. The user will see the Gatekeeper warning on first launch
  and use right-click → Open (§6). This is the accepted free-path tradeoff.
- **Licensing stays clean.** The toolkit's own code remains MIT; bundled
  ffmpeg binaries keep their own license (§3). No GPL obligation reaches the
  toolkit's code.

Non-goals for this packaging line: code signing, notarization, the App
Store, auto-update, a Windows/Linux build, and any change to the recovery
pipeline's behavior or bit-exact output.

---

## 2. Packaging tool

**Decision (D-PKG): PyInstaller, with `create-dmg` for the disk image.**

The app is a PySide6 GUI that shells out to two standalone executables
(`ffmpeg`, `ffprobe`). The tool must (a) bundle PySide6/Qt correctly on
macOS, (b) bundle arbitrary external executables that are *not* Python
dependencies, and (c) produce a `.app` (the `.dmg` can come from a separate
step).

| Tool | PySide6 on macOS | Bundling ffmpeg/ffprobe | Fit |
|------|------------------|-------------------------|-----|
| **PyInstaller** | Mature, dedicated PySide6 hooks that pull in Qt plugins/frameworks and fix `DYLD` lookup. | `--add-binary` bundles standalone executables; resolved at runtime via `sys._MEIPASS`. | **Best fit** for "GUI + shell out to a bundled binary." |
| py2app | macOS-native but historically fiddly with PySide/PySide6; Qt plugin handling is painful. | Possible via `resources`, but forces a `setup.py` build (the project is pyproject-based). | Friction. |
| Briefcase | Modern, produces native `.app`/`.dmg`. | Its strengths are signing/notarization (which we skip) and a prescribed project layout; bundling arbitrary side binaries is less battle-tested for this pattern. | Capable but heavier-handed here. |

Rationale: PyInstaller's `--add-binary` + `sys._MEIPASS` is the cleanest
match for our shell-out model, and its PySide6 hook removes the hardest part
(Qt frameworks/plugins). The `.dmg` is produced separately by `create-dmg`
(or `hdiutil`), so tool choice and image creation are decoupled.

---

## 3. ffmpeg build & license

**Decision (D-FFMPEG): bundle an LGPL ffmpeg/ffprobe build with no
`libx264` and no other GPL components.**

This is decided by what the pipeline actually does. The four ffmpeg
invocations (`mdt_rescue/orchestrator.py`, the `_FFMPEG_*_ARGS` constants
around lines 307–348) only **copy/remux** video and handle **PCM** audio —
they never encode H.264:

- `_FFMPEG_REF_TO_ANNEXB_ARGS`: `-c copy` + `-bsf:v h264_mp4toannexb`
  (stream copy plus a bitstream filter — both ffmpeg-core; no encoder).
- `_FFMPEG_WRAP_VIDEO_MOV_ARGS`: `-c copy` (wrap H.264 into MOV).
- `_FFMPEG_RAW_TO_WAV_ARGS`: raw `s16be` → WAV (PCM; ffmpeg-core).
- `_FFMPEG_MUX_AV_ARGS`: `-c:v copy` + `-c:a pcm_s16be` (video copied, audio
  PCM).

`libx264` is required to **encode** H.264 — not to copy, demux, or decode
it. Since the pipeline never encodes H.264 (and only ever produces PCM
audio), a GPL-only component is never invoked. **An LGPL build without
`libx264` is therefore sufficient**, which keeps the bundle free of GPL
code.

**Mere aggregation — the lowest-obligation case.** The toolkit invokes
`ffmpeg`/`ffprobe` as **standalone executables via `subprocess`** and never
links `libav*` into the Python/Qt process. Bundling them in the `.app` is
*mere aggregation* of independent programs on the same medium, not linking.
Consequently the LGPL's relinking/object-file obligations (which exist to
let a user swap in a modified library) are **not triggered** — there is no
linking to our code at all. What remains is the ordinary redistribution
duty for the ffmpeg binaries themselves (§7): ship their license text and,
for the LGPL build, make the corresponding source available (a link to the
exact upstream build/source suffices).

The toolkit's own source stays **MIT** and is already public on GitHub.

Candidate static-build sources (evaluated and pinned in P.3, not chosen
here): a reputable prebuilt static LGPL macOS ffmpeg, or a from-source build
configured `--disable-gpl --enable-version3` without `libx264`. Selection
criteria in P.3: LGPL/no-GPL, contains both `ffmpeg` and `ffprobe`,
arch coverage (§5), and a verifiable source/build provenance.

Bundle-size estimate: ffmpeg + ffprobe static ≈ 70–100 MB per architecture
(roughly double for Universal2 via `lipo`), on top of PySide6/Qt
(≈ 100–200 MB). A multi-hundred-MB `.app` is expected and accepted.

> **Not legal advice.** This section reflects a good-faith reading for a
> free, open-source hobby release. For serious or commercial distribution,
> consult someone qualified.

---

## 4. Runtime binary resolution (spec only)

Today both executables are hardcoded string literals that rely on `PATH`:

- `mdt_rescue/orchestrator.py` — `ffmpeg` ×4: each call builds
  `["ffmpeg", *_format_ffmpeg_args(...)]` (≈ lines 616, 712, 749, 771), and
  the preflight does `shutil.which(c)` for `("ffmpeg", "ffprobe",
  "python3")` (≈ line 446).
- `mdt_rescue/engine/verify.py` — `ffprobe` ×11: `_probe_field` builds
  `["ffprobe", …]` (≈ line 235), and `verify_mov` preflight does
  `shutil.which("ffprobe")` → `FFprobeNotFoundError` (≈ line 445).

There is no existing config point for a binary path. **P.1 introduces a
single resolution seam** so a bundled binary is used when frozen and `PATH`
is used in development.

**New module: `mdt_rescue/runtime.py`** (package-root level, dependency-free,
importable by both `orchestrator` and `engine.verify` without coupling).

**Function shape (spec — implemented in P.1, not now):**

```python
def resolve_ffmpeg_binary(tool: str) -> str:
    """Return the executable to invoke for "ffmpeg" or "ffprobe".

    Frozen (packaged .app): the bundled binary's absolute path, located
    relative to the freeze root (e.g. sys._MEIPASS for PyInstaller).
    Development: shutil.which(tool) or the bare name as a last resort.
    """
```

- The frozen branch's exact location is **packaging-tool dependent**
  (PyInstaller → `sys._MEIPASS`; a py2app/Briefcase layout would differ).
  Centralizing it here means only this one function changes if the tool
  ever changes.
- Refactor sites in P.1 (only these two):
  1. **orchestrator** — replace the literal `"ffmpeg"` at the four arg-list
     heads with `resolve_ffmpeg_binary("ffmpeg")`, and the preflight
     `shutil.which("ffmpeg")` accordingly.
  2. **engine.verify** — replace the literal `"ffprobe"` in `_probe_field`
     and the `shutil.which("ffprobe")` preflight.

**Vestigial `python3` preflight — must be removed in P.1.** The orchestrator
preflight currently also requires `python3` on `PATH` (≈ line 446), but
nothing in the pipeline shells out to `python3` (the engine primitives run
in-process). A frozen `.app` has **no `python3` on `PATH`**, so this check
would make preflight fail spuriously on every packaged run. P.1 drops
`python3` from the dependency check (keeping only `ffmpeg`/`ffprobe`, now
resolved via the seam).

This section is specification only. No code is written in P.0.

---

## 5. Target architecture

**Decision (D-ARCH): Universal2 is the release target** (one `.dmg` that
runs on both Apple Silicon and Intel).

- The "download one file, it just works" goal argues against shipping
  per-arch downloads that force a non-developer to know their chip. The
  GH5S (2018) audience runs a heterogeneous mix that still includes Intel
  Macs, so broad reach matters.
- The cost (combine two static ffmpeg builds with `lipo`; ensure the Python
  runtime and PySide6/Qt are Universal2-capable) is a one-time engineering
  cost borne by us, consistent with the accepted heavier bundle.

**Nuance.** The **first local proof-of-build (P.2) may target the current
architecture only** — it exists to prove the `.app` runs at all. The
**release** `.dmg` goal is Universal2. **Fallback:** if Universal2 PySide6
or ffmpeg tooling proves too costly, ship **two per-arch `.dmg` files**
(arm64 and x86_64) with a one-line "which one do I pick?" note.

---

## 6. `.dmg` & Gatekeeper UX

- **Production.** Build the `.app` (PyInstaller), then wrap it in an
  unsigned `.dmg` via `create-dmg` (or `hdiutil`): a window with the app
  icon and an Applications-folder alias for drag-to-install. No signing, no
  notarization.
- **First-launch UX (unsigned).** macOS Gatekeeper will block a
  double-click of an unsigned, un-notarized app. The documented path:
  **right-click (or Control-click) the app → Open → Open** in the dialog;
  or **System Settings → Privacy & Security → "Open Anyway"** after the
  first blocked attempt. This is a once-per-install step.
- **Shipping wording.** When P.4 ships, add a short "First launch on macOS"
  note to the README and the GitHub release notes with the exact
  right-click → Open steps and a plain-language reason ("the app is not
  signed with a paid Apple certificate; it is safe to open"). This wording
  is written in P.4, not now.
- **Release asset.** The GitHub release for the packaging milestone carries
  the `.dmg` (one Universal2 image, or two per-arch images under the
  fallback).

---

## 7. Licenses in the bundle

- Include ffmpeg's license text (LGPL `COPYING.LGPLv2.1` / `COPYING.LGPLv3`
  as applicable) inside the `.app` (e.g. under `Resources/licenses/`) and
  in the `.dmg`.
- Include a short `NOTICE`/`THIRD_PARTY` file recording the bundled ffmpeg
  version and the URL of the exact build/source used (satisfies the
  LGPL source-availability expectation; §3).
- Add a "Licenses" note to the README and a CHANGELOG entry when the
  packaged release ships: toolkit = MIT; bundled `ffmpeg`/`ffprobe` =
  LGPL (their own terms); invoked as standalone executables.

---

## 8. Implementation phases (preview)

Gated, non-overlapping. Each is a separate future task with its own
audit/propose/approve cycle; this doc does not authorize any of them.

- **P.1 — runtime binary-resolution seam + tests.** Add
  `mdt_rescue/runtime.py::resolve_ffmpeg_binary`; refactor the two call
  sites (§4); remove the vestigial `python3` preflight. Unit tests cover the
  frozen branch (monkeypatched `sys.frozen` + freeze root) and the
  development branch (`shutil.which`). No behavior change in dev; the suite
  stays green. **Code phase — no packaging yet.**
- **P.2 — PyInstaller config + first local `.app`.** Add the build spec
  (kept out of the installed package); produce a first `.app` (current-arch
  ok, §5) that launches and finds a *system* ffmpeg via the dev fallback.
  Proves the GUI bundles and runs.
- **P.3 — bundle ffmpeg/ffprobe + GUI end-to-end bit-exact.** Add the LGPL
  binaries to the bundle; the frozen app resolves them via the seam. Verify
  end-to-end, like the C.0.3 GUI e2e: the packaged app recovers
  `test_100mb.mdt` to a MOV **byte-identical** to the B.0a baseline.
- **P.4 — `.dmg` + Gatekeeper docs + GitHub release.** Produce the unsigned
  `.dmg` (Universal2 target, §5); write the README/release first-launch
  wording (§6); attach the `.dmg` to a GitHub release; include license files
  (§7).

---

## 9. Open decisions

None block P.1. Deferred (decided in their phase, not open now):

- The specific static-ffmpeg build/source — chosen in **P.3** against the
  §3 criteria.
- Universal2 vs the two-per-arch fallback — confirmed in **P.4** once the
  toolchain's Universal2 capability is known; the **target stays Universal2**
  (§5).

---

## 10. Decisions traceability

| ID | Decision | Choice |
|----|----------|--------|
| D-PKG | Packaging tool | PyInstaller + `create-dmg` |
| D-FFMPEG | ffmpeg build/license | LGPL, no `libx264`/GPL; bundled as standalone executables (mere aggregation) |
| D-SEAM | Binary resolution | `mdt_rescue/runtime.py::resolve_ffmpeg_binary(tool)`; bundled when frozen, `PATH` in dev |
| D-PREFLIGHT | `python3` check | Removed in P.1 (vestigial; absent in a frozen app) |
| D-ARCH | Target architecture | Universal2 release; current-arch allowed for the first proof-of-build |
| D-DMG | Distribution | Unsigned, non-notarized `.dmg`; right-click → Open first launch |
