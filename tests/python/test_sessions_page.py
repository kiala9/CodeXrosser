from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from typing import Any  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QModelIndex, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from codex_quota_viewer.models import CodexHomeTarget  # noqa: E402
from codex_quota_viewer.sessions.models import (  # noqa: E402
    SessionDetail,
    SessionRecord,
)
from codex_quota_viewer.sessions_page import (  # noqa: E402
    SessionsPage,
    _SessionsTreeModel,
    _build_workfolder_groups,
    _format_size,
    _format_started_at,
    _looks_like_markdown,
)


def _record(
    session_id: str,
    *,
    status: str = "active",
    cwd: str = "/tmp/project",
    started_at: str = "2026-01-01T10:00:00Z",
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        file_path=f"/tmp/{session_id}.jsonl",
        active_path=f"/tmp/{session_id}.jsonl" if status == "active" else None,
        archive_path=None,
        snapshot_path=None,
        original_relative_path=f"2026/01/01/rollout-{session_id}.jsonl",
        cwd=cwd,
        started_at=started_at,
        originator="vscode",
        source="vscode",
        cli_version="0.42.0",
        model_provider="openai",
        size_bytes=4096,
        line_count=10,
        event_count=2,
        tool_call_count=1,
        user_prompt_excerpt="hello",
        latest_agent_message_excerpt="world",
        status=status,
        created_at="2026-01-01T10:00:00Z",
        updated_at="2026-01-01T10:00:00Z",
        indexed_at="2026-01-01T10:00:00Z",
    )


def _make_manager_factory(records: list[SessionRecord]) -> Mock:
    manager = Mock()
    manager.list_sessions.return_value = list(records)
    manager.get_session_detail.side_effect = lambda session_id: SessionDetail(
        record=next(record for record in records if record.id == session_id),
        audit_entries=[],
        timeline=[],
        timeline_total=0,
        timeline_next_offset=None,
    )
    factory = Mock()
    factory.return_value = manager
    return factory


def test_sessions_tree_model_groups_records_by_workfolder() -> None:
    QApplication.instance() or QApplication([])
    model = _SessionsTreeModel()
    records = [_record(f"id-{i:03d}") for i in range(0, 1500)]
    model.set_records(records)
    # All share the same cwd, so we expect a single workfolder group
    assert model.rowCount() == 1
    assert model.columnCount() == 3
    group_index = model.index(0, 0)
    assert model.is_group_index(group_index)
    # Children of the group should be every record
    assert model.itemFromIndex(group_index).rowCount() == 1500


def test_sessions_tree_model_separates_distinct_workfolders() -> None:
    QApplication.instance() or QApplication([])
    model = _SessionsTreeModel()
    records = [
        _record("a", cwd="/home/alice/project-one"),
        _record("b", cwd="/home/alice/project-two"),
        _record("c", cwd="/home/alice/project-one"),
    ]
    model.set_records(records)
    assert model.rowCount() == 2  # two distinct workfolders
    matched = False
    for row in range(model.rowCount()):
        item = model.item(row, 0)
        assert item is not None
        if item.toolTip() == "/home/alice/project-one":
            assert item.rowCount() == 2
            matched = True
    assert matched


def test_workfolder_disambiguation_uses_parent_when_basename_collides() -> None:
    records = [
        _record("a", cwd="/repos/alpha/project"),
        _record("b", cwd="/repos/beta/project"),
    ]
    groups = _build_workfolder_groups(records)
    assert len(groups) == 2
    display_names = {group.display_name for group in groups}
    # both end in "project" so the parent dir is used to disambiguate
    assert all("project" in name for name in display_names)
    assert any("alpha" in name for name in display_names)
    assert any("beta" in name for name in display_names)


def test_workfolder_uses_basename_when_unique() -> None:
    records = [
        _record("a", cwd="/repos/alpha"),
        _record("b", cwd="/repos/beta"),
    ]
    groups = _build_workfolder_groups(records)
    display_names = {group.display_name for group in groups}
    assert display_names == {"alpha", "beta"}


def test_sessions_format_helpers() -> None:
    assert _format_size(1023) == "1023 B"
    assert _format_size(2048) == "2.0 KB"
    assert "2026" in _format_started_at("2026-01-15T10:00:00Z")
    assert _format_started_at("not-a-date") == "not-a-date"


def test_sessions_message_body_renders_markdown_for_markdown_text() -> None:
    assert _looks_like_markdown("## heading\n- bullet")
    assert _looks_like_markdown("```python\nprint(1)\n```")
    assert _looks_like_markdown("**bold** word")


def test_sessions_message_body_falls_back_to_plain_for_xml_envelope() -> None:
    assert not _looks_like_markdown("<environment_context>\n  <cwd>...</cwd>\n</environment_context>")
    assert not _looks_like_markdown("<shell>powershell</shell>")
    assert not _looks_like_markdown("plain text")


