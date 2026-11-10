"""Persistent application-wide text scaling for Qt widgets and stylesheets."""

import re
import math

from PySide6.QtCore import QObject, QEvent, QSettings, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget


_BASE_QSS = "_nq_font_base_stylesheet"
_LAST_QSS = "_nq_font_scaled_stylesheet"
_BASE_FONT = "_nq_font_base_font"
_BASE_MINIMUM = "_nq_font_base_minimum"
_BASE_MAXIMUM = "_nq_font_base_maximum"
_LAST_MINIMUM = "_nq_font_last_minimum"
_LAST_MAXIMUM = "_nq_font_last_maximum"
_LAST_GEOMETRY_SCALE = "_nq_font_last_geometry_scale"
_FONT_SIZE_RE = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)(px|pt)", re.IGNORECASE)
_SPACING_RE = re.compile(r"((?:padding|margin)(?:-left|-right|-top|-bottom)?\s*:\s*)([^;]+)", re.IGNORECASE)


def _scaled_stylesheet(widget, scale):
    current = widget.styleSheet()
    previous_scaled = widget.property(_LAST_QSS)
    base = widget.property(_BASE_QSS)
    if base is None or (previous_scaled is not None and current != previous_scaled):
        base = current
        widget.setProperty(_BASE_QSS, base)
    if not base:
        return
    if abs(scale - 1.0) < 0.001:
        new = base
    else:
        def replace(match):
            return f"{match.group(1)}{float(match.group(2)) * scale:g}{match.group(3)}"
        def replace_spacing(match):
            value = re.sub(
                r"(\d+(?:\.\d+)?)(px)",
                lambda dim: f"{float(dim.group(1)) * scale:g}{dim.group(2)}",
                match.group(2),
            )
            return match.group(1) + value
        new = _SPACING_RE.sub(replace_spacing, _FONT_SIZE_RE.sub(replace, base))
    if current != new:
        widget.setStyleSheet(new)
    widget.setProperty(_LAST_QSS, new)


def _rescale_font_size(font, factor):
    """Scale point- or pixel-sized fonts without touching Qt's -1 sentinel."""
    point_size = font.pointSizeF()
    if math.isfinite(point_size) and point_size > 0:
        font.setPointSizeF(point_size * factor)
        return True
    pixel_size = font.pixelSize()
    if pixel_size > 0:
        font.setPixelSize(max(1, round(pixel_size * factor)))
        return True
    return False


def _normalize_font_size(font, factor):
    """Undo a prior scale when capturing a widget's unscaled base font."""
    if factor <= 0:
        return
    point_size = font.pointSizeF()
    if math.isfinite(point_size) and point_size > 0:
        font.setPointSizeF(point_size / factor)
        return
    pixel_size = font.pixelSize()
    if pixel_size > 0:
        font.setPixelSize(max(1, round(pixel_size / factor)))


