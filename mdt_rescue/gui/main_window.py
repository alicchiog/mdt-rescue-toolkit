"""
Main application window for the mdt_rescue desktop GUI.

Implements the single-window, four-zone layout specified in
:file:`docs/c0_design.md` §1: input, action, progress, and output zones
stacked top to bottom in a :class:`QVBoxLayout`.

C.0.2 scaffold scope
--------------------
Every widget exists and the file pickers are functional, but the Recover
button does **not** start a recovery -- the :class:`RecoveryWorker` wiring
and the :func:`mdt_rescue.orchestrator.recover` call land in C.0.3. The
Cancel button and the Reveal/Show-log buttons are present but disabled in
this scaffold.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mdt_rescue import __version__
from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M, Profile

#: Profiles offered in the selector. v0.3.0a1 ships exactly one; adding more
#: later means extending this tuple, no layout change (design D2).
_AVAILABLE_PROFILES: tuple[Profile, ...] = (GH5S_FHD25_ALLI_200M,)

_PLACEHOLDER_OUTPUT = "No recovery yet."


class MainWindow(QMainWindow):
    """Single window with input, action, progress and output zones."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"MDT Rescue Toolkit {__version__}")
        self.setMinimumSize(640, 480)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_input_zone())
        layout.addWidget(self._make_separator())
        layout.addWidget(self._build_action_zone())
        layout.addWidget(self._make_separator())
        layout.addWidget(self._build_progress_zone())
        layout.addWidget(self._make_separator())
        layout.addWidget(self._build_output_zone())
        layout.addStretch(1)
        self.setCentralWidget(central)

        self._update_recover_enabled()

    @staticmethod
    def _make_separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _build_input_zone(self) -> QWidget:
        zone = QWidget()
        v = QVBoxLayout(zone)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        for profile in _AVAILABLE_PROFILES:
            self.profile_combo.addItem(profile.label, profile)
        profile_row.addWidget(self.profile_combo, stretch=1)
        v.addLayout(profile_row)

        v.addWidget(QLabel("Broken .MDT file:"))
        mdt_row = QHBoxLayout()
        self.mdt_edit = QLineEdit()
        self.mdt_edit.setPlaceholderText("/path/to/broken.mdt")
        self.mdt_edit.textChanged.connect(self._update_recover_enabled)
        mdt_browse = QPushButton("Browse…")
        mdt_browse.clicked.connect(self._browse_mdt)
        mdt_row.addWidget(self.mdt_edit, stretch=1)
        mdt_row.addWidget(mdt_browse)
        v.addLayout(mdt_row)

        v.addWidget(QLabel("Reference .MOV file:"))
        ref_row = QHBoxLayout()
        self.ref_edit = QLineEdit()
        self.ref_edit.setPlaceholderText("/path/to/sane_reference.mov")
        self.ref_edit.textChanged.connect(self._update_recover_enabled)
        ref_browse = QPushButton("Browse…")
        ref_browse.clicked.connect(self._browse_reference)
        ref_row.addWidget(self.ref_edit, stretch=1)
        ref_row.addWidget(ref_browse)
        v.addLayout(ref_row)

        return zone

    def _build_action_zone(self) -> QWidget:
        zone = QWidget()
        h = QHBoxLayout(zone)
        h.addStretch(1)
        self.recover_button = QPushButton("Recover")
        self.recover_button.clicked.connect(self._on_recover_clicked)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        h.addWidget(self.recover_button)
        h.addWidget(self.cancel_button)
        h.addStretch(1)
        return zone

    def _build_progress_zone(self) -> QWidget:
        zone = QWidget()
        v = QVBoxLayout(zone)
        self.progress_label = QLabel("Idle.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        v.addWidget(self.progress_label)
        v.addWidget(self.progress_bar)
        return zone

    def _build_output_zone(self) -> QWidget:
        zone = QWidget()
        v = QVBoxLayout(zone)
        v.addWidget(QLabel("Output"))
        self.output_label = QLabel(_PLACEHOLDER_OUTPUT)
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        v.addWidget(self.output_label)

        buttons = QHBoxLayout()
        self.reveal_button = QPushButton("Reveal in Finder")
        self.reveal_button.setEnabled(False)
        self.show_log_button = QPushButton("Show log")
        self.show_log_button.setEnabled(False)
        buttons.addWidget(self.reveal_button)
        buttons.addWidget(self.show_log_button)
        buttons.addStretch(1)
        v.addLayout(buttons)
        return zone

    def _browse_mdt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select broken .MDT file",
            "",
            "Panasonic MDT (*.mdt *.MDT);;All files (*)",
        )
        if path:
            self.mdt_edit.setText(path)

    def _browse_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select reference .MOV file",
            "",
            "Video (*.mov *.MOV *.mp4 *.MP4);;All files (*)",
        )
        if path:
            self.ref_edit.setText(path)

    def _update_recover_enabled(self) -> None:
        mdt = self.mdt_edit.text().strip()
        ref = self.ref_edit.text().strip()
        ready = (
            bool(mdt)
            and bool(ref)
            and Path(mdt).is_file()
            and Path(ref).is_file()
        )
        self.recover_button.setEnabled(ready)

    def _on_recover_clicked(self) -> None:
        # C.0.2 scaffold: recovery execution is intentionally NOT wired here.
        # Starting the RecoveryWorker and calling recover() lands in C.0.3.
        self.output_label.setText(
            "Recovery execution is not wired yet — this is the C.0.2 "
            "scaffold. The Recover action will start the recovery worker "
            "in C.0.3."
        )

    def _on_cancel_clicked(self) -> None:
        # Cancel is disabled in the C.0.2 scaffold (no running worker to
        # cancel). Wired in C.0.3 alongside RecoveryWorker.
        pass
