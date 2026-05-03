"""Shared frosted-glass surface base class + Win32 DWM helpers.

Centralises the buffered ``paintEvent`` recipe (QImage → rounded clip path
→ base+tint fill → border + inner highlight strokes → composited blit)
plus the native Windows acrylic blur and DWM rounded-corner installs that
every translucent popup / floating bar in this app needs.

Subclasses override class attributes (radius, palette, behaviour flags)
to specialise; the base class owns everything else. New popovers, toasts
and floating bars should be a ~10-line subclass of ``_FrostedSurface``,
not yet another copy of the recipe.

Hosting both the base class and the Win32 helpers in this module avoids
a circular import: ``qt_app`` imports ``SessionsPage`` from
``sessions_page``, so the helpers cannot live in either of those modules
once both modules need to use them. Both ``qt_app`` and ``sessions_page``
import from this module instead.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QFrame, QWidget


__all__ = [
    "_FrostedSurface",
    "_install_acrylic_blur_for_popup",
    "_install_dialog_chrome_for_popup",
]


# --- Native Windows acrylic blur for top-level frameless popups -------------


def _install_acrylic_blur_for_popup(window: QWidget, *, tint_alpha: int = 58) -> bool:
    """Enable Windows Terminal-style acrylic blur behind a frameless window."""
    try:
        import sys
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        class _AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class _WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        set_window_composition_attribute = (
            ctypes.windll.user32.SetWindowCompositionAttribute
        )
        set_window_composition_attribute.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_WindowCompositionAttributeData),
        ]
        set_window_composition_attribute.restype = wintypes.BOOL

        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
        WCA_ACCENT_POLICY = 19
        # GradientColor is AABBGGRR. Match the dark-info tint used by the
        # main-window status footer so popups feel native.
        alpha = min(255, max(0, int(tint_alpha)))
        red, green, blue = 24, 31, 36
        tint = (alpha << 24) | (blue << 16) | (green << 8) | red
        accent = _AccentPolicy(ACCENT_ENABLE_ACRYLICBLURBEHIND, 2, tint, 0)
        data = _WindowCompositionAttributeData(
            WCA_ACCENT_POLICY,
            ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
            ctypes.sizeof(accent),
        )
        return bool(
            set_window_composition_attribute(
                wintypes.HWND(int(window.winId())), ctypes.byref(data)
            )
        )
    except Exception:
        return False


def _install_dialog_chrome_for_popup(
    window: QWidget,
    *,
    corner_preference: int = 2,
) -> None:
    """Win11 rounded corners + transparent border for frameless windows.

    ``corner_preference`` maps to the DWMWCP_* constants:

      * ``0`` = ``DWMWCP_DEFAULT`` (system decides)
      * ``1`` = ``DWMWCP_DONOTROUND`` — keep the HWND rectangular and
        let the caller's ``paintEvent`` own all corner rounding.
        Use this when the painted corner radius is larger than Win11's
        ~8px default rounding, otherwise the DWM clip leaves a thin
        ring between the two radii where the system drop shadow shows
        through (visible as a soft "shadow on the top corners" on
        compact toolbars).
      * ``2`` = ``DWMWCP_ROUND`` (default, Win11 large radius ≈ 8px)
      * ``3`` = ``DWMWCP_ROUNDSMALL`` (Win11 small radius)
    """
    try:
        import sys
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWA_BORDER_COLOR = 34
        DWMWA_COLOR_NONE = 0xFFFFFFFE
        hwnd = wintypes.HWND(int(window.winId()))
        value = ctypes.c_int(int(corner_preference))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        border = ctypes.c_uint(DWMWA_COLOR_NONE)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_int(DWMWA_BORDER_COLOR),
            ctypes.byref(border),
            ctypes.sizeof(border),
        )
    except Exception:
        pass


# --- Frosted-glass base class ---------------------------------------------


class _FrostedSurface(QFrame):
    """Buffered, rounded, translucent surface with optional acrylic blur.

    Geometry, colour and behaviour are class attributes — subclasses
    override only what they need. The default palette is the
    ``info``-severity dark frosted glass that ``design_tokens.SURFACE_FROSTED``
    encodes as a QSS string (``rgba(18, 39, 54, 222)``); keep the QColor
    literal here in sync if the QSS token ever shifts.

    Two construction modes:

    * ``as_window=True`` (default) → top-level ``Qt.Tool`` window with
      real HWND alpha. ``paintEvent``'s final blit uses
      ``CompositionMode_Source`` so the rounded fill is exact and the
      corners go transparent (acrylic backdrop / desktop visible
      through them).
    * ``as_window=False`` → embedded child widget. ``WA_TranslucentBackground``
      is unreliable for child widgets on Windows, so we lean on
      ``WA_StyledBackground`` + the parent's QSS ``background: transparent``
      rule and switch the blit to ``CompositionMode_SourceOver`` so the
      QSS-painted parent fill stays visible at the corners and we only
      stamp the rounded shape on top.
    """

    # ---- paint geometry ----------------------------------------------
    RADIUS: float = 12.0
    """Outer fill radius (clipped path used for base + tint)."""

    BORDER_RADIUS: float = 11.5
    """Border stroke radius — the stroke is inset 0.5px so the 1px line
    sits half inside / half on the outer edge."""

    INNER_RADIUS: float = 10.5
    """Inner highlight radius — the highlight is inset 1.5px to read as
    a faint glass bevel."""

    # ---- palette (info severity by default) --------------------------
    BASE_COLOR: QColor = QColor(18, 39, 54, 222)
    BORDER_COLOR: QColor = QColor(10, 132, 255, 165)
    TINT_COLOR: QColor = QColor(10, 132, 255, 30)
    INNER_COLOR: QColor = QColor(255, 255, 255, 18)

    # ---- native acrylic alpha overrides ------------------------------
    NATIVE_ACRYLIC_BASE_ALPHA: int = 46
    """Base fill alpha when ``set_native_acrylic(True)`` — drops so the
    OS blurred backdrop reads through."""
    NATIVE_ACRYLIC_TINT_ALPHA: int = 14
    """Tint alpha when ``set_native_acrylic(True)``."""

    # ---- DWM chrome --------------------------------------------------
    DWM_CORNER_PREFERENCE: int = 2  # DWMWCP_ROUND
    """``corner_preference`` passed to ``_install_dialog_chrome_for_popup``."""
    DWM_TINT_ALPHA: int = 58
    """``tint_alpha`` passed to ``_install_acrylic_blur_for_popup``."""

    # ---- behaviour flags --------------------------------------------
    ACCEPT_FOCUS: bool = False
    """``True`` → ``StrongFocus``; ``False`` → ``NoFocus`` and (when
    top-level) ``WA_ShowWithoutActivating`` + ``WindowDoesNotAcceptFocus``."""

    DISMISS_ON_ESCAPE: bool = False
    """Emit ``dismiss_requested`` when the user presses ESC."""

    DISMISS_ON_DEACTIVATE: bool = False
    """Emit ``dismiss_requested`` when the popup loses window focus.
    Gives a click-outside-to-dismiss UX without a transparent
    click-catcher widget."""

    ABSORB_DOUBLE_CLICK: bool = False
    """Accept double-click on the surface itself so it doesn't bubble up
    to the parent (the title-bar pill uses this so double-clicking the
    pill's empty padding doesn't toggle window maximize)."""

    dismiss_requested = Signal()
    """Emitted when the user requests the surface be dismissed (ESC,
    click-outside). Subclasses can also emit this directly."""

    def __init__(self, parent: QWidget | None = None, *, as_window: bool = True):
        flags = Qt.WindowFlags()
        if as_window:
            flags = (
                Qt.Tool
                | Qt.FramelessWindowHint
                | Qt.NoDropShadowWindowHint
            )
            if not self.ACCEPT_FOCUS:
                flags |= Qt.WindowDoesNotAcceptFocus
        super().__init__(parent, flags)

        self._native_acrylic: bool = False
        self._dwm_chrome_installed: bool = False

        self.setFrameShape(QFrame.NoFrame)
        self.setLineWidth(0)
        self.setMidLineWidth(0)
        self.setAutoFillBackground(False)

        if as_window:
            # Top-level: real HWND alpha + acrylic compositor. paintEvent
            # blits with CompositionMode_Source so the rounded corners
            # go fully transparent.
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            if not self.ACCEPT_FOCUS:
                self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setFocusPolicy(
                Qt.StrongFocus if self.ACCEPT_FOCUS else Qt.NoFocus
            )
        else:
            # Embedded child: WA_TranslucentBackground is unreliable for
            # child widgets on Windows. Lean on WA_StyledBackground +
            # the QSS ``background: transparent`` rule for the object
            # name; paintEvent's final blit uses CompositionMode_SourceOver
            # so we draw the rounded surface *on top of* that QSS fill
            # at the corner ears instead of replacing it.
            self.setAttribute(Qt.WA_StyledBackground, True)

    # ---- subclass hooks ---------------------------------------------

    def _resolve_palette(self) -> tuple[QColor, QColor, QColor]:
        """Return (base, border, tint) for the current paint cycle.

        Default: returns the class-attribute palette. Override to make
        the palette runtime-mutable — e.g. ``StatusPopupFrame`` reads
        ``self.property("severity")`` to pick a colour family.
        """
        return self.BASE_COLOR, self.BORDER_COLOR, self.TINT_COLOR

    # ---- native-acrylic toggle --------------------------------------

    def set_native_acrylic(self, enabled: bool) -> None:
        """Toggle the painted alphas for when OS acrylic blur is active.

        When the host successfully installs ``SetWindowCompositionAttribute``,
        call this with ``True`` so the painted base fill drops to
        ``NATIVE_ACRYLIC_BASE_ALPHA`` (and tint to
        ``NATIVE_ACRYLIC_TINT_ALPHA``) and the OS blurred backdrop
        reads through cleanly.
        """
        if self._native_acrylic == enabled:
            return
        self._native_acrylic = enabled
        self.update()

    def install_dwm_chrome(self) -> bool:
        """Idempotent install of acrylic blur + DWM corner rounding.

        No-op for embedded children (``not self.isWindow()``). Returns
        ``True`` once acrylic is verified to have taken effect on the
        HWND; the boolean is also forwarded to ``set_native_acrylic``
        so the painted alphas drop to let the backdrop show through.
        """
        if not self.isWindow():
            return False
        if self._dwm_chrome_installed:
            return self._native_acrylic
        ok = _install_acrylic_blur_for_popup(self, tint_alpha=self.DWM_TINT_ALPHA)
        _install_dialog_chrome_for_popup(
            self, corner_preference=self.DWM_CORNER_PREFERENCE
        )
        self._dwm_chrome_installed = True
        if ok:
            self.set_native_acrylic(True)
        return ok

    # ---- paint pipeline ---------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        del event
        dpr = self.devicePixelRatioF()
        buffer = QImage(
            max(1, int(self.width() * dpr)),
            max(1, int(self.height() * dpr)),
            QImage.Format_ARGB32_Premultiplied,
        )
        buffer.setDevicePixelRatio(dpr)
        buffer.fill(Qt.transparent)

        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)

        painter = QPainter(buffer)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setClipPath(path)

        base, border, tint = self._resolve_palette()
        if self._native_acrylic:
            base = QColor(base)
            tint = QColor(tint)
            border = QColor(border)
            base.setAlpha(self.NATIVE_ACRYLIC_BASE_ALPHA)
            tint.setAlpha(self.NATIVE_ACRYLIC_TINT_ALPHA)
            border.setAlpha(min(178, max(148, border.alpha())))
        painter.fillPath(path, base)
        painter.fillPath(path, tint)
        painter.setClipping(False)

        border_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border_path = QPainterPath()
        border_path.addRoundedRect(
            border_rect, self.BORDER_RADIUS, self.BORDER_RADIUS
        )
        painter.setPen(QPen(border, 1.0))
        painter.drawPath(border_path)

        inner = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner, self.INNER_RADIUS, self.INNER_RADIUS)
        painter.setPen(QPen(self.INNER_COLOR, 1.0))
        painter.drawPath(inner_path)
        painter.end()

        target = QPainter(self)
        # Top-level windows have real HWND alpha — replace the surface
        # with our buffer (corners go transparent → desktop / acrylic
        # backdrop shows through). Embedded children don't get that
        # alpha propagation reliably; SourceOver leaves the QSS-painted
        # parent fill in place at the corners and only stamps our
        # rounded shape on top.
        if self.isWindow():
            target.setCompositionMode(QPainter.CompositionMode_Source)
        else:
            target.setCompositionMode(QPainter.CompositionMode_SourceOver)
        target.drawImage(0, 0, buffer)
        target.end()

    # ---- behaviour hooks --------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.DISMISS_ON_ESCAPE and event.key() == Qt.Key_Escape:
            event.accept()
            self.dismiss_requested.emit()
            return
        super().keyPressEvent(event)

    def event(self, event) -> bool:
        if (
            self.DISMISS_ON_DEACTIVATE
            and event.type() == QEvent.WindowDeactivate
            and self.isVisible()
        ):
            self.dismiss_requested.emit()
        return super().event(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if self.ABSORB_DOUBLE_CLICK:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
