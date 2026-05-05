from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ._perf import _perf_timer
from .models import (
    Attachment,
    ParsedSessionCatalog,
    SessionFileSummary,
    SessionTimelineItem,
    SessionTimelinePage,
)


# Manual cache buster for parser-output changes.
#
# Two mechanisms feed PARSER_VERSION:
#   1. Source-hash (dev): hashing this file auto-invalidates the cache
#      whenever you save the parser during dev — see
#      ``_compute_parser_version_from_source``.
#   2. Build-time fingerprint (installer): ``scripts/publish.ps1`` calls
#      ``scripts/compute-parser-fingerprint.py`` right before PyInstaller
#      and bundles the result as ``parser_fingerprint.json``. The runtime
#      reads it when ``sys.frozen`` is true. So installer releases get a
#      deterministic per-build version automatically — no manual ritual.
#
# This BUMP is a manual override for the rare case where parser-output
# changes live in a file the fingerprint doesn't cover (e.g. a new field
# added in ``models.py`` that the parser fills). Bump it then.
_PARSER_BUMP = 1


_FINGERPRINT_FILENAME = "parser_fingerprint.json"


def _fingerprint_path() -> Path:
    """Where the build-time fingerprint JSON lives. Separated so tests
    can monkeypatch the lookup without writing into the real package
    directory."""
    return Path(__file__).parent / _FINGERPRINT_FILENAME


def _compute_parser_version_from_source() -> int:
    """Pure source-hash + ``_PARSER_BUMP`` — what dev imports use, and
    what ``scripts/compute-parser-fingerprint.py`` calls at build time
    to bake the value into the bundled JSON.

    First 8 hex chars of sha256 over this file's bytes + the bump,
    cast to a 32-bit int for the SQLite ``parser_version`` column. Any
    change to this file (including comments/whitespace) flips the value
    and invalidates the incremental-rescan cache for every session row —
    the next access lazily reparses, then subsequent ones go fast."""
    try:
        source_bytes = Path(__file__).read_bytes()
    except OSError:
        # PyInstaller bundles don't keep .py source on disk. The
        # constant keeps the hash deterministic across frozen runs of
        # the same build (the fingerprint JSON above is the preferred
        # path; this is a last-resort fallback for broken bundles).
        source_bytes = b"codex_quota_viewer.sessions.jsonl_parser:frozen"
    digest = hashlib.sha256(
        source_bytes + str(_PARSER_BUMP).encode("ascii")
    ).hexdigest()[:8]
    return int(digest, 16)


def _compute_parser_version() -> int:
    """Resolve ``PARSER_VERSION`` at module load.

    Frozen PyInstaller bundles read the build-time fingerprint JSON
    that ``publish.ps1`` baked in — gives a deterministic version per
    release without manual ``_PARSER_BUMP`` updates.

    Source checkouts (and frozen builds whose fingerprint went missing
    or got corrupted) hash this file's source instead. In dev that
    auto-invalidates the cache whenever the parser is edited."""
    if getattr(sys, "frozen", False):
        try:
            data = json.loads(_fingerprint_path().read_text(encoding="utf-8"))
            version = int(data["parser_version"])
            if 0 <= version <= 0xFFFFFFFF:
                return version
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return _compute_parser_version_from_source()


PARSER_VERSION: int = _compute_parser_version()


_DATA_URI_MIME_RE = re.compile(r"^data:(image/[a-z0-9.+\-]+);base64,", re.IGNORECASE)
_GENERIC_DATA_URI_MIME_RE = re.compile(r"^data:([a-z0-9.+\-]+/[a-z0-9.+\-]+);base64,", re.IGNORECASE)
# Matches both the bare ``<image>`` placeholder Codex used historically
# and the newer ``<image name=[Image #N]>`` form. Anchored to the entire
# stripped text so user prose like ``check the <image> tag in HTML``
# never accidentally fires the strip rule (the rule is also gated on
# adjacency to a real ``input_image`` part — see
# ``_read_response_message_content``).
_IMAGE_OPEN_BRACKET_RE = re.compile(r"^<image\b[^>]*>$", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)")


