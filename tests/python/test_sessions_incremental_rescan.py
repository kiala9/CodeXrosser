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
from codex_quota_viewer.sessions import jsonl_parser as sessions_jsonl_parser  # noqa: E402
from codex_quota_viewer.sessions import manager as sessions_manager  # noqa: E402
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


def test_parser_version_falls_back_when_source_is_unavailable(monkeypatch) -> None:
    """PyInstaller may import from a bundle without this .py source on disk."""

    class MissingSourcePath:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def read_bytes(self) -> bytes:
            raise FileNotFoundError("frozen source")

    monkeypatch.setattr(sessions_jsonl_parser, "Path", MissingSourcePath)

    version = sessions_jsonl_parser._compute_parser_version()  # noqa: SLF001

    assert isinstance(version, int)
    assert 0 <= version <= 0xFFFFFFFF


def test_parser_version_uses_fingerprint_when_frozen(tmp_path: Path, monkeypatch) -> None:
    """In frozen PyInstaller bundles ``_compute_parser_version`` must read
    the build-time fingerprint JSON that publish.ps1 baked in. That's how
    installer releases get a deterministic per-build version without
    anyone bumping ``_PARSER_BUMP``."""
    fp = tmp_path / "parser_fingerprint.json"
    fp.write_text(json.dumps({"parser_version": 0xDEADBEEF}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sessions_jsonl_parser, "_fingerprint_path", lambda: fp)

    assert sessions_jsonl_parser._compute_parser_version() == 0xDEADBEEF  # noqa: SLF001


def test_parser_version_falls_through_when_fingerprint_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Frozen build with a missing fingerprint must NOT crash — fall
    through to the source hash (which itself falls through to the
    constant when the .py is unavailable)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sessions_jsonl_parser,
        "_fingerprint_path",
        lambda: tmp_path / "does-not-exist.json",
    )

    version = sessions_jsonl_parser._compute_parser_version()  # noqa: SLF001
    expected = sessions_jsonl_parser._compute_parser_version_from_source()  # noqa: SLF001

    assert version == expected
    assert 0 <= version <= 0xFFFFFFFF


def test_parser_version_falls_through_when_fingerprint_corrupted(
    tmp_path: Path, monkeypatch
) -> None:
    """Corrupted fingerprint (bad JSON, wrong shape, out-of-range value)
    falls through to source hash. We never want a malformed JSON to
    permanently brick the cache check."""
    fp = tmp_path / "parser_fingerprint.json"
    fp.write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sessions_jsonl_parser, "_fingerprint_path", lambda: fp)

    version = sessions_jsonl_parser._compute_parser_version()  # noqa: SLF001
    expected = sessions_jsonl_parser._compute_parser_version_from_source()  # noqa: SLF001

    assert version == expected


