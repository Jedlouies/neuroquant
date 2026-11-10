"""Compact, borderless scan upload workspace for radiologists."""
import os
import re
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QThread, Signal, Qt, QTimer, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QProgressBar,
    QHeaderView, QDialog, QDialogButtonBox, QLineEdit,
)

from database.supabase_radiologist import (
    BUCKET, delete_user_scans, is_scan_file, list_user_scans, scan_files_in_folder, upload_scan,
    user_scan_root, _has_internet_connection,
)


class _ConnectivityTask(QThread):
    checked = Signal(bool)

    def run(self):
        self.checked.emit(_has_internet_connection())


class _RadiologistTask(QThread):
    progress = Signal(int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, session, paths=None, folder_label="", base_dir=None):
        super().__init__()
        self.operation = operation
        self.session = session
        self.paths = paths or []
        self.folder_label = folder_label
        self.base_dir = Path(base_dir) if base_dir else (Path(self.paths[0]).parent if self.paths else None)

    def run(self):
        try:
            username = self.session["username"]
            display_name = self.session.get("display_name") or username
            if self.operation == "refresh":
                self.completed.emit(list_user_scans(display_name, legacy_username=username))
            elif self.operation == "delete":
                delete_user_scans(self.paths)
                self.completed.emit(None)
            elif self.operation == "upload":
                root = f"{user_scan_root(display_name)}/{_safe_segment(self.folder_label)}"
                for index, path in enumerate(self.paths, start=1):
                    self.progress.emit(-1, f"Uploading {index} of {len(self.paths)} · {Path(path).name}")
                    try:
                        relative = Path(path).relative_to(self.base_dir).as_posix()
                    except ValueError:
                        relative = Path(path).name
                    object_key = f"{root}/{relative}"
                    upload_scan(path, object_key)
                    self.progress.emit(round(index * 100 / len(self.paths)), f"Uploaded {index} of {len(self.paths)} · {Path(path).name}")
                self.completed.emit(len(self.paths))
        except Exception as exc:
            self.failed.emit(str(exc))


