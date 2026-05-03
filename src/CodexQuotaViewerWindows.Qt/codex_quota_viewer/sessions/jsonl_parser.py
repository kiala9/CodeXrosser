from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ._perf import _perf_timer
from .models import (
    ParsedSessionCatalog,
    SessionFileSummary,
    SessionTimelineItem,
    SessionTimelinePage,
)

try:
    import orjson as _orjson  # type: ignore[import-not-found]
    _json_loads: Callable[[str | bytes], Any] = _orjson.loads
except ImportError:
    _json_loads = json.loads


DEFAULT_TIMELINE_PAGE_SIZE = 100000
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
                    fallback_messages.append(
                        _TimelineDraft(
                            id=f"event-{sequence + 1}",
                            order=sequence,
                            timestamp=timestamp,
                            type="message:user" if payload_type == "user_message" else "message:assistant",
                            text=message,
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
                    full_text = _normalize_message(_read_response_message_text(payload))
                    if not full_text:
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

    def to_item(self) -> SessionTimelineItem:
        return SessionTimelineItem(
            id=self.id,
            type=self.type,  # type: ignore[arg-type]
            timestamp=self.timestamp,
            text=self.text,
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


def _read_response_message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    segments: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            segments.append(text)
            continue
        nested = part.get("content")
        if isinstance(nested, str):
            segments.append(nested)
    return "\n".join(segment for segment in segments if segment).strip()


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
