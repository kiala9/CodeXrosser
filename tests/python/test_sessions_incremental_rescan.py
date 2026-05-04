"""P1.5 incremental-rescan regression tests.

Covers the stat-based skip in ``collect_sessions``, the
``upsert_catalog`` keep/delete branches, and the end-to-end
``manager.rescan`` flow that ties them together.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from codex_quota_viewer.sessions import (  # noqa: E402
    CatalogSessionEntry,
    SessionFileSummary,
    SessionTimelineItem,
    SessionsManager,
)
from codex_quota_viewer.sessions import helpers as sessions_helpers  # noqa: E402
from codex_quota_viewer.sessions.helpers import (  # noqa: E402
    build_fallback_relative_path,
    collect_sessions,
)
from codex_quota_viewer.sessions.jsonl_parser import (  # noqa: E402
    PARSER_VERSION,
    clear_parser_cache,
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
    cwd: str = "/work",
    user_message: str = "hello",
    agent_message: str = "world",
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


def _make_catalog_entry(
    session_id: str,
    *,
    active_path: str | None = None,
    archive_path: str | None = None,
    snapshot_path: str | None = None,
    timeline_items: int = 1,
) -> CatalogSessionEntry:
    started_at = "2026-01-15T10:00:00Z"
    timeline = [
        SessionTimelineItem(
            id=f"{session_id}-i{i}",
            type="message:user" if i == 0 else "message:assistant",
            timestamp=started_at,
            text=f"item {i}",
        )
        for i in range(timeline_items)
    ]
    return CatalogSessionEntry(
        summary=SessionFileSummary(
            id=session_id,
            cwd="/work",
            started_at=started_at,
            originator="vscode",
            source="vscode",
            cli_version="0.42.0",
            model_provider="openai",
            size_bytes=100,
            line_count=timeline_items + 1,
            event_count=timeline_items,
            tool_call_count=0,
            user_prompt_excerpt="hello",
            latest_agent_message_excerpt="world",
        ),
        timeline=timeline,
        active_path=active_path,
        archive_path=archive_path,
        snapshot_path=snapshot_path,
        original_relative_path=None,
        status="active",
    )


def test_collect_sessions_skips_paths_in_skip_set(tmp_path: Path, monkeypatch) -> None:
    """When a file's path is in ``skip_paths``, ``collect_sessions`` returns
    nothing for it AND does not call the parser — that's the whole point of
    the cache short-circuit."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    target_a = _write_session_jsonl(
        sessions_root,
        session_id="0190d0e1-0001-7000-9000-000000000001",
        started_at="2026-01-15T10:00:00Z",
    )
    target_b = _write_session_jsonl(
        sessions_root,
        session_id="0190d0e1-0002-7000-9000-000000000002",
        started_at="2026-01-15T11:00:00Z",
    )

    parse_calls: list[Path] = []
    real_parse = sessions_helpers.parse_session_catalog

    def spy_parse(path: Path, *args, **kwargs):
        parse_calls.append(path)
        return real_parse(path, *args, **kwargs)

    monkeypatch.setattr(sessions_helpers, "parse_session_catalog", spy_parse)

    entries = collect_sessions(sessions_root, skip_paths={target_a})
    parsed_paths = {entry[0] for entry in entries}
    assert target_b in parsed_paths
    assert target_a not in parsed_paths
    assert target_a not in parse_calls   # not just absent from output — never parsed
    # mtime is carried in entries[i][2] so the rescan path can record it.
    for path, parsed, mtime_ns in entries:
        assert isinstance(mtime_ns, int)
        assert mtime_ns > 0