def test_sessions_page_real_target_archive_requires_confirmation(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("session-real-1")])
    confirm = Mock(return_value=False)
    captured: list[tuple[CodexHomeTarget, str, list[str]]] = []

    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=confirm,
    )
    page.batch_action_requested.connect(
        lambda target, action, ids: captured.append((target, action, list(ids)))
    )
    page._on_target_changed(1)
    monkeypatch.setattr(page, "selected_session_ids", lambda: ["session-real-1"])

    page.request_batch("archive")
    assert confirm.called
    assert not captured

    confirm.return_value = True
    page.request_batch("archive")
    assert captured == [(CodexHomeTarget.REAL, "archive", ["session-real-1"])]


def test_sessions_page_sandbox_target_does_not_prompt(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("session-sandbox-1")])
    confirm = Mock(return_value=False)
    captured: list[tuple[CodexHomeTarget, str, list[str]]] = []

    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=confirm,
    )
    page.batch_action_requested.connect(
        lambda target, action, ids: captured.append((target, action, list(ids)))
    )
    monkeypatch.setattr(page, "selected_session_ids", lambda: ["session-sandbox-1"])

    page.request_batch("trash")
    assert not confirm.called
    assert captured == [(CodexHomeTarget.SANDBOX, "trash", ["session-sandbox-1"])]


def test_sessions_page_target_independent_from_global_active_target() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("sandbox"), _record("real")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    assert page.target == CodexHomeTarget.SANDBOX
    page._on_target_changed(1)
    assert page.target == CodexHomeTarget.REAL
    page._on_target_changed(0)
    assert page.target == CodexHomeTarget.SANDBOX


def test_sessions_page_target_and_status_controls_match_visual_hierarchy() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("a")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )

    assert page._sandbox_tab_btn.objectName() == "SessionsEnvTab"
    assert page._sandbox_tab_btn.property("position") == "first"
    assert page._sandbox_tab_btn.isChecked() is True
    assert page._real_tab_btn.objectName() == "SessionsEnvTab"
    assert page._real_tab_btn.property("position") == "last"
    assert page._real_tab_btn.isChecked() is False
    assert page._env_tab_group.exclusive() is True
    assert not hasattr(page, "_target_combo")
    assert not hasattr(page, "heading_target_selector")
    assert page._rescan_button.objectName() == "SessionsFloatingActionButton"
    assert page._rescan_button.property("actionKey") == "sessions-rescan"
    assert not page._rescan_button.icon().isNull()
    assert page._status_filter_button.objectName() == "SessionsListFilterButton"
    assert page._status_filter_button.property("hasActiveFilter") is False
    assert page._status_filter_popup.parentWidget() is not None
    assert page._status_filter_popup.isHidden()
    assert page._search_button.objectName() == "SessionsSearchButton"
    assert not page._search_button.icon().isNull()
    assert page._search_popup.parentWidget() is not None
    assert page._search.parentWidget() is page._search_popup
    assert page._locate_button.objectName() == "SessionsLocateButton"
    assert not page._locate_button.icon().isNull()
    assert "105, 102, 255" not in page._tree.styleSheet()


def test_clicking_workfolder_keeps_open_session_detail() -> None:
    QApplication.instance() or QApplication([])
    rec = _record("open-session")
    factory = _make_manager_factory([rec])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    page.refresh_if_stale()

    session_index = page._tree.currentIndex()
    group_index = page._tree_model.index(0, 0)
    assert session_index.data(Qt.UserRole) == rec.id
    assert page._tree_model.is_group_index(group_index)
    assert page._tree.property("activeSessionId") == rec.id
    assert page.selected_session_ids() == [rec.id]

    page._set_detail = Mock()  # type: ignore[assignment]
    page._tree.selectionModel().clearSelection()
    assert page.selected_session_ids() == [rec.id]
    page._on_row_changed(group_index, session_index)

    page._set_detail.assert_not_called()
    assert page._tree.currentIndex().data(Qt.UserRole) == rec.id
    assert page._tree.property("activeSessionId") == rec.id
    assert page.selected_session_ids() == [rec.id]


def test_locate_button_expands_parent_and_scrolls_to_active_session() -> None:
    QApplication.instance() or QApplication([])
    rec = _record("target-session", cwd="/tmp/target-folder")
    factory = _make_manager_factory([rec])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    page.refresh_if_stale()

    group_index = page._tree_model.index(0, 0)
    session_index = page._tree.currentIndex()
    assert session_index.data(Qt.UserRole) == rec.id

    page._tree.collapse(group_index)
    page._tree.selectionModel().clearSelection()
    assert not page._tree.isExpanded(group_index)

    page._locate_button.click()

    assert page._tree.isExpanded(group_index)
    assert page._tree.currentIndex().data(Qt.UserRole) == rec.id
    assert page.selected_session_ids() == [rec.id]


def test_search_popup_applies_query_on_submit() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("searchable")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    page.refresh_if_stale()
    manager = factory.return_value
    calls_before_typing = manager.list_sessions.call_count

    page._search_button.click()
    assert not page._search_popup.isHidden()
    page._search.setText("needle")
    assert manager.list_sessions.call_count == calls_before_typing

    page._search_submit.click()

    assert page._search_popup.isHidden()
    assert page._search_button.property("hasQuery")
    filters = manager.list_sessions.call_args.args[0]
    assert filters.query == "needle"


