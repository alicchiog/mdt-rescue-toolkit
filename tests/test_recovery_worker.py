"""
Unit tests for mdt_rescue.gui.recovery_worker.RecoveryWorker (C.0.3.a).

These verify the worker's branching logic: run() calls recover() (mocked
here) and emits exactly one of finished_ok / cancelled_at / failed
depending on the returned RecoveryResult fields. recover() is substituted
with a controlled fake, so no real files, ffmpeg, or recovery run; the
worker is driven on a real QThread and the signals are awaited via
pytest-qt's qtbot.

The Qt "offscreen" platform plugin is selected before any Qt import so the
suite runs headless.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from mdt_rescue.orchestrator import (
    RecoveryCancelledError,
    RecoveryResult,
    RecoveryStageError,
    Stage,
)
from mdt_rescue.gui.recovery_worker import RecoveryWorker


def _make_result(**overrides) -> RecoveryResult:
    """Build a RecoveryResult with every field populated, overridable."""
    defaults = dict(
        success=False,
        cancelled=False,
        output_dir=Path("/tmp/recovery_output_test"),
        log_path=Path("/tmp/recovery_output_test/recovery_log.txt"),
        rescued_mov_path=None,
        artifacts={},
        stages_completed=(),
        stage_failed=None,
        error=None,
        elapsed_seconds=1.23,
        verify_result=None,
    )
    defaults.update(overrides)
    return RecoveryResult(**defaults)


def test_worker_emits_finished_ok_on_success(qtbot, monkeypatch):
    """A successful result emits finished_ok with the result, and run()
    forwards the audited kwargs (progress callback + cancel token)."""
    rescued = Path("/tmp/recovery_output_test/test_RESCUED.mov")
    result = _make_result(
        success=True,
        rescued_mov_path=rescued,
        stages_completed=tuple(Stage),
    )
    captured = {}

    def fake_recover(**kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(
        "mdt_rescue.gui.recovery_worker.recover", fake_recover
    )

    worker = RecoveryWorker("broken.mdt", "ref.mov")
    with qtbot.waitSignal(worker.finished_ok, timeout=5000) as blocker:
        worker.start()
    assert worker.wait(5000)

    assert blocker.args == [result]
    assert captured["mdt_path"] == "broken.mdt"
    assert captured["reference_mov_path"] == "ref.mov"
    assert captured["progress"] == worker._on_progress
    assert captured["cancel_token"] is worker._cancel_token


def test_worker_emits_cancelled_at_on_cancel(qtbot, monkeypatch):
    """A cancelled result emits cancelled_at with the stage_failed value."""
    result = _make_result(
        success=False,
        cancelled=True,
        stage_failed=Stage.MUX_AV,
        error=RecoveryCancelledError(detail="cancelled", stage=Stage.MUX_AV),
    )
    monkeypatch.setattr(
        "mdt_rescue.gui.recovery_worker.recover",
        lambda **kwargs: result,
    )

    worker = RecoveryWorker("broken.mdt", "ref.mov")
    with qtbot.waitSignal(worker.cancelled_at, timeout=5000) as blocker:
        worker.start()
    assert worker.wait(5000)

    assert blocker.args == ["mux_av"]


def test_worker_emits_failed_on_error(qtbot, monkeypatch):
    """An error result emits failed with (error.detail, str(log_path))."""
    log_path = Path("/tmp/recovery_output_test/recovery_log.txt")
    result = _make_result(
        success=False,
        cancelled=False,
        stage_failed=Stage.MUX_AV,
        log_path=log_path,
        error=RecoveryStageError(
            detail="ffmpeg exited with code 5", stage=Stage.MUX_AV
        ),
    )
    monkeypatch.setattr(
        "mdt_rescue.gui.recovery_worker.recover",
        lambda **kwargs: result,
    )

    worker = RecoveryWorker("broken.mdt", "ref.mov")
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    assert worker.wait(5000)

    assert blocker.args == ["ffmpeg exited with code 5", str(log_path)]
