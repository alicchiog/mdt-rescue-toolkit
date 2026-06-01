"""
QThread worker wrapping the recovery pipeline.

Implements the threading model documented in :file:`docs/c0_design.md` §3:
a :class:`QThread` subclass that runs
:func:`mdt_rescue.orchestrator.recover` off the GUI thread, relaying its
progress callback and cancel token through Qt signals.

C.0.3 status
------------
Fully wired. :meth:`RecoveryWorker.run` calls
:func:`mdt_rescue.orchestrator.recover` on the worker thread and relays its
outcome through the four signals below. Consumed by
:class:`mdt_rescue.gui.main_window.MainWindow` (wiring lands in C.0.3.b).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from mdt_rescue.orchestrator import CancelToken, ProgressEvent, recover
from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M, Profile


class RecoveryWorker(QThread):
    """Runs a recovery off the GUI thread, relaying progress via signals.

    Signals
    -------
    progress(object):
        Each :class:`~mdt_rescue.orchestrator.ProgressEvent` from the
        orchestrator's progress callback.
    finished_ok(object):
        The :class:`~mdt_rescue.orchestrator.RecoveryResult` on success.
    failed(str, str):
        ``(message, log_path)`` on an operational failure.
    cancelled_at(str, str):
        ``(stage_name, log_path)`` where a cooperative cancel took effect.
    """

    progress = Signal(object)       # ProgressEvent
    finished_ok = Signal(object)    # RecoveryResult
    failed = Signal(str, str)       # message, log_path
    cancelled_at = Signal(str, str)  # stage name, log_path

    def __init__(
        self,
        mdt_path: Path | str,
        ref_path: Path | str,
        profile: Profile = GH5S_FHD25_ALLI_200M,
    ) -> None:
        super().__init__()
        self._mdt_path = mdt_path
        self._ref_path = ref_path
        self._profile = profile
        self._cancel_token = CancelToken()

    def run(self) -> None:
        """Run the recovery on this worker thread.

        Calls :func:`mdt_rescue.orchestrator.recover` synchronously (this
        method runs on the worker thread, so blocking is intended), passing
        :meth:`_on_progress` as the progress callback and this worker's
        :class:`CancelToken`.

        ``recover()`` does not raise for operational errors: it catches
        ``RecoveryError`` / ``RecoveryCancelledError`` internally and
        returns a :class:`~mdt_rescue.orchestrator.RecoveryResult` whose
        fields describe the terminal state. The worker therefore branches
        on those fields rather than catching exceptions:

        - ``result.success``   -> emit :attr:`finished_ok` (carrying result)
        - ``result.cancelled`` -> emit :attr:`cancelled_at` with the stage
          name from ``result.stage_failed`` (the orchestrator sets it to the
          cancelling stage; never ``None`` on the cancel path) and
          ``str(result.log_path)`` so the caller can offer "Show log"
        - otherwise (``result.error`` is set) -> emit :attr:`failed` with
          ``result.error.detail`` and ``str(result.log_path)`` (passed
          through faithfully; the caller decides whether the log file exists
          before exposing a "Show log" action)

        Programming bugs (e.g. ``TypeError``) are intentionally NOT caught
        here; they propagate as an unhandled exception on the thread.
        """
        result = recover(
            mdt_path=self._mdt_path,
            reference_mov_path=self._ref_path,
            profile=self._profile,
            progress=self._on_progress,
            cancel_token=self._cancel_token,
        )
        if result.success:
            self.finished_ok.emit(result)
        elif result.cancelled:
            stage_name = (
                result.stage_failed.value
                if result.stage_failed is not None
                else "unknown"
            )
            self.cancelled_at.emit(stage_name, str(result.log_path))
        else:
            message = (
                result.error.detail
                if result.error is not None
                else "Recovery failed for an unknown reason."
            )
            self.failed.emit(message, str(result.log_path))

    def _on_progress(self, event: ProgressEvent) -> None:
        """Progress callback passed to ``recover()``; re-emits as a signal."""
        self.progress.emit(event)

    def request_cancel(self) -> None:
        """Request cooperative cancellation of the running recovery.

        Flips the worker's :class:`CancelToken`; the orchestrator checks it
        at the next stage boundary. Thread-safe (token is backed by
        :class:`threading.Event`).
        """
        self._cancel_token.cancel()
