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


# ---- Role accent solids (Time Travel vertical view) --------------------
# Solid hex colours for the role-dot indicator in the Time Travel
# vertical view's row delegate. Translucent role tokens (PRIMARY_GHOST
# etc.) don't read at dot size against the dark frosted backdrop, so
# this scale gives solid colours hand-tuned for legibility. Hue family
# aligns with the bubble role palette but saturations are bumped.

ROLE_DOT_USER = "#0A84FF"
"""Row-dot colour for user blocks — vivid PRIMARY blue."""

ROLE_DOT_ASSISTANT = "#E0E4EC"
"""Row-dot colour for assistant blocks — light cool white-gray that
reads clearly on the dark frosted base."""

ROLE_DOT_TOOL = "#7E8696"
"""Row-dot colour for single tool_call blocks — mid cool gray, echoes
the TOOL_GHOST palette in solid form."""

ROLE_DOT_TOOL_GROUP = "#5A6273"
"""Row-dot colour for coalesced tool_group blocks — one step darker
than ``ROLE_DOT_TOOL`` so multi-call sections read as denser than
isolated tool calls."""

ROLE_FILTERED_OUT_ALPHA = 0.35
"""Opacity multiplier applied to vertical-view rows whose blocks are
filtered out by the panel-level filter. The list keeps showing them so
the global session shape stays intact."""


# ---- Frosted-glass surface ----------------------------------------------

SURFACE_FROSTED = "rgba(48, 48, 50, 200)"
"""Frosted-glass popup base (search popup, status banner).
Light neutral warm-grey — replaces the previous blue-grey that
pooled with the brand-blue border into a visually dirty mix. This
token stays in sync with ``frosted_surface._FrostedSurface.BASE_COLOR``."""

SURFACE_FROSTED_BORDER = "rgba(255, 255, 255, 45)"
"""Neutral white glass edge on frosted-glass surfaces. Replaces the
previous ``PRIMARY_TINT`` blue border which pooled with the dark blue
base to create a visually "dirty" mix on dark backgrounds. The white
edge reads as a crisp frosted rim while staying subtle enough not to
compete with content. Severity-driven surfaces (status pill per
``StatusPopupFrame._SURFACES``) keep their own per-severity border
colours."""


# ---- Semantic accents ---------------------------------------------------

SUCCESS = "#30D158"
WARNING = "#FFD60A"
CAUTION = "#FF9F0A"
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
    "ROLE_DOT_USER",
    "ROLE_DOT_ASSISTANT",
    "ROLE_DOT_TOOL",
    "ROLE_DOT_TOOL_GROUP",
    "ROLE_FILTERED_OUT_ALPHA",
    "SURFACE_FROSTED",
    "SURFACE_FROSTED_BORDER",
    "SUCCESS",
    "WARNING",
    "CAUTION",
    "DANGER",
    "DANGER_TINT",
]
