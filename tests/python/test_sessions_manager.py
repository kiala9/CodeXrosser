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
    Attachment,
    CatalogSessionEntry,
    SessionError,
    SessionFileSummary,
    SessionFilters,
    SessionTimelineItem,
    SessionsManager,
)
from codex_quota_viewer.sessions import jsonl_parser as sessions_jsonl_parser  # noqa: E402
from codex_quota_viewer.sessions.helpers import build_fallback_relative_path  # noqa: E402
from codex_quota_viewer.sessions.jsonl_parser import (  # noqa: E402
    DEFAULT_TIMELINE_PAGE_SIZE,
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


def test_get_session_detail_tail_load_is_bounded(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    session_id = "bounded-detail-session"
    total = DEFAULT_TIMELINE_PAGE_SIZE + 37
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.repository.replace_catalog(
            [
                _catalog_entry(
                    session_id=session_id,
                    cwd=str(tmp_path / "project_bounded_detail"),
                    timeline=[
                        SessionTimelineItem(
                            id=f"e-{i}",
                            type="message:user" if i % 2 == 0 else "message:assistant",
                            timestamp="2026-01-15T10:00:00Z",
                            text=f"message {i}",
                        )
                        for i in range(total)
                    ],
                )
            ]
        )

        detail = manager.get_session_detail(session_id)

        assert detail.timeline_total == total
        assert len(detail.timeline) == DEFAULT_TIMELINE_PAGE_SIZE
        assert detail.timeline_loaded_offset == 37
        assert detail.timeline[0].id == "e-37"
    finally:
        manager.close()


def test_session_timeline_index_is_lightweight_and_global(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    session_id = "time-travel-index-session"
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.repository.replace_catalog(
            [
                _catalog_entry(
                    session_id=session_id,
                    cwd=str(tmp_path / "project_time_travel_index"),
                    timeline=[
                        SessionTimelineItem(
                            id="user-big",
                            type="message:user",
                            timestamp="2026-01-15T10:00:00Z",
                            text="x" * 10_000,
                        ),
                        SessionTimelineItem(
                            id="tool-1",
                            type="tool_call",
                            timestamp="2026-01-15T10:00:01Z",
                            tool_name="shell",
                            summary="run compile",
                            input="input should not be read into the index",
                            output="output should not be read into the index",
                        ),
                    ],
                )
            ]
        )

        index = manager.get_session_timeline_index(session_id)

        assert [item.ordinal for item in index] == [0, 1]
        assert index[0].item_id == "user-big"
        assert len(index[0].preview) == 240
        assert index[1].tool_name == "shell"
        assert index[1].preview == "run compile"
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


def test_jsonl_parser_extracts_markdown_image_attachments_at_parse_time(tmp_path: Path) -> None:
    codex_home, _ = _make_homes(tmp_path)
    target = _write_session_jsonl(
        codex_home / "sessions",
        session_id="aaaa0000-1111-2222-3333-444444444444",
        started_at="2026-04-28T05:30:00Z",
        cwd=str(tmp_path),
        user_message="please inspect ![screenshot](C:/tmp/screenshot.png)",
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    assert "![" not in item.text
    assert "please inspect" in item.text
    assert len(item.attachments) == 1
    attachment = item.attachments[0]
    assert attachment.kind == "image"
    assert attachment.mime == "image/png"
    assert attachment.path == "C:/tmp/screenshot.png"
    assert attachment.alt == "screenshot"
    assert attachment.source == "markdown"


def test_jsonl_parser_payload_and_markdown_attachments_concat_at_parse(
    tmp_path: Path,
) -> None:
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="aaaa1111-1111-2222-3333-444444444444",
        started_at="2026-04-28T05:40:00Z",
        content=[
            {
                "type": "input_text",
                "text": "payload first, markdown second ![diagram](C:/tmp/diagram.webp)",
            },
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI, "detail": "high"},
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    assert "![" not in item.text
    assert len(item.attachments) == 2
    payload_attachment, markdown_attachment = item.attachments
    assert payload_attachment.source == "payload"
    assert payload_attachment.data_uri == _PNG_1X1_DATA_URI
    assert markdown_attachment.source == "markdown"
    assert markdown_attachment.path == "C:/tmp/diagram.webp"


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


def test_jsonl_parser_extracts_files_mentioned_markdown(tmp_path: Path) -> None:
    """Codex Desktop's @-file mention text block lifts into ``file``
    attachments and the displayed text shrinks to the user's actual
    request body."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="ffffffff-1111-2222-3333-444444444444",
        started_at="2026-04-29T17:25:10Z",
        content=[
            {
                "type": "input_text",
                "text": (
                    "\n# Files mentioned by the user:\n\n"
                    "## GPT-account.svg: E:/Download/GPT-account.svg\n\n"
                    "## API-account.svg: E:/Download/API-account.svg\n\n"
                    "## My request for Codex:\n"
                    "两个SVG矢量图，替换之前的位图ICON\n"
                ),
            },
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    # Header + path lines stripped from displayed text — only the
    # request body survives.
    assert "Files mentioned by the user" not in item.text
    assert "GPT-account.svg" not in item.text
    assert "My request for Codex" not in item.text
    assert "两个SVG矢量图" in item.text
    # Two file attachments captured with name/path/mime preserved.
    assert len(item.attachments) == 2
    a, b = item.attachments
    assert a.kind == "file" and a.name == "GPT-account.svg"
    assert a.path == "E:/Download/GPT-account.svg"
    assert a.mime == "image/svg+xml"
    assert a.source == "markdown"
    assert b.name == "API-account.svg"
    assert b.path == "E:/Download/API-account.svg"


def test_jsonl_parser_files_mentioned_header_works_with_markdown_image(
    tmp_path: Path,
) -> None:
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="ffffffff-2222-3333-4444-555555555555",
        started_at="2026-04-29T17:27:00Z",
        content=[
            {
                "type": "input_text",
                "text": (
                    "# Files mentioned by the user:\n\n"
                    "## notes.md: C:/tmp/notes.md\n\n"
                    "## My request for Codex:\n"
                    "compare this with ![capture](C:/tmp/capture.jpg)\n"
                ),
            },
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    assert "Files mentioned by the user" not in item.text
    assert "notes.md" not in item.text
    assert "![" not in item.text
    assert "compare this with" in item.text
    assert len(item.attachments) == 2
    file_attachment, image_attachment = item.attachments
    assert file_attachment.kind == "file"
    assert file_attachment.name == "notes.md"
    assert file_attachment.path == "C:/tmp/notes.md"
    assert image_attachment.kind == "image"
    assert image_attachment.path == "C:/tmp/capture.jpg"
    assert image_attachment.source == "markdown"


def test_parser_version_changes_when_parser_source_changes(monkeypatch) -> None:
    original_version = sessions_jsonl_parser.PARSER_VERSION
    original_read_bytes = Path.read_bytes
    parser_path = Path(sessions_jsonl_parser.__file__).resolve()

    def fake_read_bytes(path: Path) -> bytes:
        data = original_read_bytes(path)
        if path.resolve() == parser_path:
            return data + b"\n# source changed for parser version test\n"
        return data

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    assert sessions_jsonl_parser._compute_parser_version() != original_version


def test_jsonl_parser_files_mentioned_no_request_body(tmp_path: Path) -> None:
    """When the Codex transcript omits ``## My request for Codex:``
    (file-only message) the request body is empty but attachments are
    still captured."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="11111111-2222-3333-4444-555555555555",
        started_at="2026-04-29T17:30:00Z",
        content=[
            {
                "type": "input_text",
                "text": (
                    "# Files mentioned by the user:\n\n"
                    "## report.pdf: C:/Users/foo/report.pdf\n"
                ),
            },
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    assert item.text == ""
    assert len(item.attachments) == 1
    assert item.attachments[0].mime == "application/pdf"


def test_jsonl_parser_files_mentioned_only_anchored_at_start(tmp_path: Path) -> None:
    """User prose that quotes the marker mid-message should NOT be
    rewritten — extraction only fires when the header is the first
    thing in the text."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="22222222-3333-4444-5555-666666666666",
        started_at="2026-04-29T17:35:00Z",
        content=[
            {
                "type": "input_text",
                "text": (
                    "let me show you what Codex emits:\n\n"
                    "# Files mentioned by the user:\n"
                    "## a.txt: /tmp/a.txt\n"
                ),
            },
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    # No leading header at the start → not extracted.
    assert "Files mentioned by the user" in item.text
    assert item.attachments == ()


def test_jsonl_parser_strips_named_image_open_tag(tmp_path: Path) -> None:
    """Codex Desktop sometimes emits ``<image name=[Image #1]>`` instead
    of the bare ``<image>`` open tag. Both forms should be stripped
    when adjacent to a real ``input_image``."""
    codex_home, _ = _make_homes(tmp_path)
    target = _write_image_session(
        codex_home / "sessions",
        session_id="33333333-4444-5555-6666-777777777777",
        started_at="2026-04-29T07:57:30Z",
        content=[
            {"type": "input_text", "text": "test message"},
            {"type": "input_text", "text": "<image name=[Image #1]>"},
            {"type": "input_image", "image_url": _PNG_1X1_DATA_URI},
            {"type": "input_text", "text": "</image>"},
        ],
    )
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    assert len(user_items) == 1
    item = user_items[0]
    assert "<image" not in item.text
    assert "</image>" not in item.text
    assert "test message" in item.text
    assert len(item.attachments) == 1


def test_jsonl_parser_dedupes_event_msg_response_item_with_files_mentioned(tmp_path: Path) -> None:
    """Regression for the duplicate-bubble bug on session
    019dce24-cd6b-7940-999e-2e801ed493ef:
    Codex emits the same user turn twice (event_msg + response_item).
    Without normalisation, the event_msg twin kept the raw "# Files
    mentioned by the user:" markdown while the response_item twin had
    it stripped, so they had different dedup keys and BOTH rendered.
    """
    codex_home, _ = _make_homes(tmp_path)
    relative = build_fallback_relative_path("2026-04-29T07:57:30Z", "duplicate-id")
    target = codex_home / "sessions" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    file_mention_text = (
        "\n# Files mentioned by the user:\n\n"
        "## icon.png: C:/tmp/icon.png\n\n"
        "## My request for Codex:\n"
        "还是先暂时改用这个吧\n"
    )
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-04-29T07:57:30Z",
                "payload": {
                    "id": "duplicate-id",
                    "timestamp": "2026-04-29T07:57:30Z",
                    "cwd": str(tmp_path),
                    "originator": "vscode",
                    "source": "vscode",
                    "cli_version": "0.42.0",
                    "model_provider": "openai",
                },
            }
        ),
        # response_item — earlier timestamp (sorts first).
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-04-29T07:57:30.933Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": file_mention_text},
                        {"type": "input_text", "text": "<image name=[Image #1]>"},
                        {"type": "input_image", "image_url": _PNG_1X1_DATA_URI},
                        {"type": "input_text", "text": "</image>"},
                    ],
                },
            }
        ),
        # event_msg twin — same logical message, slightly later timestamp.
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-04-29T07:57:30.936Z",
                "payload": {"type": "user_message", "message": file_mention_text},
            }
        ),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    parsed = parse_session_catalog(target)
    assert parsed is not None
    user_items = [item for item in parsed.timeline if item.type == "message:user"]
    # Exactly one user bubble after dedup — both twins collapse.
    assert len(user_items) == 1
    item = user_items[0]
    # The response_item version wins (earlier timestamp) so we keep
    # both the file mention AND the structured image attachment.
    assert item.text == "还是先暂时改用这个吧"
    kinds = sorted(att.kind for att in item.attachments)
    assert kinds == ["file", "image"]


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


def test_repository_list_session_attachments_returns_only_attachment_bearing_rows(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "index.db")
    session_id = "attach-list-session"
    timestamp = "2026-04-30T10:00:00Z"
    try:
        repository.replace_catalog(
            [
                _catalog_entry(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    timeline=[
                        SessionTimelineItem(
                            id="item-0",
                            type="message:user",
                            timestamp=timestamp,
                            text="plain",
                        ),
                        SessionTimelineItem(
                            id="item-1",
                            type="message:user",
                            timestamp=timestamp,
                            text="has image",
                            attachments=(
                                Attachment(
                                    kind="image",
                                    mime="image/png",
                                    path="C:/tmp/a.png",
                                    alt="a",
                                    source="markdown",
                                ),
                            ),
                        ),
                        SessionTimelineItem(
                            id="item-2",
                            type="message:assistant",
                            timestamp=timestamp,
                            text="plain reply",
                        ),
                        SessionTimelineItem(
                            id="item-3",
                            type="message:user",
                            timestamp=timestamp,
                            text="has file",
                            attachments=(
                                Attachment(
                                    kind="file",
                                    mime="text/plain",
                                    path="C:/tmp/readme.txt",
                                    name="readme.txt",
                                    source="markdown",
                                ),
                            ),
                        ),
                        SessionTimelineItem(
                            id="item-4",
                            type="message:assistant",
                            timestamp=timestamp,
                            text="done",
                        ),
                    ],
                )
            ]
        )
        rows = repository.list_session_attachments(session_id)
        assert len(rows) == 2
        assert [row.ordinal for row in rows] == [1, 3]
        assert [row.item_id for row in rows] == ["item-1", "item-3"]
        assert [row.attachment_index for row in rows] == [0, 0]
        assert rows[0].attachment.path == "C:/tmp/a.png"
        assert rows[1].attachment.name == "readme.txt"
    finally:
        repository.close()


def test_repository_list_session_attachments_picks_up_legacy_markdown_text(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "index.db")
    session_id = "legacy-markdown-session"
    timestamp = "2026-04-30T11:00:00Z"
    try:
        repository.replace_catalog(
            [
                _catalog_entry(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    timeline=[
                        SessionTimelineItem(
                            id="plain",
                            type="message:user",
                            timestamp=timestamp,
                            text="plain old text",
                        ),
                        SessionTimelineItem(
                            id="legacy",
                            type="message:user",
                            timestamp=timestamp,
                            text="legacy ![capture](C:/tmp/capture.png) row",
                        ),
                    ],
                )
            ]
        )
        rows = repository.list_session_attachments(session_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.ordinal == 1
        assert row.item_id == "legacy"
        assert row.attachment_index == 0
        assert row.attachment.kind == "image"
        assert row.attachment.path == "C:/tmp/capture.png"
        assert row.attachment.alt == "capture"
        assert row.attachment.source == "markdown"
    finally:
        repository.close()


def test_repository_list_session_attachments_uses_partial_index(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "index.db")
    try:
        plan = repository._connection.execute(  # noqa: SLF001
            """
            explain query plan
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
            ("any-session", "any-session"),
        ).fetchall()
        details = " ".join(str(row["detail"]) for row in plan)
        assert "idx_timeline_items_session_attachments" in details
    finally:
        repository.close()


def test_sessions_manager_list_session_attachments_proxy(tmp_path: Path) -> None:
    codex_home, manager_home = _make_homes(tmp_path)
    session_id = "manager-attachments-session"
    _write_image_session(
        codex_home / "sessions",
        session_id=session_id,
        started_at="2026-04-30T12:00:00Z",
        content=[
            {"type": "input_text", "text": "see ![mockup](C:/tmp/mockup.png)"},
        ],
    )
    manager = SessionsManager(codex_home, manager_home)
    try:
        manager.rescan()
        rows = manager.list_session_attachments(session_id)
        assert len(rows) == 1
        assert rows[0].item_id.startswith("message-")
        assert rows[0].attachment.path == "C:/tmp/mockup.png"
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
