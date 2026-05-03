from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from codex_quota_viewer.sessions import (  # noqa: E402
    CatalogSessionEntry,
    SessionError,
    SessionFileSummary,
    SessionFilters,
    SessionTimelineItem,
    SessionsManager,
)
from codex_quota_viewer.sessions.helpers import build_fallback_relative_path  # noqa: E402
from codex_quota_viewer.sessions.jsonl_parser import (  # noqa: E402
    clear_parser_cache,
    parse_session_catalog,
    parse_session_timeline_page,
)
from codex_quota_viewer.sessions.repository import SessionRepository  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_parser_cache():
    clear_parser_cache()
    yield
    clear_parser_cache()


def _write_session_jsonl(
    sessions_root: Path,
    *,
    session_id: str,
    started_at: str,
    cwd: str,
    user_message: str = "hello world",
    agent_message: str = "general kenobi",
) -> Path:
    relative = build_fallback_relative_path(started_at, session_id)
    target = sessions_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": started_at,
                "payload": {
                    "id": session_id,
                    "timestamp": started_at,
                    "cwd": cwd,
                    "originator": "vscode",
                    "source": "vscode",
                    "cli_version": "0.42.0",
                    "model_provider": "openai",
                },
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": started_at,
                "payload": {"type": "user_message", "message": user_message},
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": started_at,
                "payload": {"type": "agent_message", "message": agent_message},
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": started_at,
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "shell",
                    "arguments": "ls",
                },
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": started_at,
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "ok",
                },
            }
        ),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _make_homes(tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex_home"
    manager_home = tmp_path / "manager_home"
    (codex_home / "sessions").mkdir(parents=True, exist_ok=True)
    (codex_home / "archived_sessions").mkdir(parents=True, exist_ok=True)
    manager_home.mkdir(parents=True, exist_ok=True)
    return codex_home, manager_home


def _catalog_entry(
    *,
    session_id: str,
    cwd: str,
    user_prompt_excerpt: str = "hello world",
    latest_agent_message_excerpt: str = "general kenobi",
    timeline: list[SessionTimelineItem] | None = None,
) -> CatalogSessionEntry:
    started_at = "2026-01-15T10:00:00Z"
    items = timeline or [
        SessionTimelineItem(
            id=f"{session_id}-user",
            type="message:user",
            timestamp=started_at,
            text=user_prompt_excerpt,
        )
    ]
    return CatalogSessionEntry(
        summary=SessionFileSummary(
            id=session_id,
            cwd=cwd,
            started_at=started_at,
            originator="vscode",
            source="vscode",
            cli_version="0.42.0",
            model_provider="openai",
            size_bytes=123,
            line_count=len(items) + 1,
            event_count=len(items),
            tool_call_count=sum(1 for item in items if item.type == "tool_call"),
            user_prompt_excerpt=user_prompt_excerpt,
            latest_agent_message_excerpt=latest_agent_message_excerpt,
        ),
        timeline=items,
        active_path=None,
        archive_path=None,
        snapshot_path=None,
        original_relative_path=None,
        status="active",
    )


def test_sessions_repository_schema_matches_vendor_codexmm(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path / "index.db")
    try:
        connection = sqlite3.connect(str(tmp_path / "index.db"))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "select name from sqlite_master where type in ('table','view')"
                )
            }
            assert {"sessions", "timeline_items", "audit_log", "session_search"}.issubset(tables)
            session_columns = {row[1] for row in connection.execute("pragma table_info(sessions)")}
            assert {
                "id",
                "active_path",
                "archive_path",
                "snapshot_path",
                "original_relative_path",
                "cwd",
                "started_at",
                "originator",
                "source",
                "cli_version",
                "model_provider",
                "size_bytes",
                "line_count",
                "event_count",
                "tool_call_count",
                "user_prompt_excerpt",
                "latest_agent_message_excerpt",
                "status",
                "created_at",
                "updated_at",
                "indexed_at",
            }.issubset(session_columns)
            timeline_columns = {row[1] for row in connection.execute("pragma table_info(timeline_items)")}
            assert {
                "session_id",
                "ordinal",
                "item_id",
                "type",
                "timestamp",
                "text",
                "tool_name",
                "summary",
                "input_text",
                "output_text",
                "status",
            }.issubset(timeline_columns)
        finally:
            connection.close()
    finally:
        repo.close()


