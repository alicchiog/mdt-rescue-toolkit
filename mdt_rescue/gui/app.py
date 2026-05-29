"""
QApplication bootstrap for the mdt_rescue desktop GUI.

Exposes :func:`main`, the single entry point invoked by
``python -m mdt_rescue.gui`` (see :mod:`mdt_rescue.gui.__main__`).
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from PySide6.QtWidgets import QApplication

from mdt_rescue.gui.main_window import MainWindow


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Launch the GUI and run the Qt event loop.

    Parameters
    ----------
    argv:
        Optional argument vector. Defaults to :data:`sys.argv` when
        ``None``. Accepting it explicitly keeps :func:`main` testable and
        lets callers inject a controlled vector.

    Returns
    -------
    The Qt event loop's exit code (``0`` on a clean quit).
    """
    args = list(sys.argv if argv is None else argv)

    app = QApplication.instance()
    if app is None:
        app = QApplication(args)
    app.setApplicationName("MDT Rescue Toolkit")

    window = MainWindow()
    window.show()
    return app.exec()
