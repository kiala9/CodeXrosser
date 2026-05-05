from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SessionStatus = Literal["active", "archived", "deleted_pending_purge", "restorable"]


TimelineKind = Literal["message:user", "message:assistant", "tool_call"]


AttachmentKind = Literal["image", "file"]
AttachmentSource = Literal["payload", "markdown"]


@dataclass(frozen=True)
class Attachment:
    kind: AttachmentKind
    mime: str
    data_uri: str | None = None
    path: str | None = None
    alt: str | None = None
    name: str | None = None
    source: AttachmentSource = "payload"

    def to_json(self) -> dict[str, Any]:
        # ``data_uri`` is intentionally preserved for repository round-tripping
        # (image bytes live in SQLite alongside the timeline) but is dropped
        # from IPC payloads via ``to_ipc_json`` below.
        payload: dict[str, Any] = {
            "kind": self.kind,
            "mime": self.mime,
            "source": self.source,
        }
        if self.data_uri is not None:
            payload["dataUri"] = self.data_uri
        if self.path is not None:
            payload["path"] = self.path
        if self.alt is not None:
            payload["alt"] = self.alt
        if self.name is not None:
            payload["name"] = self.name
        return payload

    def to_ipc_json(self) -> dict[str, Any]:
        payload = self.to_json()
        payload.pop("dataUri", None)
        return payload

    @classmethod
    def from_json(cls, value: Any) -> "Attachment | None":
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        if kind not in ("image", "file"):
            return None
        mime = value.get("mime")
        if not isinstance(mime, str):
            return None
        source = value.get("source")
        if source not in ("payload", "markdown"):
            source = "payload"
        return cls(
            kind=kind,  # type: ignore[arg-type]
            mime=mime,
            data_uri=value.get("dataUri") if isinstance(value.get("dataUri"), str) else None,
            path=value.get("path") if isinstance(value.get("path"), str) else None,
            alt=value.get("alt") if isinstance(value.get("alt"), str) else None,
            name=value.get("name") if isinstance(value.get("name"), str) else None,
            source=source,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SessionFileSummary:
    id: str
    cwd: str
    started_at: str
    originator: str
    source: str
    cli_version: str
    model_provider: str
    size_bytes: int
    line_count: int
    event_count: int
    tool_call_count: int
    user_prompt_excerpt: str
    latest_agent_message_excerpt: str


@dataclass(frozen=True)
class SessionTimelineItem:
    id: str
    type: TimelineKind
    timestamp: str
    text: str = ""
    tool_name: str | None = None
    summary: str | None = None
    input: str = ""
    output: str = ""
    status: Literal["pending", "completed", "errored"] | None = None
    attachments: tuple[Attachment, ...] = ()

    def to_json(self) -> dict[str, Any]:
        if self.type == "tool_call":
            return {
                "id": self.id,
                "type": self.type,
                "timestamp": self.timestamp,
                "toolName": self.tool_name or "unknown_tool",
                "summary": self.summary or self.tool_name or "unknown_tool",
                "input": self.input,
                "output": self.output,
                "status": self.status or "pending",
            }
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "text": self.text,
        }
        if self.attachments:
            payload["attachments"] = [att.to_ipc_json() for att in self.attachments]
        return payload


@dataclass(frozen=True)
class ParsedSessionCatalog:
    summary: SessionFileSummary
    timeline: list[SessionTimelineItem]


@dataclass(frozen=True)
class SessionRecord:
    id: str
    file_path: str | None
    active_path: str | None
    archive_path: str | None
    snapshot_path: str | None
    original_relative_path: str | None
    cwd: str
    started_at: str
    originator: str
    source: str
    cli_version: str
    model_provider: str
    size_bytes: int
    line_count: int
    event_count: int
    tool_call_count: int
    user_prompt_excerpt: str
    latest_agent_message_excerpt: str
    status: SessionStatus
    created_at: str
    updated_at: str
    indexed_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filePath": self.file_path,
            "activePath": self.active_path,
            "archivePath": self.archive_path,
            "snapshotPath": self.snapshot_path,
            "originalRelativePath": self.original_relative_path,
            "cwd": self.cwd,
            "startedAt": self.started_at,
            "originator": self.originator,
            "source": self.source,
            "cliVersion": self.cli_version,
            "modelProvider": self.model_provider,
            "sizeBytes": self.size_bytes,
            "lineCount": self.line_count,
            "eventCount": self.event_count,
            "toolCallCount": self.tool_call_count,
            "userPromptExcerpt": self.user_prompt_excerpt,
            "latestAgentMessageExcerpt": self.latest_agent_message_excerpt,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "indexedAt": self.indexed_at,
        }


@dataclass(frozen=True)
class CatalogSessionEntry:
    summary: SessionFileSummary
    timeline: list[SessionTimelineItem]
    active_path: str | None
    archive_path: str | None
    snapshot_path: str | None
    original_relative_path: str | None
    status: SessionStatus


@dataclass(frozen=True)
class AuditEntry:
    id: int
    action: str
    session_id: str
    source_path: str | None
    target_path: str | None
    details: dict[str, Any]
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "sessionId": self.session_id,
            "sourcePath": self.source_path,
            "targetPath": self.target_path,
            "details": dict(self.details),
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class SessionTimelinePage:
    items: list[SessionTimelineItem]
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class SessionTimelineIndexItem:
    ordinal: int
    item_id: str
    type: TimelineKind
    timestamp: str
    preview: str
    tool_name: str | None = None


@dataclass(frozen=True)
class SessionAttachmentRow:
    """One attachment fetched directly from SQLite for Time Travel."""

    ordinal: int
    item_id: str
    type: TimelineKind
    timestamp: str
    attachment_index: int
    attachment: Attachment


@dataclass(frozen=True)
class SessionDetail:
    record: SessionRecord
    audit_entries: list[AuditEntry]
    timeline: list[SessionTimelineItem]
    timeline_total: int
    timeline_next_offset: int | None
    # Repository SQL offset of ``timeline[0]`` before any client-side
    # dedup. Lets the detail panel know where the loaded slice starts in
    # the full session and request older pages incrementally as the user
    # scrolls past the loaded edge. Defaults to 0 so existing callers
    # that hand over a complete timeline keep working unchanged.
    timeline_loaded_offset: int = 0


@dataclass(frozen=True)
class SessionFilters:
    query: str | None = None
    status: SessionStatus | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class BatchFailure:
    session_id: str
    error: str
    code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchResult:
    records: list[SessionRecord]
    failures: list[BatchFailure]


@dataclass(frozen=True)
class RestoreResult:
    record: SessionRecord
    resume_command: str
    launched: bool


RestoreMode = Literal["resume_only", "rebind_cwd"]
