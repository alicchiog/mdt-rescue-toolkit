"""End-to-end smoke test for the Python orchestrator (Phase B.7).

This test exercises ``mdt_rescue.orchestrator.recover()`` on real input files
and verifies the produced artifacts bit-exact against the v0.1 baseline
captured in Phase B.0a (``tests/baselines/v0.1-smoke/``).

It is the only test in the suite that performs real file I/O against the
``.mdt``/``.mov`` reference pair, runs ``ffmpeg``/``ffprobe`` subprocesses,
and writes to ``/tmp``.  Runtime on a 2019 MacBook Pro is ~3-4 minutes.

Gating (triple)
---------------
1. ``@pytest.mark.slow`` marker (registered in ``pyproject.toml``)
2. environment variable ``MDT_RUN_SMOKE=1`` (skipped silently otherwise)
3. input files present with matching SHA256 (skipped with a clear message
   otherwise)

The test is collected by default but skipped without ``MDT_RUN_SMOKE=1``.

To execute the test explicitly::

    MDT_RUN_SMOKE=1 pytest -m slow tests/test_smoke_e2e.py -v

Output directory
----------------
The test writes to ``/tmp/recovery_output_test_100mb_b7smoke/``.  This path
is intentionally distinct from the ``.sh`` orchestrator's default
``/tmp/recovery_output_test_100mb/`` to avoid clobbering manual ``.sh`` runs.

The directory is wiped before each run, and **intentionally kept after**
the run for post-mortem inspection.  On bit-exact mismatch this lets the
operator diff the produced artifact against the baseline byte-by-byte.

Assertion families
------------------
1. ``recover()`` returns a ``RecoveryResult`` with ``success is True`` and
   ``cancelled is False``.
2. All 11 ``Stage`` values appear in ``stages_completed`` (4 preflight + 7
   functional).
3. The 8 canonical artifact keys are exactly the set of keys in
   ``result.artifacts``.
4. Each artifact exists on disk with the expected ``test_100mb_*`` basename.
5. The SHA256 of each of the 8 deterministic artifacts is bit-exact against
   the B.0a baseline (the v0.1.0 invariant).
6. ``recovery_log.txt`` exists in the output directory.
7. The log contains exactly 7 ``STEP N/7:`` markers and exactly one
   ``RECOVERY COMPLETED`` marker.
8. Every stage in ``result.stages_completed`` has at least one
   ``COMPLETE`` event on the captured ``ProgressEvent`` stream
   (subset, not equality -- avoids fragility on event-stream details).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

import pytest

from mdt_rescue.orchestrator import (
    ProgressEvent,
    ProgressStatus,
    RecoveryResult,
    Stage,
    recover,
)
from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Input files (B.0a baseline inputs)
TEST_MDT_PATH = Path(
    "/Users/gianpieroalicchio/Documents/Programmazione/"
    "MDT Rescue Toolkit/file di test/test_100mb.mdt"
)
TEST_MDT_EXPECTED_SHA256 = (
    "3d1bc646f67b9a6d3d1c6be0ac8d46b521468088b17013b5c26c914b7019d1ce"
)

SANE_MOV_PATH = Path(
    "/Users/gianpieroalicchio/Documents/Programmazione/"
    "MDT Rescue Toolkit/file di test/sano.mov"
)
SANE_MOV_EXPECTED_SHA256 = (
    "c6b9888747c889c85163d1e2f8af8f0b613982df8696d366c456140c230f3b21"
)

# Output dir (B.7-specific, non-colliding with the .sh orchestrator default)
OUTPUT_DIR = Path("/tmp/recovery_output_test_100mb_b7smoke")

# Baseline B.0a location
BASELINE_DIR = Path(__file__).parent / "baselines" / "v0.1-smoke"
BASELINE_SHA_FILE = BASELINE_DIR / "output.sha256.txt"

# The 8 deterministic artifacts, in canonical orchestrator-key order.
# Each entry maps the orchestrator's artifact key to the expected on-disk
# basename (which is derived as ``<mdt.stem>_<suffix>`` by the orchestrator;
# for ``/tmp/test_100mb.mdt`` the stem is ``test_100mb``).
EXPECTED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("sane_annexb",       "test_100mb_sane_annexb.h264"),
    ("sps_pps_prefix",    "test_100mb_sps_pps_prefix.h264"),
    ("video_h264",        "test_100mb_video.h264"),
    ("video_with_header", "test_100mb_video_with_header.h264"),
    ("video_only_mov",    "test_100mb_video_only.mov"),
    ("audio_raw",         "test_100mb_audio.raw"),
    ("audio_wav",         "test_100mb_audio.wav"),
    ("rescued_mov",       "test_100mb_RESCUED.mov"),
)

# Log file (side-effect of recover(), not in RecoveryResult.artifacts)
LOG_FILENAME = "recovery_log.txt"
EXPECTED_STEP_MARKER_COUNT = 7  # one per functional stage
EXPECTED_COMPLETION_MARKER = "RECOVERY COMPLETED"
EXPECTED_COMPLETION_COUNT = 1

# SHA256 streaming chunk size (1 MiB).  Large enough to amortise syscall
# overhead on the rescued MOV (~100 MB) without growing memory.
_SHA_CHUNK_SIZE = 1 << 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_of(path: Path) -> str:
    """Return the lowercase hex SHA256 digest of the file at ``path``.

    Streams the file in 1 MiB chunks so it works for the rescued MOV
    (~100 MB) without holding the whole file in memory.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_SHA_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_baseline_shas() -> dict[str, str]:
    """Parse the B.0a SHA file into ``{basename: sha256_hex}``.

    The baseline file uses absolute paths from the original capture
    (``/tmp/recovery_output_test_100mb/...``); only the basename is used
    for matching, so the parsing is path-prefix-agnostic.

    The ``recovery_log.txt`` entry is intentionally excluded: that file is
    not bit-exact (it contains run-time timestamps); its semantic content
    is verified separately via marker grep.
    """
    result: dict[str, str] = {}
    for line in BASELINE_SHA_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sha_hex, full_path = line.split(None, 1)
        basename = Path(full_path).name
        if basename == LOG_FILENAME:
            continue
        result[basename] = sha_hex
    return result


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_recover_e2e_bit_exact_vs_baseline() -> None:
    """End-to-end smoke: recover() vs B.0a baseline, bit-exact on 8 artifacts.

    See the module docstring for full assertion families and gating rules.
    """
    # === 1. GATING (triple) ===
    if os.environ.get("MDT_RUN_SMOKE") != "1":
        pytest.skip("set MDT_RUN_SMOKE=1 to run end-to-end smoke test")

    if not TEST_MDT_PATH.exists():
        pytest.skip(f"missing input: {TEST_MDT_PATH}")
    actual_mdt_sha = _sha256_of(TEST_MDT_PATH)
    if actual_mdt_sha != TEST_MDT_EXPECTED_SHA256:
        pytest.skip(
            f"input {TEST_MDT_PATH} SHA256 mismatch "
            f"(expected {TEST_MDT_EXPECTED_SHA256}, got {actual_mdt_sha})"
        )

    if not SANE_MOV_PATH.exists():
        pytest.skip(f"missing input: {SANE_MOV_PATH}")
    actual_mov_sha = _sha256_of(SANE_MOV_PATH)
    if actual_mov_sha != SANE_MOV_EXPECTED_SHA256:
        pytest.skip(
            f"reference MOV {SANE_MOV_PATH} SHA256 mismatch "
            f"(expected {SANE_MOV_EXPECTED_SHA256}, got {actual_mov_sha})"
        )

    if not BASELINE_SHA_FILE.exists():
        pytest.skip(f"missing baseline file: {BASELINE_SHA_FILE}")

    # === 2. SETUP: clean output dir but do NOT pre-create it ===
    # The orchestrator creates the dir during PREFLIGHT_OUTPUT_DIR; that
    # stage is part of what we want to exercise.  Intentionally NOT cleaned
    # after the run -- see module docstring.
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    # === 3. INVOKE recover() ===
    captured_events: list[ProgressEvent] = []
    result: RecoveryResult = recover(
        mdt_path=TEST_MDT_PATH,
        reference_mov_path=SANE_MOV_PATH,
        profile=GH5S_FHD25_ALLI_200M,
        output_dir=OUTPUT_DIR,
        progress=captured_events.append,
        cancel_token=None,
    )

    # === 4. ASSERT RecoveryResult top-level state ===
    assert result.success is True, (
        f"recovery did not succeed: success={result.success}, "
        f"cancelled={result.cancelled}, stages_completed={result.stages_completed}"
    )
    assert result.cancelled is False, "recovery reports cancellation"

    expected_stages = tuple(Stage)
    assert result.stages_completed == expected_stages, (
        f"expected all {len(expected_stages)} stages completed, "
        f"got {len(result.stages_completed)}: {result.stages_completed}"
    )

    assert result.rescued_mov_path is not None, "rescued_mov_path is None"
    assert result.rescued_mov_path.exists(), (
        f"rescued MOV not present at reported path: {result.rescued_mov_path}"
    )

    # === 5. ASSERT artifact keys match the 8 canonical set ===
    expected_keys = {key for key, _ in EXPECTED_ARTIFACTS}
    actual_keys = set(result.artifacts.keys())
    assert actual_keys == expected_keys, (
        f"artifact keys mismatch:\n"
        f"  expected: {sorted(expected_keys)}\n"
        f"  actual:   {sorted(actual_keys)}\n"
        f"  missing:  {sorted(expected_keys - actual_keys)}\n"
        f"  extra:    {sorted(actual_keys - expected_keys)}"
    )

    # === 6. ASSERT each artifact exists with the expected basename ===
    for key, expected_basename in EXPECTED_ARTIFACTS:
        artifact_path = result.artifacts[key]
        assert artifact_path.exists(), (
            f"{key}: file not created at {artifact_path}"
        )
        assert artifact_path.name == expected_basename, (
            f"{key}: basename mismatch "
            f"(got {artifact_path.name!r}, expected {expected_basename!r})"
        )

    # === 7. ASSERT SHA256 bit-exact vs B.0a baseline (THE invariant) ===
    baseline_shas = _load_baseline_shas()
    assert len(baseline_shas) == 8, (
        f"baseline has {len(baseline_shas)} deterministic SHAs, expected 8: "
        f"{sorted(baseline_shas)}"
    )
    # Defensive: every expected basename must have a baseline SHA, and no
    # unexpected deterministic basename should be in the baseline.
    expected_basenames = {basename for _, basename in EXPECTED_ARTIFACTS}
    baseline_basenames = set(baseline_shas.keys())
    assert baseline_basenames == expected_basenames, (
        f"baseline basenames mismatch:\n"
        f"  expected: {sorted(expected_basenames)}\n"
        f"  baseline: {sorted(baseline_basenames)}\n"
        f"  missing:  {sorted(expected_basenames - baseline_basenames)}\n"
        f"  extra:    {sorted(baseline_basenames - expected_basenames)}"
    )

    mismatches: list[tuple[str, str, str, str]] = []
    for key, expected_basename in EXPECTED_ARTIFACTS:
        artifact_path = result.artifacts[key]
        actual_sha = _sha256_of(artifact_path)
        expected_sha = baseline_shas[expected_basename]
        if actual_sha != expected_sha:
            mismatches.append((key, expected_basename, expected_sha, actual_sha))

    if mismatches:
        lines = ["SHA256 bit-exact mismatch vs B.0a baseline:"]
        for key, name, exp, act in mismatches:
            lines.append(f"  {key} ({name}):")
            lines.append(f"    expected: {exp}")
            lines.append(f"    actual:   {act}")
        pytest.fail("\n".join(lines))

    # === 8. ASSERT recovery_log.txt semantic markers ===
    log_path = OUTPUT_DIR / LOG_FILENAME
    assert log_path.exists(), f"log file not created at {log_path}"
    log_text = log_path.read_text(encoding="utf-8")

    step_matches = re.findall(r"STEP \d+/7:", log_text)
    assert len(step_matches) == EXPECTED_STEP_MARKER_COUNT, (
        f"expected {EXPECTED_STEP_MARKER_COUNT} 'STEP N/7:' markers, "
        f"got {len(step_matches)}: {step_matches}"
    )

    completion_count = log_text.count(EXPECTED_COMPLETION_MARKER)
    assert completion_count == EXPECTED_COMPLETION_COUNT, (
        f"expected {EXPECTED_COMPLETION_COUNT} '{EXPECTED_COMPLETION_MARKER}' "
        f"marker, got {completion_count}"
    )

    # === 9. ASSERT ProgressEvent stream reflects completed stages ===
    # Subset check (not equality): each completed stage must have at least
    # one COMPLETE event.  Intentionally NOT checking ordering or
    # event counts -- those are implementation details, not the contract.
    completed_stages_from_events = {
        e.stage
        for e in captured_events
        if e.status == ProgressStatus.COMPLETE
    }
    missing_in_events = set(result.stages_completed) - completed_stages_from_events
    assert not missing_in_events, (
        f"stages_completed not all reflected in progress events; missing: "
        f"{sorted(s.value for s in missing_in_events)}"
    )
