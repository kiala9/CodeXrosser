"""Pure-Python Markdown exporter for Codex sessions.

Produces a self-contained .md document from a SessionRecord + timeline.
No Qt imports — keeps the unit tests headless and lets the module sit
cleanly underneath the Qt UI layer that calls into it.

The visual language (emoji-prefixed H2 headers per role, fenced code
blocks for tool input/output) is borrowed from claude-history-viewer's
exportToMarkdown so users coming from that tool feel at home.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePath
from typing import TYPE_CHECKING, Iterable, Literal, Protocol

from .models import (
    Attachment,
    SessionRecord,
    SessionTimelineItem,
    SessionTimelinePage,
)

if TYPE_CHECKING:
    from .manager import SessionsManager  # noqa: F401


ImageMode = Literal["skip", "inline"]


# Default size budget for inlining base64 images into the Markdown body.
# Picked to keep MDs small enough for GitHub previews and chat clients
# while still self-contained for typical Codex sessions (1–3 screenshots).
DEFAULT_INLINE_IMAGE_MAX_BYTES = 4 * 1024 * 1024


class _PageFetcher(Protocol):
    """Duck-type for the slice of SessionsManager exporter needs.

    Letting the unit tests substitute a fake without instantiating a
    real SQLite-backed manager.
    """

    def get_session_timeline_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> SessionTimelinePage: ...


def fetch_full_timeline(
    manager: _PageFetcher,
    session_id: str,
    *,
    page_size: int = 500,
) -> list[SessionTimelineItem]:
    """Drain every page of a session's timeline into a single list.

    The detail panel only loads a tail page on session open; export
    needs the full conversation, so we walk forward from offset 0 until
    the manager reports no more pages.
    """
    items: list[SessionTimelineItem] = []
    offset = 0
    while True:
        page = manager.get_session_timeline_page(
            session_id, offset=offset, limit=page_size
        )
        items.extend(page.items)
        if page.next_offset is None:
            break
        offset = page.next_offset
    return items


def make_export_filename_stub(record: SessionRecord) -> str:
    """Produce a filesystem-safe filename stem for an exported session.

    Shape: ``codex-session-{cwd_basename_slug}-{started_at_compact}``.
    Falls back to a session-id stem when ``cwd`` is empty.
    """
    cwd_slug = _slugify_cwd_basename(record.cwd)
    started_compact = _compact_started_at(record.started_at)
    if not cwd_slug:
        head = (record.id or "")[:12] or "unknown"
        return f"codex-session-{head}"
    if started_compact:
        return f"codex-session-{cwd_slug}-{started_compact}"
    return f"codex-session-{cwd_slug}"


def session_to_markdown(
    record: SessionRecord,
    timeline: Iterable[SessionTimelineItem],
    *,
    image_mode: ImageMode = "skip",
    inline_image_max_bytes: int = DEFAULT_INLINE_IMAGE_MAX_BYTES,
    now_iso: str | None = None,
) -> str:
    """Render a session as a single Markdown document.

    ``image_mode``:
        - ``"skip"``: image attachments are omitted entirely (file
          attachments still appear as ``📎 [name](path)`` references)
        - ``"inline"``: image attachments are inlined as
          ``![alt](data:...)`` until ``inline_image_max_bytes`` is
          consumed, after which an elision placeholder is emitted

    ``now_iso`` is exposed for deterministic tests; production callers
    should leave it None to capture the actual export time.
    """
    items = list(timeline)
    parts: list[str] = []
    parts.append(_render_header(record, now_iso=now_iso))
    if items:
        parts.append("")  # blank line between header and first message
        for item in items:
            block = _render_item(
                item,
                image_mode=image_mode,
                inline_image_max_bytes=inline_image_max_bytes,
            )
            if block:
                parts.append(block)
    body = "\n".join(parts).rstrip() + "\n"
    return body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_header(record: SessionRecord, *, now_iso: str | None) -> str:
    cwd_basename = _cwd_basename(record.cwd) or "unknown"
    started_short = (record.started_at or "")[:10] or "unknown"
    title = f"# Codex session — {cwd_basename} ({started_short})"

    excerpt = (record.user_prompt_excerpt or "").strip()
    excerpt_block = ""
    if excerpt:
        excerpt_one_line = excerpt.replace("\n", " ").strip()
        excerpt_block = f"\n\n> _{_md_escape_inline(excerpt_one_line)}_"

    exported_at = now_iso if now_iso is not None else datetime.now().isoformat(
        timespec="seconds"
    )
    meta_lines = [
        f"**Session ID:** {record.id}",
        f"**Project:** {record.cwd or '(unknown)'}",
        f"**Started:** {record.started_at or '(unknown)'}",
        f"**Originator / Source:** {record.originator or '?'} / {record.source or '?'}",
        f"**CLI version:** {record.cli_version or '(unknown)'}",
        f"**Model provider:** {record.model_provider or '(unknown)'}",
        f"**Events / Tool calls:** {record.event_count} / {record.tool_call_count}",
        f"**Status:** {record.status}",
        f"**Exported at:** {exported_at}",
    ]
    return f"{title}{excerpt_block}\n\n" + "\n".join(meta_lines) + "\n\n---"


def _render_item(
    item: SessionTimelineItem,
    *,
    image_mode: ImageMode,
    inline_image_max_bytes: int,
) -> str:
    if item.type == "message:user":
        return _render_message(
            item,
            heading="## 👤 User",
            image_mode=image_mode,
            inline_image_max_bytes=inline_image_max_bytes,
        )
    if item.type == "message:assistant":
        return _render_message(
            item,
            heading="## 🤖 Assistant",
            image_mode=image_mode,
            inline_image_max_bytes=inline_image_max_bytes,
        )
    if item.type == "tool_call":
        return _render_tool_call(item)
    return ""


def _render_message(
    item: SessionTimelineItem,
    *,
    heading: str,
    image_mode: ImageMode,
    inline_image_max_bytes: int,
) -> str:
    lines: list[str] = [heading]
    timestamp = item.timestamp.strip()
    if timestamp:
        lines.append(f"_{timestamp}_")
    text = (item.text or "").rstrip()
    if text:
        lines.append("")
        lines.append(text)

    attachment_blocks: list[str] = []
    remaining = inline_image_max_bytes
    for att in item.attachments:
        rendered, remaining = _render_attachment(
            att, image_mode=image_mode, remaining_budget=remaining
        )
        if rendered:
            attachment_blocks.append(rendered)
    if attachment_blocks:
        lines.append("")
        lines.extend(attachment_blocks)
    return "\n".join(lines) + "\n"


def _render_tool_call(item: SessionTimelineItem) -> str:
    tool_name = item.tool_name or "unknown_tool"
    lines: list[str] = [f"## 🔧 Tool · {tool_name}"]
    status = item.status or "pending"
    timestamp = item.timestamp.strip()
    if timestamp:
        lines.append(f"_{timestamp} — {status}_")
    else:
        lines.append(f"_{status}_")

    summary = (item.summary or "").strip()
    if summary and summary != tool_name:
        lines.append("")
        lines.append(summary)

    input_text = (item.input or "").rstrip()
    if input_text:
        lines.append("")
        lines.append("### Input")
        fence_lang = "json" if _looks_like_json(input_text) else ""
        lines.append(f"```{fence_lang}")
        lines.append(input_text)
        lines.append("```")

    output_text = (item.output or "").rstrip()
    if output_text:
        lines.append("")
        lines.append("### Output")
        lines.append("```")
        lines.append(output_text)
        lines.append("```")

    return "\n".join(lines) + "\n"


def _render_attachment(
    att: Attachment,
    *,
    image_mode: ImageMode,
    remaining_budget: int,
) -> tuple[str | None, int]:
    """Render a single attachment, returning (markdown_or_none, new_budget).

    Returning ``None`` means the attachment is silently dropped (e.g.
    skipping images in fast mode).
    """
    label = att.alt or att.name or att.path or _default_attachment_label(att)
    if att.kind == "image":
        if image_mode == "skip":
            return None, remaining_budget
        # inline mode
        if att.data_uri:
            cost = len(att.data_uri)
            if cost > remaining_budget:
                return (
                    f"_[image elided — exceeded inline limit ({label})]_",
                    remaining_budget,
                )
            return f"![{_md_escape_alt(label)}]({att.data_uri})", remaining_budget - cost
        if att.path:
            return (
                f"![{_md_escape_alt(label)}]({att.path}) _(local file)_",
                remaining_budget,
            )
        return f"_[image without source ({label})]_", remaining_budget
    # file attachments — same in both modes
    target = att.path or "(embedded)"
    return f"📎 [{_md_escape_alt(label)}]({target})", remaining_budget


def _default_attachment_label(att: Attachment) -> str:
    if att.mime:
        return att.mime
    return "attachment"


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in "{["


_FILENAME_DISALLOWED = re.compile(r"[^a-z0-9._-]+")


def _slugify_cwd_basename(cwd: str) -> str:
    basename = _cwd_basename(cwd)
    if not basename:
        return ""
    lowered = basename.lower()
    slug = _FILENAME_DISALLOWED.sub("-", lowered).strip("-")
    return slug[:40]


def _cwd_basename(cwd: str) -> str:
    if not cwd:
        return ""
    # Use PurePath so both Windows and POSIX separators are handled
    # without picking the wrong flavor on a non-native runner.
    candidate = cwd.rstrip("/\\")
    if not candidate:
        return ""
    return PurePath(candidate.replace("\\", "/")).name


def _compact_started_at(started_at: str) -> str:
    if not started_at:
        return ""
    # ISO-ish input; just strip the punctuation that's noisy in filenames.
    return re.sub(r"[^0-9]", "", started_at)[:14]


_INLINE_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-!])")


def _md_escape_inline(text: str) -> str:
    return _INLINE_ESCAPE.sub(r"\\\1", text)


def _md_escape_alt(text: str) -> str:
    """Escape alt text so square brackets don't break the link syntax."""
    return text.replace("[", "\\[").replace("]", "\\]")
