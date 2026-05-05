from __future__ import annotations

import os
import re
import sys
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QProcess,
    QProcessEnvironment,
    QRunnable,
    QRectF,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QCursor,
    QDesktopServices,
    QIcon,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .design_tokens import (
    PRIMARY_BAND,
    PRIMARY_GHOST,
    PRIMARY_SOFT,
)
from .frosted_surface import _FrostedSurface
from .models import (
    AccountRecord,
    AuthMode,
    CodexHomeTarget,
    CodexSnapshot,
    OfficialRepairSummary,
    RestorePointManifest,
    SandboxRealSessionSyncResult,
    SnapshotKind,
    UiLanguage,
    WritePreview,
)
from .localization import translate
from .services import AppServices
from .sessions_page import SessionsPage


APP_DISPLAY_NAME = "CodeXrosser"
APP_ICON_ASSET = "cqv-app-icon.png"


_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://[^\s\x1b\]\[]+")
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{3,5}-[A-Z0-9]{3,7})\b")


def _strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences (color, cursor) from CLI output."""
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def _summarize_status(text: str) -> str:
    """Reduce a multi-line CLI dump to a single actionable line.

    Prefers extracted (URL, device-code) pairs from codex login output;
    otherwise falls back to the last non-empty line."""
    if not text:
        return ""
    cleaned = _strip_ansi(text).strip()
    if not cleaned:
        return ""
    url_match = _URL_RE.search(cleaned)
    if url_match:
        url = url_match.group(0).rstrip(".,;:)")
        code_match = _DEVICE_CODE_RE.search(cleaned)
        if code_match:
            return f"Open {url} and enter code {code_match.group(1)}"
        return f"Open {url}"
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1]


_SUCCESS_PREFIXES = (
    "switched ", "added ", "renamed ", "removed ", "rolled back ",
    "seeded ", "opened ", "started ", "quota refreshed", "repair complete",
)


def _infer_severity(text: str) -> str:
    """Pick a banner color based on message content."""
    if not text:
        return "info"
    lower = text.lower().strip()
    if any(token in lower for token in ("failed", "error", "could not", "couldn't", "denied", "abort")):
        return "error"
    if "warning" in lower or lower.startswith("warning"):
        return "warning"
    if any(lower.startswith(prefix) for prefix in _SUCCESS_PREFIXES):
        return "success"
    return "info"


def _asset_path(name: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if bundle_root:
        root = Path(bundle_root)
        candidates.extend([root / "codex_quota_viewer" / "assets" / name, root / "assets" / name])
    candidates.append(Path(__file__).resolve().parent / "assets" / name)
    return next((path for path in candidates if path.exists()), candidates[-1])


def _asset_pixmap(name: str) -> QPixmap | None:
    pixmap = QPixmap(str(_asset_path(name)))
    return None if pixmap.isNull() else pixmap


def _asset_icon(name: str) -> QIcon:
    path = _asset_path(name)
    return QIcon(str(path)) if path.exists() else QIcon()


def _apply_dark_palette(app: QApplication) -> None:
    """Pin native Qt controls to the app's dark visual language.

    The window is dark and mostly custom-painted, but native widgets still
    consult QApplication.palette() for viewports, placeholder text, disabled
    text, menus, item selections, and tooltips. On a light Windows theme those
    roles default to white surfaces and black text, which leaks through in
    QListView/QTextEdit/QMenu surfaces that QSS does not fully cover.
    """
    app.setStyle("Fusion")
    palette = QPalette()
    colors: dict[QPalette.ColorRole, QColor] = {
        QPalette.Window: QColor(22, 25, 29),
        QPalette.WindowText: QColor(255, 255, 255),
        QPalette.Base: QColor(20, 22, 26),
        QPalette.AlternateBase: QColor(30, 34, 40),
        QPalette.ToolTipBase: QColor(32, 37, 43),
        QPalette.ToolTipText: QColor(255, 255, 255),
        QPalette.Text: QColor(255, 255, 255),
        QPalette.Button: QColor(34, 39, 46),
        QPalette.ButtonText: QColor(255, 255, 255),
        QPalette.BrightText: QColor(255, 255, 255),
        QPalette.Highlight: QColor(10, 132, 255),
        QPalette.HighlightedText: QColor(255, 255, 255),
        QPalette.Link: QColor(64, 156, 255),
        QPalette.LinkVisited: QColor(120, 168, 235),
        QPalette.Light: QColor(72, 78, 88),
        QPalette.Midlight: QColor(54, 60, 68),
        QPalette.Mid: QColor(42, 48, 56),
        QPalette.Dark: QColor(14, 17, 21),
        QPalette.Shadow: QColor(0, 0, 0),
    }
    disabled_colors: dict[QPalette.ColorRole, QColor] = {
        QPalette.WindowText: QColor(235, 235, 245, 86),
        QPalette.Text: QColor(235, 235, 245, 86),
        QPalette.ButtonText: QColor(235, 235, 245, 86),
        QPalette.Highlight: QColor(10, 132, 255, 80),
        QPalette.HighlightedText: QColor(235, 235, 245, 110),
    }
    if hasattr(QPalette, "PlaceholderText"):
        colors[QPalette.PlaceholderText] = QColor(235, 235, 245, 115)
        disabled_colors[QPalette.PlaceholderText] = QColor(235, 235, 245, 70)

    for group in (
        QPalette.Active,
        QPalette.Inactive,
        QPalette.Disabled,
    ):
        for role, color in colors.items():
            palette.setColor(group, role, color)
    for role, color in disabled_colors.items():
        palette.setColor(QPalette.Disabled, role, color)
    app.setPalette(palette)


def _globe_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(Qt.NoBrush)
    pen = QPen(QColor(235, 235, 245, 215), 3.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawEllipse(6, 6, 36, 36)
    painter.drawEllipse(18, 6, 12, 36)
    painter.drawLine(10, 18, 38, 18)
    painter.drawLine(10, 30, 38, 30)
    painter.end()
    return QIcon(pixmap)


def _refresh_icon() -> QIcon:
    """Rescan / refresh action icon. Used on the sessions panel's heading
    rescan button. Loaded from assets/refresh.svg via Qt's SVG icon
    engine (the previous procedural QPainter version produced a smaller,
    less recognisable arrow-arc that rendered worse at button sizes)."""
    return _asset_icon("refresh.svg")


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    progress = Signal(str)


class Worker(QRunnable):
    def __init__(self, action: Callable[[Callable[[str], None]], Any]):
        super().__init__()
        self.setAutoDelete(False)
        self.action = action
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.action(self.signals.progress.emit)
            self.signals.result.emit(result)
        except Exception as ex:
            self.signals.error.emit(ex)


@dataclass(frozen=True)
class SettingsData:
    target: CodexHomeTarget
    snapshots: list[RestorePointManifest]
    latest: RestorePointManifest | None
    automatic_status: Any
    manual_status: Any
    audit_events: list[dict[str, Any]]


class SmoothCheckBox(QCheckBox):
    def __init__(self, label: str, parent: QWidget | None = None):
        super().__init__(label, parent)
        self.setObjectName("SyncOption")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(26)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 34, max(26, metrics.height() + 8))

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        box_size = 18.0
        box = QRectF(1.0, (self.height() - box_size) / 2, box_size, box_size)
        if not self.isEnabled():
            border = QColor(235, 235, 245, 76)
            fill = QColor(255, 255, 255, 6)
            text = QColor(235, 235, 245, 76)
        elif self.isChecked():
            border = QColor(55, 211, 232, 255)
            fill = QColor(55, 211, 232, 255)
            text = QColor(255, 255, 255, 242)
        else:
            border = QColor(235, 235, 245, 165 if self.underMouse() else 130)
            fill = QColor(255, 255, 255, 10 if self.underMouse() else 4)
            text = QColor(255, 255, 255, 235)

        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 4.0, 4.0)

        if self.isChecked():
            pen = QPen(QColor(20, 29, 34, 245), 2.15)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(QPointF(box.left() + 4.4, box.top() + 9.5), QPointF(box.left() + 7.8, box.top() + 12.8))
            painter.drawLine(QPointF(box.left() + 7.8, box.top() + 12.8), QPointF(box.left() + 13.8, box.top() + 5.7))

        painter.setPen(text)
        painter.drawText(QRectF(29, 0, self.width() - 29, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        painter.end()


class CaptionButton(QPushButton):
    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("CaptionButton")
        self.setProperty("kind", kind)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        color = QColor(255, 255, 255, 242) if self.underMouse() else QColor(235, 235, 245, 224)
        pen = QPen(color, 1.45)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.SquareCap)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)

        cx = round(self.width() / 2)
        cy = round(self.height() / 2)
        if self.kind == "minimize":
            painter.drawLine(QPointF(cx - 5, cy + 1), QPointF(cx + 5, cy + 1))
        elif self.kind == "maximize":
            painter.drawRect(QRectF(cx - 4.5, cy - 4.5, 9, 9))
        elif self.kind == "close":
            painter.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            painter.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
        painter.end()


class TitleBar(QFrame):
    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self.window = window
        self._drag_start: QPoint | None = None
        self.setObjectName("TitleBar")
        self.setFixedHeight(54)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 8, 14, 0)
        layout.setSpacing(0)

        self._nav_layout = QHBoxLayout()
        self._nav_layout.setContentsMargins(0, 0, 0, 0)
        self._nav_layout.setSpacing(4)
        layout.addLayout(self._nav_layout)
        layout.addStretch(1)

        # Slot for the inline status pill (replaces the old bottom-of-window
        # toast). Sits centered in the empty band between nav and utility.
        # The pill itself is built by MainWindow and inserted via
        # ``add_status_pill``; until then this index is just an empty stretch.
        self._status_slot_index = layout.count()
        self._status_pill: QWidget | None = None
        layout.addStretch(1)

        self._utility_layout = QHBoxLayout()
        self._utility_layout.setContentsMargins(0, 0, 8, 0)
        self._utility_layout.setSpacing(4)
        layout.addLayout(self._utility_layout)

        self.minimize_button = self._caption_button("–", "minimize")
        self.maximize_button = self._caption_button("□", "maximize")
        self.close_button = self._caption_button("✕", "close")
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def add_nav_button(self, button: QPushButton) -> None:
        self._nav_layout.addWidget(button)

    def add_utility_button(self, button: QPushButton) -> None:
        self._utility_layout.addWidget(button)

    def add_status_pill(self, pill: QWidget) -> None:
        """Mount the inline status notification pill in the title bar.

        The pill sits in the empty horizontal band between the nav tabs
        and the utility cluster (language + min/max/close), centered by
        the surrounding ``addStretch(1)`` calls in ``__init__``.
        """
        layout = self.layout()
        if self._status_pill is not None and self._status_pill is not pill:
            self._status_pill.setParent(None)
        self._status_pill = pill
        pill.setParent(self)
        layout.insertWidget(self._status_slot_index, pill, 0, Qt.AlignVCenter)

    def _caption_button(self, text: str, kind: str = "default") -> QPushButton:
        button = CaptionButton(kind)
        button.setFixedSize(42, 32)
        return button

    def _toggle_maximized(self) -> None:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is not None and event.buttons() & Qt.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _ElidedLabel(QLabel):
    """QLabel that elides its text with a right ellipsis when its
    rendered width is too small to fit the full string. Tracks the
    original text separately so the elision recomputes cleanly on
    every resize. Used by StatusBanner so long status messages stay
    on a single visible line (the full text lives in the tooltip and,
    when extra detail is available, the details popover)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._full_text = ""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = text or ""
        self._sync_elided()

    def fullText(self) -> str:  # noqa: N802 - Qt naming
        return self._full_text

    def text(self) -> str:
        return self._full_text

    def clear(self) -> None:
        self._full_text = ""
        super().clear()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._sync_elided()

    def _sync_elided(self) -> None:
        if not self._full_text:
            super().setText("")
            return
        metrics = self.fontMetrics()
        # Subtract a small slack so wrapped layouts (e.g. label inside
        # a pill with extra controls) don't fight the ellipsis on the
        # last pixel.
        avail = max(0, self.width() - 2)
        super().setText(metrics.elidedText(self._full_text, Qt.ElideRight, avail))


class StatusBanner(QFrame):
    """A pill-shaped notification row with severity-driven color + icon."""

    dismiss_requested = Signal()
    _ICON_TEXT = {"success": "✓", "info": "i", "warning": "!", "error": "✕"}

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatusBanner")
        self.setProperty("severity", "info")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 10, 9)
        layout.setSpacing(10)
        self._icon = QLabel(self)
        self._icon.setObjectName("StatusIcon")
        self._icon.setProperty("severity", "info")
        self._icon.setFixedSize(22, 22)
        self._icon.setAlignment(Qt.AlignCenter)
        self._icon.setAttribute(Qt.WA_StyledBackground, True)
        self._icon.setAutoFillBackground(False)
        # ``_ElidedLabel`` keeps the message on a single line and
        # appends an ellipsis when the available pill width is shorter
        # than the message's natural width. The full text remains
        # accessible via tooltip + the details popover.
        self._text = _ElidedLabel(self)
        self._text.setObjectName("StatusText")
        self._text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._text.setMinimumWidth(0)
        self._text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._icon, 0, Qt.AlignVCenter)
        layout.addWidget(self._text, 1, Qt.AlignVCenter)
        for child in (self._icon, self._text):
            child.installEventFilter(self)
        self.hide()

    def set_message(self, text: str, severity: str = "info") -> None:
        if not text:
            self.hide()
            self._text.clear()
            return
        if severity not in self._ICON_TEXT:
            severity = "info"
        self.setProperty("severity", severity)
        self._icon.setProperty("severity", severity)
        self._icon.setText(self._ICON_TEXT[severity])
        self._text.setText(text)
        for widget in (self, self._icon, self._text):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.show()

    def text(self) -> str:
        return self._text.text()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        del watched
        if event.type() == QEvent.Type.MouseButtonDblClick:
            button = getattr(event, "button", lambda: None)()
            if button == Qt.LeftButton:
                self.dismiss_requested.emit()
                return True
        return False

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.dismiss_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class StatusPopupFrame(_FrostedSurface):
    """Lean toast surface, matching the original narrow notification style.

    Two construction modes via ``as_window``:

    * ``False`` (default) — embedded child of the title bar. Renders the
      pill in the chrome row; ``_FrostedSurface`` switches its blit to
      ``CompositionMode_SourceOver`` and relies on the ``QFrame#StatusFooter``
      QSS rule (``background: transparent``) to paint through the rounded
      corner ears.
    * ``True`` — standalone top-level toast window with real HWND alpha.

    Severity dispatch is per-instance via ``self.property("severity")``;
    ``_resolve_palette`` looks up ``_SURFACES`` so callers can swap
    severity at runtime with ``setProperty`` and ``style().polish()``.
    """

    ABSORB_DOUBLE_CLICK = True

    _SURFACES = {
        "success": (QColor(18, 54, 44, 222), QColor(48, 209, 88, 178), QColor(48, 209, 88, 32)),
        "info": (QColor(18, 39, 54, 222), QColor(10, 132, 255, 165), QColor(10, 132, 255, 30)),
        "warning": (QColor(54, 46, 20, 226), QColor(255, 214, 10, 178), QColor(255, 214, 10, 28)),
        "error": (QColor(54, 27, 26, 226), QColor(255, 69, 58, 185), QColor(255, 69, 58, 30)),
    }

    def __init__(self, parent: QWidget | None = None, *, as_window: bool = False):
        super().__init__(parent, as_window=as_window)

    def _resolve_palette(self) -> tuple[QColor, QColor, QColor]:
        severity = str(self.property("severity") or "info")
        return self._SURFACES.get(severity, self._SURFACES["info"])