def _safe_segment(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")[:80] or "scans"


def _icon(name):
    path = Path(__file__).resolve().parents[2] / "assets" / "icons" / name
    return QIcon(str(path)) if path.is_file() else QIcon()


class RadiologistUploadWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self._task = None
        self._normal_geometry = None
        self.setWindowTitle("NeuroQuant")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(940, 650)
        self.setMinimumSize(720, 500)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        icon_path = Path(__file__).resolve().parents[2] / "assets" / "logo" / "colored.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setStyleSheet("""
            QMainWindow{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #122333,stop:.42 #0b0f17,stop:1 #111a29)}
            QWidget#root{background:transparent;color:#e2e8f0}
            QLabel{color:#e2e8f0;background:transparent}
            QLabel#title{font-size:21px;font-weight:700;color:#f8fafc}
            QLabel#subtle{color:#94a3b8}
            QFrame#topbar{background:rgba(7,13,22,100);border-bottom:1px solid #263548}
            QFrame#toolbar{background:transparent;border:none}
            QTreeWidget{background:rgba(7,13,22,180);color:#e2e8f0;border:1px solid #334155;selection-background-color:#075985;show-decoration-selected:1}
            QTreeWidget::item{padding:5px 3px}
            QTreeWidget::item:selected{background:#075985;color:#fff}
            QHeaderView::section{background:rgba(20,35,51,210);color:#cbd5e1;border:none;padding:9px;font-weight:700}
            QPushButton{background:#0284c7;color:#fff;border:none;border-radius:7px;padding:10px 16px;font-weight:700}
            QPushButton:hover{background:#0369a1}
            QPushButton:disabled{background:#334155;color:#94a3b8}
            QPushButton#secondary{background:rgba(30,41,59,130);color:#dbeafe;border:1px solid #334155}
            QPushButton#secondary:hover{background:#1e3a5f}
            QPushButton#danger{background:rgba(127,29,29,100);color:#fecaca;border:1px solid #7f1d1d}
            QPushButton#danger:hover{background:#991b1b}
        """)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 18)
        layout.setSpacing(14)

        top = QFrame()
        top.setObjectName("topbar")
        top.installEventFilter(self)
        top_row = QHBoxLayout(top)
        top_row.setContentsMargins(18, 6, 10, 6)
        self.title_label = QLabel("NeuroQuant")
        self.title_label.setStyleSheet("font-weight:700;color:#f8fafc")
        top_row.addWidget(self.title_label)
        top_row.addStretch(1)
        self.connection_icon = QLabel()
        self.connection_icon.setFixedSize(20, 20)
        top_row.addWidget(self.connection_icon)
        self.connection_label = QLabel("Checking…")
        self.connection_label.setObjectName("subtle")
        self.connection_label.setStyleSheet("color:#94a3b8;padding:0 10px")
        self.connection_label.setToolTip("Internet connectivity")
        top_row.addWidget(self.connection_label)
        minimize = QPushButton()
        minimize.setObjectName("secondary")
        minimize.setFixedSize(34, 30)
        minimize.setIcon(_icon("window-minimize.svg"))
        minimize.setIconSize(QSize(16, 16))
        minimize.setToolTip("Minimize")
        minimize.setAccessibleName("Minimize")
        minimize.clicked.connect(self.showMinimized)
        top_row.addWidget(minimize)
        self.maximize_button = QPushButton()
        self.maximize_button.setObjectName("secondary")
        self.maximize_button.setFixedSize(34, 30)
        self.maximize_button.setIcon(_icon("window-maximize.svg"))
        self.maximize_button.setIconSize(QSize(16, 16))
        self.maximize_button.setToolTip("Maximize")
        self.maximize_button.setAccessibleName("Maximize")
        self.maximize_button.clicked.connect(self._toggle_maximize)
        top_row.addWidget(self.maximize_button)
        close = QPushButton()
        close.setObjectName("secondary")
        close.setFixedSize(34, 30)
        close.setIcon(_icon("close.svg"))
        close.setIconSize(QSize(16, 16))
        close.setToolTip("Close")
        close.setAccessibleName("Close")
        close.clicked.connect(self.close)
        top_row.addWidget(close)
        layout.addWidget(top)

        content = QVBoxLayout()
        content.setContentsMargins(28, 14, 28, 0)
        content.setSpacing(12)
        heading = QLabel("Upload MRI or CT scans")
        heading.setObjectName("title")
        content.addWidget(heading)
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(0, 4, 0, 4)
        tools.setSpacing(8)
        self.upload_folder_button = QPushButton("Upload Scan Folder")
        self.upload_folder_button.setIcon(_icon("upload-folder.svg"))
        self.upload_folder_button.setIconSize(QSize(18, 18))
        self.upload_files_button = QPushButton("Upload Files")
        self.upload_files_button.setIcon(_icon("upload-files.svg"))
        self.upload_files_button.setIconSize(QSize(18, 18))
        self.refresh_button = QPushButton()
        self.refresh_button.setObjectName("secondary")
        self.refresh_button.setIcon(_icon("refresh.svg"))
        self.refresh_button.setIconSize(QSize(20, 20))
        self.refresh_button.setFixedSize(42, 42)
        self.refresh_button.setToolTip("Refresh uploads")
        self.refresh_button.setAccessibleName("Refresh uploads")
        self.delete_button = QPushButton()
        self.delete_button.setObjectName("danger")
        self.delete_button.setIcon(_icon("delete.svg"))
        self.delete_button.setIconSize(QSize(20, 20))
        self.delete_button.setFixedSize(42, 42)
        self.delete_button.setToolTip("Delete selected uploads")
        self.delete_button.setAccessibleName("Delete selected uploads")
        tools.addWidget(self.upload_folder_button)
        tools.addWidget(self.upload_files_button)
        tools.addWidget(self.refresh_button)
        tools.addStretch(1)
        tools.addWidget(self.delete_button)
        content.addWidget(toolbar)

        self.activity_bar = QProgressBar()
        self.activity_bar.setTextVisible(False)
        self.activity_bar.setFixedHeight(5)
        self.activity_bar.setStyleSheet("QProgressBar{background:#1e293b;border:0;border-radius:2px} QProgressBar::chunk{background:#06b6d4;border-radius:2px}")
        self.activity_bar.hide()
        content.addWidget(self.activity_bar)

        self.table = QTreeWidget()
        self.table.setColumnCount(4)
        self.table.setHeaderLabels(["Case folder / Scan file", "Cloud path", "Size", "Uploaded"])
        self.table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.table.setUniformRowHeights(True)
        content.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("subtle")
        bottom.addWidget(self.status, 1)
        self.sign_out_button = QPushButton("Sign out")
        self.sign_out_button.setObjectName("secondary")
        self.sign_out_button.setIcon(_icon("sign-out.svg"))
        self.sign_out_button.setIconSize(QSize(16, 16))
        bottom.addWidget(self.sign_out_button)
        content.addLayout(bottom)
        layout.addLayout(content, 1)

        self.upload_folder_button.clicked.connect(self._choose_folder)
        self.upload_files_button.clicked.connect(self._choose_files)
        self.refresh_button.clicked.connect(self.refresh_uploads)
        self.delete_button.clicked.connect(self._delete_selected)
        self.sign_out_button.clicked.connect(self.close)
        self._drag_origin = None
        self._automatic_refresh = False
        self.auto_refresh_timer = QTimer(self)
        self.auto_refresh_timer.setInterval(3000)
        self.auto_refresh_timer.timeout.connect(lambda: self.refresh_uploads(automatic=True))
        self.auto_refresh_timer.start()
        self._connectivity_task = None
        self.connectivity_timer = QTimer(self)
        self.connectivity_timer.setInterval(10000)
        self.connectivity_timer.timeout.connect(self._check_connectivity)
        self.connectivity_timer.start()
        QTimer.singleShot(0, self._check_connectivity)
        QTimer.singleShot(0, lambda: self.refresh_uploads(automatic=True))

    def _check_connectivity(self):
        if self._connectivity_task and self._connectivity_task.isRunning():
            return
        self._connectivity_task = _ConnectivityTask(self)
        self._connectivity_task.checked.connect(self._show_connectivity)
        self._connectivity_task.start()

    def _show_connectivity(self, online):
        icon = _icon("wifi-online.svg" if online else "wifi-offline.svg")
        self.connection_icon.setPixmap(icon.pixmap(QSize(19, 19)))
        self.connection_label.setText("Online" if online else "Offline")
        self.connection_label.setToolTip(
            "Internet connection detected" if online else "No internet connection detected"
        )

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            if self._normal_geometry:
                self.setGeometry(self._normal_geometry)
            self.maximize_button.setIcon(_icon("window-maximize.svg"))
            self.maximize_button.setToolTip("Maximize")
        else:
            self._normal_geometry = self.geometry()
            self.showMaximized()
            self.maximize_button.setIcon(_icon("window-restore.svg"))
            self.maximize_button.setToolTip("Restore")

    def eventFilter(self, watched, event):
        if watched.objectName() == "topbar":
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_origin is not None:
                if event.buttons() & Qt.MouseButton.LeftButton and not self.isMaximized():
                    self.move(event.globalPosition().toPoint() - self._drag_origin)
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_origin = None
                return True
        return super().eventFilter(watched, event)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select a folder containing one MRI or CT case")
        if not folder:
            return
        files = scan_files_in_folder(folder)
        if not files:
            QMessageBox.warning(self, "No scan files found", "The folder contains no supported medical scan files.")
            return
        folder_label = self._prompt_case_folder_name(Path(folder).name)
        if folder_label is None:
            return
        if not folder_label:
            self._show_borderless_message("Folder name required", "Enter a name for the cloud case folder before uploading.")
            return
        self._start_upload(files, folder_label, folder)

    def _choose_files(self):
        folder_label = self._prompt_case_folder_name()
        if folder_label is None:
            return
        if not folder_label:
            self._show_borderless_message("Folder name required", "Enter a name for the cloud case folder before selecting scan files.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select MRI or CT scan files", "",
            "Medical scan files (*.nii *.nii.gz *.nrrd *.mha *.mhd *.hdr *.img *.dcm *.dicom);;All files (*.*)",
        )
        if paths:
            paths = [path for path in paths if is_scan_file(path)]
            if not paths:
                QMessageBox.warning(self, "No supported scan files", "Choose NIfTI, NRRD, MetaImage, Analyze, or DICOM files.")
                return
            parents = {str(Path(path).parent).casefold() for path in paths}
            if len(parents) != 1:
                QMessageBox.warning(self, "Choose one case folder", "Select files from one folder at a time so their case structure stays together.")
                return
            self._start_upload(paths, folder_label, Path(paths[0]).parent)

    def _prompt_case_folder_name(self, default_name=""):
        dialog = QDialog(self)
        dialog.setWindowTitle("Create cloud case folder")
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setStyleSheet("""
            QDialog{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #122333,stop:.42 #0b0f17,stop:1 #111a29);border:1px solid #263548;border-radius:12px}
            QLabel{color:#e2e8f0;background:transparent}
            QLineEdit{background:rgba(7,13,22,180);color:#f8fafc;border:1px solid #334155;border-radius:6px;padding:9px}
            QLineEdit:focus{border-color:#06b6d4}
            QPushButton{background:#0284c7;color:#fff;border:0;border-radius:7px;padding:8px 18px;font-weight:700}
            QPushButton:hover{background:#0369a1}
            QPushButton#cancel{background:rgba(30,41,59,130);color:#dbeafe;border:1px solid #334155}
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Create cloud case folder")
        title.setStyleSheet("font-size:16px;font-weight:700")
        layout.addWidget(title)
        layout.addWidget(QLabel("Name the folder that will group the scan files in the cloud:"))
        name_input = QLineEdit()
        name_input.setPlaceholderText("For example: PatientID_StudyDate")
        name_input.setText(default_name)
        layout.addWidget(name_input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setObjectName("cancel")
        create = buttons.addButton("Continue", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(dialog.reject)
        create.clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return name_input.text().strip()

    def _start_upload(self, paths, label, base_dir):
        if self._task and self._task.isRunning():
            return
        self._automatic_refresh = False
        self._set_busy(True)
        self.activity_bar.setRange(0, 0)
        self.activity_bar.setValue(0)
        self.status.setText(f"Uploading {len(paths)} scan file(s) to { _safe_segment(label) }…")
        self._task = _RadiologistTask("upload", self.session, paths, label, base_dir)
        self._task.progress.connect(self._upload_progress)
        self._task.completed.connect(self._upload_complete)
        self._task.failed.connect(self._task_failed)
        self._task.start()

    def refresh_uploads(self, automatic=False):
        if self._task and self._task.isRunning():
            if not automatic and self._task.operation == "refresh" and self._automatic_refresh:
                self._automatic_refresh = False
                self._set_busy(True)
                self.activity_bar.setRange(0, 0)
                self.status.setText("Loading your uploads…")
            return
        if automatic and self.table.selectionModel().hasSelection():
            return
        self._automatic_refresh = automatic
        if not automatic:
            self._set_busy(True)
            self.activity_bar.setRange(0, 0)
            self.status.setText("Loading your uploads…")
        self._task = _RadiologistTask("refresh", self.session)
        self._task.completed.connect(self._show_uploads)
        self._task.failed.connect(self._task_failed)
        self._task.start()

    def _upload_progress(self, percent, message):
        if percent < 0:
            self.activity_bar.setRange(0, 0)
            self.status.setText(message)
        else:
            self.activity_bar.setRange(0, 100)
            self.activity_bar.setValue(percent)
            self.status.setText(f"{percent}% · {message}")

    def _show_uploads(self, items):
        was_automatic = self._automatic_refresh
        self._automatic_refresh = False
        signature = tuple((entry.get("path"), entry.get("size"), entry.get("updated_at")) for entry in items)
        if signature != getattr(self, "_last_upload_signature", None):
            self.table.clear()
            display_name = self.session.get("display_name") or self.session["username"]
            root_prefixes = (
                user_scan_root(display_name).rstrip("/") + "/",
                f"radiologist_uploads/{_safe_segment(self.session['username'])}/scans/",
            )
            groups = {}
            for entry in items:
                root_prefix = next((prefix for prefix in root_prefixes if entry["path"].startswith(prefix)), "")
                relative_path = entry["path"][len(root_prefix):] if root_prefix else entry["path"]
                folder_name, separator, _ = relative_path.partition("/")
                folder_name = folder_name if separator else "Scans"
                group = groups.setdefault(folder_name, {"files": [], "prefix": root_prefix})
                group["files"].append(entry)

            for folder_name in sorted(groups, key=str.casefold):
                files = groups[folder_name]["files"]
                root_prefix = groups[folder_name]["prefix"]
                total_size = sum(int(entry.get("size") or 0) for entry in files)
                latest = max((str(entry.get("updated_at") or "") for entry in files), default="")
                folder_item = QTreeWidgetItem([
                    folder_name, f"{root_prefix}{folder_name}/", f"{len(files)} file(s) · {_format_size(total_size)}",
                    latest[:19].replace("T", " "),
                ])
                folder_item.setData(1, Qt.ItemDataRole.UserRole, [entry["path"] for entry in files])
                folder_item.setToolTip(0, "Expand to view the scans in this folder")
                self.table.addTopLevelItem(folder_item)
                for entry in sorted(files, key=lambda item: item["name"].casefold()):
                    child = QTreeWidgetItem([
                        Path(entry["name"]).name, entry["path"], _format_size(int(entry.get("size") or 0)),
                        str(entry.get("updated_at", ""))[:19].replace("T", " "),
                    ])
                    child.setData(1, Qt.ItemDataRole.UserRole, entry["path"])
                    child.setToolTip(1, entry["path"])
                    folder_item.addChild(child)
                folder_item.setExpanded(False)
            self._last_upload_signature = signature
        display_name = self.session.get("display_name") or self.session["username"]
        path_label = f"{BUCKET}/{user_scan_root(display_name)}"
        if items:
            self.status.setText(f"{len(items)} file(s) · {path_label}")
        else:
            self.status.setText(f"No files found · {path_label} · check the bucket SELECT policy if uploads exist")
        if not was_automatic:
            self._set_busy(False)

    def _upload_complete(self, count):
        self._set_busy(False)
        self._show_borderless_message(
            "Upload complete", f"Uploaded {count} scan file(s) to the Supabase {BUCKET} bucket."
        )
        QTimer.singleShot(150, lambda: self.refresh_uploads(automatic=True))

    def _show_borderless_message(self, title, message):
        dialog = self._borderless_dialog(title, message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)
        dialog.layout().addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _borderless_dialog(self, title, message):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setStyleSheet("""
            QDialog{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #122333,stop:.42 #0b0f17,stop:1 #111a29);color:#e2e8f0;border:1px solid #263548;border-radius:12px}
            QLabel{color:#e2e8f0;background:transparent;font-size:12px}
            QPushButton{background:#0284c7;color:#fff;border:0;border-radius:7px;padding:8px 22px;font-weight:700;min-width:76px}
            QPushButton:hover{background:#0369a1}
            QPushButton#cancel{background:rgba(30,41,59,130);color:#dbeafe;border:1px solid #334155}
        """)
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setStyleSheet("font-size:16px;font-weight:700")
        body = QLabel(message)
        body.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(body)
        return dialog

    def _delete_selected(self):
        paths = []
        for item in self.table.selectedItems():
            selected_paths = item.data(1, Qt.ItemDataRole.UserRole)
            if isinstance(selected_paths, list):
                paths.extend(selected_paths)
            elif selected_paths:
                paths.append(selected_paths)
        paths = sorted(set(paths))
        if not paths:
            QMessageBox.information(self, "Select uploads", "Select one or more of your uploaded files first.")
            return
        dialog = self._borderless_dialog(
            "Delete selected uploads",
            f"Permanently delete {len(paths)} selected file(s) from the Supabase bucket? This cannot be undone.",
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("cancel")
        delete_button = buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
        delete_button.setStyleSheet("background:#991b1b;color:#fff;border:0;border-radius:7px;padding:8px 22px;font-weight:700;min-width:76px")
        delete_button.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog.layout().addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._set_busy(True)
        self.status.setText("Deleting selected files…")
        self._task = _RadiologistTask("delete", self.session, paths)
        self._task.completed.connect(lambda _result: QTimer.singleShot(0, self._delete_complete))
        self._task.failed.connect(self._task_failed)
        self._task.start()

    def _delete_complete(self):
        self._set_busy(False)
        self._show_borderless_message("Delete complete", "The selected scan files were removed from the Supabase bucket.")
        self.refresh_uploads(automatic=True)

    def _task_failed(self, message):
        was_automatic = self._automatic_refresh
        self._automatic_refresh = False
        if was_automatic:
            return
        self._set_busy(False)
        self.status.setText("Could not complete the cloud request.")
        self._show_borderless_message("Supabase request failed", message)

    def _set_busy(self, busy):
        if busy:
            self.activity_bar.show()
        else:
            self.activity_bar.hide()
        for button in (self.upload_folder_button, self.upload_files_button, self.refresh_button, self.delete_button, self.sign_out_button):
            button.setEnabled(not busy)


def _format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
