from __future__ import annotations

from .errors import SessionError
from .helpers import build_resume_command
from .manager import SessionsManager, SessionsManagerConfig
from .models import (
    Attachment,
    AttachmentKind,
    AttachmentSource,
    AuditEntry,
    BatchFailure,
    BatchResult,
    CatalogSessionEntry,
    ParsedSessionCatalog,
    RestoreMode,
    RestoreResult,
    SessionAttachmentRow,
    SessionDetail,
    SessionFileSummary,
    SessionFilters,
    SessionRecord,
    SessionStatus,
    SessionTimelineIndexItem,
    SessionTimelineItem,
    SessionTimelinePage,
    TimelineKind,
)
from .paths import (
    SessionRoots,
    build_session_roots,
    ensure_inside_path,
    ensure_inside_realpath,
)
from .repository import SessionRepository

__all__ = [
    "Attachment",
    "AttachmentKind",
    "AttachmentSource",
    "AuditEntry",
    "BatchFailure",
    "BatchResult",
    "CatalogSessionEntry",
    "ParsedSessionCatalog",
    "RestoreMode",
    "RestoreResult",
    "SessionAttachmentRow",
    "SessionDetail",
    "SessionError",
    "SessionFileSummary",
    "SessionFilters",
    "SessionRecord",
    "SessionRepository",
    "SessionRoots",
    "SessionStatus",
    "SessionTimelineIndexItem",
    "SessionTimelineItem",
    "SessionTimelinePage",
    "SessionsManager",
    "SessionsManagerConfig",
    "TimelineKind",
    "build_resume_command",
    "build_session_roots",
    "ensure_inside_path",
    "ensure_inside_realpath",
]