def test_refresh_if_stale_only_refetches_when_marked_stale() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("a")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    manager = factory.return_value
    # The page is born stale, so the first refresh_if_stale loads once.
    page.refresh_if_stale()
    initial_calls = manager.list_sessions.call_count
    assert initial_calls >= 1
    # A second refresh_if_stale with no stale mark must be a no-op.
    page.refresh_if_stale()
    assert manager.list_sessions.call_count == initial_calls
    # After mark_stale, it should re-fetch once.
    page.mark_stale()
    page.refresh_if_stale()
    assert manager.list_sessions.call_count == initial_calls + 1


def _sync_task_runner(action, on_success, on_error):
    """Drop-in fake of MainWindow.run_task that runs everything inline."""
    try:
        result = action()
    except Exception as ex:  # noqa: BLE001
        on_error(ex)
    else:
        on_success(result)


def test_refresh_uses_task_runner_when_provided() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("a")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
        task_runner=_sync_task_runner,
    )
    manager = factory.return_value
    # Page is born stale, so the first refresh_if_stale dispatches via the
    # task_runner and applies records once.
    page.refresh_if_stale()
    assert manager.list_sessions.call_count == 1
    assert page._tree_model.session_count() == 1
    # Idempotent on a clean page.
    page.refresh_if_stale()
    assert manager.list_sessions.call_count == 1
    # Mark stale → another async refresh.
    page.mark_stale()
    page.refresh_if_stale()
    assert manager.list_sessions.call_count == 2


def test_stale_list_result_dropped_when_token_advances() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("first")])
    captured: list[tuple[Any, Any, Any]] = []

    def capture_runner(action, on_success, on_error):
        captured.append((action, on_success, on_error))

    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
        task_runner=capture_runner,
    )
    # Initial stale-load: action is captured but not run.
    page.refresh_if_stale()
    assert len(captured) == 1
    first_action, first_on_success, _ = captured[0]
    # Reconfigure the manager to return a different result, then trigger a
    # second refresh that bumps the list_token.
    factory.return_value.list_sessions.return_value = [_record("second")]
    page.mark_stale()
    page.refresh_if_stale()
    assert len(captured) == 2
    second_action, second_on_success, _ = captured[1]
    # Run the second worker first, then deliver the stale first result.
    second_on_success(second_action())
    assert page._tree_model.session_count() == 1
    # The stale callback must be ignored — the model keeps the second result.
    first_on_success(first_action())
    assert page._tree_model.session_count() == 1
    ids = list(page._records_by_id.keys())
    assert ids == ["second"]


def test_detail_panel_requests_older_history_at_loaded_edge() -> None:
    """Tail-loaded session: when the user scrolls to the loaded edge,
    the panel emits older_history_requested with the offset/limit
    needed to fetch the immediately-older page from the manager."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("paginated")
    # Pretend the session has 1000 events but only the last 200 are
    # currently loaded — exactly what the new tail-load path produces.
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(800, 1000)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=tail,
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=800,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    captured: list[tuple[str, int, int]] = []
    panel.older_history_requested.connect(
        lambda sid, off, lim: captured.append((sid, off, lim))
    )
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    assert panel._loaded_offset == 800
    assert panel._timeline_total == 1000

    # Slide-up at the loaded edge → fetch request goes out, single-flight
    # guard prevents duplicates until prepend_older_items lands.
    panel._slide_window_up()
    assert captured == [(rec.id, 600, 200)]
    assert panel._loading_older is True
    panel._slide_window_up()
    assert captured == [(rec.id, 600, 200)]  # still single-flight

    # Older page comes back: items prepend, anchor preserved by id.
    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)
    assert panel._loading_older is False
    assert panel._loaded_offset == 600
    assert len(panel._all_timeline_items) == 400
    assert panel._current_user_block is not None
    kind, payload = panel._all_blocks[panel._current_user_block]
    assert kind == "single"
    # Anchor must still be e-800 (first user prompt that was visible
    # before the prepend), regardless of where it now sits in _all_blocks.
    assert payload.id == "e-800"
    panel.close()


def test_prepend_older_anchors_to_topmost_visible_bubble() -> None:
    """Older-page prepend must anchor the scroll restore onto the bubble
    that was at the viewport top — not snap to the active user prompt.
    The bug being regressed: ``_scroll_to_anchor_after_prepend`` set
    scroll to ``_current_user_block.widget.y()`` unconditionally, so
    every fetch yanked the view back to the nearest user prompt."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QWidget

    QApplication.instance() or QApplication([])
    rec = _record("anchor-topmost")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(800, 1000)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=tail,
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=800,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    # Pick a non-user item as the simulated "topmost-visible" bubble.
    # If the fix is working, the prepend will anchor on this exact id;
    # if the old bug were back, it would anchor on the nearest user
    # prompt (e-800 or e-805) instead.
    target_id = "e-803"
    target_block_index = next(
        i
        for i, (kind, payload) in enumerate(panel._all_blocks)
        if kind == "single" and getattr(payload, "id", None) == target_id
    )
    fake_top = QWidget()
    fake_top.setProperty("blockIndex", target_block_index)
    panel._topmost_visible_widget = lambda: fake_top  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    def stub_apply(block_index: int, offset: int, raw_scroll: int) -> None:
        captured["block_index"] = block_index
        captured["offset"] = offset
        captured["raw_scroll"] = raw_scroll

    panel._apply_prepend_anchor = stub_apply  # type: ignore[method-assign]

    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)
    QApplication.processEvents()

    # The anchor must be the new index of e-803, not of the surrounding
    # user prompts. _block_for_anchor_id resolves the post-recoalesce
    # block index for the same id we captured before the prepend.
    new_target_block = panel._block_for_anchor_id(target_id)
    assert new_target_block is not None
    assert captured.get("block_index") == new_target_block
    user_block_e800 = panel._block_for_message_id("e-800")
    user_block_e805 = panel._block_for_message_id("e-805")
    assert captured.get("block_index") not in {user_block_e800, user_block_e805}
    panel.close()


