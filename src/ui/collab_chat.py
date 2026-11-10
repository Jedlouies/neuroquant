import urllib.request
import os
import re
import html
import mimetypes
import tempfile
import shutil
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Signal, Qt, QObject, QPoint, QThread, QSize, QRect, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QApplication,
    QListWidgetItem, QLineEdit, QPushButton, QLabel, QMessageBox, QFileDialog, QFrame, QDialog,
    QSizePolicy, QStackedWidget, QScrollArea, QGridLayout, QButtonGroup
)
from PySide6.QtGui import (
    QColor, QPixmap, QIcon, QShortcut, QKeySequence, QMouseEvent,
    QPainter, QPainterPath, QFontMetrics, QFont, QPen, QBrush, QImage
)

from database.firestore_manager import (
    initialize_firestore, send_chat_message, update_user_status, listen_for_messages,
    listen_for_all_users, toggle_message_reaction, delete_chat_message
)
from database.supabase_storage import upload_attachment, fetch_bytes, is_configured as supabase_configured
from ui.brain_diagnostic_chat import recolor_svg
from ui.theme_manager import ThemeManager

IMAGE_EXTS = "*.png *.jpg *.jpeg *.gif *.bmp *.webp"


class NetworkCheckWorker(QThread):
    checked = Signal(bool)

    def run(self):
        # A response from any public HTTPS endpoint proves that internet access
        # is available. HTTP errors still prove connectivity; only transport
        # failures mean the machine is offline.
        endpoints = (
            "https://www.google.com/generate_204",
            "https://www.cloudflare.com/cdn-cgi/trace",
        )
        for url in endpoints:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "NeuroQuant/1.0"})
                with urllib.request.urlopen(request, timeout=3):
                    self.checked.emit(True)
                    return
            except urllib.error.HTTPError:
                self.checked.emit(True)
                return
            except Exception:
                continue
        self.checked.emit(False)

