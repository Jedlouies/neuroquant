"""
theme_manager.py

Runtime Dark / Light theme switching for NeuroQuant.

The application ALWAYS starts in the dark theme. Switching to light is a
per-session choice (View > Theme); it is not remembered between launches.

The UI is styled with many hard-coded dark hex colours inside per-widget
stylesheets. Instead of rewriting every one of them, ThemeManager keeps the
ORIGINAL (dark) stylesheet of each widget and, when the light theme is
selected, produces a light version by re-mapping those colours. Switching back
to dark simply restores the originals, so the dark look is never altered.

Usage:
    from ui.theme_manager import ThemeManager, DARK, LIGHT

    tm = ThemeManager.instance()
    tm.attach(QApplication.instance())   # once, after the main window is built
    tm.apply(LIGHT)                      # switch
    tm.toggle()                          # flip between the two

Notes:
    * The 2D slice canvases stay black in both themes (mark any widget the same
      way with widget.setProperty("nq_keep_dark", True); its children are skipped).
    * Widgets that draw themselves (e.g. the 3D viewer's matplotlib canvas) can
      define `apply_theme(self, light: bool)`; it is called on every switch.
    * Whatever application-level stylesheet / palette is installed before
      attach() is captured, so it is themed too and restored on switching back.
    * Dialogs are created on demand, so they are themed when they are shown.
"""

import colorsys
import re

from PySide6.QtCore import QObject, QEvent, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

DARK = "dark"
LIGHT = "light"

# Widgets with these class names are never re-themed (brand splash is always dark).
_ALWAYS_DARK_CLASSES = {"NeuroQuantSplashScreen"}

# Property names stored on widgets.
_P_ORIG = "_nq_orig_qss"
_P_APPLIED = "_nq_applied_qss"
_P_KEEP_DARK = "nq_keep_dark"

# --------------------------------------------------------------------------- #
# Colour mapping (dark -> light)
# --------------------------------------------------------------------------- #
_DARK_TO_LIGHT = {
    # surfaces
    "#0b0f17": "#ffffff",   # window background
    "#0d1117": "#ffffff",   # panel background
    "#0a0e14": "#ffffff",
    "#080808": "#ffffff",
    "#0f1724": "#f8fafc",
    "#0f172a": "#ffffff",
    "#111827": "#f8fafc",
    "#131e2d": "#f8fafc",
    "#101622": "#f1f5f9",   # scrollbar track
    "#161b22": "#ffffff",   # inputs / headers
    "#161f2e": "#ffffff",   # menus, toggle tab
    "#1a1a1a": "#ffffff",
    "#202020": "#ffffff",   # combo boxes
    "#262626": "#ffffff",
    "#21262d": "#f1f5f9",   # buttons, grid lines
    # borders / hover fills
    "#1e293b": "#cbd5e1",
    "#1f2937": "#dbeafe",   # table selection background
    "#123746": "#dbeafe",   # selected profile/list row
    "#263244": "#cbd5e1",   # dialog borders
    "#26364c": "#e2e8f0",   # dark hover fill
    "#161e2e": "#f1f5f9",   # dark hover fill
    "#222222": "#cbd5e1",
    "#30363d": "#cbd5e1",
    "#333333": "#cbd5e1",
    "#334155": "#94a3b8",
    "#444444": "#94a3b8",
    "#475569": "#64748b",
    "#484f58": "#6e7781",
    # text
    "#ffffff": "#0f172a",
    "#f8fafc": "#0f172a",
    "#f1f5f9": "#0f172a",
    "#f0f6fc": "#0f172a",
    "#e2e8f0": "#1e293b",
    "#e0e0e0": "#1e293b",
    "#cbd5e1": "#334155",
    "#c9d1d9": "#334155",
    "#cccccc": "#334155",
    "#b0bec5": "#475569",
    "#b0b0b0": "#475569",
    "#94a3b8": "#64748b",
    "#9ca3af": "#64748b",
    "#8b949e": "#57606a",
    "#888888": "#64748b",
    "#777777": "#64748b",
    "#64748b": "#475569",
    # accents used as text / borders
    "#00e5ff": "#0e7490",
    "#00bcd4": "#0891b2",
    "#38bdf8": "#0369a1",
    "#81d4fa": "#0369a1",
    "#ff8a80": "#b91c1c",
    "#ff5252": "#dc2626",
    # accents that read fine on both themes
    "#00838f": "#00838f",
    "#0097a7": "#0097a7",
    "#0284c7": "#0284c7",
    "#ef4444": "#ef4444",
}

