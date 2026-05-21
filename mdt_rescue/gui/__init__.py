"""
mdt_rescue.gui
==============

Optional PySide6 GUI layer for the recovery toolkit.

This subpackage is installed only when the user opts in via the ``[gui]``
extra (``pip install -e .[gui]``), because PySide6 is a ~200 MB dependency
that would be inappropriate to require for headless or CLI-only usage.

Planned modules (Phase D):

- ``app`` — QApplication bootstrap
- ``main_window`` — single window with file pickers, profile selector,
  log view, status panel, recover/cancel buttons
- ``recovery_worker`` — QThread wrapper around the orchestrator so the
  GUI stays responsive during the multi-minute recovery
- ``widgets/`` — small custom widgets (file picker, log view, etc.)

At v0.2.0.dev0 this subpackage is intentionally empty. The GUI layer will
not be touched until the engine refactor (Phase B) and the orchestrator
(Phase C) are complete and stable.
"""

__all__ = []