def test_parser_version_ignores_fingerprint_in_dev(tmp_path: Path, monkeypatch) -> None:
    """In source checkouts (sys.frozen unset/false), the fingerprint
    JSON is never consulted — even if one happens to exist next to the
    module. Dev mode keeps the auto-detect-on-source-edit behaviour."""
    fp = tmp_path / "parser_fingerprint.json"
    fp.write_text(json.dumps({"parser_version": 0xDEADBEEF}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sessions_jsonl_parser, "_fingerprint_path", lambda: fp)

    version = sessions_jsonl_parser._compute_parser_version()  # noqa: SLF001
    expected = sessions_jsonl_parser._compute_parser_version_from_source()  # noqa: SLF001

    assert version == expected
    assert version != 0xDEADBEEF


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


# --- P1.5.1 lazy per-session reparse ----------------------------------------


def test_get_session_detail_reparses_when_parser_version_stale(
    tmp_path: Path, monkeypatch
) -> None:
    """Opening a session whose persisted parser_version is stale must
    trigger a one-shot reparse + cache update before returning the
    detail. Subsequent opens see the updated parser_version and don't
    reparse again."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid = "0190d0e1-9999-7000-9000-000000000999"
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid,
        started_at="2026-01-15T10:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        # Stamp the row with an older parser_version to simulate having
        # been indexed by a previous app build.
        manager.repository._connection.execute(  # noqa: SLF001
            "update sessions set parser_version = 0 where id = ?", (sid,)
        )
        assert manager.repository.get_session_parser_version(sid) == 0

        parse_calls: list[Path] = []
        real_parse = sessions_manager.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_manager, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        detail = manager.get_session_detail(sid)
        assert detail.record.id == sid
        assert len(parse_calls) == 1   # exactly one reparse, not a full rescan
        # Cache fingerprint persisted → next open skips reparse.
        assert manager.repository.get_session_parser_version(sid) == PARSER_VERSION

        manager.get_session_detail(sid)
        assert len(parse_calls) == 1   # still one — no second reparse
    finally:
        manager.close()


def test_get_session_detail_skips_reparse_on_parser_version_match(
    tmp_path: Path, monkeypatch
) -> None:
    """When the persisted parser_version matches current PARSER_VERSION,
    detail loading takes the fast path — zero parser invocations."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid = "0190d0e1-aaaa-7000-9000-0000000aaaaa"
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid,
        started_at="2026-01-15T10:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        assert manager.repository.get_session_parser_version(sid) == PARSER_VERSION

        parse_calls: list[Path] = []
        real_parse = sessions_manager.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_manager, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        detail = manager.get_session_detail(sid)
        assert detail.record.id == sid
        assert parse_calls == []
    finally:
        manager.close()


def test_get_session_detail_reparses_when_mtime_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """Opening a session whose on-disk mtime differs from the persisted
    primary_mtime_ns must trigger a one-shot reparse — covers the case
    where Codex CLI (or a manual editor) appended to the jsonl after
    the last rescan. Same lazy-reparse path as the parser-version check."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid = "0190d0e1-bbbb-7000-9000-0000000bbbbb"
    jsonl = _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid,
        started_at="2026-01-15T10:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        # Cache should now be aligned: parser_version + mtime both match.
        db_pv, db_mtime_ns = manager.repository.get_session_freshness(sid)
        assert db_pv == PARSER_VERSION
        assert db_mtime_ns == jsonl.stat().st_mtime_ns

        # Bump the file mtime to simulate external append/edit. +5s is
        # well past NTFS / WSL / network-FS resolution so the comparison
        # is unambiguous on every platform CI runs on.
        atime_ns = jsonl.stat().st_atime_ns
        bumped_mtime_ns = db_mtime_ns + 5_000_000_000
        os.utime(jsonl, ns=(atime_ns, bumped_mtime_ns))
        assert jsonl.stat().st_mtime_ns != db_mtime_ns

        parse_calls: list[Path] = []
        real_parse = sessions_manager.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_manager, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        detail = manager.get_session_detail(sid)
        assert detail.record.id == sid
        assert len(parse_calls) == 1   # mtime mismatch → exactly one reparse

        # Persisted mtime updated → next open is a cache hit.
        _, after_mtime_ns = manager.repository.get_session_freshness(sid)
        assert after_mtime_ns == jsonl.stat().st_mtime_ns

        manager.get_session_detail(sid)
        assert len(parse_calls) == 1   # still one
    finally:
        manager.close()


def test_get_session_detail_skips_reparse_when_mtime_matches(
    tmp_path: Path, monkeypatch
) -> None:
    """Happy path: parser_version + primary_mtime_ns both match the
    on-disk file → detail load takes the fast path with zero parser
    invocations. Guards against the mtime branch introducing spurious
    reparses on the most common code path."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid = "0190d0e1-cccc-7000-9000-0000000ccccc"
    jsonl = _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid,
        started_at="2026-01-15T10:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        db_pv, db_mtime_ns = manager.repository.get_session_freshness(sid)
        assert db_pv == PARSER_VERSION
        assert db_mtime_ns == jsonl.stat().st_mtime_ns

        parse_calls: list[Path] = []
        real_parse = sessions_manager.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_manager, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        detail = manager.get_session_detail(sid)
        assert detail.record.id == sid
        assert parse_calls == []
    finally:
        manager.close()


def test_get_session_detail_tolerates_missing_primary_file(
    tmp_path: Path, monkeypatch
) -> None:
    """If the primary jsonl is gone (deleted out from under us), the
    mtime stat raises OSError. We swallow it and serve whatever the DB
    has — better than crashing the detail panel. The next manual Rescan
    will cleanly drop the orphan row."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid = "0190d0e1-dddd-7000-9000-0000000ddddd"
    jsonl = _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid,
        started_at="2026-01-15T10:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        db_pv, db_mtime_ns = manager.repository.get_session_freshness(sid)
        assert db_pv == PARSER_VERSION
        assert db_mtime_ns > 0

        # Wipe the file on disk; DB row stays.
        jsonl.unlink()

        parse_calls: list[Path] = []
        real_parse = sessions_manager.parse_session_catalog

        def spy_parse(path: Path, *args, **kwargs):
            parse_calls.append(path)
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(sessions_manager, "parse_session_catalog", spy_parse)
        clear_parser_cache()

        # Should NOT raise. Should NOT reparse (stat failure → fall
        # through to DB load with the existing cached timeline).
        detail = manager.get_session_detail(sid)
        assert detail.record.id == sid
        assert parse_calls == []

        # DB row freshness fields untouched on the OSError branch.
        after_pv, after_mtime_ns = manager.repository.get_session_freshness(sid)
        assert after_pv == PARSER_VERSION
        assert after_mtime_ns == db_mtime_ns
    finally:
        manager.close()


def test_upsert_catalog_batch_apply_persists_per_batch(tmp_path: Path) -> None:
    """Phase-2 of the streaming rescan must commit each chunk in its own
    transaction so partial state is observable mid-scan — that's what
    lets the UI re-query and show progress between batches."""
    repo = SessionRepository(tmp_path / "index.db")
    try:
        a_id = "0190d0e1-d001-7000-9000-000000000001"
        b_id = "0190d0e1-d002-7000-9000-000000000002"
        # Phase-1: orphan delete + kept refresh (no fresh entries yet).
        repo.upsert_catalog_batch_start(
            kept_session_ids=set(),
            expected_fresh_session_ids={a_id, b_id},
        )
        # First batch lands; DB should already have entry A before B is applied.
        repo.upsert_catalog_batch_apply(
            entries=[_make_catalog_entry(a_id)],
            fresh_metadata={a_id: (1, PARSER_VERSION)},
        )
        ids_after_first = {r.id for r in repo.list_sessions()}
        assert a_id in ids_after_first
        assert b_id not in ids_after_first

        # Second batch lands; both visible now.
        repo.upsert_catalog_batch_apply(
            entries=[_make_catalog_entry(b_id)],
            fresh_metadata={b_id: (2, PARSER_VERSION)},
        )
        ids_after_second = {r.id for r in repo.list_sessions()}
        assert ids_after_second == {a_id, b_id}
    finally:
        repo.close()


def test_rescan_emits_progress_per_batch(tmp_path: Path) -> None:
    """``rescan(progress_cb=...)`` must invoke the callback at least once
    per batch + once for the kept-only initial state. With 120 sessions
    on disk and batch size 50, expect 4 ticks: initial(0/120),
    batch1(50/120), batch2(100/120), batch3(120/120)."""
    codex_home, manager_home = _make_homes(tmp_path)
    for i in range(120):
        _write_session_jsonl(
            codex_home / "sessions",
            session_id=f"0190d0e1-{i:04x}-7000-9000-{i:012x}",
            started_at="2026-01-15T10:00:00Z",
        )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        ticks: list[tuple[int, int]] = []
        manager.rescan(progress_cb=lambda d, t: ticks.append((d, t)))
        assert len(ticks) >= 4   # initial + at least 3 batches
        # Final tick must be done == total.
        final_done, final_total = ticks[-1]
        assert final_done == final_total == 120
        # First tick is the kept-only baseline; on first install kept=0.
        assert ticks[0] == (0, 120)
    finally:
        manager.close()


def test_first_install_auto_triggers_rescan_when_db_empty(tmp_path: Path) -> None:
    """SessionsPage's empty-list auto-trigger fires ``rescan_requested``
    exactly once per target on first install — so the user doesn't have
    to discover the manual Rescan button before seeing data."""
    from PySide6.QtWidgets import QApplication
    from codex_quota_viewer.sessions_page import SessionsPage
    from codex_quota_viewer.models import CodexHomeTarget

    QApplication.instance() or QApplication([])
    codex_home, manager_home = _make_homes(tmp_path)

    def factory(target):
        return SessionsManager(codex_home=codex_home, manager_home=manager_home)

    page = SessionsPage(
        sessions_manager_factory=factory,
        translator=lambda s: s,
        confirm_real_action=lambda action, summary: True,
    )
    try:
        emitted: list[CodexHomeTarget] = []
        page.rescan_requested.connect(lambda target: emitted.append(target))

        # Force a synchronous list load with empty DB → triggers the
        # empty-list auto-rescan branch in _apply_loaded_records.
        page.refresh_list()
        assert emitted == [page.target]

        # A second refresh on still-empty DB must NOT re-trigger.
        page.refresh_list()
        assert emitted == [page.target]
    finally:
        page.close()


def test_filtered_empty_list_does_not_trigger_auto_rescan(tmp_path: Path) -> None:
    """Codex-review regression: an empty list resulting from a user
    search/status filter must NOT trigger a rescan. The auto-trigger is
    intended for the genuine first-install empty-catalog case only."""
    from PySide6.QtWidgets import QApplication
    from codex_quota_viewer.sessions_page import SessionsPage

    QApplication.instance() or QApplication([])
    codex_home, manager_home = _make_homes(tmp_path)
    # Seed catalog with one session so DB is non-empty even though the
    # filtered list comes back empty.
    seed_manager = SessionsManager(
        codex_home=codex_home, manager_home=manager_home
    )
    _write_session_jsonl(
        codex_home / "sessions",
        session_id="0190d0e1-f001-7000-9000-00000000f001",
        started_at="2026-01-15T10:00:00Z",
    )
    seed_manager.rescan()
    seed_manager.close()

    def factory(target):
        return SessionsManager(codex_home=codex_home, manager_home=manager_home)

    page = SessionsPage(
        sessions_manager_factory=factory,
        translator=lambda s: s,
        confirm_real_action=lambda action, summary: True,
    )
    try:
        emitted: list = []
        page.rescan_requested.connect(lambda target: emitted.append(target))

        # Apply a search query that matches nothing → list will be empty
        # but the catalog itself isn't. Auto-trigger must stay quiet.
        page._search.setText("definitely-no-match-zzz")
        page.refresh_list()
        assert emitted == []
    finally:
        page.close()


def test_lazy_reparse_does_not_touch_other_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    """Opening session A with stale parser_version must NOT delete or
    rewrite session B's timeline_items rows — that's the difference
    between save_catalog_entry (single) and upsert_catalog (catalog-wide
    with orphan deletion)."""
    codex_home, manager_home = _make_homes(tmp_path)
    sid_a = "0190d0e1-bbbb-7000-9000-0000000bbbbb"
    sid_b = "0190d0e1-cccc-7000-9000-0000000ccccc"
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid_a,
        started_at="2026-01-15T10:00:00Z",
    )
    _write_session_jsonl(
        codex_home / "sessions",
        session_id=sid_b,
        started_at="2026-01-15T11:00:00Z",
    )

    manager = SessionsManager(codex_home=codex_home, manager_home=manager_home)
    try:
        manager.rescan()
        # Mark only A as stale.
        manager.repository._connection.execute(  # noqa: SLF001
            "update sessions set parser_version = 0 where id = ?", (sid_a,)
        )
        # Drop a sentinel row into B's timeline; if B gets rewritten,
        # the sentinel disappears.
        manager.repository._connection.execute(  # noqa: SLF001
            """
            insert into timeline_items (
                session_id, ordinal, item_id, type, timestamp, text
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (sid_b, 9999, "sentinel-b", "message:assistant", "t", "STAYS"),
        )

        clear_parser_cache()
        manager.get_session_detail(sid_a)

        rows = manager.repository._connection.execute(  # noqa: SLF001
            "select item_id from timeline_items where session_id = ? and ordinal = 9999",
            (sid_b,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["item_id"] == "sentinel-b"
        # Other sessions' parser_version unchanged.
        assert (
            manager.repository.get_session_parser_version(sid_b) == PARSER_VERSION
        )
    finally:
        manager.close()