# A rule whose background is one of these is a filled accent (e.g. a teal
# button with white text). Its colours are left untouched so the text stays
# white on the accent.
_ACCENT_BACKGROUNDS = {"#00838f", "#0097a7", "#00bcd4", "#ef4444"}

# Colours not in the table are only flipped when they are neutral (grey/slate).
# Saturated colours (legend swatches, tumor colours, brand accents) are kept.
_NEUTRAL_SATURATION = 0.25

_TOKEN_RE = re.compile(
    r"qlineargradient\([^)]*\)"
    r"|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*[\d.]+\s*)?\)"
    r"|#[0-9a-fA-F]{6}\b"
    r"|#[0-9a-fA-F]{3}\b"
)
_NAMED_WHITE_TEXT_RE = re.compile(r"(?<![-\w])(color\s*:\s*)white\b", re.IGNORECASE)
_BLOCK_RE = re.compile(r"([^{}]*)\{([^{}]*)\}")
_BG_RE = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{6})\b")


def _expand_hex(token: str) -> str:
    token = token.lower()
    if len(token) == 4:  # #abc -> #aabbcc
        token = "#" + "".join(ch * 2 for ch in token[1:])
    return token


def _map_color(hex6: str) -> str:
    """Map one dark-theme colour (#rrggbb) to its light-theme counterpart."""
    mapped = _DARK_TO_LIGHT.get(hex6)
    if mapped:
        return mapped
    c = QColor(hex6)
    h, l, s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    if s >= _NEUTRAL_SATURATION:
        return hex6
    r, g, b = colorsys.hls_to_rgb(h, 1.0 - l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def _map_token(token: str) -> str:
    if token.startswith("qlineargradient"):
        # Theme the subtle NeuroQuant abstract surface washes while preserving
        # the saturated progress-bar gradient used elsewhere in the UI.
        if any(color in token.lower() for color in ("#111a29", "#122333", "#101825", "#111b2b", "#101a2b", "#101722")):
            return "qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #f3f7fb,stop:0.55 #f8fafc,stop:1 #edf4f8)"
        return token  # progress-bar gradients look right on both themes
    if token.startswith("rgb"):
        nums = re.findall(r"[\d.]+", token)
        r, g, b = (int(float(n)) for n in nums[:3])
        mapped = QColor(_map_color("#{:02x}{:02x}{:02x}".format(r, g, b)))
        if len(nums) > 3:  # keep the original alpha
            return f"rgba({mapped.red()}, {mapped.green()}, {mapped.blue()}, {nums[3]})"
        return f"rgb({mapped.red()}, {mapped.green()}, {mapped.blue()})"
    return _map_color(_expand_hex(token))


def _map_colors(text: str) -> str:
    text = _NAMED_WHITE_TEXT_RE.sub(r"\1#0f172a", text)
    return _TOKEN_RE.sub(lambda m: _map_token(m.group(0)), text)


def _has_accent_background(body: str) -> bool:
    return any(m.group(1).lower() in _ACCENT_BACKGROUNDS for m in _BG_RE.finditer(body))


def to_light_stylesheet(qss: str) -> str:
    """Return a light-theme version of a dark stylesheet."""
    if not qss:
        return qss
    if "{" not in qss:              # bare declarations, e.g. "background-color: #0b0f17;"
        return _map_colors(qss)

    def repl_block(m):
        selector, body = m.group(1), m.group(2)
        if not _has_accent_background(body):
            body = _map_colors(body)
        return f"{_map_colors(selector)}{{{body}}}"

    return _BLOCK_RE.sub(repl_block, qss)


# Appended to the application stylesheet in the light theme so that message
# boxes (e.g. the exit confirmation) always have visible text and buttons,
# whatever global stylesheet the app installed for its dark look.
_LIGHT_BASELINE_QSS = """
QMessageBox {
    background-color: #ffffff;
}
QMessageBox QLabel {
    color: #0f172a;
    background: transparent;
}
QMessageBox QPushButton, QDialogButtonBox QPushButton {
    background-color: #f1f5f9;
    color: #0f172a;
    border: 1px solid #94a3b8;
    border-radius: 4px;
    padding: 6px 18px;
    min-width: 64px;
    font-weight: bold;
}
QMessageBox QPushButton:hover, QDialogButtonBox QPushButton:hover {
    background-color: #e2e8f0;
}
QMessageBox QPushButton:default, QDialogButtonBox QPushButton:default {
    border: 1px solid #00838f;
}
"""


def _light_palette() -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#ffffff",
        QPalette.ColorRole.WindowText: "#0f172a",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f8fafc",
        QPalette.ColorRole.Text: "#0f172a",
        QPalette.ColorRole.Button: "#f1f5f9",
        QPalette.ColorRole.ButtonText: "#0f172a",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Light: "#ffffff",
        QPalette.ColorRole.Midlight: "#f1f5f9",
        QPalette.ColorRole.Mid: "#cbd5e1",
        QPalette.ColorRole.Dark: "#94a3b8",
        QPalette.ColorRole.Shadow: "#64748b",
        QPalette.ColorRole.ToolTipBase: "#ffffff",
        QPalette.ColorRole.ToolTipText: "#0f172a",
        QPalette.ColorRole.PlaceholderText: "#64748b",
        QPalette.ColorRole.Highlight: "#00838f",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#0369a1",
        QPalette.ColorRole.LinkVisited: "#6d28d9",
    }
    if hasattr(QPalette.ColorRole, "Accent"):  # Qt 6.6+
        roles[QPalette.ColorRole.Accent] = "#00838f"
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#94a3b8"))
    return p


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
class ThemeManager(QObject):
    theme_changed = Signal(str)

    _instance = None

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._theme = DARK          # dark is always the starting theme
        self._attached = False
        self._orig_app_qss = ""
        self._orig_palette = None

    # -- public API ---------------------------------------------------------
    @property
    def current(self) -> str:
        return self._theme

    def attach(self, app: QApplication):
        """Capture the app's current (dark) look and hook dialogs so they can be themed."""
        if self._attached:
            return
        self._attached = True
        self.setParent(app)  # let Qt own/destroy it together with the application
        self._orig_app_qss = app.styleSheet()
        self._orig_palette = QPalette(app.palette())
        app.installEventFilter(self)
        app.aboutToQuit.connect(lambda: app.removeEventFilter(self))

    def apply(self, theme: str):
        theme = LIGHT if theme == LIGHT else DARK
        app = QApplication.instance()
        light = theme == LIGHT

        if self._attached:
            if light:
                app.setStyleSheet(to_light_stylesheet(self._orig_app_qss) + _LIGHT_BASELINE_QSS)
                app.setPalette(_light_palette())
            else:
                app.setStyleSheet(self._orig_app_qss)
                app.setPalette(self._orig_palette)

        self._theme = theme
        for widget in app.topLevelWidgets():
            self._walk(widget, light)

        self.theme_changed.emit(theme)

    def toggle(self):
        self.apply(DARK if self._theme == LIGHT else LIGHT)

    def apply_widget(self, widget: QWidget):
        """Apply the current palette to widgets created after the last theme switch."""
        self._walk(widget, self._theme == LIGHT)

    # -- internals ----------------------------------------------------------
    def eventFilter(self, obj, event):
        # Dialogs are built (and styled dark) on demand: theme them as they appear.
        try:
            if (self._theme == LIGHT
                    and event.type() == QEvent.Type.Show
                    and isinstance(obj, QWidget)
                    and obj.isWindow()):
                self._walk(obj, True)
        except RuntimeError:  # widget already deleted during teardown
            pass
        return False

    def _walk(self, widget: QWidget, light: bool):
        # Qt wrappers may outlive their C++ widgets briefly while dialogs are
        # being closed. Theme changes must tolerate that teardown window.
        try:
            if widget.property(_P_KEEP_DARK) or type(widget).__name__ in _ALWAYS_DARK_CLASSES:
                return
            self._retheme(widget, light)

            hook = getattr(widget, "apply_theme", None)
            if callable(hook):
                try:
                    hook(light)
                except RuntimeError:
                    return

            children = list(widget.children())
        except RuntimeError:
            return

        for child in children:
            try:
                if isinstance(child, QWidget):
                    self._walk(child, light)
            except RuntimeError:
                continue

    @staticmethod
    def _retheme(widget: QWidget, light: bool):
        current = widget.styleSheet()
        orig = widget.property(_P_ORIG)
        applied = widget.property(_P_APPLIED)

        # First visit, or the app restyled this widget itself since we last touched it.
        if orig is None or (current != orig and current != applied):
            orig = current
            widget.setProperty(_P_ORIG, orig)
        if not orig:
            return

        target = to_light_stylesheet(orig) if light else orig
        if target != current:
            widget.setStyleSheet(target)
        widget.setProperty(_P_APPLIED, target)