class StatusDetailsPopup(StatusPopupFrame):
    """Frosted-glass dropdown anchored under the title-bar status pill,
    exposing the full cleaned message text. Mirrors the search/filter
    popup pattern from sessions_page (top-level Qt.Tool window with a
    custom paintEvent) so all three frosted popovers feel like the
    same UI primitive. ESC and click-outside dismiss."""

    ACCEPT_FOCUS = True
    DISMISS_ON_ESCAPE = True
    DISMISS_ON_DEACTIVATE = True
    ABSORB_DOUBLE_CLICK = False

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, as_window=True)
        self.setProperty("severity", "info")


class QuotaRingWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.label = "--"
        self.percent: float | None = None
        self.caption = "remaining"
        self.setFixedSize(86, 106)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_quota(self, label: str, percent: float | None, caption: str = "remaining") -> None:
        self.label = label.upper()
        self.percent = min(max(percent, 0.0), 100.0) if percent is not None else None
        self.caption = caption
        self.update()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 86 width -> (86 - 64) / 2 = 11 left/right
        # 106 height -> ring from top=8, size 64 -> bottom=72
        ring_rect = self.rect().adjusted(11, 8, -11, -34)

        base_pen = QPen(QColor(255, 255, 255, 28), 5)
        base_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(ring_rect, 0, 360 * 16)

        percent = self.percent
        if percent is not None:
            accent = QColor("#30D158" if percent >= 70 else "#FFD60A" if percent >= 35 else "#FF453A")
            accent_pen = QPen(accent, 5)
            accent_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(accent_pen)
            painter.drawArc(ring_rect, 90 * 16, int(-360 * 16 * (percent / 100.0)))

        # Label: "5H" (inside ring, top)
        painter.setPen(QColor(235, 235, 245, 170))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect().adjusted(11, 16, -11, -70), Qt.AlignCenter, self.label)

        # Value: "100%" (inside ring, center)
        painter.setPen(QColor(255, 255, 255))
        font.setPointSize(13)
        font.setBold(True)
        painter.setFont(font)
        value = "--" if percent is None else f"{percent:.0f}%"
        painter.drawText(self.rect().adjusted(11, 38, -11, -44), Qt.AlignCenter, value)

        # Caption: "remaining" (below ring)
        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(235, 235, 245, 120))
        painter.drawText(self.rect().adjusted(0, 80, 0, -6), Qt.AlignHCenter | Qt.AlignTop, self.caption)


def _install_dialog_chrome(window, *, disable_border: bool = False) -> None:
    """Win11 rounded corners for frameless QDialog windows. No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        hwnd = wintypes.HWND(int(window.winId()))
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if disable_border:
            DWMWA_BORDER_COLOR = 34
            DWMWA_COLOR_NONE = 0xFFFFFFFE
            border = ctypes.c_uint(DWMWA_COLOR_NONE)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_int(DWMWA_BORDER_COLOR),
                ctypes.byref(border),
                ctypes.sizeof(border),
            )
    except Exception:
        pass


class FramelessDialog(QDialog):
    """Base dialog with the same dark, frameless chrome as MainWindow.

    Provides a custom title bar (title text + close button), a draggable
    title area, DWM rounded corners, and a fade-in entry animation."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("FramelessDialog")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._drag_start: QPoint | None = None
        self._title_text = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._title_bar = QFrame(self)
        self._title_bar.setObjectName("DialogTitleBar")
        self._title_bar.setFixedHeight(40)
        title_layout = QHBoxLayout(self._title_bar)
        title_layout.setContentsMargins(18, 0, 6, 0)
        title_layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("DialogTitle")
        title_layout.addWidget(title_label)
        title_layout.addStretch(1)
        close_btn = CaptionButton("close")
        close_btn.setFixedSize(40, 28)
        close_btn.clicked.connect(self.reject)
        title_layout.addWidget(close_btn)
        self._title_bar.installEventFilter(self)
        outer.addWidget(self._title_bar)

        self._body = QFrame(self)
        self._body.setObjectName("DialogBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(22, 16, 22, 18)
        self._body_layout.setSpacing(12)
        outer.addWidget(self._body, 1)

    def body(self) -> QVBoxLayout:
        return self._body_layout

    def eventFilter(self, obj, event):
        if obj is self._title_bar:
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return False
            if etype == QEvent.MouseMove and event.buttons() & Qt.LeftButton and self._drag_start is not None:
                self.move(event.globalPosition().toPoint() - self._drag_start)
                return False
            if etype == QEvent.MouseButtonRelease:
                self._drag_start = None
                return False
            if etype == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                # No maximize for dialogs; consume to avoid odd behavior.
                return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        _install_dialog_chrome(self)
        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(170)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QAbstractAnimation.DeleteWhenStopped)


class ApiAccountDialog(FramelessDialog):
    def __init__(self, parent: QWidget | None = None, translator: Callable[[str], str] | None = None):
        self._tr = translator or (lambda text: text)
        super().__init__(self._tr("Add API Account Title"), parent)
        self.setMinimumWidth(620)
        layout = self.body()
        form = QGridLayout()
        layout.addLayout(form)

        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText(self._tr("Display name (optional)"))
        self.base_url = QLineEdit("https://api.openai.com/v1")
        self.model = QLineEdit()
        self.model.setPlaceholderText(self._tr("Model (optional)"))
        self.api_key = QLineEdit()
        self.api_key.setPlaceholderText(self._tr("API key placeholder"))
        self.api_key.setEchoMode(QLineEdit.Password)

        for row, (label, field) in enumerate(
            [
                ("Display name", self.display_name),
                ("Base URL", self.base_url),
                ("Model", self.model),
                ("API key", self.api_key),
            ]
        ):
            form.addWidget(QLabel(self._tr(label)), row, 0)
            form.addWidget(field, row, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self._tr("Add"))
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self.base_url.text().strip() or not self.api_key.text().strip():
            QMessageBox.warning(self, self._tr("Add API Account Title"), self._tr("API key and Base URL are required."))
            return
        self.accept()

    def values(self) -> tuple[str, str, str, str]:
        return (
            self.api_key.text(),
            self.base_url.text(),
            self.display_name.text(),
            self.model.text(),
        )


class ConfirmPhraseDialog(FramelessDialog):
    def __init__(self, title: str, body: str, phrase: str, parent: QWidget | None = None):
        super().__init__(title, parent)
        self.phrase = phrase
        self.setMinimumSize(720, 380)
        layout = self.body()
        body_box = QTextEdit()
        body_box.setReadOnly(True)
        body_box.setPlainText(body)
        layout.addWidget(body_box)
        instruction = QLabel("Type the exact confirmation phrase below to continue:")
        layout.addWidget(instruction)
        phrase_label = QLabel(phrase)
        phrase_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        phrase_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 15px; padding: 8px; border: 1px solid rgba(255, 255, 255, 38); background: rgba(255, 255, 255, 13); color: #ffffff; border-radius: 6px;"
        )
        layout.addWidget(phrase_label)
        self.input = QLineEdit()
        self.input.setPlaceholderText(f"Type exactly: {phrase}")
        layout.addWidget(self.input)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Confirm")
        self.ok_button.setEnabled(False)
        self.input.textChanged.connect(lambda text: self.ok_button.setEnabled(self._matches_phrase(text)))
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.input.setFocus()

    def _validate(self) -> None:
        if not self._matches_phrase(self.input.text()):
            QMessageBox.warning(self, self.windowTitle(), f"Type the exact phrase: {self.phrase}")
            return
        self.accept()

    def _matches_phrase(self, value: str) -> bool:
        return self._normalize_phrase(value) == self._normalize_phrase(self.phrase)

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return " ".join(value.strip().split())


