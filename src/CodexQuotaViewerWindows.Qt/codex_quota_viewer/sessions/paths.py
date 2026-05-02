from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import path_outside_managed_root


@dataclass(frozen=True)
class SessionRoots:
    sessions_root: Path
    archive_root: Path
    snapshot_root: Path
    database_path: Path


def build_session_roots(codex_home: Path, manager_home: Path) -> SessionRoots:
    return SessionRoots(
        sessions_root=codex_home / "sessions",
        archive_root=codex_home / "archived_sessions",
        snapshot_root=manager_home / "snapshots",
        database_path=manager_home / "index.db",
    )


def ensure_inside_path(root: Path, candidate: Path) -> Path:
    resolved_root = Path(os.path.abspath(root))
    resolved_candidate = Path(os.path.abspath(candidate))
    if resolved_candidate != resolved_root and not _is_subpath(resolved_root, resolved_candidate):
        raise path_outside_managed_root(str(resolved_root), str(candidate), str(resolved_candidate))
    return resolved_candidate


def ensure_inside_realpath(
    root: Path,
    candidate: Path,
    *,
    allow_missing_tail: bool = False,
) -> Path:
    resolved_root = Path(os.path.realpath(root))
    resolved_candidate = _resolve_candidate_realpath(candidate, allow_missing_tail)
    if resolved_candidate != resolved_root and not _is_subpath(resolved_root, resolved_candidate):
        raise path_outside_managed_root(str(resolved_root), str(candidate), str(resolved_candidate))
    return resolved_candidate


def session_archive_path(archive_root: Path, relative_path: str) -> Path:
    return archive_root / relative_path


def session_snapshot_path(snapshot_root: Path, session_id: str) -> Path:
    return snapshot_root / f"{session_id}.jsonl"


def shell_quote(value: str) -> str:
    if os.name == "nt":
        if all(_is_safe_win_char(ch) for ch in value):
            return value
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    if all(_is_safe_posix_char(ch) for ch in value):
        return value
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


def _resolve_candidate_realpath(candidate: Path, allow_missing_tail: bool) -> Path:
    resolved = Path(os.path.abspath(candidate))
    try:
        return Path(os.path.realpath(resolved, strict=False))
    except OSError:
        if not allow_missing_tail:
            raise
        return _resolve_with_missing_tail(resolved)


def _resolve_with_missing_tail(candidate: Path) -> Path:
    missing: list[str] = []
    cursor = candidate
    while True:
        try:
            real_cursor = Path(os.path.realpath(cursor, strict=True))
            for segment in reversed(missing):
                real_cursor = real_cursor / segment
            return real_cursor
        except OSError:
            parent = cursor.parent
            if parent == cursor:
                return Path(os.path.realpath(candidate, strict=False))
            missing.append(cursor.name)
            cursor = parent


def _is_subpath(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_safe_win_char(char: str) -> bool:
    return char.isalnum() or char in "._:-/\\@"


def _is_safe_posix_char(char: str) -> bool:
    return char.isalnum() or char in "._-/:@"