def test_block_for_anchor_id_resolves_tool_group_inner_ids() -> None:
    """``_block_for_anchor_id`` must find a tool_call by id even when
    coalesce has merged it into a tool_group block. ``_block_for_message_id``
    only checks single-item blocks and returns None for tool_group inner
    ids — the new helper has to cover that gap so prepend's topmost-
    visible anchor survives a tool-group merge at the boundary."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("anchor-toolgroup")
    timeline = [
        SessionTimelineItem(
            id="e-user", type="message:user", timestamp="t", text="hi"
        ),
        SessionTimelineItem(
            id="e-tc-1",
            type="tool_call",
            timestamp="t",
            tool_name="read",
            summary="r1",
            status="completed",
        ),
        SessionTimelineItem(
            id="e-tc-2",
            type="tool_call",
            timestamp="t",
            tool_name="read",
            summary="r2",
            status="completed",
        ),
        SessionTimelineItem(
            id="e-asst", type="message:assistant", timestamp="t", text="ok"
        ),
    ]
    detail = SessionDetail(
        record=rec, audit_entries=[], timeline=timeline,
        timeline_total=len(timeline), timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    # tool_call items coalesce into a tool_group block.
    group_block = next(
        i for i, (kind, _payload) in enumerate(panel._all_blocks) if kind == "tool_group"
    )
    # The legacy helper can't see inner tool_call ids.
    assert panel._block_for_message_id("e-tc-2") is None
    # The new helper must.
    assert panel._block_for_anchor_id("e-tc-2") == group_block
    assert panel._block_for_anchor_id("e-tc-1") == group_block
    # Single-block lookups still work.
    assert panel._block_for_anchor_id("e-user") == panel._block_for_message_id("e-user")
    assert panel._block_for_anchor_id("e-asst") == panel._block_for_message_id("e-asst")
    panel.close()


def test_scroll_settled_at_top_edge_triggers_older_fetch() -> None:
    """Regression: when the panel is tail-loaded with _window_start == 0
    but _loaded_offset > 0, scrolling near the top edge MUST trigger
    older_history_requested. The bug was that _on_scroll_settled gated
    the up-slide on _window_start > 0 alone, so the natural
    "scroll-up-at-top-of-loaded-tail" gesture never reached the older-
    page request path."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("scroll-edge")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(800, 1000)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=tail,
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=800,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.resize(900, 700)
    captured: list[tuple[str, int, int]] = []
    panel.older_history_requested.connect(
        lambda sid, off, lim: captured.append((sid, off, lim))
    )
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    # Initial state: window starts at the loaded edge, but there's older
    # history to fetch. _window_start == 0 alone used to short-circuit
    # the up-slide path; with the fix, _on_scroll_settled must still
    # invoke _slide_window_up so it can route to _maybe_request_older.
    assert panel._window_start == 0
    assert panel._loaded_offset == 800

    scrollbar = panel._timeline_scroll.verticalScrollBar()
    # Synthesize a scroll-near-top condition. Range needs a positive
    # max so edge-trigger arithmetic has room to run.
    scrollbar.setRange(0, 5_000)
    scrollbar.setValue(20)  # well within the edge_px band
    panel._scroll_throttle.stop()
    panel._on_scroll_settled()

    assert captured == [(rec.id, 600, 200)]
    panel.close()


