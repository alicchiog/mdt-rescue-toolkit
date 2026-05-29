"""
Smoke tests for the mdt_rescue desktop GUI scaffold (C.0.2).

These verify the GUI modules import cleanly and that :class:`MainWindow`
can be constructed headless, without showing a window or starting the Qt
event loop. They do NOT exercise any recovery logic (the Recover button is
not wired until C.0.3).

The Qt "offscreen" platform plugin is selected before any Qt import so the
suite runs without a display server. PySide6 is imported unconditionally:
C.0.2 requires the ``[gui]`` extra, so a missing PySide6 should fail loudly
rather than skip.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import mdt_rescue


def test_app_module_imports():
    """The QApplication bootstrap module imports without error."""
    import mdt_rescue.gui.app as app_module

    assert hasattr(app_module, "main")


def test_main_window_module_imports():
    """The main window module imports without error."""
    import mdt_rescue.gui.main_window as mw_module

    assert hasattr(mw_module, "MainWindow")


def test_main_window_instantiates():
    """MainWindow builds headless and exposes a version-stamped title.

    Also asserts the profile selector contains the validated profile's
    display text, keeping the smoke test aligned with the Profile API the
    UI resolves to (``Profile.label``).
    """
    from PySide6.QtWidgets import QApplication

    from mdt_rescue.gui.main_window import MainWindow
    from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        title = window.windowTitle()
        assert title
        assert mdt_rescue.__version__ in title

        combo_items = [
            window.profile_combo.itemText(i)
            for i in range(window.profile_combo.count())
        ]
        expected_text = GH5S_FHD25_ALLI_200M.label
        assert expected_text in combo_items, (
            f"profile combo should contain {expected_text!r}, "
            f"got {combo_items}"
        )
    finally:
        window.deleteLater()
