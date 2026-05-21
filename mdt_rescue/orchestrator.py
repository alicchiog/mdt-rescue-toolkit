"""
mdt_rescue.orchestrator
=======================

High-level recovery driver. Composes the low-level engine primitives into
a complete recovery flow, usable by both the CLI and the GUI.

Planned public API (Phase C):

- ``recover(mdt_path, reference_mov_path, profile, progress_cb, cancel_token)
  -> RecoveryResult``
- ``ProgressEvent`` — structured progress notifications (stage, percent,
  detail message)
- ``RecoveryResult`` — terminal state of a recovery run (output path,
  duration, video/audio stats, anomalies detected)
- ``CancelToken`` — cooperative cancellation signal that the GUI can flip
  to interrupt a running recovery cleanly

At v0.2.0.dev0 this module is an empty placeholder. The implementation
will be filled in Phase C, after the engine refactor (Phase B) provides
the importable primitives this module needs to compose.
"""

__all__ = []
