import numpy as np
from PySide6.QtWidgets import QWidget, QLabel, QSlider, QVBoxLayout, QHBoxLayout, QSizePolicy, QFrame, QPushButton, QDialog, QComboBox
from PySide6.QtCore import Qt, QEvent, QTimer, Signal, QPoint
from PySide6.QtGui import QImage, QPixmap, QCursor, QPainter, QColor, QIcon
from pathlib import Path

class ToolTipPopover(QFrame):
    """Floating borderless dark panel that shows on hover and vanishes immediately on mouse exit."""
    def __init__(self, title: str, description: str, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedWidth(300)
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


class SliceHelpIcon(QPushButton):
    """Small question mark icon for 2D slice viewers with popover help."""
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

        screen = self.screen()
        if screen:
            screen_geo = screen.availableGeometry()
            if target_x + self.popover.width() > screen_geo.right():
                target_x = global_pos.x() - self.popover.width() - 8
            if target_y + self.popover.height() > screen_geo.bottom():
                target_y = screen_geo.bottom() - self.popover.height() - 10

        self.popover.move(target_x, target_y)
        self.popover.show()
        self.popover.raise_()

    def check_auto_hide(self):
        if not self.underMouse() and not self.popover.underMouse():
            self.popover.hide()


class SliceViewer(QWidget):
    def __init__(self, plane_name="Axial", parent=None):
        super().__init__(parent)
        self.plane_name = plane_name
        self.volume_data = None
        self.mask_data = None
        self.current_slice = 0
        self.alpha = 0.45
        self.zoom_factor = 1.0
        self.pan_offset = QPoint(0, 0)
        self._is_panning = False
        self._pan_start_pos = QPoint()

        self.play_interval = 100
        self.playing = False
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(self.play_interval)
        self.play_timer.timeout.connect(self._advance_slice)

        self.init_ui()

    OVERLAY_MARGIN = 6

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        # The black canvas. Supports mouse drag to pan and scroll to zoom.
        self.display_label = QLabel()
        self.display_label.setObjectName("sliceCanvas")
        self.display_label.setMinimumSize(180, 180)
        self.display_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.display_label.setStyleSheet(
            "QLabel#sliceCanvas { background-color: #000000; border: none; border-radius: 0px; }"
        )
        self.display_label.setAlignment(Qt.AlignCenter)
        self.display_label.installEventFilter(self)
        layout.addWidget(self.display_label)

        # Top-left overlay: plane name
        self.header_bar = QFrame(self.display_label)
        self.header_bar.setObjectName("sliceHeader")
        self.header_bar.setStyleSheet(
            "QFrame#sliceHeader { background-color: rgba(0, 0, 0, 150); border: none; border-radius: 3px; }"
            "QFrame#sliceHeader QLabel { background: transparent; border: none; color: #ffffff;"
            " font-size: 14px; font-weight: bold; }"
        )
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(6, 2, 6, 2)
        header_layout.setSpacing(6)
        self.header_layout = header_layout

        self.header_label = QLabel(self.plane_name or "Scan")
        self.header_label.setStyleSheet("font-size:11px;font-weight:bold;color:#ffffff;background:transparent;border:none")
        header_layout.addWidget(self.header_label)

        # Add '?' info icon to slice header
        help_desc = (
            f"• {self.plane_name} Slice Inspector:\n\n"
            "• Use the slider to step through slices.\n"
            "• Scroll to zoom in/out, or click & drag to pan across the image.\n"
            "• Colored overlays highlight segmented tumor core (Red) and peritumoral edema (Green)."
        )
        self.help_icon = SliceHelpIcon(f"{self.plane_name} Viewport Guide", help_desc, self)
        header_layout.addWidget(self.help_icon)
        self.fullscreen_button = QPushButton()
        self.fullscreen_button.setFixedSize(18, 18)
        self.fullscreen_button.setToolTip("Open this scan in a fullscreen viewer")
        icon_path = Path(__file__).resolve().parents[2] / "assets" / "icons" / "fullscreen.svg"
        if icon_path.exists():
            self.fullscreen_button.setIcon(QIcon(str(icon_path)))
        else:
            self.fullscreen_button.setText("⛶")
        self.fullscreen_button.setStyleSheet("QPushButton{background:transparent;border:none;color:#fff;padding:0} QPushButton:hover{background:#334155;border-radius:3px}")
        self.fullscreen_button.clicked.connect(self.open_fullscreen)
        header_layout.addWidget(self.fullscreen_button)

        self.header_bar.setVisible(bool(self.plane_name))

        # Bottom-right overlay: current slice / total
        self.slice_num_label = QLabel("0/0", self.display_label)
        self.slice_num_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 150); color: #e2e8f0; font-size: 10px;"
            " border: none; border-radius: 3px; padding: 1px 6px;"
        )
        self.slice_num_label.adjustSize()

        slider_layout = QHBoxLayout()
        slider_layout.setContentsMargins(0, 0, 0, 0)
        slider_layout.setSpacing(4)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setFixedHeight(16)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self.on_slider_changed)

        slider_layout.addWidget(self.slider)
        layout.addLayout(slider_layout)

        # Playback + image zoom controls tightly grouped together without space between
        controls = QHBoxLayout()
        controls.setContentsMargins(2, 0, 2, 0)
        controls.setSpacing(2)

        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedSize(28, 20)
        self.btn_play.setToolTip("Play / pause automatic slice playback")
        self.btn_play.clicked.connect(self.toggle_playback)

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedSize(24, 20)
        self.btn_zoom_out.clicked.connect(lambda: self.set_zoom(self.zoom_factor - 0.25))
        self.btn_zoom_reset = QPushButton("100%")
        self.btn_zoom_reset.setFixedSize(42, 20)
        self.btn_zoom_reset.clicked.connect(lambda: self.set_zoom(1.0))
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedSize(24, 20)
        self.btn_zoom_in.clicked.connect(lambda: self.set_zoom(self.zoom_factor + 0.25))

        for button in (self.btn_play, self.btn_zoom_out, self.btn_zoom_reset, self.btn_zoom_in):
            button.setStyleSheet(
                "QPushButton { background:#161f2e; color:#e2e8f0; border:1px solid #334155; "
                "border-radius:3px; font-size:10px; font-weight:bold; padding:0px; }"
                "QPushButton:hover { background:#00838f; color:#ffffff; }"
            )

        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_zoom_out)
        controls.addWidget(self.btn_zoom_reset)
        controls.addWidget(self.btn_zoom_in)
        controls.addStretch()  # Keep controls aligned to the left side together
        layout.addLayout(controls)

        self._place_overlays()

    def add_header_widget(self, widget: QWidget):
        self.header_layout.addWidget(widget)
        self.header_bar.setVisible(True)
        self._place_overlays()

    def open_fullscreen(self):
        if self.volume_data is None:
            return
        dialog = QDialog(self.window())
        view_title = self.plane_name or "Scan"
        dialog.setWindowTitle(f"{view_title} Viewer")
        dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.Window)
        dialog.setStyleSheet("QDialog{background:#080808}")
        layout = QVBoxLayout(dialog)
        viewer = SliceViewer(self.plane_name)
        viewer.fullscreen_button.hide()
        viewer.volume_data = self.volume_data
        viewer.mask_data = self.mask_data
        viewer.slider.setMaximum(self.slider.maximum())
        viewer.slider.setEnabled(self.slider.isEnabled())
        viewer.slider.setValue(self.slider.value())
        viewer.set_alpha(self.alpha)
        viewer.render_slice()
        for control in (viewer.btn_play, viewer.btn_zoom_out, viewer.btn_zoom_reset, viewer.btn_zoom_in):
            control.hide()
        toolbar = QHBoxLayout()
        instruction = QLabel("Scroll or use Zoom controls to enlarge · Press Esc or select Exit Fullscreen to return")
        instruction.setStyleSheet("color:#cbd5e1;font-size:12px;background:transparent")
        toolbar.addWidget(instruction)
        toolbar.addStretch()
        btn_play = QPushButton("▶ Play")
        btn_zoom_out = QPushButton("− Zoom")
        btn_zoom_reset = QPushButton("100%")
        btn_zoom_in = QPushButton("+ Zoom")
        btn_exit = QPushButton("Exit Fullscreen")
        for button in (btn_play, btn_zoom_out, btn_zoom_reset, btn_zoom_in, btn_exit):
            button.setFixedHeight(34)
            button.setStyleSheet("QPushButton{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:5px;padding:5px 12px} QPushButton:hover{background:#334155}")
            toolbar.addWidget(button)
        btn_play.clicked.connect(lambda: (viewer.toggle_playback(), btn_play.setText("Ⅱ Pause" if viewer.playing else "▶ Play")))
        btn_zoom_out.clicked.connect(lambda: viewer.set_zoom(viewer.zoom_factor - 0.25))
        btn_zoom_reset.clicked.connect(lambda: viewer.set_zoom(1.0))
        btn_zoom_in.clicked.connect(lambda: viewer.set_zoom(viewer.zoom_factor + 0.25))
        btn_exit.clicked.connect(dialog.accept)
        layout.addLayout(toolbar)
        layout.addWidget(viewer)
        self.slider.valueChanged.connect(viewer.slider.setValue)
        viewer.slider.valueChanged.connect(self.slider.setValue)
        dialog.resize(1100, 800)
        dialog.showFullScreen()
        dialog.exec()

    def eventFilter(self, obj, event):
        if obj is self.display_label:
            if event.type() == QEvent.Type.Resize:
                self._place_overlays()
            elif event.type() == QEvent.Type.Wheel:
                self.wheel_zoom(event)
                return True
            elif event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
                self._is_panning = True
                self._pan_start_pos = event.pos()
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                return True
            elif event.type() == QEvent.Type.MouseMove and self._is_panning:
                delta = event.pos() - self._pan_start_pos
                self._pan_start_pos = event.pos()
                self.pan_offset += delta
                self.render_slice()
                return True
            elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._is_panning:
                    self._is_panning = False
                    self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
                return True
        return super().eventFilter(obj, event)

    def _place_overlays(self):
        m = self.OVERLAY_MARGIN
        self.header_bar.adjustSize()
        self.header_bar.move(m, m)
        self.slice_num_label.adjustSize()
        self.slice_num_label.move(
            max(m, self.display_label.width() - self.slice_num_label.width() - m),
            max(m, self.display_label.height() - self.slice_num_label.height() - m),
        )
        self.header_bar.raise_()
        self.slice_num_label.raise_()

    def set_alpha(self, alpha: float):
        self.alpha = float(np.clip(alpha, 0.0, 1.0))
        self.render_slice()

    def set_data(self, volume: np.ndarray, mask: np.ndarray = None):
        if volume is None:
            self.volume_data = None
            self.mask_data = None
            self.slider.setRange(0, 0)
            self.slider.setEnabled(False)
            self.slice_num_label.setText("0/0")
            self.display_label.clear()
            self._place_overlays()
            return

        if self.plane_name == "Coronal":
            self.volume_data = np.ascontiguousarray(np.swapaxes(volume, 0, 1))
            self.mask_data = np.ascontiguousarray(np.swapaxes(mask, 0, 1)) if mask is not None else None
        elif self.plane_name == "Sagittal":
            self.volume_data = np.ascontiguousarray(np.swapaxes(volume, 0, 2))
            self.mask_data = np.ascontiguousarray(np.swapaxes(mask, 0, 2)) if mask is not None else None
        else:
            self.volume_data = np.ascontiguousarray(volume)
            self.mask_data = np.ascontiguousarray(mask) if mask is not None else None

        num_slices = self.volume_data.shape[0]
        self.slider.setMaximum(num_slices - 1)
        self.slider.setValue(num_slices // 2)
        self.slider.setEnabled(True)
        self.pan_offset = QPoint(0, 0)
        self.render_slice()

    def on_slider_changed(self, value):
        self.current_slice = value
        self.slice_num_label.setText(f"{value + 1}/{self.slider.maximum() + 1}")
        self._place_overlays()
        self.render_slice()

    def toggle_playback(self):
        if self.volume_data is None or self.slider.maximum() <= 0:
            return
        if self.playing:
            self.playing = False
            self.play_timer.stop()
            self.btn_play.setText("▶")
        else:
            self.playing = True
            self.play_timer.start()
            self.btn_play.setText("Ⅱ")

    def _advance_slice(self):
        if self.volume_data is None or self.slider.maximum() <= 0:
            self.toggle_playback()
            return
        next_value = self.slider.value() + 1
        if next_value > self.slider.maximum():
            next_value = 0
        self.slider.setValue(next_value)

    def set_zoom(self, factor: float):
        self.zoom_factor = float(np.clip(factor, 0.5, 4.0))
        if self.zoom_factor == 1.0:
            self.pan_offset = QPoint(0, 0)
        self.btn_zoom_reset.setText(f"{int(self.zoom_factor * 100)}%")
        self.render_slice()

    def wheel_zoom(self, event):
        delta = event.angleDelta().y()
        self.set_zoom(self.zoom_factor + (0.25 if delta > 0 else -0.25))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render_slice()

    def render_slice(self):
        if self.volume_data is None:
            return

        raw_slice = self.volume_data[self.current_slice]
        min_v, max_v = float(raw_slice.min()), float(raw_slice.max())
        denom = max_v - min_v if max_v > min_v else 1.0
        norm_slice = np.clip(((raw_slice - min_v) / denom) * 255.0, 0, 255).astype(np.uint8)

        h, w = norm_slice.shape
        rgb_img = np.repeat(norm_slice[:, :, np.newaxis], 3, axis=-1)

        if self.mask_data is not None and self.alpha > 0.0:
            mask_slice = self.mask_data[self.current_slice]
            core_mask = mask_slice == 1
            edema_mask = mask_slice == 2

            if core_mask.any():
                rgb_img[core_mask] = (
                    (1.0 - self.alpha) * rgb_img[core_mask] + self.alpha * np.array([255, 23, 68], dtype=np.float32)
                ).astype(np.uint8)
            if edema_mask.any():
                rgb_img[edema_mask] = (
                    (1.0 - self.alpha) * rgb_img[edema_mask] + self.alpha * np.array([118, 255, 3], dtype=np.float32)
                ).astype(np.uint8)

        rgb_img = np.ascontiguousarray(rgb_img)
        q_img = QImage(rgb_img.data, w, h, 3 * w, QImage.Format.Format_RGB888)

        canvas_w = max(10, self.display_label.width())
        canvas_h = max(10, self.display_label.height())
        base_pixmap = QPixmap.fromImage(q_img)
        fit_pixmap = base_pixmap.scaled(
            canvas_w, canvas_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )

        scaled_pixmap = fit_pixmap.scaled(
            max(1, int(fit_pixmap.width() * self.zoom_factor)),
            max(1, int(fit_pixmap.height() * self.zoom_factor)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

        final_pixmap = QPixmap(canvas_w, canvas_h)
        final_pixmap.fill(QColor("#080808"))
        
        painter = QPainter(final_pixmap)
        dx = (canvas_w - scaled_pixmap.width()) // 2 + self.pan_offset.x()
        dy = (canvas_h - scaled_pixmap.height()) // 2 + self.pan_offset.y()
        painter.drawPixmap(dx, dy, scaled_pixmap)
        painter.end()

        self.display_label.setPixmap(final_pixmap)

    def grab_slice_snapshot(self) -> QPixmap:
        overlays = [self.header_bar, self.slice_num_label]
        was_hidden = [w.isHidden() for w in overlays]
        for w in overlays:
            w.hide()
        pixmap = self.display_label.grab()
        for w, hidden in zip(overlays, was_hidden):
            if not hidden:
                w.show()
        return pixmap