def create_svg(name, svg_content):
    path = os.path.join(tempfile.gettempdir(), f"nq_svg_{name}.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg_content)
    return path

def _svg(inner, size=24, stroke="#cbd5e1", fill="none"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{inner}</svg>')

_THUMB = '<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path>'
_TRASH = '<polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>'
_REPLY = '<polyline points="9 17 4 12 9 7"></polyline><path d="M20 18v-2a4 4 0 0 0-4-4H4"></path>'

IMAGE_PATH = create_svg("image", _svg('<rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline>'))
FILE_PATH = create_svg("file", _svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line>'))
SEND_PATH = create_svg("send", _svg('<line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>', stroke="#ffffff"))
DM_PATH = create_svg("dm", _svg('<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline>', stroke="#64748b"))
MENU_PATH = create_svg("menu", _svg('<line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="18" x2="21" y2="18"></line>', size=16))
SEARCH_PATH = create_svg("search", _svg('<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>', size=14))
DOWNLOAD_PATH = create_svg("download", _svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line>', size=16, stroke="#ffffff"))
UNSEND_PATH = create_svg("unsend", _svg(_TRASH, size=13, stroke="#94a3b8"))
REPLY_PATH = create_svg("reply", _svg(_REPLY, size=13, stroke="#94a3b8"))

LIKE_GRAY = create_svg("like_gray", _svg(_THUMB, size=14, stroke="#64748b"))
LIKE_LIGHT = create_svg("like_light", _svg(_THUMB, size=14, stroke="#bae6fd"))
LIKED_BLUE = create_svg("liked_blue", _svg(_THUMB, size=14, stroke="#0ea5e9", fill="#0ea5e9"))
LIKED_WHITE = create_svg("liked_white", _svg(_THUMB, size=14, stroke="#ffffff", fill="#ffffff"))

def is_remote(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))

def file_badge(name):
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    colours = {
        "pdf": "#ef4444", "doc": "#3b82f6", "docx": "#3b82f6", "txt": "#64748b",
        "xls": "#22c55e", "xlsx": "#22c55e", "csv": "#22c55e",
        "ppt": "#f97316", "pptx": "#f97316",
        "zip": "#eab308", "rar": "#eab308", "7z": "#eab308",
        "dcm": "#a855f7", "nii": "#a855f7", "gz": "#a855f7",
    }
    return (ext.upper()[:4] or "FILE"), colours.get(ext, "#0ea5e9")

def rounded_cover(pix, w, h, radius):
    scaled = pix.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = (scaled.width() - w) // 2
    y = (scaled.height() - h) // 2
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    p.setClipPath(path)
    p.drawPixmap(-x, -y, scaled)
    p.end()
    return out

def pixmap_from_attachment(value, cache):
    pix = QPixmap()
    if is_remote(value):
        data = cache.get(value)
        if data is None:
            return None
        pix.loadFromData(data)
    elif value:
        from PySide6.QtCore import QByteArray
        pix.loadFromData(QByteArray.fromBase64(value.encode("utf-8")))
    return pix if not pix.isNull() else None

class AttachmentCache(QObject):
    loaded = Signal(str)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._data = {}
        self._pending = set()
        self._lock = threading.Lock()

    def get(self, url):
        return self._data.get(url)

    def request(self, url):
        with self._lock:
            if url in self._data or url in self._pending:
                return
            self._pending.add(url)
        threading.Thread(target=self._work, args=(url,), daemon=True).start()

    def _work(self, url):
        try:
            self._data[url] = fetch_bytes(url)
            self.loaded.emit(url)
        except Exception:
            self.failed.emit(url)
        finally:
            with self._lock:
                self._pending.discard(url)

_cache = None

def get_cache():
    global _cache
    if _cache is None:
        _cache = AttachmentCache()
    return _cache

class ClickableFrame(QFrame):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

class ClickableLabel(QLabel):
    clicked = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

class HighlightableImageLabel(QLabel):
    selection_made = Signal(QRect)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlight_enabled = False
        self._start_pos = None
        self._current_pos = None
        self.highlights = []

    def set_highlight_mode(self, enabled):
        self.highlight_enabled = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.update()

    def clear_highlights(self):
        self.highlights.clear()
        self._start_pos = None
        self._current_pos = None
        self.update()

    def mousePressEvent(self, e: QMouseEvent):
        if self.highlight_enabled and e.button() == Qt.LeftButton:
            self._start_pos = e.position().toPoint()
            self._current_pos = self._start_pos
            self.update()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self.highlight_enabled and self._start_pos is not None:
            self._current_pos = e.position().toPoint()
            self.update()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent):
        if self.highlight_enabled and e.button() == Qt.LeftButton and self._start_pos is not None:
            rect = QRect(self._start_pos, e.position().toPoint()).normalized()
            if rect.width() > 8 and rect.height() > 8:
                self.highlights.append(rect)
                self.selection_made.emit(rect)
            self._start_pos = None
            self._current_pos = None
            self.update()
        else:
            super().mouseReleaseEvent(e)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.highlights and not (self.highlight_enabled and self._start_pos):
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(QColor("#facc15"), 2, Qt.DashLine)
        brush = QBrush(QColor(250, 204, 21, 55))
        painter.setPen(pen)
        painter.setBrush(brush)
        for h_rect in self.highlights:
            painter.drawRoundedRect(h_rect, 4, 4)

        if self.highlight_enabled and self._start_pos and self._current_pos:
            live_rect = QRect(self._start_pos, self._current_pos).normalized()
            live_pen = QPen(QColor("#38bdf8"), 2, Qt.SolidLine)
            live_brush = QBrush(QColor(56, 189, 248, 60))
            painter.setPen(live_pen)
            painter.setBrush(live_brush)
            painter.drawRect(live_rect)

class ImagePreviewDialog(QDialog):
    def __init__(self, pixmap, name, chat_panel, url, parent=None):
        super().__init__(parent)
        self.setWindowTitle(name or "Image Viewer")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("""
            QDialog { background-color: #0b0f17; border: 1px solid #1e293b; border-radius: 10px; }
            QPushButton { background-color: #0ea5e9; color: #fff; font-weight: bold; border: none; border-radius: 8px; padding: 8px 18px; }
            QPushButton:hover { background-color: #0284c7; }
            QPushButton#secondary { background-color: #1e293b; color: #cbd5e1; }
            QPushButton#secondary:hover { background-color: #334155; }
            QPushButton#activeBtn { background-color: #f59e0b; color: #0f172a; }
        """)
        self._drag_position = QPoint()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        screen = QApplication.primaryScreen().availableGeometry()
        shown = pixmap.scaled(int(screen.width() * 0.7), int(screen.height() * 0.7), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        
        self.lbl = HighlightableImageLabel()
        self.lbl.setPixmap(shown)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet("border: 1px solid #1e293b; background: transparent; border-radius: 6px;")
        lay.addWidget(self.lbl)

        row = QHBoxLayout()
        self.btn_highlight = QPushButton("Highlight Selection")
        self.btn_highlight.setObjectName("secondary")
        self.btn_highlight.setCursor(Qt.PointingHandCursor)
        self.btn_highlight.clicked.connect(self._toggle_highlight)

        self.btn_clear_hl = QPushButton("Clear Highlights")
        self.btn_clear_hl.setObjectName("secondary")
        self.btn_clear_hl.setCursor(Qt.PointingHandCursor)
        self.btn_clear_hl.clicked.connect(self.lbl.clear_highlights)

        btn_save = QPushButton("Save Image")
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.clicked.connect(lambda: chat_panel.download_attachment(url, name))

        btn_close = QPushButton("Close")
        btn_close.setObjectName("secondary")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)

        row.addWidget(self.btn_highlight)
        row.addWidget(self.btn_clear_hl)
        row.addStretch()
        row.addWidget(btn_save)
        row.addWidget(btn_close)
        lay.addLayout(row)

    def _toggle_highlight(self):
        new_state = not self.lbl.highlight_enabled
        self.lbl.set_highlight_mode(new_state)
        if new_state:
            self.btn_highlight.setText("Exit Highlight Mode")
            self.btn_highlight.setObjectName("activeBtn")
        else:
            self.btn_highlight.setText("Highlight Selection")
            self.btn_highlight.setObjectName("secondary")
        self.btn_highlight.style().unpolish(self.btn_highlight)
        self.btn_highlight.style().polish(self.btn_highlight)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and not self.lbl.geometry().contains(event.position().toPoint()):
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() == Qt.LeftButton and not self.lbl.geometry().contains(event.position().toPoint()):
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

class StackedImageDeckWidget(QFrame):
    """Renders a package/deck of images stacked on top of each other with navigation and a counter."""
    def __init__(self, images, chat_panel, parent=None):
        super().__init__(parent)
        self.images = images  # List of tuples: [(url_or_path, name_or_caption), ...]
        self.chat_panel = chat_panel
        self.current_idx = 0
        self.IMG_W, self.IMG_H = 210, 210
        self.cached_pixmaps = {}

        self.setFixedSize(self.IMG_W + 16, self.IMG_H + 42)
        self.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.setSpacing(4)

        self.img_lbl = ClickableLabel("Loading…")
        self.img_lbl.setFixedSize(self.IMG_W, self.IMG_H)
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.img_lbl.setCursor(Qt.PointingHandCursor)
        self.img_lbl.setToolTip("Click to view full size")
        self.img_lbl.setStyleSheet(
            "background-color: #1e293b; color: #94a3b8; font-size: 11px; "
            "border: 1px solid #334155; border-radius: 8px;"
        )
        self.img_lbl.clicked.connect(self._open_current_preview)
        layout.addWidget(self.img_lbl)

        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(2, 0, 2, 0)
        ctrl_row.setSpacing(6)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedSize(22, 22)
        self.btn_prev.setCursor(Qt.PointingHandCursor)
        self.btn_prev.setStyleSheet(
            "background: #1e293b; color: #94a3b8; border: 1px solid #334155; "
            "border-radius: 11px; font-size: 9px; padding: 0;"
        )
        self.btn_prev.clicked.connect(self._prev)

        self.lbl_counter = QLabel(f"1 of {len(self.images)}")
        self.lbl_counter.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: bold; background: transparent;")
        self.lbl_counter.setAlignment(Qt.AlignCenter)

        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedSize(22, 22)
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_next.setStyleSheet(
            "background: #1e293b; color: #94a3b8; border: 1px solid #334155; "
            "border-radius: 11px; font-size: 9px; padding: 0;"
        )
        self.btn_next.clicked.connect(self._next)

        ctrl_row.addWidget(self.btn_prev)
        ctrl_row.addWidget(self.lbl_counter, 1)
        ctrl_row.addWidget(self.btn_next)
        layout.addLayout(ctrl_row)

        self._load_current()

    def paintEvent(self, e):
        super().paintEvent(e)
        if len(self.images) > 1:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)

            # Second stacked layer
            p.setBrush(QColor("#0f172a"))
            p.setPen(QColor("#334155"))
            p.drawRoundedRect(10, 10, self.IMG_W, self.IMG_H, 8, 8)

            # Third stacked layer
            if len(self.images) > 2:
                p.setBrush(QColor("#1e293b"))
                p.setPen(QColor("#475569"))
                p.drawRoundedRect(14, 14, self.IMG_W, self.IMG_H, 8, 8)
            p.end()

    def _prev(self):
        if self.images:
            self.current_idx = (self.current_idx - 1) % len(self.images)
            self._load_current()

    def _next(self):
        if self.images:
            self.current_idx = (self.current_idx + 1) % len(self.images)
            self._load_current()

    def _load_current(self):
        url, name = self.images[self.current_idx]
        self.lbl_counter.setText(f"{self.current_idx + 1} of {len(self.images)}")
        cache = get_cache()
        pix = self.cached_pixmaps.get(url) or pixmap_from_attachment(url, cache)
        if pix is not None:
            self.cached_pixmaps[url] = pix
            self.img_lbl.setText("")
            self.img_lbl.setPixmap(rounded_cover(pix, self.IMG_W, self.IMG_H, 8))
        elif is_remote(url):
            self.img_lbl.setText("Loading…")
            cache.loaded.connect(self._on_loaded)
            cache.request(url)
        else:
            self.img_lbl.setText("Image error")

    def _on_loaded(self, loaded_url):
        url, name = self.images[self.current_idx]
        if loaded_url == url:
            pix = pixmap_from_attachment(url, get_cache())
            if pix is not None:
                self.cached_pixmaps[url] = pix
                self.img_lbl.setText("")
                self.img_lbl.setPixmap(rounded_cover(pix, self.IMG_W, self.IMG_H, 8))

    def _open_current_preview(self):
        url, name = self.images[self.current_idx]
        pix = self.cached_pixmaps.get(url) or pixmap_from_attachment(url, get_cache())
        if pix:
            dlg = ImagePreviewDialog(pix, name, self.chat_panel, url, self.window())
            dlg.exec()


class ChatInfoSidebar(QFrame):
    """Facebook-style chat details panel with Images / Files / Members tabs."""
    closed = Signal()
    COLS = 3
    THUMB = 84
    TABS = ("Images", "Files", "Members")

    def __init__(self, chat_panel, parent=None):
        super().__init__(parent)
        self.setObjectName("mediaSidebar")
        self.chat_panel = chat_panel
        self._thumbs = {}      # image source -> [QLabel, ...]
        self.setFixedWidth(300)
        self.setStyleSheet(
            "QFrame#mediaSidebar{background:#161b22;border:1px solid #1e293b;border-radius:10px}"
            "QLabel{background:transparent;border:none}"
            "QScrollArea{background:transparent;border:none}"
            "QScrollArea>QWidget>QWidget{background:transparent}"
            "QScrollBar:vertical{width:6px;background:transparent}"
            "QScrollBar::handle:vertical{background:#334155;border-radius:3px;min-height:24px}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
            "QPushButton#tabBtn{background:#1e293b;color:#94a3b8;border:none;border-radius:14px;"
            "padding:0 4px;font-size:12px;font-weight:bold}"
            "QPushButton#tabBtn:hover{background:#334155;color:#ffffff}"
            "QPushButton#tabBtn:checked{background:#0ea5e9;color:#ffffff}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Chat details")
        title.setStyleSheet("color:#f8fafc;font-size:15px;font-weight:bold")
        btn_close = QPushButton("\u2715")
        btn_close.setFixedSize(26, 26)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(
            "QPushButton{background:transparent;color:#94a3b8;border:none;font-size:13px;padding:0}"
            "QPushButton:hover{background:#1e293b;color:#ffffff;border-radius:13px}")
        btn_close.clicked.connect(self.closed.emit)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(btn_close)
        root.addLayout(header)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.stack = QStackedWidget()
        self._layouts = []
        for index, label in enumerate(self.TABS):
            button = QPushButton(label)
            button.setObjectName("tabBtn")
            button.setCheckable(True)
            button.setFixedHeight(28)
            button.setCursor(Qt.PointingHandCursor)
            self.tab_group.addButton(button, index)
            tabs.addWidget(button, 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(0, 4, 0, 4)
            body_layout.setSpacing(14)
            body_layout.addStretch(1)
            scroll.setWidget(body)
            self.stack.addWidget(scroll)
            self._layouts.append(body_layout)
        self.tab_group.button(0).setChecked(True)
        self.tab_group.idClicked.connect(self.stack.setCurrentIndex)
        root.addLayout(tabs)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#1e293b;border:none")
        root.addWidget(line)
        root.addWidget(self.stack, 1)

        self.cache = get_cache()
        self.cache.loaded.connect(self._on_loaded)

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _when(value):
        """Normalise a Firestore timestamp to a naive local datetime (None -> now)."""
        if hasattr(value, "strftime") and hasattr(value, "year"):
            if getattr(value, "tzinfo", None) is not None:
                return value.astimezone().replace(tzinfo=None)
            return value
        return datetime.now()   # server timestamp not resolved yet -> "just sent"

    def _month_groups(self, records):
        groups = {}
        for source, name, sent_at in records:
            when = self._when(sent_at)
            groups.setdefault((when.year, when.month), []).append((when, source, name))
        for key in sorted(groups, reverse=True):
            yield key, sorted(groups[key], key=lambda r: r[0], reverse=True)

    def _reset(self, index):
        layout = self._layouts[index]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        return layout

    @staticmethod
    def _heading(text, size=13, colour="#e2e8f0"):
        label = QLabel(text)
        label.setStyleSheet(f"color:{colour};font-size:{size}px;font-weight:bold")
        return label

    @staticmethod
    def _empty(text):
        empty = QLabel(text)
        empty.setWordWrap(True)
        empty.setAlignment(Qt.AlignCenter)
        empty.setStyleSheet("color:#64748b;font-size:12px;padding:24px 8px")
        return empty

    def _update_tab_counts(self, index, count):
        self.tab_group.button(index).setText(f"{self.TABS[index]} ({count})" if count else self.TABS[index])

    # ---- Images ----------------------------------------------------------
    def set_images(self, records):
        """records: [(source, name, sent_at), ...]"""
        self._thumbs.clear()
        layout = self._reset(0)
        self._update_tab_counts(0, len(records))
        if not records:
            layout.addWidget(self._empty("No photos have been shared in this chat yet."))
            layout.addStretch(1)
            return
        for (year, month), items in self._month_groups(records):
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(6)
            section_layout.addWidget(self._heading(f"{datetime(year, month, 1):%B %Y}"))
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)
            for index, (when, source, name) in enumerate(items):
                thumb = ClickableLabel()
                thumb.setFixedSize(self.THUMB, self.THUMB)
                thumb.setAlignment(Qt.AlignCenter)
                thumb.setToolTip(f"{name}\n{when:%b %d, %Y \u00b7 %I:%M %p}")
                thumb.setStyleSheet("background:#1e293b;border-radius:6px;color:#64748b;font-size:10px")
                pix = pixmap_from_attachment(source, self.cache)
                if pix is not None:
                    thumb.setPixmap(rounded_cover(pix, self.THUMB, self.THUMB, 6))
                else:
                    thumb.setText("\u2026")
                    if is_remote(source):
                        self.cache.request(source)
                self._thumbs.setdefault(source, []).append(thumb)
                thumb.clicked.connect(lambda s=source, n=name: self._open_image(s, n))
                grid.addWidget(thumb, index // self.COLS, index % self.COLS)
            grid.setColumnStretch(self.COLS, 1)
            section_layout.addWidget(grid_host)
            layout.addWidget(section)
        layout.addStretch(1)

    def _on_loaded(self, loaded_url):
        for thumb in self._thumbs.get(loaded_url, []):
            pix = pixmap_from_attachment(loaded_url, self.cache)
            if pix is not None:
                try:
                    thumb.setText("")
                    thumb.setPixmap(rounded_cover(pix, self.THUMB, self.THUMB, 6))
                except RuntimeError:
                    pass   # thumbnail was rebuilt while the download was in flight

    def _open_image(self, source, name):
        pix = pixmap_from_attachment(source, self.cache)
        if pix:
            ImagePreviewDialog(pix, name, self.chat_panel, source, self.chat_panel).exec()

    # ---- Files -----------------------------------------------------------
    def set_files(self, records):
        """records: [(url, name, sent_at), ...]"""
        layout = self._reset(1)
        self._update_tab_counts(1, len(records))
        if not records:
            layout.addWidget(self._empty("No files have been shared in this chat yet."))
            layout.addStretch(1)
            return
        metrics = QFontMetrics(QFont("Segoe UI", 9))
        for (year, month), items in self._month_groups(records):
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(6)
            section_layout.addWidget(self._heading(f"{datetime(year, month, 1):%B %Y}"))
            for when, url, name in items:
                row = ClickableFrame()
                row.setObjectName("fileRow")
                row.setStyleSheet(
                    "QFrame#fileRow{background:#1e293b;border:1px solid #334155;border-radius:8px}"
                    "QFrame#fileRow:hover{background:#334155}")
                row.setToolTip(f"{name}\nClick to download")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(8, 8, 10, 8)
                row_layout.setSpacing(10)
                badge, colour = file_badge(name)
                tile = QLabel(badge)
                tile.setFixedSize(36, 36)
                tile.setAlignment(Qt.AlignCenter)
                tile.setStyleSheet(f"background:{colour};color:#ffffff;border-radius:8px;font-weight:bold;font-size:10px")
                text_col = QVBoxLayout()
                text_col.setSpacing(1)
                name_lbl = QLabel(metrics.elidedText(name, Qt.ElideMiddle, 160))
                name_lbl.setStyleSheet("color:#e2e8f0;font-size:12px;font-weight:bold")
                date_lbl = QLabel(f"{when:%b %d, %Y \u00b7 %I:%M %p}")
                date_lbl.setStyleSheet("color:#94a3b8;font-size:10px")
                for label in (name_lbl, date_lbl):
                    label.setAttribute(Qt.WA_TransparentForMouseEvents)
                text_col.addWidget(name_lbl)
                text_col.addWidget(date_lbl)
                row_layout.addWidget(tile)
                row_layout.addLayout(text_col, 1)
                row.clicked.connect(lambda u=url, n=name: self.chat_panel.download_attachment(u, n))
                section_layout.addWidget(row)
            layout.addWidget(section)
        layout.addStretch(1)

    # ---- Members ---------------------------------------------------------
    def set_members(self, members):
        """members: [(user_id, display_name, designation, is_online, is_self), ...]"""
        layout = self._reset(2)
        self._update_tab_counts(2, len(members))
        if not members:
            layout.addWidget(self._empty("No members to show yet."))
            layout.addStretch(1)
            return
        online = sum(1 for m in members if m[3])
        layout.addWidget(self._heading(f"{len(members)} member{'s' if len(members) != 1 else ''} \u00b7 {online} online",
                                       size=12, colour="#94a3b8"))
        for uid, name, designation, is_online, is_self in members:
            row = ClickableFrame()
            row.setObjectName("memberRow")
            row.setStyleSheet(
                "QFrame#memberRow{background:#1e293b;border:1px solid #334155;border-radius:8px}"
                "QFrame#memberRow:hover{background:#334155}")
            if is_self:
                row.setCursor(Qt.ArrowCursor)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(10)
            dot = QLabel("\u25cf" if is_online else "\u25cb")
            dot.setStyleSheet(f"color:{'#10b981' if is_online else '#64748b'};font-size:13px")
            text_col = QVBoxLayout()
            text_col.setSpacing(1)
            name_lbl = QLabel(name + (" (You)" if is_self else ""))
            name_lbl.setStyleSheet("color:#e2e8f0;font-size:12px;font-weight:bold")
            role_lbl = QLabel(designation or "Medical Staff")
            role_lbl.setStyleSheet("color:#94a3b8;font-size:10px")
            for label in (dot, name_lbl, role_lbl):
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
            text_col.addWidget(name_lbl)
            text_col.addWidget(role_lbl)
            row_layout.addWidget(dot)
            row_layout.addLayout(text_col, 1)
            if not is_self:
                row.setToolTip(f"Message {name}")
                row.clicked.connect(lambda u=uid, n=name: self.chat_panel.switch_chat(u, n))
            layout.addWidget(row)
        layout.addStretch(1)


class UploadWorker(QThread):
    uploaded = Signal(str, str, str, str)
    failed = Signal(str, str)

    def __init__(self, path, kind, target, parent=None):
        super().__init__(parent)
        self.path, self.kind, self.target = path, kind, target

    def run(self):
        try:
            folder = "general" if self.target == "general" else "direct"
            info = upload_attachment(self.path, folder)
            self.uploaded.emit(self.kind, info["name"], info["url"], self.target)
        except Exception as e:
            self.failed.emit(os.path.basename(self.path), str(e))

class ChatSignalBridge(QObject):
    messages_updated = Signal(list)
    users_updated = Signal(list)
    download_finished = Signal(str)
    download_failed = Signal(str)

class OnlineUserWidget(QWidget):
    def __init__(self, display_name, designation, is_online, is_self):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(52)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        status_sym = "●" if is_online else "○"
        status_col = "#10b981" if is_online else "#64748b"

        name_text = f"{status_sym} {display_name}" + (" (You)" if is_self else "")
        name_lbl = QLabel(name_text)
        name_lbl.setStyleSheet(f"font-weight: bold; font-size: 13px; color: {status_col}; background: transparent;")
        name_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        desig_lbl = QLabel(designation if designation else "Medical Staff")
        desig_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; background: transparent;")
        desig_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        info_layout.addWidget(name_lbl)
        info_layout.addWidget(desig_lbl)
        layout.addLayout(info_layout, 1)

        if not is_self:
            dm_icon = QLabel()
            dm_icon.setPixmap(QPixmap(DM_PATH).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.dm_icon = dm_icon
            dm_icon.setAttribute(Qt.WA_TransparentForMouseEvents)
            dm_icon.setStyleSheet("background: transparent;")
            layout.addWidget(dm_icon, alignment=Qt.AlignRight | Qt.AlignVCenter)
        else:
            self.dm_icon = None

    def apply_theme(self, light):
        if self.dm_icon:
            color = "#475569" if light else "#64748b"
            self.dm_icon.setPixmap(QPixmap(recolor_svg(DM_PATH, "dm_theme", color)).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))

class DateSeparatorWidget(QWidget):
    def __init__(self, date_str):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 15, 20, 15)

        line1 = QFrame()
        line1.setFrameShape(QFrame.HLine)
        line1.setStyleSheet("background-color: #334155; max-height: 1px; border: none;")

        lbl = QLabel(date_str)
        lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold; background: transparent; padding: 0 10px;")
        lbl.setAlignment(Qt.AlignCenter)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("background-color: #334155; max-height: 1px; border: none;")

        layout.addWidget(line1, stretch=1)
        layout.addWidget(lbl)
        layout.addWidget(line2, stretch=1)

class MessageItemWidget(QWidget):
    def __init__(self, msg_id, display_name, designation, text, timestamp_str, msg_type="text",
                 file_path="", liked_by=None, is_self=False, current_user_id="", chat_panel=None,
                 reply_to=None, images_deck=None):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self.msg_id = msg_id
        self.chat_panel = chat_panel
        self.msg_type = msg_type
        self.display_name = display_name
        self.designation = designation or ""
        self.raw_text = text
        self.file_path = file_path
        self.images_deck = images_deck or []
        self.is_self = is_self
        self.reply_to = reply_to or {}
        self.msg_lbl = None

        kind_word = {"image": "image photo attachment", "file": "file attachment", "images": "photos package"}.get(msg_type, "")
        self.search_blob = " ".join([text or "", display_name or "", self.designation, kind_word]).lower()

        likers = set(liked_by or [])
        self.has_liked = current_user_id in likers
        self.reaction_count = len(likers)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(10, 3, 10, 3)
        main_layout.setSpacing(8)

        self.bubble = QFrame()
        self.bubble.setObjectName("bubble")
        self.bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.bubble.setMaximumWidth(540)
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setSpacing(4)

        if msg_type in ["image", "images", "file"]:
            self.bubble.setStyleSheet("QFrame#bubble { background: transparent; border: none; }")
            bubble_layout.setContentsMargins(0, 0, 0, 0)
            name_color = "#38bdf8"
            text_color = "#e2e8f0"
            desig_color = "#64748b"
            time_color = "#64748b"
        elif is_self:
            self.bubble.setStyleSheet("QFrame#bubble { background-color: #0284c7; border-radius: 12px; border-bottom-right-radius: 3px; border: none; }")
            bubble_layout.setContentsMargins(14, 10, 14, 10)
            name_color = "#bae6fd"
            text_color = "#ffffff"
            desig_color = "#7dd3fc"
            time_color = "#bae6fd"
        else:
            self.bubble.setStyleSheet("QFrame#bubble { background-color: #1e293b; border-radius: 12px; border-bottom-left-radius: 3px; border: 1px solid #334155; }")
            bubble_layout.setContentsMargins(14, 10, 14, 10)
            name_color = "#38bdf8"
            text_color = "#e2e8f0"
            desig_color = "#64748b"
            time_color = "#64748b"
            
        self._text_color, self._time_color = text_color, time_color

        # Reply Quote Banner
        if self.reply_to and self.reply_to.get("id"):
            reply_quote = ClickableFrame()
            reply_quote.setStyleSheet("""
                QFrame { background-color: rgba(0, 0, 0, 0.25); border-left: 3px solid #38bdf8; border-radius: 4px; padding: 4px 8px; }
                QFrame:hover { background-color: rgba(0, 0, 0, 0.4); }
            """)
            quote_lay = QVBoxLayout(reply_quote)
            quote_lay.setContentsMargins(4, 2, 4, 2)
            quote_lay.setSpacing(2)
            lbl_rep_name = QLabel(f"Replying to {self.reply_to.get('sender', 'User')}")
            lbl_rep_name.setStyleSheet("color: #38bdf8; font-size: 10px; font-weight: bold; border: none; background: transparent;")
            lbl_rep_txt = QLabel(self.reply_to.get("text", "Original message"))
            lbl_rep_txt.setStyleSheet("color: #cbd5e1; font-size: 11px; border: none; background: transparent;")
            quote_lay.addWidget(lbl_rep_name)
            quote_lay.addWidget(lbl_rep_txt)
            target_id = self.reply_to.get("id")
            reply_quote.clicked.connect(lambda: self.chat_panel.scroll_to_message_id(target_id) if self.chat_panel else None)
            bubble_layout.addWidget(reply_quote)

        header_layout = QHBoxLayout()
        name_lbl = QLabel(display_name)
        name_lbl.setStyleSheet(f"font-weight: bold; font-size: 13px; color: {name_color}; background: transparent; border: none;")
        desig_lbl = QLabel(f"• {designation}" if designation else "• Medical Staff")
        desig_lbl.setStyleSheet(f"color: {desig_color}; font-size: 10px; font-weight: bold; background: transparent; border: none;")
        header_layout.addWidget(name_lbl)
        header_layout.addWidget(desig_lbl)
        header_layout.addStretch()
        bubble_layout.addLayout(header_layout)

        if msg_type == "images" or len(self.images_deck) > 1:
            deck = StackedImageDeckWidget(self.images_deck, self.chat_panel)
            bubble_layout.addWidget(deck, alignment=Qt.AlignRight if is_self else Qt.AlignLeft)
        elif msg_type == "image":
            deck = StackedImageDeckWidget([(self.file_path, self.raw_text)], self.chat_panel)
            bubble_layout.addWidget(deck, alignment=Qt.AlignRight if is_self else Qt.AlignLeft)
        elif msg_type == "file":
            bubble_layout.addWidget(self._build_file_card(), alignment=Qt.AlignRight if is_self else Qt.AlignLeft)
        else:
            self.msg_lbl = QLabel(text)
            self.msg_lbl.setStyleSheet(f"color: {text_color}; font-size: 13px; background: transparent; border: none;")
            self.msg_lbl.setWordWrap(True)
            self.msg_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bubble_layout.addWidget(self.msg_lbl)

        footer_layout = QHBoxLayout()
        status_lbl = QLabel(f"{timestamp_str} {' ✓' if is_self else ''}")
        status_lbl.setStyleSheet(f"color: {time_color}; font-size: 10px; background: transparent; border: none;")

        self.btn_react = QPushButton()
        self.btn_react.setCursor(Qt.PointingHandCursor)
        self.btn_react.setToolTip("Like")
        self.btn_react.clicked.connect(self.trigger_reaction)
        self._style_like_button()

        btn_reply = QPushButton("Reply")
        btn_reply.setIcon(QIcon(REPLY_PATH))
        self.btn_reply = btn_reply
        btn_reply.setCursor(Qt.PointingHandCursor)
        btn_reply.setToolTip("Reply to this message")
        btn_reply.clicked.connect(self.trigger_reply)
        btn_reply.setStyleSheet("""
            QPushButton { background: transparent; color: #94a3b8; border: none; font-size: 10px; padding: 0 4px; }
            QPushButton:hover { color: #38bdf8; }
        """)

        if is_self:
            btn_unsend = QPushButton("Unsend")
            btn_unsend.setIcon(QIcon(UNSEND_PATH))
            self.btn_unsend = btn_unsend
            btn_unsend.setCursor(Qt.PointingHandCursor)
            btn_unsend.setToolTip("Unsend this message")
            btn_unsend.setStyleSheet("""
                QPushButton { background: transparent; color: #bae6fd; border: none; font-size: 10px; padding: 0 4px; }
                QPushButton:hover { color: #f87171; }
            """)
            btn_unsend.clicked.connect(self.trigger_unsend)

            footer_layout.addStretch()
            footer_layout.addWidget(btn_reply)
            footer_layout.addWidget(btn_unsend)
            footer_layout.addWidget(self.btn_react)
        else:
            self.btn_unsend = None
            footer_layout.addWidget(status_lbl)
            footer_layout.addWidget(self.btn_react)
            footer_layout.addWidget(btn_reply)
            footer_layout.addStretch()

        bubble_layout.addLayout(footer_layout)

        if is_self:
            main_layout.addStretch()
            main_layout.addWidget(self.bubble)
        else:
            main_layout.addWidget(self.bubble)
            main_layout.addStretch()

    def _build_file_card(self):
        downloadable = is_remote(self.file_path)
        card = ClickableFrame()
        card.setObjectName("fileCard")
        card.setStyleSheet("""
            QFrame#fileCard { background-color: #1e293b; border: 1px solid #334155; border-radius: 10px; }
            QFrame#fileCard:hover { background-color: #334155; }
        """)
        card.setMinimumWidth(260)
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 10, 12, 10)
        row.setSpacing(12)

        label, colour = file_badge(self.raw_text)
        tile = QLabel(label)
        tile.setFixedSize(44, 44)
        tile.setAlignment(Qt.AlignCenter)
        tile.setStyleSheet(f"background-color: {colour}; color: #ffffff; border-radius: 10px; font-weight: bold; font-size: 11px;")
        row.addWidget(tile)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_lbl = QLabel()
        name_lbl.setStyleSheet("color: #e2e8f0; font-size: 13px; font-weight: bold; background: transparent; border: none;")
        
        fm = QFontMetrics(QFont("Segoe UI", 10))
        name_lbl.setText(fm.elidedText(self.raw_text, Qt.ElideMiddle, 180))
        name_lbl.setToolTip(self.raw_text)
        
        sub_lbl = QLabel(f"{label} file · click to download" if downloadable else "Local attachment")
        sub_lbl.setStyleSheet("color: #94a3b8; font-size: 10px; background: transparent; border: none;")
        text_col.addWidget(name_lbl)
        text_col.addWidget(sub_lbl)
        row.addLayout(text_col, 1)

        if downloadable:
            dl = QLabel()
            dl.setPixmap(QPixmap(DOWNLOAD_PATH).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            dl.setFixedSize(32, 32)
            dl.setAlignment(Qt.AlignCenter)
            dl.setStyleSheet("background-color: #0ea5e9; border-radius: 16px; border: none;")
            row.addWidget(dl)
            card.setCursor(Qt.PointingHandCursor)
            card.clicked.connect(lambda: self.chat_panel.download_attachment(self.file_path, self.raw_text) if self.chat_panel else None)
        return card

    def _style_like_button(self):
        is_blue = (self.is_self and self.msg_type == "text")
        if self.has_liked:
            icon = LIKED_WHITE if is_blue else LIKED_BLUE
            colour = "#ffffff" if is_blue else "#0ea5e9"
            hover = colour
        else:
            icon = LIKE_LIGHT if is_blue else LIKE_GRAY
            colour = "#bae6fd" if is_blue else "#64748b"
            hover = "#ffffff" if is_blue else "#f8fafc"
            
        self.btn_react.setIcon(QIcon(icon))
        self.btn_react.setText(f" {self.reaction_count}" if self.reaction_count > 0 else "")
        self.btn_react.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {colour}; border: none; font-size: 11px; font-weight: bold; padding: 2px 4px; }}"
            f"QPushButton:hover {{ color: {hover}; }}")

    def trigger_reply(self):
        if self.chat_panel:
            preview = self.raw_text if self.msg_type == "text" else f"[{self.msg_type.capitalize()}]"
            self.chat_panel.set_reply_target(self.msg_id, self.display_name, preview)

    def apply_theme(self, light):
        icon_color = "#475569" if light else "#94a3b8"
        self.btn_reply.setIcon(QIcon(recolor_svg(REPLY_PATH, "reply_theme", icon_color)))
        if self.btn_unsend:
            self.btn_unsend.setIcon(QIcon(recolor_svg(UNSEND_PATH, "unsend_theme", icon_color)))

    def trigger_reaction(self):
        if not (self.chat_panel and self.msg_id):
            return
        desired = not self.has_liked
        if self.chat_panel.request_like(self.msg_id, desired):
            self.has_liked = desired
            self.reaction_count = max(0, self.reaction_count + (1 if desired else -1))
            self._style_like_button()

    def trigger_unsend(self):
        if not (self.chat_panel and self.msg_id):
            return
        reply = QMessageBox.question(
            self, "Unsend Message", "Are you sure you want to unsend this message for everyone?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.chat_panel.unsend_message(self.msg_id)

    def flash_highlight(self):
        orig_style = self.bubble.styleSheet()
        self.bubble.setStyleSheet("QFrame#bubble { background-color: #854d0e; border: 2px solid #facc15; border-radius: 12px; }")
        QTimer.singleShot(1000, lambda: self.bubble.setStyleSheet(orig_style))

    def apply_highlight(self, term):
        if self.msg_lbl is None:
            return
        if not term:
            if self.msg_lbl.textFormat() != Qt.PlainText:
                self.msg_lbl.setTextFormat(Qt.PlainText)
                self.msg_lbl.setText(self.raw_text)
            return
        escaped = html.escape(self.raw_text)
        pattern = re.compile(re.escape(html.escape(term)), re.IGNORECASE)
        marked = pattern.sub(lambda m: f'<span style="background-color:#facc15; color:#0f172a;">{m.group(0)}</span>', escaped)
        self.msg_lbl.setTextFormat(Qt.RichText)
        self.msg_lbl.setText(marked.replace("\n", "<br>"))

class CollabChatPanel(QWidget):
    network_status_changed = Signal(bool)
    new_message_received = Signal(str, str)

    def __init__(self, active_user_id, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Chat Room")
        self.setObjectName("collabRoot")
        self._drag_position = QPoint()

        screen_geometry = QApplication.primaryScreen().geometry()
        width_80 = int(screen_geometry.width() * 0.8)
        height_80 = int(screen_geometry.height() * 0.8)
        self.resize(width_80, height_80)

        self.is_online = False
        self._network_worker = None
        self.user_id = active_user_id if active_user_id else "admin"
        self.display_name = self.user_id
        self.designation = "Attending Physician"

        self.current_target = "general"
        self.current_target_name = "General Chat"
        self.chat_listener = None
        self.user_listener = None

        self._active_reply = None
        self._staged_image_path = None
        self._msg_widget_map = {}

        self._optimistic_likes = {}      
        self._last_messages = []
        self._last_msg_count = -1
        self._known_message_ids = set()
        self._notification_baseline_ready = False
        self._upload_queue = deque()
        self._upload_worker = None
        self._matches = []
        self._match_idx = -1

        self.db = initialize_firestore()
        self.load_profile_from_cloud()

        self.bridge = ChatSignalBridge()
        self.bridge.messages_updated.connect(self.render_messages)
        self.bridge.users_updated.connect(self.render_online_users)
        self.bridge.download_finished.connect(lambda p: self.show_status(f"Saved to {p}", 6000))
        self.bridge.download_failed.connect(lambda e: QMessageBox.warning(self, "Download failed", e))

        shortcut_close = QShortcut(QKeySequence("Ctrl+M"), self)
        shortcut_close.activated.connect(self.hide)
        shortcut_max = QShortcut(QKeySequence("F11"), self)
        shortcut_max.activated.connect(self.toggle_maximized)
        shortcut_find = QShortcut(QKeySequence("Ctrl+F"), self)
        shortcut_find.activated.connect(self.focus_search)

        self.setStyleSheet("""
            QWidget#collabRoot { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #111a29, stop:0.35 #0b0f17, stop:1 #101825); font-family: 'Segoe UI', Arial, sans-serif; border: none; border-radius: 10px; }
            QFrame#chatTitleBar { background: transparent; border: none; }
            QFrame#sidebar { background-color: #161b22; border: 1px solid #1e293b; border-radius: 10px; }
            QFrame#chatArea { background-color: transparent; border: none; border-radius: 10px; }

            QListWidget { background-color: transparent; border: none; outline: none; padding: 4px; }
            QListWidget QScrollBar:vertical, QListWidget QScrollBar:horizontal { width: 0px; height: 0px; background: transparent; }
            QListWidget QScrollBar::handle:vertical, QListWidget QScrollBar::handle:horizontal { background: transparent; }

            QListWidget#sidebarList::item { border-radius: 8px; margin-bottom: 6px; padding: 2px; background-color: transparent; }
            QListWidget#sidebarList::item:selected { background-color: #1e293b; }
            QListWidget#sidebarList::item:hover { background-color: #161e2e; }

            QListWidget#chatList::item { margin-bottom: 2px; padding: 0px; background-color: transparent; }
            QListWidget#chatList::item:selected { background-color: transparent; }
            QListWidget#chatList::item:hover { background-color: transparent; }

            QLineEdit { background-color: #0d1117; color: #e2e8f0; border: 1px solid #1e293b; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
            QPushButton { background-color: #0ea5e9; color: #ffffff; font-weight: bold; border-radius: 8px; padding: 10px 16px; font-size: 13px; border: none; }
            QPushButton:hover { background-color: #0284c7; }
            QPushButton:disabled { background-color: #1e293b; color: #64748b; }
            QPushButton#iconBtn { background-color: #1e293b; color: #cbd5e1; padding: 8px 14px; border-radius: 8px; font-size: 12px; font-weight: bold; border: none; }
            QPushButton#iconBtn:hover { background-color: #334155; color: #ffffff; }
            QPushButton#iconBtn:disabled { background-color: #161b22; color: #475569; }
        """)

        main_vbox = QVBoxLayout(self)
        main_vbox.setContentsMargins(0, 0, 0, 0)
        main_vbox.setSpacing(0)

        # Title Bar
        title_bar = QFrame(objectName="chatTitleBar")
        title_bar.setFixedHeight(44)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(14, 0, 14, 0)

        lbl_window_title = QLabel("Chat Room")
        lbl_window_title.setStyleSheet("color: #f8fafc; font-size: 13px; font-weight: bold; border: none; background: transparent;")

        btn_close_window = QPushButton("✕")
        btn_close_window.setFixedSize(28, 28)
        btn_close_window.setCursor(Qt.PointingHandCursor)
        btn_close_window.setStyleSheet("""
            QPushButton { background-color: transparent; color: #94a3b8; border: none; font-size: 14px; font-weight: bold; padding: 0; margin: 0; }
            QPushButton:hover { color: #ef4444; background-color: #1e293b; border-radius: 14px; }
        """)
        btn_close_window.clicked.connect(self.hide)

        title_layout.addWidget(lbl_window_title)
        title_layout.addStretch()
        self.btn_maximize = QPushButton("\u2610")
        self.btn_maximize.setFixedSize(28, 28)
        self.btn_maximize.setCursor(Qt.PointingHandCursor)
        self.btn_maximize.setToolTip("Maximize chat room (F11)")
        self.btn_maximize.setStyleSheet("""
            QPushButton { background-color: transparent; color: #94a3b8; border: none; font-size: 15px; font-weight: bold; padding: 0; margin: 0; }
            QPushButton:hover { color: #38bdf8; background-color: #1e293b; border-radius: 14px; }
        """)
        self.btn_maximize.clicked.connect(self.toggle_maximized)
        self._title_bar = title_bar

        title_layout.addWidget(self.btn_maximize)
        title_layout.addWidget(btn_close_window)
        main_vbox.addWidget(title_bar)

        # Content Area
        content_widget = QWidget()
        main_layout = QHBoxLayout(content_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        # Left Sidebar
        left_panel = QFrame(objectName="sidebar")
        left_panel.setFixedWidth(260)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)

        self.btn_general = QPushButton("General Chat")
        self.btn_general.setCursor(Qt.PointingHandCursor)
        self.btn_general.setStyleSheet("text-align: left; padding: 12px; background-color: #1e293b; color: #ffffff; border-radius: 8px; font-size: 13px; font-weight: bold; border: none;")
        self.btn_general.clicked.connect(lambda: self.switch_chat("general", "General Chat"))
        left_layout.addWidget(self.btn_general)

        lbl_dm = QLabel("Direct Messages")
        lbl_dm.setStyleSheet("color: #64748b; font-weight: bold; font-size: 11px; letter-spacing: 0.5px; border: none; margin-top: 14px; margin-bottom: 6px; text-transform: uppercase; background: transparent;")
        left_layout.addWidget(lbl_dm)

        self.online_users_list = QListWidget(objectName="sidebarList")
        self.online_users_list.setCursor(Qt.PointingHandCursor)
        self.online_users_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.online_users_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.online_users_list.itemClicked.connect(self.on_user_clicked)
        left_layout.addWidget(self.online_users_list, 1)

        main_layout.addWidget(left_panel)

        # Right Panel
        right_panel = QFrame(objectName="chatArea")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 12, 12, 12)

        header_layout = QHBoxLayout()
        self.lbl_chat_header = QLabel("General Chat Room", objectName="header")
        self.lbl_chat_header.setStyleSheet("color: #f8fafc; font-weight: bold; font-size: 15px; border: none; background: transparent;")

        # Compact Search Bar
        self.lbl_search_count = QLabel("")
        self.lbl_search_count.setStyleSheet("color: #94a3b8; font-size: 10px; background: transparent; border: none; padding-right: 6px;")
        self.lbl_search_count.hide()

        search_wrapper = QFrame(objectName="searchBox")
        search_wrapper.setFixedHeight(28)
        search_wrapper.setFixedWidth(155)
        search_wrapper.setStyleSheet("QFrame#searchBox { background-color: #0f172a; border: 1px solid #334155; border-radius: 14px; }")
        search_layout = QHBoxLayout(search_wrapper)
        search_layout.setContentsMargins(8, 0, 6, 0)
        search_layout.setSpacing(4)

        search_icon = QLabel()
        search_icon.setPixmap(QPixmap(SEARCH_PATH).scaled(12, 12, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.search_icon_label = search_icon
        search_icon.setStyleSheet("background: transparent; border: none;")

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search...")
        self.search_input.setFixedWidth(115)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setStyleSheet("QLineEdit { background: transparent; color: #e2e8f0; border: none; font-size: 11px; padding: 0px; margin: 0px; } QLineEdit:focus { border: none; }")
        
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self.perform_search)
        self.search_input.textChanged.connect(lambda _: self._search_timer.start())
        self.search_input.returnPressed.connect(self.goto_next_match)
        
        esc = QShortcut(QKeySequence("Esc"), self.search_input)
        esc.setContext(Qt.WidgetShortcut)
        esc.activated.connect(self.search_input.clear)

        search_layout.addWidget(search_icon)
        search_layout.addWidget(self.search_input)

        header_layout.addWidget(self.lbl_chat_header)
        header_layout.addStretch()
        header_layout.addWidget(self.lbl_search_count)
        self.btn_media = QPushButton()
        self.btn_media.setObjectName("iconBtn")
        self.btn_media.setStyleSheet("padding: 0px;")
        self.btn_media.setIcon(QIcon(MENU_PATH))
        self.btn_media.setIconSize(QSize(16, 16))
        self.btn_media.setCursor(Qt.PointingHandCursor)
        self.btn_media.setFixedSize(34, 28)
        self.btn_media.setToolTip("Menu: images, files and members")
        self.btn_media.clicked.connect(self.toggle_media_sidebar)
        header_layout.addWidget(search_wrapper)
        header_layout.addWidget(self.btn_media)
        right_layout.addLayout(header_layout)

        line_div = QFrame()
        line_div.setFrameShape(QFrame.HLine)
        line_div.setStyleSheet("background-color: #1e293b; max-height: 1px; border: none; margin-top: 5px; margin-bottom: 5px;")
        right_layout.addWidget(line_div)

        self.chat_display = QListWidget(objectName="chatList")
        self.chat_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_display.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_display.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.chat_display.setSelectionMode(QListWidget.NoSelection)
        right_layout.addWidget(self.chat_display, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #38bdf8; font-size: 11px; background: transparent; border: none; padding: 2px 4px;")
        self.lbl_status.hide()
        right_layout.addWidget(self.lbl_status)

        # Dynamic Reply Preview Banner
        self.reply_preview_frame = QFrame()
        self.reply_preview_frame.setStyleSheet("background-color: #1e293b; border-left: 3px solid #38bdf8; border-radius: 4px; padding: 4px;")
        reply_preview_layout = QHBoxLayout(self.reply_preview_frame)
        reply_preview_layout.setContentsMargins(8, 4, 8, 4)
        self.lbl_reply_preview = QLabel()
        self.lbl_reply_preview.setStyleSheet("color: #cbd5e1; font-size: 11px; background: transparent; border: none;")
        btn_cancel_reply = QPushButton("✕")
        btn_cancel_reply.setFixedSize(18, 18)
        btn_cancel_reply.setCursor(Qt.PointingHandCursor)
        btn_cancel_reply.setStyleSheet("background: transparent; color: #94a3b8; font-size: 11px; border: none;")
        btn_cancel_reply.clicked.connect(self.cancel_reply)
        reply_preview_layout.addWidget(self.lbl_reply_preview, 1)
        reply_preview_layout.addWidget(btn_cancel_reply)
        self.reply_preview_frame.hide()
        right_layout.addWidget(self.reply_preview_frame)

        # Staged Image Preview Bar (Appears upon Ctrl+V before sending)
        self.staged_image_frame = QFrame()
        self.staged_image_frame.setStyleSheet("QFrame{background:#111827;border:1px solid #334155;border-radius:7px;padding:4px}")
        staged_layout = QHBoxLayout(self.staged_image_frame)
        staged_layout.setContentsMargins(8, 4, 8, 4)
        staged_layout.setSpacing(8)

        self.lbl_staged_thumbnail = QLabel()
        self.lbl_staged_thumbnail.setFixedSize(36, 36)
        self.lbl_staged_thumbnail.setStyleSheet("border-radius: 4px; background: transparent;")

        self.lbl_staged_name = QLabel()
        self.lbl_staged_name.setTextFormat(Qt.PlainText)
        self.lbl_staged_name.setStyleSheet("color: #e2e8f0; font-size: 11px; background: transparent; border: none;")

        btn_cancel_staged = QPushButton("✕")
        btn_cancel_staged.setFixedSize(18, 18)
        self.btn_cancel_staged = btn_cancel_staged
        btn_cancel_staged.setCursor(Qt.PointingHandCursor)
        btn_cancel_staged.setStyleSheet(
            "QPushButton{background:#1e293b;color:#f8fafc;font-size:16px;font-weight:bold;"
            "border:1px solid #64748b;border-radius:12px;padding:0}"
            "QPushButton:hover{background:#7f1d1d;border-color:#ef4444;color:#fff}"
        )
        btn_cancel_staged.clicked.connect(self.clear_staged_image)

        staged_layout.addWidget(self.lbl_staged_thumbnail)
        staged_layout.addWidget(self.lbl_staged_name, 1)
        staged_layout.addWidget(btn_cancel_staged)
        self.staged_image_frame.hide()
        right_layout.addWidget(self.staged_image_frame)

        input_layout = QHBoxLayout()
        input_layout.setSpacing(8)

        self.btn_attach_img = QPushButton(" Image")
        self.btn_attach_img.setObjectName("iconBtn")
        self.btn_attach_img.setCursor(Qt.PointingHandCursor)
        self.btn_attach_img.setIcon(QIcon(IMAGE_PATH))
        self.btn_attach_img.clicked.connect(self.attach_image)
        input_layout.addWidget(self.btn_attach_img)

        self.btn_attach_file = QPushButton(" File")
        self.btn_attach_file.setObjectName("iconBtn")
        self.btn_attach_file.setCursor(Qt.PointingHandCursor)
        self.btn_attach_file.setIcon(QIcon(FILE_PATH))
        self.btn_attach_file.clicked.connect(self.attach_file)
        input_layout.addWidget(self.btn_attach_file)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type a message or press Ctrl+V to paste an image...")
        self.input_field.returnPressed.connect(self.send_message)
        self.input_field.installEventFilter(self)
        input_layout.addWidget(self.input_field, stretch=1)

        self.btn_send = QPushButton(" Send")
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.setIcon(QIcon(SEND_PATH))
        self.btn_send.clicked.connect(self.send_message)
        input_layout.addWidget(self.btn_send)

        right_layout.addLayout(input_layout)
        main_layout.addWidget(right_panel, stretch=1)

        # Facebook-style media sidebar (hidden until the Images button is pressed)
        self.media_sidebar = ChatInfoSidebar(self)
        self.media_sidebar.hide()
        self.media_sidebar.closed.connect(self.toggle_media_sidebar)
        main_layout.addWidget(self.media_sidebar)

        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.content_stack.addWidget(content_widget)
        offline_page = QWidget()
        offline_layout = QVBoxLayout(offline_page)
        offline_layout.addStretch(1)
        self.offline_label = QLabel("No internet connection")
        self.offline_label.setAlignment(Qt.AlignCenter)
        self.offline_label.setObjectName("offlineMessage")
        self.offline_label.setWordWrap(True)
        self.offline_label.setStyleSheet("color: #94a3b8; font-size: 14px; background: transparent; border: none;")
        offline_layout.addWidget(self.offline_label)
        offline_layout.addStretch(1)
        self.content_stack.addWidget(offline_page)
        main_vbox.addWidget(self.content_stack, 1)
        self.setMinimumSize(760, 520)
        self._title_bar.installEventFilter(self)   # double-click the title bar to maximize / restore

        self.network_timer = QTimer(self)
        self.network_timer.timeout.connect(self.check_internet_connection)
        self.network_timer.start(15000)
        # Let the main window connect to network_status_changed before the
        # first result is emitted.
        QTimer.singleShot(0, self.check_internet_connection)

        if self.db:
            self.user_listener = listen_for_all_users(self.on_users_snapshot)
            self.switch_chat("general", "General Chat")

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
            self.btn_maximize.setText("\u2610")
            self.btn_maximize.setToolTip("Maximize chat room (F11)")
        else:
            self.showMaximized()
            self.btn_maximize.setText("\u2750")
            self.btn_maximize.setToolTip("Restore chat room (F11)")

    def apply_theme(self, light):
        neutral = "#475569" if light else "#cbd5e1"
        try:
            self.btn_attach_img.setIcon(QIcon(recolor_svg(IMAGE_PATH, "chat_image_theme", neutral)))
            self.btn_attach_file.setIcon(QIcon(recolor_svg(FILE_PATH, "chat_file_theme", neutral)))
            self.btn_media.setIcon(QIcon(recolor_svg(MENU_PATH, "chat_menu_theme", neutral)))
            self.search_icon_label.setPixmap(QPixmap(recolor_svg(SEARCH_PATH, "chat_search_theme", neutral)).scaled(12, 12, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            staged_text = "#1e293b" if light else "#e2e8f0"
            staged_bg = "#f8fafc" if light else "#111827"
            staged_border = "#cbd5e1" if light else "#334155"
            self.staged_image_frame.setStyleSheet(
                f"QFrame{{background:{staged_bg};border:1px solid {staged_border};border-radius:7px;padding:4px}}"
            )
            self.lbl_staged_name.setStyleSheet(
                f"color:{staged_text};font-size:11px;background:transparent;border:none"
            )
            self.btn_cancel_staged.setStyleSheet(
                f"QPushButton{{background:{'#e2e8f0' if light else '#1e293b'};color:{'#1e293b' if light else '#f8fafc'};"
                f"font-size:16px;font-weight:bold;border:1px solid {staged_border};border-radius:12px;padding:0}}"
                "QPushButton:hover{background:#7f1d1d;border-color:#ef4444;color:#fff}"
            )
            for user_widget in self.findChildren(OnlineUserWidget):
                user_widget.apply_theme(light)
            for message_widget in self.findChildren(MessageItemWidget):
                message_widget.apply_theme(light)
        except RuntimeError:
            # The dialog can be disposed while the global theme walk is in flight.
            return

    def eventFilter(self, watched, event):
        """Intercepts Ctrl+V in the input box to stage an image from the clipboard instead of auto-sending."""
        if watched is getattr(self, "_title_bar", None):
            if event.type() == QEvent.MouseButtonDblClick:
                self.toggle_maximized()
                return True
            return False
        if watched is getattr(self, "input_field", None) and event.type() == QEvent.KeyPress:
            if event.matches(QKeySequence.Paste):
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime and mime.hasImage():
                    img = clipboard.image()
                    if not img.isNull():
                        temp_path = os.path.join(tempfile.gettempdir(), f"pasted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
                        img.save(temp_path, "PNG")
                        self.stage_clipboard_image(temp_path)
                        return True
        return super().eventFilter(watched, event)

    def stage_clipboard_image(self, file_path):
        self._staged_image_path = file_path
        pix = QPixmap(file_path)
        if not pix.isNull():
            self.lbl_staged_thumbnail.setPixmap(
                pix.scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        self.lbl_staged_name.setText(f"Image ready to send · {os.path.basename(file_path)}")
        self.staged_image_frame.show()
        self.input_field.setFocus()

    def clear_staged_image(self):
        if self._staged_image_path and os.path.exists(self._staged_image_path):
            try:
                os.remove(self._staged_image_path)
            except Exception:
                pass
        self._staged_image_path = None
        self.staged_image_frame.hide()

    def set_reply_target(self, msg_id, sender_name, text_preview):
        self._active_reply = {
            "id": msg_id,
            "sender": sender_name,
            "text": (text_preview[:60] + "…") if len(text_preview) > 60 else text_preview
        }
        self.lbl_reply_preview.setText(f"Replying to <b>{sender_name}</b>: {self._active_reply['text']}")
        self.reply_preview_frame.show()
        self.input_field.setFocus()

    def cancel_reply(self):
        self._active_reply = None
        self.reply_preview_frame.hide()

    def scroll_to_message_id(self, msg_id):
        if msg_id in self._msg_widget_map:
            item, widget = self._msg_widget_map[msg_id]
            self.chat_display.scrollToItem(item, QListWidget.PositionAtCenter)
            widget.flash_highlight()

    def focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def perform_search(self):
        term = self.search_input.text().strip().lower()
        matches = []
        for i in range(self.chat_display.count()):
            item = self.chat_display.item(i)
            widget = self.chat_display.itemWidget(item)
            if isinstance(widget, MessageItemWidget):
                ok = (not term) or (term in widget.search_blob)
                item.setHidden(not ok)
                widget.apply_highlight(term if ok else "")
                if term and ok:
                    matches.append(item)
            elif isinstance(widget, DateSeparatorWidget):
                item.setHidden(bool(term))

        self._matches = matches
        self._match_idx = -1
        if term:
            self.lbl_search_count.show()
            if matches:
                n = len(matches)
                self.lbl_search_count.setText(f"{n} match" + ("es" if n != 1 else "") + " · Enter = next")
                self.lbl_search_count.setStyleSheet("color: #94a3b8; font-size: 10px; background: transparent; border: none; padding-right: 6px;")
            else:
                self.lbl_search_count.setText("No matches")
                self.lbl_search_count.setStyleSheet("color: #f87171; font-size: 10px; background: transparent; border: none; padding-right: 6px;")
        else:
            self.lbl_search_count.hide()
            self.chat_display.scrollToBottom()

    def goto_next_match(self):
        if not self._matches:
            return
        self._match_idx = (self._match_idx + 1) % len(self._matches)
        self.chat_display.scrollToItem(self._matches[self._match_idx], QListWidget.PositionAtCenter)

    def load_profile_from_cloud(self):
        from database.db_manager import get_local_user_profile
        saved_name, saved_desig = get_local_user_profile(self.user_id)
        if saved_name:
            self.display_name = saved_name
        if saved_desig:
            self.designation = saved_desig

        if self.db and self.is_online:
            try:
                doc = self.db.collection("users").document(self.user_id).get()
                if doc.exists:
                    data = doc.to_dict()
                    self.display_name = data.get("display_name", self.display_name)
                    self.designation = data.get("designation", self.designation)
            except Exception:
                pass

    def update_profile(self, name, designation):
        self.display_name = name
        self.designation = designation
        from database.db_manager import save_local_user_profile
        save_local_user_profile(self.user_id, name, designation)
        if self.db:
            update_user_status(self.user_id, self.display_name, self.designation, True)

    def switch_chat(self, target_id, target_display_name):
        self.current_target = target_id
        self.current_target_name = target_display_name
        self.cancel_reply()
        self.clear_staged_image()
        self.search_input.clear()
        self._last_msg_count = -1
        self._last_messages = []
        self._known_message_ids.clear()
        self._notification_baseline_ready = False
        self._optimistic_likes.clear()

        if target_id == "general":
            self.lbl_chat_header.setText("General Chat Room")
            self.btn_general.setStyleSheet("text-align: left; padding: 12px; background-color: #1e293b; color: #ffffff; border-radius: 8px; font-size: 13px; font-weight: bold; border: none;")
            self.online_users_list.clearSelection()
            self.input_field.setPlaceholderText("Type a message or press Ctrl+V to paste an image...")
        else:
            self.lbl_chat_header.setText(f"Direct Message: {target_display_name}")
            self.btn_general.setStyleSheet("text-align: left; padding: 12px; background-color: transparent; color: #94a3b8; border-radius: 8px; font-size: 13px; border: none;")
            self.input_field.setPlaceholderText(f"Message {target_display_name}...")

        if self.chat_listener:
            try:
                self.chat_listener.unsubscribe()
            except Exception:
                pass
            self.chat_listener = None

        self.chat_display.clear()
        self._msg_widget_map.clear()

        if self.db and self.current_target:
            self.chat_listener = listen_for_messages(self.current_target, self.user_id, self.on_firestore_snapshot)
        self.refresh_media_sidebar()

    def on_user_clicked(self, item):
        target_id = item.data(Qt.UserRole)
        target_display = item.data(Qt.UserRole + 1)
        if target_id and target_id != self.user_id:
            self.switch_chat(target_id, target_display)

    def _collect_image_records(self):
        records = []
        seen = set()
        for message in self._last_messages:
            message_type = message.get("msg_type") or ""
            file_mime = mimetypes.guess_type(str(message.get("file_path") or ""))[0] or ""
            if message_type not in ("image", "images") and not (
                    message_type == "file" and file_mime.startswith("image/")):
                continue
            sent_at = message.get("timestamp")
            attachments = message.get("attachments") or []
            image_entries = list(message.get("images_deck") or [])
            if message.get("file_path"):
                image_entries.append((message.get("file_path"), message.get("text") or "Image"))
            for attachment in attachments:
                if isinstance(attachment, dict):
                    image_entries.append((attachment.get("url") or attachment.get("file_path") or "",
                                          attachment.get("name") or "Image"))
                elif isinstance(attachment, (tuple, list)) and len(attachment) >= 2:
                    image_entries.append((attachment[0], attachment[1]))
            for source, name in image_entries:
                if not source:
                    continue
                key = (str(source), str(sent_at))
                if key in seen:
                    continue
                seen.add(key)
                records.append((str(source), str(name or "Image"), sent_at))
        return records

    def _collect_file_records(self):
        records = []
        seen = set()
        for message in self._last_messages:
            if (message.get("msg_type") or "") != "file":
                continue
            url = str(message.get("file_path") or "")
            name = str(message.get("text") or "") or os.path.basename(url.split("?")[0]) or "File"
            if not url or (mimetypes.guess_type(name)[0] or "").startswith("image/"):
                continue   # images live in the Images tab
            sent_at = message.get("timestamp")
            key = (url, str(sent_at))
            if key in seen:
                continue
            seen.add(key)
            records.append((url, name, sent_at))
        return records

    def _sidebar_members(self):
        members = []
        for user in getattr(self, "_members", []):
            uid = user.get("user_id", "")
            is_self = (uid == self.user_id)
            if self.current_target != "general" and not (is_self or uid == self.current_target):
                continue   # in a direct message only the two participants are relevant
            if is_self:
                name = self.display_name or "Doctor"
                designation = self.designation or "Medical Staff"
            else:
                name = user.get("display_name") or "Doctor"
                designation = user.get("designation") or "Medical Staff"
            members.append((uid, name, designation, bool(user.get("is_online", False)), is_self))
        members.sort(key=lambda m: (not m[3], m[1].lower()))
        return members

    def refresh_media_sidebar(self):
        sidebar = getattr(self, "media_sidebar", None)
        if sidebar is None or not sidebar.isVisible():
            return
        sidebar.set_images(self._collect_image_records())
        sidebar.set_files(self._collect_file_records())
        sidebar.set_members(self._sidebar_members())

    def toggle_media_sidebar(self):
        if self.media_sidebar.isVisible():
            self.media_sidebar.hide()
        else:
            self.media_sidebar.show()
            self.refresh_media_sidebar()

    def check_internet_connection(self):
        try:
            if self._network_worker is not None and self._network_worker.isRunning():
                return
            self._network_worker = NetworkCheckWorker(self)
            self._network_worker.checked.connect(self._apply_network_status)
            self._network_worker.finished.connect(self._network_check_finished)
            self._network_worker.start()
        except RuntimeError:
            # Timer callbacks can arrive during window teardown.
            return

    def _network_check_finished(self):
        worker = self._network_worker
        self._network_worker = None
        if worker is not None:
            worker.deleteLater()

    def _apply_network_status(self, online):
        try:
            was_online = self.is_online
            self.is_online = bool(online)
            # Internet connectivity and the chat backend are separate states.
            # Keep the room useful and explain backend outages instead of
            # leaving an empty content pane.
            if not self.is_online:
                self.content_stack.setCurrentIndex(1)
                self.input_field.setPlaceholderText("Offline - Messages will be queued")
            elif not self.db:
                self.offline_label.setText("Chat service unavailable")
                self.content_stack.setCurrentIndex(1)
                self.input_field.setPlaceholderText("Chat service unavailable")
            else:
                self.offline_label.setText("No internet connection")
                self.content_stack.setCurrentIndex(0)
                if self.current_target == "general":
                    self.input_field.setPlaceholderText("Type a message or press Ctrl+V to paste an image...")
                else:
                    self.input_field.setPlaceholderText(f"Message {self.current_target_name}...")

            if self.is_online and self.db:
                try:
                    update_user_status(self.user_id, self.display_name, self.designation, True)
                except Exception:
                    pass
                if not was_online:
                    threading.Thread(target=self._run_cloud_backup, daemon=True).start()
            self.network_status_changed.emit(self.is_online)
        except RuntimeError:
            return

    def _run_cloud_backup(self):
        try:
            from database.db_manager import export_workstation_backup
            from database.firestore_manager import push_workstation_backup_to_cloud
            payload = export_workstation_backup(self.user_id)
            if payload:
                push_workstation_backup_to_cloud(self.user_id, payload)
        except Exception:
            pass
        
    def on_firestore_snapshot(self, *args, **kwargs):
        if not args:
            return
        first = args[0]
        if isinstance(first, list):
            msgs = first
        else:
            msgs = []
            try:
                for doc in first:
                    d = doc.to_dict() or {}
                    d.setdefault("id", doc.id)
                    msgs.append(d)
            except Exception:
                return

        try:
            if hasattr(self, 'bridge') and self.bridge is not None:
                self.bridge.messages_updated.emit(msgs)
        except (RuntimeError, AttributeError):
            pass

    def on_users_snapshot(self, *args, **kwargs):
        if not args:
            return
        col = args[0]
        try:
            users = [doc.to_dict() for doc in col]
            if hasattr(self, 'bridge') and self.bridge is not None:
                self.bridge.users_updated.emit(users)
        except (RuntimeError, AttributeError):
            pass
        except Exception:
            pass
        
    def request_like(self, msg_id, desired):
        if not (self.is_online and self.db and self.current_target):
            return False
        if msg_id in self._optimistic_likes:
            return False
        self._optimistic_likes[msg_id] = desired
        target, user = self.current_target, self.user_id

        def work():
            try:
                toggle_message_reaction(target, user, msg_id, not desired)
            except Exception as e:
                print(f"Like failed: {e}")

        threading.Thread(target=work, daemon=True).start()
        QTimer.singleShot(4000, lambda: self._expire_optimistic(msg_id))
        return True

    def _expire_optimistic(self, msg_id):
        if self._optimistic_likes.pop(msg_id, None) is not None and self._last_messages:
            self.render_messages(self._last_messages)

    def unsend_message(self, msg_id):
        if not (self.is_online and self.db and self.current_target):
            QMessageBox.warning(self, "Offline", "Cannot unsend messages while offline.")
            return
        target, user = self.current_target, self.user_id

        def work():
            delete_chat_message(target, user, msg_id)

        threading.Thread(target=work, daemon=True).start()

    def _group_consecutive_images(self, raw_messages):
        """Bundles consecutive image messages from the same sender into a single stacked deck message."""
        grouped = []
        i = 0
        n = len(raw_messages)
        while i < n:
            m = raw_messages[i]
            if m.get("msg_type") == "image":
                batch = [(m.get("file_path", ""), m.get("text", ""))]
                j = i + 1
                while j < n:
                    next_m = raw_messages[j]
                    if (next_m.get("msg_type") == "image" and 
                        next_m.get("sender_id") == m.get("sender_id") and
                        not next_m.get("reply_to")):
                        batch.append((next_m.get("file_path", ""), next_m.get("text", "")))
                        j += 1
                    else:
                        break
                if len(batch) > 1:
                    merged = dict(m)
                    merged["msg_type"] = "images"
                    merged["images_deck"] = batch
                    grouped.append(merged)
                    i = j
                    continue
            grouped.append(m)
            i += 1
        return grouped

    def render_messages(self, messages):
        current_ids = {str(msg.get("id")) for msg in messages if msg.get("id")}
        if self._notification_baseline_ready:
            new_incoming = [msg for msg in messages
                            if msg.get("id") and str(msg.get("id")) not in self._known_message_ids
                            and msg.get("sender_id") != self.user_id]
            if new_incoming and not (self.isVisible() and self.isActiveWindow()):
                msg = new_incoming[-1]
                sender = str(msg.get("display_name") or "A colleague")
                preview = str(msg.get("text") or "Sent an attachment").strip()
                self.new_message_received.emit(sender, preview[:140])
        self._known_message_ids = current_ids
        self._notification_baseline_ready = True
        self._last_messages = messages
        self.refresh_media_sidebar()
        bar = self.chat_display.verticalScrollBar()
        was_at_bottom = bar.value() >= bar.maximum() - 24
        old_value = bar.value()
        got_new = len(messages) != self._last_msg_count
        self._last_msg_count = len(messages)

        processed = self._group_consecutive_images(messages)

        self.chat_display.setUpdatesEnabled(False)
        try:
            self.chat_display.clear()
            self._msg_widget_map.clear()
            last_date_str = ""
            for msg in processed:
                try:
                    last_date_str = self._add_message(msg, last_date_str)
                except Exception:
                    traceback.print_exc()
        finally:
            self.chat_display.setUpdatesEnabled(True)

        try:
            self.perform_search()
        except Exception:
            traceback.print_exc()
            
        if not self.search_input.text().strip():
            if was_at_bottom or got_new or old_value == 0:
                self.chat_display.scrollToBottom()
            else:
                bar.setValue(old_value)

    def _add_message(self, msg, last_date_str):
        is_self = (msg.get("sender_id") == self.user_id)

        ts = msg.get("timestamp")
        time_str = "Just now"
        date_str = datetime.now().strftime('%B %d, %Y')
        if ts:
            try:
                time_str = ts.strftime('%I:%M %p')
                date_str = ts.strftime('%B %d, %Y')
            except Exception:
                pass

        if date_str != last_date_str:
            sep_widget = DateSeparatorWidget(date_str)
            sep_item = QListWidgetItem()
            sep_item.setSizeHint(sep_widget.sizeHint())
            sep_item.setFlags(Qt.NoItemFlags)
            self.chat_display.addItem(sep_item)
            self.chat_display.setItemWidget(sep_item, sep_widget)
            last_date_str = date_str

        msg_id = msg.get("id", "")
        likers = set(msg.get("liked_by", []) or [])
        if msg_id in self._optimistic_likes:
            want = self._optimistic_likes[msg_id]
            if (self.user_id in likers) == want:
                del self._optimistic_likes[msg_id]
            elif want:
                likers.add(self.user_id)
            else:
                likers.discard(self.user_id)

        shown_name = self.display_name if is_self else str(msg.get("display_name") or "Colleague")
        shown_desig = self.designation if is_self else str(msg.get("designation") or "")

        widget = MessageItemWidget(
            msg_id=msg_id,
            display_name=shown_name,
            designation=shown_desig,
            text=str(msg.get("text") or ""),
            timestamp_str=time_str,
            msg_type=msg.get("msg_type") or "text",
            file_path=str(msg.get("file_path") or ""),
            liked_by=list(likers),
            is_self=is_self,
            current_user_id=self.user_id,
            chat_panel=self,
            reply_to=msg.get("reply_to"),
            images_deck=msg.get("images_deck")
        )
        ThemeManager.instance().apply_widget(widget)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        item.setFlags(Qt.NoItemFlags)
        self.chat_display.addItem(item)
        self.chat_display.setItemWidget(item, widget)

        if msg_id:
            self._msg_widget_map[msg_id] = (item, widget)

        return last_date_str

    def render_online_users(self, users):
        self._members = list(users)
        self.online_users_list.clear()

        for u in users:
            uid = u.get("user_id", "")
            is_online = u.get("is_online", False)
            is_self = (uid == self.user_id)

            if is_self:
                display_name = self.display_name or "Doctor"
                designation = self.designation or "Medical Staff"
            else:
                display_name = u.get("display_name") or "Doctor"
                designation = u.get("designation") or "Medical Staff"

            widget = OnlineUserWidget(
                display_name=display_name,
                designation=designation,
                is_online=is_online,
                is_self=is_self
            )
            ThemeManager.instance().apply_widget(widget)
            item = QListWidgetItem()
            item.setSizeHint(QSize(240, 56))

            item.setData(Qt.UserRole, uid)
            item.setData(Qt.UserRole + 1, display_name)

            self.online_users_list.addItem(item)
            self.online_users_list.setItemWidget(item, widget)
        self.refresh_media_sidebar()

    def show_status(self, text, timeout_ms=0):
        self.lbl_status.setText(text)
        self.lbl_status.show()
        if timeout_ms:
            QTimer.singleShot(timeout_ms, lambda t=text: (self.lbl_status.hide() if self.lbl_status.text() == t else None))

    def hide_status(self):
        self.lbl_status.hide()

    def attach_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach Image", "", f"Images ({IMAGE_EXTS})")
        if path:
            # Selection only stages the attachment. It is uploaded and sent only
            # when the user presses Send.
            suffix = Path(path).suffix or ".img"
            staged_path = os.path.join(tempfile.gettempdir(), f"neuroquant_staged_{os.urandom(8).hex()}{suffix}")
            try:
                shutil.copy2(path, staged_path)
                self.stage_clipboard_image(staged_path)
            except OSError as exc:
                QMessageBox.warning(self, "Attachment error", f"Could not prepare the selected image:\n{exc}")

    def attach_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select File(s) to Send")
        self._queue_uploads(paths, "file")

    def _queue_uploads(self, paths, kind):
        if not paths:
            return
        if not (self.is_online and self.db):
            QMessageBox.warning(self, "Offline Mode", "No internet connection. Attachments can't be sent right now.")
            return
        if not supabase_configured():
            QMessageBox.warning(self, "Supabase not configured",
                                "Set the Supabase settings (see database/supabase_storage.py), then restart the app.")
            return
        for p in paths:
            self._upload_queue.append((p, kind, self.current_target))
        self._start_next_upload()

    def _start_next_upload(self):
        if self._upload_worker is not None or not self._upload_queue:
            return
        path, kind, target = self._upload_queue.popleft()
        remaining = len(self._upload_queue)
        extra = f" (+{remaining} more)" if remaining else ""
        self.show_status(f"Uploading {os.path.basename(path)}…{extra}")
        self.btn_attach_img.setEnabled(False)
        self.btn_attach_file.setEnabled(False)

        worker = UploadWorker(path, kind, target, self)
        worker.uploaded.connect(self._on_upload_done)
        worker.failed.connect(self._on_upload_failed)
        worker.finished.connect(self._on_worker_finished)
        self._upload_worker = worker
        worker.start()

    def _on_upload_done(self, kind, name, url, target):
        try:
            send_chat_message(self.user_id, self.display_name, self.designation, name, kind, url, target, reply_to=self._active_reply)
            self.cancel_reply()
        except TypeError:
            send_chat_message(self.user_id, self.display_name, self.designation, name, kind, url, target)
            self.cancel_reply()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Uploaded, but the message could not be sent: {e}")

    def _on_upload_failed(self, name, error):
        QMessageBox.warning(self, "Upload failed", f"Could not send {name}:\n{error}")

    def _on_worker_finished(self):
        if self._upload_worker is not None:
            self._upload_worker.deleteLater()
        self._upload_worker = None
        if self._upload_queue:
            self._start_next_upload()
        else:
            self.btn_attach_img.setEnabled(True)
            self.btn_attach_file.setEnabled(True)
            self.hide_status()

    def download_attachment(self, url, name):
        if not is_remote(url):
            QMessageBox.information(self, "Not available", "This attachment was sent before cloud sharing was enabled.")
            return
        default_dir = Path.home() / "Downloads"
        if not default_dir.exists():
            default_dir = Path.home()
        dest, _ = QFileDialog.getSaveFileName(self, "Save attachment", str(default_dir / (name or "attachment")))
        if not dest:
            return
        self.show_status(f"Downloading {name}…")
        cache = get_cache()

        def work():
            try:
                data = cache.get(url) or fetch_bytes(url, timeout=60)
                with open(dest, "wb") as f:
                    f.write(data)
                self.bridge.download_finished.emit(dest)
            except Exception as e:
                self.bridge.download_failed.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def send_message(self):
        text = self.input_field.text().strip()

        # If an image was pasted from clipboard, upload and send it now
        if self._staged_image_path:
            staged_path = self._staged_image_path
            self._staged_image_path = None
            self.staged_image_frame.hide()
            self._queue_uploads([staged_path], "image")

            if text:
                if self.is_online and self.db:
                    try:
                        send_chat_message(self.user_id, self.display_name, self.designation, text, "text", "", self.current_target, reply_to=self._active_reply)
                    except TypeError:
                        send_chat_message(self.user_id, self.display_name, self.designation, text, "text", "", self.current_target)
                self.input_field.clear()
                self.cancel_reply()
            return

        if not text:
            return

        if not self.db:
            from database.firestore_manager import get_db
            self.db = get_db()

        if self.is_online and self.db:
            try:
                send_chat_message(self.user_id, self.display_name, self.designation, text, "text", "", self.current_target, reply_to=self._active_reply)
            except TypeError:
                send_chat_message(self.user_id, self.display_name, self.designation, text, "text", "", self.current_target)
            self.input_field.clear()
            self.cancel_reply()
        else:
            if not self.db:
                QMessageBox.warning(self, "Cloud Service Disconnected", 
                                    "Could not connect to Firebase database.\nPlease verify the Firebase settings in the installation .env file.")
            else:
                QMessageBox.warning(self, "Offline Mode", "No internet connection. Message queued.")
                
    def closeEvent(self, event):
        if hasattr(self, "network_timer"):
            self.network_timer.stop()
        worker = getattr(self, "_network_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(6500)
        if self.chat_listener:
            try:
                self.chat_listener.unsubscribe()
            except Exception:
                pass
            self.chat_listener = None
        if self.user_listener:
            try:
                self.user_listener.unsubscribe()
            except Exception:
                pass
            self.user_listener = None
        super().closeEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.isMaximized():
            return
        if event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()
