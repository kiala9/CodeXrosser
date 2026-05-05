from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader

from ._perf import _perf_timer
from .jsonl_parser import _extract_markdown_image_attachments
from .models import (
    Attachment,
    AuditEntry,
    CatalogSessionEntry,
    SessionAttachmentRow,
    SessionFilters,
    SessionRecord,
    SessionTimelineIndexItem,
    SessionTimelineItem,
    SessionTimelinePage,
)


@dataclass(frozen=True)
class SessionScanMetadata:
    """Lightweight per-session row used by the incremental-rescan cache
    check. Just enough to decide whether a file's path/mtime matches
    what we last parsed — the full ``CatalogSessionEntry`` is only
    rebuilt when the cache misses."""
    session_id: str
    active_path: str | None
    archive_path: str | None
    snapshot_path: str | None
    primary_mtime_ns: int
    parser_version: int


SCHEMA_FILE_NAME = "schema.sql"
_ATTACHMENT_THUMBNAIL_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_ATTACHMENT_THUMBNAIL_SIZE = QSize(100, 100)


SESSION_SELECT_COLUMNS = """
    id,
    coalesce(active_path, archive_path, snapshot_path) as filePath,
    active_path as activePath,
    archive_path as archivePath,
    snapshot_path as snapshotPath,
    original_relative_path as originalRelativePath,
    cwd,
    started_at as startedAt,
    originator,
    source,
    cli_version as cliVersion,
    model_provider as modelProvider,
    size_bytes as sizeBytes,
    line_count as lineCount,
    event_count as eventCount,
    tool_call_count as toolCallCount,
    user_prompt_excerpt as userPromptExcerpt,
    latest_agent_message_excerpt as latestAgentMessageExcerpt,
    status,
    created_at,
    updated_at,
    indexed_at
"""


SESSION_SELECT_COLUMNS_QUALIFIED = """
    sessions.id as id,
    coalesce(sessions.active_path, sessions.archive_path, sessions.snapshot_path) as filePath,
    sessions.active_path as activePath,
    sessions.archive_path as archivePath,
    sessions.snapshot_path as snapshotPath,
    sessions.original_relative_path as originalRelativePath,
    sessions.cwd as cwd,
    sessions.started_at as startedAt,
    sessions.originator as originator,
    sessions.source as source,
    sessions.cli_version as cliVersion,
    sessions.model_provider as modelProvider,
    sessions.size_bytes as sizeBytes,
    sessions.line_count as lineCount,
    sessions.event_count as eventCount,
    sessions.tool_call_count as toolCallCount,
    sessions.user_prompt_excerpt as userPromptExcerpt,
    sessions.latest_agent_message_excerpt as latestAgentMessageExcerpt,
    sessions.status as status,
    sessions.created_at as created_at,
    sessions.updated_at as updated_at,
    sessions.indexed_at as indexed_at
"""


class SessionRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("pragma journal_mode = WAL")
        self._connection.execute("pragma foreign_keys = ON")
        self._fts_available: bool | None = None
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass

    @property
    def fts_available(self) -> bool:
        if self._fts_available is None:
            try:
                self._connection.execute("select count(*) from session_search").fetchone()
                self._fts_available = True
            except sqlite3.Error:
                self._fts_available = False
        return bool(self._fts_available)

    def replace_catalog(self, entries: list[CatalogSessionEntry]) -> list[SessionRecord]:
        # Thin wrapper over upsert_catalog: every entry treated as fresh,
        # nothing kept → DB rows not present in ``entries`` get deleted.
        # Same end-state as the old all-delete-then-insert path; kept for
        # callers (e.g. existing tests) that don't have stat metadata.
        return self.upsert_catalog(
            fresh_entries=entries,
            fresh_metadata={},
            kept_session_ids=set(),
        )

    def upsert_catalog(
        self,
        *,
        fresh_entries: list[CatalogSessionEntry],
        fresh_metadata: dict[str, tuple[int, int]],
        kept_session_ids: set[str],
    ) -> list[SessionRecord]:
        """Selective catalog rewrite for the incremental-rescan path.

        - ``fresh_entries``: sessions that were re-parsed this scan. Their
          ``sessions`` row is upserted, ``timeline_items`` and
          ``session_search`` rows are deleted-then-reinserted.
          ``fresh_metadata[session_id] == (primary_mtime_ns, parser_version)``
          is written into the cache columns; defaults to (0, 0) when absent.
        - ``kept_session_ids``: cache hits — DB rows are left untouched
          (no timeline rewrite, no search rewrite). ``indexed_at`` is
          refreshed so the UI knows we saw them this scan.
        - Anything in DB but in neither set: deleted (file removed from
          disk between scans)."""
        indexed_at = _now_iso()
        fresh_ids = {entry.summary.id for entry in fresh_entries}
        with _perf_timer(
            "repository.upsert_catalog",
            fresh=len(fresh_entries),
            kept=len(kept_session_ids),
        ):
            with self._lock, self._transaction():
                existing = self._read_all_session_rows()
                # 1. Delete sessions absent from disk (orphaned).
                obsolete_ids = [
                    sid
                    for sid in existing.keys()
                    if sid not in fresh_ids and sid not in kept_session_ids
                ]
                for sid in obsolete_ids:
                    self._connection.execute(
                        "delete from timeline_items where session_id = ?", (sid,)
                    )
                    self._connection.execute(
                        "delete from session_search where session_id = ?", (sid,)
                    )
                    self._connection.execute(
                        "delete from sessions where id = ?", (sid,)
                    )
                # 2. Refresh ``indexed_at`` for kept rows so they don't
                # look stale to the UI. No content change.
                for sid in kept_session_ids:
                    if sid in existing:
                        self._connection.execute(
                            "update sessions set indexed_at = ? where id = ?",
                            (indexed_at, sid),
                        )
                # 3. Upsert fresh entries — same per-entry behavior as the
                # old replace_catalog, but per-row instead of nuke-and-pave.
                for entry in fresh_entries:
                    prior = existing.get(entry.summary.id)
                    created_at = prior["created_at"] if prior else indexed_at
                    updated_at = (
                        prior["updated_at"]
                        if prior and not _did_catalog_entry_change(prior, entry)
                        else indexed_at
                    )
                    mtime_ns, parser_version = fresh_metadata.get(
                        entry.summary.id, (0, 0)
                    )
                    self._connection.execute(
                        "delete from timeline_items where session_id = ?",
                        (entry.summary.id,),
                    )
                    self._connection.execute(
                        "delete from session_search where session_id = ?",
                        (entry.summary.id,),
                    )
                    self._upsert_session(
                        entry,
                        created_at=created_at,
                        updated_at=updated_at,
                        indexed_at=indexed_at,
                        primary_mtime_ns=mtime_ns,
                        parser_version=parser_version,
                    )
                    self._insert_session_search(entry)
                    self._insert_timeline_items(entry)
        return self.list_sessions()

    def list_session_scan_metadata(self) -> list[SessionScanMetadata]:
        """Lightweight per-session read used by the incremental-rescan
        cache check. Returns just the columns needed to decide whether
        the on-disk primary file's path/mtime match what we last parsed."""
        rows = self._connection.execute(
            """
            select id, active_path, archive_path, snapshot_path,
                   primary_mtime_ns, parser_version
            from sessions
            """
        ).fetchall()
        return [
            SessionScanMetadata(
                session_id=str(row["id"]),
                active_path=row["active_path"],
                archive_path=row["archive_path"],
                snapshot_path=row["snapshot_path"],
                primary_mtime_ns=int(row["primary_mtime_ns"] or 0),
                parser_version=int(row["parser_version"] or 0),
            )
            for row in rows
        ]

    def save_catalog_entry(self, entry: CatalogSessionEntry) -> SessionRecord:
        now = _now_iso()
        with self._lock, self._transaction():
            existing = self._read_session_row(entry.summary.id)
            created_at = existing["created_at"] if existing else now
            updated_at = (
                existing["updated_at"]
                if existing and not _did_catalog_entry_change(existing, entry)
                else now
            )
            self._upsert_session(entry, created_at=created_at, updated_at=updated_at, indexed_at=now)
            self._connection.execute(
                "delete from timeline_items where session_id = ?",
                (entry.summary.id,),
            )
            self._connection.execute(
                "delete from session_search where session_id = ?",
                (entry.summary.id,),
            )
            self._insert_session_search(entry)
            self._insert_timeline_items(entry)
        record = self.get_session(entry.summary.id)
        if record is None:
            raise RuntimeError(f"Session disappeared after upsert: {entry.summary.id}")
        return record

    def update_session(self, session_id: str, mutation: dict[str, Any]) -> SessionRecord:
        with self._lock, self._transaction():
            existing = self._read_session_row(session_id)
            if existing is None:
                raise RuntimeError(f"Session not found: {session_id}")
            now = _now_iso()
            merged = dict(existing)
            for key, value in mutation.items():
                column = _MUTATION_KEY_COLUMNS.get(key)
                if column is None:
                    continue
                merged[column] = value
            self._connection.execute(
                """
                update sessions set
                    active_path = :active_path,
                    archive_path = :archive_path,
                    snapshot_path = :snapshot_path,
                    original_relative_path = :original_relative_path,
                    cwd = :cwd,
                    started_at = :started_at,
                    originator = :originator,
                    source = :source,
                    cli_version = :cli_version,
                    model_provider = :model_provider,
                    size_bytes = :size_bytes,
                    line_count = :line_count,
                    event_count = :event_count,
                    tool_call_count = :tool_call_count,
                    user_prompt_excerpt = :user_prompt_excerpt,
                    latest_agent_message_excerpt = :latest_agent_message_excerpt,
                    status = :status,
                    updated_at = :updated_at,
                    indexed_at = :indexed_at
                where id = :id
                """,
                {
                    "id": session_id,
                    "active_path": merged.get("active_path"),
                    "archive_path": merged.get("archive_path"),
                    "snapshot_path": merged.get("snapshot_path"),
                    "original_relative_path": merged.get("original_relative_path"),
                    "cwd": merged.get("cwd"),
                    "started_at": merged.get("started_at"),
                    "originator": merged.get("originator"),
                    "source": merged.get("source"),
                    "cli_version": merged.get("cli_version"),
                    "model_provider": merged.get("model_provider"),
                    "size_bytes": int(merged.get("size_bytes") or 0),
                    "line_count": int(merged.get("line_count") or 0),
                    "event_count": int(merged.get("event_count") or 0),
                    "tool_call_count": int(merged.get("tool_call_count") or 0),
                    "user_prompt_excerpt": merged.get("user_prompt_excerpt") or "",
                    "latest_agent_message_excerpt": merged.get("latest_agent_message_excerpt") or "",
                    "status": merged.get("status"),
                    "updated_at": now,
                    "indexed_at": now,
                },
            )
            self._connection.execute(
                "delete from session_search where session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                """
                insert into session_search (
                    session_id, id, cwd, user_prompt_excerpt, latest_agent_message_excerpt
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session_id,
                    merged.get("cwd") or "",
                    merged.get("user_prompt_excerpt") or "",
                    merged.get("latest_agent_message_excerpt") or "",
                ),
            )
        record = self.get_session(session_id)
        if record is None:
            raise RuntimeError(f"Session not found after update: {session_id}")
        return record

    def delete_session(self, session_id: str) -> SessionRecord:
        with self._lock, self._transaction():
            existing = self.get_session(session_id)
            if existing is None:
                raise RuntimeError(f"Session not found: {session_id}")
            self._connection.execute(
                "delete from timeline_items where session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                "delete from session_search where session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                "delete from sessions where id = ?",
                (session_id,),
            )
            return existing

    def list_sessions(self, filters: SessionFilters | None = None) -> list[SessionRecord]:
        filters = filters or SessionFilters()
        if filters.query:
            try:
                return self._list_sessions_with_fts(filters)
            except sqlite3.Error:
                return self._list_sessions_with_like(filters)
        return self._list_sessions_without_query(filters)

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._connection.execute(
            f"select {SESSION_SELECT_COLUMNS} from sessions where id = ?",
            (session_id,),
        ).fetchone()
        return _map_session_row(row) if row else None

    def list_timeline_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 200,
    ) -> SessionTimelinePage:
        normalized_offset = max(offset or 0, 0)
        total_row = self._connection.execute(
            "select count(*) as count from timeline_items where session_id = ?",
            (session_id,),
        ).fetchone()
        total = int(total_row["count"] if total_row else 0)
        rows = self._connection.execute(
            """
            select item_id, type, timestamp, text, tool_name, summary,
                   input_text, output_text, status, attachments_json
            from timeline_items
            where session_id = ?
            order by ordinal asc
            limit ? offset ?
            """,
            (session_id, limit, normalized_offset),
        ).fetchall()
        items = [_map_timeline_row(row) for row in rows]
        next_offset = normalized_offset + limit if normalized_offset + limit < total else None
        return SessionTimelinePage(items=items, total=total, next_offset=next_offset)

    def list_timeline_tail(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> SessionTimelinePage:
        # Most-recent ``limit`` items in ascending ordinal order. Chat
        # sessions are read tail-first, so seeding the detail panel with
        # the tail keeps detail-open cost bounded by ``limit`` regardless
        # of session size — instead of paying O(N) for the full timeline
        # to dedup and ferry across the SQLite/Python boundary.
        normalized_limit = max(1, int(limit))
        total_row = self._connection.execute(
            "select count(*) as count from timeline_items where session_id = ?",
            (session_id,),
        ).fetchone()
        total = int(total_row["count"] if total_row else 0)
        if total <= 0:
            return SessionTimelinePage(items=[], total=0, next_offset=None)
        offset = max(0, total - normalized_limit)
        rows = self._connection.execute(
            """
            select item_id, type, timestamp, text, tool_name, summary,
                   input_text, output_text, status, attachments_json
            from timeline_items
            where session_id = ?
            order by ordinal asc
            limit ? offset ?
            """,
            (session_id, normalized_limit, offset),
        ).fetchall()
        items = [_map_timeline_row(row) for row in rows]
        return SessionTimelinePage(items=items, total=total, next_offset=None)

    def count_timeline_items(self, session_id: str) -> int:
        # Single-row count used by the detail panel's tab-switch
        # freshness check: comparing against panel._timeline_total tells
        # us whether the displayed slice is still consistent with the
        # repository (rescan may have rewritten timeline_items while
        # the user was on another tab).
        row = self._connection.execute(
            "select count(*) as count from timeline_items where session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_timeline_index(self, session_id: str) -> list[SessionTimelineIndexItem]:
        # Lightweight Time Travel source. Keep the result bounded to fields
        # needed by the popup, and slice large message/tool payloads in SQLite
        # so opening the popup does not materialize the full timeline bodies.
        rows = self._connection.execute(
            """
            select ordinal, item_id, type, timestamp,
                   case
                     when type = 'tool_call' then substr(coalesce(summary, tool_name, ''), 1, 240)
                     else substr(coalesce(text, ''), 1, 240)
                   end as preview,
                   tool_name
            from timeline_items
            where session_id = ?
            order by ordinal asc
            """,
            (session_id,),
        ).fetchall()
        return [_map_timeline_index_row(row) for row in rows]

    def list_session_attachments(self, session_id: str) -> list[SessionAttachmentRow]:
        """Return every attachment in a session in ordinal order."""
        with self._lock:
            rows = self._connection.execute(
                """
                select ordinal, item_id, type, timestamp, attachments_json, NULL as text
                from timeline_items
                where session_id = ? and attachments_json is not null
                union all
                select ordinal, item_id, type, timestamp, NULL as attachments_json, text
                from timeline_items
                where session_id = ?
                  and attachments_json is null
                  and text is not null
                  and instr(text, '![') > 0
                order by ordinal asc
                """,
                (session_id, session_id),
            ).fetchall()
            attachment_rows: list[SessionAttachmentRow] = []
            for row in rows:
                attachments = _decode_attachments_json(row)
                if not attachments:
                    _text, attachments = _extract_markdown_image_attachments(
                        str(row["text"] or "")
                    )
                if not attachments:
                    continue
                item_type = str(row["type"])
                normalized_type = (
                    "tool_call"
                    if item_type == "tool_call"
                    else "message:assistant"
                    if item_type == "message:assistant"
                    else "message:user"
                )
                for attachment_index, attachment in enumerate(attachments):
                    attachment_rows.append(
                        SessionAttachmentRow(
                            ordinal=int(row["ordinal"] or 0),
                            item_id=str(row["item_id"]),
                            type=normalized_type,  # type: ignore[arg-type]
                            timestamp=str(row["timestamp"]),
                            attachment_index=attachment_index,
                            attachment=_attachment_with_lightweight_preview(attachment),
                        )
                    )
            return attachment_rows

    def list_audit_entries(self, session_id: str) -> list[AuditEntry]:
        rows = self._connection.execute(
            """
            select id, action, session_id, source_path, target_path, details_json, created_at
            from audit_log
            where session_id = ?
            order by id desc
            """,
            (session_id,),
        ).fetchall()
        return [_map_audit_row(row) for row in rows]

    def list_latest_audit_entries(self) -> list[AuditEntry]:
        rows = self._connection.execute(
            """
            select a.id, a.action, a.session_id, a.source_path, a.target_path,
                   a.details_json, a.created_at
            from audit_log a
            inner join (
                select session_id, max(id) as max_id
                from audit_log
                group by session_id
            ) latest
              on latest.session_id = a.session_id and latest.max_id = a.id
            order by a.id asc
            """,
        ).fetchall()
        return [_map_audit_row(row) for row in rows]

    def insert_audit(
        self,
        action: str,
        session_id: str,
        source_path: str | None,
        target_path: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        details_payload = details or {}
        with self._lock:
            self._connection.execute(
                """
                insert into audit_log (
                    action, session_id, source_path, target_path, details_json, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    session_id,
                    source_path,
                    target_path,
                    json.dumps(details_payload, ensure_ascii=False),
                    _now_iso(),
                ),
            )

    def list_all_ids(self) -> list[str]:
        rows = self._connection.execute("select id from sessions").fetchall()
        return [str(row["id"]) for row in rows]

    def _list_sessions_without_query(self, filters: SessionFilters) -> list[SessionRecord]:
        clause, params = _build_session_filter_clause(filters)
        rows = self._connection.execute(
            f"""
            select {SESSION_SELECT_COLUMNS}
            from sessions
            where {clause}
            order by started_at desc, id asc
            """,
            params,
        ).fetchall()
        return [_map_session_row(row) for row in rows]

    def _list_sessions_with_like(self, filters: SessionFilters) -> list[SessionRecord]:
        clause, params = _build_session_filter_clause(filters)
        like_value = f"%{filters.query or ''}%"
        rows = self._connection.execute(
            f"""
            select {SESSION_SELECT_COLUMNS}
            from sessions
            where {clause}
              and (
                id like :query
                or cwd like :query
                or user_prompt_excerpt like :query
                or latest_agent_message_excerpt like :query
              )
            order by started_at desc, id asc
            """,
            {**params, "query": like_value},
        ).fetchall()
        return [_map_session_row(row) for row in rows]

    def _list_sessions_with_fts(self, filters: SessionFilters) -> list[SessionRecord]:
        if not self.fts_available:
            raise sqlite3.Error("session_search FTS unavailable")
        clause, params = _build_session_filter_clause(filters, table="sessions")
        rows = self._connection.execute(
            f"""
            select {SESSION_SELECT_COLUMNS_QUALIFIED}
            from sessions
            inner join session_search on session_search.session_id = sessions.id
            where {clause} and session_search match :query
            order by sessions.started_at desc, sessions.id asc
            """,
            {**params, "query": filters.query or ""},
        ).fetchall()
        return [_map_session_row(row) for row in rows]

    def _read_all_session_rows(self) -> dict[str, sqlite3.Row]:
        rows = self._connection.execute(
            f"select {SESSION_SELECT_COLUMNS}, created_at, updated_at, indexed_at from sessions"
        ).fetchall()
        return {str(row["id"]): row for row in rows}

    def _read_session_row(self, session_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            f"""
            select {SESSION_SELECT_COLUMNS},
                   active_path, archive_path, snapshot_path, original_relative_path,
                   started_at, cli_version, model_provider, size_bytes, line_count,
                   event_count, tool_call_count, user_prompt_excerpt,
                   latest_agent_message_excerpt
            from sessions where id = ?
            """,
            (session_id,),
        ).fetchone()

    def _insert_session(
        self,
        entry: CatalogSessionEntry,
        *,
        created_at: str,
        updated_at: str,
        indexed_at: str,
        primary_mtime_ns: int = 0,
        parser_version: int = 0,
    ) -> None:
        self._connection.execute(
            _INSERT_SESSION_SQL,
            _entry_to_params(
                entry, created_at, updated_at, indexed_at,
                primary_mtime_ns=primary_mtime_ns,
                parser_version=parser_version,
            ),
        )

    def _upsert_session(
        self,
        entry: CatalogSessionEntry,
        *,
        created_at: str,
        updated_at: str,
        indexed_at: str,
        primary_mtime_ns: int = 0,
        parser_version: int = 0,
    ) -> None:
        self._connection.execute(
            _UPSERT_SESSION_SQL,
            _entry_to_params(
                entry, created_at, updated_at, indexed_at,
                primary_mtime_ns=primary_mtime_ns,
                parser_version=parser_version,
            ),
        )

    def _insert_session_search(self, entry: CatalogSessionEntry) -> None:
        if not self.fts_available:
            return
        self._connection.execute(
            """
            insert into session_search (
                session_id, id, cwd, user_prompt_excerpt, latest_agent_message_excerpt
            ) values (?, ?, ?, ?, ?)
            """,
            (
                entry.summary.id,
                entry.summary.id,
                entry.summary.cwd,
                entry.summary.user_prompt_excerpt,
                entry.summary.latest_agent_message_excerpt,
            ),
        )

    def _insert_timeline_items(self, entry: CatalogSessionEntry) -> None:
        for ordinal, item in enumerate(entry.timeline):
            self._connection.execute(
                """
                insert into timeline_items (
                    session_id, ordinal, item_id, type, timestamp, text,
                    tool_name, summary, input_text, output_text, status,
                    attachments_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _timeline_item_to_params(entry.summary.id, ordinal, item),
            )

    def _clear_session_search(self) -> None:
        if self.fts_available:
            self._connection.execute("delete from session_search")

    def _ensure_schema(self) -> None:
        schema_path = Path(__file__).parent / SCHEMA_FILE_NAME
        sql = schema_path.read_text(encoding="utf-8")
        with self._lock, self._transaction():
            self._ensure_legacy_timeline_attachment_column()
            self._connection.executescript(sql)
            self._ensure_legacy_columns()

    def _ensure_legacy_timeline_attachment_column(self) -> None:
        table = self._connection.execute(
            """
            select name from sqlite_master
            where type = 'table' and name = 'timeline_items'
            """
        ).fetchone()
        if table is None:
            return
        timeline_rows = self._connection.execute("pragma table_info(timeline_items)").fetchall()
        timeline_columns = {str(row["name"]) for row in timeline_rows if row and row["name"]}
        if "attachments_json" not in timeline_columns:
            self._connection.execute(
                "alter table timeline_items add column attachments_json text"
            )

    def _ensure_legacy_columns(self) -> None:
        rows = self._connection.execute("pragma table_info(sessions)").fetchall()
        column_names = {str(row["name"]) for row in rows if row and row["name"]}
        if "indexed_at" not in column_names:
            self._connection.execute("alter table sessions add column indexed_at text")
            self._connection.execute(
                "update sessions set indexed_at = coalesce(indexed_at, updated_at, created_at, CURRENT_TIMESTAMP)"
            )
        # P1.5 incremental-rescan freshness columns. Existing rows default
        # to 0 → mismatches the current PARSER_VERSION → first post-upgrade
        # rescan reparses everything (same cost as today's behavior), then
        # writes real values and subsequent rescans are incremental.
        if "primary_mtime_ns" not in column_names:
            self._connection.execute(
                "alter table sessions add column primary_mtime_ns integer not null default 0"
            )
        if "parser_version" not in column_names:
            self._connection.execute(
                "alter table sessions add column parser_version integer not null default 0"
            )
        timeline_rows = self._connection.execute("pragma table_info(timeline_items)").fetchall()
        timeline_columns = {str(row["name"]) for row in timeline_rows if row and row["name"]}
        if "attachments_json" not in timeline_columns:
            self._connection.execute(
                "alter table timeline_items add column attachments_json text"
            )

    def _transaction(self):
        return _Transaction(self._connection)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._owns = False

    def __enter__(self) -> sqlite3.Connection:
        if not self._connection.in_transaction:
            self._connection.execute("begin")
            self._owns = True
        return self._connection

    def __exit__(self, exc_type, exc, tb):
        if not self._owns:
            return False
        try:
            if exc is None:
                self._connection.execute("commit")
            else:
                self._connection.execute("rollback")
        except sqlite3.Error:
            pass
        return False


_INSERT_SESSION_SQL = """
insert into sessions (
    id, active_path, archive_path, snapshot_path, original_relative_path,
    cwd, started_at, originator, source, cli_version, model_provider,
    size_bytes, line_count, event_count, tool_call_count,
    user_prompt_excerpt, latest_agent_message_excerpt, status,
    created_at, updated_at, indexed_at,
    primary_mtime_ns, parser_version
) values (
    :id, :active_path, :archive_path, :snapshot_path, :original_relative_path,
    :cwd, :started_at, :originator, :source, :cli_version, :model_provider,
    :size_bytes, :line_count, :event_count, :tool_call_count,
    :user_prompt_excerpt, :latest_agent_message_excerpt, :status,
    :created_at, :updated_at, :indexed_at,
    :primary_mtime_ns, :parser_version
)
"""


_UPSERT_SESSION_SQL = """
insert into sessions (
    id, active_path, archive_path, snapshot_path, original_relative_path,
    cwd, started_at, originator, source, cli_version, model_provider,
    size_bytes, line_count, event_count, tool_call_count,
    user_prompt_excerpt, latest_agent_message_excerpt, status,
    created_at, updated_at, indexed_at,
    primary_mtime_ns, parser_version
) values (
    :id, :active_path, :archive_path, :snapshot_path, :original_relative_path,
    :cwd, :started_at, :originator, :source, :cli_version, :model_provider,
    :size_bytes, :line_count, :event_count, :tool_call_count,
    :user_prompt_excerpt, :latest_agent_message_excerpt, :status,
    :created_at, :updated_at, :indexed_at,
    :primary_mtime_ns, :parser_version
)
on conflict(id) do update set
    active_path = excluded.active_path,
    archive_path = excluded.archive_path,
    snapshot_path = excluded.snapshot_path,
    original_relative_path = excluded.original_relative_path,
    cwd = excluded.cwd,
    started_at = excluded.started_at,
    originator = excluded.originator,
    source = excluded.source,
    cli_version = excluded.cli_version,
    model_provider = excluded.model_provider,
    size_bytes = excluded.size_bytes,
    line_count = excluded.line_count,
    event_count = excluded.event_count,
    tool_call_count = excluded.tool_call_count,
    user_prompt_excerpt = excluded.user_prompt_excerpt,
    latest_agent_message_excerpt = excluded.latest_agent_message_excerpt,
    status = excluded.status,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    indexed_at = excluded.indexed_at,
    primary_mtime_ns = excluded.primary_mtime_ns,
    parser_version = excluded.parser_version
"""


_MUTATION_KEY_COLUMNS = {
    "active_path": "active_path",
    "activePath": "active_path",
    "archive_path": "archive_path",
    "archivePath": "archive_path",
    "snapshot_path": "snapshot_path",
    "snapshotPath": "snapshot_path",
    "original_relative_path": "original_relative_path",
    "originalRelativePath": "original_relative_path",
    "cwd": "cwd",
    "started_at": "started_at",
    "startedAt": "started_at",
    "originator": "originator",
    "source": "source",
    "cli_version": "cli_version",
    "cliVersion": "cli_version",
    "model_provider": "model_provider",
    "modelProvider": "model_provider",
    "size_bytes": "size_bytes",
    "sizeBytes": "size_bytes",
    "line_count": "line_count",
    "lineCount": "line_count",
    "event_count": "event_count",
    "eventCount": "event_count",
    "tool_call_count": "tool_call_count",
    "toolCallCount": "tool_call_count",
    "user_prompt_excerpt": "user_prompt_excerpt",
    "userPromptExcerpt": "user_prompt_excerpt",
    "latest_agent_message_excerpt": "latest_agent_message_excerpt",
    "latestAgentMessageExcerpt": "latest_agent_message_excerpt",
    "status": "status",
}


def _entry_to_params(
    entry: CatalogSessionEntry,
    created_at: str,
    updated_at: str,
    indexed_at: str,
    *,
    primary_mtime_ns: int = 0,
    parser_version: int = 0,
) -> dict[str, Any]:
    return {
        "id": entry.summary.id,
        "active_path": entry.active_path,
        "archive_path": entry.archive_path,
        "snapshot_path": entry.snapshot_path,
        "original_relative_path": entry.original_relative_path,
        "cwd": entry.summary.cwd,
        "started_at": entry.summary.started_at,
        "originator": entry.summary.originator,
        "source": entry.summary.source,
        "cli_version": entry.summary.cli_version,
        "model_provider": entry.summary.model_provider,
        "size_bytes": entry.summary.size_bytes,
        "line_count": entry.summary.line_count,
        "event_count": entry.summary.event_count,
        "tool_call_count": entry.summary.tool_call_count,
        "user_prompt_excerpt": entry.summary.user_prompt_excerpt,
        "latest_agent_message_excerpt": entry.summary.latest_agent_message_excerpt,
        "status": entry.status,
        "created_at": created_at,
        "updated_at": updated_at,
        "indexed_at": indexed_at,
        "primary_mtime_ns": primary_mtime_ns,
        "parser_version": parser_version,
    }


def _timeline_item_to_params(session_id: str, ordinal: int, item: SessionTimelineItem) -> tuple[Any, ...]:
    if item.type == "tool_call":
        return (
            session_id,
            ordinal,
            item.id,
            item.type,
            item.timestamp,
            None,
            item.tool_name,
            item.summary,
            item.input,
            item.output,
            item.status or "pending",
            None,
        )
    attachments_json: str | None = None
    if item.attachments:
        attachments_json = json.dumps(
            [att.to_json() for att in item.attachments], ensure_ascii=False
        )
    return (
        session_id,
        ordinal,
        item.id,
        item.type,
        item.timestamp,
        item.text,
        None,
        None,
        None,
        None,
        None,
        attachments_json,
    )


def _build_session_filter_clause(
    filters: SessionFilters, *, table: str | None = None
) -> tuple[str, dict[str, Any]]:
    clauses = ["1 = 1"]
    params: dict[str, Any] = {}
    prefix = f"{table}." if table else ""
    if filters.status:
        if filters.status == "archived":
            clauses.append(f"({prefix}status = :status or {prefix}status = 'restorable')")
            params["status"] = filters.status
        else:
            clauses.append(f"{prefix}status = :status")
            params["status"] = filters.status
    if filters.cwd:
        clauses.append(f"{prefix}cwd = :cwd")
        params["cwd"] = filters.cwd
    return " and ".join(clauses), params


def _did_catalog_entry_change(existing: sqlite3.Row, entry: CatalogSessionEntry) -> bool:
    summary = entry.summary
    return (
        existing["activePath"] != entry.active_path
        or existing["archivePath"] != entry.archive_path
        or existing["snapshotPath"] != entry.snapshot_path
        or existing["originalRelativePath"] != entry.original_relative_path
        or existing["cwd"] != summary.cwd
        or existing["startedAt"] != summary.started_at
        or existing["originator"] != summary.originator
        or existing["source"] != summary.source
        or existing["cliVersion"] != summary.cli_version
        or existing["modelProvider"] != summary.model_provider
        or int(existing["sizeBytes"] or 0) != summary.size_bytes
        or int(existing["lineCount"] or 0) != summary.line_count
        or int(existing["eventCount"] or 0) != summary.event_count
        or int(existing["toolCallCount"] or 0) != summary.tool_call_count
        or existing["userPromptExcerpt"] != summary.user_prompt_excerpt
        or existing["latestAgentMessageExcerpt"] != summary.latest_agent_message_excerpt
        or existing["status"] != entry.status
    )


def _map_session_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        file_path=row["filePath"],
        active_path=row["activePath"],
        archive_path=row["archivePath"],
        snapshot_path=row["snapshotPath"],
        original_relative_path=row["originalRelativePath"],
        cwd=str(row["cwd"]),
        started_at=str(row["startedAt"]),
        originator=str(row["originator"]),
        source=str(row["source"]),
        cli_version=str(row["cliVersion"]),
        model_provider=str(row["modelProvider"]),
        size_bytes=int(row["sizeBytes"] or 0),
        line_count=int(row["lineCount"] or 0),
        event_count=int(row["eventCount"] or 0),
        tool_call_count=int(row["toolCallCount"] or 0),
        user_prompt_excerpt=str(row["userPromptExcerpt"] or ""),
        latest_agent_message_excerpt=str(row["latestAgentMessageExcerpt"] or ""),
        status=str(row["status"]),  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        indexed_at=str(row["indexed_at"] or row["updated_at"]),
    )


def _map_audit_row(row: sqlite3.Row) -> AuditEntry:
    details_raw = row["details_json"] or "{}"
    try:
        details = json.loads(details_raw)
    except (TypeError, ValueError):
        details = {}
    return AuditEntry(
        id=int(row["id"]),
        action=str(row["action"]),
        session_id=str(row["session_id"]),
        source_path=row["source_path"],
        target_path=row["target_path"],
        details=details if isinstance(details, dict) else {},
        created_at=str(row["created_at"]),
    )


def _map_timeline_row(row: sqlite3.Row) -> SessionTimelineItem:
    item_type = str(row["type"])
    if item_type == "tool_call":
        status_value = row["status"]
        status = status_value if status_value in ("completed", "errored") else "pending"
        return SessionTimelineItem(
            id=str(row["item_id"]),
            type="tool_call",
            timestamp=str(row["timestamp"]),
            tool_name=row["tool_name"] or "unknown_tool",
            summary=row["summary"] or row["tool_name"] or "unknown_tool",
            input=row["input_text"] or "",
            output=row["output_text"] or "",
            status=status,  # type: ignore[arg-type]
        )
    normalized_type = "message:assistant" if item_type == "message:assistant" else "message:user"
    attachments = _decode_attachments_json(row)
    return SessionTimelineItem(
        id=str(row["item_id"]),
        type=normalized_type,  # type: ignore[arg-type]
        timestamp=str(row["timestamp"]),
        text=str(row["text"] or ""),
        attachments=attachments,
    )


def _map_timeline_index_row(row: sqlite3.Row) -> SessionTimelineIndexItem:
    item_type = str(row["type"])
    normalized_type = (
        "tool_call"
        if item_type == "tool_call"
        else "message:assistant"
        if item_type == "message:assistant"
        else "message:user"
    )
    return SessionTimelineIndexItem(
        ordinal=int(row["ordinal"] or 0),
        item_id=str(row["item_id"]),
        type=normalized_type,  # type: ignore[arg-type]
        timestamp=str(row["timestamp"]),
        preview=str(row["preview"] or ""),
        tool_name=row["tool_name"],
    )


def _decode_attachments_json(row: sqlite3.Row) -> tuple[Attachment, ...]:
    # ``attachments_json`` may be missing entirely on rows written by older
    # builds that predate the column. ``sqlite3.Row`` raises ``IndexError``
    # for unknown column names, so probe via ``keys()``.
    if "attachments_json" not in row.keys():
        return ()
    raw = row["attachments_json"]
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    attachments: list[Attachment] = []
    for item in decoded:
        attachment = Attachment.from_json(item)
        if attachment is not None:
            attachments.append(attachment)
    return tuple(attachments)


def _attachment_with_lightweight_preview(attachment: Attachment) -> Attachment:
    """Return attachment metadata suitable for the full-session navigator.

    Detail timeline pages still round-trip original ``data_uri`` bytes for
    visible bubbles. The full Time Travel attachment list can contain hundreds
    of screenshots, so it replaces inline image bytes with a tiny PNG preview.
    That keeps the Images tab visually useful while avoiding a cache of every
    full-resolution screenshot.
    """
    if attachment.data_uri is None:
        return attachment
    data_uri = None
    if attachment.kind == "image":
        data_uri = _build_inline_image_thumbnail_data_uri(attachment.data_uri)
    return Attachment(
        kind=attachment.kind,
        mime=attachment.mime,
        data_uri=data_uri,
        path=attachment.path,
        alt=attachment.alt,
        name=attachment.name,
        source=attachment.source,
    )


def _build_inline_image_thumbnail_data_uri(data_uri: str) -> str | None:
    decoded = _decode_data_uri_bytes(data_uri)
    if decoded is None:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(decoded))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.width() > 0 and source_size.height() > 0:
        scaled_size = source_size.scaled(_ATTACHMENT_THUMBNAIL_SIZE, Qt.KeepAspectRatio)
        if (
            scaled_size.width() > 0
            and scaled_size.height() > 0
            and scaled_size != source_size
        ):
            reader.setScaledSize(scaled_size)
    image = reader.read()
    if image.isNull():
        return None
    if (
        image.width() > _ATTACHMENT_THUMBNAIL_SIZE.width()
        or image.height() > _ATTACHMENT_THUMBNAIL_SIZE.height()
    ):
        image = image.scaled(
            _ATTACHMENT_THUMBNAIL_SIZE,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    out_buffer = QBuffer()
    if not out_buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None
    if not image.save(out_buffer, "PNG"):
        return None
    encoded = base64.b64encode(bytes(out_buffer.data())).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _decode_data_uri_bytes(data_uri: str) -> bytes | None:
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return None
    comma = data_uri.find(",")
    if comma < 0:
        return None
    payload = data_uri[comma + 1 :]
    try:
        encoded = payload.encode("ascii", errors="ignore")
        if len(encoded) // 4 * 3 > _ATTACHMENT_THUMBNAIL_MAX_SOURCE_BYTES:
            return None
        decoded = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) > _ATTACHMENT_THUMBNAIL_MAX_SOURCE_BYTES:
        return None
    return decoded


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
