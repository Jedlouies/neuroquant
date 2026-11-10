import sys
import os
import csv
import zipfile
import tempfile
import platform
import glob
from pathlib import Path
from datetime import datetime
from collections import deque
import ctypes
import hashlib
import webbrowser
import textwrap
import json
import urllib.request
import urllib.error

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import numpy as np
import SimpleITK as sitk
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox, QDialog, QLineEdit,
    QGroupBox, QProgressBar, QSlider, QFrame, QComboBox, QScrollArea, QGridLayout, 
    QSizePolicy, QMenu, QMenuBar, QSystemTrayIcon, QDialogButtonBox, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QInputDialog
)
from PySide6.QtGui import (
    QIcon, QPixmap, QCloseEvent, QMouseEvent, QKeySequence, QColor, 
    QAction, QActionGroup, QPainter
)
from PySide6.QtCore import Qt, QThread, Signal, QSettings, QSize, QRectF, QTimer
from PySide6.QtSvg import QSvgRenderer

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from ui.slice_viewer import SliceViewer
from ui.three_d_viewer import ThreeDViewer, HelpInfoIcon, detect_gpu_label
from ui.theme_manager import ThemeManager, DARK, LIGHT
from ui.collab_chat import CollabChatPanel
from ui.brain_diagnostic_chat import BrainDiagnosticWorkstation, ICON_AI, ICON_CHATROOM, recolor_svg
from ml.worker import SegmentationWorker
from database.db_manager import (
    save_history_record, fetch_recent_history, fetch_patient_details, get_connection, get_user_id_by_username,
    get_local_user_profile, save_patient_record
)
from database.db_manager import DATA_DIR, save_segmentation_artifact, update_segmentation_artifact_sync
from image_processing.mri_loader import MultiParametricMRILoader, read_image_source
from database.supabase_radiologist import list_all_radiologist_scans, download_scan, is_scan_file


def _asset_path(*parts):
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root.joinpath(*parts)


def mobile_folder_name(segmentation_id):
    """Use the desktop segmentation ID as the stable mobile folder name."""
    value = str(segmentation_id or "").strip()
    if not value or any(not (char.isalnum() or char in "_-") for char in value):
        raise ValueError("The segmentation ID cannot be used as a mobile folder name.")
    return f"segmentation_{value}"


def _render_svg(path, size=QSize(18, 18)):
    renderer = QSvgRenderer(str(path))
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    if not renderer.isValid():
        return pixmap
    painter = QPainter(pixmap)
    view_size = renderer.defaultSize()
    if view_size.width() > 0 and view_size.height() > 0:
        scale = min(size.width() / view_size.width(), size.height() / view_size.height())
        width = view_size.width() * scale
        height = view_size.height() * scale
        target = QRectF((size.width() - width) / 2, (size.height() - height) / 2, width, height)
        renderer.render(painter, target)
    else:
        renderer.render(painter)
    painter.end()
    return pixmap


def _svg_icon(path, size=QSize(18, 18)):
    return QIcon(_render_svg(path, size))

from ui.dialogs import (
    resolve_colored_logo_path, AuthenticationDialog, ChangePasswordDialog, ChangeUsernameDialog,
    CustomTitleBar, VolumetricBarChart, FirstRunWelcomeDialog, LongitudinalComparisonDialog, 
    LayerColorPreferencesDialog, PatientIntakeDialog, ImportPatientDetailsDialog,
    PatientDetailsManagerDialog, SystemDatabaseDialog,
    AdjustmentPreferencesDialog, OthersPreferencesDialog, AboutNeuroQuantDialog, 
    ComputeDeviceDialog, SystemRequirementsDialog, UserManualDialog, 
    KeyboardShortcutsDialog, DiagnosticZoomDialog, UserProfileDialog, AIAPIKeyDialog, AIDataSelectionDialog, PatientProfilesDialog,
    ExitConfirmationDialog, ScanAssignmentDialog, ImportSourceDialog, UninstallFeedbackDialog, add_search_icon,
    open_documentation_site
)
from ui.font_scale import apply_font_scale
from credential_store import (
    read_api_key, save_api_key, read_mobile_folder_password,
    save_mobile_folder_password,
)
from app_config import get_setting

ACTIVE_USER = ""
SUPABASE_FOLDER_SERVICE_URL = get_setting("NEUROQUANT_FOLDER_SERVICE_URL")
SUPABASE_PUBLISHABLE_KEY = get_setting("SUPABASE_PUBLISHABLE_KEY")


def _call_folder_service(action, payload, timeout=30):
    request_body = json.dumps({"action": action, **payload}).encode("utf-8")
    request = urllib.request.Request(
        SUPABASE_FOLDER_SERVICE_URL,
        data=request_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": SUPABASE_PUBLISHABLE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
            message = detail.get("error") or detail.get("message")
        except Exception:
            message = None
        raise RuntimeError(message or f"Folder service returned HTTP {error.code}.") from None

class FolderLoaderWorker(QThread):
    progress = Signal(int, str)
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, folder_path: str, assigned_paths=None, detect_ct_dicom=True):
        super().__init__()
        self.folder_path = folder_path
        self.assigned_paths = dict(assigned_paths or {})
        self.detect_ct_dicom = bool(detect_ct_dicom)

    def run(self):
        try:
            self.progress.emit(15, f"Resolving NIfTI files for {os.path.basename(self.folder_path)}...")
            modality_paths = {key: path for key, path in self.assigned_paths.items() if key != "ct"}
            images = MultiParametricMRILoader.load_nifti_modalities(modality_paths)

            self.progress.emit(40, "Reading voxel spacing & coordinate system...")
            ref_img = images["flair"]
            voxel_spacing = ref_img.GetSpacing()
            full_flair = np.ascontiguousarray(sitk.GetArrayFromImage(ref_img).astype(np.float32))

            self.progress.emit(65, "Extracting channels (T1, T1ce, T2, FLAIR)...")
            modality_dict = {}
            for mod in ["t1", "t1ce", "t2", "flair"]:
                modality_dict[mod] = np.ascontiguousarray(sitk.GetArrayFromImage(images[mod]).astype(np.float32))
            scan_digest = hashlib.sha256()
            for mod in ("flair", "t1", "t1ce", "t2"):
                scan_digest.update(mod.encode("ascii"))
                scan_digest.update(memoryview(modality_dict[mod]))

            self.progress.emit(85, "Normalizing & assembling stacked volume...")
            channels = []
            for mod in ["t1", "t1ce", "t2", "flair"]:
                arr = modality_dict[mod].copy()
                mask = arr > 0
                if mask.any():
                    arr[mask] = (arr[mask] - arr[mask].mean()) / (arr[mask].std() + 1e-8)
                channels.append(arr)

            full_stacked = np.ascontiguousarray(np.stack(channels, axis=0))
            self.progress.emit(92, "Checking for an optional CT scan...")
            if self.assigned_paths.get("ct"):
                ct_path = self.assigned_paths["ct"]
                ct_volume = np.ascontiguousarray(
                    sitk.GetArrayFromImage(read_image_source(ct_path)).astype(np.float32)
                )
            elif self.detect_ct_dicom:
                ct_volume, ct_path = self.load_optional_ct_with_path(self.folder_path)
            else:
                ct_volume, ct_path = None, None
            self.progress.emit(100, "MRI data loaded successfully (100%)")

            self.finished.emit({
                "folder_path": self.folder_path,
                "source_scan_key": scan_digest.hexdigest(),
                "subject_name": os.path.basename(os.path.normpath(self.folder_path)),
                "images": images,
                "voxel_spacing": voxel_spacing,
                "full_flair": full_flair,
                "modality_dict": modality_dict,
                "full_stacked": full_stacked,
                "ct_volume": ct_volume,
                "ct_path": ct_path
            })
        except Exception as e:
            self.error.emit(str(e))

    @staticmethod
    def load_optional_ct_with_path(folder_path):
        candidates = [p for p in glob.glob(os.path.join(folder_path, "**", "*.nii*"), recursive=True)
                      if "ct" in os.path.basename(p).lower() and "seg" not in os.path.basename(p).lower()]
        for candidate in candidates:
            try:
                return (np.ascontiguousarray(
                    sitk.GetArrayFromImage(sitk.ReadImage(candidate)).astype(np.float32)
                ), candidate)
            except Exception:
                continue
        folders = []
        if "ct" in os.path.basename(os.path.normpath(folder_path)).lower():
            folders.append(folder_path)
        folders.extend(p for p in glob.glob(os.path.join(folder_path, "**", "*"), recursive=True)
                       if os.path.isdir(p) and "ct" in os.path.basename(p).lower())
        for candidate in folders:
            try:
                reader = sitk.ImageSeriesReader()
                series_ids = reader.GetGDCMSeriesIDs(candidate) or []
                for series_id in series_ids:
                    names = reader.GetGDCMSeriesFileNames(candidate, series_id)
                    if names:
                        reader.SetFileNames(names)
                        return (np.ascontiguousarray(sitk.GetArrayFromImage(reader.Execute()).astype(np.float32)), candidate)
            except Exception:
                continue
        return None, None

    @staticmethod
    def load_optional_ct(folder_path):
        """Compatibility helper for callers that only need the CT voxel array."""
        return FolderLoaderWorker.load_optional_ct_with_path(folder_path)[0]