def test_hide_event_keeps_rendered_timeline_alive() -> None:
    """Tab-switch keep-alive contract: hideEvent must NOT tear down the
    panel's rendered state. The user's scroll position, loaded items,
    and window bounds all need to survive the page being hidden so the
    next show is instant."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from PySide6.QtGui import QHideEvent

    QApplication.instance() or QApplication([])
    rec = _record("keepalive")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 3 == 0 else "message:assistant",
            timestamp="t",
            text=f"item {i}",
        )
        for i in range(20)
    ]
    detail = SessionDetail(
        record=rec, audit_entries=[], timeline=timeline,
        timeline_total=len(timeline), timeline_next_offset=None,
    )
    factory = _make_manager_factory([rec])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    # Populate the panel directly so we don't depend on async detail
    # fetch timing.
    page._detail_panel.set_detail(detail, page.target)
    pre_session_id = page._detail_panel.loaded_session_id()
    pre_total = page._detail_panel.loaded_timeline_total()
    pre_blocks_count = len(page._detail_panel._all_blocks)
    pre_render_token = page._detail_panel._render_token

    page.hideEvent(QHideEvent())

    # The panel's contents must be untouched.
    assert page._detail_panel.loaded_session_id() == pre_session_id
    assert page._detail_panel.loaded_timeline_total() == pre_total
    assert len(page._detail_panel._all_blocks) == pre_blocks_count
    # _render_token would bump if discard_rendered_timeline / set_detail
    # ran — keep-alive guarantees neither does.
    assert page._detail_panel._render_token == pre_render_token
    page.close()


def test_show_event_skips_refetch_when_repository_count_unchanged() -> None:
    """Freshness check: when ``count_timeline_items`` matches the
    panel's ``loaded_timeline_total``, showEvent is a true no-op —
    no detail token bump, no fetch dispatched."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from PySide6.QtGui import QShowEvent

    QApplication.instance() or QApplication([])
    rec = _record("fresh-data")
    timeline = [
        SessionTimelineItem(
            id="e-0", type="message:user", timestamp="t", text="hi"
        ),
        SessionTimelineItem(
            id="e-1", type="message:assistant", timestamp="t", text="ok"
        ),
    ]
    detail = SessionDetail(
        record=rec, audit_entries=[], timeline=timeline,
        timeline_total=2, timeline_next_offset=None,
    )
    factory = _make_manager_factory([rec])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    page._detail_panel.set_detail(detail, page.target)

    manager = factory.return_value
    manager.repository.get_session.side_effect = lambda sid: rec
    # Repository agrees with what panel saw at set_detail time.
    manager.repository.count_timeline_items.side_effect = lambda sid: 2

    pre_token = page._detail_token
    pre_detail_calls = manager.get_session_detail.call_count

    page.showEvent(QShowEvent())

    assert page._detail_token == pre_token
    assert manager.get_session_detail.call_count == pre_detail_calls
    page.close()


def test_show_event_refetches_when_repository_count_diverges() -> None:
    """If a rescan rewrote timeline_items while we were on another
    tab, ``count_timeline_items`` will diverge from the panel's cached
    ``loaded_timeline_total``. showEvent must spot the divergence and
    dispatch a refetch via the same path as row-change."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from PySide6.QtGui import QShowEvent

    QApplication.instance() or QApplication([])
    rec = _record("stale-data")
    timeline = [
        SessionTimelineItem(
            id="e-0", type="message:user", timestamp="t", text="hi"
        ),
    ]
    detail = SessionDetail(
        record=rec, audit_entries=[], timeline=timeline,
        timeline_total=1, timeline_next_offset=None,
    )
    factory = _make_manager_factory([rec])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
    )
    page._detail_panel.set_detail(detail, page.target)

    manager = factory.return_value
    manager.repository.get_session.side_effect = lambda sid: rec
    # Pretend a rescan added rows while the user was away.
    manager.repository.count_timeline_items.side_effect = lambda sid: 7

    pre_token = page._detail_token

    page.showEvent(QShowEvent())

    # _request_detail bumps the token; that's our refetch signal.
    assert page._detail_token > pre_token
    page.close()


def test_detail_panel_no_older_request_at_session_start() -> None:
    """A session that fits entirely in the initial load (loaded_offset
    == 0) must NOT trigger older fetches at the top edge — there's no
    history to page in."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("complete")
    items = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(150)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=items,
        timeline_total=150,
        timeline_next_offset=None,
        timeline_loaded_offset=0,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    captured: list[Any] = []
    panel.older_history_requested.connect(lambda *args: captured.append(args))
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._slide_window_up()
    assert captured == []
    assert panel._loading_older is False
    panel.close()


