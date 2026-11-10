import os
from pathlib import Path
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QIcon

class NeuroQuantSplashScreen(QWidget):
    def __init__(self):
        super().__init__()
        # Borderless, centered, always-on-top splash
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(560, 340)

        # Real window title + icon so the OS taskbar / Alt+Tab entry reads
        # "NeuroQuant" instead of falling back to the Python interpreter's own.
        self.setWindowTitle("NeuroQuant")

        self.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #122333, stop:0.45 #0d1117, stop:1 #111a29);
                border: 1px solid #1f2937;
                border-radius: 10px;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 22px;
                font-weight: bold;
                letter-spacing: 0.5px;
                border: none;
                background: transparent;
            }
            QLabel#subtitle {
                color: #00e5ff;
                font-size: 10px;
                font-weight: 600;
                letter-spacing: 1.2px;
                border: none;
                background: transparent;
            }
            QLabel#status {
                color: #ffffff;
                font-size: 11px;
                font-weight: 500;
                border: none;
                background: transparent;
            }
            QProgressBar {
                background-color: #161b22;
                border: 1px solid #30363d;
                border-radius: 3px;
                height: 5px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00838f, stop:0.5 #00bcd4, stop:1 #00e5ff);
                border-radius: 2px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 24)
        layout.setSpacing(8)

        # Stack the brand mark, name, and description vertically.
        brand_row = QVBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(6)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(104, 104)
        self.logo_label.setAlignment(Qt.AlignCenter)
        self.logo_label.setStyleSheet("border: none; background: transparent;")
        icon_path = self.load_colored_logo()
        taskbar_icon = Path(__file__).resolve().parents[2] / "assets" / "logo" / "colored.ico"
        if taskbar_icon.exists() or icon_path:
            app_icon = QIcon(str(taskbar_icon)) if taskbar_icon.exists() else QIcon(icon_path)
            self.setWindowIcon(app_icon)
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(app_icon)
        brand_row.addWidget(self.logo_label, alignment=Qt.AlignHCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(8)
        title_lbl = QLabel("NeuroQuant")
        title_lbl.setObjectName("title")
        title_lbl.setAlignment(Qt.AlignCenter)
        brand_text.addWidget(title_lbl)

        sub_lbl = QLabel("OFFLINE 3D NEUROIMAGING & VOLUMETRIC SYSTEM")
        sub_lbl.setObjectName("subtitle")
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setWordWrap(True)
        brand_text.addWidget(sub_lbl)
        brand_row.addLayout(brand_text)
        layout.addStretch(1)
        layout.addLayout(brand_row)
        layout.addStretch(1)

        # Sky-Blue Palette Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        layout.addWidget(self.progress_bar)

        # Step Text in Bright White
        self.status_lbl = QLabel("Initializing runtime environment...")
        self.status_lbl.setObjectName("status")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_lbl)

    def load_colored_logo(self):
        possible_paths = [Path(__file__).resolve().parent.parent.parent / "assets" / "logo" / "colored.png"]

        found_path = None
        for p in possible_paths:
            if p.exists() and p.is_file():
                found_path = str(p)
                break

        if found_path:
            pixmap = QPixmap(found_path).scaled(
                100, 100, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.logo_label.setPixmap(pixmap)
        else:
            self.logo_label.setText("🧠")
            self.logo_label.setStyleSheet("font-size: 42px; border: none; background: transparent;")

        return found_path

    def set_progress(self, val: int, message: str):
        self.progress_bar.setValue(val)
        self.status_lbl.setText(message)