class CloudCaseTask(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(self, operation, case_files=None, destination=None):
        super().__init__()
        self.operation = operation
        self.case_files = case_files or []
        self.destination = Path(destination) if destination else None

    def run(self):
        try:
            if self.operation == "list":
                self.completed.emit(list_all_radiologist_scans())
                return
            total_bytes = sum(max(0, int(entry.get("size") or 0)) for entry in self.case_files)
            completed_bytes = 0
            for index, entry in enumerate(self.case_files, start=1):
                relative = entry["relative"]
                self.progress.emit(
                    round(completed_bytes * 100 / total_bytes) if total_bytes else round((index - 1) * 100 / len(self.case_files)),
                    f"Downloading {index} of {len(self.case_files)} · {Path(relative).name}",
                )
                target = self.destination.joinpath(*relative.replace("\\", "/").split("/"))
                if self.destination.resolve() not in target.resolve().parents:
                    raise ValueError("Cloud case contains an invalid file path.")
                prior_bytes = completed_bytes
                last_file_bytes = 0

                def report_file_progress(done, expected):
                    nonlocal last_file_bytes
                    last_file_bytes = done
                    current_total = total_bytes or sum(max(0, int(item.get("size") or 0)) for item in self.case_files)
                    numerator = prior_bytes + done
                    percent = round(numerator * 100 / current_total) if current_total else round((index - 1 + min(done / max(expected, 1), 1)) * 100 / len(self.case_files))
                    self.progress.emit(min(percent, 99), f"Downloading {index} of {len(self.case_files)} · {Path(relative).name}")

                download_scan(
                    entry["path"], target, progress_callback=report_file_progress,
                    expected_size=int(entry.get("size") or 0),
                )
                completed_bytes += max(last_file_bytes, int(entry.get("size") or 0))
            self.progress.emit(100, "Download complete")
            self.completed.emit(str(self.destination))
        except Exception as exc:
            self.failed.emit(str(exc))


class CloudCaseDialog(QDialog):
    """Select a radiologist case stored in Supabase and download it for import."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from Cloud")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.resize(620, 440)
        if parent is not None:
            self.move(parent.frameGeometry().center() - self.rect().center())
        self.setStyleSheet("""
            QDialog{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #122333,stop:.42 #0b0f17,stop:1 #111a29);color:#e2e8f0;border:1px solid #263548;border-radius:12px}
            QLabel{color:#e2e8f0;background:transparent}
            QTreeWidget{background:rgba(7,13,22,180);color:#e2e8f0;border:1px solid #334155;selection-background-color:#075985;show-decoration-selected:1}
            QTreeWidget::item{padding:6px 4px}
            QTreeWidget::item:selected{background:#075985;color:#fff}
            QHeaderView::section{background:rgba(20,35,51,210);color:#cbd5e1;border:none;padding:8px;font-weight:700}
            QPushButton{background:#0284c7;color:#fff;border:0;border-radius:7px;padding:8px 18px;font-weight:700}
            QPushButton:hover{background:#0369a1}
            QPushButton#cancel{background:rgba(30,41,59,130);color:#dbeafe;border:1px solid #334155}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        heading = QLabel("Import from Cloud")
        heading.setStyleSheet("font-size:16px;font-weight:700")
        layout.addWidget(heading)
        self.hint = QLabel("Loading cloud folders…")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setColumnCount(3)
        self.folder_tree.setHeaderLabels(["Folder", "Date Uploaded", "Uploaded by"])
        self.folder_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.folder_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.folder_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.folder_tree.setUniformRowHeights(True)
        layout.addWidget(self.folder_tree, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.hide()
        layout.addWidget(self.progress)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = self.buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("cancel")
        self.buttons.rejected.connect(self.reject)
        self.import_button.clicked.connect(self._download_selected)
        layout.addWidget(self.buttons)
        self.folder_tree.itemSelectionChanged.connect(self._selection_changed)
        self._groups = {}
        self._case_items = {}
        self.imported_folder = None
        self._task = CloudCaseTask("list")
        self._task.completed.connect(self._populate)
        self._task.failed.connect(self._failed)
        self._task.start()

    @staticmethod
    def _identity(path):
        parts = path.split("/")
        if len(parts) < 5 or parts[0] not in ("radiologist", "radiologist_uploads") or parts[2] != "scans":
            return None
        return "/".join(parts[:4]), parts[1], parts[3], "/".join(parts[4:])

    def _populate(self, items):
        for item in items:
            if not is_scan_file(item.get("name", "")):
                continue
            identity = self._identity(item.get("path", ""))
            if not identity:
                continue
            key, username, case_name, relative = identity
            group = self._groups.setdefault(key, {"username": username, "case": case_name, "files": []})
            group["files"].append({
                "path": item["path"], "relative": relative,
                "size": item.get("size", 0), "updated_at": item.get("updated_at", ""),
            })
        self.folder_tree.clear()
        for key, group in sorted(self._groups.items(), key=lambda pair: pair[0].casefold()):
            uploaded = max((str(entry.get("updated_at") or "") for entry in group["files"]), default="")
            uploaded = uploaded[:19].replace("T", " ") or "—"
            case_item = QTreeWidgetItem([group["case"], uploaded, group["username"]])
            case_item.setData(0, Qt.ItemDataRole.UserRole, key)
            case_item.setToolTip(0, "Select this case folder to import all its scan files together")
            self.folder_tree.addTopLevelItem(case_item)
            self._case_items[key] = case_item
        self.hint.setText("Select a case folder to import all its scan files." if self._groups else "No scan folders found. Check the bucket SELECT policy for radiologist uploads.")
        self._selection_changed()

    def _selection_changed(self):
        selected = self.folder_tree.selectedItems()
        key = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        group = self._groups.get(key)
        self.import_button.setEnabled(bool(group and group["files"]))

    def _download_selected(self):
        selected = self.folder_tree.selectedItems()
        key = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        group = self._groups.get(key)
        if not group:
            return
        destination = Path(DATA_DIR) / "cloud_imports" / group["username"] / group["case"]
        self.import_button.setEnabled(False)
        self.folder_tree.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self._task = CloudCaseTask("download", group["files"], destination)
        self._task.progress.connect(self._download_progress)
        self._task.completed.connect(self._downloaded)
        self._task.failed.connect(self._failed)
        self._task.start()

    def _downloaded(self, folder):
        self.imported_folder = folder
        self.accept()

    def _download_progress(self, percent, message):
        self.progress.setValue(percent)
        self.hint.setText(message)

    def _failed(self, message):
        self.progress.hide()
        self.folder_tree.setEnabled(True)
        self.import_button.setEnabled(bool(self._groups))
        self.hint.setText("Cloud import failed.")
        QMessageBox.warning(self, "Cloud import failed", message)


class CloudGifSyncWorker(QThread):
    completed = Signal(object, object)

    def __init__(self, segmentation_id, patient_id, local_paths, owner_key="", cloud_password="", folder_password=""):
        super().__init__()
        self.segmentation_id = segmentation_id
        self.patient_id = patient_id
        self.local_paths = list(local_paths)
        self.owner_key = owner_key
        self.cloud_password = cloud_password
        self.folder_password = folder_password
        self.folder_name = mobile_folder_name(segmentation_id)

    def run(self):
        uploaded, failures = {}, {}
        try:
            from database.firestore_manager import get_db
            db = get_db()
            if not self.owner_key or not self.cloud_password or not self.folder_password:
                raise RuntimeError("A desktop account and folder password are required for mobile sharing.")
            object_path = f"segmentation_gifs/{self.owner_key}/{self.folder_name}"
            password_payload = {
                "username": self.owner_key, "accountPassword": self.cloud_password,
                "folderPassword": self.folder_password, "objectPath": object_path,
            }
            password_result = _call_folder_service("setSegmentationFolderPassword", password_payload)
            if not password_result.get("ok"):
                raise RuntimeError("The folder service did not save this password to Firestore.")
            for path in self.local_paths:
                try:
                    name = Path(path).name
                    payload = {
                        "username": self.owner_key, "password": self.cloud_password,
                        "segmentationId": str(self.segmentation_id), "folderName": self.folder_name,
                        "fileName": name,
                    }
                    result = _call_folder_service("createDesktopUploadUrl", payload)
                    signed_url = result.get("signedUrl")
                    if not signed_url:
                        raise RuntimeError("The upload service did not return a scoped URL.")
                    with open(path, "rb") as source:
                        put = urllib.request.Request(signed_url, data=source.read(), method="PUT", headers={"Content-Type": "image/gif", "x-upsert": "true"})
                    try:
                        with urllib.request.urlopen(put, timeout=120) as response:
                            response.read()
                    except urllib.error.HTTPError as error:
                        raise RuntimeError(f"Supabase rejected GIF upload with HTTP {error.code}.") from None
                    storage = {"bucket": result.get("bucket", "neuro"), "object_path": result.get("objectPath"), "name": name, "size": os.path.getsize(path)}
                    uploaded[path] = storage
                    if not db:
                        failures[path] = "Supabase upload succeeded, but Firestore is unavailable."
                        continue
                    artifact_type = f"MRI GIF ({Path(path).stem.title()})"
                    db.collection("segmentation_artifacts").document(
                        f"{self.segmentation_id}_{Path(path).stem}"
                    ).set({
                        "segmentation_id": self.segmentation_id,
                        "owner_id": self.owner_key,
                        "folder_name": self.folder_name.removeprefix("segmentation_"),
                        "patient_id": self.patient_id,
                        "artifact_type": artifact_type,
                        "local_path": os.path.abspath(path),
                        "supabase_bucket": storage["bucket"],
                        "supabase_object_path": storage["object_path"],
                        "file_name": storage["name"],
                        "created_at": datetime.now().astimezone().isoformat()
                    }, merge=True)
                except Exception as exc:
                    failures[path] = str(exc)
        except Exception as exc:
            failures.update({path: str(exc) for path in self.local_paths})
        self.completed.emit(uploaded, failures)

class SegmentationGifWorker(QThread):
    completed = Signal(str, str, str, list)
    failed = Signal(str)

    def __init__(self, volume, mask, output_dir, segmentation_id, patient_id, modality_dict=None, ct_volume=None):
        super().__init__()
        self.volume = np.array(volume, copy=True)
        self.mask = np.array(mask, copy=True)
        self.output_dir = output_dir
        self.segmentation_id = segmentation_id
        self.patient_id = patient_id
        self.modality_dict = modality_dict or {}
        self.ct_volume = np.array(ct_volume, copy=True) if ct_volume is not None else None

    def run(self):
        try:
            from PIL import Image
            os.makedirs(self.output_dir, exist_ok=True)
            outputs = []
            views = (("axial", 0), ("coronal", 1), ("sagittal", 2))

            def save_volume_gif(volume, mask, file_name):
                vol_array = np.asarray(volume)
                if vol_array.ndim != 3 or not vol_array.size:
                    return
                mask_array = np.asarray(mask) if mask is not None else None
                if mask_array is not None and mask_array.shape != vol_array.shape:
                    return
                for view_name, axis in views:
                    slices = np.moveaxis(vol_array, axis, 0)
                    masks = np.moveaxis(mask_array, axis, 0) if mask_array is not None else None
                    indices = np.linspace(0, len(slices) - 1, min(len(slices), 40), dtype=int)
                    frames = []
                    for idx in indices:
                        plane = np.nan_to_num(slices[idx], copy=False)
                        nonzero = plane[plane > 0]
                        low, high = np.percentile(nonzero, (1, 99)) if nonzero.size else (float(plane.min()), float(plane.max()))
                        gray = np.clip((plane - low) / max(float(high - low), 1e-6) * 255, 0, 255).astype(np.uint8)
                        rgb = np.repeat(gray[..., None], 3, axis=2)
                        if masks is not None:
                            labels = masks[idx]
                            rgb[labels == 1] = (255, 60, 60)
                            rgb[labels == 2] = (55, 220, 130)
                        frame = Image.fromarray(rgb)
                        frame.thumbnail((320, 320), Image.Resampling.LANCZOS)
                        frames.append(frame)
                    if not frames:
                        continue
                    output_path = os.path.join(self.output_dir, f"{file_name}_{view_name}.gif")
                    frames[0].save(output_path, save_all=True, append_images=frames[1:], duration=100, loop=0, optimize=True)
                    outputs.append(output_path)

            # Export raw previews for every acquired sequence. The segmentation
            # overlay is generated only for FLAIR, the model's input volume.
            for name in ("flair", "t1", "t2", "t1ce"):
                volume = self.modality_dict.get(name)
                if volume is None and name == "flair":
                    volume = self.volume
                if volume is None:
                    continue
                save_volume_gif(volume, None, f"raw_{name}")
                if name == "flair":
                    save_volume_gif(volume, self.mask, f"segmented_{name}")

            # CT is shown as its own unsegmented series; MRI mask coordinates are not applied to CT.
            if self.ct_volume is not None:
                save_volume_gif(self.ct_volume, None, "raw_ct")

            # Keep the original filenames for existing desktop/mobile clients.
            save_volume_gif(self.volume, self.mask, "legacy")
            for path in list(outputs):
                if os.path.basename(path).startswith("legacy_"):
                    legacy_path = os.path.join(self.output_dir, os.path.basename(path).replace("legacy_", ""))
                    import shutil
                    shutil.copy2(path, legacy_path)
                    outputs.append(legacy_path)
            self.completed.emit(self.segmentation_id, self.patient_id, self.output_dir, outputs)
        except Exception as exc:
            self.failed.emit(str(exc))

class MobileFolderPasswordDialog(QDialog):
    """Set a reusable password for opening mobile segmentation folders."""

    def __init__(self, username, cloud_password="", folders=None, parent=None):
        super().__init__(parent)
        self.username = username or "default"
        self.cloud_password = cloud_password
        self.folders = list(folders or [])
        self.setWindowTitle("Mobile Folder Password")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setFixedWidth(430)
        self.setStyleSheet("""
            QDialog { background: #101820; border: 1px solid #334155; border-radius: 10px; }
            QLabel { color: #e2e8f0; font-size: 11px; background: transparent; border: none; }
            QLabel#hint { color: #94a3b8; }
            QLineEdit { background: #091522; color: #e2e8f0; border: 1px solid #31536d; border-radius: 6px; padding: 9px; }
            QPushButton { background: #12374b; color: #e2e8f0; border: 1px solid #31536d; border-radius: 6px; padding: 7px 14px; }
            QPushButton:hover { background: #164a63; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel("Mobile Folder Password")
        heading.setStyleSheet("font-size: 13px; font-weight: 700; color: #ffffff;")
        layout.addWidget(heading)
        protected = sum(1 for folder in self.folders if folder.get("hasPassword"))
        if self.folders:
            status_text = f"Cloud status: a password is set for {protected} of {len(self.folders)} synced folders. Saving here applies the entered password to all of them."
        else:
            status_text = "No synced GIF folders were found in the cloud yet. This password will be used for new segmentation folders."
        hint = QLabel(status_text)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("At least 6 characters")
        self.password_input.setText(read_mobile_folder_password(self.username))
        layout.addWidget(self.password_input)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save / Change Password")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _save(self):
        password = self.password_input.text()
        if len(password) < 6:
            QMessageBox.warning(self, "Password too short", "Use at least 6 characters for the mobile folder password.")
            return
        try:
            if self.folders:
                result = _call_folder_service("setSegmentationFolderPasswords", {
                    "username": self.username,
                    "accountPassword": self.cloud_password,
                    "folderPassword": password,
                    "objectPaths": [folder["objectPath"] for folder in self.folders],
                })
                if not result.get("ok") or int(result.get("updated", 0)) != len(self.folders):
                    raise RuntimeError("The cloud did not confirm updating every synced folder password.")
            save_mobile_folder_password(self.username, password)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save password", str(exc))
            return
        self.accept()


class NeuroQuantMainWindow(QMainWindow):
    def __init__(self, authenticated_user="admin", cloud_password=""):
        super().__init__()
        self._first_run_welcome = None
        global ACTIVE_USER
        ACTIVE_USER = authenticated_user or "admin"
        self.authenticated_user = ACTIVE_USER
        self._cloud_password = cloud_password

        if platform.system() == "Windows":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NeuroQuant.NeuroOncology.Platform.6.0")
            except Exception:
                pass

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setWindowTitle("NeuroQuant - Brain Tumor Segmentation & Quantification")
        self.resize(1440, 940)
        
        self.logo_path = resolve_colored_logo_path()
        app_icon_path = Path(__file__).resolve().parents[2] / "assets" / "logo" / "colored.ico"
        if app_icon_path.exists() or self.logo_path:
            app_icon = QIcon(str(app_icon_path)) if app_icon_path.exists() else QIcon(self.logo_path)
            self.setWindowIcon(app_icon)
            QApplication.instance().setWindowIcon(app_icon)
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip("NeuroQuant — Chat Room notifications")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

        self.current_subject_name = "None Loaded"
        self.attending_physician = get_local_user_profile(ACTIVE_USER)[0] or ACTIVE_USER
        self.patient_info = {
            "patient_name": "None Loaded",
            "patient_age": "--",
            "patient_gender": "--",
            "radiologist": self.attending_physician,
            "full_name": "", "date_of_birth": "", "phone": "", "email": "", "address": "",
            "emergency_contact": "", "emergency_phone": "", "medical_history": "",
            "allergies": "", "current_medications": ""
        }
        self.patient_metadata_confirmed = False
        self.import_queue = deque()
        self.is_processing_queue = False

        self.full_flair = None
        self.modality_dict = {}
        self.voxel_spacing = (1.0, 1.0, 1.0)
        self.full_stacked_mri = None
        self.full_mask = None
        self.ct_volume = None
        self.gif_workers = []
        self.cloud_sync_workers = []
        self.lobe_masks = {}
        self.current_metrics = {}
        try:
            self.ai_api_key = read_api_key()
        except Exception:
            self.ai_api_key = ""

        self.init_ui()
        self.refresh_history_dropdown()

        for viewer in self.all_slice_viewers:
            viewer.display_label.setProperty("nq_keep_dark", True)
        theme_manager = ThemeManager.instance()
        theme_manager.attach(QApplication.instance())
        theme_manager.theme_changed.connect(lambda _theme: apply_font_scale(QApplication.instance()))
        self._sync_theme_actions(theme_manager.current)
        apply_font_scale(QApplication.instance())

    def closeEvent(self, event: QCloseEvent):
        if ExitConfirmationDialog(self).exec() == QDialog.DialogCode.Accepted:
            if hasattr(self, 'chat_panel') and self.chat_panel:
                try:
                    if hasattr(self.chat_panel, 'user_listener') and self.chat_panel.user_listener:
                        self.chat_panel.user_listener.unsubscribe()
                        self.chat_panel.user_listener = None
                    if hasattr(self.chat_panel, 'chat_listener') and self.chat_panel.chat_listener:
                        self.chat_panel.chat_listener.unsubscribe()
                        self.chat_panel.chat_listener = None
                    
                    from database.firestore_manager import update_user_status
                    update_user_status(
                        self.chat_panel.user_id, 
                        self.chat_panel.display_name, 
                        self.chat_panel.designation, 
                        False
                    )
                except Exception:
                    pass

            if hasattr(self, 'diagnostic_ai') and self.diagnostic_ai:
                self.diagnostic_ai.close()
            
            event.accept()
            QApplication.quit()
        else:
            event.ignore()
                
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def set_theme(self, theme: str):
        ThemeManager.instance().apply(theme)

    def apply_sidebar_theme(self, light: bool):
        colors = {
            "sidebarSectionTitle": "#0f172a" if light else "#e2e8f0",
            "sidebarMetaLabel": "#64748b" if light else "#888888",
            "sidebarMetaValue": "#0e7490" if light else "#00e5ff",
            "sidebarMetricStrong": "#0f172a" if light else "#ffffff",
            "sidebarMetricHighlight": "#dc2626" if light else "#ff5252",
            "sidebarMetricName": "#475569" if light else "#b0b0b0",
            "sidebarMetricValue": "#334155" if light else "#e0e0e0",
            "sidebarDiagnosticPrimary": "#0f172a" if light else "#ffffff",
            "sidebarDiagnosticSecondary": "#334155" if light else "#e0e0e0",
        }
        for label in self.findChildren(QLabel):
            role = label.objectName()
            if role not in colors:
                continue
            size = "11px" if role in ("sidebarSectionTitle", "sidebarMetricStrong", "sidebarMetricHighlight", "sidebarDiagnosticPrimary") else "10px"
            weight = "bold" if role in ("sidebarSectionTitle", "sidebarMetaValue", "sidebarMetricStrong", "sidebarMetricHighlight", "sidebarMetricValue", "sidebarDiagnosticPrimary") else "normal"
            label.setStyleSheet(f"color:{colors[role]};font-size:{size};font-weight:{weight};background:transparent;border:none")

    def _set_run_available(self, available: bool):
        if hasattr(self, "btn_run"):
            self.btn_run.setEnabled(bool(available))

    def _sync_theme_actions(self, theme: str):
        self.act_theme_light.setChecked(theme == LIGHT)
        self.act_theme_dark.setChecked(theme != LIGHT)
        if hasattr(self, "act_ai_menu"):
            if theme == LIGHT:
                self.act_ai_menu.setIcon(_svg_icon(recolor_svg(ICON_AI, "ai_menu_light", "#334155")))
                self.act_chat_menu.setIcon(_svg_icon(recolor_svg(ICON_CHATROOM, "chat_menu_light", "#334155")))
                if hasattr(self, "act_website_menu"):
                    website_svg = recolor_svg(self.website_icon_path, "website_menu_light", "#334155")
                    self.act_website_menu.setIcon(_svg_icon(website_svg))
            else:
                self.act_ai_menu.setIcon(_svg_icon(ICON_AI))
                self.act_chat_menu.setIcon(_svg_icon(ICON_CHATROOM))
                if hasattr(self, "act_website_menu"):
                    website_svg = recolor_svg(self.website_icon_path, "website_menu_dark", "#38bdf8")
                    self.act_website_menu.setIcon(_svg_icon(website_svg))
        self.apply_sidebar_theme(theme == LIGHT)

    def init_ui(self):
        root_container = QWidget(self)
        root_container.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #111a29, stop:0.28 #0b0f17, stop:0.72 #0b0f17, stop:1 #101825);")
        self.setCentralWidget(root_container)
        root_layout = QVBoxLayout(root_container)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = CustomTitleBar(self)
        root_layout.addWidget(self.title_bar)
        
        global ACTIVE_USER
        self.chat_panel = CollabChatPanel(ACTIVE_USER, self)
        self.chat_panel.network_status_changed.connect(self.update_network_status_text)
        self.chat_panel.new_message_received.connect(self.show_chat_notification)

        # AI chat window
        ai_user_id = get_user_id_by_username(ACTIVE_USER) or ACTIVE_USER
        self.diagnostic_ai = BrainDiagnosticWorkstation(user_id=ai_user_id)
        self.diagnostic_ai.set_api_key(self.ai_api_key)
        self.diagnostic_ai.configuration_requested.connect(self.open_ai_key_settings)
        self.diagnostic_ai.database_attachment_requested.connect(self.attach_database_data_to_ai)
        self.chat_panel.network_status_changed.connect(self.diagnostic_ai.set_online)
        self.diagnostic_ai.set_online(self.chat_panel.is_online)

        self.menu_bar = QMenuBar(self)
        self.menu_bar.setStyleSheet("""
            QMenuBar {
                background-color: #0b0f17; color: #e2e8f0; font-size: 11px;
                border-bottom: 1px solid #1e293b; padding: 2px;
            }
            QMenuBar::item { background: transparent; padding: 4px 12px; border-radius: 3px; }
            QMenuBar::item:selected { background-color: #1e293b; color: #00e5ff; }
            QMenu { background-color: #161f2e; color: #e2e8f0; border: 1px solid #334155; }
            QMenu::item { padding: 7px 22px 7px 8px; }
            QMenu::icon { margin-left: 7px; margin-right: 8px; }
            QMenu::item:selected { background-color: #00838f; color: #ffffff; }
        """)

        file_menu = self.menu_bar.addMenu("File")
        self.file_menu = file_menu
        act_import = QAction(_svg_icon(_asset_path("assets", "icons", "import-scan.svg")), "Import MRI/CT", self)
        act_import.setShortcut(QKeySequence("Ctrl+I"))
        act_import.triggered.connect(self.prompt_open_folder)
        file_menu.addAction(act_import)
        self.act_import = act_import
        act_import_cloud = QAction(_svg_icon(_asset_path("assets", "icons", "cloud-download.svg")), "Import from Cloud", self)
        act_import_cloud.triggered.connect(self.prompt_import_from_cloud)
        file_menu.addAction(act_import_cloud)
        self.act_import_cloud = act_import_cloud
        file_menu.addSeparator()

        export_submenu = file_menu.addMenu("Export")
        act_pdf = QAction("Export Full Clinical Report (PDF)", self)
        act_pdf.triggered.connect(self.export_clinical_dossier)
        export_submenu.addAction(act_pdf)
        act_3d = QAction("Export 3D Render Views Only (PNG)", self)
        act_3d.triggered.connect(self.export_3d_images_only)
        export_submenu.addAction(act_3d)
        act_slices = QAction("Export 2D Orthogonal Slices Only (PNG)", self)
        act_slices.triggered.connect(self.export_slices_only)
        export_submenu.addAction(act_slices)
        act_csv = QAction("Export Metrics & Volumetrics (CSV)", self)
        act_csv.triggered.connect(self.export_metrics_csv)
        export_submenu.addAction(act_csv)
        act_bundle = QAction("Export Full Diagnostic Package (ZIP)", self)
        act_bundle.triggered.connect(self.export_full_bundle)
        export_submenu.addAction(act_bundle)

        file_menu.addSeparator()
        act_exit = QAction("Exit", self)
        act_exit.setShortcut(QKeySequence("Ctrl+Q"))
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        db_menu = self.menu_bar.addMenu("Database")
        self.db_menu = db_menu
        act_view_db = QAction(_svg_icon(_asset_path("assets", "icons", "database.svg")), "View Database", self)
        act_view_db.triggered.connect(self.open_patient_database)
        db_menu.addAction(act_view_db)
        act_patient_details = QAction("Enter Full Patient Details…", self)
        act_patient_details.triggered.connect(self.open_patient_details_manager)
        db_menu.addAction(act_patient_details)

        view_menu = self.menu_bar.addMenu("View")
        self.view_menu = view_menu
        act_toggle_sidebar = QAction("Toggle Control Sidebar", self)
        act_toggle_sidebar.setShortcut(QKeySequence("Ctrl+B"))
        act_toggle_sidebar.triggered.connect(self.toggle_sidebar)
        view_menu.addAction(act_toggle_sidebar)
        act_fullscreen = QAction("Toggle Fullscreen Mode", self)
        act_fullscreen.setShortcut(QKeySequence("F11"))
        act_fullscreen.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(act_fullscreen)

        view_menu.addSeparator()
        self.act_hide_layers = QAction("Show 3D Layers Legend", self, checkable=True)
        self.act_hide_layers.setChecked(True)
        self.act_hide_layers.triggered.connect(lambda chk: self.three_d_viewer.set_visibility_option("layers", chk))
        view_menu.addAction(self.act_hide_layers)
        self.act_hide_ruler = QAction("Show Top Scale Ruler", self, checkable=True)
        self.act_hide_ruler.setChecked(True)
        self.act_hide_ruler.triggered.connect(lambda chk: self.three_d_viewer.set_visibility_option("ruler", chk))
        view_menu.addAction(self.act_hide_ruler)
        self.act_hide_gizmo = QAction("Show XYZ Orientation Axis", self, checkable=True)
        self.act_hide_gizmo.setChecked(True)
        self.act_hide_gizmo.triggered.connect(lambda chk: self.three_d_viewer.set_visibility_option("gizmo", chk))
        view_menu.addAction(self.act_hide_gizmo)
        self.act_hide_scale = QAction("Show Vertical Height Scale Bar", self, checkable=True)
        self.act_hide_scale.setChecked(True)
        self.act_hide_scale.triggered.connect(lambda chk: self.three_d_viewer.set_visibility_option("scale", chk))
        view_menu.addAction(self.act_hide_scale)

        layout_submenu = view_menu.addMenu("Layout")
        layout_group = QActionGroup(self)
        layout_group.setExclusive(True)
        self.act_layout_center = QAction("3D Center / Slices Right", self, checkable=True)
        self.act_layout_center.triggered.connect(lambda: self.set_view_layout("3d_center_slices_right"))
        layout_group.addAction(self.act_layout_center)
        layout_submenu.addAction(self.act_layout_center)
        self.act_layout_focus = QAction("3D Focus / Slices Above", self, checkable=True)
        self.act_layout_focus.triggered.connect(lambda: self.set_view_layout("3d_focus_below"))
        layout_group.addAction(self.act_layout_focus)
        layout_submenu.addAction(self.act_layout_focus)
        self.act_layout_classic = QAction("3D Top / Slices Bottom", self, checkable=True)
        self.act_layout_classic.triggered.connect(lambda: self.set_view_layout("3d_top_slices_bottom"))
        layout_group.addAction(self.act_layout_classic)
        layout_submenu.addAction(self.act_layout_classic)
        self.act_layout_center.setChecked(True)

        tools_menu = self.menu_bar.addMenu("Tools")
        self.tools_menu = tools_menu
        
        tools_menu.addSeparator()
        act_compare = QAction(_svg_icon(_asset_path("assets", "icons", "compare.svg")), "Compare with Another Folder...", self)
        act_compare.setShortcut(QKeySequence("Ctrl+Shift+C"))
        act_compare.triggered.connect(self.open_comparison_tool)
        tools_menu.addAction(act_compare)
        
        tools_menu.addSeparator()
        act_device = QAction("Compute Device (CPU / GPU Delegate)...", self)
        act_device.setShortcut(QKeySequence("Ctrl+D"))
        act_device.triggered.connect(lambda: ComputeDeviceDialog(self).exec())
        tools_menu.addAction(act_device)

        settings_menu = self.menu_bar.addMenu("Settings")
        self.settings_menu = settings_menu
        act_ai_setup = QAction("AI API Key...", self)
        act_ai_setup.triggered.connect(self.open_ai_key_settings)
        settings_menu.addAction(act_ai_setup)
        settings_menu.addSeparator()
        act_uninstall = QAction("Uninstall NeuroQuant...", self)
        act_uninstall.setToolTip("Open Windows Apps settings to uninstall NeuroQuant")
        act_uninstall.triggered.connect(self.open_uninstall_settings)
        settings_menu.addAction(act_uninstall)
        settings_menu.addSeparator()
        act_profile = QAction("User Profile...", self)
        act_profile.triggered.connect(lambda: UserProfileDialog(self.chat_panel, self).exec())
        settings_menu.addAction(act_profile)
        settings_menu.addSeparator()

        act_mobile_folder_password = QAction("Mobile Folder Password…", self)
        act_mobile_folder_password.setToolTip("Set or change the password used to open synced folders in the mobile app")
        act_mobile_folder_password.triggered.connect(self.open_mobile_folder_password)
        settings_menu.addAction(act_mobile_folder_password)
        settings_menu.addSeparator()
        
        auth_submenu = settings_menu.addMenu("Authentication")
        act_change_user = QAction("Change Username...", self)
        act_change_user.triggered.connect(self.action_change_username)
        auth_submenu.addAction(act_change_user)
        
        act_change_pwd = QAction("Change Password...", self)
        act_change_pwd.triggered.connect(lambda: ChangePasswordDialog(ACTIVE_USER, self).exec())
        auth_submenu.addAction(act_change_pwd)
        
        preferences_submenu = settings_menu.addMenu("Preferences")
        theme_submenu = preferences_submenu.addMenu("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.act_theme_dark = QAction("Dark", self, checkable=True)
        self.act_theme_dark.triggered.connect(lambda: self.set_theme(DARK))
        theme_group.addAction(self.act_theme_dark)
        theme_submenu.addAction(self.act_theme_dark)
        self.act_theme_light = QAction("Light", self, checkable=True)
        self.act_theme_light.triggered.connect(lambda: self.set_theme(LIGHT))
        theme_group.addAction(self.act_theme_light)
        theme_submenu.addAction(self.act_theme_light)
        self.act_theme_dark.setChecked(True)
        ThemeManager.instance().theme_changed.connect(self._sync_theme_actions)

        act_theme_toggle_shortcut = QAction(self)
        act_theme_toggle_shortcut.setShortcut(QKeySequence("Ctrl+T"))
        act_theme_toggle_shortcut.triggered.connect(ThemeManager.instance().toggle)
        self.addAction(act_theme_toggle_shortcut)

        act_adjustment = QAction("Adjustment...", self)
        act_adjustment.triggered.connect(lambda: AdjustmentPreferencesDialog(self).exec())
        preferences_submenu.addAction(act_adjustment)

        font_menu = preferences_submenu.addMenu("Application Font Size")
        for label, scale in (("Smaller", 0.9), ("Default", 1.0), ("Larger", 1.12), ("Largest", 1.25)):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, value=scale: self.set_application_font_scale(value))
            font_menu.addAction(action)

        act_colors = QAction("3D Layer Colors...", self)
        act_colors.triggered.connect(lambda: LayerColorPreferencesDialog(self.three_d_viewer, self).exec())
        preferences_submenu.addAction(act_colors)
        preferences_submenu.addSeparator()

        act_others = QAction("Others...", self)
        act_others.triggered.connect(lambda: OthersPreferencesDialog(self).exec())
        preferences_submenu.addAction(act_others)
        
        settings_menu.addSeparator()

        help_menu = self.menu_bar.addMenu("Help")
        self.help_menu = help_menu
        act_getting_started = QAction("Getting Started", self)
        act_getting_started.triggered.connect(self.show_getting_started)
        help_menu.addAction(act_getting_started)
        help_menu.addSeparator()
        act_about = QAction("About NeuroQuant", self)
        act_about.triggered.connect(lambda: AboutNeuroQuantDialog(self).exec())
        help_menu.addAction(act_about)
        act_reqs = QAction("System Requirements", self)
        act_reqs.triggered.connect(lambda: SystemRequirementsDialog(self).exec())
        help_menu.addAction(act_reqs)
        act_manual = QAction("User Manual", self)
        act_manual.triggered.connect(lambda: UserManualDialog(self).exec())
        help_menu.addAction(act_manual)
        act_shortcuts = QAction("Keyboard Shortcuts", self)
        act_shortcuts.triggered.connect(lambda: KeyboardShortcutsDialog(self).exec())
        help_menu.addAction(act_shortcuts)

        act_ai_menu = QAction(QIcon(ICON_AI), "NeuroQuant AI", self)
        act_ai_menu.setShortcut(QKeySequence("Ctrl+A"))
        act_ai_menu.triggered.connect(self.toggle_ai_copilot)
        self.menu_bar.addAction(act_ai_menu)
        self.act_ai_menu = act_ai_menu
        act_chat_menu = QAction(QIcon(ICON_CHATROOM), "Chat Room", self)
        act_chat_menu.setShortcut(QKeySequence("Ctrl+M"))
        act_chat_menu.triggered.connect(self.toggle_chat_panel)
        self.menu_bar.addAction(act_chat_menu)
        self.act_chat_menu = act_chat_menu
        self.website_icon_path = str(_asset_path("assets", "icons", "external-link.svg"))
        website_svg = recolor_svg(self.website_icon_path, "website_menu_dark", "#38bdf8")
        self.act_website_menu = QAction(_svg_icon(website_svg), "Visit our website", self)
        self.act_website_menu.triggered.connect(open_documentation_site)
        self.menu_bar.addAction(self.act_website_menu)

        act_ai_alt_shortcut = QAction(self)
        act_ai_alt_shortcut.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_ai_alt_shortcut.triggered.connect(self.toggle_ai_copilot)
        self.addAction(act_ai_alt_shortcut)

        root_layout.addWidget(self.menu_bar)

        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(0)
        root_layout.addLayout(body_layout)

        self.left_scroll = QScrollArea()
        self.left_scroll.setFixedWidth(350)
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.left_scroll.setStyleSheet("""
            QScrollArea { border: 1px solid #1e293b; border-radius: 4px; background-color: #0d1117; }
            QScrollBar:vertical { width: 6px; background: #101622; }
            QScrollBar::handle:vertical { background: #333333; border-radius: 3px; }
        """)

        left_container = QWidget()
        controls_panel = QVBoxLayout(left_container)
        controls_panel.setContentsMargins(6, 6, 6, 6)
        controls_panel.setSpacing(7)

        self.btn_run = QPushButton("SCAN")
        self.btn_run.setFixedHeight(42)
        self._set_run_available(False)
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.setStyleSheet("""
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00838f, stop:1 #00bcd4);
                color: #ffffff; font-size: 12px; font-weight: bold; letter-spacing: 0.5px;
                border: none; border-radius: 6px;
            }
            QPushButton:hover { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00acc1, stop:1 #26e0f5); }
            QPushButton:pressed { background-color: #00838f; }
            QPushButton:disabled { background-color: #21262d; color: #64748b; }
        """)
        self.btn_run.clicked.connect(self.prompt_patient_info_and_run)
        controls_panel.addWidget(self.btn_run)

        act_run_shortcut = QAction(self)
        act_run_shortcut.setShortcut(QKeySequence("Ctrl+R"))
        act_run_shortcut.triggered.connect(lambda: self.btn_run.click() if self.btn_run.isEnabled() else None)
        self.addAction(act_run_shortcut)

        self.progress_group = QGroupBox("Scan Import")
        self.progress_group.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_group)
        progress_layout.setContentsMargins(8, 8, 8, 8)
        progress_layout.setSpacing(5)

        pct_row = QHBoxLayout()
        pct_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_progress_status = QLabel("Processing...")
        self.lbl_progress_status.setStyleSheet("color: #9ca3af; font-size: 10px;")
        self.lbl_progress_pct = QLabel("0%")
        self.lbl_progress_pct.setStyleSheet("color: #00e5ff; font-size: 10px; font-weight: bold;")
        self.lbl_progress_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pct_row.addWidget(self.lbl_progress_status)
        pct_row.addStretch()
        pct_row.addWidget(self.lbl_progress_pct)
        progress_layout.addLayout(pct_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background-color: #161b22; border: none; border-radius: 1px; }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00838f, stop:0.5 #00bcd4, stop:1 #00e5ff);
                border-radius: 1px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)
        controls_panel.addWidget(self.progress_group)

        patient_group = QGroupBox()
        patient_group.setStyleSheet("QGroupBox{border:none;background:transparent;margin:0;padding:0}")
        patient_layout = QVBoxLayout(patient_group)
        patient_layout.setContentsMargins(8, 8, 8, 8)
        patient_layout.setSpacing(3)

        head_subj = QHBoxLayout()
        self.btn_patient_expand = QPushButton("▾  Patients")
        self.btn_patient_expand.setCheckable(True)
        self.btn_patient_expand.setChecked(True)
        self.btn_patient_expand.setCursor(Qt.PointingHandCursor)
        self.btn_patient_expand.setStyleSheet("QPushButton{background:transparent;color:#fff;border:none;text-align:left;padding:0;font-size:11px;font-weight:bold} QPushButton:hover{color:#38bdf8}")
        info_subj = HelpInfoIcon(
            title="Patients",
            description="Displays active Patient ID, age, and biological sex recorded during intake. Included on Page 1 of the clinical PDF report.",
            parent=self
        )
        head_subj.addWidget(self.btn_patient_expand)
        head_subj.addStretch()
        head_subj.addWidget(info_subj)
        patient_layout.addLayout(head_subj)
        patient_details = QWidget()
        patient_details_layout = QVBoxLayout(patient_details)
        patient_details_layout.setContentsMargins(0, 0, 0, 0)
        patient_details_layout.setSpacing(3)
        self.btn_patient_expand.toggled.connect(lambda expanded: (
            patient_details.setVisible(expanded),
            self.btn_patient_expand.setText("▾  Patients" if expanded else "▸  Patients")
        ))

        def create_info_row(label_text, default_val="--"):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            lbl_title = QLabel(label_text)
            lbl_title.setObjectName("sidebarMetaLabel")
            lbl_title.setStyleSheet("color: #888888; font-size: 10px;")
            lbl_val = QLabel(default_val)
            lbl_val.setObjectName("sidebarMetaValue")
            lbl_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl_val.setWordWrap(True)
            lbl_val.setMinimumWidth(0)
            lbl_val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            lbl_val.setStyleSheet("color: #00e5ff; font-size: 10px; font-weight: bold;")
            row.addWidget(lbl_title, 1)
            row.addWidget(lbl_val, 2)
            patient_details_layout.addLayout(row)
            return lbl_val

        self.val_patient_id = create_info_row("Patient ID / Name:", "None Loaded")
        self.val_patient_age = create_info_row("Age:", "--")
        self.val_patient_gender = create_info_row("Gender:", "--")
        self.btn_patient_profiles = QPushButton("View details  ↗")
        self.btn_patient_profiles.setCursor(Qt.PointingHandCursor)
        self.btn_patient_profiles.setStyleSheet(
            "QPushButton{background:transparent;color:#8cb4d8;border:1px solid #263244;"
            "border-radius:5px;text-align:left;padding:5px 7px;font-size:10px}"
            "QPushButton:hover{color:#38bdf8;border-color:#38bdf8}"
        )
        self.btn_patient_profiles.clicked.connect(self.open_patient_profiles)
        patient_details_layout.addWidget(self.btn_patient_profiles)
        patient_layout.addWidget(patient_details)
        controls_panel.addWidget(patient_group)

        opacity_group = QGroupBox("Display")
        opacity_layout = QVBoxLayout(opacity_group)
        opacity_layout.setContentsMargins(8, 8, 8, 8)
        opacity_layout.setSpacing(3)
        self.lbl_opacity = QLabel("Tumor Opacity: 60%")
        self.lbl_opacity.setStyleSheet("color: #cccccc; font-size: 11px;")
        self.slider_opacity = QSlider(Qt.Horizontal)
        self.slider_opacity.setRange(0, 100)
        self.slider_opacity.setValue(60)
        self.slider_opacity.valueChanged.connect(self.on_tumor_opacity_changed)
        self.lbl_healthy = QLabel("Healthy Tissue Opacity: 18%")
        self.lbl_healthy.setStyleSheet("color: #cccccc; font-size: 11px;")
        self.slider_healthy = QSlider(Qt.Horizontal)
        self.slider_healthy.setRange(0, 100)
        self.slider_healthy.setValue(18)
        self.slider_healthy.valueChanged.connect(self.on_healthy_opacity_changed)
        opacity_layout.addWidget(self.lbl_opacity)
        opacity_layout.addWidget(self.slider_opacity)
        opacity_layout.addWidget(self.lbl_healthy)
        opacity_layout.addWidget(self.slider_healthy)
        controls_panel.addWidget(opacity_group)

        metrics_group = QGroupBox()
        metrics_layout = QVBoxLayout(metrics_group)
        metrics_layout.setContentsMargins(8, 8, 8, 8)
        metrics_layout.setSpacing(4)

        head_met = QHBoxLayout()
        lbl_met_title = QLabel("Volumetric Metrics")
        lbl_met_title.setObjectName("sidebarSectionTitle")
        lbl_met_title.setStyleSheet("font-weight: bold; color: #ffffff; font-size: 11px;")
        info_met = HelpInfoIcon(
            title="Volumetric Metrics (Physical mm³)",
            description=(
                "Volume is computed by counting segmented voxels and multiplying by scanner spacing.\n\n"
                "• Frontal Lobe: Anterior cerebrum.\n• Parietal Lobe: Superior/crown cortex.\n"
                "• Occipital Lobe: Posterior visual pole.\n• Temporal Lobe: Inferior-lateral cortex.\n"
                "• Cerebellum: Infratentorial posterior balance center.\n• Spinal Cord: Central basal brainstem trunk."
            ),
            parent=self
        )
        head_met.addWidget(lbl_met_title)
        head_met.addStretch()
        head_met.addWidget(info_met)
        metrics_layout.addLayout(head_met)

        def create_metric_row(label_text, is_bold=False, is_highlight=False):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            name_lbl = QLabel(label_text)
            name_lbl.setObjectName("sidebarMetricHighlight" if is_highlight else "sidebarMetricStrong" if is_bold else "sidebarMetricName")
            name_lbl.setWordWrap(True)
            name_lbl.setMinimumWidth(0)
            name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            val_lbl = QLabel("-- mm³")
            val_lbl.setObjectName("sidebarMetricHighlight" if is_highlight else "sidebarMetricStrong" if is_bold else "sidebarMetricValue")
            val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val_lbl.setWordWrap(True)
            val_lbl.setMinimumWidth(92)
            val_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            if is_highlight:
                name_lbl.setStyleSheet("color: #ff8a80; font-size: 11px; font-weight: bold;")
                val_lbl.setStyleSheet("color: #ff5252; font-size: 11px; font-weight: bold;")
            elif is_bold:
                name_lbl.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                val_lbl.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
            else:
                name_lbl.setStyleSheet("color: #b0b0b0; font-size: 10px;")
                val_lbl.setStyleSheet("color: #e0e0e0; font-size: 10px; font-weight: bold;")
            row.addWidget(name_lbl, 1)
            row.addWidget(val_lbl, 0)
            metrics_layout.addLayout(row)
            return val_lbl

        self.val_all_total = create_metric_row("Total Intracranial", is_bold=True)
        self.val_total_tumor = create_metric_row("Total Tumor", is_highlight=True)
        self.val_core = create_metric_row("• Active Core")
        self.val_edema = create_metric_row("• Peritumoral Edema")
        self.val_frontal = create_metric_row("• Frontal Lobe")
        self.val_parietal = create_metric_row("• Parietal Lobe")
        self.val_occipital = create_metric_row("• Occipital Lobe")
        self.val_temporal = create_metric_row("• Temporal Lobe")
        self.val_cerebellum = create_metric_row("• Cerebellum")
        self.val_spinal_cord = create_metric_row("• Spinal Cord / Stem")

        self.metrics_chart = VolumetricBarChart()
        metrics_layout.addWidget(self.metrics_chart)
        controls_panel.addWidget(metrics_group)

        diag_group = QGroupBox()
        diag_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        diag_layout = QVBoxLayout(diag_group)
        diag_layout.setContentsMargins(8, 10, 8, 10)
        diag_layout.setSpacing(6)
        diag_layout.setAlignment(Qt.AlignTop)

        head_diag = QHBoxLayout()
        lbl_diag_title = QLabel("Diagnostic Assessment")
        lbl_diag_title.setObjectName("sidebarSectionTitle")
        lbl_diag_title.setStyleSheet("font-weight: bold; color: #ffffff; font-size: 11px;")
        info_diag = HelpInfoIcon(
            title="WHO CNS5 Diagnostic Classification",
            description=(
                "Maps quantitative lesion volumetrics to World Health Organization (WHO) CNS5 criteria:\n\n"
                "• Grade IV (HGG / Glioblastoma): Marked necrotic / enhancing core volume with infiltrative vasogenic swelling.\n"
                "• Grade III (Anaplastic Glioma): Intermediate active core burden.\n"
                "• Grade II (Low-Grade Glioma): Diffuse hyperintensity on FLAIR without large necrotic core.\n"
                "• Mass Effect: Evaluates elevated risk of ventricular compression or subfalcine herniation."
            ),
            parent=self
        )
        btn_diag_zoom = QPushButton("Expand")
        btn_diag_zoom.setFixedSize(50, 18)
        btn_diag_zoom.setCursor(Qt.PointingHandCursor)
        btn_diag_zoom.setToolTip("Open Diagnostic Assessment in a larger view")
        btn_diag_zoom.setStyleSheet("""
            QPushButton {
                background-color: #161f2e; color: #94a3b8; border: 1px solid #334155;
                border-radius: 9px; font-size: 10px; font-weight: bold;
            }
            QPushButton:hover { background-color: #00838f; color: #ffffff; }
        """)
        btn_diag_zoom.clicked.connect(lambda: DiagnosticZoomDialog(self, self).exec())

        head_diag.addWidget(lbl_diag_title)
        head_diag.addStretch()
        head_diag.addWidget(btn_diag_zoom)
        head_diag.addWidget(info_diag)
        diag_layout.addLayout(head_diag)

        self.diag_pathology = QLabel("Suspected Pathology:\nAwaiting Segmentation")
        self.diag_pathology.setObjectName("sidebarDiagnosticPrimary")
        self.diag_pathology.setWordWrap(True)
        self.diag_pathology.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.diag_pathology.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold; line-height: 1.25;")

        self.diag_burden = QLabel("Cranial Vault Burden: --")
        self.diag_burden.setObjectName("sidebarDiagnosticSecondary")
        self.diag_burden.setWordWrap(True)
        self.diag_burden.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.diag_burden.setStyleSheet("color: #e0e0e0; font-size: 10px;")

        self.diag_mass_effect = QLabel("Mass Effect Risk: --")
        self.diag_mass_effect.setObjectName("sidebarDiagnosticSecondary")
        self.diag_mass_effect.setWordWrap(True)
        self.diag_mass_effect.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.diag_mass_effect.setStyleSheet("color: #e0e0e0; font-size: 10px;")

        self.diag_lobar_focus = QLabel("Lobar Involvement: --")
        self.diag_lobar_focus.setObjectName("sidebarDiagnosticSecondary")
        self.diag_lobar_focus.setWordWrap(True)
        self.diag_lobar_focus.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.diag_lobar_focus.setStyleSheet("color: #e0e0e0; font-size: 10px;")

        self.diag_impression = QLabel("Clinical Impression:\nLoad scan and run segmentation to compute automated volumetric findings.")
        self.diag_impression.setObjectName("sidebarDiagnosticSecondary")
        self.diag_impression.setWordWrap(True)
        self.diag_impression.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.diag_impression.setStyleSheet("color: #b0bec5; font-size: 10px; font-style: italic; line-height: 1.3;")

        diag_layout.addWidget(self.diag_pathology)
        diag_layout.addWidget(self.diag_burden)
        diag_layout.addWidget(self.diag_mass_effect)
        diag_layout.addWidget(self.diag_lobar_focus)
        diag_layout.addWidget(self.diag_impression)
        controls_panel.addWidget(diag_group)

        history_group = QGroupBox("Segmentation History")
        self.history_group = history_group
        history_layout = QVBoxLayout(history_group)
        history_layout.setContentsMargins(8, 8, 8, 8)
        history_layout.setSpacing(4)

        self.search_history = QLineEdit()
        self.search_history.setPlaceholderText("Search history (patient, MD, date)...")
        add_search_icon(self.search_history)
        self.search_history.setFixedHeight(22)
        self.search_history.setStyleSheet("""
            QLineEdit {
                background-color: #1a1a1a; color: #ffffff; border: 1px solid #333333;
                border-radius: 3px; padding-left: 5px; font-size: 9px;
            }
            QLineEdit:focus { border-color: #00e5ff; }
        """)
        self.search_history.textChanged.connect(self.on_search_history_changed)
        history_layout.addWidget(self.search_history)

        self.combo_history = QComboBox()
        self.combo_history.setFixedHeight(22)
        self.combo_history.setStyleSheet("background-color: #202020; color: #ffffff; font-size: 10px;")
        self.combo_history.currentIndexChanged.connect(self.on_history_selected)
        history_layout.addWidget(self.combo_history)

        self.lbl_history_detail = QLabel("No previous records selected")
        self.lbl_history_detail.setWordWrap(True)
        self.lbl_history_detail.setStyleSheet("color: #888888; font-size: 9px;")
        history_layout.addWidget(self.lbl_history_detail)

        controls_panel.addWidget(history_group)
        controls_panel.addStretch()
        self.left_scroll.setWidget(left_container)
        body_layout.addWidget(self.left_scroll)

        self.btn_toggle_sidebar = QPushButton("◀")
        self.btn_toggle_sidebar.setFixedWidth(16)
        self.btn_toggle_sidebar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.btn_toggle_sidebar.setToolTip("Toggle Sidebar (Collapse / Expand)")
        self.btn_toggle_sidebar.setStyleSheet("""
            QPushButton {
                background-color: #161f2e; color: #00e5ff; border: 1px solid #1e293b;
                border-left: none; font-size: 10px; font-weight: bold; padding: 0px;
            }
            QPushButton:hover { background-color: #00838f; color: #ffffff; }
        """)
        self.btn_toggle_sidebar.clicked.connect(self.toggle_sidebar)
        body_layout.addWidget(self.btn_toggle_sidebar)

        self.viewport_widget = QWidget()
        self.viewport_widget.setMinimumWidth(500)
        self.viewport_layout = None
        body_layout.addWidget(self.viewport_widget, stretch=1)

        self.three_d_viewer = ThreeDViewer()

        self.axial_viewer = SliceViewer("Axial")
        self.coronal_viewer = SliceViewer("Coronal")
        self.sagittal_viewer = SliceViewer("Sagittal")
        self.modality_viewer = SliceViewer("CT")
        self.slice_viewers = (
            self.axial_viewer, self.coronal_viewer, self.sagittal_viewer, self.modality_viewer
        )
        self.slice_mode_bar = QWidget()
        mode_layout = QHBoxLayout(self.slice_mode_bar)
        mode_layout.setContentsMargins(4, 2, 4, 2)
        mode_layout.setSpacing(8)
        mode_label = QLabel("Slice view")
        mode_layout.addWidget(mode_label)
        self.btn_anatomical_mode = QPushButton("Anatomical Views")
        self.btn_sequence_mode = QPushButton("MRI Sequences")
        for button in (self.btn_anatomical_mode, self.btn_sequence_mode):
            button.setCheckable(True)
            button.setStyleSheet("QPushButton{padding:5px 12px;border:1px solid #334155;border-radius:5px;background:transparent;color:#cbd5e1} QPushButton:checked{background:#087e8b;color:#fff;border-color:#22d3ee} QPushButton:hover{border-color:#38bdf8}")
            mode_layout.addWidget(button)
        self.btn_anatomical_mode.setChecked(True)
        self.btn_anatomical_mode.clicked.connect(lambda: self._switch_slice_mode(0))
        self.btn_sequence_mode.clicked.connect(lambda: self._switch_slice_mode(1))
        mode_layout.addStretch()
        self.sequence_viewers = tuple(SliceViewer(name) for name in ("FLAIR", "T1", "T2", "T1ce"))
        self.all_slice_viewers = self.slice_viewers + self.sequence_viewers
        self.slice_mode = "Anatomical Views"

        self.set_view_layout("3d_center_slices_right")
        # AI and team chat are available in the menu bar beside Help.

    def set_application_font_scale(self, scale):
        applied = apply_font_scale(QApplication.instance(), scale)
        self.statusBar().showMessage(f"Application text size: {round(applied * 100)}%", 4000)

    def start_workstation(self):
        """Open the workspace immediately, then show the welcome guide without blocking it."""
        settings = QSettings("NeuroQuant", "NeuroQuant")
        self.showMaximized()
        self.raise_()
        self.activateWindow()
        if not settings.value("welcome/dont_ask", False, type=bool):
            # Defer guide creation until the main window has entered the event loop.
            # Keep it modeless so the clinician can use the workspace immediately.
            QTimer.singleShot(0, self._show_first_run_welcome)

    def _show_first_run_welcome(self):
        if self._first_run_welcome is not None and self._first_run_welcome.isVisible():
            return
        self._first_run_welcome = FirstRunWelcomeDialog(self)
        self._first_run_welcome.setModal(False)
        self._first_run_welcome.setWindowModality(Qt.WindowModality.NonModal)
        self._first_run_welcome.show()

    def toggle_ai_copilot(self):
        if self.diagnostic_ai.isVisible():
            self.diagnostic_ai.hide()
            return
        screen = QApplication.primaryScreen().geometry()
        width, height = int(screen.width() * 0.55), int(screen.height() * 0.65)
        self.diagnostic_ai.resize(width, height)
        self.diagnostic_ai.move((screen.width() - width) // 2, (screen.height() - height) // 2)
        self.diagnostic_ai.showNormal()
        self.diagnostic_ai.raise_()
        self.diagnostic_ai.activateWindow()

    def open_mobile_folder_password(self):
        cloud_password = getattr(self, "_cloud_password", "")
        if not cloud_password:
            cloud_password, accepted = QInputDialog.getText(
                self, "Verify Cloud Account", "Enter your desktop cloud account password to check folder password status:", QLineEdit.Password
            )
            if not accepted or not cloud_password:
                return
        try:
            status = _call_folder_service("getSegmentationFolderStatus", {
                "username": self.authenticated_user,
                "accountPassword": cloud_password,
            })
            folders = status.get("folders", [])
        except Exception as exc:
            QMessageBox.warning(
                self, "Could not check mobile folders",
                f"NeuroQuant could not read folder password status from Firestore.\n\n{exc}",
            )
            return
        self._cloud_password = cloud_password
        dialog = MobileFolderPasswordDialog(self.authenticated_user, cloud_password, folders, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if folders:
                self.statusBar().showMessage(f"Mobile folder password saved to {len(folders)} cloud folders and this computer.", 7000)
            else:
                self.statusBar().showMessage("Mobile folder password saved for future segmentation folders.", 7000)

    def open_ai_key_settings(self):
        dialog = AIAPIKeyDialog(api_key=self.ai_api_key, parent=self)
        if dialog.exec() == QDialog.Accepted:
            try:
                save_api_key(dialog.api_key)
            except Exception as error:
                QMessageBox.warning(self, "Could not save API key", str(error))
                return
            self.ai_api_key = dialog.api_key
            self.diagnostic_ai.set_api_key(self.ai_api_key)

    def open_uninstall_settings(self):
        dialog = UninstallFeedbackDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reason, feedback = dialog.get_data()
        # Keep feedback locally and open Windows' installed-apps page; Windows
        # manages the actual uninstall and confirmation flow.
        settings = QSettings("NeuroQuant", "NeuroQuant")
        settings.setValue("feedback/uninstall_reason", reason)
        settings.setValue("feedback/uninstall_comment", feedback.strip())
        try:
            os.startfile("ms-settings:appsfeatures")
        except Exception as error:
            QMessageBox.information(
                self, "Uninstall NeuroQuant",
                f"Your feedback has been saved. Open Windows Settings → Apps → Installed apps "
                f"to uninstall NeuroQuant.\n\n{error}"
            )

    def action_change_username(self):
        global ACTIVE_USER
        dlg = ChangeUsernameDialog(ACTIVE_USER, self)
        if dlg.exec() == QDialog.Accepted:
            ACTIVE_USER = dlg.new_username
            self.chat_panel.user_id = ACTIVE_USER
            self.chat_panel.load_profile_from_cloud()
            self.diagnostic_ai.set_user_id(get_user_id_by_username(ACTIVE_USER) or ACTIVE_USER)

    def _make_slice_grid(self):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        grid.addWidget(self.axial_viewer, 0, 0)
        grid.addWidget(self.coronal_viewer, 0, 1)
        grid.addWidget(self.sagittal_viewer, 1, 0)
        grid.addWidget(self.modality_viewer, 1, 1)
        for col in range(2):
            grid.setColumnStretch(col, 1)
        for row in range(2):
            grid.setRowStretch(row, 1)
        return grid

    def _make_sequence_grid(self):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for index, viewer in enumerate(self.sequence_viewers):
            grid.addWidget(viewer, index // 2, index % 2)
        for index in range(2):
            grid.setColumnStretch(index, 1)
            grid.setRowStretch(index, 1)
        return grid

    def _switch_slice_mode(self, index):
        self.slice_mode = "Anatomical Views" if index == 0 else "MRI Sequences"
        self.btn_anatomical_mode.setChecked(index == 0)
        self.btn_sequence_mode.setChecked(index == 1)
        if hasattr(self, "current_view_layout"):
            self.set_view_layout(self.current_view_layout)

    def _current_slice_grid(self):
        return self._make_slice_grid() if self.slice_mode == "Anatomical Views" else self._make_sequence_grid()

    def _set_slice_series(self, mask=None):
        for viewer, source in zip(self.slice_viewers, ("flair", "flair", "flair", "ct")):
            volume = self.ct_volume if source == "ct" else self.modality_dict.get(source)
            viewer.set_data(volume, None if source == "ct" else mask)
        for viewer, key in zip(self.sequence_viewers, ("flair", "t1", "t2", "t1ce")):
            viewer.set_data(self.modality_dict.get(key), mask)

    def _clear_viewport_layout(self):
        if self.viewport_layout is None:
            return
        def clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                child_layout = item.layout()
                if child_layout is not None:
                    clear_layout(child_layout)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
        clear_layout(self.viewport_layout)
        QWidget().setLayout(self.viewport_layout)
        self.viewport_layout = None
        
    def set_view_layout(self, layout_name: str):
        if not hasattr(self, "viewport_widget"):
            return
        self._clear_viewport_layout()

        if layout_name == "3d_center_slices_right":
            layout = QHBoxLayout(self.viewport_widget)
            layout.setContentsMargins(6, 0, 0, 0)
            layout.setSpacing(4)
            layout.addWidget(self.three_d_viewer, stretch=7)
            controls = QVBoxLayout()
            controls.setContentsMargins(0, 0, 0, 0)
            controls.setSpacing(2)
            controls.addWidget(self.slice_mode_bar)
            slice_grid = self._current_slice_grid()
            controls.addLayout(slice_grid, 1)
            layout.addLayout(controls, stretch=4)
        elif layout_name == "3d_focus_below":
            layout = QVBoxLayout(self.viewport_widget)
            layout.setContentsMargins(6, 0, 0, 0)
            layout.setSpacing(4)
            controls = QVBoxLayout()
            controls.setContentsMargins(0, 0, 0, 0)
            controls.setSpacing(2)
            controls.addWidget(self.slice_mode_bar)
            controls.addLayout(self._current_slice_grid(), 1)
            layout.addLayout(controls, stretch=2)
            layout.addWidget(self.three_d_viewer, stretch=8)
        else:
            layout = QVBoxLayout(self.viewport_widget)
            layout.setContentsMargins(6, 0, 0, 0)
            layout.setSpacing(4)
            layout.addWidget(self.three_d_viewer, stretch=5)
            controls = QVBoxLayout()
            controls.setContentsMargins(0, 0, 0, 0)
            controls.setSpacing(2)
            controls.addWidget(self.slice_mode_bar)
            controls.addLayout(self._current_slice_grid(), 1)
            layout.addLayout(controls, stretch=4)

        self.viewport_layout = layout
        self.current_view_layout = layout_name

        self.act_layout_center.setChecked(layout_name == "3d_center_slices_right")
        self.act_layout_focus.setChecked(layout_name == "3d_focus_below")
        self.act_layout_classic.setChecked(layout_name == "3d_top_slices_bottom")

    def toggle_sidebar(self):
        is_visible = self.left_scroll.isVisible()
        self.left_scroll.setVisible(not is_visible)
        self.btn_toggle_sidebar.setText("▶" if is_visible else "◀")

    def open_patient_database(self):
        dlg = SystemDatabaseDialog(self)
        dlg.exec()

    def open_patient_details_manager(self):
        PatientDetailsManagerDialog(self).exec()

    def open_patient_profiles(self):
        if not self.current_subject_name or self.current_subject_name == "None Loaded":
            QMessageBox.information(self, "No patient loaded", "Import a patient scan to view its details.")
            return
        PatientProfilesDialog(self).exec()

    def show_getting_started(self):
        FirstRunWelcomeDialog(self).exec()

    def attach_database_data_to_ai(self):
        dialog = AIDataSelectionDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.diagnostic_ai.stage_files(dialog.selected_paths)

    def open_comparison_tool(self):
        if not self.current_metrics:
            QMessageBox.warning(self, "Compare Tool", "Please run segmentation on the current scan first.")
            return
        dlg = LongitudinalComparisonDialog(self.current_subject_name, self.current_metrics, self)
        dlg.exec()

    def prompt_open_folder(self):
        source_dialog = ImportSourceDialog(self)
        if source_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if source_dialog.choice == "files":
            dialog = ScanAssignmentDialog([], self, allow_add_files=True)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                case_folder = os.path.dirname(next(path for path in dialog.assigned_paths.values() if isinstance(path, str)))
                self.enqueue_import(case_folder, dialog.assigned_paths, dialog.detect_ct_dicom)
            return
        if source_dialog.choice != "folder":
            return

        folder = QFileDialog.getExistingDirectory(self, "Select one case folder containing MRI and optional CT scans")
        if not folder:
            return
        self._assign_folder_for_import(folder)

    def _assign_folder_for_import(self, folder):
        candidates, unsupported = [], []
        supported_suffixes = (".nii", ".nii.gz", ".nrrd", ".mha", ".mhd", ".hdr")
        for root, _dirs, names in os.walk(folder):
            for name in names:
                lower_name = name.lower()
                if any(token in lower_name for token in ("seg", "mask")):
                    continue
                path = os.path.join(root, name)
                if lower_name.endswith(supported_suffixes):
                    candidates.append(path)
                elif lower_name.endswith(".dsv"):
                    unsupported.append(path)
            try:
                series_ids = sitk.ImageSeriesReader.GetGDCMSeriesIDs(root) or []
            except Exception:
                series_ids = []
            for series_id in series_ids:
                try:
                    dicom_files = list(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(root, series_id))
                    if not dicom_files:
                        continue
                    meta_reader = sitk.ImageFileReader()
                    meta_reader.SetFileName(dicom_files[0])
                    meta_reader.LoadPrivateTagsOn()
                    meta_reader.ReadImageInformation()

                    def read_tag(key):
                        try:
                            return meta_reader.GetMetaData(key).strip()
                        except Exception:
                            return ""

                    modality = read_tag("0008|0060").upper()
                    description = read_tag("0008|103e") or read_tag("0018|1030") or "Unnamed series"
                    candidates.append({
                        "kind": "dicom", "files": dicom_files, "path": root,
                        "label": f"DICOM {modality or 'series'} · {description} · {len(dicom_files)} images",
                        "modality": modality, "description": description,
                    })
                except Exception:
                    continue
        candidates.sort(key=lambda entry: (entry.get("label", "") if isinstance(entry, dict) else os.path.basename(entry)).casefold())
        if not candidates:
            detail = "Supported scan data includes NIfTI (.nii/.nii.gz), NRRD (.nrrd), MetaImage (.mha/.mhd), Analyze (.hdr/.img pair), and DICOM series."
            if unsupported:
                detail += "\n\nThe .dsv file is Disect Pro's native format and cannot be read directly. Use a DICOM or NIfTI version if the source provides one, or a converter that explicitly supports this DSV format."
            QMessageBox.warning(self, "No supported scan series found", detail)
            return
        dialog = ScanAssignmentDialog(candidates, self, unsupported_files=unsupported)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.enqueue_import(folder, dialog.assigned_paths, dialog.detect_ct_dicom)

    def prompt_import_from_cloud(self):
        dialog = CloudCaseDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.imported_folder:
            # Use the same modality assignment step as a local folder import.
            self._assign_folder_for_import(dialog.imported_folder)

    def enqueue_import(self, folder_path: str, assigned_paths=None, detect_ct_dicom=True):
        queued = (folder_path, dict(assigned_paths or {}), bool(detect_ct_dicom))
        if not any(item[0] == folder_path for item in self.import_queue):
            self.import_queue.append(queued)
        if not self.is_processing_queue and self.import_queue:
            self._process_next_in_queue()

    def _process_next_in_queue(self):
        if not self.import_queue:
            self.is_processing_queue = False
            return

        self.is_processing_queue = True
        next_folder, assigned_paths, detect_ct_dicom = self.import_queue.popleft()

        previous_subject = self.current_subject_name
        previous_patient_info = dict(self.patient_info)
        previous_metadata_confirmed = self.patient_metadata_confirmed
        self.current_subject_name = os.path.basename(os.path.normpath(next_folder))
        self.patient_metadata_confirmed = False
        saved_patient = fetch_patient_details(self.current_subject_name)
        if saved_patient:
            self.patient_info = saved_patient
            self.patient_info["radiologist"] = self.attending_physician
        else:
            self.patient_info = {
                "patient_name": self.current_subject_name, "patient_age": "0", "patient_gender": "Unknown",
                "radiologist": self.attending_physician, "full_name": "", "date_of_birth": "", "phone": "",
                "email": "", "address": "", "emergency_contact": "", "emergency_phone": "",
                "medical_history": "", "allergies": "", "current_medications": ""
            }

        intake = ImportPatientDetailsDialog(
            patient_code=self.current_subject_name,
            full_name=self.patient_info.get("full_name", ""),
            age=self.patient_info.get("patient_age", 0),
            parent=self,
        )
        if intake.exec() != QDialog.DialogCode.Accepted:
            self.current_subject_name = previous_subject
            self.patient_info = previous_patient_info
            self.patient_metadata_confirmed = previous_metadata_confirmed
            self.is_processing_queue = False
            self._set_run_available(self.full_stacked_mri is not None)
            self.lbl_progress_status.setText("Import cancelled: patient metadata is required.")
            self._process_next_in_queue()
            return

        try:
            self.patient_info.update(intake.get_data())
            self.current_subject_name = self.patient_info["patient_name"]
            self.patient_info["radiologist"] = self.attending_physician
            self.patient_info["patient_gender"] = self.patient_info.get("patient_gender") or "Unknown"
            save_patient_record(self.patient_info, preserve_existing=True)
            self.patient_metadata_confirmed = True
        except Exception as exc:
            QMessageBox.critical(self, "Patient record could not be saved", str(exc))
            self.is_processing_queue = False
            return

        self.reset_previous_patient_data()
        self.update_patient_banner()
        self.btn_run.setText("SCAN")

        self.progress_group.setVisible(True)
        self.progress_group.setTitle("Importing MRI and optional CT")
        self.progress_bar.setValue(0)
        self.lbl_progress_pct.setText("0%")
        self.lbl_progress_status.setText(f"Importing {self.current_subject_name}...")
        self._set_run_available(False)

        self.loader_worker = FolderLoaderWorker(next_folder, assigned_paths, detect_ct_dicom)
        self.loader_worker.progress.connect(self.on_load_progress)
        self.loader_worker.finished.connect(self.on_load_finished)
        self.loader_worker.error.connect(self.on_load_error)
        self.loader_worker.start()

    def on_load_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.lbl_progress_pct.setText(f"{percent}%")
        self.lbl_progress_status.setText(message)

    def on_load_finished(self, data: dict):
        self.progress_group.setVisible(False)
        self.btn_run.setText("SCAN")
        self._set_run_available(True)

        self.current_scan_key = data.get("source_scan_key")
        self.voxel_spacing = data["voxel_spacing"]
        self.full_flair = data["full_flair"]
        self.modality_dict = data["modality_dict"]
        self.full_stacked_mri = data["full_stacked"]
        self.ct_volume = data.get("ct_volume")
        self._set_slice_series()
        detected_ct_path = data.get("ct_path")
        if detected_ct_path:
            self.statusBar().showMessage("MRI and CT imported from the selected case folder.", 8000)
        else:
            self.statusBar().showMessage("MRI imported. No CT scan was found in this case folder.", 8000)

        self.three_d_viewer.set_data(self.full_flair, mask=None, spacing=self.voxel_spacing)

        self.calculate_lobar_volumes()
        self.lbl_progress_status.setText("Scan loaded. Review the patient details, then select SCAN when ready.")
        self.statusBar().showMessage("Scan imported. Select SCAN when you are ready to segment.", 10000)

    def on_load_error(self, message: str):
        self.progress_group.setVisible(False)
        self._set_run_available(False)
        QMessageBox.critical(self, "Import Error", f"Failed to load scan folder: {message}")
        self._process_next_in_queue()

    def run_segmentation(self):
        self.progress_group.setVisible(True)
        self.progress_group.setTitle("Running Segmentation")
        self.progress_bar.setValue(0)
        self.lbl_progress_pct.setText("0%")
        self.lbl_progress_status.setText("Starting inference...")
        self._set_run_available(False)

        self.worker = SegmentationWorker(
            model_path=str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "data" / "models" / "neuroquant_3dunet.onnx"),
            full_volume=self.full_stacked_mri,
            voxel_spacing=self.voxel_spacing
        )
        self.worker.progress.connect(self.on_segmentation_progress)
        self.worker.finished.connect(self.on_segmentation_finished)
        self.worker.error.connect(self.on_segmentation_error)
        self.worker.start()

    def on_segmentation_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.lbl_progress_pct.setText(f"{percent}%")
        self.lbl_progress_status.setText(message)

    def on_segmentation_finished(self, result: dict):
        self.progress_group.setVisible(False)
        self.btn_run.setText("SEGMENTATION COMPLETED")
        self._set_run_available(False)

        self.full_mask = result["mask"]
        metrics = result["metrics"]

        self._set_slice_series(self.full_mask)

        self.three_d_viewer.set_data(self.full_flair, self.full_mask, self.voxel_spacing)

        core_val = float(metrics.get("active_tumor_core_mm3", 0.0))
        edema_val = float(metrics.get("peritumoral_edema_mm3", 0.0))
        total_val = float(metrics.get("total_tumor_volume_mm3", 0.0))

        self.current_metrics["active_tumor_core_mm3"] = core_val
        self.current_metrics["peritumoral_edema_mm3"] = edema_val
        self.current_metrics["total_tumor_volume_mm3"] = total_val

        self.val_total_tumor.setText(f"{total_val:,.0f} mm³")
        self.val_core.setText(f"{core_val:,.0f} mm³")
        self.val_edema.setText(f"{edema_val:,.0f} mm³")
        self.metrics_chart.update_values(core_val, edema_val, total_val)

        self.calculate_lobar_volumes()
        self.update_diagnostic_findings(core_val, edema_val)

        try:
            segmentation_id = getattr(self, "latest_segmentation_id", None)
            if segmentation_id and self.full_flair is not None:
                safe_patient = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.current_subject_name)
                output_dir = os.path.join(DATA_DIR, "segmentation_gifs", safe_patient, segmentation_id)
                gif_worker = SegmentationGifWorker(
                    self.full_flair, self.full_mask, output_dir, segmentation_id,
                    self.current_subject_name,
                    modality_dict=getattr(self, "modality_dict", {}),
                    ct_volume=getattr(self, "ct_volume", None),
                )
                gif_worker.completed.connect(self.on_segmentation_gifs_saved)
                gif_worker.failed.connect(lambda msg: print(f"Segmentation GIF export failed: {msg}"))
                self.gif_workers.append(gif_worker)
                gif_worker.finished.connect(lambda worker=gif_worker: self.gif_workers.remove(worker) if worker in self.gif_workers else None)
                gif_worker.start()
        except Exception as exc:
            print(f"Unable to start segmentation GIF export: {exc}")

        self._process_next_in_queue()

    def on_segmentation_gifs_saved(self, segmentation_id, patient_id, _output_dir, paths):
        if not segmentation_id:
            return
        self.statusBar().showMessage(f"GIFs saved locally: {_output_dir}", 6000)
        registered = []
        for path in paths:
            try:
                save_segmentation_artifact(segmentation_id, f"MRI GIF ({Path(path).stem.title()})", path)
                registered.append(path)
            except Exception as exc:
                print(f"Unable to register GIF artifact: {exc}")
        if registered:
            owner_key = getattr(self, "authenticated_user", ACTIVE_USER)
            folder_password = read_mobile_folder_password(owner_key)
            if not folder_password:
                self.statusBar().showMessage(
                    "GIFs are saved locally. Set a mobile folder password in Settings > Mobile Folder Password before cloud sync.",
                    12000,
                )
                return
            if not getattr(self, "_cloud_password", ""):
                account_password, accepted = QInputDialog.getText(
                    self, "Verify Cloud Account", "Enter your cloud account password to protect this folder:", QLineEdit.Password
                )
                if not accepted or not account_password:
                    self.statusBar().showMessage("GIFs are saved locally; cloud sharing needs account verification.", 10000)
                    return
                self._cloud_password = account_password
            worker = CloudGifSyncWorker(
                segmentation_id, patient_id, registered,
                owner_key=owner_key,
                cloud_password=getattr(self, "_cloud_password", ""),
                folder_password=folder_password,
            )
            worker.completed.connect(lambda uploaded, failures, seg_id=segmentation_id:
                                     self.on_cloud_gif_sync_finished(seg_id, uploaded, failures))
            self.cloud_sync_workers.append(worker)
            worker.finished.connect(lambda current=worker: self.cloud_sync_workers.remove(current)
                                    if current in self.cloud_sync_workers else None)
            worker.start()

    def on_cloud_gif_sync_finished(self, segmentation_id, uploaded, failures):
        for path, storage in uploaded.items():
            object_path = f"{storage['bucket']}/{storage['object_path']}"
            state = "pending" if path in failures else "synced"
            update_segmentation_artifact_sync(segmentation_id, path, state, None, object_path,
                                              failures.get(path))
        for path, error in failures.items():
            if path not in uploaded:
                update_segmentation_artifact_sync(segmentation_id, path, "pending", error=error)
        if failures:
            local_dir = os.path.dirname(next(iter(failures))) if failures else os.path.join(DATA_DIR, "segmentation_gifs")
            box = QDialog(self)
            box.setWindowTitle("GIF Cloud Sync Incomplete")
            box.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            box.setMinimumWidth(560)
            box.setStyleSheet("QDialog{background:#101820;border:1px solid #334155;border-radius:10px} QLabel{color:#e2e8f0;font-size:11px} QPushButton{background:#12374b;color:#e2e8f0;border:1px solid #31536d;border-radius:6px;padding:8px 14px} QPushButton:hover{background:#164a63}")
            layout = QVBoxLayout(box)
            heading = QLabel("GIFs were saved on this computer, but cloud upload failed.")
            heading.setWordWrap(True)
            layout.addWidget(heading)
            details = QLabel(f"Local folder:\n{local_dir}\n\n" + "\n".join(
                f"{os.path.basename(path)}: {error}" for path, error in list(failures.items())[:3]
            ))
            details.setWordWrap(True)
            layout.addWidget(details)
            buttons = QHBoxLayout()
            open_folder = QPushButton("Open GIF Folder")
            open_folder.clicked.connect(lambda: box.done(2))
            buttons.addWidget(open_folder)
            close = QPushButton("Close")
            close.clicked.connect(box.accept)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            if box.exec() == 2:
                try:
                    os.startfile(local_dir)
                except Exception as exc:
                    self.statusBar().showMessage(f"GIF folder: {local_dir} ({exc})", 12000)

    def on_segmentation_error(self, message: str):
        self.progress_group.setVisible(False)
        self.btn_run.setText("SCAN")
        self._set_run_available(True)
        QMessageBox.critical(self, "Error", f"Segmentation failed: {message}")
        self._process_next_in_queue()

    def reset_previous_patient_data(self):
        self.full_mask = None
        self.current_metrics = {}
        if hasattr(self, "all_slice_viewers"):
            self.modality_dict = {}
            self.ct_volume = None
            for viewer in self.all_slice_viewers:
                viewer.set_data(None)

        self.val_patient_id.setText(self.current_subject_name)
        self.val_patient_age.setText("--")
        self.val_patient_gender.setText("--")

        self.val_all_total.setText("-- mm³")
        self.val_total_tumor.setText("-- mm³")
        self.val_core.setText("-- mm³")
        self.val_edema.setText("-- mm³")
        self.val_frontal.setText("-- mm³")
        self.val_parietal.setText("-- mm³")
        self.val_occipital.setText("-- mm³")
        self.val_temporal.setText("-- mm³")
        self.val_cerebellum.setText("-- mm³")
        self.val_spinal_cord.setText("-- mm³")
        self.metrics_chart.update_values(0.0, 0.0, 0.0)

        self.diag_pathology.setText("Suspected Pathology:\nAwaiting Segmentation")
        self.diag_burden.setText("Cranial Vault Burden: --")
        self.diag_mass_effect.setText("Mass Effect Risk: --")
        self.diag_lobar_focus.setText("Lobar Involvement: --")
        self.diag_impression.setText("Clinical Impression:\nLoad scan and run segmentation to compute automated volumetric findings.")

        if hasattr(self, "three_d_viewer"):
            self.three_d_viewer.show_placeholder("Ctrl + I to import folder")

    def on_search_history_changed(self, text: str):
        self.refresh_history_dropdown(search_term=text)

    def refresh_history_dropdown(self, search_term=""):
        records = fetch_recent_history(search_term=search_term)
        self.combo_history.blockSignals(True)
        self.combo_history.clear()
        self.history_records_cache = records

        if not records:
            self.combo_history.addItem("No matching history")
            self.lbl_history_detail.setText("No records found.")
        else:
            for rec in records:
                self.combo_history.addItem(f"{rec[1]} — {rec[5]}")
            self.on_history_selected(0)

        self.combo_history.blockSignals(False)

    def on_history_selected(self, index: int):
        if not hasattr(self, "history_records_cache") or not self.history_records_cache:
            return
        if 0 <= index < len(self.history_records_cache):
            rec = self.history_records_cache[index]
            self.lbl_history_detail.setText(
                f"Patient: {rec[1]} ({rec[2]}yo, {rec[3]})\n"
                f"Attending Physician: {rec[4]}\n"
                f"Date & Time: {rec[5]}\n"
                f"Total Tumor: {rec[6]:,.0f} mm³ (Core: {rec[7]:,.0f} | Edema: {rec[8]:,.0f})\n"
                f"Pathology: {rec[9]}"
            )

    def prompt_patient_info_and_run(self):
        if self.full_stacked_mri is None:
            self._set_run_available(False)
            QMessageBox.information(self, "No scan imported", "Import a patient MRI folder before running tumor segmentation.")
            return
        if self.patient_metadata_confirmed:
            self.run_segmentation()
        elif self.collect_patient_metadata():
            self.run_segmentation()

    def collect_patient_metadata(self):
        dlg = PatientIntakeDialog(
            default_subject=self.current_subject_name,
            default_age=self.patient_info["patient_age"],
            default_gender=self.patient_info["patient_gender"],
            default_details=self.patient_info,
            parent=self
        )
        if dlg.exec() == QDialog.Accepted:
            self.patient_info = dlg.get_data()
            self.patient_metadata_confirmed = True
            try:
                save_patient_record(self.patient_info)
            except Exception as exc:
                QMessageBox.critical(self, "Patient details", f"Could not save patient details: {exc}")
                self.patient_metadata_confirmed = False
                return False
            self.update_patient_banner()
            return True
        return False

    def update_patient_banner(self):
        patient_label = self.patient_info.get("patient_name", self.current_subject_name)
        if self.patient_info.get("full_name"):
            patient_label = f"{self.patient_info['full_name']} · {patient_label}"
        self.val_patient_id.setText(patient_label)
        self.val_patient_id.setToolTip(patient_label)
        self.val_patient_age.setText(f"{self.patient_info['patient_age']} yo")
        self.val_patient_gender.setText(self.patient_info["patient_gender"])

    def on_tumor_opacity_changed(self, value: int):
        alpha = value / 100.0
        self.lbl_opacity.setText(f"Tumor Opacity: {value}%")
        for viewer in self.all_slice_viewers:
            viewer.set_alpha(alpha)
        self.three_d_viewer.set_alpha(alpha)

    def on_healthy_opacity_changed(self, value: int):
        alpha = value / 100.0
        self.lbl_healthy.setText(f"Healthy Tissue Opacity: {value}%")
        self.three_d_viewer.set_healthy_alpha(alpha)

    def calculate_lobar_volumes(self):
        if self.full_flair is None:
            return

        vx_vol = float(self.voxel_spacing[0] * self.voxel_spacing[1] * self.voxel_spacing[2])
        nz, ny, nx = self.full_flair.shape
        nonzero = self.full_flair[self.full_flair > 0]
        mean_val = float(np.mean(nonzero)) if len(nonzero) > 0 else 10.0

        brain_bin = self.full_flair > (mean_val * 0.35)
        total_brain_voxels = float(brain_bin.sum())
        total_brain_mm3 = total_brain_voxels * vx_vol

        z_grid, y_grid, x_grid = np.indices((nz, ny, nx))
        z_norm = z_grid / float(nz)
        y_norm = 1.0 - (y_grid / float(ny))
        x_norm = x_grid / float(nx)
        lat_dist = np.abs(x_norm - 0.5)

        spinal_cord_mask = (brain_bin & (z_norm < 0.28) & (y_norm >= 0.38) & (y_norm <= 0.62) & (lat_dist < 0.14))
        cerebellum_mask = (brain_bin & (z_norm < 0.38) & (y_norm < 0.48) & (~spinal_cord_mask))
        temporal_mask = (brain_bin & (z_norm < 0.56) & (y_norm >= 0.28) & (y_norm <= 0.72) & (lat_dist >= 0.11) & (~cerebellum_mask) & (~spinal_cord_mask))
        occipital_mask = (brain_bin & (y_norm < 0.35) & (z_norm >= 0.36) & (~cerebellum_mask) & (~temporal_mask) & (~spinal_cord_mask))
        frontal_mask = (brain_bin & (((y_norm > 0.55) & (z_norm >= 0.46)) | ((y_norm > 0.72) & (z_norm >= 0.25))) & (~temporal_mask) & (~spinal_cord_mask))
        parietal_mask = (brain_bin & (~frontal_mask) & (~occipital_mask) & (~temporal_mask) & (~cerebellum_mask) & (~spinal_cord_mask))

        self.lobe_masks = {
            "frontal": frontal_mask,
            "parietal": parietal_mask,
            "occipital": occipital_mask,
            "temporal": temporal_mask,
            "cerebellum": cerebellum_mask,
            "spinal_cord": spinal_cord_mask,
        }

        self.current_metrics["total_intracranial_mm3"] = total_brain_mm3
        self.current_metrics["frontal_mm3"] = float(frontal_mask.sum() * vx_vol)
        self.current_metrics["parietal_mm3"] = float(parietal_mask.sum() * vx_vol)
        self.current_metrics["occipital_mm3"] = float(occipital_mask.sum() * vx_vol)
        self.current_metrics["temporal_mm3"] = float(temporal_mask.sum() * vx_vol)
        self.current_metrics["cerebellum_mm3"] = float(cerebellum_mask.sum() * vx_vol)
        self.current_metrics["spinal_cord_mm3"] = float(spinal_cord_mask.sum() * vx_vol)

        self.val_all_total.setText(f"{total_brain_mm3:,.0f} mm³")
        self.val_frontal.setText(f"{self.current_metrics['frontal_mm3']:,.0f} mm³")
        self.val_parietal.setText(f"{self.current_metrics['parietal_mm3']:,.0f} mm³")
        self.val_occipital.setText(f"{self.current_metrics['occipital_mm3']:,.0f} mm³")
        self.val_temporal.setText(f"{self.current_metrics['temporal_mm3']:,.0f} mm³")
        self.val_cerebellum.setText(f"{self.current_metrics['cerebellum_mm3']:,.0f} mm³")
        self.val_spinal_cord.setText(f"{self.current_metrics['spinal_cord_mm3']:,.0f} mm³")

    def update_diagnostic_findings(self, core_mm3: float, edema_mm3: float, persist=True):
        self.latest_segmentation_id = None
        total_tumor_mm3 = core_mm3 + edema_mm3
        vx_vol = float(self.voxel_spacing[0] * self.voxel_spacing[1] * self.voxel_spacing[2])
        total_brain_mm3 = float((self.full_flair > 0).sum()) * vx_vol if self.full_flair is not None else 1.0

        involved_lobes = []
        if self.full_mask is not None and len(self.lobe_masks) > 0:
            tumor_bin = self.full_mask > 0
            for name, mask_bin in self.lobe_masks.items():
                overlap = np.logical_and(tumor_bin, mask_bin).sum()
                if overlap > 5:
                    lbl = name.replace("_", " ").capitalize()
                    involved_lobes.append(lbl + (" Lobe" if "cord" not in name and "cerebellum" not in name else ""))

        lobar_str = ", ".join(involved_lobes) if involved_lobes else "Centrum Semiovale / Deep Subcortical"

        burden_pct = (total_tumor_mm3 / total_brain_mm3) * 100.0 if total_brain_mm3 > 0 else 0.0
        core_ratio = (core_mm3 / total_tumor_mm3) if total_tumor_mm3 > 0 else 0.0

        if total_tumor_mm3 > 25000 or (core_mm3 > 4000 and core_ratio >= 0.20):
            pathology_text = "High-Grade Glioma (HGG)\nSuspected Glioblastoma (WHO Grade IV)"
            impression = "Morphology indicates aggressive High-Grade Glioma (GBM) with necrotic/enhancing core burden and significant peritumoral vasogenic edema."
            short_pathology = "HGG / Glioblastoma (Grade IV)"
        elif core_mm3 > 1000:
            pathology_text = "Anaplastic Glioma / Astrocytoma\nIntermediate Grade (WHO Grade III)"
            impression = "Moderate active core burden with localized infiltrative margins, characteristic of an intermediate-grade astrocytic neoplasm."
            short_pathology = "Astrocytoma (Grade III)"
        else:
            pathology_text = "Low-Grade Glioma (LGG)\nDiffuse Astrocytoma (WHO Grade II)"
            impression = "Predominantly non-enhancing expansile hyperintensity on FLAIR with minimal necrotic volume, consistent with Low-Grade Glioma."
            short_pathology = "LGG / Astrocytoma (Grade II)"

        if edema_mm3 > 30000 or total_tumor_mm3 > 45000:
            mass_effect = "High (Risk of Ventricular Compression / Subfalcine Shift)"
        elif edema_mm3 > 10000:
            mass_effect = "Moderate (Peritumoral Vasogenic Edema Present)"
        else:
            mass_effect = "Low (Focal Lesion; Minimal Displacement)"

        self.current_metrics["pathology"] = pathology_text.replace("\n", " - ")
        self.current_metrics["lobar_location"] = lobar_str
        self.current_metrics["mass_effect"] = mass_effect
        self.current_metrics["cranial_burden"] = f"{burden_pct:.2f}% of intracranial volume"
        self.current_metrics["impression_text"] = impression

        self.diag_pathology.setText(f"Suspected Pathology:\n{pathology_text}")
        self.diag_burden.setText(f"Cranial Vault Burden: {burden_pct:.2f}% of intracranial volume")
        self.diag_mass_effect.setText(f"Mass Effect Risk: {mass_effect}")
        self.diag_lobar_focus.setText(f"Lobar Involvement: {lobar_str}")
        self.diag_impression.setText(f"Clinical Impression:\n{impression}")

        if not persist:
            return

        try:
            global ACTIVE_USER
            lobe_metrics = {
                "total_intracranial_mm3": self.current_metrics.get("total_intracranial_mm3", 0.0),
                "frontal_mm3": self.current_metrics.get("frontal_mm3", 0.0),
                "parietal_mm3": self.current_metrics.get("parietal_mm3", 0.0),
                "occipital_mm3": self.current_metrics.get("occipital_mm3", 0.0),
                "temporal_mm3": self.current_metrics.get("temporal_mm3", 0.0),
                "cerebellum_mm3": self.current_metrics.get("cerebellum_mm3", 0.0),
                "spinal_cord_mm3": self.current_metrics.get("spinal_cord_mm3", 0.0)
            }
            self.latest_segmentation_id = save_history_record(
                active_username=ACTIVE_USER,
                subject_name=self.patient_info["patient_name"],
                patient_age=self.patient_info["patient_age"],
                patient_gender=self.patient_info["patient_gender"],
                radiologist=self.patient_info["radiologist"],
                total_tumor=total_tumor_mm3,
                core=core_mm3,
                edema=edema_mm3,
                pathology=short_pathology,
                mass_effect=mass_effect,
                impression=impression,
                lobe_metrics=lobe_metrics,
                patient_details=self.patient_info,
                source_scan_key=getattr(self, "current_scan_key", None)
            )
            self.refresh_history_dropdown(self.search_history.text())
            
            if hasattr(self, 'chat_panel') and self.chat_panel and self.chat_panel.is_online:
                import threading
                threading.Thread(target=self.chat_panel._run_cloud_backup, daemon=True).start()
        except Exception as e:
            print(f"Database save error: {e}")

    def _render_slice_rgb(self, raw_slice, mask_slice):
        mn, mx = float(raw_slice.min()), float(raw_slice.max())
        denom = mx - mn if mx > mn else 1.0
        norm = np.clip(((raw_slice - mn) / denom) * 255.0, 0, 255).astype(np.uint8)
        rgb = np.repeat(norm[:, :, np.newaxis], 3, axis=-1)
        if mask_slice is not None:
            c = mask_slice == 1
            e = mask_slice == 2
            if c.any():
                rgb[c] = (0.35 * rgb[c] + 0.65 * np.array([255, 23, 68])).astype(np.uint8)
            if e.any():
                rgb[e] = (0.40 * rgb[e] + 0.60 * np.array([118, 255, 3])).astype(np.uint8)
        return rgb

    def _get_primary_slice_idx(self, mask_3d):
        sums = [int((mask_3d[i] > 0).sum()) for i in range(mask_3d.shape[0])]
        max_idx = int(np.argmax(sums))
        return max_idx if sums[max_idx] > 0 else mask_3d.shape[0] // 2
    
    def log_report_export(self, report_type: str, file_path: str):
        try:
            from database.db_manager import save_report_log
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sr.id FROM segmentation_results sr
                JOIN patients p ON sr.patient_id = p.id
                WHERE p.patient_id = ?
                ORDER BY sr.scan_date DESC LIMIT 1
            """, (self.patient_info["patient_name"],))
            row = cursor.fetchone()
            conn.close()
            
            seg_id = row[0] if row else "UNKNOWN_SEG"
            save_report_log(seg_id, report_type, file_path)
        except Exception as e:
            print(f"Error logging report: {e}")

    def export_3d_images_only(self):
        if self.full_mask is None:
            QMessageBox.warning(self, "Export", "Please run segmentation first before exporting.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Destination Folder for 3D Images")
        if not folder:
            return
        try:
            prefix = f"{self.patient_info['patient_name']}_3D_"
            self.three_d_viewer.grab_single_isolated_snapshot(True, True, True).save(os.path.join(folder, f"{prefix}Brain_and_Tumor.png"))
            self.three_d_viewer.grab_single_isolated_snapshot(False, True, False).save(os.path.join(folder, f"{prefix}Active_Core_Only.png"))
            self.three_d_viewer.grab_single_isolated_snapshot(False, False, True).save(os.path.join(folder, f"{prefix}Edema_Only.png"))
            
            QMessageBox.information(self, "Export Successful", f"Saved 3D images to:\n{folder}")
            self.log_report_export("3D Snapshots (PNG)", folder)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not save 3D images: {str(e)}")

    def export_slices_only(self):
        if self.full_mask is None:
            QMessageBox.warning(self, "Export", "Please run segmentation first before exporting.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Destination Folder for Slices")
        if not folder:
            return
        try:
            prefix = f"{self.patient_info['patient_name']}_Slice_"
            idx_ax = self._get_primary_slice_idx(self.full_mask)
            vol_cor = np.ascontiguousarray(np.swapaxes(self.full_flair, 0, 1))
            mask_cor = np.ascontiguousarray(np.swapaxes(self.full_mask, 0, 1))
            idx_cor = self._get_primary_slice_idx(mask_cor)

            vol_sag = np.ascontiguousarray(np.swapaxes(self.full_flair, 0, 2))
            mask_sag = np.ascontiguousarray(np.swapaxes(self.full_mask, 0, 2))
            idx_sag = self._get_primary_slice_idx(mask_sag)

            plt.imsave(os.path.join(folder, f"{prefix}Axial_{idx_ax + 1}.png"), self._render_slice_rgb(self.full_flair[idx_ax], self.full_mask[idx_ax]))
            plt.imsave(os.path.join(folder, f"{prefix}Coronal_{idx_cor + 1}.png"), self._render_slice_rgb(vol_cor[idx_cor], mask_cor[idx_cor]))
            plt.imsave(os.path.join(folder, f"{prefix}Sagittal_{idx_sag + 1}.png"), self._render_slice_rgb(vol_sag[idx_sag], mask_sag[idx_sag]))
            
            QMessageBox.information(self, "Export Successful", f"Saved 2D slices to:\n{folder}")
            self.log_report_export("2D Slices (PNG)", folder)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not save 2D slices: {str(e)}")

    def export_metrics_csv(self):
        if not self.current_metrics:
            QMessageBox.warning(self, "Export", "No metrics computed yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Metrics CSV", f"{self.patient_info['patient_name']}_metrics.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Parameter", "Value", "Unit"])
                for k, v in self.current_metrics.items():
                    writer.writerow([k, v, ""])
                    
            QMessageBox.information(self, "Export Successful", f"Metrics CSV saved to:\n{path}")
            self.log_report_export("Metrics Export (CSV)", path)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not write CSV: {str(e)}")

    def export_full_bundle(self):
        if self.full_mask is None:
            QMessageBox.warning(self, "Export", "Please run segmentation first before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Diagnostic Package (ZIP)", f"{self.patient_info['patient_name']}_Dossier.zip", "ZIP Files (*.zip)")
        if not path:
            return
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                csv_file = os.path.join(tmp_dir, f"{self.patient_info['patient_name']}_Metrics.csv")
                with open(csv_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Parameter", "Value"])
                    for k, v in self.current_metrics.items():
                        writer.writerow([k, v])
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    zipf.write(csv_file, os.path.basename(csv_file))
                    
            QMessageBox.information(self, "Export Successful", f"Export bundle created at:\n{path}")
            self.log_report_export("Diagnostic Package (ZIP)", path)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not create bundle: {str(e)}")
            
    def toggle_chat_panel(self):
        if self.chat_panel.isVisible():
            self.chat_panel.hide()
            return
        screen = QApplication.primaryScreen().geometry()
        width, height = int(screen.width() * 0.8), int(screen.height() * 0.8)
        self.chat_panel.resize(width, height)
        self.chat_panel.move((screen.width() - width) // 2, (screen.height() - height) // 2)
        self.chat_panel.showNormal()
        self.chat_panel.raise_()
        self.chat_panel.activateWindow()

    def show_chat_notification(self, sender, preview):
        message = f"{sender}: {preview}" if preview else f"New message from {sender}"
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "NeuroQuant Chat Room", message,
                QSystemTrayIcon.MessageIcon.Information, 7000
            )
        else:
            self.statusBar().showMessage(message, 10000)
            QApplication.beep()

    def update_network_status_text(self, is_online):
        icon_name = "wifi-online.svg" if is_online else "wifi-offline.svg"
        icon_path = _asset_path("assets", "icons", icon_name)
        self.title_bar.lbl_network_icon.setPixmap(_render_svg(icon_path, QSize(16, 16)))
        if is_online:
            self.title_bar.lbl_network.setText("Online")
            self.title_bar.lbl_network.setStyleSheet("background: transparent; border: none; color: #00e5ff; font-weight: bold; font-size: 11px; margin-right: 10px;")
            self.title_bar.lbl_network.setToolTip("Connected to network")
        else:
            self.title_bar.lbl_network.setText("Offline")
            self.title_bar.lbl_network.setStyleSheet("background: transparent; border: none; color: #ef4444; font-weight: bold; font-size: 11px; margin-right: 10px;")
            self.title_bar.lbl_network.setToolTip("No internet connection")

    def resizeEvent(self, event):
        super().resizeEvent(event)
            
    def export_clinical_dossier(self):
        if self.full_mask is None or self.full_flair is None:
            QMessageBox.warning(self, "Export", "Please run segmentation first before exporting.")
            return

        default_filename = f"NeuroQuant_Report_{self.patient_info['patient_name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path, _ = QFileDialog.getSaveFileName(
            self, "Save Clinical PDF Report", default_filename, "PDF Files (*.pdf)"
        )
        if not pdf_path:
            return

        try:
            snap_comb = self.three_d_viewer.grab_single_isolated_snapshot(show_cortex=True, show_core=True, show_edema=True)
            snap_red = self.three_d_viewer.grab_single_isolated_snapshot(show_cortex=False, show_core=True, show_edema=False)
            snap_green = self.three_d_viewer.grab_single_isolated_snapshot(show_cortex=False, show_core=False, show_edema=True)

            with tempfile.TemporaryDirectory() as tmp_dir:
                p_comb = os.path.join(tmp_dir, "comb.png")
                p_red = os.path.join(tmp_dir, "red.png")
                p_green = os.path.join(tmp_dir, "green.png")

                snap_comb.save(p_comb, "PNG")
                snap_red.save(p_red, "PNG")
                snap_green.save(p_green, "PNG")

                img_comb = plt.imread(p_comb)
                img_red = plt.imread(p_red)
                img_green = plt.imread(p_green)

                idx_ax = self._get_primary_slice_idx(self.full_mask)
                vol_cor = np.ascontiguousarray(np.swapaxes(self.full_flair, 0, 1))
                mask_cor = np.ascontiguousarray(np.swapaxes(self.full_mask, 0, 1))
                idx_cor = self._get_primary_slice_idx(mask_cor)

                vol_sag = np.ascontiguousarray(np.swapaxes(self.full_flair, 0, 2))
                mask_sag = np.ascontiguousarray(np.swapaxes(self.full_mask, 0, 2))
                idx_sag = self._get_primary_slice_idx(mask_sag)

                with PdfPages(pdf_path) as pdf:
                    fig1 = plt.figure(figsize=(8.5, 11), dpi=180)
                    fig1.patch.set_facecolor('#ffffff')
                    left_margin, right_margin = 0.075, 0.925
                    fig1.text(left_margin, 0.958, "NEUROQUANT  |  CLINICAL QUANTIFICATION REPORT",
                              fontsize=14, fontweight='bold', color='#0f172a', va='top')
                    fig1.text(left_margin, 0.929, "Brain tumor segmentation and volumetric review",
                              fontsize=9.5, color='#475569', va='top')
                    fig1.text(left_margin, 0.905,
                              f"Prepared {datetime.now().strftime('%d %B %Y, %I:%M %p')}     •     Classification reference: WHO CNS5",
                              fontsize=8, color='#0369a1', va='top')
                    fig1.add_artist(plt.Line2D([left_margin, right_margin], [0.885, 0.885],
                                               color='#0e7490', linewidth=1.4, transform=fig1.transFigure))

                    def add_report_panel(bounds, title, content):
                        panel = fig1.add_axes(bounds)
                        panel.set_facecolor('#f8fafc')
                        for spine in panel.spines.values():
                            spine.set_edgecolor('#cbd5e1')
                            spine.set_linewidth(0.8)
                        panel.set_xticks([])
                        panel.set_yticks([])
                        panel.text(0.045, 0.91, title, transform=panel.transAxes, va='top',
                                   fontsize=9.2, fontweight='bold', color='#0e7490')
                        panel.text(0.045, 0.73, content, transform=panel.transAxes, va='top',
                                   fontsize=8.1, color='#1e293b', linespacing=1.45, wrap=True)

                    patient_text = (
                        f"Patient / scan: {self.patient_info.get('patient_name') or '—'}\n"
                        f"Age / sex: {self.patient_info.get('patient_age') or '—'} years / {self.patient_info.get('patient_gender') or '—'}\n"
                        f"Attending physician: {self.patient_info.get('radiologist') or '—'}\n"
                        "Sequences: T1, T1ce, T2, FLAIR (when available)"
                    )
                    diagnostic_text = (
                        f"Suspected pathology: {self.current_metrics.get('pathology', 'Awaiting analysis')}\n"
                        f"Lobar focus: {self.current_metrics.get('lobar_location', 'Not classified')}\n"
                        f"Intracranial burden: {self.current_metrics.get('cranial_burden', '—')}\n"
                        f"Mass effect risk: {self.current_metrics.get('mass_effect', '—')}"
                    )
                    add_report_panel([left_margin, 0.685, 0.405, 0.17], "PATIENT AND SCAN", patient_text)
                    add_report_panel([0.52, 0.685, 0.405, 0.17], "AUTOMATED ANALYSIS", diagnostic_text)

                    ax_table = fig1.add_axes([left_margin, 0.245, right_margin-left_margin, 0.405])
                    ax_table.axis('off')

                    table_data = [
                        ["Compartment", "Volume (mm³)", "Description"],
                        ["Total intracranial", f"{self.current_metrics.get('total_intracranial_mm3', 0.0):,.0f}", "Measured intracranial tissue volume"],
                        ["Total tumor", f"{self.current_metrics.get('total_tumor_volume_mm3', 0.0):,.0f}", "Combined active core and peritumoral edema"],
                        ["Active core", f"{self.current_metrics.get('active_tumor_core_mm3', 0.0):,.0f}", "Segmented active tumor component"],
                        ["Peritumoral edema", f"{self.current_metrics.get('peritumoral_edema_mm3', 0.0):,.0f}", "Segmented surrounding edema component"],
                        ["Frontal lobe", f"{self.current_metrics.get('frontal_mm3', 0.0):,.0f}", "Regional anatomical volume"],
                        ["Parietal lobe", f"{self.current_metrics.get('parietal_mm3', 0.0):,.0f}", "Regional anatomical volume"],
                        ["Occipital lobe", f"{self.current_metrics.get('occipital_mm3', 0.0):,.0f}", "Regional anatomical volume"],
                        ["Temporal lobe", f"{self.current_metrics.get('temporal_mm3', 0.0):,.0f}", "Regional anatomical volume"],
                        ["Cerebellum", f"{self.current_metrics.get('cerebellum_mm3', 0.0):,.0f}", "Regional anatomical volume"],
                        ["Spinal cord / brainstem", f"{self.current_metrics.get('spinal_cord_mm3', 0.0):,.0f}", "Regional anatomical volume"]
                    ]

                    tab = ax_table.table(cellText=table_data, colWidths=[0.31, 0.20, 0.49],
                                         bbox=[0, 0, 1, 1], cellLoc='left')
                    tab.auto_set_font_size(False)
                    tab.set_fontsize(7.8)
                    for (row_index, _col_index), cell in tab.get_celld().items():
                        cell.set_edgecolor('#dbe3ea')
                        cell.set_linewidth(0.55)
                        cell.set_height(1.0 / len(table_data))
                        cell.PAD = 0.045
                        if row_index > 0:
                            cell.set_facecolor('#ffffff' if row_index % 2 else '#f1f5f9')
                            cell.get_text().set_color('#1e293b')
                            cell.get_text().set_verticalalignment('center')
                    for j in range(3):
                        tab[(0, j)].set_facecolor('#0f172a')
                        tab[(0, j)].get_text().set_color('#ffffff')
                        tab[(0, j)].get_text().set_fontweight('bold')

                    impression = self.current_metrics.get('impression_text', 'Volumetric analysis completed.')
                    add_report_panel([left_margin, 0.105, right_margin-left_margin, 0.105],
                                     "CLINICAL IMPRESSION", textwrap.fill(
                                         impression.replace('Clinical Impression:', '').strip(), width=150))
                    fig1.add_artist(plt.Line2D([left_margin, right_margin], [0.073, 0.073],
                                               color='#cbd5e1', linewidth=0.7, transform=fig1.transFigure))
                    fig1.text(left_margin, 0.052, "NeuroQuant  •  Clinical review report",
                              fontsize=7.5, color='#64748b', va='center')
                    fig1.text(right_margin, 0.052, "Page 1 of 7", fontsize=7.5,
                              color='#64748b', ha='right', va='center')

                    pdf.savefig(fig1)
                    plt.close(fig1)

                    def generate_single_big_image_page(page_title, subtitle, img_data, page_num, total_pages=7):
                        fig_p = plt.figure(figsize=(8.5, 11), dpi=200)
                        fig_p.patch.set_facecolor('#ffffff')

                        fig_p.text(0.075, 0.96, textwrap.fill(page_title, width=78),
                                   fontsize=12, fontweight='bold', color='#0f172a', va='top')
                        fig_p.text(0.075, 0.925, textwrap.fill(subtitle, width=120),
                                   fontsize=8.5, color='#475569', va='top')

                        l_div = plt.Line2D([0.075, 0.925], [0.895, 0.895], color='#cbd5e1', linewidth=1.2, transform=fig_p.transFigure)
                        fig_p.add_artist(l_div)

                        ax_big = fig_p.add_axes([0.075, 0.095, 0.85, 0.77])
                        ax_big.axis('off')
                        ax_big.imshow(img_data, aspect='equal')

                        fig_p.add_artist(plt.Line2D([0.075, 0.925], [0.07, 0.07], color='#cbd5e1', linewidth=0.7, transform=fig_p.transFigure))
                        fig_p.text(0.075, 0.048, "NeuroQuant  •  Clinical review report",
                                   fontsize=7.5, color='#64748b', va='center')
                        fig_p.text(0.925, 0.048, f"Page {page_num} of {total_pages}",
                                   fontsize=7.5, color='#64748b', ha='right', va='center')

                        pdf.savefig(fig_p)
                        plt.close(fig_p)

                    generate_single_big_image_page(
                        "3D VOLUMETRIC RECONSTRUCTION - ANATOMICAL CORTEX & TUMOR",
                        "Translucent Parenchymal Shell with Core (Red) & Edema (Green) Visualized",
                        img_comb, 2
                    )
                    generate_single_big_image_page(
                        "3D ISOLATED PATHOLOGY - ACTIVE TUMOR CORE (RED ONLY)",
                        "Neoplastic Contrast-Enhancing Rim & Central Necrosis",
                        img_red, 3
                    )
                    generate_single_big_image_page(
                        "3D ISOLATED PATHOLOGY - PERITUMORAL EDEMA (GREEN ONLY)",
                        "Perilesional Vasogenic Fluid Expansion & Infiltrative Margins",
                        img_green, 4
                    )

                    rgb_ax = self._render_slice_rgb(self.full_flair[idx_ax], self.full_mask[idx_ax])
                    generate_single_big_image_page(
                        f"AXIAL PLANE PRIMARY LESION SLICE (#{idx_ax + 1} / {self.full_flair.shape[0]})",
                        "High-Resolution Transverse Superior-Inferior Cross Section",
                        rgb_ax, 5
                    )
                    rgb_cor = self._render_slice_rgb(vol_cor[idx_cor], mask_cor[idx_cor])
                    generate_single_big_image_page(
                        f"CORONAL PLANE PRIMARY LESION SLICE (#{idx_cor + 1} / {vol_cor.shape[0]})",
                        "High-Resolution Frontal Anterior-Posterior Cross Section",
                        rgb_cor, 6
                    )
                    rgb_sag = self._render_slice_rgb(vol_sag[idx_sag], mask_sag[idx_sag])
                    generate_single_big_image_page(
                        f"SAGITTAL PLANE PRIMARY LESION SLICE (#{idx_sag + 1} / {vol_sag.shape[0]})",
                        "High-Resolution Lateral Left-Right Cross Section",
                        rgb_sag, 7
                    )

            self.log_report_export("Clinical Dossier (PDF)", pdf_path)

            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("PDF Export Complete")
            msg_box.setText(f"Clinical PDF Report successfully generated:\n\n{pdf_path}\n\nWould you like to open the PDF file now?")
            btn_open = msg_box.addButton("Open PDF", QMessageBox.ButtonRole.AcceptRole)
            btn_close = msg_box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(btn_open)
            msg_box.exec()

            if msg_box.clickedButton() == btn_open:
                try:
                    os.startfile(pdf_path)
                except Exception:
                    import subprocess
                    subprocess.Popen([pdf_path], shell=True)

        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to generate multi-page PDF report: {str(e)}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NeuroQuantMainWindow()
    window.show()
    sys.exit(app.exec())