def test_sliding_window_keeps_widget_count_capped() -> None:
    """Big session → timeline panel materializes only _WINDOW_SIZE widgets,
    and bidirectional sliding preserves that cap exactly."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    QApplication.instance() or QApplication([])
    rec = _record("huge")
    # Build a 600-item synthetic timeline (alternating user / assistant).
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp=f"2026-01-01T00:00:{i % 60:02d}Z",
            text=f"message {i}",
        )
        for i in range(600)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    rendered = panel._timeline_layout.count() - 1  # minus trailing stretch
    assert rendered == _WINDOW_SIZE
    assert panel._window_start == 0
    assert panel._window_end == _WINDOW_SIZE
    # Slide forward — widget count must stay capped at _WINDOW_SIZE.
    for _ in range(3):
        panel._slide_window_down()
    rendered = panel._timeline_layout.count() - 1
    assert rendered == _WINDOW_SIZE
    assert panel._window_end - panel._window_start == _WINDOW_SIZE
    # Slide back — same invariant.
    panel._slide_window_up()
    rendered = panel._timeline_layout.count() - 1
    assert rendered == _WINDOW_SIZE


def test_minimap_click_scrolls_current_window_without_recentering() -> None:
    """Clicking the minimap moves the current viewport only; far-window
    recentering remains reserved for explicit async jumps."""
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("recenter")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 7 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(800)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.resize(900, 700)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._refresh_minimap()
    initial_window = (panel._window_start, panel._window_end)
    scrollbar = panel._timeline_scroll.verticalScrollBar()

    scrollbar.setRange(0, 1600)
    markers = list(panel._navigator._markers)
    panel._navigator.resize(22, 300)
    panel._navigator.set_viewport(
        markers,
        content_height=2000,
        scroll_value=0,
        viewport_height=400,
        scroll_maximum=scrollbar.maximum(),
    )
    target = panel._navigator._target_value_for_y(int(panel._navigator.height() * 0.82))
    assert target > 0
    panel._navigator.scroll_value_requested.emit(target)

    assert (panel._window_start, panel._window_end) == initial_window
    assert scrollbar.value() == target
    assert panel._scroll_throttle.isActive()
    assert not panel._timeline_overlay.isVisible()
    panel.close()


def test_timeline_navigator_edge_drag_slides_window() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("navigator-edge")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 4 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(400)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.resize(900, 700)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 50000)

    initial_window = (panel._window_start, panel._window_end)
    panel._set_scrollbar_value_from_minimap(scrollbar.maximum())
    panel._scroll_throttle.stop()
    panel._on_scroll_settled()

    assert panel._timeline_scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert (panel._window_start, panel._window_end) != initial_window
    assert panel._window_start > initial_window[0]
    assert 0 < scrollbar.value() < scrollbar.maximum()
    panel.close()


def test_empty_audit_line_does_not_reserve_footer_space() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("no-audit")
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=[
            SessionTimelineItem(
                id="e-1",
                type="message:user",
                timestamp="2026-01-01T00:00:00Z",
                text="hello",
            )
        ],
        timeline_total=1,
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    assert panel._audit_label.isHidden()
    panel._set_audit_text("Audit trail")
    assert not panel._audit_label.isHidden()
    panel.close()


def test_minimap_markers_mirror_materialized_window_blocks() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("nav")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"item {i}",
        )
        for i in range(40)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    markers = panel._navigator._markers
    assert len(markers) == panel._timeline_layout.count() - 1
    assert {marker.kind for marker in markers} == {"user", "assistant"}
    for marker in markers:
        kind, payload = panel._all_blocks[marker.block_index]
        assert kind == "single"
        assert marker.kind == ("user" if payload.type == "message:user" else "assistant")


def test_minimap_thumb_ratio_matches_viewport_to_content() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("minimap-ratio")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp=f"2026-01-01T00:00:{i % 60:02d}Z",
            text=f"message {i}",
        )
        for i in range(600)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.resize(900, 700)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._refresh_minimap()

    rail = panel._navigator
    assert rail._markers
    assert rail._content_height > rail._viewport_height > 0
    thumb = rail._thumb_rect()
    assert thumb.height() > 0
    assert thumb.height() / rail.height() == pytest.approx(
        rail._viewport_height / rail._content_height,
        rel=0.02,
        abs=0.02,
    )
    panel.close()


def test_stale_detail_result_dropped_when_selection_changes() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail

    QApplication.instance() or QApplication([])
    rec_a = _record("a")
    rec_b = _record("b")
    factory = _make_manager_factory([rec_a, rec_b])
    captured: list[tuple[str, Any, Any]] = []

    def capture_runner(action, on_success, _on_error):
        # Tag each captured task with whatever id it asks for, by running
        # the action eagerly (it's cheap in the fake) and stashing the result.
        captured.append((action, on_success, _on_error))

    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
        task_runner=capture_runner,
    )
    # Allow the initial list refresh to complete inline so the tree has rows.
    page._task_runner = _sync_task_runner
    page.refresh_if_stale()
    page._task_runner = capture_runner
    captured.clear()
    # Manually drive _on_row_changed for record A, then for record B.
    a_index = page._tree_model.match(  # type: ignore[attr-defined]
        page._tree_model.index(0, 0),
        Qt.UserRole,
        rec_a.id,
        1,
        Qt.MatchRecursive,
    )[0]
    b_index = page._tree_model.match(  # type: ignore[attr-defined]
        page._tree_model.index(0, 0),
        Qt.UserRole,
        rec_b.id,
        1,
        Qt.MatchRecursive,
    )[0]
    page._tree.setCurrentIndex(a_index)
    page._on_row_changed(a_index, QModelIndex())
    assert len(captured) >= 1
    a_action, a_on_success, _ = captured[-1]
    page._tree.setCurrentIndex(b_index)
    page._on_row_changed(b_index, QModelIndex())
    b_action, b_on_success, _ = captured[-1]
    # Deliver the stale A result first — it must not overwrite the panel,
    # because selection is now B.
    page._set_detail = Mock()  # type: ignore[assignment]
    a_detail = SessionDetail(record=rec_a, audit_entries=[], timeline=[], timeline_total=0, timeline_next_offset=None)
    b_detail = SessionDetail(record=rec_b, audit_entries=[], timeline=[], timeline_total=0, timeline_next_offset=None)
    a_on_success(a_detail)
    page._set_detail.assert_not_called()
    b_on_success(b_detail)
    page._set_detail.assert_called_once_with(b_detail)


# --- char-count chip on tool bubbles -----------------------------------------


def test_format_char_count_picks_compact_unit_per_magnitude() -> None:
    from codex_quota_viewer.sessions_page import _format_char_count

    # Plain count under 1k stays raw.
    assert _format_char_count(0) == "0 chars"
    assert _format_char_count(42) == "42 chars"
    assert _format_char_count(999) == "999 chars"
    # Thousands get comma separator until 10k, then switch to k suffix.
    assert _format_char_count(1_234) == "1,234 chars"
    assert _format_char_count(9_999) == "9,999 chars"
    assert _format_char_count(10_000) == "10.0k chars"
    assert _format_char_count(28_341) == "28.3k chars"
    # Past 1M, switch to M suffix so the chip stays narrow.
    assert _format_char_count(1_500_000) == "1.5M chars"


def test_build_tool_size_chip_returns_none_for_empty_payload() -> None:
    QApplication.instance() or QApplication([])
    from codex_quota_viewer.sessions_page import _build_tool_size_chip

    # Empty input/output → no chip (would just be visual noise next to the
    # disabled ▶ toggle).
    assert _build_tool_size_chip("", "") is None
    assert _build_tool_size_chip("", None) is None  # type: ignore[arg-type]


def test_build_tool_size_chip_severity_buckets() -> None:
    QApplication.instance() or QApplication([])
    from codex_quota_viewer.sessions_page import _build_tool_size_chip

    low = _build_tool_size_chip("a" * 100, "")
    mid = _build_tool_size_chip("a" * 5_000, "")
    high = _build_tool_size_chip("a" * 50_000, "b" * 50_000)
    assert low is not None and low.property("severity") == "low"
    assert mid is not None and mid.property("severity") == "mid"
    assert high is not None and high.property("severity") == "high"
    # Tooltip mentions both halves so the user can see the breakdown.
    assert "input" in high.toolTip() and "output" in high.toolTip()


def test_tool_call_bubble_renders_size_chip_when_payload_present() -> None:
    """Wiring: a SessionTimelineItem with input/output produces a header chip
    that survives layout."""
    from codex_quota_viewer.sessions.models import SessionTimelineItem
    from codex_quota_viewer.sessions_page import _ToolCallBubble
    from PySide6.QtWidgets import QLabel, QWidget

    QApplication.instance() or QApplication([])
    item = SessionTimelineItem(
        id="t1",
        type="tool_call",
        timestamp="2026-01-01T10:00:00Z",
        text="",
        tool_name="Bash",
        summary="ls -la",
        input='{"command":"ls -la"}',
        output="total 42\ndrwxr-xr-x ...",
        status="completed",
    )
    parent = QWidget()
    bubble = _ToolCallBubble(item, parent=parent)
    chips = [
        child for child in bubble.findChildren(QLabel)
        if child.objectName() == "SessionsBubbleSizeChip"
    ]
    assert len(chips) == 1
    # The text should reflect the combined char count.
    expected = len(item.input) + len(item.output)
    assert str(expected) in chips[0].text() or "chars" in chips[0].text()


def test_tool_call_bubble_skips_size_chip_when_no_payload() -> None:
    from codex_quota_viewer.sessions.models import SessionTimelineItem
    from codex_quota_viewer.sessions_page import _ToolCallBubble
    from PySide6.QtWidgets import QLabel, QWidget

    QApplication.instance() or QApplication([])
    item = SessionTimelineItem(
        id="t2",
        type="tool_call",
        timestamp="2026-01-01T10:00:00Z",
        text="",
        tool_name="Noop",
        summary=None,
        input="",
        output="",
        status="pending",
    )
    parent = QWidget()
    bubble = _ToolCallBubble(item, parent=parent)
    chips = [
        child for child in bubble.findChildren(QLabel)
        if child.objectName() == "SessionsBubbleSizeChip"
    ]
    assert chips == []


# --- floating scroll-to-top / scroll-to-bottom buttons -----------------------


def test_scroll_jump_buttons_hidden_when_content_fits_viewport() -> None:
    """No scrolling needed (max == 0) → both buttons stay hidden so they don't
    just sit there pointlessly on small sessions."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 0)
    scrollbar.setValue(0)
    panel._update_scroll_jump_buttons()
    assert not panel._scroll_top_btn.isVisible()
    assert not panel._scroll_bottom_btn.isVisible()


