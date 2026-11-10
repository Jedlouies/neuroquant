import os
import time
import platform
import subprocess
from collections import deque
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QFrame, QPushButton, QSizePolicy, QButtonGroup
)
from PySide6.QtCore import Qt, QPoint, QTimer, QThread, Signal
from PySide6.QtGui import QCursor, QPainter, QColor, QPen, QBrush, QPolygonF, QFont

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from skimage import measure
from scipy.ndimage import binary_closing, binary_fill_holes, gaussian_filter, label as ndimage_label

class ToolTipPopover(QFrame):
    def __init__(self, title: str, description: str, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedWidth(320)
        self.setStyleSheet("""
            QFrame {
                background-color: #0d1117;
                border: 1px solid #30363d;
                border-radius: 6px;
            }
            QLabel#pop_title {
                color: #f0f6fc;
                font-size: 11px;
                font-weight: bold;
                border: none;
                background: transparent;
            }
            QLabel#pop_desc {
                color: #94a3b8;
                font-size: 10px;
                line-height: 1.45;
                border: none;
                background: transparent;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        lbl_title = QLabel(title)
        lbl_title.setObjectName("pop_title")
        lbl_title.setWordWrap(True)

        lbl_desc = QLabel(description)
        lbl_desc.setObjectName("pop_desc")
        lbl_desc.setWordWrap(True)

        layout.addWidget(lbl_title)
        layout.addWidget(lbl_desc)

    def leaveEvent(self, event):
        self.hide()
        super().leaveEvent(event)


class HelpInfoIcon(QPushButton):
    def __init__(self, title: str, description: str, parent=None):
        super().__init__("?", parent)
        self.info_title = title
        self.info_desc = description
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton {
                background-color: #161b22;
                color: #8b949e;
                border: 1px solid #30363d;
                border-radius: 8px;
                font-size: 10px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: #21262d;
                color: #f0f6fc;
                border-color: #484f58;
            }
        """)

        self.popover = ToolTipPopover(self.info_title, self.info_desc)
        
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(500)
        self.hover_timer.timeout.connect(self.show_popover)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(120)
        self.hide_timer.timeout.connect(self.check_auto_hide)

    def enterEvent(self, event):
        self.hide_timer.stop()
        self.hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_timer.stop()
        self.hide_timer.start()
        super().leaveEvent(event)

    def hideEvent(self, event):
        if hasattr(self, "popover") and self.popover:
            self.popover.hide()
        super().hideEvent(event)

    def show_popover(self):
        if not self.underMouse():
            return
        global_pos = self.mapToGlobal(QPoint(0, 0))
        target_x = global_pos.x() + self.width() + 8
        target_y = global_pos.y() - 6
        self.popover.move(target_x, target_y)
        self.popover.show()
        self.popover.raise_()

    def check_auto_hide(self):
        if not self.underMouse() and not self.popover.underMouse():
            self.popover.hide()


_GPU_LABEL_CACHE = None

def detect_gpu_label() -> str:
    global _GPU_LABEL_CACHE
    if _GPU_LABEL_CACHE is not None:
        return _GPU_LABEL_CACHE

    label = "CPU Inference (ONNX Runtime)"
    try:
        # Check NVIDIA System Management Interface first
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=1.5,
        )
        if res.returncode == 0 and res.stdout.strip():
            label = res.stdout.strip().split("\n")[0]
        else:
            # Fallback to ONNX Runtime providers
            import onnxruntime as ort
            if "CUDAExecutionProvider" in ort.get_available_providers():
                label = "NVIDIA CUDA GPU (ONNX Runtime)"
    except Exception:
        pass

    _GPU_LABEL_CACHE = label
    return label


class OrientationGizmoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(104, 104)
        self.elev = 22.0
        self.azim = -65.0
        self.light = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")

    def set_light(self, light: bool):
        self.light = bool(light)
        self.update()

    def set_angles(self, elev, azim):
        self.elev = float(elev)
        self.azim = float(azim)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        axis_len = 34.0
        rad_elev = np.radians(self.elev)
        rad_azim = np.radians(self.azim)

        col_x = QColor("#D32F2F") if self.light else QColor("#FF5252")
        col_y = QColor("#2E7D32") if self.light else QColor("#69F0AE")
        col_z = QColor("#0277BD") if self.light else QColor("#40C4FF")

        axes_3d = [
            ("X", np.array([1.0, 0.0, 0.0]), col_x),
            ("Y", np.array([0.0, 1.0, 0.0]), col_y),
            ("Z", np.array([0.0, 0.0, 1.0]), col_z),
        ]

        projected = []
        for name, v, color in axes_3d:
            vx, vy, vz = v
            sx = -vx * np.sin(rad_azim) + vy * np.cos(rad_azim)
            sy = -vx * np.cos(rad_azim) * np.sin(rad_elev) - vy * np.sin(rad_azim) * np.sin(rad_elev) + vz * np.cos(rad_elev)
            sz =  vx * np.cos(rad_azim) * np.cos(rad_elev) + vy * np.sin(rad_azim) * np.cos(rad_elev) + vz * np.sin(rad_elev)
            projected.append((sz, sx, -sy, name, color))

        projected.sort(key=lambda item: item[0])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#78909C")))
        painter.drawEllipse(QPoint(int(cx), int(cy)), 3, 3)

        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)

        for sz, px, py, name, color in projected:
            end_x = cx + px * axis_len
            end_y = cy + py * axis_len
            painter.setPen(QPen(color, 2.6, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPoint(int(cx), int(cy)), QPoint(int(end_x), int(end_y)))

            angle = np.arctan2(end_y - cy, end_x - cx)
            arrow_size = 8.0
            p1 = QPoint(int(end_x), int(end_y))
            p2 = QPoint(int(end_x - arrow_size * np.cos(angle - np.pi / 6)), int(end_y - arrow_size * np.sin(angle - np.pi / 6)))
            p3 = QPoint(int(end_x - arrow_size * np.cos(angle + np.pi / 6)), int(end_y - arrow_size * np.sin(angle + np.pi / 6)))
            painter.setBrush(QBrush(color))
            painter.drawPolygon(QPolygonF([p1, p2, p3]))

            lbl_x = end_x + 9.0 * np.cos(angle) - 5.0
            lbl_y = end_y + 9.0 * np.sin(angle) + 5.0
            painter.setPen(QPen(color))
            painter.drawText(QPoint(int(lbl_x), int(lbl_y)), name)
        painter.end()


def scale_spec_for_zoom(zoom_factor: float):
    """Range (mm) and tick step shown by BOTH the top and the vertical scale.
    Zooming in shows a smaller real-world span, so the numbers change with zoom."""
    scaled_mm = 100.0 / max(0.1, float(zoom_factor))
    if scaled_mm >= 75:
        return 100, 25
    if scaled_mm >= 45:
        return 50, 10
    if scaled_mm >= 25:
        return 25, 5
    return 10, 2


class TopRulerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(260, 40)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self.zoom_factor = 1.0
        self.light = False

    def set_light(self, light: bool):
        self.light = bool(light)
        self.update()

    def set_zoom(self, factor: float):
        self.zoom_factor = float(factor)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        baseline_y = 19.0
        start_x = 24.0
        ruler_len = w - 48.0
        c_line = QColor("#475569") if self.light else QColor("#cbd5e1")
        c_text = QColor("#475569") if self.light else QColor("#94a3b8")

        display_max, step = scale_spec_for_zoom(self.zoom_factor)

        painter.setPen(QPen(c_line, 1.2))
        painter.drawLine(QPoint(int(start_x), int(baseline_y)), QPoint(int(start_x + ruler_len), int(baseline_y)))

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(c_text)
        title = f"SCALE ({display_max} mm)"
        title_w = painter.fontMetrics().horizontalAdvance(title)
        painter.drawText(QPoint(int(start_x + (ruler_len - title_w) / 2.0), int(baseline_y - 8)), title)

        painter.setFont(QFont("Segoe UI", 8))
        for mm in range(0, display_max + 1, step):
            tx = start_x + (mm / float(display_max)) * ruler_len
            painter.setPen(QPen(c_line, 1.2))
            painter.drawLine(QPoint(int(tx), int(baseline_y)), QPoint(int(tx), int(baseline_y + 5)))
            painter.setPen(c_text)
            label = f"{mm}"
            painter.drawText(QPoint(int(tx - painter.fontMetrics().horizontalAdvance(label) / 2.0), int(baseline_y + 18)), label)
        painter.end()


class VerticalHeightScaleWidget(QWidget):
    """Vertical height scale bar on the middle-left. Uses the same range/tick rule as the
    top scale, so the two always change together when the 3D view is zoomed."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(68, 190)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self.zoom_factor = 1.0
        self.light = False

    def set_light(self, light: bool):
        self.light = bool(light)
        self.update()

    def set_zoom(self, factor: float):
        self.zoom_factor = float(factor)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        c_line = QColor("#475569") if self.light else QColor("#cbd5e1")
        c_text = QColor("#475569") if self.light else QColor("#94a3b8")

        display_max, step = scale_spec_for_zoom(self.zoom_factor)

        x_pos = 56.0
        top_y = 18.0
        bot_y = self.height() - 18.0
        ruler_len = bot_y - top_y

        painter.setPen(QPen(c_line, 1.2))
        painter.drawLine(QPoint(int(x_pos), int(top_y)), QPoint(int(x_pos), int(bot_y)))

        painter.setFont(QFont("Segoe UI", 8))
        for mm in range(0, display_max + 1, step):
            ty = bot_y - (mm / float(display_max)) * ruler_len   # 0 at the bottom, max at the top
            painter.setPen(QPen(c_line, 1.2))
            painter.drawLine(QPoint(int(x_pos - 4), int(ty)), QPoint(int(x_pos), int(ty)))
            painter.setPen(c_text)
            label = f"{mm}"
            lw = painter.fontMetrics().horizontalAdvance(label)
            painter.drawText(QPoint(int(x_pos - 7 - lw), int(ty + 3)), label)

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(c_text)
        title = f"HEIGHT ({display_max} mm)"
        title_w = painter.fontMetrics().horizontalAdvance(title)
        painter.save()
        painter.translate(9, top_y + (ruler_len + title_w) / 2.0)
        painter.rotate(-90)
        painter.drawText(QPoint(0, 0), title)
        painter.restore()
        painter.end()


class ThreeDViewer(QWidget):
    ZOOM_STEPS = [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]

    CANVAS_BG_DARK = "#0a0e14"
    CANVAS_BG_LIGHT = "#ffffff"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.alpha = 0.85
        self.healthy_alpha = 0.22
        self.raw_volume = None
        self.mask_data = None
        self.spacing = (1.0, 1.0, 1.0)
        self.view_mode = "Transparent"
        self._light = False
        self._placeholder_text = "Ctrl + I to import folder"
        self._swatches = {}

        self.layer_names = {
            "frontal": "Frontal Lobe",
            "parietal": "Parietal Lobe",
            "occipital": "Occipital Lobe",
            "temporal": "Temporal Lobe",
            "cerebellum": "Cerebellum",
            "spinal_cord": "Spinal Cord",
            "core": "Active Core",
            "edema": "Peritumoral Edema"
        }
        self.custom_colors = {
            "frontal": "#F59E0B",
            "parietal": "#0284C7",
            "occipital": "#F43F5E",
            "temporal": "#84CC16",
            "cerebellum": "#EA580C",
            "spinal_cord": "#FBBF24",
            "core": "#FF1744",
            "edema": "#76FF03"
        }

        self.show_layers_panel = True
        self.show_ruler_bar = True
        self.show_gizmo_axis = True
        self.show_height_scale = True

        self.visibility = {
            "cortex": True,
            "frontal": True,
            "parietal": True,
            "occipital": True,
            "temporal": True,
            "cerebellum": True,
            "spinal_cord": True,
            "core": True,
            "edema": True,
        }

        self.mesh_data = {}
        self.cortex_mesh = None
        self.centroid = np.array([0.0, 0.0, 0.0])
        self.bounding_radius = 100.0

        self.default_elev = 22.0
        self.default_azim = -65.0
        self.current_elev = self.default_elev
        self.current_azim = self.default_azim
        self.zoom_index = 1
        self._is_dragging = False
        self._last_drag_pos = None
        self._drag_render_pending = False

        self.frame_latencies = deque(maxlen=20)

        self.spin_timer = QTimer(self)
        self.spin_timer.setInterval(60)
        self.spin_timer.timeout.connect(self._auto_spin_step)

        self.resume_timer = QTimer(self)
        self.resume_timer.setSingleShot(True)
        self.resume_timer.setInterval(2000)
        self.resume_timer.timeout.connect(self._start_reset_interpolation)

        self.return_timer = QTimer(self)
        self.return_timer.setInterval(30)
        self.return_timer.timeout.connect(self._interpolate_to_default_step)
        self.drag_render_timer = QTimer(self)
        self.drag_render_timer.setInterval(33)
        self.drag_render_timer.timeout.connect(self._render_drag_frame)
        self.interp_step = 0
        self.interp_total_steps = 20
        self.start_elev = self.default_elev
        self.start_azim = self.default_azim

        self.init_ui()

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.setInterval(1500)
        self.telemetry_timer.timeout.connect(self.update_live_telemetry)
        self.telemetry_timer.start()

    @property
    def zoom_factor(self):
        return self.ZOOM_STEPS[self.zoom_index]

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.viewport_container = QWidget(self)
        self.viewport_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container_layout = QVBoxLayout(self.viewport_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        self.fig = Figure(figsize=(6, 3.8), dpi=85, facecolor="#0a0e14")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ax = self.fig.add_axes([0.0, -0.05, 1.0, 1.10], projection="3d", facecolor="#0a0e14")
        self.ax.set_axis_off()

        self.canvas.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))

        self.canvas.mpl_connect("button_press_event", self.on_mouse_press)
        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("button_release_event", self.on_mouse_release)
        self.canvas.mpl_connect("scroll_event", self.on_scroll_zoom)
        self._disable_matplotlib_drag_redraw()
        container_layout.addWidget(self.canvas)

        self.overlay_panel = QFrame(self.viewport_container)
        self.overlay_panel.setStyleSheet("""
            QFrame#legend_box {
                background-color: rgba(13, 17, 23, 240);
                border: 1px solid #30363d;
                border-radius: 6px;
            }
            QCheckBox {
                color: #c9d1d9;
                font-size: 10px;
                background: transparent;
                padding: 1px 0px;
            }
            QCheckBox::indicator {
                width: 11px;
                height: 11px;
            }
            QPushButton {
                background-color: #161f2e;
                color: #c9d1d9;
                border: 1px solid #334155;
                border-radius: 3px;
                font-size: 9px;
                padding: 2px 4px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #ffffff;
            }
        """)
        self.overlay_panel.setObjectName("legend_box")
        self.overlay_panel.setMinimumWidth(165)

        overlay_layout = QVBoxLayout(self.overlay_panel)
        overlay_layout.setContentsMargins(8, 8, 8, 8)
        overlay_layout.setSpacing(4)
        overlay_layout.setAlignment(Qt.AlignLeft)

        title_row = QHBoxLayout()
        title = QLabel("3D LAYERS & LEGEND")
        title.setStyleSheet("color: #888888; font-size: 8px; font-weight: bold; border: none; background: transparent;")
        
        info_legend = HelpInfoIcon(
            title="Anatomical Lobes (Reference Standard)",
            description="• Frontal, Parietal, Occipital, Temporal lobes, Cerebellum, and Spinal Cord layer controls.",
            parent=self
        )
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(info_legend)
        overlay_layout.addLayout(title_row)

        self.checkboxes = {}
        for key, label_text in self.layer_names.items():
            color_hex = self.custom_colors[key]
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(5)

            swatch = QFrame()
            swatch.setFixedSize(9, 9)
            swatch.setStyleSheet(f"background-color: {color_hex}; border-radius: 2px; border: none;")
            swatch.setProperty("nq_keep_dark", True)
            self._swatches[key] = swatch

            chk = QCheckBox(label_text)
            chk.setChecked(True)
            chk.toggled.connect(lambda _, k=key: self.on_layer_toggled(k))
            self.checkboxes[key] = chk

            row.addWidget(swatch)
            row.addWidget(chk)
            row.addStretch()
            overlay_layout.addLayout(row)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 3, 0, 0)
        btn_row.setSpacing(4)
        btn_tumor_only = QPushButton("Tumor Only")
        btn_tumor_only.clicked.connect(self.isolate_tumor)
        btn_all = QPushButton("Show All")
        btn_all.clicked.connect(self.show_all_layers)
        btn_row.addWidget(btn_tumor_only)
        btn_row.addWidget(btn_all)
        overlay_layout.addLayout(btn_row)

        self.top_ruler = TopRulerWidget(self.viewport_container)
        self.height_scale = VerticalHeightScaleWidget(self.viewport_container)

        self.axis_panel = QFrame(self.viewport_container)
        self.axis_panel.setStyleSheet("background: transparent; border: none;")
        axis_layout = QVBoxLayout(self.axis_panel)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        axis_layout.setSpacing(0)
        axis_layout.setAlignment(Qt.AlignCenter)

        self.gizmo = OrientationGizmoWidget()
        self.gizmo.set_angles(self.current_elev, self.current_azim)
        axis_layout.addWidget(self.gizmo, alignment=Qt.AlignCenter)

        self.lbl_angles = QLabel(f"{int(self.current_elev)}° | {int(self.current_azim)}°")
        self.lbl_angles.setAlignment(Qt.AlignCenter)
        self.lbl_angles.setStyleSheet("color: #78909c; font-size: 8px; font-weight: bold; border: none; background: transparent;")
        axis_layout.addWidget(self.lbl_angles)

        self.specs_container = QWidget(self.viewport_container)
        specs_layout = QVBoxLayout(self.specs_container)
        specs_layout.setContentsMargins(0, 0, 0, 0)
        specs_layout.setSpacing(1)
        self.specs_container.setStyleSheet("background: transparent; border: none;")

        gpu_name = detect_gpu_label()
        self.lbl_os = QLabel(f"OS: {platform.system()}", self.specs_container)
        self.lbl_cpu = QLabel(f"CPU: {os.cpu_count() or 4}-Core Processor", self.specs_container)
        self.lbl_gpu = QLabel(f"GPU: {gpu_name}", self.specs_container)
        self.lbl_speed = QLabel("Speed: 12.5 ms (80.0 FPS)", self.specs_container)

        for lbl in (self.lbl_os, self.lbl_cpu, self.lbl_gpu):
            lbl.setStyleSheet("background: transparent; border: none; color: #94a3b8; font-size: 9px; font-weight: bold;")
        self.lbl_speed.setStyleSheet("background: transparent; border: none; color: #22c55e; font-size: 9px; font-weight: bold;")

        specs_layout.addWidget(self.lbl_os)
        specs_layout.addWidget(self.lbl_cpu)
        specs_layout.addWidget(self.lbl_gpu)
        specs_layout.addWidget(self.lbl_speed)

        self.btn_reset_perspective = QPushButton("⟲ Reset Perspective", self.viewport_container)
        self.btn_reset_perspective.setToolTip("Reset Camera to Default Perspective (22°, -65°)")
        self.btn_reset_perspective.setStyleSheet("""
            QPushButton {
                background-color: rgba(22, 31, 46, 210);
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 10px;
                font-weight: bold;
                padding: 4px 8px;
            }
            QPushButton:hover { background-color: #334155; color: #ffffff; }
        """)
        self.btn_reset_perspective.clicked.connect(self.reset_perspective)

        self.mode_panel = QFrame(self.viewport_container)
        self.mode_panel.setStyleSheet("""
            QFrame { background: transparent; border: none; }
            QPushButton {
                background-color: rgba(22, 31, 46, 210);
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 10px;
                font-weight: bold;
                padding: 4px 8px;
            }
            QPushButton:hover { background-color: #334155; color: #ffffff; }
            QPushButton:checked { background-color: #1e293b; color: #ffffff; border-color: #475569; }
        """)
        mode_layout = QHBoxLayout(self.mode_panel)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(4)

        self.mode_group = QButtonGroup(self)
        self.btn_mode_real = QPushButton("Transparent")
        self.btn_mode_real.setCheckable(True)
        self.btn_mode_real.setChecked(True)
        self.btn_mode_real.clicked.connect(lambda: self.set_view_mode("Transparent"))

        self.btn_mode_lobes = QPushButton("Lobes")
        self.btn_mode_lobes.setCheckable(True)
        self.btn_mode_lobes.clicked.connect(lambda: self.set_view_mode("Lobes"))

        self.btn_mode_left = QPushButton("Left Hem.")
        self.btn_mode_left.setCheckable(True)
        self.btn_mode_left.clicked.connect(lambda: self.set_view_mode("Left Hemisphere"))

        self.btn_mode_right = QPushButton("Right Hem.")
        self.btn_mode_right.setCheckable(True)
        self.btn_mode_right.clicked.connect(lambda: self.set_view_mode("Right Hemisphere"))

        self.btn_mode_tumor = QPushButton("Tumor Only")
        self.btn_mode_tumor.setCheckable(True)
        self.btn_mode_tumor.clicked.connect(lambda: self.set_view_mode("Tumor Only"))

        for b in (self.btn_mode_real, self.btn_mode_lobes, self.btn_mode_left, self.btn_mode_right, self.btn_mode_tumor):
            self.mode_group.addButton(b)
            mode_layout.addWidget(b)

        self.zoom_panel = QFrame(self.viewport_container)
        self.zoom_panel.setStyleSheet("""
            QFrame { background: transparent; border: none; }
            QLabel { color: #94a3b8; font-size: 11px; font-weight: bold; background: transparent; border: none; }
            QPushButton { background: transparent; color: #94a3b8; border: none; font-size: 18px; font-weight: bold; width: 26px; height: 26px; }
            QPushButton:hover { color: #ffffff; }
        """)
        zoom_layout = QVBoxLayout(self.zoom_panel)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(1)
        zoom_layout.setAlignment(Qt.AlignCenter)

        self.lbl_zoom_indicator = QLabel("1.0x")
        self.lbl_zoom_indicator.setAlignment(Qt.AlignCenter)
        zoom_layout.addWidget(self.lbl_zoom_indicator)

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        zoom_layout.addWidget(self.btn_zoom_in)
        zoom_layout.addWidget(self.btn_zoom_out)

        layout.addWidget(self.viewport_container)
        self.show_placeholder("Ctrl + I to import folder")

    def set_visibility_option(self, option_key: str, visible: bool):
        if option_key == "layers":
            self.show_layers_panel = visible
        elif option_key == "ruler":
            self.show_ruler_bar = visible
        elif option_key == "gizmo":
            self.show_gizmo_axis = visible
        elif option_key == "scale":
            self.show_height_scale = visible
        self.reposition_static_overlays()

    def update_live_telemetry(self):
        try:
            if len(self.frame_latencies) > 0:
                avg_ms = sum(self.frame_latencies) / len(self.frame_latencies)
                fps = 1000.0 / max(0.1, avg_ms)
                speed_text = f"Speed: {avg_ms:.1f} ms ({fps:.1f} FPS)"
                color_hex = "#22c55e" if avg_ms < 15.0 else ("#f59e0b" if avg_ms <= 30.0 else "#ef4444")
            else:
                speed_text = "Speed: 12.5 ms (80.0 FPS)"
                color_hex = "#22c55e"
            self.lbl_speed.setText(speed_text)
            self.lbl_speed.setStyleSheet(f"background: transparent; border: none; color: {color_hex}; font-size: 9px; font-weight: bold;")
        except Exception:
            pass

    def set_view_mode(self, mode: str):
        self.view_mode = mode
        self.update_layer_visibility()

    def apply_theme(self, light: bool):
        light = bool(light)
        if light == self._light:
            return
        self._light = light
        self._sync_canvas_theme()
        self.top_ruler.set_light(light)
        self.height_scale.set_light(light)
        self.gizmo.set_light(light)

        if self.raw_volume is not None and self.cortex_mesh is not None:
            self.update_layer_visibility()
        else:
            self.show_placeholder(self._placeholder_text or "Ctrl + I to import folder")

    def apply_custom_colors(self, new_colors):
        self.custom_colors.update(new_colors)
        for key, swatch in self._swatches.items():
            swatch.setStyleSheet(f"background-color: {self.custom_colors[key]}; border-radius: 2px; border: none;")
        if self.raw_volume is not None:
            self.rebuild_meshes()

    def _sync_canvas_theme(self):
        bg = self.CANVAS_BG_LIGHT if self._light else self.CANVAS_BG_DARK
        self.fig.set_facecolor(bg)
        self.ax.set_facecolor(bg)
        self.canvas.draw_idle()

    def _auto_spin_step(self):
        if self._is_dragging or self.raw_volume is None or self.cortex_mesh is None:
            return
        self.current_azim = (self.current_azim + 0.5) % 360.0
        self.ax.view_init(elev=self.current_elev, azim=self.current_azim)
        self.gizmo.set_angles(self.current_elev, self.current_azim)
        self.lbl_angles.setText(f"{int(self.current_elev)}° | {int(self.current_azim)}°")
        self.canvas.draw_idle()

    def _disable_matplotlib_drag_redraw(self):
        callbacks = self.canvas.callbacks.callbacks.get("motion_notify_event", {})
        for callback_id, callback_ref in list(callbacks.items()):
            callback = callback_ref()
            if getattr(callback, "__self__", None) is self.ax and getattr(callback, "__name__", "") == "_on_move":
                self.canvas.mpl_disconnect(callback_id)

    def _render_drag_frame(self):
        if not self._drag_render_pending:
            self.drag_render_timer.stop()
            return
        self._drag_render_pending = False
        self.canvas.draw_idle()

    def _start_reset_interpolation(self):
        self.spin_timer.stop()
        if hasattr(self.ax, "elev") and hasattr(self.ax, "azim"):
            self.start_elev = float(self.ax.elev)
            self.start_azim = float(self.ax.azim)
        else:
            self.start_elev = float(self.current_elev)
            self.start_azim = float(self.current_azim)
        diff_azim = (self.default_azim - self.start_azim + 180.0) % 360.0 - 180.0
        self.target_azim = self.start_azim + diff_azim
        self.target_elev = self.default_elev
        self.interp_step = 0
        self.return_timer.start()

    def _interpolate_to_default_step(self):
        self.interp_step += 1
        t = self.interp_step / float(self.interp_total_steps)
        ease = 1.0 - (1.0 - t) ** 3
        cur_elev = self.start_elev + (self.target_elev - self.start_elev) * ease
        cur_azim = self.start_azim + (self.target_azim - self.start_azim) * ease
        self.current_elev = cur_elev
        self.current_azim = cur_azim
        if hasattr(self.ax, "view_init"):
            self.ax.view_init(elev=cur_elev, azim=cur_azim)
            self.gizmo.set_angles(cur_elev, cur_azim)
            self.lbl_angles.setText(f"{int(cur_elev)}° | {int(cur_azim)}°")
            self.canvas.draw_idle()
        if self.interp_step >= self.interp_total_steps:
            self.return_timer.stop()
            self.current_elev = self.default_elev
            self.current_azim = self.default_azim
            self.ax.view_init(elev=self.default_elev, azim=self.default_azim)
            self.gizmo.set_angles(self.default_elev, self.default_azim)
            self.lbl_angles.setText(f"{int(self.default_elev)}° | {int(self.default_azim)}°")
            self.canvas.draw_idle()
            self.spin_timer.start()

    def on_mouse_press(self, event):
        if event.inaxes == self.ax:
            self._is_dragging = True
            self._last_drag_pos = (event.x, event.y)
            self.spin_timer.stop()
            self.resume_timer.stop()
            self.return_timer.stop()
            self.canvas.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def on_mouse_move(self, event):
        if self._is_dragging and self._last_drag_pos is not None:
            previous_x, previous_y = self._last_drag_pos
            delta_x = event.x - previous_x
            delta_y = event.y - previous_y
            self._last_drag_pos = (event.x, event.y)

            self.current_azim = (self.current_azim - delta_x * 0.5) % 360.0
            self.current_elev = float(np.clip(self.current_elev + delta_y * 0.5, -90.0, 90.0))
            self.ax.view_init(elev=self.current_elev, azim=self.current_azim)
            self.lbl_angles.setText(f"{int(self.current_elev)}° | {int(self.current_azim)}°")
            self.gizmo.set_angles(self.current_elev, self.current_azim)
            self._drag_render_pending = True
            if not self.drag_render_timer.isActive():
                self.drag_render_timer.start()

    def on_mouse_release(self, event):
        if self._is_dragging:
            self._is_dragging = False
            self._last_drag_pos = None
            self._drag_render_pending = False
            self.drag_render_timer.stop()
            self.canvas.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            if hasattr(self.ax, "elev") and hasattr(self.ax, "azim"):
                self.current_elev = float(self.ax.elev)
                self.current_azim = float(self.ax.azim)
                self.lbl_angles.setText(f"{int(self.current_elev)}° | {int(self.current_azim)}°")
                self.gizmo.set_angles(self.current_elev, self.current_azim)
            self.resume_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reposition_static_overlays()

    def reposition_static_overlays(self):
        """
        Dynamically calculates available space and perfectly positions 
        the overlays. If the window is crushed horizontally or vertically 
        (e.g., in a split layout), non-essential UI gracefully auto-hides 
        to prevent any overlapping.
        """
        vw = self.viewport_container.width()
        vh = self.viewport_container.height()

        # --- Top Left: Legend ---
        self.overlay_panel.adjustSize()
        self.overlay_panel.move(12, 10)
        legend_bottom = (self.overlay_panel.y() + self.overlay_panel.height()) if self.show_layers_panel else 0
        legend_right = (self.overlay_panel.x() + self.overlay_panel.width()) if self.show_layers_panel else 0

        # --- Bottom Left: Specs ---
        self.specs_container.adjustSize()
        specs_y = max(10, vh - self.specs_container.height() - 12)
        self.specs_container.move(12, specs_y)

        # --- Bottom Left: Reset Button ---
        self.btn_reset_perspective.adjustSize()
        btn_reset_y = max(10, specs_y - self.btn_reset_perspective.height() - 6)
        self.btn_reset_perspective.move(12, btn_reset_y)

        # --- Top Right: Axis Panel ---
        self.axis_panel.adjustSize()
        aw = self.axis_panel.width()
        axis_x = max(10, vw - aw - 16)
        self.axis_panel.move(axis_x, 8)
        axis_left = axis_x if self.show_gizmo_axis else vw

        # --- Bottom Right: Zoom Panel ---
        self.zoom_panel.adjustSize()
        pw = self.zoom_panel.width()
        ph = self.zoom_panel.height()
        self.zoom_panel.move(max(10, vw - pw - 16), max(10, vh - ph - 10))

        # --- Bottom Center: Mode Panel ---
        self.mode_panel.adjustSize()
        mw = self.mode_panel.width()
        self.mode_panel.move(max(10, (vw - mw) // 2), max(10, vh - self.mode_panel.height() - 12))

        # --- COLLISION DETECTION & DYNAMIC POSITIONING ---

        # 1. Vertical Scale (Middle Left)
        self.height_scale.adjustSize()
        scale_h = self.height_scale.height()
        available_left_space = btn_reset_y - legend_bottom
        
        actual_show_scale = self.show_height_scale
        if available_left_space < scale_h + 10:
            actual_show_scale = False  # Hide if vertically squished
        else:
            # Perfectly center it in the available gap between legend and buttons
            scale_y = legend_bottom + (available_left_space - scale_h) // 2
            self.height_scale.move(12, int(scale_y))

        # 2. Top Ruler (Top Center)
        self.top_ruler.adjustSize()
        rw = self.top_ruler.width()
        ruler_x = (vw - rw) // 2
        
        actual_show_ruler = self.show_ruler_bar
        # Hide if it overlaps the legend (left) or axis gizmo (right)
        if self.show_ruler_bar:
            if (self.show_layers_panel and ruler_x < legend_right + 10) or \
               (self.show_gizmo_axis and ruler_x + rw > axis_left - 10):
                actual_show_ruler = False
            else:
                self.top_ruler.move(int(ruler_x), 6)

        # --- Apply Visibility ---
        self.overlay_panel.setVisible(self.show_layers_panel)
        self.axis_panel.setVisible(self.show_gizmo_axis)
        self.top_ruler.setVisible(actual_show_ruler)
        self.height_scale.setVisible(actual_show_scale)

        # --- Raise Z-Index ---
        if self.show_layers_panel: self.overlay_panel.raise_()
        if self.show_gizmo_axis: self.axis_panel.raise_()
        if actual_show_ruler: self.top_ruler.raise_()
        if actual_show_scale: self.height_scale.raise_()
        self.specs_container.raise_()
        self.btn_reset_perspective.raise_()
        self.mode_panel.raise_()
        self.zoom_panel.raise_()

    def reset_perspective(self):
        self.return_timer.stop()
        self.resume_timer.stop()
        self.current_elev = self.default_elev
        self.current_azim = self.default_azim
        self.zoom_index = 1
        self.lbl_zoom_indicator.setText("1.0x")
        self.top_ruler.set_zoom(1.0)
        self.height_scale.set_zoom(1.0)
        self.update_layer_visibility()
        self.spin_timer.start()

    def zoom_in(self):
        if self.zoom_index < len(self.ZOOM_STEPS) - 1:
            self.zoom_index += 1
            self.lbl_zoom_indicator.setText(f"{self.zoom_factor:.1f}x")
            self.top_ruler.set_zoom(self.zoom_factor)
            self.height_scale.set_zoom(self.zoom_factor)
            self.update_layer_visibility()

    def zoom_out(self):
        if self.zoom_index > 0:
            self.zoom_index -= 1
            self.lbl_zoom_indicator.setText(f"{self.zoom_factor:.1f}x")
            self.top_ruler.set_zoom(self.zoom_factor)
            self.height_scale.set_zoom(self.zoom_factor)
            self.update_layer_visibility()

    def on_scroll_zoom(self, event):
        if event.inaxes != self.ax:
            return
        if event.button == "up":
            self.zoom_in()
        elif event.button == "down":
            self.zoom_out()

    def set_healthy_alpha(self, alpha: float):
        self.healthy_alpha = float(np.clip(alpha, 0.0, 1.0))
        self.update_layer_visibility()

    def set_alpha(self, alpha: float):
        self.alpha = float(np.clip(alpha, 0.0, 1.0))
        self.update_layer_visibility()

    def on_layer_toggled(self, key: str):
        self.visibility[key] = self.checkboxes[key].isChecked()
        self.update_layer_visibility()

    def isolate_tumor(self):
        for k in ["frontal", "parietal", "occipital", "temporal", "cerebellum", "spinal_cord"]:
            self.checkboxes[k].setChecked(False)
        self.checkboxes["core"].setChecked(True)
        self.checkboxes["edema"].setChecked(True)
        self.btn_mode_tumor.setChecked(True)
        self.set_view_mode("Tumor Only")

    def show_all_layers(self):
        for k in self.checkboxes:
            self.checkboxes[k].setChecked(True)
        self.btn_mode_real.setChecked(True)
        self.set_view_mode("Transparent")

    def show_placeholder(self, text: str):
        self._placeholder_text = text
        self.spin_timer.stop()
        self.resume_timer.stop()
        self.return_timer.stop()
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.text2D(0.5, 0.5, text, color="#94a3b8", ha="center", va="center", 
                       transform=self.ax.transAxes, fontsize=13, fontweight="bold")
        self.canvas.draw_idle()
        self.reposition_static_overlays()

    def set_data(self, raw_volume: np.ndarray, mask: np.ndarray = None, spacing: tuple = (1.0, 1.0, 1.0)):
        self.raw_volume = raw_volume
        self.mask_data = mask
        self.spacing = spacing
        self.zoom_index = 1
        self.lbl_zoom_indicator.setText("1.0x")
        self.top_ruler.set_zoom(1.0)
        self.height_scale.set_zoom(1.0)
        self.rebuild_meshes()
        self.spin_timer.start()

    def rebuild_meshes(self):
        if self.raw_volume is None:
            return

        def hex_to_rgb(h):
            h = h.lstrip('#')
            return np.array([int(h[i:i+2], 16) for i in (0, 2, 4)]) / 255.0

        sz, sy, sx = self.spacing[2], self.spacing[1], self.spacing[0]
        nz, ny, nx = self.raw_volume.shape
        nonzero = self.raw_volume[self.raw_volume > 0]
        mean_val = float(np.mean(nonzero)) if len(nonzero) > 0 else 10.0

        brain_bin = self.raw_volume > (mean_val * 0.35)
        if brain_bin.sum() < 400:
            self.update_layer_visibility()
            return

        verts_raw, faces, _, _ = measure.marching_cubes(brain_bin, level=0.5, step_size=4, spacing=(sz, sy, sx))
        verts = np.stack([verts_raw[:, 2], (ny * sy) - verts_raw[:, 1], verts_raw[:, 0]], axis=-1)

        min_bound = np.min(verts, axis=0)
        max_bound = np.max(verts, axis=0)
        self.centroid = (min_bound + max_bound) / 2.0
        self.bounding_radius = float(np.max(max_bound - min_bound) / 2.0)

        face_centers = verts_raw[faces].mean(axis=1)
        zc = face_centers[:, 0] / (nz * sz)
        yc = 1.0 - (face_centers[:, 1] / (ny * sy))
        xc = face_centers[:, 2] / (nx * sx)
        lat_dist = np.abs(xc - 0.5)

        is_stem = (zc < 0.28) & (yc >= 0.38) & (yc <= 0.62) & (lat_dist < 0.14)
        is_cereb = (zc < 0.38) & (yc < 0.48) & (~is_stem)
        is_temp = ((zc < 0.56) & (yc >= 0.28) & (yc <= 0.72) & (lat_dist >= 0.11) & (~is_cereb) & (~is_stem))
        is_occip = (yc < 0.35) & (zc >= 0.36) & (~is_cereb) & (~is_temp) & (~is_stem)
        is_front = (((yc > 0.55) & (zc >= 0.46)) | ((yc > 0.72) & (zc >= 0.25))) & (~is_temp) & (~is_stem)
        is_pariet = (~is_front) & (~is_occip) & (~is_temp) & (~is_cereb) & (~is_stem)

        is_left_hem = xc < 0.5
        is_right_hem = xc >= 0.5

        self.cortex_mesh = {
            "verts": verts,
            "faces": faces,
            "face_groups": {
                "frontal": is_front,
                "parietal": is_pariet,
                "occipital": is_occip,
                "temporal": is_temp,
                "cerebellum": is_cereb,
                "spinal_cord": is_stem,
                "left_hem": is_left_hem,
                "right_hem": is_right_hem
            }
        }

        self.lobe_colors = {
            "frontal": hex_to_rgb(self.custom_colors["frontal"]),
            "parietal": hex_to_rgb(self.custom_colors["parietal"]),
            "occipital": hex_to_rgb(self.custom_colors["occipital"]),
            "temporal": hex_to_rgb(self.custom_colors["temporal"]),
            "cerebellum": hex_to_rgb(self.custom_colors["cerebellum"]),
            "spinal_cord": hex_to_rgb(self.custom_colors["spinal_cord"])
        }

        self.mesh_data = {}
        if self.mask_data is not None:
            edema_bin = (self.mask_data == 2)
            if edema_bin.sum() > 20:
                try:
                    ve, fe, _, _ = measure.marching_cubes(edema_bin, level=0.5, step_size=3, spacing=(sz, sy, sx))
                    ve_aligned = np.stack([ve[:, 2], (ny * sy) - ve[:, 1], ve[:, 0]], axis=-1)
                    self.mesh_data["edema"] = (ve_aligned, fe, self.custom_colors["edema"])
                except Exception:
                    pass

            core_bin = (self.mask_data == 1)
            if core_bin.sum() > 20:
                try:
                    vc, fc, _, _ = measure.marching_cubes(core_bin, level=0.5, step_size=3, spacing=(sz, sy, sx))
                    vc_aligned = np.stack([vc[:, 2], (ny * sy) - vc[:, 1], vc[:, 0]], axis=-1)
                    self.mesh_data["core"] = (vc_aligned, fc, self.custom_colors["core"])
                except Exception:
                    pass

        self.update_layer_visibility()

    def update_layer_visibility(self):
        if self.raw_volume is None or self.cortex_mesh is None:
            return

        t_start = time.time()
        self.ax.clear()
        self.ax.set_axis_off()

        has_mesh = False
        c = self.centroid
        z_factor = self.zoom_factor
        source_verts, faces = self.cortex_mesh["verts"], self.cortex_mesh["faces"]
        verts = (source_verts - c) * z_factor + c
        num_faces = len(faces)

        if self.view_mode != "Tumor Only":
            face_colors = np.zeros((num_faces, 4), dtype=np.float32)

            if self.view_mode == "Transparent":
                brain_rgb = (150, 108, 88) if self._light else (226, 201, 182)
                face_colors[:, :3] = np.array(brain_rgb) / 255.0
                face_colors[:, 3] = float(np.clip(self.healthy_alpha, 0.05, 0.40))
            elif self.view_mode == "Lobes":
                base_alpha = float(np.clip(self.healthy_alpha * 1.5, 0.10, 0.70))
                for key, is_group in self.cortex_mesh["face_groups"].items():
                    if key in self.lobe_colors and is_group.any():
                        rgb = self.lobe_colors[key]
                        alpha = base_alpha if self.visibility.get(key, True) else 0.03
                        face_colors[is_group, :3] = rgb
                        face_colors[is_group, 3] = alpha
            elif self.view_mode == "Left Hemisphere":
                brain_rgb = (150, 108, 88) if self._light else (226, 201, 182)
                is_left = self.cortex_mesh["face_groups"]["left_hem"]
                face_colors[:, :3] = np.array(brain_rgb) / 255.0
                face_colors[is_left, 3] = float(np.clip(self.healthy_alpha * 1.8, 0.15, 0.60))
                face_colors[~is_left, 3] = 0.01
            elif self.view_mode == "Right Hemisphere":
                brain_rgb = (150, 108, 88) if self._light else (226, 201, 182)
                is_right = self.cortex_mesh["face_groups"]["right_hem"]
                face_colors[:, :3] = np.array(brain_rgb) / 255.0
                face_colors[is_right, 3] = float(np.clip(self.healthy_alpha * 1.8, 0.15, 0.60))
                face_colors[~is_right, 3] = 0.01

            cortex_collection = Poly3DCollection(
                verts[faces], facecolors=face_colors, edgecolor="none", rasterized=True
            )
            cortex_collection.set_clip_on(False)
            self.ax.add_collection3d(cortex_collection)
            has_mesh = True

        if self.visibility.get("edema", True) and "edema" in self.mesh_data:
            edema_mesh = self.mesh_data["edema"]
            ve, fe = edema_mesh[0], edema_mesh[1]
            col_e = edema_mesh[2] if len(edema_mesh) > 2 else self.custom_colors["edema"]
            scaled_ve = (ve - c) * z_factor + c
            edema_collection = Poly3DCollection(
                scaled_ve[fe], alpha=float(np.clip(self.alpha * 0.85, 0.2, 1.0)),
                facecolor=col_e, edgecolor="none", rasterized=True
            )
            edema_collection.set_clip_on(False)
            self.ax.add_collection3d(edema_collection)
            has_mesh = True

        if self.visibility.get("core", True) and "core" in self.mesh_data:
            core_mesh = self.mesh_data["core"]
            vc, fc = core_mesh[0], core_mesh[1]
            col_c = core_mesh[2] if len(core_mesh) > 2 else self.custom_colors["core"]
            scaled_vc = (vc - c) * z_factor + c
            core_collection = Poly3DCollection(
                scaled_vc[fc], alpha=float(np.clip(self.alpha, 0.3, 1.0)),
                facecolor=col_c, edgecolor="none", rasterized=True
            )
            core_collection.set_clip_on(False)
            self.ax.add_collection3d(core_collection)
            has_mesh = True

        if has_mesh:
            r = self.bounding_radius * 1.02
            cx, cy, cz = self.centroid
            self.ax.set_xlim(cx - r, cx + r)
            self.ax.set_ylim(cy - r, cy + r)
            self.ax.set_zlim(cz - r, cz + r)

            r_marker = r * 0.98
            f_opt = dict(fontsize=8, fontweight="bold", color="#757575", ha="center", va="center")
            self.ax.text(cx, cy + r_marker, cz, "A", **f_opt)
            self.ax.text(cx, cy - r_marker, cz, "P", **f_opt)
            self.ax.text(cx - r_marker, cy, cz, "L", **f_opt)
            self.ax.text(cx + r_marker, cy, cz, "R", **f_opt)
            self.ax.text(cx, cy, cz + r_marker, "S", **f_opt)

            self.ax.view_init(elev=self.current_elev, azim=self.current_azim)
            self.gizmo.set_angles(self.current_elev, self.current_azim)
            self.lbl_angles.setText(f"{int(self.current_elev)}° | {int(self.current_azim)}°")
        else:
            self.show_placeholder("Ctrl + I to import folder")

        self.canvas.draw_idle()
        self.reposition_static_overlays()

        elapsed_ms = (time.time() - t_start) * 1000.0
        self.frame_latencies.append(max(1.0, elapsed_ms))

    def grab_single_isolated_snapshot(self, show_cortex: bool, show_core: bool, show_edema: bool, elev=22, azim=-65):
        was_spinning = self.spin_timer.isActive()
        self.spin_timer.stop()
        self.resume_timer.stop()
        self.return_timer.stop()

        old_elev = self.current_elev
        old_azim = self.current_azim
        old_mode = self.view_mode
        old_core = self.visibility["core"]
        old_edema = self.visibility["edema"]

        old_light = self._light
        self._light = False
        self._sync_canvas_theme()

        self.overlay_panel.hide()
        self.axis_panel.hide()
        self.top_ruler.hide()
        self.height_scale.hide()
        self.specs_container.hide()
        self.btn_reset_perspective.hide()
        self.mode_panel.hide()
        self.zoom_panel.hide()

        self.view_mode = "Transparent" if show_cortex else "Tumor Only"
        self.visibility["core"] = show_core
        self.visibility["edema"] = show_edema

        self.current_elev = elev
        self.current_azim = azim
        self.update_layer_visibility()
        self.canvas.draw()
        pix = self.canvas.grab()

        self.current_elev = old_elev
        self.current_azim = old_azim
        self.view_mode = old_mode
        self.visibility["core"] = old_core
        self.visibility["edema"] = old_edema
        self._light = old_light
        self._sync_canvas_theme()
        self.update_layer_visibility()

        self.overlay_panel.show()
        self.top_ruler.show()
        self.height_scale.show()
        self.axis_panel.show()
        self.specs_container.show()
        self.btn_reset_perspective.show()
        self.mode_panel.show()
        self.zoom_panel.show()

        if was_spinning:
            self.spin_timer.start()

        return pix