def test_sessions_fts_search_qualifies_join_columns(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    cwd = str(tmp_path / "project_fts")
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.repository.replace_catalog(
            [
                _catalog_entry(
                    session_id="fts-session",
                    cwd=cwd,
                    user_prompt_excerpt="needle prompt",
                )
            ]
        )

        records = manager.repository._list_sessions_with_fts(
            SessionFilters(query="needle", status="active", cwd=cwd)
        )

        assert [record.id for record in records] == ["fts-session"]
    finally:
        manager.close()


def test_sessions_timeline_page_preserves_total_after_dedupe(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    session_id = "timeline-session"
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.repository.replace_catalog(
            [
                _catalog_entry(
                    session_id=session_id,
                    cwd=str(tmp_path / "project_timeline"),
                    timeline=[
                        SessionTimelineItem(
                            id="user-1",
                            type="message:user",
                            timestamp="2026-01-15T10:00:00Z",
                            text="same text",
                        ),
                        SessionTimelineItem(
                            id="user-2",
                            type="message:user",
                            timestamp="2026-01-15T10:00:01Z",
                            text="same text",
                        ),
                        SessionTimelineItem(
                            id="assistant-1",
                            type="message:assistant",
                            timestamp="2026-01-15T10:00:02Z",
                            text="reply",
                        ),
                    ],
                )
            ]
        )

        page = manager.get_session_timeline_page(session_id, offset=0, limit=10)

        assert [item.id for item in page.items] == ["user-1", "assistant-1"]
        assert page.total == 3
        assert page.next_offset is None
    finally:
        manager.close()


def test_sessions_rescan_indexes_active_and_archived_jsonl(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        started_at="2026-01-15T10:00:00Z",
        cwd=str(tmp_path / "project_a"),
    )
    _write_session_jsonl(
        codex_home / "archived_sessions",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        started_at="2026-01-16T10:00:00Z",
        cwd=str(tmp_path / "project_b"),
    )

    manager = SessionsManager(codex_home, manager_home)
    try:
        records = manager.rescan()
        statuses = {record.id: record.status for record in records}
        assert statuses["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] == "active"
        assert statuses["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"] == "archived"
    finally:
        manager.close()


def test_sessions_rescan_skips_locked_files_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        started_at="2026-01-17T10:00:00Z",
        cwd=str(tmp_path / "project_c"),
    )
    bad_path = codex_home / "sessions" / "2026" / "01" / "17" / "rollout-bad.jsonl"
    bad_path.write_text("{garbage", encoding="utf-8")

    manager = SessionsManager(codex_home, manager_home)
    try:
        records = manager.rescan()
        assert len(records) == 1
        assert records[0].id == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    finally:
        manager.close()


def test_sessions_jsonl_parser_paginates_timeline(tmp_path: Path) -> None:
    codex_home, _ = _make_homes(tmp_path)
    file_path = _write_session_jsonl(
        codex_home / "sessions",
        session_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        started_at="2026-01-18T10:00:00Z",
        cwd=str(tmp_path / "project_d"),
    )
    parsed = parse_session_catalog(file_path)
    assert parsed is not None
    assert parsed.summary.user_prompt_excerpt == "hello world"
    assert any(item.type == "tool_call" for item in parsed.timeline)

    page = parse_session_timeline_page(file_path, offset=0, limit=2)
    assert page.total >= 3
    assert len(page.items) == 2
    assert page.next_offset == 2


def test_sessions_archive_moves_active_into_archive_root(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    active_path = _write_session_jsonl(
        codex_home / "sessions",
        session_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        started_at="2026-01-19T10:00:00Z",
        cwd=str(tmp_path / "project_e"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        record = manager.archive_session("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        assert record.status == "archived"
        assert record.active_path is None
        assert record.archive_path is not None
        assert not active_path.exists()
        assert Path(record.archive_path).exists()
    finally:
        manager.close()


def test_sessions_delete_creates_snapshot_then_archives(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        started_at="2026-01-20T10:00:00Z",
        cwd=str(tmp_path / "project_f"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        record = manager.delete_session("ffffffff-ffff-ffff-ffff-ffffffffffff")
        assert record.status == "deleted_pending_purge"
        assert record.archive_path is not None
        assert record.snapshot_path is not None
        assert Path(record.snapshot_path).exists()
        assert Path(record.archive_path).exists()
    finally:
        manager.close()


def test_sessions_restore_resume_only_rebinds_active_path(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="11111111-1111-1111-1111-111111111111",
        started_at="2026-01-21T10:00:00Z",
        cwd=str(tmp_path / "project_g"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        manager.archive_session("11111111-1111-1111-1111-111111111111")
        result = manager.restore_session(
            "11111111-1111-1111-1111-111111111111",
            restore_mode="resume_only",
        )
        assert result.record.status == "active"
        assert result.record.active_path is not None
        assert Path(result.record.active_path).exists()
        assert "codex resume" in result.resume_command
    finally:
        manager.close()


def test_sessions_restore_rebind_cwd_rejected_when_target_missing(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="22222222-2222-2222-2222-222222222222",
        started_at="2026-01-22T10:00:00Z",
        cwd=str(tmp_path / "project_h"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        manager.archive_session("22222222-2222-2222-2222-222222222222")
        with pytest.raises(SessionError) as info:
            manager.restore_session(
                "22222222-2222-2222-2222-222222222222",
                restore_mode="rebind_cwd",
            )
        assert info.value.code == "rebind_requires_target"
    finally:
        manager.close()


def test_sessions_purge_removes_archive_and_snapshot(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="33333333-3333-3333-3333-333333333333",
        started_at="2026-01-23T10:00:00Z",
        cwd=str(tmp_path / "project_i"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        deleted = manager.delete_session("33333333-3333-3333-3333-333333333333")
        archive_path = Path(deleted.archive_path)
        snapshot_path = Path(deleted.snapshot_path)
        manager.purge_session("33333333-3333-3333-3333-333333333333")
        assert not archive_path.exists()
        assert not snapshot_path.exists()
        assert manager.repository.get_session("33333333-3333-3333-3333-333333333333") is None
    finally:
        manager.close()


def test_sessions_batch_archive_collects_failures_per_id(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="44444444-4444-4444-4444-444444444444",
        started_at="2026-01-24T10:00:00Z",
        cwd=str(tmp_path / "project_j"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        result = manager.batch_archive_sessions(
            [
                "44444444-4444-4444-4444-444444444444",
                "missing-id-1",
                "missing-id-2",
            ]
        )
        assert {record.id for record in result.records} == {
            "44444444-4444-4444-4444-444444444444"
        }
        assert {failure.session_id for failure in result.failures} == {
            "missing-id-1",
            "missing-id-2",
        }
        assert all(failure.code == "unknown_session" for failure in result.failures)
    finally:
        manager.close()


def test_sessions_manager_rejects_paths_outside_managed_root(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="55555555-5555-5555-5555-555555555555",
        started_at="2026-01-25T10:00:00Z",
        cwd=str(tmp_path / "project_k"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        outside = tmp_path / "outside.jsonl"
        outside.write_text("noop", encoding="utf-8")
        manager.repository.update_session(
            "55555555-5555-5555-5555-555555555555",
            {"active_path": str(outside)},
        )
        with pytest.raises(SessionError) as info:
            manager.archive_session("55555555-5555-5555-5555-555555555555")
        assert info.value.code == "managed_session_path_outside"
    finally:
        manager.close()


def test_sessions_jsonl_parser_keeps_response_items_sharing_timestamp(tmp_path: Path) -> None:
    codex_home, _ = _make_homes(tmp_path)
    relative = build_fallback_relative_path("2026-02-10T08:00:00Z", "burst-id")
    target = codex_home / "sessions" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shared_ts = "2026-02-10T08:00:01Z"
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-02-10T08:00:00Z",
                "payload": {
                    "id": "burst-id",
                    "timestamp": "2026-02-10T08:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "vscode",
                    "source": "vscode",
                    "cli_version": "0.42.0",
                    "model_provider": "openai",
                },
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": shared_ts,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first user line"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": shared_ts,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "first assistant reply"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": shared_ts,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "second assistant reply"}],
                },
            }
        ),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    parsed = parse_session_catalog(target)
    assert parsed is not None
    timeline_types = [item.type for item in parsed.timeline]
    assert timeline_types.count("message:user") == 1
    assert timeline_types.count("message:assistant") == 2


# ----------------------------------------------------------------------
# Image / attachment parsing
# ----------------------------------------------------------------------


# 1×1 transparent PNG, encoded as a Codex-style data URI. Compact enough
# to inline in tests without bloating the file but exercises the full
# decode path (real PNG header → MIME extraction → Attachment).
_PNG_1X1_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
_JPEG_TINY_DATA_URI = "data:image/jpeg;base64,/9j/2wBDAA=="


def _write_image_session(
    sessions_root: Path,
    *,
    session_id: str,
    started_at: str,
    content: list[dict],
) -> Path:
    relative = build_fallback_relative_path(started_at, session_id)
    target = sessions_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": started_at,
                "payload": {
                    "id": session_id,
                    "timestamp": started_at,
                    "cwd": str(sessions_root),
                    "originator": "vscode",
                    "source": "vscode",
                    "cli_version": "0.42.0",
                    "model_provider": "openai",
                },
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "timestamp": started_at,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": content,
                },
            }
        ),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def test_jsonl_parser_extracts_bracketed_image_payload(tmp_path: Path) -> None:
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="aaaaaaaa-1111-2222-3333-444444444444",
        started_at="2026-04-28T05:10:14Z",
        content=[
            {"type": "input_text", "text": "点击seed sandbox弹出\n"},
            {"type": "input_text", "text": "<image>"},
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI, "detail": "high"},
            {"type": "input_text", "text": "</image>"},
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    # Bracket tokens were adjacent to the image part — both should be
    # stripped from the rendered text.
    assert "<image>" not in item.text
    assert "</image>" not in item.text
    assert "点击seed sandbox弹出" in item.text
    assert len(item.attachments) == 1
    attachment = item.attachments[0]
    assert attachment.kind == "image"
    assert attachment.mime == "image/png"
    assert attachment.data_uri is not None
    assert attachment.data_uri.startswith("data:image/png;base64,")


def test_jsonl_parser_handles_jpeg_mime(tmp_path: Path) -> None:
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="bbbbbbbb-1111-2222-3333-444444444444",
        started_at="2026-04-28T06:00:00Z",
        content=[
            {"type": "input_text", "text": "look at this"},
            {"type": "input_image", "image_url": _JPEG_TINY_DATA_URI},
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    assert user_items[0].attachments[0].mime == "image/jpeg"


def test_jsonl_parser_keeps_bare_angle_brackets_in_prose(tmp_path: Path) -> None:
    """Pure prose containing literal ``<image>`` text but no real image
    part keeps the bracket tokens intact — the stripping rule fires only
    when adjacent to an actual ``input_image`` content part."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="cccccccc-1111-2222-3333-444444444444",
        started_at="2026-04-28T07:00:00Z",
        content=[
            {
                "type": "input_text",
                "text": "I tried <image> in the markdown but it didn't render",
            },
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    assert "<image>" in user_items[0].text
    assert user_items[0].attachments == ()


def test_jsonl_parser_strips_only_neighbouring_brackets(tmp_path: Path) -> None:
    """When two image parts are present and the brackets only neighbour
    the second one, the first image's text remains untouched."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="dddddddd-1111-2222-3333-444444444444",
        started_at="2026-04-28T08:00:00Z",
        content=[
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI},
            {"type": "input_text", "text": "<image>"},
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI},
            {"type": "input_text", "text": "</image>"},
            {"type": "input_text", "text": "bye"},
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    # Both images captured; bracket tokens around the SECOND image stripped.
    assert len(item.attachments) == 2
    # Brackets removed because they wrap an actual image part.
    assert "<image>" not in item.text
    assert "</image>" not in item.text
    # Surrounding prose preserved.
    assert "hi" in item.text and "bye" in item.text


def test_repository_migrates_attachments_column_on_upgrade(tmp_path: Path) -> None:
    """An existing pre-migration DB (timeline_items without
    ``attachments_json``) gains the column when the repository opens it,
    so users upgrading from an older build don't lose access to their
    sessions."""
    db_path = tmp_path / "sessions.db"
    legacy_schema = """
        create table sessions (
          id text primary key,
          active_path text,
          archive_path text,
          snapshot_path text,
          original_relative_path text,
          cwd text not null,
          started_at text not null,
          originator text not null,
          source text not null,
          cli_version text not null,
          model_provider text not null,
          size_bytes integer not null default 0,
          line_count integer not null default 0,
          event_count integer not null default 0,
          tool_call_count integer not null default 0,
          user_prompt_excerpt text not null default '',
          latest_agent_message_excerpt text not null default '',
          status text not null,
          created_at text not null,
          updated_at text not null
        );
        create table timeline_items (
          session_id text not null,
          ordinal integer not null,
          item_id text not null,
          type text not null,
          timestamp text not null,
          text text,
          tool_name text,
          summary text,
          input_text text,
          output_text text,
          status text,
          primary key (session_id, ordinal)
        );
        create table audit_log (
          id integer primary key autoincrement,
          action text not null,
          session_id text not null,
          source_path text,
          target_path text,
          details_json text not null default '{}',
          created_at text not null
        );
    """
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript(legacy_schema)
    legacy.close()

    repository = SessionRepository(db_path)
    try:
        rows = repository._connection.execute("pragma table_info(timeline_items)").fetchall()
        column_names = {str(row["name"]) for row in rows}
        assert "attachments_json" in column_names
    finally:
        repository.close()


def test_repository_round_trips_attachments(tmp_path: Path) -> None:
    """Attachments survive the SQLite write/read cycle so the detail
    panel (which always pulls timeline items via ``list_timeline_page``)
    sees the same images the parser captured."""
    codex_home, manager_home = _make_homes(tmp_path)
    _write_image_session(
        codex_home / "sessions",
        session_id="eeeeeeee-1111-2222-3333-444444444444",
        started_at="2026-04-28T09:00:00Z",
        content=[
            {"type": "input_text", "text": "first"},
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI, "detail": "high"},
        ],
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        detail = manager.get_session_detail("eeeeeeee-1111-2222-3333-444444444444")
        user_items = [item for item in detail.timeline if item.type == "message:user"]
        assert len(user_items) == 1
        attachments = user_items[0].attachments
        assert len(attachments) == 1
        assert attachments[0].kind == "image"
        assert attachments[0].mime == "image/png"
        assert attachments[0].data_uri == _PNG_1X1_DATA_URI
    finally:
        manager.close()


def test_sessions_filters_by_status(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="66666666-6666-6666-6666-666666666666",
        started_at="2026-01-26T10:00:00Z",
        cwd=str(tmp_path / "project_l"),
    )
    _write_session_jsonl(
        codex_home / "archived_sessions",
        session_id="77777777-7777-7777-7777-777777777777",
        started_at="2026-01-26T11:00:00Z",
        cwd=str(tmp_path / "project_m"),
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        active_only = manager.list_sessions(SessionFilters(status="active"))
        archived_only = manager.list_sessions(SessionFilters(status="archived"))
        assert {record.id for record in active_only} == {"66666666-6666-6666-6666-666666666666"}
        assert {record.id for record in archived_only} == {"77777777-7777-7777-7777-777777777777"}
    finally:
        manager.close()