def test_scroll_jump_buttons_show_only_bottom_when_at_top() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QWidget

    QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(800, 600)
    panel = _SessionDetailPanel(translator=lambda s: s, parent=parent)
    parent.show()  # need realized geometry so isVisible reflects intent
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 5_000)
    scrollbar.setValue(0)
    panel._update_scroll_jump_buttons()
    # At the top: only "scroll to bottom" makes sense.
    assert not panel._scroll_top_btn.isVisible()
    assert panel._scroll_bottom_btn.isVisible()


def test_scroll_jump_buttons_show_only_top_when_at_bottom() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QWidget

    QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(800, 600)
    panel = _SessionDetailPanel(translator=lambda s: s, parent=parent)
    parent.show()
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 5_000)
    scrollbar.setValue(5_000)
    panel._update_scroll_jump_buttons()
    assert panel._scroll_top_btn.isVisible()
    assert not panel._scroll_bottom_btn.isVisible()


def test_scroll_jump_buttons_show_both_in_middle() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QWidget

    QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(800, 600)
    panel = _SessionDetailPanel(translator=lambda s: s, parent=parent)
    parent.show()
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 5_000)
    scrollbar.setValue(2_500)
    panel._update_scroll_jump_buttons()
    assert panel._scroll_top_btn.isVisible()
    assert panel._scroll_bottom_btn.isVisible()