# Codex Desktop serialises @-file mentions as a single ``input_text``
# content part with a markdown-style preamble — NOT as structured
# ``input_file`` content parts. The format is:
#
#     # Files mentioned by the user:
#
#     ## <filename>: <full_path>
#     ## <filename>: <full_path>
#
#     ## My request for Codex:
#     <user prompt body>
#
# We pattern-match this and lift each file reference into a real
# ``Attachment(kind="file")`` so the renderer can show a proper file
# card under the message. The ``Files mentioned`` header and the file
# list are stripped from the displayed text; the trailing prompt body
# survives as the message text. Matching is anchored at the start of
# the text so a user prose message that *quotes* the same header does
# not get rewritten.
_FILES_MENTIONED_HEADER_RE = re.compile(
    r"^\s*#\s+Files\s+mentioned\s+by\s+the\s+user:\s*\n",
    re.IGNORECASE,
)
_REQUEST_HEADER_RE = re.compile(
    r"^\s*##\s+My\s+request\s+for\s+Codex:\s*(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
_FILE_MENTION_LINE_RE = re.compile(
    r"^\s*##\s+(?P<name>[^\n:]+?):\s*(?P<path>\S[^\n]*?)\s*$",
    re.MULTILINE,
)
_FILE_EXTENSION_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".py": "text/x-python",
    ".html": "text/html",
    ".css": "text/css",
    ".csv": "text/csv",
}

try:
    import orjson as _orjson  # type: ignore[import-not-found]
    _json_loads: Callable[[str | bytes], Any] = _orjson.loads
except ImportError:
    _json_loads = json.loads


# Detail views should never default to a near-full timeline load. The UI seeds
# the panel from a bounded tail page and pages older rows on demand.
DEFAULT_TIMELINE_PAGE_SIZE = 64
MAX_TIMELINE_PAGE_SIZE = 500
_PARSER_CACHE_LIMIT = 128
_EPOCH_TIMESTAMP = "1970-01-01T00:00:00.000Z"


@dataclass(frozen=True)
class SessionMetaSnapshot:
    id: str
    started_at: str
    cwd: str
    source: Any
    cli_version: str
    model_provider: str
    model: str | None
    reasoning_effort: str | None
    approval_mode: str | None
    sandbox_policy: str | None
    memory_mode: str | None
    agent_path: str | None


@dataclass
class _CachedValue:
    size_bytes: int
    mtime_ns: int
    value: ParsedSessionCatalog | None


_session_catalog_cache: "OrderedDict[str, _CachedValue]" = OrderedDict()