class _FontScaleFilter(QObject):
    def __init__(self, app, scale):
        super().__init__(app)
        self.app = app
        self.scale = scale
        self.base_font = QFont(app.font())
        self._applying = False

    def apply_to(self, widget):
        if not isinstance(widget, QWidget) or self._applying:
            return
        self._applying = True
        try:
            _scaled_stylesheet(widget, self.scale)
            base_font = widget.property(_BASE_FONT)
            if base_font is None:
                base_font = QFont(widget.font())
                _normalize_font_size(base_font, self.scale)
                widget.setProperty(_BASE_FONT, base_font)
            font = QFont(base_font)
            _rescale_font_size(font, self.scale)
            widget.setFont(font)
            self._scale_fixed_geometry(widget)
        except RuntimeError:
            # Widgets can be deleted while Qt is sending polish/show events.
            return
        finally:
            self._applying = False

    def _scale_fixed_geometry(self, widget):
        minimum = widget.minimumSize()
        maximum = widget.maximumSize()
        last_min = widget.property(_LAST_MINIMUM)
        last_max = widget.property(_LAST_MAXIMUM)
        last_scale = float(widget.property(_LAST_GEOMETRY_SCALE) or self.scale)
        base_min = widget.property(_BASE_MINIMUM)
        base_max = widget.property(_BASE_MAXIMUM)
        if base_min is None or (last_min is not None and (minimum != last_min or maximum != last_max)):
            base_min = QSize(minimum)
            base_max = QSize(maximum)
            if last_min is not None and last_scale > 0:
                for size in (base_min, base_max):
                    size.setWidth(round(size.width() / last_scale))
                    size.setHeight(round(size.height() / last_scale))
            widget.setProperty(_BASE_MINIMUM, base_min)
            widget.setProperty(_BASE_MAXIMUM, base_max)
        new_min = QSize(base_min)
        new_max = QSize(base_max)
        for axis in ("Width", "Height"):
            getter = getattr(base_min, f"width" if axis == "Width" else "height")
            max_getter = getattr(base_max, f"width" if axis == "Width" else "height")
            minimum_value, maximum_value = getter(), max_getter()
            if minimum_value > 0 and minimum_value == maximum_value:
                value = round(minimum_value * self.scale)
                getattr(new_min, f"set{axis}")(value)
                getattr(new_max, f"set{axis}")(value)
        if minimum != new_min or maximum != new_max:
            widget.setMinimumSize(new_min)
            widget.setMaximumSize(new_max)
        widget.setProperty(_LAST_MINIMUM, new_min)
        widget.setProperty(_LAST_MAXIMUM, new_max)
        widget.setProperty(_LAST_GEOMETRY_SCALE, self.scale)

    def apply_all(self):
        widgets = self.app.allWidgets()
        for widget in widgets:
            if widget.property(_BASE_FONT) is None:
                base_font = QFont(widget.font())
                _normalize_font_size(base_font, self.scale)
                widget.setProperty(_BASE_FONT, base_font)
            _scaled_stylesheet(widget, self.scale)
        app_font = QFont(self.base_font)
        _rescale_font_size(app_font, self.scale)
        self.app.setFont(app_font)
        for widget in widgets:
            self.apply_to(widget)

    def eventFilter(self, obj, event):
        if self._applying:
            return False
        try:
            if isinstance(obj, QWidget) and event.type() in (QEvent.Type.Show, QEvent.Type.Polish):
                self.apply_to(obj)
        except RuntimeError:
            return False
        return False


def scale_widget_tree(root, scale):
    """Scale a dialog and its current children relative to their original fonts."""
    global_scale = float(QSettings("NeuroQuant", "NeuroQuant").value("accessibility/font_scale", 1.0))
    app = QApplication.instance()
    manager = getattr(app, "_nq_font_scale_filter", None) if app else None
    previous_scale = manager.scale if manager else None
    if manager:
        manager.scale = scale
    widgets = [root, *root.findChildren(QWidget)]
    try:
        for widget in widgets:
            _scaled_stylesheet(widget, scale)
            base_font = widget.property(_BASE_FONT)
            if base_font is None:
                base_font = QFont(widget.font())
                _normalize_font_size(base_font, global_scale)
                widget.setProperty(_BASE_FONT, base_font)
            font = QFont(base_font)
            _rescale_font_size(font, scale)
            widget.setFont(font)
            if manager:
                manager._scale_fixed_geometry(widget)
    finally:
        if manager:
            manager.scale = previous_scale


def apply_font_scale(app=None, scale=None):
    app = app or QApplication.instance()
    if app is None:
        return 1.0
    settings = QSettings("NeuroQuant", "NeuroQuant")
    if scale is None:
        scale = float(settings.value("accessibility/font_scale", 1.0))
    scale = max(0.8, min(1.5, float(scale)))
    settings.setValue("accessibility/font_scale", scale)
    manager = getattr(app, "_nq_font_scale_filter", None)
    if manager is None:
        manager = _FontScaleFilter(app, scale)
        app._nq_font_scale_filter = manager
        app.installEventFilter(manager)
    else:
        manager.scale = scale
    manager.apply_all()
    return scale
