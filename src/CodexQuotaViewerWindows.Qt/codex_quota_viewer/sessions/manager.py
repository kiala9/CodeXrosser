from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import errors
from ._perf import _perf_timer
from .helpers import (
    build_fallback_relative_path,
    build_resume_command,
    collect_sessions,
    count_jsonl_files,
    looks_canonical_session_relative_path,
    make_catalog_entry,
    resolve_session_relative_path,
    unique_session_ids,
)
from .jsonl_parser import (
    DEFAULT_TIMELINE_PAGE_SIZE,
    PARSER_VERSION,
    clamp_timeline_page_size,
    parse_session_catalog,
)
from .models import (
    AuditEntry,
    BatchFailure,
    BatchResult,
    CatalogSessionEntry,
    ParsedSessionCatalog,
    RestoreMode,
    RestoreResult,
    SessionAttachmentRow,
    SessionDetail,
    SessionFilters,
    SessionRecord,
    SessionTimelineIndexItem,
    SessionTimelinePage,
)
from .paths import (
    SessionRoots,
    build_session_roots,
    ensure_inside_realpath,
    session_archive_path,
    session_snapshot_path,
)
from .repository import SessionRepository


ResumeLauncher = Callable[[str, str | None], bool]


# Per-batch commit size for the streaming rescan path (P1.8). Smaller →
# more transactions (fsync overhead) and more frequent UI updates;
# larger → fewer commits but longer "no progress" gaps. 50 picked to
# keep batch fsync ~10ms on NTFS WAL while still feeling live to the UI.
_RESCAN_BATCH_SIZE = 50


@dataclass(frozen=True)
class SessionsManagerConfig:
    codex_home: Path
    manager_home: Path


