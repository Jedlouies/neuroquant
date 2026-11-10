"""Borderless role selection and case-queue dialogs."""

from pathlib import Path
import os

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QInputDialog, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from database.db_manager import (
    fetch_workflow_cases, remove_workflow_case, reorder_workflow_case,
    update_workflow_case,
)


ROOT = Path(__file__).resolve().parents[2]


def _svg_icon(name):
    return QIcon(str(ROOT / "assets" / "icons" / f"{name}.svg"))


def _center_on_screen(dialog):
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    frame = dialog.frameGeometry()
    frame.moveCenter(screen.availableGeometry().center())
    dialog.move(frame.topLeft())


def _add_close_control(dialog, layout, callback=None):
    row = QHBoxLayout()
    row.addStretch()
    close_button = QPushButton()
    close_button.setIcon(_svg_icon("close"))
    close_button.setObjectName("windowClose")
    close_button.setFixedSize(34, 30)
    close_button.setToolTip("Close window")
    close_button.setAccessibleName("Close window")
    close_button.clicked.connect(callback or dialog.reject)
    row.addWidget(close_button)
    layout.addLayout(row)


def _svg_pixmap(name, size=QSize(132, 132)):
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(ROOT / "assets" / "roles" / f"{name}.svg"))
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


_BORDERLESS_STYLE = """
    QDialog { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #122333,stop:0.55 #0b111b,stop:1 #101a29);
        color:#eaf2fa; border:none; border-radius:18px; }
    QLabel { background:transparent; color:#eaf2fa; }
    QLabel#heading { font-size:27px; font-weight:700; color:#f8fbff; }
    QLabel#subheading { font-size:14px; color:#a9bacb; }
    QLabel#cardTitle { font-size:19px; font-weight:700; color:#f2f7fb; }
    QLabel#cardNote { font-size:12px; color:#9eb0c2; }
    QFrame#roleCard { background:rgba(16,30,45,210); border:1px solid #30445b; border-radius:14px; }
    QFrame#roleCard[roleType="secretary"] { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(13,47,63,235),stop:1 rgba(17,25,42,235)); border-color:#256477; }
    QFrame#roleCard[roleType="doctor"] { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(34,33,67,235),stop:1 rgba(17,25,42,235)); border-color:#514c83; }
    QPushButton#roleCardButton { background:transparent; color:#eaf2fa; border:none; border-radius:13px;
        padding:18px; text-align:center; }
    QPushButton#roleCardButton:hover { background:rgba(34,211,238,24); border:1px solid #22d3ee; }
    QPushButton#primary { background:#087e8b; color:white; border:1px solid #1397a4; border-radius:7px;
        padding:9px 16px; font-weight:700; }
    QPushButton#primary:hover { background:#0995a2; }
    QPushButton#quiet { background:transparent; color:#9fb3c7; border:none; padding:8px; }
    QPushButton#quiet:hover { color:#22d3ee; }
    QPushButton#windowClose { background:transparent; color:#a9bacb; border:1px solid transparent; border-radius:6px; font-size:20px; padding:0; }
    QPushButton#windowClose:hover { background:#7f1d1d; color:#fff; border-color:#ef4444; }
    QPushButton { background:#172334; color:#eaf2fa; border:1px solid #33465d; border-radius:6px; padding:8px 12px; }
    QPushButton:hover { border-color:#22d3ee; }
    QTableWidget, QListWidget { background:#0f1722; color:#eaf2fa; gridline-color:#26384a; border:1px solid #26384a; }
    QHeaderView::section { background:#142131; color:#cbd5e1; border:0; padding:7px; }
"""


class WorkstationEntryDialog(QDialog):
    role_chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_role = None
        self.setWindowTitle("Choose NeuroQuant Workspace")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(800, 610)
        self.setStyleSheet(_BORDERLESS_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(38, 30, 38, 24)
        layout.setSpacing(10)
        _add_close_control(self, layout)
        head = QLabel("Choose your workspace")
        head.setObjectName("heading")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(head)
        subtitle = QLabel("Prepare and queue cases, or review a prepared segmentation.")
        subtitle.setObjectName("subheading")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(15)

        cards = QHBoxLayout()
        cards.setSpacing(18)
        layout.addLayout(cards, 1)
        for role, name, title, note in (
            ("secretary", "secretary", "Secretary", "Enter patient details, import scans,\nand organize the case queue."),
            ("doctor", "doctor", "Doctor", "Choose a prepared case and review\nthe MRI segmentation and measurements."),
        ):
            card = QFrame()
            card.setObjectName("roleCard")
            card.setProperty("roleType", role)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 10, 14, 14)
            card_layout.setSpacing(6)
            accent = QLabel()
            accent.setPixmap(_svg_pixmap(f"{name}-accent", QSize(300, 72)))
            accent.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(accent)
            image = QLabel()
            image.setPixmap(_svg_pixmap(name, QSize(146, 146)))
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(image)
            title_label = QLabel(title)
            title_label.setObjectName("cardTitle")
            title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(title_label)
            note_label = QLabel(note)
            note_label.setObjectName("cardNote")
            note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(note_label)
            button = QPushButton("Continue")
            button.setIcon(_svg_icon("arrow-right"))
            button.setObjectName("roleCardButton")
            button.clicked.connect(lambda _checked=False, selected=role: self._select(selected))
            card_layout.addWidget(button)
            cards.addWidget(card, 1)
        foot = QHBoxLayout()
        foot.addStretch()
        quit_button = QPushButton("Exit")
        quit_button.setIcon(_svg_icon("close"))
        quit_button.setObjectName("quiet")
        quit_button.clicked.connect(self.reject)
        foot.addWidget(quit_button)
        layout.addLayout(foot)

    def _select(self, role):
        self.selected_role = role
        self.role_chosen.emit(role)
        self.done(QDialog.DialogCode.Accepted)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_screen(self)


class SecretaryQueuePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_BORDERLESS_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(14)
        title_row = QHBoxLayout()
        title = QLabel("Doctors Queue")
        title.setObjectName("heading")
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Patient / Scan", "Status", "Case ID"])
        self.table.setColumnHidden(2, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(120)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        for label, icon_name, delta in (("Move up", "arrow-up", -1), ("Move down", "arrow-down", 1)):
            button = QPushButton(label)
            button.setIcon(_svg_icon(icon_name))
            button.clicked.connect(lambda _checked=False, direction=delta: self._move(direction))
            buttons.addWidget(button)
        change_status = QPushButton("Change status")
        change_status.setIcon(_svg_icon("check"))
        change_status.clicked.connect(self._change_status)
        buttons.addWidget(change_status)
        remove = QPushButton("Remove from doctor queue")
        remove.setIcon(_svg_icon("delete"))
        remove.clicked.connect(self._remove)
        buttons.addWidget(remove)
        buttons.addStretch()
        refresh = QPushButton("Refresh")
        refresh.setIcon(_svg_icon("refresh"))
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self.rows = fetch_workflow_cases()
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            case_id, patient_id, full_name, _age, _gender, _mri, _ct, _mask, status, position, _created = row
            label = f"{full_name} · {patient_id}" if full_name else patient_id
            for col, value in enumerate((label, status, case_id)):
                self.table.setItem(index, col, QTableWidgetItem(str(value)))

    def _selected_case(self):
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def _move(self, direction):
        row = self._selected_case()
        if row:
            reorder_workflow_case(row[0], direction)
            self.refresh()
            self.table.selectRow(max(0, min(self.table.rowCount() - 1, row[9] - 1 + direction)))

    def _change_status(self):
        row = self._selected_case()
        if not row:
            QMessageBox.information(self, "Change case status", "Select a case in the queue first.")
            return
        statuses = ["Importing", "Segmenting", "Import failed", "Segmentation failed"]
        mri_path, mask_path = row[5], row[7]
        if os.path.isdir(mri_path) and os.path.isfile(mask_path):
            statuses.insert(2, "Ready for doctor")
        current = row[8] if row[8] in statuses else statuses[0]
        status, accepted = QInputDialog.getItem(
            self, "Change case status", "Status:", statuses, statuses.index(current), False
        )
        if accepted and status != row[8]:
            update_workflow_case(row[0], status=status)
            self.refresh()
            for index, queued_case in enumerate(self.rows):
                if queued_case[0] == row[0]:
                    self.table.selectRow(index)
                    break

    def _remove(self):
        row = self._selected_case()
        if not row:
            return
        answer = QMessageBox.question(self, "Remove case", "Remove this case from the doctor queue?")
        if answer == QMessageBox.StandardButton.Yes:
            remove_workflow_case(row[0])
            self.refresh()

class DoctorCasePickerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Prepared Patient")
        self.setMinimumSize(570, 420)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(_BORDERLESS_STYLE)
        self.selected_case = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        _add_close_control(self, layout, self.reject)
        title_row = QHBoxLayout()
        title = QLabel("Prepared cases")
        title.setObjectName("heading")
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)
        hint = QLabel("Choose a case that has completed segmentation. The first queued case is next.")
        hint.setObjectName("subheading")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._open())
        layout.addWidget(self.list, 1)
        self.empty = QLabel("No cases are ready yet. Ask the secretary to import and prepare a scan.")
        self.empty.setObjectName("cardNote")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.setIcon(_svg_icon("refresh"))
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        buttons.addStretch()
        open_button = QPushButton("Open selected case")
        open_button.setIcon(_svg_icon("arrow-right"))
        open_button.setObjectName("primary")
        open_button.clicked.connect(self._open)
        buttons.addWidget(open_button)
        cancel = QPushButton("Cancel")
        cancel.setIcon(_svg_icon("close"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self.rows = []
        unavailable = 0
        for row in fetch_workflow_cases(ready_only=True):
            if not os.path.isdir(row[5]):
                update_workflow_case(row[0], status="Import failed")
                unavailable += 1
                continue
            if not os.path.isfile(row[7]):
                update_workflow_case(row[0], status="Segmentation failed")
                unavailable += 1
                continue
            self.rows.append(row)
        self.list.clear()
        for row in self.rows:
            _case_id, patient_id, full_name, age, gender, _mri, _ct, _mask, _status, position, _created = row
            label = f"{full_name or patient_id}  ·  {patient_id}  ·  {age}y · {gender}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list.addItem(item)
        if unavailable:
            self.empty.setText(
                f"{unavailable} case(s) were missing an MRI folder or saved segmentation and have been removed from the ready list. "
                "Ask the secretary to import and prepare them again."
            )
        else:
            self.empty.setText("No cases are ready yet. Ask the secretary to import and prepare a scan.")
        self.empty.setVisible(not self.rows)

    def _open(self):
        item = self.list.currentItem()
        if not item:
            QMessageBox.information(self, "Select a case", "Choose a prepared case first.")
            return
        self.selected_case = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_screen(self)
