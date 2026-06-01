"""
Main application window for the mdt_rescue desktop GUI.

Implements the single-window, four-zone layout specified in
:file:`docs/c0_design.md` §1: input, action, progress, and output zones
stacked top to bottom in a :class:`QVBoxLayout`.

C.0.3 status
------------
Live. The Recover button starts a :class:`RecoveryWorker` that runs
:func:`mdt_rescue.orchestrator.recover` off the GUI thread; the worker's
signals drive the progress and output zones. Cancel requests cooperative
cancellation. Reveal in Finder / Show log act on the completed run via
``open`` (macOS).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

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
from mdt_rescue.orchestrator import ProgressStatus, Stage
from mdt_rescue.profiles import GH5S_FHD25_ALLI_200M, Profile
from mdt_rescue.gui.recovery_worker import RecoveryWorker

if TYPE_CHECKING:
    from mdt_rescue.orchestrator import ProgressEvent, RecoveryResult

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

        # Recovery state. Set before building zones: _update_recover_enabled
        # reads self._worker, and the file-edit textChanged wiring calls it.
        self._worker: RecoveryWorker | None = None
        self._stages_completed: int = 0
        self._total_stages: int = len(Stage)
        self._rescued_mov_path: Path | None = None
        self._log_path: Path | None = None

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
        self.reveal_button.clicked.connect(self._on_reveal_clicked)
        self.show_log_button = QPushButton("Show log")
        self.show_log_button.setEnabled(False)
        self.show_log_button.clicked.connect(self._on_show_log_clicked)
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
        # Never enable Recover while a worker is running, even if the user
        # edits a path field mid-run (textChanged fires this slot).
        if self._worker is not None:
            self.recover_button.setEnabled(False)
            return
        mdt = self.mdt_edit.text().strip()
        ref = self.ref_edit.text().strip()
        ready = (
            bool(mdt)
            and bool(ref)
            and Path(mdt).is_file()
            and Path(ref).is_file()
        )
        self.recover_button.setEnabled(ready)

    # ------------------------------------------------------------------
    # Recovery lifecycle
    # ------------------------------------------------------------------

    def _on_recover_clicked(self) -> None:
        mdt_path = Path(self.mdt_edit.text().strip())
        ref_path = Path(self.ref_edit.text().strip())
        profile = self.profile_combo.currentData() or GH5S_FHD25_ALLI_200M

        # Reset run state and the progress/output zones.
        self._stages_completed = 0
        self._rescued_mov_path = None
        self._log_path = None
        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting…")
        self.output_label.setStyleSheet("")
        self.output_label.setText("")
        self.reveal_button.setEnabled(False)
        self.show_log_button.setEnabled(False)

        # Build and wire a fresh worker (one per run, no reuse).
        self._worker = RecoveryWorker(mdt_path, ref_path, profile)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled_at.connect(self._on_cancelled_at)
        self._worker.finished.connect(self._on_worker_done)

        # Lock the UI for the duration of the run.
        self.recover_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._worker.start()

    def _on_cancel_clicked(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()
            self.progress_label.setText("Cancelling…")
        # Cancel stays enabled until the worker finishes (disabled in
        # _on_worker_done).

    def _on_progress(self, event: "ProgressEvent") -> None:
        if event.status == ProgressStatus.START:
            self.progress_label.setText(event.message)
        elif event.status == ProgressStatus.COMPLETE:
            self._stages_completed += 1
            pct = int(self._stages_completed / self._total_stages * 100)
            self.progress_bar.setValue(min(pct, 100))

    def _on_finished_ok(self, result: "RecoveryResult") -> None:
        self._rescued_mov_path = result.rescued_mov_path
        self._log_path = result.log_path
        self.progress_bar.setValue(100)
        self.progress_label.setText("Done.")
        self.output_label.setStyleSheet("")
        self.output_label.setText(f"✓ Rescued: {result.rescued_mov_path}")
        self.reveal_button.setEnabled(result.rescued_mov_path is not None)
        self.show_log_button.setEnabled(
            result.log_path is not None and Path(result.log_path).is_file()
        )

    def _on_failed(self, message: str, log_path: str) -> None:
        self._log_path = Path(log_path) if log_path else None
        self.progress_label.setText("Failed.")
        self.output_label.setStyleSheet("color: red;")
        self.output_label.setText(f"✗ Recovery failed: {message}")
        self.reveal_button.setEnabled(False)
        self.show_log_button.setEnabled(
            bool(log_path) and Path(log_path).is_file()
        )

    def _on_cancelled_at(self, stage: str) -> None:
        self.progress_label.setText("Cancelled.")
        self.output_label.setStyleSheet("")
        self.output_label.setText(
            f"⏹ Recovery cancelled at stage {stage}. "
            "Partial files left in the output directory."
        )

    def _on_worker_done(self) -> None:
        self._worker = None
        self.cancel_button.setEnabled(False)
        self._update_recover_enabled()

    def _on_reveal_clicked(self) -> None:
        if self._rescued_mov_path is not None:
            subprocess.run(
                ["open", "-R", str(self._rescued_mov_path)], check=False
            )

    def _on_show_log_clicked(self) -> None:
        if self._log_path is not None:
            subprocess.run(["open", str(self._log_path)], check=False)
