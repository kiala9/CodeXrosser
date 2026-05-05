from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .jsonl_parser import parse_session_catalog
from .models import CatalogSessionEntry, ParsedSessionCatalog, SessionRecord
from .paths import ensure_inside_realpath, shell_quote


SESSION_RELATIVE_PATTERN = re.compile(r"^rollout-.+-(?P<id>[0-9a-fA-F-]+)\.jsonl$")


def collect_sessions(
    root: Path,
    *,
    skip_paths: frozenset[Path] | set[Path] | None = None,
    progress_cb: Callable[[Path], None] | None = None,
) -> list[tuple[Path, ParsedSessionCatalog, int]]:
    """Walk session JSONL files under ``root`` and parse each.

    Returns ``[(file_path, parsed, mtime_ns), ...]`` for every file that
    parsed successfully. ``mtime_ns`` is captured before parse so the
    incremental-rescan cache can use it as the freshness key on the next
    scan.

    Files whose absolute path appears in ``skip_paths`` are skipped
    without stat-ing or parsing — caller has already determined they are
    cache-fresh. The cache check itself happens upstream in
    ``SessionsManager._scan_and_index_sessions``.

    ``progress_cb`` (if given) is called once per file actually parsed
    (i.e. not skipped, not a stat/parse failure). Used by the streaming
    rescan path to push UI ticks during the long parse phase — without
    it, big first-install scans look frozen until the commit phase
    starts."""
    if not root.exists():
        return []
    skip = skip_paths or frozenset()
    entries: list[tuple[Path, ParsedSessionCatalog, int]] = []
    for file_path in _walk_jsonl_files(root):
        try:
            ensure_inside_realpath(root, file_path)
        except Exception:
            continue
        if file_path in skip:
            continue
        try:
            mtime_ns = file_path.stat().st_mtime_ns
        except OSError:
            continue
        try:
            parsed = parse_session_catalog(file_path)
        except OSError:
            continue
        if parsed is None:
            continue
        entries.append((file_path, parsed, mtime_ns))
        if progress_cb is not None:
            try:
                progress_cb(file_path)
            except Exception:
                # Progress callbacks are best-effort — a buggy UI hook
                # must not abort the rescan.
                pass
    return entries


def count_jsonl_files(
    root: Path,
    *,
    skip_paths: frozenset[Path] | set[Path] | None = None,
) -> int:
    """Cheap pre-walk used by the streaming rescan path to compute the
    parse-phase total before ``collect_sessions`` actually parses
    anything. Pure ``os.scandir`` recursion — no parse work, no stat
    beyond what the dir entry already gives us."""
    if not root.exists():
        return 0
    skip = skip_paths or frozenset()
    return sum(1 for path in _walk_jsonl_files(root) if path not in skip)


def build_fallback_relative_path(started_at: str, session_id: str) -> str:
    parsed = _parse_started_at(started_at)
    year = f"{parsed.year:04d}"
    month = f"{parsed.month:02d}"
    day = f"{parsed.day:02d}"
    safe_timestamp = started_at.replace(":", "-")
    file_name = f"rollout-{safe_timestamp}-{session_id}.jsonl"
    return os.path.join(year, month, day, file_name)


def resolve_session_relative_path(record: SessionRecord) -> str:
    return record.original_relative_path or build_fallback_relative_path(record.started_at, record.id)


def looks_canonical_session_relative_path(relative_path: str, session_id: str) -> bool:
    parts = relative_path.split(os.sep)
    if len(parts) < 4:
        parts = relative_path.replace("\\", "/").split("/")
    if len(parts) < 4:
        return False
    year, month, day, *rest = parts
    basename = os.sep.join(rest)
    if not (re.fullmatch(r"\d{4}", year) and re.fullmatch(r"\d{2}", month) and re.fullmatch(r"\d{2}", day)):
        return False
    name = os.path.basename(basename)
    return name.startswith("rollout-") and name.endswith(f"-{session_id}.jsonl")


def unique_session_ids(values: Iterable[str | None]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for value in values:
        if not value or value in seen_set:
            continue
        seen.append(value)
        seen_set.add(value)
    return seen


def copy_if_missing(source: Path, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def path_exists(file_path: Path | str | None) -> bool:
    if not file_path:
        return False
    try:
        return Path(file_path).exists()
    except OSError:
        return False


def build_resume_command(session_id: str, target_cwd: str | None = None) -> str:
    base = f"codex resume {shell_quote(session_id)}"
    if not target_cwd:
        return base
    return f"{base} -C {shell_quote(target_cwd)}"


def make_catalog_entry(
    parsed: ParsedSessionCatalog,
    *,
    active_path: Path | None,
    archive_path: Path | None,
    snapshot_path: Path | None,
    original_relative_path: str | None,
    status: str,
) -> CatalogSessionEntry:
    return CatalogSessionEntry(
        summary=parsed.summary,
        timeline=list(parsed.timeline),
        active_path=str(active_path) if active_path else None,
        archive_path=str(archive_path) if archive_path else None,
        snapshot_path=str(snapshot_path) if snapshot_path else None,
        original_relative_path=original_relative_path,
        status=status,  # type: ignore[arg-type]
    )


def _walk_jsonl_files(root: Path) -> list[Path]:
    files: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.name.endswith(".jsonl"):
                        files.append(Path(entry.path))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return files


def _parse_started_at(value: str) -> datetime:
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
