from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SessionApiErrorCode = str


@dataclass(frozen=True)
class SessionError(Exception):
    code: SessionApiErrorCode
    message: str
    status_code: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "error": self.message, "details": dict(self.details)}


def unknown_session(session_id: str) -> SessionError:
    return SessionError(
        code="unknown_session",
        message=f"Unknown session: {session_id}",
        status_code=404,
        details={"sessionId": session_id},
    )


def path_outside_managed_root(managed_root: str, candidate: str, resolved: str) -> SessionError:
    return SessionError(
        code="path_outside_managed_root",
        message=f"Path is outside managed root: {candidate}",
        status_code=400,
        details={
            "managedRoot": managed_root,
            "candidatePath": candidate,
            "resolvedCandidatePath": resolved,
        },
    )


def managed_session_path_outside(label: str, **detail: Any) -> SessionError:
    return SessionError(
        code="managed_session_path_outside",
        message=f"Session {label} file path escapes the managed root; refusing to continue.",
        status_code=400,
        details={"label": label, **detail},
    )


def active_session_cannot_be_archived(session_id: str) -> SessionError:
    return SessionError(
        code="active_session_cannot_be_archived",
        message="Session is not active and cannot be archived.",
        status_code=409,
        details={"sessionId": session_id},
    )


def active_session_must_be_deleted_before_purge(session_id: str) -> SessionError:
    return SessionError(
        code="active_session_must_be_deleted_before_purge",
        message="Active sessions must be deleted before purge.",
        status_code=409,
        details={"sessionId": session_id},
    )


def session_has_no_file_to_delete(session_id: str) -> SessionError:
    return SessionError(
        code="session_has_no_file_to_delete",
        message="Session has no file available to delete.",
        status_code=409,
        details={"sessionId": session_id},
    )


def session_is_not_restorable(session_id: str) -> SessionError:
    return SessionError(
        code="session_is_not_restorable",
        message="Session is not restorable.",
        status_code=409,
        details={"sessionId": session_id},
    )


def unsupported_restore_mode(mode: str) -> SessionError:
    return SessionError(
        code="unsupported_restore_mode",
        message=f"Restore mode is not supported: {mode}",
        status_code=400,
        details={"restoreMode": mode},
    )


def rebind_requires_target() -> SessionError:
    return SessionError(
        code="rebind_requires_target",
        message="Rebind requires a target project directory.",
        status_code=400,
    )


def restore_target_missing_directory(path: str) -> SessionError:
    return SessionError(
        code="restore_target_missing_directory",
        message="Restore target directory does not exist.",
        status_code=400,
        details={"candidatePath": path},
    )


def restore_target_not_directory(path: str) -> SessionError:
    return SessionError(
        code="restore_target_not_directory",
        message="Restore target path is not a directory.",
        status_code=400,
        details={"candidatePath": path},
    )


def restore_target_permission_denied(path: str) -> SessionError:
    return SessionError(
        code="restore_target_permission_denied",
        message="Restore target directory cannot be accessed.",
        status_code=400,
        details={"candidatePath": path},
    )