def parse_session_catalog(file_path: Path) -> ParsedSessionCatalog | None:
    file_path = Path(file_path)
    try:
        stats = file_path.stat()
    except FileNotFoundError:
        return None
    cached = _read_cache(str(file_path), stats.st_size, stats.st_mtime_ns)
    if cached is not None:
        return cached.value

    meta: dict[str, Any] | None = None
    line_count = 0
    event_count = 0
    tool_call_count = 0
    user_prompt_excerpt = ""
    latest_agent_message_excerpt = ""
    response_user_excerpt = ""
    response_assistant_excerpt = ""
    response_messages: list[_TimelineDraft] = []
    fallback_messages: list[_TimelineDraft] = []
    tool_calls: list[_ToolDraft] = []
    tool_call_index: dict[str, int] = {}
    sequence = 0

    with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle, _perf_timer(
        "jsonl_parser.parse_session_catalog", path=file_path.name, size=stats.st_size
    ):
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            line_count += 1
            try:
                entry = _json_loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            timestamp = _normalize_optional_string(entry.get("timestamp"), _EPOCH_TIMESTAMP)

            if entry_type == "session_meta" and meta is None:
                payload = entry.get("payload") or {}
                if isinstance(payload, dict):
                    meta = payload
                continue

            if entry_type == "event_msg":
                event_count += 1
                payload = entry.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                message = _normalize_message(_read_message(payload.get("message")))
                if payload_type == "user_message" and not user_prompt_excerpt:
                    user_prompt_excerpt = _truncate_message(message)
                if payload_type == "agent_message":
                    latest_agent_message_excerpt = _truncate_message(message)
                if message and payload_type in ("user_message", "agent_message"):
                    # Lift Codex Desktop's @-file mention markdown out of
                    # the user_message string the same way we do for
                    # response_item content. Without this, the
                    # event_msg twin keeps the raw markdown body while
                    # the response_item twin has it stripped + holds
                    # attachments — they end up with different dedup
                    # keys and BOTH bubbles render. With both paths
                    # producing the same post-extraction text, dedup
                    # collapses them to a single bubble (the
                    # response_item version wins because it sorts
                    # earlier and carries the structured image
                    # attachment data the event_msg twin can't see).
                    extracted_attachments: tuple[Attachment, ...] = ()
                    cleaned_message = message
                    if payload_type == "user_message":
                        cleaned_message, extracted_attachments = _extract_files_mentioned(message)
                    cleaned_message, markdown_attachments = _extract_markdown_image_attachments(
                        cleaned_message
                    )
                    if markdown_attachments:
                        extracted_attachments = extracted_attachments + markdown_attachments
                    fallback_messages.append(
                        _TimelineDraft(
                            id=f"event-{sequence + 1}",
                            order=sequence,
                            timestamp=timestamp,
                            type="message:user" if payload_type == "user_message" else "message:assistant",
                            text=cleaned_message,
                            attachments=extracted_attachments,
                        )
                    )
                    sequence += 1
                continue

            if entry_type == "response_item":
                payload = entry.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if payload_type == "function_call":
                    tool_call_count += 1
                    call_id = _normalize_optional_string(payload.get("call_id"), f"tool-{sequence + 1}")
                    input_text = _normalize_message(_normalize_optional_string(payload.get("arguments"), ""))
                    tool_name = _normalize_optional_string(payload.get("name"), "unknown_tool")
                    tool_call_index[call_id] = len(tool_calls)
                    tool_calls.append(
                        _ToolDraft(
                            id=f"tool-{sequence + 1}",
                            order=sequence,
                            timestamp=timestamp,
                            tool_name=tool_name,
                            summary=_build_tool_summary(tool_name, input_text, ""),
                            input=input_text,
                            output="",
                            status="pending",
                        )
                    )
                    sequence += 1
                elif payload_type == "message":
                    # Keep the full text on the timeline draft so it matches
                    # the event_msg twin in dedup (which also carries the full
                    # text). Previously we stored ``_truncate_message(...)``
                    # here, which clamped to 180 chars — the cut routinely
                    # fell mid-markdown-table or mid-code-fence and Qt's
                    # setMarkdown went pathological on the malformed result.
                    # The truncated form is still used for the session-list
                    # excerpt, but the timeline keeps the full body.
                    raw_text, attachments = _read_response_message_content(payload)
                    full_text = _normalize_message(raw_text)
                    if not full_text and not attachments:
                        continue
                    role = payload.get("role")
                    if role == "user" and not response_user_excerpt:
                        response_user_excerpt = _truncate_message(full_text)
                    if role == "assistant":
                        response_assistant_excerpt = _truncate_message(full_text)
                    if role in ("user", "assistant"):
                        response_messages.append(
                            _TimelineDraft(
                                id=f"message-{sequence + 1}",
                                order=sequence,
                                timestamp=timestamp,
                                type="message:user" if role == "user" else "message:assistant",
                                text=full_text,
                                attachments=attachments,
                            )
                        )
                        sequence += 1
                elif payload_type == "function_call_output":
                    call_id = _normalize_optional_string(payload.get("call_id"), "")
                    tool_index = tool_call_index.get(call_id)
                    if tool_index is None or tool_index >= len(tool_calls):
                        continue
                    output_text = _normalize_message(_normalize_optional_string(payload.get("output"), ""))
                    existing = tool_calls[tool_index]
                    tool_calls[tool_index] = _ToolDraft(
                        id=existing.id,
                        order=existing.order,
                        timestamp=existing.timestamp,
                        tool_name=existing.tool_name,
                        summary=_build_tool_summary(existing.tool_name, existing.input, output_text),
                        input=existing.input,
                        output=output_text,
                        status=_read_tool_status(payload.get("output")),
                    )

    meta_payload = meta or {}
    session_id = _normalize_required_string(meta_payload.get("id"))
    started_at = _normalize_required_string(meta_payload.get("timestamp"))
    cwd = _normalize_required_string(meta_payload.get("cwd"))
    if not session_id or not started_at or not cwd:
        _write_cache(str(file_path), stats.st_size, stats.st_mtime_ns, None)
        return None

    summary = SessionFileSummary(
        id=session_id,
        cwd=cwd,
        started_at=started_at,
        originator=_normalize_optional_string(meta_payload.get("originator"), "Unknown"),
        source=_normalize_source(meta_payload.get("source")),
        cli_version=_normalize_optional_string(
            meta_payload.get("cli_version") or meta_payload.get("cliVersion"),
            "unknown",
        ),
        model_provider=_normalize_optional_string(
            meta_payload.get("model_provider") or meta_payload.get("modelProvider"),
            "unknown",
        ),
        size_bytes=stats.st_size,
        line_count=line_count,
        event_count=event_count,
        tool_call_count=tool_call_count,
        user_prompt_excerpt=user_prompt_excerpt or response_user_excerpt,
        latest_agent_message_excerpt=latest_agent_message_excerpt or response_assistant_excerpt,
    )

    timeline = _dedupe_timeline_drafts([*fallback_messages, *response_messages, *tool_calls])
    parsed = ParsedSessionCatalog(summary=summary, timeline=[draft.to_item() for draft in timeline])
    _write_cache(str(file_path), stats.st_size, stats.st_mtime_ns, parsed)
    return parsed


