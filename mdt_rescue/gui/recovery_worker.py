"""
QThread worker wrapping the recovery pipeline.

Implements the threading model documented in :file:`docs/c0_design.md` §3:
a :class:`QThread` subclass that will run
:func:`mdt_rescue.orchestrator.recover` off the GUI thread, relaying its
progress callback and cancel token through Qt signals.

C.0.2 status
------------
This is the scaffold. The signal shape, the :class:`CancelToken` ownership,
:meth:`_on_progress`, and :meth:`request_cancel` are implemented, but
:meth:`run` is a deliberate deferred stub -- the real ``recover()`` call
lands in C.0.3.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from mdt_rescue.orchestrator import CancelToken, ProgressEvent
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
    cancelled_at(str):
        The stage name where a cooperative cancel took effect.
    """

    progress = Signal(object)       # ProgressEvent
    finished_ok = Signal(object)    # RecoveryResult
    failed = Signal(str, str)       # message, log_path
    cancelled_at = Signal(str)      # stage name

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
        """Execute the recovery on the worker thread.

        DEFERRED TO C.0.3 -- intentionally unimplemented in the C.0.2
        scaffold.

        When wired in C.0.3, this must call
        :func:`mdt_rescue.orchestrator.recover` with ``self._on_progress`` as
        the progress callback and ``self._cancel_token`` as the cancel
        token, then branch on the returned :class:`RecoveryResult` fields --
        NOT on caught exceptions:

        - ``result.success``   -> emit :attr:`finished_ok`
        - ``result.cancelled`` -> emit :attr:`cancelled_at`
        - otherwise (``result.error`` is set) -> emit :attr:`failed`

        ``recover()`` catches ``RecoveryError`` / ``RecoveryCancelledError``
        internally and surfaces them as ``RecoveryResult.error`` /
        ``RecoveryResult.cancelled``, so the result fields are the primary
        control flow; a try/except on those exceptions would never fire.
        """
        raise NotImplementedError(
            "RecoveryWorker.run() is wired in C.0.3 "
            "-- see docs/c0_design.md §3"
        )

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