class SessionsManager:
    def __init__(
        self,
        codex_home: Path,
        manager_home: Path,
        *,
        resume_launcher: ResumeLauncher | None = None,
    ):
        self.codex_home = Path(codex_home)
        self.manager_home = Path(manager_home)
        self.roots: SessionRoots = build_session_roots(self.codex_home, self.manager_home)
        self.manager_home.mkdir(parents=True, exist_ok=True)
        self.roots.archive_root.mkdir(parents=True, exist_ok=True)
        self.roots.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.repository = SessionRepository(self.roots.database_path)
        self._mutation_lock = threading.RLock()
        self._resume_launcher = resume_launcher

    def close(self) -> None:
        self.repository.close()

    def rescan(
        self,
        *,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> list[SessionRecord]:
        """Run a full incremental rescan.

        ``progress_cb(phase, done, total)`` (if given) is invoked across
        two phases:
          - ``phase="parsing"``: ``done`` is files parsed so far,
            ``total`` is total files to parse this run (excludes
            cache-skipped files).
          - ``phase="indexing"``: ``done`` is sessions committed so far
            (including cache-kept), ``total`` is sessions this run will
            touch (committed + kept).
        The two phases are emitted contiguously: parse ticks first,
        index ticks second. UI uses the phase to label the count
        differently."""
        with self._mutation_lock:
            self._scan_and_index_sessions(progress_cb=progress_cb)
            return self.repository.list_sessions()

    def list_sessions(self, filters: SessionFilters | None = None) -> list[SessionRecord]:
        return self.repository.list_sessions(filters)

    def get_session_detail(self, session_id: str) -> SessionDetail:
        record = self._require_session(session_id)
        # Lazy freshness check before serving detail. Two trigger conditions
        # share one reparse path:
        #   1) parser_version mismatch → indexed by an older parser, the
        #      DB timeline is structurally stale.
        #   2) on-disk mtime ≠ stored mtime → the jsonl was modified
        #      externally (most common: Codex CLI appending; also manual
        #      edits). The DB timeline is content-stale.
        # Both reuse `_reparse_one`, which is trigger-agnostic (it parses
        # + rewrites timeline_items + persists fresh mtime + parser_version).
        # Cost is one file parse (~50-200ms) hidden behind the existing
        # detail loading overlay — much better than forcing a full rescan
        # on every parser upgrade or external edit.
        db_parser_version, db_mtime_ns = self.repository.get_session_freshness(
            session_id
        )
        needs_reparse = db_parser_version != PARSER_VERSION
        if not needs_reparse:
            primary_str = (
                record.active_path
                or record.archive_path
                or record.snapshot_path
            )
            if primary_str:
                try:
                    disk_mtime_ns = Path(primary_str).stat().st_mtime_ns
                    if disk_mtime_ns != db_mtime_ns:
                        needs_reparse = True
                except OSError:
                    # File missing → fall through to DB load. Orphan row
                    # will be cleaned up by the next manual Rescan.
                    pass
        if needs_reparse:
            self._reparse_one(record)
            record = self._require_session(session_id)
        with _perf_timer("manager.get_session_detail", session_id=session_id):
            # Tail-load only the most recent page. Chat sessions are read
            # most-recent-first, and full-history loads at million-row scale
            # were blocking the UI thread on dedup + object construction
            # alone. The detail panel runs a sliding-window renderer over
            # whatever timeline we hand it, so a bounded tail keeps the
            # detail-open cost flat regardless of session size — and the
            # panel pages older history in via ``get_session_timeline_page``
            # when the user scrolls past the loaded edge.
            page = self.repository.list_timeline_tail(
                session_id, limit=DEFAULT_TIMELINE_PAGE_SIZE
            )
            deduped = _dedupe_persisted_timeline(page.items)
            audit_entries = self.repository.list_audit_entries(session_id)
            # Pre-dedup SQL offset of the slice we just loaded. Used by the
            # panel as the anchor for older-page requests; dedup reduces
            # the visible item count but does not move the SQL offset.
            loaded_offset = max(0, page.total - len(page.items))
            return SessionDetail(
                record=record,
                audit_entries=audit_entries,
                timeline=deduped,
                timeline_total=page.total,
                timeline_next_offset=page.next_offset,
                timeline_loaded_offset=loaded_offset,
            )

    def _reparse_one(self, record: SessionRecord) -> None:
        """Reparse a single session in place and persist the new
        timeline_items + cache fingerprints. Best-effort: file gone or
        parse failure leaves the DB row alone — the next access will
        retry, and a manual Rescan still cleans up orphans."""
        primary_str = (
            record.active_path or record.archive_path or record.snapshot_path
        )
        if not primary_str:
            return
        primary = Path(primary_str)
        try:
            parsed = parse_session_catalog(primary)
            if parsed is None:
                return
            mtime_ns = primary.stat().st_mtime_ns
        except OSError:
            return
        entry = make_catalog_entry(
            parsed,
            active_path=Path(record.active_path) if record.active_path else None,
            archive_path=(
                Path(record.archive_path) if record.archive_path else None
            ),
            snapshot_path=(
                Path(record.snapshot_path) if record.snapshot_path else None
            ),
            original_relative_path=record.original_relative_path,
            status=record.status,
        )
        self.repository.save_catalog_entry(
            entry,
            primary_mtime_ns=mtime_ns,
            parser_version=PARSER_VERSION,
        )

    def get_session_timeline_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> SessionTimelinePage:
        self._require_session(session_id)
        page = self.repository.list_timeline_page(
            session_id,
            offset=offset,
            limit=clamp_timeline_page_size(limit),
        )
        deduped = _dedupe_persisted_timeline(page.items)
        if deduped is page.items:
            return page
        return SessionTimelinePage(
            items=deduped,
            total=page.total,
            next_offset=page.next_offset,
        )

    def get_session_timeline_index(self, session_id: str) -> list[SessionTimelineIndexItem]:
        self._require_session(session_id)
        return self.repository.list_timeline_index(session_id)

    def list_session_attachments(self, session_id: str) -> list[SessionAttachmentRow]:
        return self.repository.list_session_attachments(session_id)

    def archive_session(self, session_id: str) -> SessionRecord:
        with self._mutation_lock:
            return self._archive_session_unsafe(session_id)

    def delete_session(self, session_id: str) -> SessionRecord:
        with self._mutation_lock:
            return self._delete_session_unsafe(session_id)

    def restore_session(
        self,
        session_id: str,
        *,
        restore_mode: RestoreMode = "resume_only",
        target_cwd: str | None = None,
        launch: bool = False,
    ) -> RestoreResult:
        with self._mutation_lock:
            return self._restore_session_unsafe(
                session_id,
                restore_mode=restore_mode,
                target_cwd=target_cwd,
                launch=launch,
            )

    def purge_session(self, session_id: str) -> dict[str, str]:
        with self._mutation_lock:
            return self._purge_session_unsafe(session_id)

    def batch_archive_sessions(self, session_ids: list[str]) -> BatchResult:
        with self._mutation_lock:
            return self._run_batch(session_ids, self._archive_session_unsafe)

    def batch_trash_sessions(self, session_ids: list[str]) -> BatchResult:
        with self._mutation_lock:
            return self._run_batch(session_ids, self._delete_session_unsafe)

    def batch_restore_sessions(self, session_ids: list[str]) -> BatchResult:
        def runner(sid: str) -> SessionRecord:
            return self._restore_session_unsafe(sid, restore_mode="resume_only").record

        with self._mutation_lock:
            return self._run_batch(session_ids, runner)

    def batch_purge_sessions(self, session_ids: list[str]) -> BatchResult:
        with self._mutation_lock:
            unique_ids = unique_session_ids(session_ids)
            failures: list[BatchFailure] = []
            for sid in unique_ids:
                try:
                    self._purge_session_unsafe(sid)
                except errors.SessionError as ex:
                    failures.append(_map_batch_failure(sid, ex))
                except Exception as ex:  # noqa: BLE001
                    failures.append(_map_batch_failure(sid, ex))
            return BatchResult(records=[], failures=failures)

    def _archive_session_unsafe(self, session_id: str) -> SessionRecord:
        self._ensure_roots()
        record = self._require_session(session_id)
        if not record.active_path:
            if record.archive_path:
                return record
            raise errors.active_session_cannot_be_archived(session_id)
        source_path = self._assert_managed_path(
            "active", self.roots.sessions_root, Path(record.active_path)
        )
        target_path = self._assert_managed_path(
            "archive",
            self.roots.archive_root,
            session_archive_path(self.roots.archive_root, resolve_session_relative_path(record)),
            allow_missing_tail=True,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, target_path)
        record_after = self.repository.update_session(
            session_id,
            {
                "active_path": None,
                "archive_path": str(target_path),
                "status": "archived",
            },
        )
        self.repository.insert_audit("archive", session_id, str(source_path), str(target_path))
        return record_after

    def _delete_session_unsafe(self, session_id: str) -> SessionRecord:
        self._ensure_roots()
        record = self._require_session(session_id)
        source_path = self._assert_managed_current_path(record)
        archive_target = self._assert_managed_path(
            "archive",
            self.roots.archive_root,
            session_archive_path(self.roots.archive_root, resolve_session_relative_path(record)),
            allow_missing_tail=True,
        )
        snapshot_target_initial = (
            Path(record.snapshot_path)
            if record.snapshot_path
            else session_snapshot_path(self.roots.snapshot_root, session_id)
        )
        snapshot_label = "snapshot"
        snapshot_path = self._assert_managed_path(
            snapshot_label,
            self.roots.snapshot_root,
            snapshot_target_initial,
            allow_missing_tail=record.snapshot_path is None,
        )
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot_path.exists():
            shutil.copyfile(source_path, snapshot_path)
        if str(source_path) != str(archive_target):
            archive_target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, archive_target)
        next_record = self.repository.update_session(
            session_id,
            {
                "active_path": None,
                "archive_path": str(archive_target),
                "snapshot_path": str(snapshot_path),
                "status": "deleted_pending_purge",
            },
        )
        self.repository.insert_audit(
            "delete",
            session_id,
            str(source_path),
            str(archive_target),
            {"snapshotPath": str(snapshot_path)},
        )
        return next_record

    def _restore_session_unsafe(
        self,
        session_id: str,
        *,
        restore_mode: RestoreMode,
        target_cwd: str | None = None,
        launch: bool = False,
    ) -> RestoreResult:
        self._ensure_roots()
        record = self._require_session(session_id)
        normalized_mode = self._normalize_restore_mode(restore_mode)
        is_already_active = bool(record.active_path)
        if is_already_active:
            source_path = self._assert_managed_path(
                "active", self.roots.sessions_root, Path(record.active_path)
            )
            restore_path = source_path
        else:
            source_path = self._assert_managed_restore_source(record)
            relative = record.original_relative_path or build_fallback_relative_path(
                record.started_at, record.id
            )
            restore_path = self._assert_managed_path(
                "active",
                self.roots.sessions_root,
                self.roots.sessions_root / relative,
                allow_missing_tail=True,
            )
        if target_cwd:
            self._validate_restore_target_directory(target_cwd)
        if normalized_mode == "rebind_cwd" and not target_cwd:
            raise errors.rebind_requires_target()

        if not is_already_active:
            restore_path.parent.mkdir(parents=True, exist_ok=True)
            if str(source_path) != str(restore_path):
                if record.archive_path and str(source_path) == str(Path(record.archive_path)):
                    os.replace(source_path, restore_path)
                else:
                    shutil.copyfile(source_path, restore_path)

        if normalized_mode == "rebind_cwd":
            assert target_cwd is not None
            _rewrite_session_meta_cwd(restore_path, target_cwd)

        if is_already_active:
            if normalized_mode == "rebind_cwd":
                next_record = self.repository.update_session(
                    record.id, {"cwd": target_cwd}
                )
            else:
                next_record = record
        else:
            mutation: dict[str, Any] = {
                "active_path": str(restore_path),
                "status": "active",
            }
            if record.archive_path and str(source_path) == str(Path(record.archive_path)):
                mutation["archive_path"] = None
            if normalized_mode == "rebind_cwd":
                mutation["cwd"] = target_cwd
            next_record = self.repository.update_session(record.id, mutation)

        resume_command = build_resume_command(
            record.id,
            target_cwd if normalized_mode == "resume_only" else None,
        )
        launched = False
        if launch and self._resume_launcher:
            try:
                launched = bool(self._resume_launcher(record.id, target_cwd))
            except Exception:
                launched = False
        self.repository.insert_audit(
            "restore",
            record.id,
            str(source_path),
            str(restore_path),
            {
                "targetCwd": target_cwd,
                "restoreMode": normalized_mode,
                "launched": launched,
            },
        )
        return RestoreResult(record=next_record, resume_command=resume_command, launched=launched)

    def _purge_session_unsafe(self, session_id: str) -> dict[str, str]:
        self._ensure_roots()
        record = self._require_session(session_id)
        if record.active_path:
            raise errors.active_session_must_be_deleted_before_purge(session_id)
        if record.archive_path:
            archive_path = self._assert_managed_path(
                "archive", self.roots.archive_root, Path(record.archive_path)
            )
            try:
                archive_path.unlink()
            except FileNotFoundError:
                pass
        if record.snapshot_path:
            snapshot_path = self._assert_managed_path(
                "snapshot", self.roots.snapshot_root, Path(record.snapshot_path)
            )
            try:
                snapshot_path.unlink()
            except FileNotFoundError:
                pass
        self.repository.insert_audit(
            "purge",
            session_id,
            record.archive_path,
            None,
            {"snapshotPath": record.snapshot_path},
        )
        self.repository.delete_session(session_id)
        return {"purgedId": session_id}

    def _scan_and_index_sessions(
        self,
        *,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> list[CatalogSessionEntry]:
        self._ensure_roots()

        # Incremental rescan cache check: pull DB metadata once, then for
        # each single-root session whose primary file's mtime + parser
        # fingerprint match what's recorded, skip re-parse. Multi-root
        # sessions (active + archive simultaneously, mid-transition) fall
        # through to fresh parse so the path/state combo stays accurate.
        skip_paths: set[Path] = set()
        kept_session_ids: set[str] = set()
        for meta in self.repository.list_session_scan_metadata():
            if meta.parser_version != PARSER_VERSION:
                continue
            present_paths = [
                p for p in (meta.active_path, meta.archive_path, meta.snapshot_path) if p
            ]
            if len(present_paths) != 1:
                continue
            primary = present_paths[0]
            try:
                st = os.stat(primary)
            except OSError:
                continue
            if st.st_mtime_ns == meta.primary_mtime_ns:
                skip_paths.add(Path(primary))
                kept_session_ids.add(meta.session_id)

        # Pre-walk all three roots so the parse-phase progress total is
        # known before we start parsing. The walk itself is os.scandir
        # only — no parse, no stat — so even thousands of files come in
        # under 100ms total. This trades a tiny up-front cost for
        # genuinely usable progress feedback during the long parse phase.
        parse_total = (
            count_jsonl_files(self.roots.sessions_root, skip_paths=skip_paths)
            + count_jsonl_files(self.roots.archive_root, skip_paths=skip_paths)
            + count_jsonl_files(self.roots.snapshot_root, skip_paths=skip_paths)
        )
        parsed_done = 0

        def _on_parsed(_path: Path) -> None:
            nonlocal parsed_done
            parsed_done += 1
            if progress_cb is not None:
                progress_cb("parsing", parsed_done, parse_total)

        if progress_cb is not None:
            # Initial 0/N tick so the UI can flip into "parsing" mode
            # immediately, even before the first file finishes parsing.
            progress_cb("parsing", 0, parse_total)

        active_entries = collect_sessions(
            self.roots.sessions_root, skip_paths=skip_paths, progress_cb=_on_parsed
        )
        archived_entries = collect_sessions(
            self.roots.archive_root, skip_paths=skip_paths, progress_cb=_on_parsed
        )
        snapshot_entries = collect_sessions(
            self.roots.snapshot_root, skip_paths=skip_paths, progress_cb=_on_parsed
        )

        latest_audit_by_id = {entry.session_id: entry for entry in self.repository.list_latest_audit_entries()}

        active_by_id: dict[str, tuple[Path, ParsedSessionCatalog, int]] = {}
        for path, parsed, mtime_ns in active_entries:
            active_by_id[parsed.summary.id] = (path, parsed, mtime_ns)

        archived_by_id: dict[str, tuple[Path, ParsedSessionCatalog, int]] = {}
        archived_relative_paths: dict[str, str] = {}
        for path, parsed, mtime_ns in archived_entries:
            normalized_path, original_relative = self._canonicalize_archived_entry(
                path, parsed, build_fallback_relative_path(parsed.summary.started_at, parsed.summary.id)
            )
            archived_by_id[parsed.summary.id] = (normalized_path, parsed, mtime_ns)
            archived_relative_paths[parsed.summary.id] = original_relative

        snapshot_by_id: dict[str, tuple[Path, ParsedSessionCatalog, int]] = {}
        for path, parsed, mtime_ns in snapshot_entries:
            snapshot_by_id[parsed.summary.id] = (path, parsed, mtime_ns)

        catalog_entries: list[CatalogSessionEntry] = []
        fresh_metadata: dict[str, tuple[int, int]] = {}
        all_ids = set(active_by_id) | set(archived_by_id) | set(snapshot_by_id)
        for session_id in all_ids:
            active = active_by_id.get(session_id)
            archived = archived_by_id.get(session_id)
            snapshot = snapshot_by_id.get(session_id)
            primary = active or archived or snapshot
            if primary is None:
                continue
            primary_path, primary_parsed, primary_mtime_ns = primary
            latest_audit = latest_audit_by_id.get(session_id)
            active_path = active[0] if active else None
            archive_path = archived[0] if archived else None
            snapshot_path = snapshot[0] if snapshot else None
            if active_path:
                original_relative = os.path.relpath(active_path, self.roots.sessions_root)
            else:
                original_relative = (
                    archived_relative_paths.get(session_id)
                    or _read_relative_path_from_audit(
                        latest_audit.source_path if latest_audit else None, self.roots
                    )
                    or _read_relative_path_from_audit(
                        latest_audit.target_path if latest_audit else None, self.roots
                    )
                    or build_fallback_relative_path(primary_parsed.summary.started_at, primary_parsed.summary.id)
                )
            catalog_entries.append(
                make_catalog_entry(
                    primary_parsed,
                    active_path=active_path,
                    archive_path=archive_path,
                    snapshot_path=snapshot_path,
                    original_relative_path=original_relative,
                    status=_resolve_catalog_status(active_path, archive_path, latest_audit.action if latest_audit else None),
                )
            )
            fresh_metadata[session_id] = (primary_mtime_ns, PARSER_VERSION)
            # If a session was provisionally cached but reappeared as fresh
            # (e.g. file was modified between the cache check and walk, or
            # multi-root cross-fertilization), the fresh path wins.
            kept_session_ids.discard(session_id)

        # Streaming commit: orphan delete + kept refresh in one transaction,
        # then fresh entries committed in batches of _RESCAN_BATCH_SIZE so
        # the UI can re-query ``list_sessions`` between batches and show
        # progress. Without this split, first-install rescans block until
        # the entire parse pass finishes.
        fresh_ids = {entry.summary.id for entry in catalog_entries}
        self.repository.upsert_catalog_batch_start(
            kept_session_ids=kept_session_ids,
            expected_fresh_session_ids=fresh_ids,
        )
        total = len(catalog_entries) + len(kept_session_ids)
        done = len(kept_session_ids)
        if progress_cb:
            progress_cb("indexing", done, total)
        for batch_start in range(0, len(catalog_entries), _RESCAN_BATCH_SIZE):
            batch = catalog_entries[batch_start : batch_start + _RESCAN_BATCH_SIZE]
            self.repository.upsert_catalog_batch_apply(
                entries=batch,
                fresh_metadata=fresh_metadata,
            )
            done += len(batch)
            if progress_cb:
                progress_cb("indexing", done, total)
        return catalog_entries

    def _canonicalize_archived_entry(
        self,
        path: Path,
        parsed: ParsedSessionCatalog,
        fallback_relative: str,
    ) -> tuple[Path, str]:
        try:
            current_relative = os.path.relpath(path, self.roots.archive_root)
        except ValueError:
            current_relative = fallback_relative
        if looks_canonical_session_relative_path(current_relative, parsed.summary.id):
            original_relative = current_relative
        else:
            original_relative = fallback_relative
        archive_path = ensure_inside_realpath(
            self.roots.archive_root,
            session_archive_path(self.roots.archive_root, original_relative),
            allow_missing_tail=True,
        )
        if str(path) != str(archive_path):
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, archive_path)
        return archive_path, original_relative

    def _ensure_roots(self) -> None:
        self.roots.sessions_root.mkdir(parents=True, exist_ok=True)
        self.roots.archive_root.mkdir(parents=True, exist_ok=True)
        self.roots.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.manager_home.mkdir(parents=True, exist_ok=True)

    def _require_session(self, session_id: str) -> SessionRecord:
        record = self.repository.get_session(session_id)
        if record is None:
            raise errors.unknown_session(session_id)
        return record

    def _run_batch(
        self,
        session_ids: list[str],
        action: Callable[[str], SessionRecord],
    ) -> BatchResult:
        unique_ids = unique_session_ids(session_ids)
        records: list[SessionRecord] = []
        failures: list[BatchFailure] = []
        for sid in unique_ids:
            try:
                records.append(action(sid))
            except errors.SessionError as ex:
                failures.append(_map_batch_failure(sid, ex))
            except Exception as ex:  # noqa: BLE001
                failures.append(_map_batch_failure(sid, ex))
        return BatchResult(records=records, failures=failures)

    def _validate_restore_target_directory(self, target_cwd: str) -> None:
        target = Path(target_cwd)
        try:
            stat_result = target.stat()
        except FileNotFoundError as ex:
            raise errors.restore_target_missing_directory(target_cwd) from ex
        except PermissionError as ex:
            raise errors.restore_target_permission_denied(target_cwd) from ex
        except NotADirectoryError as ex:
            raise errors.restore_target_not_directory(target_cwd) from ex
        if not (stat_result.st_mode & 0o555):
            raise errors.restore_target_permission_denied(target_cwd)
        if not target.is_dir():
            raise errors.restore_target_not_directory(target_cwd)
        if not os.access(target, os.R_OK | os.X_OK):
            raise errors.restore_target_permission_denied(target_cwd)

    def _assert_managed_current_path(self, record: SessionRecord) -> Path:
        if record.active_path:
            return self._assert_managed_path("active", self.roots.sessions_root, Path(record.active_path))
        if record.archive_path:
            return self._assert_managed_path("archive", self.roots.archive_root, Path(record.archive_path))
        raise errors.session_has_no_file_to_delete(record.id)

    def _assert_managed_restore_source(self, record: SessionRecord) -> Path:
        if record.archive_path:
            return self._assert_managed_path("archive", self.roots.archive_root, Path(record.archive_path))
        if record.snapshot_path:
            return self._assert_managed_path("snapshot", self.roots.snapshot_root, Path(record.snapshot_path))
        raise errors.session_is_not_restorable(record.id)

    def _assert_managed_path(
        self,
        label: str,
        root: Path,
        candidate: Path,
        *,
        allow_missing_tail: bool = False,
    ) -> Path:
        try:
            return ensure_inside_realpath(root, candidate, allow_missing_tail=allow_missing_tail)
        except errors.SessionError as ex:
            payload = ex.details if isinstance(ex.details, dict) else {}
            propagated = {
                key: payload.get(key)
                for key in ("managedRoot", "candidatePath", "resolvedCandidatePath")
                if key in payload
            }
            raise errors.managed_session_path_outside(label, **propagated) from ex

    def _normalize_restore_mode(self, value: str) -> RestoreMode:
        if value == "resume_only":
            return "resume_only"
        if value == "rebind_cwd":
            return "rebind_cwd"
        raise errors.unsupported_restore_mode(value)


