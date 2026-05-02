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
    SessionError,
    SessionFilters,
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