class MainWindow(QMainWindow):
    # Inline pill geometry (lives in the title bar). The width band is
    # sized so the pill reads coherently with the 3 nav-tab buttons
    # (~210px total) — wider than nav (carries a sentence, not a label)
    # but capped well below the empty band (~900px at 1280px window)
    # so the pill never dominates the title bar. Long messages elide
    # with ellipsis at the visible width; the details popover (anchored
    # below the pill) exposes the full text.
    _STATUS_PILL_MIN_WIDTH = 320
    _STATUS_PILL_MAX_WIDTH = 520
    _STATUS_PILL_HEIGHT = 40
    _STATUS_AUTO_HIDE_MS = {
        "success": 7_000,
        "info": 9_000,
        "warning": 12_000,
        "error": 15_000,
    }

    def __init__(self, services: AppServices):
        super().__init__()
        self.services = services
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(max(4, self.thread_pool.maxThreadCount()))
        self._workers: set[Worker] = set()
        self._process_tasks: dict[QProcess, dict[str, Any]] = {}
        self._locked_action_keys: set[str] = set()
        self.current_view = "Accounts"
        self.accounts_quota_label: QLabel | None = None
        self.quota_ring_widgets: list[QuotaRingWidget] = []
        self._latest_quota_snapshot: CodexSnapshot | None = None
        self.tray_icon: QSystemTrayIcon | None = None
        self._language = self.services.ui_language
        self._snapshot_target = self.services.active_target
        self._quota_inflight = False
        self._quota_pending_force = False
        self._quota_epoch = 0
        self._settings_epoch = 0
        self._sync_sessions_to_real = True
        self._sync_assets_to_real = True
        self._restore_points_table_height = 286
        self._audit_table_height = 224
        self._fade_animation: QPropertyAnimation | None = None
        self._resize_material_suspended = False
        self.quota_timer = QTimer(self)
        self.quota_timer.setInterval(30_000)
        self.quota_timer.timeout.connect(self._auto_refresh_quota)
        self._status_auto_hide_timer = QTimer(self)
        self._status_auto_hide_timer.setSingleShot(True)
        self._status_auto_hide_timer.timeout.connect(self.clear_status)

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(_asset_icon(APP_ICON_ASSET))
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMinimumSize(1100, 680)
        self.resize(1280, 760)
        self._resize_border = 10
        self._build_shell()
        self.show_accounts()
        self.quota_timer.start()

    def nativeEvent(self, eventType, message):
        if sys.platform == "win32" and eventType in ("windows_generic_MSG", b"windows_generic_MSG"):
            try:
                import ctypes
                import ctypes.wintypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                WM_NCCALCSIZE = 0x0083
                WM_NCHITTEST = 0x0084
                WM_ENTERSIZEMOVE = 0x0231
                WM_EXITSIZEMOVE = 0x0232
                if msg.message == WM_ENTERSIZEMOVE:
                    self._set_resize_material_suspended(True)
                elif msg.message == WM_EXITSIZEMOVE:
                    self._set_resize_material_suspended(False)
                if msg.message == WM_NCCALCSIZE and msg.wParam:
                    return True, 0
                if msg.message == WM_NCHITTEST and not (self.isMaximized() or self.isFullScreen()):
                    pos = self.mapFromGlobal(QCursor.pos())
                    border = self._resize_border
                    w, h = self.width(), self.height()
                    on_left = 0 <= pos.x() < border
                    on_right = w - border <= pos.x() <= w
                    on_top = 0 <= pos.y() < border
                    on_bottom = h - border <= pos.y() <= h
                    HTTOPLEFT, HTTOP, HTTOPRIGHT = 13, 12, 14
                    HTLEFT, HTRIGHT = 10, 11
                    HTBOTTOMLEFT, HTBOTTOM, HTBOTTOMRIGHT = 16, 15, 17
                    if on_top and on_left:
                        return True, HTTOPLEFT
                    if on_top and on_right:
                        return True, HTTOPRIGHT
                    if on_bottom and on_left:
                        return True, HTBOTTOMLEFT
                    if on_bottom and on_right:
                        return True, HTBOTTOMRIGHT
                    if on_top:
                        return True, HTTOP
                    if on_bottom:
                        return True, HTBOTTOM
                    if on_left:
                        return True, HTLEFT
                    if on_right:
                        return True, HTRIGHT
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _set_resize_material_suspended(self, suspended: bool) -> None:
        if self._resize_material_suspended == suspended:
            return
        self._resize_material_suspended = suspended
        if suspended:
            _install_acrylic_blur(self, enabled=False)
        else:
            QTimer.singleShot(80, lambda: _install_acrylic_blur(self, enabled=True))

    def set_tray_icon(self, tray_icon: QSystemTrayIcon) -> None:
        self.tray_icon = tray_icon

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "status_footer"):
            self.status_footer.hide()
        if self.tray_icon and self.tray_icon.isVisible():
            self.hide()
            event.ignore()
            return
        event.accept()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32":
            QTimer.singleShot(0, self._restore_native_window_chrome)
        self._position_status_popup()

    def _restore_native_window_chrome(self) -> None:
        _install_native_window_chrome(self)
        _install_acrylic_blur(self, enabled=not self._resize_material_suspended)

    def _restore_native_window_chrome_after_popup(self) -> None:
        if sys.platform != "win32":
            return
        QTimer.singleShot(0, self._restore_native_window_chrome)
        QTimer.singleShot(120, self._restore_native_window_chrome)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._position_status_popup()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_status_popup()

    def _build_shell(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        root.setAttribute(Qt.WA_StyledBackground, True)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        title_bar = TitleBar(self)
        self.title_bar = title_bar
        self.nav_buttons: dict[str, QPushButton] = {}
        for name in ["Accounts", "Sessions", "Settings"]:
            button = QPushButton(self._tr(name))
            button.setObjectName("NavButton")
            button.setProperty("viewKey", name)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(40)
            button.setToolTip(self._tr(f"{name} view tooltip"))
            button.setToolTipDuration(12_000)
            button.clicked.connect(lambda checked=False, view=name: self.navigate(view))
            self.nav_buttons[name] = button
            title_bar.add_nav_button(button)
        self.language_button = QPushButton()
        self.language_button.setObjectName("LanguageButton")
        self.language_button.setIcon(_globe_icon())
        self.language_button.setIconSize(QSize(20, 20))
        self.language_button.setFixedSize(42, 32)
        self.language_button.setCursor(Qt.PointingHandCursor)
        self.language_button.setToolTip(self._tr("Switch language tooltip"))
        self.language_button.setToolTipDuration(12_000)
        self.language_button.clicked.connect(self._show_language_menu)
        title_bar.add_utility_button(self.language_button)
        root_layout.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("Content")
        content.setAttribute(Qt.WA_StyledBackground, True)
        self.content = content
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 18)
        content_layout.setSpacing(14)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("MainScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setAttribute(Qt.WA_StyledBackground, True)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.body = QWidget()
        self.body.setObjectName("Body")
        self.body.setAttribute(Qt.WA_StyledBackground, True)
        self.body.setAutoFillBackground(False)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setAlignment(Qt.AlignTop)
        self.body_layout.setSpacing(14)
        self.body_layout.setContentsMargins(0, 0, 8, 0)
        self.scroll.setWidget(self.body)
        content_layout.addWidget(self.scroll, 1)

        # Inline status pill — lives in the title bar's empty band between
        # the nav tabs and the utility cluster (language + min/max/close).
        # Three controls:
        #   1. ``status_banner`` — severity icon + elided one-line text
        #   2. ``details_button`` (▾) — opens ``status_details_popup``
        #      with the full cleaned text; visible only when the cleaned
        #      text contains more than the one-line summary
        #   3. ``status_close_button`` (×) — explicit dismiss, replaces
        #      the older double-click-to-dismiss UX (which collided with
        #      the title bar's double-click-to-maximize handler).
        self.status_banner = StatusBanner()
        self.status_banner.setToolTip(self._tr("Status notification tooltip"))
        self.status_banner.setToolTipDuration(12_000)
        self.status_banner.dismiss_requested.connect(self.clear_status)

        self.details_button = QPushButton("▾")
        self.details_button.setObjectName("StatusDetailsButton")
        self.details_button.setCheckable(True)
        self.details_button.setCursor(Qt.PointingHandCursor)
        self.details_button.setFlat(True)
        self.details_button.setFocusPolicy(Qt.NoFocus)
        self.details_button.setFixedSize(24, 24)
        self.details_button.setToolTip(self._tr("Details tooltip"))
        self.details_button.setToolTipDuration(12_000)
        self.details_button.toggled.connect(self._toggle_details)
        self.details_button.hide()

        self.status_close_button = QPushButton("✕")
        self.status_close_button.setObjectName("StatusCloseButton")
        self.status_close_button.setCursor(Qt.PointingHandCursor)
        self.status_close_button.setFlat(True)
        self.status_close_button.setFocusPolicy(Qt.NoFocus)
        self.status_close_button.setFixedSize(24, 24)
        self.status_close_button.setToolTip(self._tr("Dismiss tooltip"))
        self.status_close_button.setToolTipDuration(12_000)
        self.status_close_button.clicked.connect(self.clear_status)

        self.status_footer = StatusPopupFrame(title_bar, as_window=False)
        self.status_footer.setObjectName("StatusFooter")
        self.status_footer.setProperty("severity", "info")
        self.status_footer.setMinimumWidth(self._STATUS_PILL_MIN_WIDTH)
        self.status_footer.setMaximumWidth(self._STATUS_PILL_MAX_WIDTH)
        self.status_footer.setFixedHeight(self._STATUS_PILL_HEIGHT)

        status_layout = QHBoxLayout(self.status_footer)
        status_layout.setContentsMargins(0, 0, 8, 0)
        status_layout.setSpacing(2)
        status_layout.addWidget(self.status_banner, 1)
        status_layout.addWidget(self.details_button, 0, Qt.AlignVCenter)
        status_layout.addWidget(self.status_close_button, 0, Qt.AlignVCenter)

        # Details popover — a frosted-glass dropdown anchored under the
        # pill exposing the full cleaned message text. Hidden by
        # default; shown when ``details_button`` is checked.
        self.status_details_popup = StatusDetailsPopup(self)
        self.status_details_popup.setObjectName("StatusDetailsPopup")
        self.status_details_popup.dismiss_requested.connect(
            self._dismiss_details_popover
        )
        details_popup_layout = QVBoxLayout(self.status_details_popup)
        details_popup_layout.setContentsMargins(14, 12, 14, 12)
        details_popup_layout.setSpacing(0)
        self.details_panel = QTextEdit(self.status_details_popup)
        self.details_panel.setObjectName("DetailsPanel")
        self.details_panel.setReadOnly(True)
        self.details_panel.setFrameShape(QFrame.NoFrame)
        self.details_panel.setAttribute(Qt.WA_TranslucentBackground, True)
        self.details_panel.viewport().setAutoFillBackground(False)
        self.details_panel.viewport().setStyleSheet("background: transparent;")
        details_popup_layout.addWidget(self.details_panel)
        self.status_details_popup.hide()

        self._raw_status: str = ""
        self.status_footer.hide()
        title_bar.add_status_pill(self.status_footer)
        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)
        self._apply_style()

    def _apply_style(self) -> None:
        style = """
            /* Base ----------------------------------------------------- */
            QWidget#AppRoot {
                background: rgba(22, 25, 29, 72);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 12px;
                color: #ffffff;
                font-family: "-apple-system", "BlinkMacSystemFont", "SF Pro Text", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
                font-size: 13px;
            }
            QDialog, QMessageBox {
                background: #1e1e1e;
                color: #ffffff;
                font-family: "-apple-system", "BlinkMacSystemFont", "SF Pro Text", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
                font-size: 13px;
            }
            QDialog QLabel, QMessageBox QLabel {
                color: #ffffff;
            }
            QWidget#Content, QWidget#Body,
            QScrollArea#MainScroll, QScrollArea#MainScroll > QWidget,
            QScrollArea#MainScroll > QWidget > QWidget {
                background: transparent;
            }

            /* Title bar (single-row header) ---------------------------- */
            QFrame#TitleBar {
                background: transparent;
                border-bottom: 0;
            }

            /* Frameless dialog chrome ---------------------------------- */
            QDialog#FramelessDialog {
                background: #1e1e1e;
                border: 1px solid rgba(255, 255, 255, 26);
                border-radius: 12px;
            }
            QFrame#DialogTitleBar {
                background: #161b22;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid rgba(255, 255, 255, 18);
            }
            QFrame#DialogBody {
                background: #1e1e1e;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }
            QLabel#DialogTitle {
                color: #f0f6fc;
                font-size: 13px;
                font-weight: 600;
            }

            /* Window caption buttons ----------------------------------- */
            QPushButton#CaptionButton {
                background: transparent;
                border: 0;
                border-radius: 8px;
                color: rgba(235, 235, 245, 178);
                font-size: 15px;
                font-weight: 400;
                padding: 0;
                margin: 0;
            }
            QPushButton#CaptionButton:hover {
                background: rgba(255, 255, 255, 26);
                color: #ffffff;
            }
            QPushButton#CaptionButton[kind="close"]:hover {
                background: #e81123;
                color: #ffffff;
            }
            QPushButton#LanguageButton {
                background: transparent;
                border: 0;
                border-radius: 8px;
                padding: 0;
                margin: 0;
            }
            QPushButton#LanguageButton:hover {
                background: rgba(255, 255, 255, 26);
            }
            QPushButton#LanguageButton:pressed {
                background: rgba(255, 255, 255, 16);
            }
            QPushButton#HeaderIconButton {
                background: transparent;
                border: 0;
                border-radius: 8px;
                padding: 0;
                margin: 0;
                min-width: 36px;
                max-width: 36px;
                min-height: 36px;
                max-height: 36px;
            }
            QPushButton#HeaderIconButton:hover {
                background: rgba(255, 255, 255, 18);
            }
            QPushButton#HeaderIconButton:pressed {
                background: rgba(10, 132, 255, 35);
            }
            QPushButton#HeaderIconButton:disabled {
                background: transparent;
            }
            /* Navigation segmented tabs -------------------------------- */
            QPushButton#NavButton {
                background: transparent;
                border: 0;
                border-radius: 6px;
                color: rgba(235, 235, 245, 153);
                padding: 6px 16px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#NavButton:hover {
                background: rgba(255, 255, 255, 13);
                color: #ffffff;
            }
            QPushButton#NavButton:checked {
                background: rgba(255, 255, 255, 38);
                color: #ffffff;
                font-weight: 600;
            }

            /* Buttons -------------------------------------------------- */
            QPushButton {
                background: rgba(255, 255, 255, 26);
                border: 1px solid rgba(255, 255, 255, 26);
                border-radius: 6px;
                color: #ffffff;
                padding: 6px 14px;
                min-height: 24px;
                font-weight: 500;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 38);
                border-color: rgba(255, 255, 255, 38);
            }
            QPushButton:pressed { background: rgba(255, 255, 255, 13); }
            QPushButton:disabled {
                color: rgba(235, 235, 245, 76);
                background: rgba(255, 255, 255, 13);
                border-color: transparent;
            }
            QPushButton[accent="true"] {
                background: #0A84FF;
                border: 1px solid #0A84FF;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton[accent="true"]:hover {
                background: #409CFF;
                border-color: #409CFF;
            }
            QPushButton[accent="true"]:pressed {
                background: #006ADC;
                border-color: #006ADC;
            }
            QPushButton[danger="true"] {
                color: #FF6961;
                border-color: rgba(255, 105, 97, 102);
            }
            QPushButton[danger="true"]:hover {
                background: rgba(255, 105, 97, 26);
                border-color: rgba(255, 105, 97, 153);
            }

            /* Target Mode segmented buttons ---------------------------- */
            QPushButton#TargetButton {
                text-align: center;
                min-height: 30px;
                padding: 8px 18px;
            }
            QPushButton#TargetButton:checked {
                background: rgba(10, 132, 255, 38);
                border: 1px solid rgba(10, 132, 255, 128);
                color: #0A84FF;
                font-weight: 600;
            }

            /* Cards & panels ------------------------------------------- */
            QFrame[panel="true"], QFrame[toolbar="true"] {
                background: rgba(255, 255, 255, 12);
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 12px;
            }
            QFrame[card="true"] {
                background: rgba(255, 255, 255, 13);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 12px;
            }
            QFrame[card="true"]:hover {
                border-color: rgba(255, 255, 255, 48);
                background: rgba(255, 255, 255, 18);
            }
            QFrame[current="true"] {
                background: rgba(10, 132, 255, 24);
                border: 1px solid rgba(10, 132, 255, 130);
                border-radius: 12px;
            }

            /* Typography ----------------------------------------------- */
            QLabel#PageTitle {
                font-size: 26px;
                font-weight: 700;
                color: #ffffff;
                padding: 2px 0 4px 0;
            }
            QLabel#SectionTitle {
                font-size: 14px;
                font-weight: 600;
                color: #ffffff;
                padding-top: 2px;
            }
            QLabel#Muted {
                color: rgba(235, 235, 245, 153);
                font-size: 12px;
            }
            QLabel#AccountName {
                font-size: 14px;
                font-weight: 600;
                color: #ffffff;
            }
            QWidget#AccountDetails {
                background: transparent;
                border: 0;
            }
            QLabel#AccountIcon {
                background: rgba(255, 255, 255, 26);
                border: 1px solid rgba(255, 255, 255, 38);
                border-radius: 22px;
                color: #ffffff;
                font-weight: 700;
                min-width: 44px; max-width: 44px;
                min-height: 44px; max-height: 44px;
            }
            QLabel#SettingsLine {
                color: #ffffff;
                font-weight: 500;
            }
            QLabel#WarningLine {
                color: #FFD60A;
                font-size: 12px;
            }
            QCheckBox#SyncOption {
                color: #ffffff;
                font-size: 13px;
                font-weight: 500;
                spacing: 8px;
                padding: 3px 0;
            }
            QCheckBox#SyncOption:disabled {
                color: rgba(235, 235, 245, 76);
            }
            QToolTip {
                background: #20252b;
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 46);
                border-radius: 0;
                padding: 8px 10px;
                font-size: 12px;
            }
            /* Status banner (severity-driven notification) ------------ */
            /* The pill (#StatusFooter) lives inline in the title bar.
               Frame chrome is painted by StatusPopupFrame.paintEvent;
               the QSS rule keeps the frame transparent so the painted
               surface shows through. */
            QFrame#StatusFooter {
                background: transparent;
                border-radius: 0;
                border: 0;
            }
            QFrame#StatusBanner {
                background: transparent;
                border: 0;
            }
            QLabel#StatusText {
                color: #ffffff;
                font-size: 13px;
                background: transparent;
                border: 0;
            }
            QLabel#StatusIcon {
                color: #ffffff;
                background: rgba(255, 255, 255, 80);
                border-radius: 11px;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#StatusIcon[severity="success"] { background: #30D158; }
            QLabel#StatusIcon[severity="info"] { background: #0A84FF; }
            QLabel#StatusIcon[severity="warning"] { background: #FFD60A; color: #14181f; }
            QLabel#StatusIcon[severity="error"] { background: #FF453A; }
            /* Pill-end controls: small flat buttons that share the
               same neutral-on-hover, brighter-on-active rhythm. The
               details chevron uses ``[checked]`` to show the popover
               is open; the close button always reads as a dismiss. */
            QPushButton#StatusDetailsButton,
            QPushButton#StatusCloseButton {
                background: transparent;
                border: 0;
                border-radius: 6px;
                color: rgba(235, 235, 245, 153);
                font-size: 13px;
                padding: 0;
            }
            QPushButton#StatusDetailsButton:hover,
            QPushButton#StatusCloseButton:hover {
                background: rgba(255, 255, 255, 28);
                color: #ffffff;
            }
            QPushButton#StatusDetailsButton:pressed,
            QPushButton#StatusCloseButton:pressed {
                background: rgba(255, 255, 255, 46);
                color: #ffffff;
            }
            QPushButton#StatusDetailsButton:checked {
                background: rgba(10, 132, 255, 64);
                color: #ffffff;
            }
            QPushButton#StatusDetailsButton:disabled {
                color: rgba(235, 235, 245, 60);
            }
            QFrame#StatusDetailsPopup {
                background: transparent;
                border: 0;
            }
            QTextEdit#DetailsPanel {
                background: transparent;
                border: 0;
                color: rgba(235, 235, 245, 220);
                font-family: "Consolas", "SF Mono", "JetBrains Mono", monospace;
                font-size: 12px;
                selection-background-color: rgba(10, 132, 255, 130);
            }

            /* Inputs --------------------------------------------------- */
            QLineEdit, QComboBox, QTextEdit {
                background: rgba(255, 255, 255, 13);
                border: 1px solid rgba(255, 255, 255, 38);
                border-radius: 6px;
                color: #ffffff;
                padding: 6px 10px;
                selection-background-color: #0A84FF;
                selection-color: #ffffff;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border-color: #0A84FF;
                background: rgba(255, 255, 255, 20);
            }

            /* Tables --------------------------------------------------- */
            QFrame#RestoreTableFrame {
                background: rgba(255, 255, 255, 7);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 12px;
            }
            QTableWidget, QTableView {
                background: rgba(255, 255, 255, 5);
                border: 1px solid rgba(255, 255, 255, 20);
                border-radius: 10px;
                gridline-color: rgba(255, 255, 255, 13);
                color: #ffffff;
                alternate-background-color: rgba(255, 255, 255, 5);
                outline: 0;
            }
            QTableWidget::item, QTableView::item {
                padding: 7px 10px;
                border: 0;
            }
            QTableWidget::item:selected, QTableView::item:selected {
                background: rgba(10, 132, 255, 82);
                color: #ffffff;
            }
            QHeaderView::section {
                background: #2C2C2E;
                color: rgba(235, 235, 245, 153);
                border: 0;
                border-bottom: 1px solid rgba(255, 255, 255, 20);
                padding: 8px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QTableWidget#RestorePointsTable {
                background: transparent;
                border: 0;
                border-radius: 12px;
            }
            QTableWidget#RestorePointsTable::item {
                background: rgba(255, 255, 255, 4);
                border: 0;
            }
            QTableWidget#RestorePointsTable::item:selected {
                background: rgba(10, 132, 255, 95);
                color: #ffffff;
            }
            QHeaderView#RestorePointsHeader {
                background: transparent;
                border: 0;
            }
            QTableWidget#RestorePointsTable QHeaderView::section {
                background: rgba(255, 255, 255, 8);
            }
            QTableWidget#RestorePointsTable QHeaderView::section:first {
                border-top-left-radius: 10px;
            }
            QTableWidget#RestorePointsTable QHeaderView::section:last {
                border-top-right-radius: 10px;
            }
            QTableWidget#RestorePointsTable QTableCornerButton::section {
                background: transparent;
                border: 0;
                border-bottom: 1px solid rgba(255, 255, 255, 20);
                border-top-left-radius: 10px;
            }
            QTableCornerButton::section {
                background: #2C2C2E;
                border: 0;
                border-bottom: 1px solid rgba(255, 255, 255, 20);
            }

            /* Scrollbars ----------------------------------------------- */
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 38);
                border-radius: 4px;
                min-height: 40px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 76);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """
        # Token-substitute the "selected/current" rules so the highlight reads
        # consistently across NavButton, QFrame[current=true], and the two
        # selected-row table styles. See design_tokens.py for the full
        # palette; only six rules are migrated here — the rest of the
        # stylesheet is intentionally left as raw rgba for stage 2.
        style = (
            style
            .replace("rgba(10, 132, 255, 38)", PRIMARY_GHOST, 1)
            .replace("rgba(10, 132, 255, 128)", PRIMARY_BAND, 1)
            .replace("rgba(10, 132, 255, 24)", PRIMARY_GHOST, 1)
            .replace("rgba(10, 132, 255, 130)", PRIMARY_BAND, 1)
            .replace("rgba(10, 132, 255, 82)", PRIMARY_SOFT, 1)
            .replace("rgba(10, 132, 255, 95)", PRIMARY_SOFT, 1)
        )
        self.setStyleSheet(style)
        if QApplication.instance():
            QApplication.instance().setStyleSheet(style)

    def _tr(self, key: str, **kwargs: Any) -> str:
        text = translate(getattr(self, "_language", UiLanguage.ENGLISH), key)
        return text.format(**kwargs) if kwargs else text

    def _target_label(self, target: CodexHomeTarget | str) -> str:
        value = target.value if isinstance(target, CodexHomeTarget) else str(target)
        return self._tr(value)

    def _show_language_menu(self) -> None:
        menu = QMenu(self)
        english = QAction(self._tr("English"), menu)
        chinese = QAction(self._tr("Simplified Chinese"), menu)
        english.setCheckable(True)
        chinese.setCheckable(True)
        english.setChecked(self._language == UiLanguage.ENGLISH)
        chinese.setChecked(self._language == UiLanguage.CHINESE)
        english.triggered.connect(lambda checked=False: self._set_language(UiLanguage.ENGLISH))
        chinese.triggered.connect(lambda checked=False: self._set_language(UiLanguage.CHINESE))
        menu.addAction(english)
        menu.addAction(chinese)
        button = getattr(self, "language_button", None)
        if button is None:
            menu.exec(QCursor.pos())
            return
        menu.exec(button.mapToGlobal(QPoint(0, button.height() + 4)))

    def _set_language(self, language: UiLanguage) -> None:
        if language == self._language:
            return
        self._language = language
        self.services.set_ui_language(language)
        self._refresh_chrome_text()
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        elif self.current_view == "Sessions":
            self.show_sessions()
        else:
            self.show_accounts(refresh_quota=False)
        self.set_status(self._tr("Language changed."), "success")

    def _refresh_chrome_text(self) -> None:
        for key, button in self.nav_buttons.items():
            button.setText(self._tr(key))
            button.setToolTip(self._tr(f"{key} view tooltip"))
        if hasattr(self, "language_button"):
            self.language_button.setToolTip(self._tr("Switch language tooltip"))
        if hasattr(self, "status_banner"):
            self.status_banner.setToolTip(self._tr("Status notification tooltip"))

    def navigate(self, view: str) -> None:
        transitioning = view != self.current_view
        if view == "Accounts":
            self.show_accounts()
        elif view == "Sessions":
            self.show_sessions()
        else:
            self.show_settings()
        if transitioning:
            self._fade_in_body()

    def _fade_in_body(self) -> None:
        if self._fade_animation is not None and self._fade_animation.state() == QAbstractAnimation.Running:
            self._fade_animation.stop()
        effect = QGraphicsOpacityEffect(self.body)
        effect.setOpacity(0.0)
        self.body.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(160)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda body=self.body: body.setGraphicsEffect(None))
        self._fade_animation = anim
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _clear(self, heading: str, heading_actions: list[QWidget] | None = None) -> None:
        self.accounts_quota_label = None
        self.quota_ring_widgets = []
        preserved = getattr(self, "_sessions_page_widget", None)
        body = getattr(self, "body", None)
        if body is not None:
            self._hide_combo_popups(body)
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if preserved is not None and item is not None and item.widget() is preserved:
                self._hide_combo_popups(preserved)
                preserved.hide()
                # NOTE: do NOT call preserved.setParent(None). takeAt() already
                # removes the widget from the layout without changing its parent
                # (the widget stays a child of `body`). Calling setParent(None)
                # briefly demotes the page to a top-level window — Windows then
                # draws default OS chrome (titlebar + app icon) for the new HWND
                # before Qt re-attaches it. That OS chrome is the small white
                # popup users saw flash mid-screen during page switches.
                continue
            self._delete_layout_item(item)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == self.current_view)
        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(12)
        heading_label = QLabel(self._tr(heading))
        heading_label.setObjectName("PageTitle")
        heading_row.addWidget(heading_label, 0, Qt.AlignVCenter)
        for action in heading_actions or []:
            heading_row.addWidget(action, 0, Qt.AlignVCenter)
            action.show()
        heading_row.addStretch(1)
        self.body_layout.addLayout(heading_row)

    def _main_scroll_value(self) -> int:
        if not hasattr(self, "scroll"):
            return 0
        return self.scroll.verticalScrollBar().value()

    def _restore_main_scroll_later(self, value: int) -> None:
        if not hasattr(self, "scroll"):
            return

        def restore() -> None:
            scrollbar = self.scroll.verticalScrollBar()
            scrollbar.setValue(max(scrollbar.minimum(), min(value, scrollbar.maximum())))

        QTimer.singleShot(0, restore)
        QTimer.singleShot(50, restore)

    def _show_settings_preserving_scroll(
        self,
        settings_data: SettingsData | None = None,
        scroll_value: int | None = None,
    ) -> None:
        value = self._main_scroll_value() if scroll_value is None else scroll_value
        self.show_settings(settings_data=settings_data)
        self._restore_main_scroll_later(value)

    def _refresh_current_view_after_data_change(self) -> None:
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        elif self.current_view == "Sessions":
            self.show_sessions()
        else:
            self.show_accounts()

    def _delete_layout_item(self, item: Any) -> None:
        widget = item.widget()
        if widget:
            self._hide_combo_popups(widget)
            widget.hide()
            if widget.property("preserveOnClear"):
                return
            # On Windows, calling `setParent(None)` to detach a widget before
            # deleteLater would temporarily promote it to a top-level window.
            # Qt then asks Windows for a new HWND with default chrome
            # (titlebar + app icon). Even though we have already hidden the
            # widget, that fresh HWND flashes for a frame or two before
            # deleteLater actually destroys it — that flash is the small
            # white popup with the app icon users saw on every page switch
            # and even on re-clicking the same nav button.
            #
            # WA_DontShowOnScreen tells Qt the widget will never be visible,
            # so Qt skips the native-window creation step entirely during
            # the upcoming setParent(None). The widget is still detached
            # from its old parent and scheduled for deletion exactly as
            # before, just without the native flash.
            widget.setAttribute(Qt.WA_DontShowOnScreen, True)
            widget.setParent(None)
            widget.deleteLater()
            return
        layout = item.layout()
        if layout:
            while layout.count():
                child = layout.takeAt(0)
                self._delete_layout_item(child)
            layout.deleteLater()

    def _hide_combo_popups(self, widget: QWidget) -> None:
        if isinstance(widget, QComboBox):
            widget.hidePopup()
        for combo in widget.findChildren(QComboBox):
            combo.hidePopup()

    def _position_status_popup(self) -> None:
        """Show / hide the inline title-bar status pill.

        Geometry is owned by ``TitleBar``'s QHBoxLayout (the pill's
        min/max width and fixed height are set on the widget itself), so
        this method no longer computes positions manually — it just
        ensures the pill is raised above sibling widgets when visible
        and re-anchors the details popover (if open) so it tracks the
        pill on window move/resize. Kept under the original name so
        existing call sites don't change.
        """
        footer = getattr(self, "status_footer", None)
        if footer is None:
            return
        if footer.isVisible():
            footer.raise_()
        popup = getattr(self, "status_details_popup", None)
        if popup is not None and popup.isVisible():
            self._position_details_popover()

    def set_status(self, text: str, severity: str | None = None) -> None:
        cleaned = _strip_ansi(text or "")
        summary = _summarize_status(cleaned)
        if not summary:
            self.clear_status()
            return
        self._raw_status = cleaned
        kind = severity or _infer_severity(summary)
        self.status_banner.set_message(summary, kind)
        if hasattr(self, "status_footer"):
            self.status_footer.setProperty("severity", kind)
            self.status_footer.style().unpolish(self.status_footer)
            self.status_footer.style().polish(self.status_footer)
            self.status_footer.update()
            # Tooltip carries the full cleaned text so users can read
            # the long form on hover even when ellipsis truncates it.
            self.status_banner.setToolTip(cleaned or summary)
            # Details popover state: show the chevron only when the
            # cleaned text has more than the one-line summary; refresh
            # the panel contents so an already-open popover updates in
            # place.
            has_extra = bool(cleaned) and cleaned != summary
            if hasattr(self, "details_button"):
                self.details_button.setEnabled(has_extra)
                self.details_button.setVisible(has_extra)
                if not has_extra and self.details_button.isChecked():
                    was_blocked = self.details_button.blockSignals(True)
                    self.details_button.setChecked(False)
                    self.details_button.blockSignals(was_blocked)
                    if hasattr(self, "status_details_popup"):
                        self.status_details_popup.hide()
            if hasattr(self, "details_panel"):
                self.details_panel.setPlainText(cleaned)
            if hasattr(self, "status_details_popup"):
                self.status_details_popup.setProperty("severity", kind)
                self.status_details_popup.update()
            self.status_footer.setVisible(True)
            self._position_status_popup()
        if self.tray_icon:
            self.tray_icon.setToolTip(_trim_tray_text(APP_DISPLAY_NAME + ": " + summary))
        self._schedule_status_auto_hide(kind)

    def clear_status(self) -> None:
        if hasattr(self, "_status_auto_hide_timer"):
            self._status_auto_hide_timer.stop()
        self._raw_status = ""
        if hasattr(self, "status_banner"):
            self.status_banner.set_message("")
            self.status_banner.setToolTip(self._tr("Status notification tooltip"))
        if hasattr(self, "details_button") and self.details_button.isChecked():
            was_blocked = self.details_button.blockSignals(True)
            self.details_button.setChecked(False)
            self.details_button.blockSignals(was_blocked)
        if hasattr(self, "details_button"):
            self.details_button.setEnabled(False)
            self.details_button.hide()
        if hasattr(self, "details_panel"):
            self.details_panel.clear()
        if hasattr(self, "status_details_popup"):
            self.status_details_popup.hide()
        if hasattr(self, "status_footer"):
            self.status_footer.hide()
        if getattr(self, "tray_icon", None):
            self.tray_icon.setToolTip(APP_DISPLAY_NAME)

    def _schedule_status_auto_hide(self, severity: str) -> None:
        if not hasattr(self, "_status_auto_hide_timer"):
            return
        self._status_auto_hide_timer.stop()
        # Pause auto-hide while the details popover is open so the user
        # can read at their own pace; resume when they dismiss it.
        details_open = (
            getattr(self, "details_button", None) is not None
            and self.details_button.isChecked()
        )
        if details_open:
            return
        self._status_auto_hide_timer.start(self._STATUS_AUTO_HIDE_MS.get(severity, self._STATUS_AUTO_HIDE_MS["info"]))

    # ---- details popover ------------------------------------------------

    def _toggle_details(self, checked: bool) -> None:
        if checked:
            self._show_details_popover()
            self._status_auto_hide_timer.stop()
        else:
            self.status_details_popup.hide()
            if self.status_footer.isVisible():
                self._schedule_status_auto_hide(
                    str(self.status_footer.property("severity") or "info")
                )

    def _show_details_popover(self) -> None:
        self._position_details_popover()
        self.status_details_popup.show()
        self.status_details_popup.raise_()
        self.status_details_popup.install_dwm_chrome()
        self.status_details_popup.activateWindow()

    def _position_details_popover(self) -> None:
        pill = self.status_footer
        if pill is None:
            return
        # Width: same as the pill so the dropdown reads as an extension
        # of the same visual element. Height: tall enough for the text,
        # capped so it never crowds the window.
        width = pill.width()
        height = min(260, max(120, self.status_details_popup.sizeHint().height()))
        anchor = pill.mapToGlobal(QPoint(0, pill.height() + 4))
        self.status_details_popup.setFixedSize(width, height)
        self.status_details_popup.move(anchor)

    def _dismiss_details_popover(self) -> None:
        if self.details_button.isChecked():
            was_blocked = self.details_button.blockSignals(True)
            self.details_button.setChecked(False)
            self.details_button.blockSignals(was_blocked)
        self.status_details_popup.hide()
        if self.status_footer.isVisible():
            self._schedule_status_auto_hide(
                str(self.status_footer.property("severity") or "info")
            )

    def run_task(
        self,
        action: Callable[[Callable[[str], None]], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        worker = Worker(action)
        self._workers.add(worker)

        def handle_result(result: Any) -> None:
            try:
                if on_success:
                    on_success(result)
            finally:
                self._workers.discard(worker)

        def handle_error(ex: Exception) -> None:
            try:
                self._handle_task_error(ex, on_error)
            finally:
                self._workers.discard(worker)

        worker.signals.progress.connect(self.set_status)
        worker.signals.result.connect(handle_result)
        worker.signals.error.connect(handle_error)
        self.thread_pool.start(worker)

    def run_process_task(
        self,
        operation: str,
        arguments: list[str] | None = None,
        action_key: str | None = None,
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if action_key and action_key in self._locked_action_keys:
            self.set_status("That task is already running.", "info")
            return
        program, base_args, working_dir = self._worker_command()
        process = QProcess(self)
        process.setProgram(program)
        process.setArguments([*base_args, operation, *(arguments or [])])
        process.setWorkingDirectory(str(working_dir))
        process.setProcessChannelMode(QProcess.SeparateChannels)
        env = QProcessEnvironment.systemEnvironment()
        if not getattr(sys, "frozen", False):
            package_root = str(Path(__file__).resolve().parents[1])
            existing = env.value("PYTHONPATH", "")
            env.insert("PYTHONPATH", package_root if not existing else package_root + os.pathsep + existing)
        process.setProcessEnvironment(env)
        task = {
            "stdout": "",
            "stderr": "",
            "result": None,
            "error": None,
            "action_key": action_key,
            "on_success": on_success,
            "on_error": on_error,
        }
        self._process_tasks[process] = task
        if action_key:
            self._set_action_locked(action_key, True)

        process.readyReadStandardOutput.connect(lambda process=process: self._read_process_stdout(process))
        process.readyReadStandardError.connect(lambda process=process: self._read_process_stderr(process))
        process.errorOccurred.connect(lambda error, process=process: self._process_start_failed(process, error))
        process.finished.connect(lambda exit_code, exit_status, process=process: self._process_finished(process, exit_code, exit_status))
        process.start()

    def _worker_command(self) -> tuple[str, list[str], Path]:
        if getattr(sys, "frozen", False):
            return sys.executable, ["--cqv-worker"], Path(sys.executable).resolve().parent
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            console_python = executable.with_name("python.exe")
            if console_python.exists():
                executable = console_python
        return str(executable), ["-m", "codex_quota_viewer.task_worker"], Path(__file__).resolve().parents[3]

    def _read_process_stdout(self, process: QProcess) -> None:
        task = self._process_tasks.get(process)
        if task is None:
            return
        task["stdout"] += bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        while "\n" in task["stdout"]:
            line, task["stdout"] = task["stdout"].split("\n", 1)
            self._handle_process_line(task, line.strip())

    def _read_process_stderr(self, process: QProcess) -> None:
        task = self._process_tasks.get(process)
        if task is not None:
            task["stderr"] += bytes(process.readAllStandardError()).decode("utf-8", errors="replace")

    def _handle_process_line(self, task: dict[str, Any], line: str) -> None:
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            task["stderr"] += line + "\n"
            return
        message_type = payload.get("type")
        if message_type == "progress":
            kind = payload.get("kind")
            if kind == "sessions-rescan-batch":
                self._handle_sessions_rescan_progress(payload)
            else:
                self.set_status(str(payload.get("message") or ""), "info")
        elif message_type == "result":
            task["result"] = payload
        elif message_type == "error":
            task["error"] = payload

    def _handle_sessions_rescan_progress(self, payload: dict[str, Any]) -> None:
        # Per-batch progress signal from task_worker.sessions_rescan. Routes
        # to SessionsPage so the user sees the list filling in live instead
        # of waiting for the whole rescan to finish. Drop progress for a
        # target the user has since switched away from — otherwise the
        # Sandbox rescan's batch counts would briefly clobber the Real
        # tab's count label and trigger spurious refreshes.
        page = getattr(self, "_sessions_page_widget", None)
        if page is None:
            return
        target_value = payload.get("target")
        if isinstance(target_value, str) and target_value != page.target.value:
            return
        try:
            done = int(payload.get("done") or 0)
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return
        page.apply_rescan_progress(done, total)

    def _process_start_failed(self, process: QProcess, error: QProcess.ProcessError) -> None:
        task = self._process_tasks.get(process)
        message = f"Could not start background task: {process.errorString() or error}"
        if task is not None:
            task["error"] = {"message": message}
        failed_to_start = getattr(getattr(QProcess, "ProcessError", object), "FailedToStart", getattr(QProcess, "FailedToStart", None))
        if failed_to_start is not None and error == failed_to_start:
            task = self._process_tasks.pop(process, None)
            if task is not None:
                action_key = task.get("action_key")
                if action_key:
                    self._set_action_locked(str(action_key), False)
                self._handle_task_error(RuntimeError(message), task.get("on_error"))
            process.deleteLater()

    def _process_finished(self, process: QProcess, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        task = self._process_tasks.pop(process, None)
        if task is None:
            process.deleteLater()
            return
        if task.get("stdout"):
            self._handle_process_line(task, str(task["stdout"]).strip())
        action_key = task.get("action_key")
        if action_key:
            self._set_action_locked(str(action_key), False)
        try:
            error_payload = task.get("error")
            if error_payload or exit_code != 0 or exit_status != QProcess.NormalExit:
                message = ""
                if isinstance(error_payload, dict):
                    message = str(error_payload.get("message") or "")
                if not message:
                    message = str(task.get("stderr") or f"Background task exited with code {exit_code}.").strip()
                raise RuntimeError(message)
            result = task.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Background task finished without a result.")
            on_success = task.get("on_success")
            if on_success:
                on_success(result)
        except Exception as ex:
            self._handle_task_error(ex, task.get("on_error"))
        finally:
            process.deleteLater()

    def _set_action_locked(self, action_key: str, locked: bool) -> None:
        if locked:
            self._locked_action_keys.add(action_key)
        else:
            self._locked_action_keys.discard(action_key)
        for button in self.findChildren(QPushButton):
            if button.property("actionKey") == action_key:
                enabled = not locked
                if action_key == "sync-sandbox-real":
                    enabled = enabled and (self._sync_sessions_to_real or self._sync_assets_to_real)
                button.setEnabled(enabled)
                button.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)

    def _handle_task_error(self, ex: Exception, on_error: Callable[[Exception], None] | None) -> None:
        if on_error:
            on_error(ex)
            return
        self.set_status(str(ex))
        QMessageBox.warning(self, APP_DISPLAY_NAME, str(ex))

    def show_accounts(self, refresh_quota: bool = True) -> None:
        self.current_view = "Accounts"
        self.body.setUpdatesEnabled(False)
        try:
            self._clear("Accounts")
            try:
                accounts = self.services.load_accounts()
                active = self.services.resolve_active_account()
                self._render_accounts(accounts, active)
                if refresh_quota:
                    self.refresh_quota(force_refresh=False, update_status=False)
            except Exception as ex:
                self.set_status(str(ex))
        finally:
            self.body.setUpdatesEnabled(True)

    def _render_accounts(self, accounts: list[AccountRecord], active: AccountRecord | None) -> None:
        self.body_layout.addWidget(self._summary_card(accounts, active))
        active_id = active.metadata.id if active else None
        current_accounts = [active] if active else []
        chat_accounts = [item for item in accounts if item.metadata.auth_mode == AuthMode.CHAT_GPT and item.metadata.id != active_id]
        api_accounts = [item for item in accounts if item.metadata.auth_mode == AuthMode.API_KEY and item.metadata.id != active_id]
        self._add_group("Current Account", current_accounts, active_id, "The active Codex home does not match a saved vault account.")
        self._add_group("ChatGPT Accounts", chat_accounts, active_id)
        self._add_group("API Accounts", api_accounts, active_id)
        self.body_layout.addStretch(1)

    def _summary_card(self, accounts: list[AccountRecord], active: AccountRecord | None) -> QFrame:
        del accounts, active
        card = QFrame()
        card.setProperty("toolbar", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        codex_label = "Open Sandbox Codex" if self.services.active_target == CodexHomeTarget.SANDBOX else "Open Real Codex"
        actions.addWidget(self._button(codex_label, self.open_codex_app, width=150, accent=True, tooltip=f"{codex_label} tooltip"))
        actions.addWidget(self._button("Use ChatGPT Login", self.add_chatgpt_account, width=140, tooltip="Use ChatGPT Login tooltip"))
        actions.addWidget(self._button("Add API Account", self.add_api_account, width=130, tooltip="Add API Account tooltip"))
        actions.addWidget(self._button("Refresh Quota", lambda: self.refresh_quota(True, True), width=110, tooltip="Refresh Quota tooltip"))
        actions.addWidget(self._button("Open Vault", self.open_vault, width=100, tooltip="Open Vault tooltip"))
        actions.addWidget(self._button("Rollback", self.rollback, width=100, tooltip="Rollback tooltip"))
        actions.addStretch(1)
        layout.addLayout(actions)

        self.accounts_quota_label = QLabel(self._tr("Quota: loading..."))
        self.accounts_quota_label.hide()
        layout.addWidget(self.accounts_quota_label)
        return card

    def _add_group(self, title: str, accounts: list[AccountRecord], active_id: str | None, empty_text: str | None = None) -> None:
        label = QLabel(f"{self._tr(title)} ({len(accounts)})")
        label.setObjectName("SectionTitle")
        self.body_layout.addWidget(label)
        if not accounts:
            empty = QLabel(self._tr(empty_text or "No saved accounts in this group."))
            empty.setObjectName("Muted")
            self.body_layout.addWidget(empty)
            return
        for account in accounts:
            self.body_layout.addWidget(self._account_row(account, account.metadata.id == active_id))

    def _account_row(self, account: AccountRecord, is_current: bool) -> QFrame:
        row = QFrame()
        row.setProperty("card", not is_current)
        row.setProperty("current", is_current)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(16)

        icon = self._account_icon_label(account.metadata.auth_mode)
        layout.addWidget(icon)

        details_widget = QWidget()
        details_widget.setObjectName("AccountDetails")
        details_widget.setAttribute(Qt.WA_StyledBackground, True)
        details = QVBoxLayout(details_widget)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(3)
        name = QLabel(account.metadata.display_name)
        name.setObjectName("AccountName")
        details.addWidget(name)
        subtitle = QLabel(self._describe_account(account, is_current))
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Muted")
        details.addWidget(subtitle)
        layout.addWidget(details_widget, 1, Qt.AlignVCenter)

        if is_current:
            rings = QHBoxLayout()
            rings.setSpacing(14)
            for _ in range(2):
                ring = QuotaRingWidget(row)
                self.quota_ring_widgets.append(ring)
                rings.addWidget(ring)
            layout.addLayout(rings)
            self._update_quota_widgets(self._latest_quota_snapshot)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        if not is_current:
            switch_label = "Switch Real" if self.services.active_target == CodexHomeTarget.REAL else "Switch"
            actions.addWidget(self._button(switch_label, lambda account=account: self.switch_account(account), width=96, accent=True, tooltip=f"{switch_label} tooltip"))
        actions.addWidget(self._button("Rename...", lambda account=account: self.rename_account(account), width=96, tooltip="Rename account tooltip"))
        if not is_current:
            actions.addWidget(self._button("Remove...", lambda account=account: self.delete_account(account), width=96, danger=True, tooltip="Remove account tooltip"))
        layout.addLayout(actions)
        return row

    def _account_icon_label(self, auth_mode: AuthMode) -> QLabel:
        label = QLabel("API" if auth_mode == AuthMode.API_KEY else "")
        label.setObjectName("AccountIcon")
        label.setAlignment(Qt.AlignCenter)
        label.setFixedSize(44, 44)
        asset_name = "api-account.svg" if auth_mode == AuthMode.API_KEY else "gpt-account.svg"
        icon = _asset_icon(asset_name)
        if not icon.isNull():
            label.setText("")
            label.setPixmap(icon.pixmap(QSize(36, 36)))
        elif auth_mode == AuthMode.CHAT_GPT:
            label.setText("@")
        return label

    def show_sessions(self) -> None:
        self.current_view = "Sessions"
        self.body.setUpdatesEnabled(False)
        try:
            page = self._sessions_page()
            # Heading is intentionally bare — the env switch lives as
            # segmented Sandbox/Real tabs at the top of the list panel,
            # and the rescan button lives inside the floating action bar.
            # That mirrors the clean "title only" header in Accounts /
            # Settings.
            self._clear("Sessions")
            self.body_layout.addWidget(page, 1)
            page.show()
        finally:
            self.body.setUpdatesEnabled(True)
        # Page is wired with a task_runner, so the data fetch now happens on
        # a worker thread; we can call refresh_if_stale directly without an
        # extra event-loop hop. Navigating to/from Sessions paints instantly.
        page.refresh_if_stale()

    def _sessions_page(self) -> SessionsPage:
        page = getattr(self, "_sessions_page_widget", None)
        if page is None:
            page = SessionsPage(
                sessions_manager_factory=self.services.sessions_manager,
                confirm_real_action=self._confirm_real_sessions_action,
                translator=self._tr,
                task_runner=self._run_sessions_task,
                parent=self,
            )
            page.rescan_requested.connect(self._on_sessions_rescan_requested)
            page.batch_action_requested.connect(self._on_sessions_batch_requested)
            self._sessions_page_widget = page
        return page

    def _run_sessions_task(
        self,
        action: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        # Adapt SessionsPage's TaskRunner contract (no progress arg) onto
        # MainWindow.run_task, which expects action(progress_cb) -> result.
        self.run_task(
            lambda _progress, fn=action: fn(),
            on_success=on_success,
            on_error=on_error,
        )

    def _confirm_real_sessions_action(self, action: str, summary: str) -> bool:
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Warning)
        confirmation.setWindowTitle(self._tr("Confirm Real Sessions action"))
        confirmation.setText(summary)
        confirmation.setInformativeText(
            self._tr(
                "Real Codex home will be modified. Type-confirm by selecting Yes only if you intend to {action}."
            ).format(action=action)
        )
        confirmation.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        confirmation.setDefaultButton(QMessageBox.Cancel)
        return confirmation.exec() == QMessageBox.Yes

    def _on_sessions_rescan_requested(self, target: CodexHomeTarget) -> None:
        self.run_process_task(
            "sessions-rescan",
            ["--target", target.value],
            action_key="sessions-rescan",
            on_success=lambda payload: self._on_sessions_worker_success(payload, refresh=True),
            on_error=lambda ex: self.set_status(f"Sessions rescan failed: {ex}", "error"),
        )

    def _on_sessions_batch_requested(
        self,
        target: CodexHomeTarget,
        action: str,
        session_ids: list[str],
    ) -> None:
        if not session_ids:
            return
        action_key = f"sessions-{action}"
        self.run_process_task(
            "sessions-batch",
            [
                "--target",
                target.value,
                "--batch-action",
                action,
                "--session-ids",
                *session_ids,
            ],
            action_key=action_key,
            on_success=lambda payload: self._on_sessions_worker_success(payload, refresh=True),
            on_error=lambda ex: self.set_status(f"Sessions {action} failed: {ex}", "error"),
        )

    def _on_sessions_worker_success(self, payload: dict[str, Any], *, refresh: bool) -> None:
        message = str(payload.get("message") or "")
        if message:
            self.set_status(message, "success")
        if refresh:
            page = getattr(self, "_sessions_page_widget", None)
            if page is not None:
                page.reload_after_rescan()

    def show_settings(self, settings_data: SettingsData | None = None) -> None:
        self.current_view = "Settings"
        self.body.setUpdatesEnabled(False)
        try:
            self._clear("Settings")
            self.body_layout.addWidget(self._target_mode_card())
            self.body_layout.addWidget(self._maintenance_card())
            self.body_layout.addWidget(self._restore_points_card(settings_data))
            self.body_layout.addWidget(self._audit_card(settings_data))
            self.body_layout.addWidget(self._paths_card())
            self.body_layout.addStretch(1)
        finally:
            self.body.setUpdatesEnabled(True)
        if settings_data is None:
            self._settings_epoch += 1
            epoch = self._settings_epoch
            QTimer.singleShot(0, lambda epoch=epoch: self._load_settings_cards(epoch))

    def _load_settings_cards(self, epoch: int) -> None:
        if self.current_view != "Settings" or epoch != self._settings_epoch:
            return
        self.run_task(
            lambda progress: self._load_settings_data(),
            on_success=lambda data: self._settings_data_loaded(data, epoch),
            on_error=lambda ex: self.set_status(self._tr("Settings load failed: {error}", error=str(ex)), "error"),
        )

    def _load_settings_data(self) -> SettingsData:
        target = self._snapshot_target
        return SettingsData(
            target,
            self.services.list_snapshots(target),
            self.services.snapshot_manager.latest_automatic(target),
            self.services.snapshot_retention_status(target, SnapshotKind.AUTOMATIC),
            self.services.snapshot_retention_status(target, SnapshotKind.MANUAL),
            self.services.recent_audit_events(12),
        )

    def _settings_data_loaded(self, data: SettingsData, epoch: int) -> None:
        if self.current_view != "Settings" or epoch != self._settings_epoch or data.target != self._snapshot_target:
            return
        self._show_settings_preserving_scroll(settings_data=data)

    def _target_mode_card(self) -> QFrame:
        card, layout = self._section_card("Target Mode")

        target_buttons = QHBoxLayout()
        target_buttons.setSpacing(12)
        group = QButtonGroup(card)
        group.setExclusive(True)
        for target in [CodexHomeTarget.SANDBOX, CodexHomeTarget.REAL]:
            button = self._button(target.value, lambda target=target: self._set_target(target), width=230, tooltip=f"{target.value} target tooltip")
            button.setObjectName("TargetButton")
            button.setCheckable(True)
            button.setChecked(target == self.services.active_target)
            button.setProperty("accent", target == self.services.active_target)
            group.addButton(button)
            target_buttons.addWidget(button)
        target_buttons.addStretch(1)
        layout.addLayout(target_buttons)

        active = QLabel(self._tr("Active target: {target}", target=self._target_label(self.services.active_target)))
        active.setObjectName("SettingsLine")
        layout.addWidget(active)

        warning = QLabel(self._tr("Real Mode modifies {path} after confirmation.", path=self.services.paths.real_codex_home))
        warning.setObjectName("WarningLine")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        return card

    def _maintenance_card(self) -> QFrame:
        card, layout = self._section_card("Maintenance")
        actions = QHBoxLayout()
        actions.setSpacing(12)
        actions.addWidget(self._button("Seed Sandbox", self.seed_sandbox, width=150, accent=True, action_key="seed-sandbox", tooltip="Seed Sandbox tooltip"))
        sync_button = self._button(
            "Sync Sandbox to Real",
            self.sync_sandbox_sessions_to_real,
            width=210,
            action_key="sync-sandbox-real",
            enabled=self._sync_sessions_to_real or self._sync_assets_to_real,
            tooltip="Sync Sandbox to Real tooltip",
        )
        actions.addWidget(sync_button)

        options = QVBoxLayout()
        options.setContentsMargins(4, 0, 0, 0)
        options.setSpacing(2)
        sessions = self._sync_option_checkbox("Sync Sessions", self._sync_sessions_to_real, "Sync Sessions tooltip")
        assets = self._sync_option_checkbox("Sync Assets", self._sync_assets_to_real, "Sync Assets tooltip")

        def update_sync_button() -> None:
            sync_button.setEnabled(
                (self._sync_sessions_to_real or self._sync_assets_to_real)
                and "sync-sandbox-real" not in self._locked_action_keys
            )

        sessions.toggled.connect(lambda checked: (self._set_sync_sessions(checked), update_sync_button()))
        assets.toggled.connect(lambda checked: (self._set_sync_assets(checked), update_sync_button()))
        options.addWidget(sessions)
        options.addWidget(assets)
        actions.addLayout(options)
        actions.addWidget(self._button(self._tr("Repair {target} Threads", target=self._target_label(self.services.active_target)), self.repair, width=210, action_key="repair", tooltip="Repair Threads tooltip"))
        actions.addStretch(1)
        layout.addLayout(actions)
        return card

    def _sync_option_checkbox(self, label: str, checked: bool, tooltip: str | None = None) -> QCheckBox:
        checkbox = SmoothCheckBox(self._tr(label))
        checkbox.setChecked(checked)
        if tooltip:
            checkbox.setToolTip(self._tr(tooltip))
            checkbox.setToolTipDuration(12_000)
        return checkbox

    def _set_sync_sessions(self, enabled: bool) -> None:
        self._sync_sessions_to_real = enabled

    def _set_sync_assets(self, enabled: bool) -> None:
        self._sync_assets_to_real = enabled

    def _restore_points_card(self, data: SettingsData | None = None) -> QFrame:
        card, layout = self._section_card("Restore Points")
        target_buttons = QHBoxLayout()
        target_buttons.setSpacing(10)
        for target in [CodexHomeTarget.SANDBOX, CodexHomeTarget.REAL]:
            button = self._button(
                target.value,
                lambda target=target: self._set_snapshot_target(target),
                width=150,
                tooltip=f"{target.value} snapshots tooltip",
            )
            button.setObjectName("TargetButton")
            button.setCheckable(True)
            button.setChecked(target == self._snapshot_target)
            target_buttons.addWidget(button)
        target_buttons.addStretch(1)
        layout.addLayout(target_buttons)

        loading = data is None or data.target != self._snapshot_target
        snapshots = [] if loading else data.snapshots
        table = QTableWidget(len(snapshots), 7)
        table.setObjectName("RestorePointsTable")
        table.setFrameShape(QFrame.NoFrame)
        table.setAttribute(Qt.WA_StyledBackground, True)
        table.viewport().setAutoFillBackground(False)
        table.viewport().setAttribute(Qt.WA_StyledBackground, True)
        table.viewport().setStyleSheet("background: transparent;")
        table.setHorizontalHeaderLabels([self._tr(text) for text in ["Kind", "ID", "Created At", "Target", "Size", "Files", "Note"]])
        table.horizontalHeader().setObjectName("RestorePointsHeader")
        table.horizontalHeader().setAttribute(Qt.WA_StyledBackground, True)
        table.horizontalHeader().setAutoFillBackground(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStyleSheet("background: transparent;")
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setMinimumHeight(self._restore_points_table_height)
        table.setMaximumHeight(self._restore_points_table_height)
        for row, snapshot in enumerate(snapshots):
            values = [
                self._tr(snapshot.kind.value),
                snapshot.id,
                snapshot.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                self._target_label(snapshot.target or self._snapshot_target),
                self._format_bytes(snapshot.size_bytes),
                str(snapshot.file_count or len(snapshot.files)),
                snapshot.summary or snapshot.reason or self._tr("Snapshot"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, snapshot.id)
                table.setItem(row, column, item)
        if loading:
            table.setRowCount(1)
            item = QTableWidgetItem(self._tr("Loading snapshots..."))
            table.setItem(0, 0, item)
            table.setSpan(0, 0, 1, 7)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)

        table_frame = QFrame()
        table_frame.setObjectName("RestoreTableFrame")
        table_frame.setAttribute(Qt.WA_StyledBackground, True)
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)
        table_layout.addWidget(table)
        layout.addWidget(table_frame)

        self._restore_points_table = table
        if loading:
            latest_text = self._tr("Loading restore points...")
        else:
            latest_text = self._tr(
                "Latest automatic: {latest} | automatic {auto_count}/50, {auto_size} | manual {manual_count}/50, {manual_size}",
                latest=(data.latest.id if data.latest else self._tr("none")),
                auto_count=data.automatic_status.count,
                auto_size=self._format_bytes(data.automatic_status.size_bytes),
                manual_count=data.manual_status.count,
                manual_size=self._format_bytes(data.manual_status.size_bytes),
            )
        latest_label = QLabel(latest_text)
        latest_label.setObjectName("Muted")
        layout.addWidget(latest_label)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addWidget(self._button("Create Snapshot", self.create_snapshot, width=150, tooltip="Create Snapshot tooltip"))
        actions.addWidget(self._button("Restore Selected", self.restore_selected_snapshot, width=150, enabled=not loading, tooltip="Restore Selected tooltip"))
        actions.addWidget(self._button("Rollback Last Automatic Change", self.rollback, width=220, tooltip="Rollback Last Automatic Change tooltip"))
        actions.addWidget(self._button("Delete Selected", self.delete_selected_snapshot, width=140, danger=True, enabled=not loading, tooltip="Delete Selected tooltip"))
        actions.addWidget(self._button("Open Snapshot Folder", self.open_snapshot_folder, width=180, tooltip="Open Snapshot Folder tooltip"))
        actions.addStretch(1)
        layout.addLayout(actions)
        return card

    def _audit_card(self, data: SettingsData | None = None) -> QFrame:
        card, layout = self._section_card("Audit")
        loading = data is None
        events = [] if loading else data.audit_events
        table = QTableWidget(len(events), 5)
        table.setObjectName("RestorePointsTable")
        table.setFrameShape(QFrame.NoFrame)
        table.setAttribute(Qt.WA_StyledBackground, True)
        table.viewport().setAutoFillBackground(False)
        table.viewport().setAttribute(Qt.WA_StyledBackground, True)
        table.viewport().setStyleSheet("background: transparent;")
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setHorizontalHeaderLabels([self._tr(text) for text in ["Time", "Event", "Target", "Status", "Summary"]])
        table.horizontalHeader().setObjectName("RestorePointsHeader")
        table.horizontalHeader().setAttribute(Qt.WA_StyledBackground, True)
        table.horizontalHeader().setAutoFillBackground(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStyleSheet("background: transparent;")
        table.setMinimumHeight(self._audit_table_height)
        table.setMaximumHeight(self._audit_table_height)
        for row, event in enumerate(events):
            values = [
                str(event.get("timestamp") or ""),
                str(event.get("eventType") or ""),
                str(event.get("target") or ""),
                str(event.get("status") or ""),
                str(event.get("summary") or event.get("error") or ""),
            ]
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        if loading:
            table.setRowCount(1)
            item = QTableWidgetItem(self._tr("Loading recent audit events..."))
            table.setItem(0, 0, item)
            table.setSpan(0, 0, 1, 5)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)

        frame = QFrame()
        frame.setObjectName("RestoreTableFrame")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.addWidget(table)
        layout.addWidget(frame)
        row = QHBoxLayout()
        row.addWidget(self._button("Open Logs", self.open_logs, width=120, tooltip="Open Logs tooltip"))
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _paths_card(self) -> QFrame:
        card, layout = self._section_card("Paths")
        row = QHBoxLayout()
        paths = QGridLayout()
        paths.setHorizontalSpacing(22)
        paths.setVerticalSpacing(8)
        entries = [
            ("App data", self.services.paths.data_root),
            ("Storage root", self.services.paths.storage_root),
            ("Sandbox home", self.services.paths.sandbox_codex_home),
            ("Real Codex home", self.services.paths.real_codex_home),
            ("Session Manager home", self.services.paths.session_manager_home),
            ("Real Session Manager home", self.services.paths.real_session_manager_home),
            ("Snapshot folder", self.services.snapshot_folder(self._snapshot_target)),
            ("Audit log", self.services.paths.audit_log_path),
            ("OAuth common config", self.services.vault.oauth_common_config_path),
        ]
        for index, (name, value) in enumerate(entries):
            key = QLabel(self._tr(name))
            key.setObjectName("Muted")
            paths.addWidget(key, index, 0)
            value_label = QLabel(str(value))
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            paths.addWidget(value_label, index, 1)
        row.addLayout(paths, 1)
        row.addWidget(self._button("Open Logs", self.open_logs, width=120, tooltip="Open Logs tooltip"), 0, Qt.AlignTop)
        layout.addLayout(row)
        return card

    def _section_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("panel", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        title_label = QLabel(self._tr(title))
        title_label.setObjectName("SectionTitle")
        layout.addWidget(title_label)
        return card, layout

    def _add_section_title(self, text: str) -> None:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 700; margin-top: 8px;")
        self.body_layout.addWidget(label)

    def _add_separator(self) -> None:
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("color: #555; margin: 8px 0;")
        self.body_layout.addWidget(separator)

    def _target_changed(self, text: str) -> None:
        self._set_target(CodexHomeTarget.REAL if text == "Real" else CodexHomeTarget.SANDBOX)

    def _set_snapshot_target(self, target: CodexHomeTarget) -> None:
        if target == self._snapshot_target:
            return
        scroll_value = self._main_scroll_value()
        self._snapshot_target = target
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll(scroll_value=scroll_value)

    def _set_target(self, target: CodexHomeTarget) -> None:
        if target == self.services.active_target:
            return
        if target == CodexHomeTarget.REAL and not self._confirm_phrase(
            "Enable Real Mode",
            f"Real Mode makes tray, repair, switch, and rollback target {self.services.paths.real_codex_home}.",
            "ENABLE REAL MODE",
        ):
            self.services.audit_cancel("settings.target", target, "Enable Real Mode cancelled")
            self._show_settings_preserving_scroll()
            return
        self.services.set_active_target(target)
        self._snapshot_target = target
        self.services.clear_active_quota_buffer()
        self._invalidate_quota_results()
        self._show_settings_preserving_scroll()
        self.set_status(self._tr("{target} Mode is active.", target=self._target_label(target)))

    def refresh_quota(self, force_refresh: bool, update_status: bool) -> None:
        if self._quota_inflight:
            if force_refresh:
                self._quota_pending_force = True
                if self.accounts_quota_label:
                    self.accounts_quota_label.setText(self._tr("Quota: refresh queued..."))
                if update_status:
                    self.set_status(self._tr("Quota refresh is already running; queued one fresh refresh."))
            return
        self._quota_inflight = True
        target = self.services.active_target
        epoch = self._quota_epoch
        if self.accounts_quota_label and force_refresh:
            self.accounts_quota_label.setText(self._tr("Quota: refreshing..."))
        if force_refresh:
            self._update_quota_widgets(None)
        self.run_task(
            lambda progress: self.services.refresh_quota(force_refresh),
            on_success=lambda snapshot: self._quota_refreshed(snapshot, update_status, target, epoch),
            on_error=lambda ex: self._quota_failed(ex, target, epoch),
        )

    def _auto_refresh_quota(self) -> None:
        if self.current_view == "Accounts" and self.accounts_quota_label:
            self.refresh_quota(force_refresh=False, update_status=False)

    def _quota_refreshed(self, snapshot: CodexSnapshot, update_status: bool, target: CodexHomeTarget, epoch: int) -> None:
        self._quota_inflight = False
        if self._quota_pending_force:
            self._quota_pending_force = False
            self.refresh_quota(force_refresh=True, update_status=update_status)
            return
        if target != self.services.active_target or epoch != self._quota_epoch:
            return
        text = self._tr("Quota") + ": " + self._describe_snapshot(snapshot)
        self._latest_quota_snapshot = snapshot
        self._update_quota_widgets(snapshot)
        if self.accounts_quota_label:
            self.accounts_quota_label.setText(text)
        if self.tray_icon:
            self.tray_icon.setToolTip(_trim_tray_text(f"[{self.services.active_target_label}] {snapshot.account.display_label}: {text}"))
        if update_status:
            self.set_status(self._tr("Quota refreshed at {time}.", time=snapshot.fetched_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")))

    def _quota_failed(self, ex: Exception, target: CodexHomeTarget, epoch: int) -> None:
        self._quota_inflight = False
        if self._quota_pending_force:
            self._quota_pending_force = False
            self.refresh_quota(force_refresh=True, update_status=True)
            return
        if target != self.services.active_target or epoch != self._quota_epoch:
            return
        message = self._tr("Quota refresh failed: {error}", error=str(ex))
        self._latest_quota_snapshot = None
        self._update_quota_widgets(None)
        if self.accounts_quota_label:
            self.accounts_quota_label.setText(message)
        self.set_status(message)

    def _invalidate_quota_results(self) -> None:
        self._quota_epoch += 1
        self._quota_pending_force = False
        self._latest_quota_snapshot = None
        self._update_quota_widgets(None)

    def _update_quota_widgets(self, snapshot: CodexSnapshot | None) -> None:
        windows = snapshot.display_windows() if snapshot else []
        for index, widget in enumerate(self.quota_ring_widgets):
            if index < len(windows):
                item = windows[index]
                widget.set_quota(item.label, item.window.remaining_percent, self._tr("remaining"))
            else:
                widget.set_quota("--", None, self._tr("remaining"))
            if widget.parentWidget() is not None:
                widget.setVisible(True)

    def add_api_account(self) -> None:
        dialog = ApiAccountDialog(self, self._tr)
        if dialog.exec() != QDialog.Accepted:
            self.set_status(self._tr("Cancelled."))
            return
        api_key, base_url, display_name, model = dialog.values()
        self.set_status(self._tr("Validating API account..."))
        self.run_task(
            lambda progress: self.services.add_api_account(api_key, base_url, display_name, model),
            on_success=lambda record: (self.show_accounts(), self.set_status(f"Added API account {record.metadata.display_name}.")),
        )

    def add_chatgpt_account(self) -> None:
        self.set_status(self._tr("Starting Codex login. Complete the browser/device flow when it appears."))
        self.run_task(
            lambda progress: self.services.add_chatgpt_account(progress=progress),
            on_success=lambda record: (self.show_accounts(), self.set_status(f"Added ChatGPT account {record.metadata.display_name}.")),
        )

    def switch_account(self, account: AccountRecord) -> None:
        if self.services.active_target == CodexHomeTarget.REAL and not self._confirm_real_switch(account):
            self.services.audit_cancel(
                "switch",
                CodexHomeTarget.REAL,
                f"Switch Real to {account.metadata.display_name} cancelled",
                account_display_name=account.metadata.display_name,
            )
            self.set_status(self._tr("Cancelled."))
            return
        self._invalidate_quota_results()
        if self.accounts_quota_label:
            self.accounts_quota_label.setText(self._tr("Quota: switching to {name}...", name=account.metadata.display_name))
        self.set_status(
            self._tr(
                "Switch progress: preparing {target} switch to {name}...",
                target=self._target_label(self.services.active_target),
                name=account.metadata.display_name,
            ),
            "info",
        )
        self.run_task(
            lambda progress: self.services.switch(account, progress=progress),
            on_success=lambda result: self._switch_done(account, result),
            on_error=lambda ex: (self.show_accounts(), self.set_status("Switch failed: " + str(ex)), QMessageBox.warning(self, "Switch failed", str(ex))),
        )

    def _switch_done(self, account: AccountRecord, result: Any) -> None:
        self.show_accounts(refresh_quota=False)
        desktop = (" " + result.desktop_message) if result.desktop_message else ""
        repair = (" " + result.repair_warning) if getattr(result, "repair_warning", None) else ""
        if result.target == CodexHomeTarget.REAL:
            sync_text = (
                f" provider metadata synced for {result.updated_session_files} rollout files."
                if result.updated_session_files
                else " sessions and state DB were left untouched."
            )
            self.set_status(
                f"Switched Real to {account.metadata.display_name}. "
                f"Restore point {result.restore_point.id};{sync_text}{desktop}{repair}"
            )
        else:
            self.set_status(
                f"Switched Sandbox to {account.metadata.display_name}. "
                f"Restore point {result.restore_point.id}; updated {result.updated_session_files} sessions.{repair}"
            )
        self.refresh_quota(force_refresh=True, update_status=False)

    def _repair_after_switch(self, target: CodexHomeTarget) -> None:
        self.run_task(
            lambda progress: self.services.repair_target(target),
            on_success=lambda result: self.set_status(self._describe_repair(result), "success"),
            on_error=lambda ex: self.set_status("Background repair failed: " + str(ex)),
        )

    def rename_account(self, account: AccountRecord) -> None:
        dialog = FramelessDialog(self._tr("Rename Account"), self)
        dialog.setMinimumWidth(420)
        layout = dialog.body()
        field = QLineEdit(account.metadata.display_name)
        field.selectAll()
        layout.addWidget(QLabel(self._tr("Display name")))
        layout.addWidget(field)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self._tr("Rename"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            self.set_status(self._tr("Cancelled."))
            return
        name = field.text().strip()
        if not name:
            QMessageBox.warning(self, self._tr("Rename Account"), self._tr("Display name is required."))
            return
        self.run_task(
            lambda progress: self.services.rename_account(account, name),
            on_success=lambda record: (self.show_accounts(), self.set_status(f"Renamed account to {record.metadata.display_name}.")),
        )

    def delete_account(self, account: AccountRecord) -> None:
        result = QMessageBox.warning(
            self,
            self._tr("Remove Account"),
            self._tr("Remove {name} from the local vault? This does not modify the active Codex home.", name=account.metadata.display_name),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Ok:
            self.set_status(self._tr("Cancelled."))
            return
        self.run_task(
            lambda progress: self.services.delete_account(account),
            on_success=lambda _: (self.show_accounts(), self.set_status(f"Removed account {account.metadata.display_name}.")),
        )

    def create_snapshot(self) -> None:
        target = self._snapshot_target
        note = self._prompt_snapshot_note()
        if note is None:
            self.services.audit_cancel("snapshot.create", target, f"Create {target.value} snapshot cancelled")
            self.set_status(self._tr("Cancelled."))
            return
        if target == CodexHomeTarget.REAL:
            preview = self.services.snapshot_write_preview(target, "Create Real Snapshot")
            if not self._confirm_phrase("Create Real Snapshot", self._preview_text(preview), "CREATE REAL SNAPSHOT"):
                self.services.audit_cancel("snapshot.create", target, "Create Real snapshot confirmation cancelled")
                self.set_status(self._tr("Cancelled."))
                return
        self.set_status(f"Snapshot progress: creating manual {target.value} snapshot...", "info")
        self.run_task(
            lambda progress: self.services.create_snapshot(target, note),
            on_success=lambda manifest: self._snapshot_created(manifest),
        )

    def _snapshot_created(self, manifest: RestorePointManifest) -> None:
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        self.set_status(
            f"Created {manifest.target.value if manifest.target else self._snapshot_target.value} "
            f"{manifest.kind.value} snapshot {manifest.id}; {manifest.file_count or len(manifest.files)} files.",
            "success",
        )

    def restore_selected_snapshot(self) -> None:
        target = self._snapshot_target
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            QMessageBox.information(self, self._tr("Restore Snapshot"), self._tr("Select a snapshot first."))
            return
        if target == CodexHomeTarget.REAL:
            preview = self.services.restore_snapshot_preview(target, snapshot_id)
            if not self._confirm_phrase("Restore Real Snapshot", self._preview_text(preview), "RESTORE REAL SNAPSHOT"):
                self.services.audit_cancel("snapshot.restore", target, "Restore Real snapshot confirmation cancelled", snapshot_id)
                self.set_status(self._tr("Cancelled."))
                return
        else:
            result = QMessageBox.warning(
                self,
                self._tr("Restore Snapshot"),
                f"Restore Sandbox snapshot {snapshot_id}? A pre-restore automatic snapshot will be created first.",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if result != QMessageBox.Ok:
                self.services.audit_cancel("snapshot.restore", target, "Restore Sandbox snapshot cancelled", snapshot_id)
                self.set_status(self._tr("Cancelled."))
                return
        self.set_status(f"Restore progress: restoring {target.value} snapshot {snapshot_id}...", "info")
        self.run_task(
            lambda progress: self.services.restore_snapshot(target, snapshot_id, progress=progress),
            on_success=lambda manifest: self._snapshot_restored(manifest),
        )

    def _snapshot_restored(self, manifest: RestorePointManifest) -> None:
        self._invalidate_quota_results()
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        self.set_status(
            f"Restored {manifest.target.value if manifest.target else self._snapshot_target.value} "
            f"snapshot {manifest.id}.",
            "success",
        )

    def delete_selected_snapshot(self) -> None:
        target = self._snapshot_target
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            QMessageBox.information(self, self._tr("Delete Snapshot"), self._tr("Select a snapshot first."))
            return
        result = QMessageBox.warning(
            self,
            self._tr("Delete Snapshot"),
            self._tr("Delete {target} snapshot {snapshot_id}? This cannot be undone.", target=self._target_label(target), snapshot_id=snapshot_id),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Ok:
            self.services.audit_cancel("snapshot.delete", target, "Delete snapshot cancelled", snapshot_id)
            self.set_status(self._tr("Cancelled."))
            return
        self.set_status(f"Snapshot progress: deleting {target.value} snapshot {snapshot_id}...", "info")
        self.run_task(
            lambda progress: self.services.delete_snapshot(target, snapshot_id),
            on_success=lambda manifest: self._snapshot_deleted(manifest),
        )

    def _snapshot_deleted(self, manifest: RestorePointManifest) -> None:
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        self.set_status(f"Deleted snapshot {manifest.id}.", "success")

    def open_snapshot_folder(self) -> None:
        folder = self.services.snapshot_folder(self._snapshot_target)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.set_status(f"Opened {self._snapshot_target.value} snapshot folder.")

    def sync_sandbox_sessions_to_real(self) -> None:
        sync_sessions = self._sync_sessions_to_real
        sync_assets = self._sync_assets_to_real
        if not sync_sessions and not sync_assets:
            self.set_status(self._tr("Choose at least one sync option."), "warning")
            return
        arguments = self._sync_worker_arguments(sync_sessions, sync_assets)
        self.set_status(self._tr("Sync progress: scanning Sandbox and Real {sections} for preview...", sections=self._sync_sections_text(sync_sessions, sync_assets)), "info")
        self.run_process_task(
            "sync-preview",
            arguments,
            action_key="sync-sandbox-real",
            on_success=lambda payload: self._sync_preview_ready(payload, sync_sessions, sync_assets),
        )

    def _sync_preview_ready(self, payload: dict[str, Any], sync_sessions: bool, sync_assets: bool) -> None:
        preview = payload.get("preview")
        if not isinstance(preview, dict):
            raise RuntimeError("Sync preview finished without preview data.")
        if not self._confirm_phrase("Sync Sandbox to Real", self._preview_text(preview), "SYNC SANDBOX TO REAL"):
            self.services.audit_cancel("session.sync", CodexHomeTarget.REAL, "Sync Sandbox to Real cancelled")
            self.set_status(self._tr("Cancelled."))
            return
        arguments = self._sync_worker_arguments(sync_sessions, sync_assets)
        self.set_status(self._tr("Sync progress: preparing Sandbox {sections} to Real sync...", sections=self._sync_sections_text(sync_sessions, sync_assets)), "info")
        self.run_process_task(
            "sync-sandbox-to-real",
            arguments,
            action_key="sync-sandbox-real",
            on_success=lambda result: self._session_sync_done(result),
        )

    @staticmethod
    def _sync_worker_arguments(sync_sessions: bool, sync_assets: bool) -> list[str]:
        return [
            "--sync-sessions",
            "true" if sync_sessions else "false",
            "--sync-assets",
            "true" if sync_assets else "false",
        ]

    def _sync_sections_text(self, sync_sessions: bool, sync_assets: bool) -> str:
        if sync_sessions and sync_assets:
            return self._tr("sessions/assets")
        if sync_sessions:
            return self._tr("sessions")
        if sync_assets:
            return self._tr("assets")
        return self._tr("nothing")

    def _session_sync_done(self, result: SandboxRealSessionSyncResult | dict[str, Any]) -> None:
        if self.current_view == "Settings":
            self._show_settings_preserving_scroll()
        if isinstance(result, dict):
            self.set_status(str(result.get("message") or "Synced Sandbox to Real."), "success")
        else:
            self.set_status(
                "Synced Sandbox to Real. "
                f"Sessions copied {result.copied_files}, overwritten {result.overwritten_files}, "
                f"skipped same {result.skipped_same_files}, skipped Real-newer {result.skipped_real_newer_files}. "
                f"Assets copied {result.asset_copied_files}, overwritten {result.asset_overwritten_files}, "
                f"skipped same {result.asset_skipped_same_files}, skipped Real-newer {result.asset_skipped_real_newer_files}; "
                f"snapshot {result.restore_point.id}.",
                "success",
            )

    def rollback(self) -> None:
        if self.services.active_target == CodexHomeTarget.REAL and not self._confirm_phrase(
            "Rollback Real Codex",
            f"This will restore the latest real restore point under {self.services.paths.real_backups_root}.",
            "ROLLBACK REAL CODEX",
        ):
            self.services.audit_cancel("rollback", CodexHomeTarget.REAL, "Rollback Real Codex cancelled")
            self.set_status("Cancelled.")
            return
        self.run_task(
            lambda progress: self.services.rollback(),
            on_success=lambda manifest: (
                self._refresh_current_view_after_data_change(),
                self.set_status(f"Rolled back {self.services.active_target_label} restore point {manifest.id}."),
            ),
        )

    def seed_sandbox(self) -> None:
        self.set_status("Seed progress: preparing Sandbox seed...", "info")
        self.run_process_task(
            "seed-sandbox",
            action_key="seed-sandbox",
            on_success=lambda result: self._seed_done(result),
        )

    def _seed_done(self, result: dict[str, Any]) -> None:
        self.services.quota_buffer.clear(CodexHomeTarget.SANDBOX)
        self._refresh_current_view_after_data_change()
        self.set_status(str(result.get("message") or "Seeded sandbox."), "success")

    @staticmethod
    def _describe_seed(result) -> str:
        message = f"Seeded sandbox. Copied {result.copied_files} files."
        if result.skipped_files:
            message += f" Skipped {result.skipped_files} files."
        if result.warnings:
            message += " " + "; ".join(result.warnings[:3])
        return message

    def repair(self) -> None:
        if self.services.active_target == CodexHomeTarget.REAL and not self._confirm_phrase(
            "Repair Real Codex",
            f"This will run Session Manager repair against {self.services.paths.real_codex_home}.",
            "REPAIR REAL CODEX",
        ):
            self.services.audit_cancel("repair", CodexHomeTarget.REAL, "Repair Real Codex cancelled")
            self.set_status("Cancelled.")
            return
        self.set_status(f"Repair progress: preparing {self.services.active_target_label} threads...", "info")
        self.run_process_task(
            "repair",
            ["--target", self.services.active_target.value],
            action_key="repair",
            on_success=lambda result: self.set_status(str(result.get("message") or "Repair complete."), "success"),
        )

    def open_session_manager(self) -> None:
        self.run_task(
            lambda progress: self.services.open_session_manager(),
            on_success=lambda _: self.set_status(f"Opened {self.services.active_target_label} Session Manager."),
        )

    def open_codex_app(self) -> None:
        target = self.services.active_target_label
        self.set_status(f"Opening Codex App for {target}...")
        self.run_task(
            lambda progress: self.services.open_codex_app(),
            on_success=lambda message: self.set_status(str(message)),
        )

    def open_vault(self) -> None:
        self.services.paths.accounts_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.services.paths.accounts_root)))
        self.set_status("Opened account vault folder.")

    def open_logs(self) -> None:
        self.services.paths.logs_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.services.paths.logs_root)))
        self.set_status("Opened logs folder.")

    def _selected_snapshot_id(self) -> str | None:
        table = getattr(self, "_restore_points_table", None)
        if table is None:
            return None
        selected = table.selectionModel().selectedRows()
        if not selected:
            return None
        item = table.item(selected[0].row(), 0)
        if item is None:
            return None
        value = item.data(Qt.UserRole)
        return str(value) if value else None

    def _prompt_snapshot_note(self) -> str | None:
        dialog = FramelessDialog(self._tr("Create Snapshot"), self)
        dialog.setMinimumWidth(480)
        layout = dialog.body()
        layout.addWidget(QLabel(self._tr("Create a manual snapshot for {target}.", target=self._target_label(self._snapshot_target))))
        field = QLineEdit(self._tr("Manual snapshot"))
        field.selectAll()
        layout.addWidget(field)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self._tr("Create"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return field.text().strip() or self._tr("Manual snapshot")

    def _confirm_real_switch(self, account: AccountRecord) -> bool:
        preview = self.services.switch_write_preview(account, CodexHomeTarget.REAL)
        body = self._preview_text(preview, [f"Target account: {account.metadata.display_name}"])
        return self._confirm_phrase("Switch Real Codex", body, "SWITCH REAL CODEX")

    def _confirm_phrase(self, title: str, body: str, phrase: str) -> bool:
        return ConfirmPhraseDialog(title, body, phrase, self).exec() == QDialog.Accepted

    def _preview_text(self, preview: WritePreview | dict[str, Any], prefix: list[str] | None = None) -> str:
        def read(name: str, default: Any = None) -> Any:
            return preview.get(name, default) if isinstance(preview, dict) else getattr(preview, name)

        target = read("target")
        target_label = target if isinstance(target, str) else target.value
        lines = list(prefix or [])
        lines.extend(
            [
                f"Operation: {read('operation')}",
                f"Target: {target_label}",
                f"Codex home: {read('target_home')}",
                f"Impact: {read('affected_files', 0)} files, about {self._format_bytes(int(read('affected_bytes', 0) or 0))}",
                (
                    "Stats: "
                    f"created {read('created_files', 0)}, "
                    f"modified/replaced {read('modified_files', 0)}, "
                    f"deleted {read('deleted_files', 0)}"
                ),
            ]
        )
        summary = read("summary", "")
        sample_paths = read("sample_paths", ()) or ()
        warnings = read("warnings", ()) or ()
        if summary:
            lines.extend(["", str(summary)])
        if sample_paths:
            lines.extend(["", "Path samples:"])
            lines.extend(f" - {path}" for path in list(sample_paths)[:5])
        if warnings:
            lines.extend(["", "Warnings:"])
            lines.extend(f" - {warning}" for warning in warnings)
        lines.extend(
            [
                "",
                "Secrets and session message bodies are not shown in this preview.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(max(0, value))
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} GB"

    def _button(
        self,
        label: str,
        action: Callable[[], None],
        width: int | None = None,
        accent: bool = False,
        danger: bool = False,
        action_key: str | None = None,
        enabled: bool = True,
        tooltip: str | None = None,
    ) -> QPushButton:
        button = QPushButton(self._tr(label) if self is not None else label)
        if width:
            button.setMinimumWidth(width)
        if accent:
            button.setProperty("accent", True)
        if danger:
            button.setProperty("danger", True)
        if action_key:
            button.setProperty("actionKey", action_key)
        locked_keys = getattr(self, "_locked_action_keys", set()) if self is not None else set()
        is_enabled = enabled and not (action_key and action_key in locked_keys)
        button.setEnabled(is_enabled)
        button.setCursor(Qt.PointingHandCursor if is_enabled else Qt.ArrowCursor)
        if tooltip:
            button.setToolTip(self._tr(tooltip) if self is not None else tooltip)
            button.setToolTipDuration(12_000)
        button.clicked.connect(lambda checked=False: action())
        return button

    def _icon_button(
        self,
        icon: QIcon,
        action: Callable[[], None],
        *,
        action_key: str | None = None,
        tooltip: str | None = None,
    ) -> QPushButton:
        button = QPushButton()
        button.setObjectName("HeaderIconButton")
        button.setIcon(icon)
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(36, 36)
        button.setFlat(True)
        if action_key:
            button.setProperty("actionKey", action_key)
        locked_keys = getattr(self, "_locked_action_keys", set()) if self is not None else set()
        is_enabled = not (action_key and action_key in locked_keys)
        button.setEnabled(is_enabled)
        button.setCursor(Qt.PointingHandCursor if is_enabled else Qt.ArrowCursor)
        if tooltip:
            text = self._tr(tooltip) if self is not None else tooltip
            button.setToolTip(text)
            button.setAccessibleName(text)
            button.setToolTipDuration(12_000)
        button.clicked.connect(lambda checked=False: action())
        return button

    def _describe_account(self, account: AccountRecord, is_current: bool) -> str:
        pieces = [
            self._tr("Active" if is_current else "Saved"),
            self._tr("ChatGPT" if account.metadata.auth_mode == AuthMode.CHAT_GPT else "API key"),
            self._tr("local vault"),
        ]
        if account.metadata.auth_mode == AuthMode.CHAT_GPT:
            source = self.services.oauth_config_source_label(account)
            if isinstance(source, str) and source:
                pieces.append(self._tr(source))
        if account.metadata.auth_mode == AuthMode.API_KEY and account.metadata.base_url:
            pieces.append(account.metadata.base_url)
        if account.metadata.model:
            pieces.append(account.metadata.model)
        return " - ".join(pieces)

    def _describe_snapshot(self, snapshot: CodexSnapshot) -> str:
        windows = snapshot.display_windows()
        if not windows:
            quota = snapshot.quota_error or self._tr("Quota unavailable")
        else:
            quota = " ".join(f"{item.label}: {item.window.remaining_percent:.0f}% {self._tr('remaining')}" for item in windows)
        return f"{snapshot.account.display_label} | {quota} | fetched {snapshot.fetched_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"

    @staticmethod
    def _describe_repair(result: OfficialRepairSummary) -> str:
        return (
            f"Repair complete. Provider metadata synced. SQLite rows {result.updated_threads}, "
            f"rollout files {result.updated_session_index_entries}."
        )


def _install_native_window_chrome(window: QMainWindow) -> None:
    """On Windows 11: enable native rounded corners + native edge resize on a frameless window.
    Combined with MainWindow.nativeEvent (WM_NCHITTEST / WM_NCCALCSIZE), this gives the user
    OS-level resize cursors and Aero Snap on every edge and corner."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd_int = int(window.winId())

        GWL_STYLE = -16
        WS_THICKFRAME = 0x00040000
        WS_CAPTION = 0x00C00000
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOOWNERZORDER = 0x0200
        SWP_NOACTIVATE = 0x0010

        style = user32.GetWindowLongW(hwnd_int, GWL_STYLE)
        # Re-enable WS_THICKFRAME so Windows handles edge resize hit-testing for us.
        # Drop WS_CAPTION (FramelessWindowHint already removed it; keep it removed).
        new_style = (style | WS_THICKFRAME) & ~WS_CAPTION
        if new_style != style:
            user32.SetWindowLongW(hwnd_int, GWL_STYLE, new_style)
            user32.SetWindowPos(
                hwnd_int, 0, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                | SWP_NOOWNERZORDER | SWP_NOACTIVATE,
            )

        hwnd = wintypes.HWND(hwnd_int)
        dwmapi = ctypes.windll.dwmapi

        # Dark title bar painting (Win10 1809+ / Win11)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dark = ctypes.c_int(1)
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_int(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(dark),
                ctypes.sizeof(dark),
            )
        except Exception:
            pass

        # Keep the DWM backdrop disabled so Qt's per-pixel alpha shows the
        # actual desktop behind the frameless window instead of a Windows
        # material layer.
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_NONE = 1
        backdrop = ctypes.c_int(DWMSBT_NONE)
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_int(DWMWA_SYSTEMBACKDROP_TYPE),
                ctypes.byref(backdrop),
                ctypes.sizeof(backdrop),
            )
        except Exception:
            pass

        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def _install_acrylic_blur(window: QWidget, enabled: bool = True, tint_alpha: int | None = None) -> bool:
    """Enable Windows Terminal-style acrylic blur behind the Qt client area."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        set_window_composition_attribute = ctypes.windll.user32.SetWindowCompositionAttribute
        set_window_composition_attribute.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(WindowCompositionAttributeData),
        ]
        set_window_composition_attribute.restype = wintypes.BOOL

        ACCENT_ENABLE_GRADIENT = 1
        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
        WCA_ACCENT_POLICY = 19
        # GradientColor is AABBGGRR. This tint roughly matches Windows
        # Terminal's dark acrylic without making desktop details too sharp.
        alpha = 150 if enabled else 195
        if enabled and tint_alpha is not None:
            alpha = min(255, max(0, tint_alpha))
        red, green, blue = 24, 31, 36
        tint = (alpha << 24) | (blue << 16) | (green << 8) | red
        state = ACCENT_ENABLE_ACRYLICBLURBEHIND if enabled else ACCENT_ENABLE_GRADIENT
        accent = AccentPolicy(state, 2, tint, 0)
        data = WindowCompositionAttributeData(
            WCA_ACCENT_POLICY,
            ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
            ctypes.sizeof(accent),
        )
        return bool(set_window_composition_attribute(wintypes.HWND(int(window.winId())), ctypes.byref(data)))
    except Exception:
        return False


def run_app() -> int:
    app = QApplication(sys.argv)
    _apply_dark_palette(app)
    app.setQuitOnLastWindowClosed(False)
    app_icon = _asset_icon(APP_ICON_ASSET)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    services = AppServices()
    window = MainWindow(services)
    icon = app_icon if not app_icon.isNull() else app.style().standardIcon(QStyle.SP_ComputerIcon)
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(APP_DISPLAY_NAME)
    menu = QMenu()
    open_action = QAction("Open", menu)
    open_action.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    refresh_action = QAction("Refresh quota", menu)
    refresh_action.triggered.connect(lambda: window.refresh_quota(force_refresh=True, update_status=True))
    codex_action = QAction("Open Codex App", menu)
    codex_action.triggered.connect(window.open_codex_app)
    exit_action = QAction("Exit", menu)
    exit_action.triggered.connect(app.quit)
    menu.addAction(open_action)
    menu.addAction(refresh_action)
    menu.addAction(codex_action)
    menu.addSeparator()
    menu.addAction(exit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (window.show(), window.raise_(), window.activateWindow()) if reason == QSystemTrayIcon.DoubleClick else None)
    tray.show()
    window.set_tray_icon(tray)
    window.show()
    _install_native_window_chrome(window)
    _install_acrylic_blur(window)
    app.aboutToQuit.connect(lambda: (tray.hide(), services.dispose()))
    return app.exec()


def _trim_tray_text(text: str) -> str:
    return text[:117] + "..." if len(text) > 120 else text
