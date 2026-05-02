from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SessionStatus = Literal["active", "archived", "deleted_pending_purge", "restorable"]


TimelineKind = Literal["message:user", "message:assistant", "tool_call"]


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
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "text": self.text,
        }


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
class SessionDetail:
    record: SessionRecord
    audit_entries: list[AuditEntry]
    timeline: list[SessionTimelineItem]
    timeline_total: int
    timeline_next_offset: int | None


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