def test_upsert_catalog_keeps_unchanged_session_intact(tmp_path: Path) -> None:
    """``kept_session_ids`` must NOT touch timeline_items or session_search
    rows for the kept session — that's what makes the rescan incremental."""
    repo = SessionRepository(tmp_path / "index.db")
    try:
        keep_id = "0190d0e1-aaaa-7000-9000-000000000aaa"
        repo.replace_catalog([_make_catalog_entry(keep_id, timeline_items=3)])
        before = repo.list_timeline_page(keep_id, offset=0, limit=100)
        assert before.total == 3

        # Insert a sentinel timeline row that the test itself wrote — if
        # upsert_catalog deletes/reinserts the kept session's timeline,
        # the sentinel disappears.
        repo._connection.execute(  # noqa: SLF001
            """
            insert into timeline_items (
                session_id, ordinal, item_id, type, timestamp, text
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (keep_id, 99, "sentinel", "message:assistant", "t", "DO NOT DELETE"),
        )

        # Second pass: nothing fresh, this id is "kept".
        repo.upsert_catalog(
            fresh_entries=[],
            fresh_metadata={},
            kept_session_ids={keep_id},
        )

        rows = repo._connection.execute(  # noqa: SLF001
            "select item_id from timeline_items where session_id = ? and ordinal = 99",
            (keep_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["item_id"] == "sentinel"
    finally:
        repo.close()


def test_upsert_catalog_deletes_orphaned_session(tmp_path: Path) -> None:
    """A session present in DB but not in fresh_entries ∪ kept_session_ids
    must be removed (file deleted from disk between scans)."""
    repo = SessionRepository(tmp_path / "index.db")
    try:
        a_id = "0190d0e1-bbbb-7000-9000-000000000bb1"
        b_id = "0190d0e1-bbbb-7000-9000-000000000bb2"
        repo.replace_catalog(
            [_make_catalog_entry(a_id), _make_catalog_entry(b_id)]
        )
        assert {r.id for r in repo.list_sessions()} == {a_id, b_id}

        # Second pass: only A is fresh, B is neither fresh nor kept.
        repo.upsert_catalog(
            fresh_entries=[_make_catalog_entry(a_id)],
            fresh_metadata={a_id: (12345, PARSER_VERSION)},
            kept_session_ids=set(),
        )
        assert {r.id for r in repo.list_sessions()} == {a_id}
        # B's timeline_items should also be gone.
        leftover = repo._connection.execute(  # noqa: SLF001
            "select count(*) as c from timeline_items where session_id = ?", (b_id,)
        ).fetchone()
        assert int(leftover["c"]) == 0
    finally:
        repo.close()


def test_upsert_catalog_records_freshness_columns(tmp_path: Path) -> None:
    """Fresh entries must persist their primary_mtime_ns + parser_version
    so the next rescan's cache check has something to compare against."""
    repo = SessionRepository(tmp_path / "index.db")
    try:
        sid = "0190d0e1-cccc-7000-9000-000000000ccc"
        repo.upsert_catalog(
            fresh_entries=[_make_catalog_entry(sid)],
            fresh_metadata={sid: (42_000_000, PARSER_VERSION)},
            kept_session_ids=set(),
        )
        meta = {m.session_id: m for m in repo.list_session_scan_metadata()}
        assert sid in meta
        assert meta[sid].primary_mtime_ns == 42_000_000
        assert meta[sid].parser_version == PARSER_VERSION
    finally:
        repo.close()


def test_rescan_skips_unchanged_sessions(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: rescan twice with no changes → second pass parses zero
    files because every primary path's stat matches the DB cache columns."""
    codex_home, manager_home = _make_homes(tmp_path)
    started_at = "2026-01-15T10:00:00Z"
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="0190d0e1-1111-7000-9000-000000000001",
        started_at=started_at,
    )
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="0190d0e1-2222-7000-9000-000000000002",
        started_at="2026-01-15T11:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        # Baseline scan populates cache columns.
        first_records = manager.rescan()
        assert len(first_records) == 2

        # Spy on parser for the second scan only.
        parse_calls: list[Path] = []
        real_parse = sessions_helpers.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_helpers, "parse_session_catalog", spy_parse)
        clear_parser_cache()  # bypass in-memory parser cache too

        second_records = manager.rescan()
        assert {r.id for r in second_records} == {r.id for r in first_records}
        assert parse_calls == []   # the cache short-circuit kicked in
    finally:
        manager.close()


def test_rescan_reparses_after_mtime_change(tmp_path: Path, monkeypatch) -> None:
    """Touching a session file (mtime change) must invalidate just THAT
    session's cache entry — others stay cached."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid_changed = "0190d0e1-3333-7000-9000-000000000003"
    sid_stable = "0190d0e1-4444-7000-9000-000000000004"
    path_changed = _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid_changed,
        started_at="2026-01-15T10:00:00Z",
    )
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid_stable,
        started_at="2026-01-15T11:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()

        # Bump only the changed file's mtime — content unchanged but the
        # cache key (st_mtime_ns) differs.
        st = path_changed.stat()
        os.utime(path_changed, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        parse_calls: list[Path] = []
        real_parse = sessions_helpers.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_helpers, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        manager.rescan()
        assert path_changed in parse_calls
        assert len(parse_calls) == 1   # only the touched file got reparsed
    finally:
        manager.close()


def test_rescan_reparses_when_parser_version_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    """If the persisted parser_version differs from current PARSER_VERSION,
    every session is reparsed — covers the parser-source-changed scenario."""
    codex_home, manager_home = _make_homes(tmp_path)
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="0190d0e1-5555-7000-9000-000000000005",
        started_at="2026-01-15T10:00:00Z",
    )
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="0190d0e1-6666-7000-9000-000000000006",
        started_at="2026-01-15T11:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        # Simulate parser source changing between scans by stamping all DB
        # rows with an older parser_version.
        manager.repository._connection.execute(  # noqa: SLF001
            "update sessions set parser_version = 0"
        )

        parse_calls: list[Path] = []
        real_parse = sessions_helpers.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_helpers, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        manager.rescan()
        assert len(parse_calls) == 2   # cache invalidated for all
    finally:
        manager.close()


def test_rescan_drops_session_when_file_deleted(tmp_path: Path) -> None:
    """A session whose file is removed from disk between scans must be
    deleted from the DB — that's the orphan branch of upsert_catalog."""
    codex_home, manager_home = _make_homes(tmp_path)
    keep_id = "0190d0e1-7777-7000-9000-000000000007"
    drop_id = "0190d0e1-8888-7000-9000-000000000008"
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=keep_id,
        started_at="2026-01-15T10:00:00Z",
    )
    drop_path = _write_session_jsonl(
        codex_home / "sessions",
        session_id=drop_id,
        started_at="2026-01-15T11:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        first = manager.rescan()
        assert {r.id for r in first} == {keep_id, drop_id}

        drop_path.unlink()
        clear_parser_cache()
        second = manager.rescan()
        assert {r.id for r in second} == {keep_id}
    finally:
        manager.close()