def test_scroll_jump_buttons_jump_to_extreme_values() -> None:
    """Clicking the jump buttons sets the scrollbar to its extremes."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    scrollbar = panel._timeline_scroll.verticalScrollBar()
    scrollbar.setRange(0, 5_000)
    scrollbar.setValue(2_500)
    panel._scroll_to_top()
    assert scrollbar.value() == 0
    panel._scroll_to_bottom()
    assert scrollbar.value() == 5_000


def _build_search_timeline(total: int, marker: str, matches_at: range) -> list[Any]:
    """Synthetic timeline for the search-filter tests. Items at the
    `matches_at` indices have ``marker`` appended to their text so a
    substring search hits exactly those positions; every item is a
    plain message (no tool_calls) so coalescing is 1 item = 1 block."""
    from codex_quota_viewer.sessions.models import SessionTimelineItem
    matches = set(matches_at)
    return [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp=f"2026-01-01T00:00:{i % 60:02d}Z",
            text=f"item {i} {marker}" if i in matches else f"item {i}",
        )
        for i in range(total)
    ]


def test_search_filter_packs_matches_into_contiguous_window() -> None:
    """All matched bubbles must be materialized contiguously, even when
    the matches lie in the back half of a long timeline that the
    unfiltered window would never reach."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("packed")
    matches_at = range(0, 600, 50)  # 12 matches: 0, 50, 100, ..., 550
    timeline = _build_search_timeline(600, "transparent", matches_at)
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    panel._search_query = "transparent"
    panel._apply_filters()

    assert panel._filtered_block_indices is not None
    assert len(panel._filtered_block_indices) == 12
    assert panel._window_start == 0
    assert panel._window_end == 12
    rendered = panel._timeline_layout.count() - 1  # minus trailing stretch
    assert rendered == 12
    # Every materialized widget belongs to a matching physical block.
    expected = set(matches_at)
    for layout_index in range(rendered):
        widget = panel._timeline_layout.itemAt(layout_index).widget()
        assert widget.property("blockIndex") in expected
    panel.close()


def test_search_filter_can_advance_window_to_later_matches() -> None:
    """When matches outnumber _WINDOW_SIZE, the slide must advance through
    matches — not raw blocks. Regression guard for the bug where a
    filter-collapsed viewport had ``scrollbar.maximum() == 0`` so the
    edge-trigger never fired and late matches stayed unreachable."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    QApplication.instance() or QApplication([])
    rec = _record("advance")
    matches_at = range(0, 600, 3)  # 200 matches
    timeline = _build_search_timeline(600, "transparent", matches_at)
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    panel._search_query = "transparent"
    panel._apply_filters()

    assert panel._filtered_block_indices is not None
    assert len(panel._filtered_block_indices) == 200
    assert panel._window_start == 0
    assert panel._window_end == _WINDOW_SIZE

    last_widget = panel._timeline_layout.itemAt(
        panel._timeline_layout.count() - 2
    ).widget()
    last_block_before = last_widget.property("blockIndex")

    panel._slide_window_down()

    assert panel._window_end > _WINDOW_SIZE
    assert panel._window_end - panel._window_start == _WINDOW_SIZE
    rendered = panel._timeline_layout.count() - 1
    assert rendered == _WINDOW_SIZE
    last_widget_after = panel._timeline_layout.itemAt(
        panel._timeline_layout.count() - 2
    ).widget()
    last_block_after = last_widget_after.property("blockIndex")
    assert last_block_after > last_block_before
    panel.close()


def test_clearing_search_restores_unfiltered_view() -> None:
    """Reset must drop the filtered view back to None and re-seed the
    window with the legacy [0, _WINDOW_SIZE) bounds."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    QApplication.instance() or QApplication([])
    rec = _record("restore")
    matches_at = range(0, 600, 50)
    timeline = _build_search_timeline(600, "transparent", matches_at)
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=timeline,
        timeline_total=len(timeline),
        timeline_next_offset=None,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)

    panel._search_query = "transparent"
    panel._apply_filters()
    assert panel._filtered_block_indices is not None

    panel._on_reset_filters()

    assert panel._filtered_block_indices is None
    assert panel._window_start == 0
    assert panel._window_end == _WINDOW_SIZE
    rendered = panel._timeline_layout.count() - 1
    assert rendered == _WINDOW_SIZE
    panel.close()