def parse_session_file(file_path: Path) -> SessionFileSummary | None:
    parsed = parse_session_catalog(file_path)
    return parsed.summary if parsed else None


def parse_session_timeline(file_path: Path) -> list[SessionTimelineItem]:
    parsed = parse_session_catalog(file_path)
    return list(parsed.timeline) if parsed else []


def parse_session_timeline_page(
    file_path: Path,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> SessionTimelinePage:
    """**Test-only.** Do NOT call from any UI hot path.

    This is a "parse the entire JSONL then slice" implementation — every
    call constructs the full ``ParsedSessionCatalog`` in memory before
    returning the requested page. For UI pagination use the SQLite
    path: ``SessionsManager.get_session_timeline_page`` →
    ``SessionRepository.list_timeline_page``, which reads only the
    requested rows.

    Kept around so the manager-level parser tests can verify offset/
    limit behaviour against fixture JSONL without going through the
    repository round-trip."""
    items = parse_session_timeline(file_path)
    start = max(offset or 0, 0)
    page_size = clamp_timeline_page_size(limit)
    page_items = items[start : start + page_size]
    total = len(items)
    next_offset = start + page_size if start + page_size < total else None
    return SessionTimelinePage(items=page_items, total=total, next_offset=next_offset)


def read_session_meta_snapshot(file_path: Path) -> SessionMetaSnapshot | None:
    file_path = Path(file_path)
    if not file_path.exists():
        return None
    with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entry = _json_loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "session_meta":
                continue
            payload = entry.get("payload") or {}
            if not isinstance(payload, dict):
                return None
            session_id = _normalize_required_string(payload.get("id"))
            started_at = _normalize_required_string(payload.get("timestamp"))
            cwd = _normalize_required_string(payload.get("cwd"))
            if not session_id or not started_at or not cwd:
                return None
            return SessionMetaSnapshot(
                id=session_id,
                started_at=started_at,
                cwd=cwd,
                source=payload.get("source") if payload.get("source") is not None else "vscode",
                cli_version=_normalize_optional_string(
                    payload.get("cli_version") or payload.get("cliVersion"),
                    "unknown",
                ),
                model_provider=_normalize_optional_string(
                    payload.get("model_provider") or payload.get("modelProvider"),
                    "unknown",
                ),
                model=_normalize_nullable_string(payload.get("model")),
                reasoning_effort=_normalize_nullable_string(
                    payload.get("reasoning_effort") or payload.get("reasoningEffort"),
                ),
                approval_mode=_normalize_nullable_string(
                    payload.get("approval_mode") or payload.get("approvalMode"),
                ),
                sandbox_policy=_normalize_nullable_string(
                    payload.get("sandbox_policy") or payload.get("sandboxPolicy"),
                ),
                memory_mode=_normalize_nullable_string(
                    payload.get("memory_mode") or payload.get("memoryMode"),
                ),
                agent_path=_normalize_nullable_string(
                    payload.get("agent_path") or payload.get("agentPath"),
                ),
            )
    return None


def clamp_timeline_page_size(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_TIMELINE_PAGE_SIZE
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_TIMELINE_PAGE_SIZE
    return min(max(value, 1), MAX_TIMELINE_PAGE_SIZE)


def clear_parser_cache() -> None:
    _session_catalog_cache.clear()


@dataclass
class _TimelineDraft:
    id: str
    order: int
    timestamp: str
    type: str
    text: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)

    def to_item(self) -> SessionTimelineItem:
        return SessionTimelineItem(
            id=self.id,
            type=self.type,  # type: ignore[arg-type]
            timestamp=self.timestamp,
            text=self.text,
            attachments=self.attachments,
        )


@dataclass
class _ToolDraft:
    id: str
    order: int
    timestamp: str
    tool_name: str
    summary: str
    input: str
    output: str
    status: str

    @property
    def type(self) -> str:
        return "tool_call"

    def to_item(self) -> SessionTimelineItem:
        status = self.status if self.status in ("pending", "completed", "errored") else "pending"
        return SessionTimelineItem(
            id=self.id,
            type="tool_call",
            timestamp=self.timestamp,
            tool_name=self.tool_name,
            summary=self.summary or self.tool_name or "unknown_tool",
            input=self.input,
            output=self.output,
            status=status,  # type: ignore[arg-type]
        )


def _read_message(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _read_response_message_content(
    payload: dict[str, Any],
) -> tuple[str, tuple[Attachment, ...]]:
    """Extract message text and attachments from a response_item ``message``.

    The Codex Desktop client wraps inline screenshots with literal
    ``<image>`` / ``</image>`` text tokens that bracket the actual
    ``input_image`` content part. These bracket tokens are stripped only
    when adjacent to a real image part — user prose containing other
    angle-bracketed text (HTML samples, generic placeholders) is left
    alone.
    """
    content = payload.get("content")
    if not isinstance(content, list):
        return "", ()
    parts: list[dict[str, Any]] = [part for part in content if isinstance(part, dict)]
    bracket_indices: set[int] = set()
    for index, part in enumerate(parts):
        if not _is_image_part(part):
            continue
        prev_index = index - 1
        if prev_index >= 0 and _is_image_open_bracket(parts[prev_index]):
            bracket_indices.add(prev_index)
        next_index = index + 1
        if next_index < len(parts) and _is_bracket_text(parts[next_index], "</image>"):
            bracket_indices.add(next_index)

    segments: list[str] = []
    attachments: list[Attachment] = []
    for index, part in enumerate(parts):
        if index in bracket_indices:
            continue
        if _is_image_part(part):
            attachment = _parse_image_part(part)
            if attachment is not None:
                attachments.append(attachment)
            continue
        if _is_file_part(part):
            attachment = _parse_file_part(part)
            if attachment is not None:
                attachments.append(attachment)
            continue
        text = part.get("text")
        if isinstance(text, str):
            segments.append(text)
            continue
        nested = part.get("content")
        if isinstance(nested, str):
            segments.append(nested)
    text_value = "\n".join(segment for segment in segments if segment).strip()
    text_value, mention_attachments = _extract_files_mentioned(text_value)
    if mention_attachments:
        attachments.extend(mention_attachments)
    text_value, markdown_attachments = _extract_markdown_image_attachments(text_value)
    if markdown_attachments:
        attachments.extend(markdown_attachments)
    return text_value, tuple(attachments)


def _read_response_message_text(payload: dict[str, Any]) -> str:
    """Backwards-compatible accessor used by tests / external callers."""
    return _read_response_message_content(payload)[0]


def _extract_files_mentioned(text: str) -> tuple[str, tuple[Attachment, ...]]:
    """Lift Codex Desktop's @-file mention markdown into ``Attachment``\\ s.

    The serialised shape is documented next to ``_FILES_MENTIONED_HEADER_RE``
    above. We anchor on the leading ``# Files mentioned by the user:``
    line so user prose that merely mentions or quotes the same header
    is left alone.
    """
    if not text:
        return text, ()
    header_match = _FILES_MENTIONED_HEADER_RE.match(text)
    if header_match is None:
        return text, ()
    body = text[header_match.end():]
    request_match = _REQUEST_HEADER_RE.search(body)
    if request_match is not None:
        files_block = body[: request_match.start()]
        request_body = body[request_match.end():].strip()
    else:
        files_block = body
        request_body = ""
    attachments: list[Attachment] = []
    for line_match in _FILE_MENTION_LINE_RE.finditer(files_block):
        name = line_match.group("name").strip()
        path_value = line_match.group("path").strip()
        if not name or not path_value:
            continue
        suffix = ""
        dot = name.rfind(".")
        if dot >= 0:
            suffix = name[dot:].lower()
        mime = _FILE_EXTENSION_MIME.get(suffix, "application/octet-stream")
        attachments.append(
            Attachment(
                kind="file",
                mime=mime,
                path=path_value,
                name=name,
                source="markdown",
            )
        )
    if not attachments:
        return text, ()
    return request_body, tuple(attachments)


def _extract_markdown_image_attachments(text: str) -> tuple[str, tuple[Attachment, ...]]:
    """Lift local markdown image links into ``Attachment`` entries.

    Local file paths and ``data:image/...`` data URIs become
    ``Attachment(kind="image", source="markdown")`` records and the
    original ``![]()`` fragment is stripped from the displayed text.
    Remote ``http(s)://`` images stay in the text so Qt can render them
    inline without a download/zoom card.
    """
    if not text or "![" not in text:
        return text, ()
    attachments: list[Attachment] = []

    def replace(match: re.Match[str]) -> str:
        src = match.group("src").strip()
        alt = match.group("alt") or ""
        if not src:
            return match.group(0)
        if src.startswith("data:"):
            mime_match = _GENERIC_DATA_URI_MIME_RE.match(src)
            mime = mime_match.group(1).lower() if mime_match else "image/octet-stream"
            attachments.append(
                Attachment(
                    kind="image",
                    mime=mime,
                    data_uri=src,
                    alt=alt or None,
                    source="markdown",
                )
            )
            return ""
        lowered = src.lower()
        if lowered.startswith(("http://", "https://")):
            return match.group(0)
        suffix = Path(src).suffix.lower()
        mime = _FILE_EXTENSION_MIME.get(suffix)
        if mime is not None and mime.startswith("image/"):
            attachments.append(
                Attachment(
                    kind="image",
                    mime=mime,
                    path=src,
                    alt=alt or None,
                    source="markdown",
                )
            )
            return ""
        return match.group(0)

    rewritten = _MARKDOWN_IMAGE_RE.sub(replace, text)
    return rewritten, tuple(attachments)


def _is_image_part(part: dict[str, Any]) -> bool:
    part_type = part.get("type")
    return part_type in ("input_image", "output_image", "image_url", "image")


def _is_file_part(part: dict[str, Any]) -> bool:
    part_type = part.get("type")
    return part_type in ("input_file", "file_url", "attachment")


def _is_bracket_text(part: dict[str, Any], expected: str) -> bool:
    if part.get("type") not in ("input_text", "text"):
        return False
    text = part.get("text")
    if not isinstance(text, str):
        return False
    return text.strip() == expected


def _is_image_open_bracket(part: dict[str, Any]) -> bool:
    """Match either ``<image>`` or ``<image name=[Image #N]>`` open tags."""
    if part.get("type") not in ("input_text", "text"):
        return False
    text = part.get("text")
    if not isinstance(text, str):
        return False
    return _IMAGE_OPEN_BRACKET_RE.match(text.strip()) is not None


def _parse_image_part(part: dict[str, Any]) -> Attachment | None:
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if not isinstance(image_url, str) or not image_url:
        return None
    if image_url.startswith("data:"):
        match = _DATA_URI_MIME_RE.match(image_url)
        mime = match.group(1).lower() if match else _fallback_data_uri_mime(image_url)
        return Attachment(
            kind="image",
            mime=mime,
            data_uri=image_url,
            alt=_string_or_none(part.get("detail")),
            source="payload",
        )
    return Attachment(
        kind="image",
        mime="image/unknown",
        path=image_url,
        alt=_string_or_none(part.get("detail")),
        source="payload",
    )


def _parse_file_part(part: dict[str, Any]) -> Attachment | None:
    file_url = part.get("file_url") or part.get("url")
    file_path = part.get("file_path") or part.get("path")
    name = _string_or_none(part.get("filename") or part.get("name"))
    mime = _string_or_none(part.get("mime") or part.get("media_type")) or "application/octet-stream"
    data_uri: str | None = None
    path: str | None = None
    if isinstance(file_url, str) and file_url:
        if file_url.startswith("data:"):
            data_uri = file_url
            mime_match = _GENERIC_DATA_URI_MIME_RE.match(file_url)
            if mime_match:
                mime = mime_match.group(1).lower()
        else:
            path = file_url
    if path is None and isinstance(file_path, str) and file_path:
        path = file_path
    if data_uri is None and path is None:
        return None
    return Attachment(
        kind="file",
        mime=mime,
        data_uri=data_uri,
        path=path,
        name=name,
        source="payload",
    )


def _fallback_data_uri_mime(data_uri: str) -> str:
    match = _GENERIC_DATA_URI_MIME_RE.match(data_uri)
    if match:
        return match.group(1).lower()
    return "image/octet-stream"


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _truncate_message(message: str) -> str:
    trimmed = _normalize_message(message)
    if len(trimmed) <= 180:
        return trimmed
    return trimmed[:177] + "..."


def _normalize_message(message: str) -> str:
    return message.strip() if isinstance(message, str) else ""


def _normalize_required_string(value: Any) -> str:
    normalized = _normalize_optional_string(value, "")
    return normalized if len(normalized) > 0 else ""


def _normalize_optional_string(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict) or isinstance(value, list):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return fallback
    return fallback


def _normalize_nullable_string(value: Any) -> str | None:
    normalized = _normalize_optional_string(value, "")
    return normalized if normalized else None


def _build_tool_summary(tool_name: str, input_text: str, output_text: str) -> str:
    detail = input_text or output_text
    return f"{tool_name} · {_truncate_message(detail)}" if detail else tool_name


def _read_tool_status(value: Any) -> str:
    normalized = _normalize_optional_string(value, "").lower()
    if "error" in normalized or "failed" in normalized or "exception" in normalized:
        return "errored"
    return "completed"


def _normalize_source(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        role = _read_subagent_role(value)
        if role:
            return f"subagent:{role}"
    return _normalize_optional_string(value, "unknown")


def _read_subagent_role(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    subagent = value.get("subagent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return None
    role = thread_spawn.get("agent_role")
    return role if isinstance(role, str) else None


def _dedupe_timeline_drafts(items: list[Any]) -> list[Any]:
    with _perf_timer("jsonl_parser.dedupe_timeline_drafts", count=len(items)):
        sorted_items = sorted(items, key=_timeline_sort_key)
        seen: set[tuple[str, ...]] = set()
        result: list[Any] = []
        for item in sorted_items:
            key = _timeline_dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


def _timeline_sort_key(item: Any) -> tuple[int, float, int]:
    parsed = _parse_iso_ms(item.timestamp)
    is_valid = 0 if parsed is not None else 1
    return (is_valid, parsed if parsed is not None else 0.0, item.order)


def _parse_iso_ms(value: str) -> float | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


def _timeline_dedupe_key(item: Any) -> tuple[str, ...]:
    # Codex JSONL emits each chat turn twice: once as an `event_msg`
    # (streaming notification) and once as a canonical `response_item`
    # (the message that was actually sent to the model). They have distinct
    # synthetic ids but identical text, so id-based dedup leaves the duplicate
    # in the timeline. For messages we therefore key on (type, normalized
    # text) so identical pairs collapse to one. For tool calls each id maps
    # to a logically distinct call, so id-based keying is still correct.
    item_type = getattr(item, "type", "")
    if item_type == "tool_call":
        return ("tool", item.id)
    text = (getattr(item, "text", "") or "").strip()
    return ("msg", item_type, text)


def _read_cache(file_path: str, size_bytes: int, mtime_ns: int) -> _CachedValue | None:
    cached = _session_catalog_cache.get(file_path)
    if cached and cached.size_bytes == size_bytes and cached.mtime_ns == mtime_ns:
        _session_catalog_cache.move_to_end(file_path)
        return cached
    return None


def _write_cache(
    file_path: str,
    size_bytes: int,
    mtime_ns: int,
    value: ParsedSessionCatalog | None,
) -> None:
    _session_catalog_cache.pop(file_path, None)
    _session_catalog_cache[file_path] = _CachedValue(
        size_bytes=size_bytes, mtime_ns=mtime_ns, value=value
    )
    while len(_session_catalog_cache) > _PARSER_CACHE_LIMIT:
        _session_catalog_cache.popitem(last=False)
