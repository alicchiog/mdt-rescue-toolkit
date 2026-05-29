"""
mdt_rescue.gui
==============

Optional PySide6 desktop GUI for the recovery toolkit.

This subpackage is installed only when the user opts in via the ``[gui]``
extra (``pip install -e ".[gui]"``), because PySide6 is a large dependency
(~100 MB) that would be inappropriate to require for headless or CLI-only
usage.

Modules
-------
- :mod:`mdt_rescue.gui.app` -- ``QApplication`` bootstrap and ``main()``
  entry point.
- :mod:`mdt_rescue.gui.main_window` -- :class:`MainWindow`, the single
  window implementing the four-zone layout (input / action / progress /
  output) documented in :file:`docs/c0_design.md`.
- :mod:`mdt_rescue.gui.recovery_worker` -- :class:`RecoveryWorker`, a
  ``QThread`` wrapper around :func:`mdt_rescue.orchestrator.recover` so the
  GUI stays responsive during the multi-minute recovery.

Launch with ``python -m mdt_rescue.gui``.

Import safety
-------------
This package marker intentionally does **not** import PySide6 or any of the
submodules above, so ``import mdt_rescue.gui`` (and ``from mdt_rescue import
gui``) succeeds even when the ``[gui]`` extra is not installed. PySide6 is
imported lazily, only when :mod:`~mdt_rescue.gui.app` or
:mod:`~mdt_rescue.gui.main_window` is imported.
"""

__all__ = []
