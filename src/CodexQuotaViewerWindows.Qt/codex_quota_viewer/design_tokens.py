"""Shared color and surface tokens for the Qt UI.

Single source of truth for the dark theme's brand color and translucent
overlays. Every QSS string in this package should reference these tokens
instead of inlining ``rgba(...)`` literals so a future palette change is a
one-line edit.

Hue policy:
  All translucent "blue" tints in the UI use the same hue (10, 132, 255 —
  iOS systemBlue). Drift used to mean three near-identical blues spread
  across hover borders, focus borders, and selected states; they all collapse
  here.

Alpha scale:
  Six steps from "barely visible" to "near-opaque". Snap any new ``rgba``
  to the closest step rather than introducing a new alpha value.
"""

from __future__ import annotations

# ---- Brand --------------------------------------------------------------

PRIMARY = "#0A84FF"
"""Solid Primary color — used as a button fill / strong border."""

PRIMARY_HOVER = "#409CFF"
"""Hover for solid Primary buttons. One step lighter than ``PRIMARY``."""

PRIMARY_PRESSED = "#006ADC"
"""Pressed for solid Primary buttons. One step darker than ``PRIMARY``."""


# ---- Translucent Primary scale ------------------------------------------

PRIMARY_GHOST = "rgba(10, 132, 255, 32)"
"""Barely-there hover background or subtle row tint."""

PRIMARY_TINT = "rgba(10, 132, 255, 64)"
"""Light tint — hover border on neutral surfaces."""

PRIMARY_SOFT = "rgba(10, 132, 255, 96)"
"""Selected row / chip-checked background."""

PRIMARY_BAND = "rgba(10, 132, 255, 130)"
"""Selected border / focus border."""

PRIMARY_STRONG = "rgba(10, 132, 255, 165)"
"""High-contrast border (e.g. floating action bar)."""

PRIMARY_SOLID_TINT = "rgba(10, 132, 255, 200)"
"""Half-opaque fill — rarely used; reserve for popup primary buttons."""


# ---- Surfaces (neutral) -------------------------------------------------

SURFACE_PANEL = "rgba(255, 255, 255, 12)"
"""Card / panel base fill (matches ``QFrame[panel="true"]``)."""

SURFACE_PANEL_BORDER = "rgba(255, 255, 255, 28)"
"""Card / panel border."""

SURFACE_CARD = "rgba(255, 255, 255, 13)"
"""Slightly elevated card variant (``QFrame[card="true"]``)."""


# ---- Role accents (translucent, low-saturation) -------------------------
# Pair convention mirrors PRIMARY_GHOST (alpha 32, fill) / PRIMARY_TINT
# (alpha 64, border). Reserved for non-brand role tints on the same
# card-style surface language as ``SURFACE_PANEL``.

TOOL_GHOST = "rgba(196, 200, 208, 28)"
"""Tool role accent fill — neutral cool gray, used by Tool / Shell
bubbles. Subtle enough to sit beneath assistant prose without
competing for attention; replaces the previous amber palette which
clashed with the rest of the cool/blue surface family."""

TOOL_TINT = "rgba(196, 200, 208, 96)"
"""Tool role accent border — pairs with ``TOOL_GHOST``. Border alpha
is bumped vs. other role tints (64) so the gray outline still reads
clearly against the dark app background despite the desaturated hue."""

SLATE_GHOST = "rgba(130, 160, 200, 32)"
"""Slate role accent fill — used by Environment / system bubbles."""

SLATE_TINT = "rgba(130, 160, 200, 64)"
"""Slate role accent border — pairs with ``SLATE_GHOST``."""


# ---- Frosted-glass surface ----------------------------------------------

SURFACE_FROSTED = "rgba(18, 39, 54, 222)"
"""Frosted-glass popup base (search popup, status banner)."""

SURFACE_FROSTED_BORDER = PRIMARY_STRONG
"""Border on frosted-glass surfaces — reuses ``PRIMARY_STRONG``."""


# ---- Semantic accents ---------------------------------------------------

SUCCESS = "#30D158"
WARNING = "#FFD60A"
DANGER = "#FF453A"
DANGER_TINT = "#FF6961"


__all__ = [
    "PRIMARY",
    "PRIMARY_HOVER",
    "PRIMARY_PRESSED",
    "PRIMARY_GHOST",
    "PRIMARY_TINT",
    "PRIMARY_SOFT",
    "PRIMARY_BAND",
    "PRIMARY_STRONG",
    "PRIMARY_SOLID_TINT",
    "SURFACE_PANEL",
    "SURFACE_PANEL_BORDER",
    "SURFACE_CARD",
    "TOOL_GHOST",
    "TOOL_TINT",
    "SLATE_GHOST",
    "SLATE_TINT",
    "SURFACE_FROSTED",
    "SURFACE_FROSTED_BORDER",
    "SUCCESS",
    "WARNING",
    "DANGER",
    "DANGER_TINT",
]