def _resolve_catalog_status(active_path: Path | None, archive_path: Path | None, latest_audit_action: str | None) -> str:
    if active_path:
        return "active"
    if archive_path:
        return "deleted_pending_purge" if latest_audit_action == "delete" else "archived"
    return "restorable"


def _read_relative_path_from_audit(candidate: str | None, roots: SessionRoots) -> str | None:
    if not candidate:
        return None
    try:
        candidate_path = Path(candidate)
        if str(candidate_path).startswith(str(roots.archive_root)):
            return os.path.relpath(candidate_path, roots.archive_root)
        if str(candidate_path).startswith(str(roots.sessions_root)):
            return os.path.relpath(candidate_path, roots.sessions_root)
        return None
    except (ValueError, OSError):
        return None


def _rewrite_session_meta_cwd(file_path: Path, target_cwd: str) -> None:
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.split("\n")
    updated = False
    next_lines: list[str] = []
    for line in lines:
        if updated or not line.strip():
            next_lines.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            next_lines.append(line)
            continue
        if (
            isinstance(entry, dict)
            and entry.get("type") == "session_meta"
            and isinstance(entry.get("payload"), dict)
        ):
            entry["payload"]["cwd"] = target_cwd
            next_lines.append(json.dumps(entry, ensure_ascii=False))
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        raise RuntimeError(f"Session metadata is missing from {file_path}")
    file_path.write_text("\n".join(next_lines), encoding="utf-8")


def _map_batch_failure(session_id: str, exception: Exception) -> BatchFailure:
    if isinstance(exception, errors.SessionError):
        return BatchFailure(
            session_id=session_id,
            error=exception.message,
            code=exception.code,
            details=dict(exception.details),
        )
    return BatchFailure(session_id=session_id, error=str(exception) or "Unknown error")


def _dedupe_persisted_timeline(
    items: list[SessionTimelineItem],
) -> list[SessionTimelineItem]:
    """Collapse the (event_msg, response_item) duplicates that older parser
    runs persisted into the timeline_items table. Tool calls keep id-based
    identity; user/assistant messages with identical normalized text fold
    into the first occurrence.

    Returns the original list object when nothing changes, so callers can
    cheaply detect the no-op case and pass through the original page total."""
    seen: set[str] = set()
    deduped: list[SessionTimelineItem] = []
    changed = False
    for item in items:
        if item.type == "tool_call":
            key = f"tool::{item.id}"
        else:
            key = f"msg::{item.type}::{(item.text or '').strip()}"
        if key in seen:
            changed = True
            continue
        seen.add(key)
        deduped.append(item)
    return deduped if changed else items
