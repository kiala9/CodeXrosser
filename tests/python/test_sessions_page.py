from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from typing import Any  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QModelIndex, QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from codex_quota_viewer.localization import translate  # noqa: E402
from codex_quota_viewer.models import CodexHomeTarget, UiLanguage  # noqa: E402
from codex_quota_viewer.sessions.models import (  # noqa: E402
    Attachment,
    SessionDetail,
    SessionRecord,
    SessionTimelineIndexItem,
    SessionTimelineItem,
)
from codex_quota_viewer.sessions_page import (  # noqa: E402
    SessionsPage,
    _AttachmentCard,
    _FileCard,
    _ImageCard,
    _MessageBubble,
    _RECORD_SUBTITLE_ROLE,
    _SessionsTreeModel,
    _TT_JUMP_OFFSET_ROLE,
    _TT_PREVIEW_ROLE,
    _WINDOW_SIZE,
    _block_preview_text,
    _build_workfolder_groups,
    _decode_data_uri,
    _extract_markdown_attachments,
    _format_size,
    _format_started_at,
    _index_preview_text,
    _index_matches_viewport_position,
    _looks_like_markdown,
    _normalize_preview_text,
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


def test_preview_text_flattens_xml_markdown_and_preserves_emoji() -> None:
    xml = (
        "<cwd>E:\\Download\\Graduation Design\\tmp\\Convertzone</cwd>\n"
        "<shell>powershell</shell>\n"
        "⚠️ Skipped loading 1 skill(s) due to invalid SKILL.md files."
    )
    preview = _normalize_preview_text(xml, max_chars=180)

    assert "\n" not in preview
    assert "<cwd>" not in preview
    assert "cwd: E:\\Download\\Graduation Design\\tmp\\Convertzone" in preview
    assert "shell: powershell" in preview
    assert "⚠" in preview

    markdown = (
        "```yaml\n"
        "$pdf-to-md\n"
        "---\n"
        "name: pdf-to-md\n"
        "<path>C:\\Users\\KIALA\\.codex\\skills\\pdf-to-md\\SKILL.md</path>\n"
        "```\n"
        "- supports **tables** and `code`"
    )
    preview = _normalize_preview_text(markdown, max_chars=220)

    assert "\n" not in preview
    assert "```" not in preview
    assert "**" not in preview
    assert "`" not in preview
    assert "name: pdf-to-md" in preview
    assert "path: C:\\Users\\KIALA\\.codex\\skills\\pdf-to-md\\SKILL.md" in preview
    assert "tables" in preview and "code" in preview

    image_preview = _normalize_preview_text("see ![screenshot](data:image/png;base64,abc")
    assert image_preview == "see screenshot"


def test_time_travel_preview_uses_single_line_plain_text() -> None:
    block = (
        "single",
        SessionTimelineItem(
            id="e-1",
            type="message:user",
            timestamp="2026-01-01T10:00:00Z",
            text="<cwd>C:\\tmp</cwd>\n<shell>powershell</shell>\n## Heading\n✅ done",
        ),
    )

    preview = _block_preview_text(block, translator=lambda s: s, max_chars=160)

    assert "\n" not in preview
    assert "<cwd>" not in preview
    assert "cwd: C:\\tmp" in preview
    assert "shell: powershell" in preview
    assert "Heading" in preview
    assert "✅" in preview

    index_preview = _index_preview_text(
        SessionTimelineIndexItem(
            ordinal=0,
            item_id="e-1",
            type="message:user",
            timestamp="2026-01-01T10:00:00Z",
            preview="```md\n# Title\n- item\n```\n🙂",
        ),
        translator=lambda s: s,
        max_chars=80,
    )
    assert "\n" not in index_preview
    assert "```" not in index_preview
    assert "Title" in index_preview
    assert "item" in index_preview
    assert "🙂" in index_preview


def test_sessions_tree_model_normalizes_record_preview_text() -> None:
    record = _record("preview-record")
    record = SessionRecord(
        **{
            **record.__dict__,
            "user_prompt_excerpt": "<cwd>C:\\tmp</cwd>\n<shell>powershell</shell>",
            "latest_agent_message_excerpt": "## Reply\n- done ✅",
            "cwd": "",
        }
    )
    model = _SessionsTreeModel()
    model.set_records([record])
    group = model.index(0, 0)
    child = model.index(0, 0, group)

    assert child.data(Qt.DisplayRole) == "cwd: C:\\tmp shell: powershell"
    assert child.data(_RECORD_SUBTITLE_ROLE) == "Reply done ✅"


# ----------------------------------------------------------------------
# Image attachment rendering
# ----------------------------------------------------------------------


# 1×1 transparent PNG, encoded as a Codex-style data URI. Same fixture
# used by the parser tests; kept duplicated to avoid cross-file import.
_TEST_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_decode_data_uri_returns_image_for_valid_png() -> None:
    QApplication.instance() or QApplication([])
    image = _decode_data_uri(_TEST_PNG_DATA_URI)
    assert image is not None
    assert not image.isNull()
    assert image.width() == 1
    assert image.height() == 1


def test_decode_data_uri_rejects_malformed_payload() -> None:
    QApplication.instance() or QApplication([])
    assert _decode_data_uri("data:image/png;base64,not-actually-base64!!!") is None
    assert _decode_data_uri("not a data uri") is None
    assert _decode_data_uri("") is None


def test_extract_markdown_attachments_strips_data_uri_image() -> None:
    text = (
        "intro line\n"
        "![cap](" + _TEST_PNG_DATA_URI + ")\n"
        "outro line"
    )
    rewritten, attachments = _extract_markdown_attachments(text)
    assert "![" not in rewritten
    assert "intro line" in rewritten
    assert "outro line" in rewritten
    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].mime == "image/png"
    assert attachments[0].source == "markdown"
    assert attachments[0].alt == "cap"


def test_extract_markdown_attachments_leaves_remote_url_inline() -> None:
    text = "see ![remote](https://example.com/foo.png) inline"
    rewritten, attachments = _extract_markdown_attachments(text)
    # Remote URLs stay in the markdown text so Qt's native renderer can
    # still try to fetch them — no attachment card for those.
    assert "https://example.com/foo.png" in rewritten
    assert attachments == ()


def test_message_bubble_renders_image_card_for_payload_attachment() -> None:
    QApplication.instance() or QApplication([])
    attachment = Attachment(
        kind="image",
        mime="image/png",
        data_uri=_TEST_PNG_DATA_URI,
        source="payload",
    )
    item = SessionTimelineItem(
        id="m-1",
        type="message:user",
        timestamp="2026-04-28T05:10:14Z",
        text="点击seed sandbox弹出",
        attachments=(attachment,),
    )
    bubble = _MessageBubble(
        "user", item.timestamp, item.text, parent=None, attachments=item.attachments
    )
    image_cards = bubble.findChildren(_ImageCard)
    assert len(image_cards) == 1
    assert isinstance(image_cards[0], _AttachmentCard)


def test_message_bubble_extracts_markdown_image_into_card() -> None:
    """A bubble whose text body contains ``![](data:...)`` rewrites the
    body to drop the markdown fragment and adds an ``_ImageCard``."""
    QApplication.instance() or QApplication([])
    text = f"see ![diagram]({_TEST_PNG_DATA_URI}) above"
    item = SessionTimelineItem(
        id="m-2",
        type="message:assistant",
        timestamp="2026-04-28T05:10:15Z",
        text=text,
    )
    bubble = _MessageBubble(
        "assistant",
        item.timestamp,
        item.text,
        parent=None,
        attachments=item.attachments,
    )
    image_cards = bubble.findChildren(_ImageCard)
    assert len(image_cards) == 1


def test_message_bubble_handles_malformed_data_uri_with_placeholder() -> None:
    QApplication.instance() or QApplication([])
    bad_attachment = Attachment(
        kind="image",
        mime="image/png",
        data_uri="data:image/png;base64,xxxxxxxx-not-valid-xxxxxxxx",
        source="payload",
    )
    item = SessionTimelineItem(
        id="m-3",
        type="message:user",
        timestamp="2026-04-28T05:10:16Z",
        text="broken image",
        attachments=(bad_attachment,),
    )
    bubble = _MessageBubble(
        "user", item.timestamp, item.text, parent=None, attachments=item.attachments
    )
    image_cards = bubble.findChildren(_ImageCard)
    # Card still constructs — the failure placeholder is shown only after
    # the card receives its first showEvent (lazy decode). The card's
    # presence with a non-decodable URI is the regression we care about:
    # parser must hand it through without crashing.
    assert len(image_cards) == 1


def test_message_bubble_renders_file_card_for_file_attachment() -> None:
    QApplication.instance() or QApplication([])
    attachment = Attachment(
        kind="file",
        mime="application/pdf",
        path="/tmp/example.pdf",
        name="example.pdf",
        source="payload",
    )
    item = SessionTimelineItem(
        id="m-4",
        type="message:user",
        timestamp="2026-04-28T05:10:17Z",
        text="here's a file",
        attachments=(attachment,),
    )
    bubble = _MessageBubble(
        "user", item.timestamp, item.text, parent=None, attachments=item.attachments
    )
    file_cards = bubble.findChildren(_FileCard)
    assert len(file_cards) == 1


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
    assert page._tree.hasMouseTracking()
    assert page._tree.viewport().hasMouseTracking()


def test_detail_filter_quick_buttons_select_single_role() -> None:
    from codex_quota_viewer.sessions_page import _DETAIL_PANEL_QSS, _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)

    assert set(panel._quick_filter_buttons) == {"user", "assistant"}
    assert panel._quick_filter_buttons["user"].text() == "Only User"
    assert panel._quick_filter_buttons["assistant"].text() == "Only Assistant"
    assert "QPushButton#SessionsDetailQuickFilterButton" in _DETAIL_PANEL_QSS
    assert "QPushButton#SessionsDetailFilterChip:checked" in _DETAIL_PANEL_QSS
    assert "QPushButton#SessionsDetailFilterChip:checked {\n    background: rgba(10, 132, 255, 64)" in _DETAIL_PANEL_QSS
    assert "QPushButton#SessionsDetailQuickFilterButton[active=\"true\"] {\n    background: rgba(10, 132, 255, 64)" in _DETAIL_PANEL_QSS

    panel._quick_filter_buttons["user"].click()
    assert panel._active_chip_kinds == {"user"}
    assert panel._chip_buttons["user"].isChecked()
    assert not panel._chip_buttons["assistant"].isChecked()
    assert panel._quick_filter_buttons["user"].property("active") is True

    panel._quick_filter_buttons["assistant"].click()
    assert panel._active_chip_kinds == {"assistant"}
    assert panel._chip_buttons["assistant"].isChecked()
    assert not panel._chip_buttons["user"].isChecked()
    assert panel._quick_filter_buttons["assistant"].property("active") is True

    panel.close()


def test_detail_toolbar_export_menu_and_time_travel_primary_style() -> None:
    from codex_quota_viewer.sessions_page import _DETAIL_PANEL_QSS, _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)

    assert panel._time_travel_button.objectName() == "SessionsTimeTravelButton"
    assert panel._time_travel_button.text() == "Time Travel"
    assert panel._time_travel_button.minimumWidth() >= 118
    assert "QPushButton#SessionsTimeTravelButton" in _DETAIL_PANEL_QSS
    assert "background: #0A84FF" in _DETAIL_PANEL_QSS

    assert not hasattr(panel, "_screenshot_button")
    assert panel._export_popup.objectName() == "SessionsExportPopup"
    assert panel._export_screenshot_button.text() == "Screenshot"
    assert panel._export_markdown_button.text() == "Export to MD"
    assert panel._export_screenshot_button.parentWidget() is panel._export_popup
    assert panel._export_markdown_button.parentWidget() is panel._export_popup
    assert "QFrame#SessionsFilterPopup,\nQFrame#SessionsExportPopup" in _DETAIL_PANEL_QSS
    assert "QPushButton#SessionsExportMenuItem" in _DETAIL_PANEL_QSS

    panel.close()


def test_detail_filter_quick_buttons_translate_to_chinese() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(
        translator=lambda key: translate(UiLanguage.CHINESE, key)
    )

    assert panel._quick_filter_buttons["user"].text() == "仅 User"
    assert panel._quick_filter_buttons["assistant"].text() == "仅 Assistant"
    assert panel._time_travel_button.text() == "Time Travel"
    assert panel._export_button.text() == "导出"
    assert panel._export_screenshot_button.text() == "截图"
    assert panel._export_markdown_button.text() == "导出 MD"

    panel.close()


def test_sessions_tree_hover_tracks_current_viewport_position() -> None:
    from PySide6.QtWidgets import QTreeView

    app = QApplication.instance() or QApplication([])
    model = _SessionsTreeModel()
    model.set_records(
        [
            _record("first", started_at="2026-01-01T10:00:00Z"),
            _record("second", started_at="2026-01-01T09:00:00Z"),
        ]
    )
    tree = QTreeView()
    tree.setModel(model)
    tree.setHeaderHidden(True)
    tree.resize(520, 240)
    group_index = model.index(0, 0)
    tree.expand(group_index)
    tree.show()
    app.processEvents()

    first_index = model.index(0, 0, group_index)
    second_index = model.index(1, 0, group_index)
    second_pos = tree.visualRect(second_index).center()

    assert _index_matches_viewport_position(tree, second_index, second_pos)
    assert not _index_matches_viewport_position(tree, first_index, second_pos)

    tree.close()


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


def test_initial_sessions_refresh_reads_catalog_without_rescan() -> None:
    QApplication.instance() or QApplication([])
    factory = _make_manager_factory([_record("a")])
    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
        task_runner=_sync_task_runner,
    )
    manager = factory.return_value

    page.refresh_if_stale()

    manager.list_sessions.assert_called_once()
    manager.rescan.assert_not_called()
    assert page._tree_model.session_count() == 1
    assert page._is_stale is False


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


def test_older_loading_consumes_upward_wheel_at_loaded_edge() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("older-wheel")
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
    panel._loading_older = True
    panel._timeline_scroll.verticalScrollBar().setValue(0)

    assert panel._should_consume_timeline_wheel(object())
    panel._loading_older = False
    assert not panel._should_consume_timeline_wheel(object())
    panel.close()


class _WheelEvent:
    def __init__(self, y: int) -> None:
        self._y = y

    def type(self) -> QEvent.Type:
        return QEvent.Wheel

    def angleDelta(self) -> QPoint:  # noqa: N802 - Qt naming
        return QPoint(0, self._y)

    def pixelDelta(self) -> QPoint:  # noqa: N802 - Qt naming
        return QPoint(0, 0)


def _wait_detail_panel_ready(panel, *, timeout: float = 5.0) -> None:
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        overlay_hidden = (
            panel._timeline_overlay is None or panel._timeline_overlay.isHidden()
        )
        if not panel._suppress_edge_slide and overlay_hidden:
            return
        time.sleep(0.001)
    raise AssertionError("detail panel did not settle")


def test_first_upward_wheel_at_static_top_requests_older_history() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("first-wheel-fetch")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"visible loaded item {i}",
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
    panel._timeline_scroll.verticalScrollBar().setValue(0)

    handled = panel.eventFilter(panel._timeline_scroll.viewport(), _WheelEvent(120))

    assert handled
    assert captured == [(rec.id, 600, 200)]
    assert panel._loading_older is True
    panel.close()


def test_scroll_settled_noops_during_scroll_transaction() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("settled-locked")
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
    panel._timeline_scroll.viewport().resize(800, 600)
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    panel._slide_window_up = Mock()  # type: ignore[method-assign]

    panel._suppress_edge_slide = True
    panel._on_scroll_settled()
    panel._slide_window_up.assert_not_called()

    panel._suppress_edge_slide = False
    panel._loading_older = True
    panel._on_scroll_settled()
    panel._slide_window_up.assert_not_called()
    panel.close()


def test_timeline_wheel_filter_covers_bubble_children() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QTextEdit

    QApplication.instance() or QApplication([])
    rec = _record("child-wheel-filter")
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
    body = panel._timeline_container.findChild(QTextEdit)

    assert body is not None
    assert body.property("_cqvTimelineWheelFilterInstalled") is True
    assert panel._is_timeline_event_source(body)

    panel._suppress_edge_slide = True
    assert panel.eventFilter(body, _WheelEvent(120))
    panel.close()


def test_older_result_uses_request_time_anchor() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel
    from PySide6.QtWidgets import QWidget

    QApplication.instance() or QApplication([])
    rec = _record("request-anchor")
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
    requested: list[tuple[str, int, int]] = []
    panel.older_history_requested.connect(
        lambda sid, off, lim: requested.append((sid, off, lim))
    )

    def block_for(item_id: str) -> int:
        return next(
            i
            for i, (kind, payload) in enumerate(panel._all_blocks)
            if kind == "single" and getattr(payload, "id", None) == item_id
        )

    request_top = QWidget()
    request_top.setProperty("blockIndex", block_for("e-803"))
    panel._topmost_visible_widget = lambda: request_top  # type: ignore[method-assign]

    panel._maybe_request_older()

    assert requested == [(rec.id, 600, 200)]
    assert panel._suppress_edge_slide is True

    later_top = QWidget()
    later_top.setProperty("blockIndex", block_for("e-820"))
    panel._topmost_visible_widget = lambda: later_top  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    def stub_set(block_index: int, offset: int, raw_scroll: int) -> int:
        captured["calls"] = int(captured.get("calls", 0)) + 1
        captured["block_index"] = block_index
        captured["offset"] = offset
        captured["raw_scroll"] = raw_scroll
        return 0

    panel._set_prepend_anchor_scroll = stub_set  # type: ignore[method-assign]

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
    _wait_detail_panel_ready(panel)

    assert captured["block_index"] == panel._block_for_anchor_id("e-803")
    assert captured["block_index"] != panel._block_for_anchor_id("e-820")
    assert captured["calls"] >= 1
    request_anchor = panel._block_for_anchor_id("e-803")
    assert request_anchor is not None
    assert panel._window_start < request_anchor < panel._window_end
    assert panel._suppress_edge_slide is False
    panel.close()


def test_older_prepend_keeps_loaded_edge_at_viewport_top() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    app = QApplication.instance() or QApplication([])
    rec = _record("loaded-edge-stability")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"visible loaded item {i}",
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
    panel.show()
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    app.processEvents()
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    app.processEvents()

    before_id = panel._anchor_id_for_widget(panel._topmost_visible_widget())
    assert before_id == "e-800"

    panel._maybe_request_older()
    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"older item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)
    _wait_detail_panel_ready(panel)

    after_id = panel._anchor_id_for_widget(panel._topmost_visible_widget())
    assert after_id == before_id
    anchor_block = panel._block_for_anchor_id("e-800")
    assert anchor_block is not None
    assert panel._window_start < anchor_block < panel._window_end
    assert panel._window_end == panel._window_start + _WINDOW_SIZE
    assert panel._timeline_scroll.verticalScrollBar().value() > 0
    panel.close()


def test_older_prepend_reuses_visible_anchor_widget_without_overlay_rebuild() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    app = QApplication.instance() or QApplication([])
    rec = _record("prepend-reuse-anchor-widget")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"visible loaded item {i}",
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
    panel.show()
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    app.processEvents()
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    app.processEvents()

    before_top = panel._topmost_visible_widget()
    assert before_top is not None
    assert panel._anchor_id_for_widget(before_top) == "e-800"

    panel._maybe_request_older()
    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"older item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)

    rendered_widgets = [
        panel._timeline_layout.itemAt(i).widget()
        for i in range(panel._timeline_layout.count() - 1)
    ]
    assert len(rendered_widgets) == _WINDOW_SIZE
    assert before_top in rendered_widgets
    assert panel._topmost_visible_widget() is before_top
    assert panel._timeline_overlay is not None
    assert panel._timeline_overlay.isHidden()
    panel.close()


def test_older_prepend_does_not_expose_intermediate_headroom_before_anchor_restore() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    app = QApplication.instance() or QApplication([])
    rec = _record("prepend-hidden-intermediate")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=(f"visible loaded item {i} ") * 8,
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
    panel.show()
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    for _ in range(10):
        app.processEvents()
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    for _ in range(3):
        app.processEvents()

    before_id = panel._anchor_id_for_widget(panel._topmost_visible_widget())
    assert before_id == "e-800"

    panel._maybe_request_older()
    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=(f"older item {i} ") * 8,
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)

    assert panel._timeline_layout.count() - 1 > 0
    assert panel._timeline_overlay is not None
    assert panel._timeline_overlay.isHidden()
    assert panel._anchor_id_for_widget(panel._topmost_visible_widget()) == before_id
    _wait_detail_panel_ready(panel)
    assert panel._anchor_id_for_widget(panel._topmost_visible_widget()) == before_id
    assert panel._timeline_scroll.verticalScrollBar().value() > 0
    panel.close()


def test_consecutive_older_prepends_keep_visible_anchor_and_headroom() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    def make_items(start: int, end: int, label: str) -> list[SessionTimelineItem]:
        return [
            SessionTimelineItem(
                id=f"e-{i}",
                type="message:user" if i % 5 == 0 else "message:assistant",
                timestamp="2026-01-01T00:00:00Z",
                text=f"{label} item {i}",
            )
            for i in range(start, end)
        ]

    app = QApplication.instance() or QApplication([])
    rec = _record("prepend-two-pages")
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=make_items(800, 1000, "tail"),
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=800,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.resize(900, 700)
    panel.show()
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    for _ in range(8):
        app.processEvents()

    for offset in (600, 400):
        panel._timeline_scroll.verticalScrollBar().setValue(0)
        for _ in range(3):
            app.processEvents()
        before_id = panel._anchor_id_for_widget(panel._topmost_visible_widget())
        assert before_id is not None

        panel._maybe_request_older()
        panel.prepend_older_items(make_items(offset, offset + 200, "older"), offset)
        _wait_detail_panel_ready(panel)

        assert panel._loaded_offset == offset
        assert panel._anchor_id_for_widget(panel._topmost_visible_widget()) == before_id
        anchor_block = panel._block_for_anchor_id(before_id)
        assert anchor_block is not None
        assert panel._window_start < anchor_block < panel._window_end
        assert panel._timeline_scroll.verticalScrollBar().value() > 0

    panel.close()


def test_older_prepend_refreshes_minimap_window_before_full_geometry_scan() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    app = QApplication.instance() or QApplication([])
    rec = _record("prepend-fast-minimap")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"visible loaded item {i}",
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
    app.processEvents()
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    before_widget = panel._topmost_visible_widget()
    assert before_widget is not None
    panel._maybe_request_older()
    panel._refresh_minimap = Mock()  # type: ignore[method-assign]

    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"older item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)
    assert panel._timeline_overlay is not None
    assert panel._timeline_overlay.isHidden()
    rendered_widgets = [
        panel._timeline_layout.itemAt(i).widget()
        for i in range(panel._timeline_layout.count() - 1)
    ]
    assert before_widget in rendered_widgets
    assert panel._timeline_layout.count() - 1 == _WINDOW_SIZE
    _wait_detail_panel_ready(panel)

    assert panel._suppress_edge_slide is False
    assert panel._timeline_overlay is not None
    assert panel._timeline_overlay.isHidden()
    assert not panel._should_consume_timeline_wheel(object())
    assert panel._navigator._markers
    assert panel._navigator._markers[0].block_index == panel._window_start
    assert panel._navigator._markers[-1].block_index == panel._window_end - 1
    assert panel._navigator._scroll_maximum == panel._timeline_scroll.verticalScrollBar().maximum()
    panel._refresh_minimap.assert_not_called()
    panel.close()


def test_upward_wheel_after_prepend_slides_into_loaded_headroom() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    app = QApplication.instance() or QApplication([])
    rec = _record("prepend-wheel-headroom")
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"visible loaded item {i}",
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
    panel.show()
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    app.processEvents()
    panel._timeline_scroll.verticalScrollBar().setValue(0)
    panel._maybe_request_older()
    older = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"older item {i}",
        )
        for i in range(600, 800)
    ]
    panel.prepend_older_items(older, 600)
    _wait_detail_panel_ready(panel)

    old_start = panel._window_start
    bar = panel._timeline_scroll.verticalScrollBar()
    assert old_start > 0
    assert bar.value() > 0
    bar.setValue(0)

    handled = panel.eventFilter(panel._timeline_scroll.viewport(), _WheelEvent(120))

    assert handled
    assert panel._window_start < old_start
    assert panel._loading_older is False
    panel.close()


def test_downward_wheel_at_static_bottom_requests_newer_history() -> None:
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("bottom-wheel-fetch")
    page = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 5 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"middle item {i}",
        )
        for i in range(200, 400)
    ]
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=page,
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=200,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    captured: list[tuple[str, int, int]] = []
    panel.newer_history_requested.connect(
        lambda sid, off, lim: captured.append((sid, off, lim))
    )
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._recenter_window(len(panel._all_blocks) - 1)
    bar = panel._timeline_scroll.verticalScrollBar()
    bar.setRange(0, 500)
    bar.setValue(bar.maximum())

    handled = panel.eventFilter(panel._timeline_scroll.viewport(), _WheelEvent(-120))

    assert handled
    assert captured == [(rec.id, 400, 200)]
    assert panel._loading_newer is True
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

    def stub_set(block_index: int, offset: int, raw_scroll: int) -> int:
        captured["block_index"] = block_index
        captured["offset"] = offset
        captured["raw_scroll"] = raw_scroll
        return 0

    panel._set_prepend_anchor_scroll = stub_set  # type: ignore[method-assign]

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
    _wait_detail_panel_ready(panel)

    # The anchor must be the new index of e-803, not of the surrounding
    # user prompts. _block_for_anchor_id resolves the post-recoalesce
    # block index for the same id we captured before the prepend.
    new_target_block = panel._block_for_anchor_id(target_id)
    assert new_target_block is not None
    assert captured.get("block_index") == new_target_block
    user_block_e800 = panel._block_for_message_id("e-800")
    user_block_e805 = panel._block_for_message_id("e-805")
    assert captured.get("block_index") not in {user_block_e800, user_block_e805}
    assert panel._suppress_edge_slide is False
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


def test_large_timeline_window_renders_in_event_loop_chunks() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _WINDOW_SIZE

    app = QApplication.instance() or QApplication([])
    rec = _record("large-render")
    timeline = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp=f"2026-01-01T00:00:{i % 60:02d}Z",
            text="x" * 512,
        )
        for i in range(_WINDOW_SIZE + 40)
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

    assert panel._timeline_layout.count() - 1 == 0
    assert not panel._timeline_overlay.isHidden()

    deadline = time.monotonic() + 5
    while panel._timeline_layout.count() - 1 < _WINDOW_SIZE:
        app.processEvents()
        if time.monotonic() > deadline:
            break

    assert panel._timeline_layout.count() - 1 == _WINDOW_SIZE
    assert panel._timeline_overlay.isHidden()
    assert panel._suppress_edge_slide is False
    panel.close()


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


def test_selecting_session_shows_placeholder_before_detail_worker_returns() -> None:
    QApplication.instance() or QApplication([])
    rec = _record("deferred-detail")
    factory = _make_manager_factory([rec])
    captured: list[tuple[Any, Any, Any]] = []

    def capture_runner(action, on_success, on_error):
        captured.append((action, on_success, on_error))

    page = SessionsPage(
        sessions_manager_factory=factory,
        confirm_real_action=Mock(return_value=True),
        task_runner=_sync_task_runner,
    )
    page.refresh_if_stale()
    page._task_runner = capture_runner
    page._detail_panel.show_loading_placeholder = Mock()  # type: ignore[method-assign]
    page._set_detail = Mock()  # type: ignore[assignment]
    index = page._tree_model.match(  # type: ignore[attr-defined]
        page._tree_model.index(0, 0),
        Qt.UserRole,
        rec.id,
        1,
        Qt.MatchRecursive,
    )[0]

    page._tree.setCurrentIndex(index)
    page._on_row_changed(index, QModelIndex())

    page._detail_panel.show_loading_placeholder.assert_called_once_with(rec)
    page._set_detail.assert_not_called()
    assert captured


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


# ---------------------------------------------------------------------------
# Time Travel popup
# ---------------------------------------------------------------------------


def _make_time_travel_detail(
    record_id: str,
    *,
    user_count: int = 4,
    asst_per_user: int = 1,
    tool_per_user: int = 0,
    tool_group_size: int = 0,
) -> Any:
    """Build a SessionDetail with a known shape for Time Travel tests.

    Each "turn" is: 1 user message + ``asst_per_user`` assistant messages
    + ``tool_per_user`` isolated tool calls + (if tool_group_size > 1)
    a coalesced tool_group of that size. Returns the SessionDetail.
    """
    from codex_quota_viewer.sessions.models import SessionDetail, SessionTimelineItem

    items: list[SessionTimelineItem] = []
    counter = 0
    for u in range(user_count):
        items.append(
            SessionTimelineItem(
                id=f"u-{u}",
                type="message:user",
                timestamp=f"2026-01-01T0{u}:00:00Z",
                text=f"user prompt {u}",
            )
        )
        for a in range(asst_per_user):
            items.append(
                SessionTimelineItem(
                    id=f"a-{u}-{a}",
                    type="message:assistant",
                    timestamp=f"2026-01-01T0{u}:00:30Z",
                    text=f"assistant reply {u}-{a}",
                )
            )
        for t in range(tool_per_user):
            counter += 1
            items.append(
                SessionTimelineItem(
                    id=f"t-{u}-{t}",
                    type="tool_call",
                    timestamp=f"2026-01-01T0{u}:01:00Z",
                    tool_name=f"tool_{counter % 3}",
                    summary=f"call {counter}",
                    status="completed",
                )
            )
        if tool_group_size > 1:
            for g in range(tool_group_size):
                items.append(
                    SessionTimelineItem(
                        id=f"g-{u}-{g}",
                        type="tool_call",
                        timestamp=f"2026-01-01T0{u}:02:00Z",
                        tool_name=f"grouped_{g}",
                        summary=f"grouped call {g}",
                        status="completed",
                    )
                )
    return SessionDetail(
        record=_record(record_id),
        audit_entries=[],
        timeline=items,
        timeline_total=len(items),
        timeline_next_offset=None,
    )


def _open_time_travel_for_test(panel: Any) -> Any:
    """Test helper: stand up a Time Travel popup attached to ``panel``
    without going through the click path's ``show()`` /
    ``install_dwm_chrome()`` / ``activateWindow()`` calls. Those calls
    are fine in production but unsafe in offscreen pytest mode after
    many prior tests have left orphaned top-level widgets behind —
    activating a focus-accepting Tool window then walks stale window
    pointers and crashes with an access violation. The popup itself
    plus all wiring is identical to the live path."""
    from codex_quota_viewer.sessions_page import _TimeTravelPopup

    if panel._time_travel_popup is not None:
        return panel._time_travel_popup
    host = panel.window() or panel
    popup = _TimeTravelPopup(panel._translator, host)
    popup.dismiss_requested.connect(panel._dismiss_time_travel_popup)
    popup.blockJumpRequested.connect(panel._on_time_travel_jump)
    popup.offsetJumpRequested.connect(panel._on_time_travel_offset_jump)
    panel._time_travel_popup = popup
    panel._push_time_travel_data()
    return popup


def test_time_travel_button_click_invokes_handler() -> None:
    """The clock button is wired to ``_on_time_travel_clicked`` — verify
    the connection without triggering the Qt show pipeline (offscreen
    pytest cannot survive show + activate of a focus-accepting Tool
    window after many prior tests). Lifecycle behaviour is exercised
    in the dedicated lifecycle tests below using ``_open_time_travel_for_test``."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(_make_time_travel_detail("tt-click"), CodexHomeTarget.SANDBOX)

    fired: list[bool] = []
    panel._on_time_travel_clicked = (  # type: ignore[method-assign]
        lambda: fired.append(True)
    )
    panel._time_travel_button.clicked.emit()
    assert fired == [True]
    panel.close()


def test_time_travel_popup_is_a_frosted_surface() -> None:
    """The popup must subclass ``_FrostedSurface`` so it inherits the
    ESC + click-outside dismissal contract and the acrylic chrome path
    — the rest of the system relies on this."""
    from codex_quota_viewer.frosted_surface import _FrostedSurface
    from codex_quota_viewer.sessions_page import _SessionDetailPanel, _TimeTravelPopup

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(_make_time_travel_detail("tt-frosted"), CodexHomeTarget.SANDBOX)
    popup = _open_time_travel_for_test(panel)

    assert isinstance(popup, _TimeTravelPopup)
    assert isinstance(popup, _FrostedSurface)
    assert popup.ACCEPT_FOCUS is True
    assert popup.DISMISS_ON_ESCAPE is True
    assert popup.DISMISS_ON_DEACTIVATE is True
    panel._dismiss_time_travel_popup()
    panel.close()


def test_time_travel_row_click_jumps_without_closing_popup() -> None:
    """A row click routes through _on_time_travel_jump, which calls
    _recenter_async with the picked block index. Popup must NOT close —
    rapid-browse semantics requires the user to be able to keep picking
    targets after a jump."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(
        _make_time_travel_detail("tt-jump", user_count=10, asst_per_user=2),
        CodexHomeTarget.SANDBOX,
    )
    _open_time_travel_for_test(panel)

    captured: list[int] = []
    panel._recenter_async = (  # type: ignore[method-assign]
        lambda focus_block, *, on_anchor: captured.append(focus_block)
    )

    target = 7
    panel._on_time_travel_jump(target)

    assert captured == [target]
    assert panel._time_travel_popup is not None
    panel._dismiss_time_travel_popup()
    panel.close()


def test_time_travel_jump_clamps_to_block_range() -> None:
    """Out-of-range block indices are dropped — the vertical view emits
    a block index per row and we cannot trust the source widget to
    never emit a stale index."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(_make_time_travel_detail("tt-clamp"), CodexHomeTarget.SANDBOX)

    captured: list[int] = []
    panel._recenter_async = (  # type: ignore[method-assign]
        lambda focus_block, *, on_anchor: captured.append(focus_block)
    )

    panel._on_time_travel_jump(-1)
    panel._on_time_travel_jump(99999)
    assert captured == []
    panel.close()


def test_time_travel_global_index_replaces_loaded_slice_rows() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"tail {i}",
        )
        for i in range(800, 1000)
    ]
    rec = _record("tt-global-index")
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
    popup = _open_time_travel_for_test(panel)
    index_items = [
        SessionTimelineIndexItem(
            ordinal=i,
            item_id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            preview=f"global {i}",
        )
        for i in range(1000)
    ]

    panel.set_time_travel_index(rec.id, index_items)

    assert popup._vertical._model.rowCount() == 1000
    assert popup._vertical._model.index(100, 0).data(_TT_JUMP_OFFSET_ROLE) == 100
    panel.close()


def test_time_travel_offset_jump_requests_page_for_unloaded_offset() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    tail = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"tail {i}",
        )
        for i in range(800, 1000)
    ]
    rec = _record("tt-offset-request")
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
    captured: list[Any] = []
    panel.timeline_offset_requested.connect(lambda *args: captured.append(args))

    panel._on_time_travel_offset_jump(100, "e-100")

    assert captured == [(rec.id, 0, 200, 100, "e-100")]
    assert not panel._timeline_overlay.isHidden()
    panel.close()


def test_time_travel_offset_page_replaces_slice_and_centers_focus() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("tt-offset-apply")
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=[
            SessionTimelineItem(
                id=f"e-{i}",
                type="message:user" if i % 2 == 0 else "message:assistant",
                timestamp="2026-01-01T00:00:00Z",
                text=f"tail {i}",
            )
            for i in range(800, 1000)
        ],
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=800,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    page_items = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"page {i}",
        )
        for i in range(0, 200)
    ]

    panel.replace_timeline_page(
        page_items,
        sql_offset=0,
        total=1000,
        focus_offset=100,
        focus_item_id="e-100",
    )

    focus_block = panel._block_for_anchor_id("e-100")
    assert panel._loaded_offset == 0
    assert panel._timeline_total == 1000
    assert focus_block is not None
    assert panel._window_start <= focus_block < panel._window_end
    rendered = panel._timeline_layout.count() - 1
    assert rendered <= _WINDOW_SIZE
    panel.close()


def test_time_travel_middle_page_requests_newer_at_bottom_edge() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("tt-newer-request")
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=[
            SessionTimelineItem(
                id=f"e-{i}",
                type="message:user" if i % 2 == 0 else "message:assistant",
                timestamp="2026-01-01T00:00:00Z",
                text=f"page {i}",
            )
            for i in range(200, 400)
        ],
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=200,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._recenter_window(len(panel._all_blocks) - 1)
    captured: list[Any] = []
    panel.newer_history_requested.connect(lambda *args: captured.append(args))

    panel._slide_window_down()

    assert captured == [(rec.id, 400, 200)]
    assert panel._loading_newer is True
    panel._slide_window_down()
    assert captured == [(rec.id, 400, 200)]
    panel.close()


def test_appending_newer_items_continues_downward_window_after_time_travel() -> None:
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    rec = _record("tt-newer-append")
    detail = SessionDetail(
        record=rec,
        audit_entries=[],
        timeline=[
            SessionTimelineItem(
                id=f"e-{i}",
                type="message:user" if i % 2 == 0 else "message:assistant",
                timestamp="2026-01-01T00:00:00Z",
                text=f"page {i}",
            )
            for i in range(200, 400)
        ],
        timeline_total=1000,
        timeline_next_offset=None,
        timeline_loaded_offset=200,
    )
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(detail, CodexHomeTarget.SANDBOX)
    panel._recenter_window(len(panel._all_blocks) - 1)
    old_end = panel._window_end
    newer = [
        SessionTimelineItem(
            id=f"e-{i}",
            type="message:user" if i % 2 == 0 else "message:assistant",
            timestamp="2026-01-01T00:00:00Z",
            text=f"newer {i}",
        )
        for i in range(400, 600)
    ]

    panel.append_newer_items(newer, 400, 1000)

    assert panel._loading_newer is False
    assert len(panel._all_timeline_items) == 400
    assert panel._window_end > old_end
    rendered = panel._timeline_layout.count() - 1
    assert rendered <= _WINDOW_SIZE
    panel.close()


def test_time_travel_vertical_model_one_row_per_tool_group() -> None:
    """A coalesced tool_group block contributes exactly one row to the
    vertical view — the inner tool calls collapse into a single
    ``Tool calls · N`` summary row, mirroring how the minimap segments
    treat them."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelVerticalModel,
        _coalesce_timeline_blocks,
        _TT_PREVIEW_ROLE,
        _TT_ROLE_ROLE,
    )

    QApplication.instance() or QApplication([])
    # 1 user + 4-call tool group + 1 assistant = 3 blocks total.
    detail = _make_time_travel_detail(
        "tt-group", user_count=1, asst_per_user=1, tool_group_size=4
    )
    blocks = _coalesce_timeline_blocks(detail.timeline)
    group_idx = next(i for i, (k, _p) in enumerate(blocks) if k == "tool_group")

    model = _TimeTravelVerticalModel()
    model.set_translator(lambda s: s)
    model.refresh(blocks, None)

    assert model.rowCount() == len(blocks)
    group_row = model.index(group_idx, 0)
    assert group_row.data(_TT_ROLE_ROLE) == "tool_group"
    preview = group_row.data(_TT_PREVIEW_ROLE)
    assert isinstance(preview, str)
    assert "Tool calls" in preview and "4" in preview


def test_time_travel_filter_proxy_matches_text_and_tool_name() -> None:
    """The popup's own search filters rows by both preview text and tool
    name. Independent from the panel filter — that one drives the
    ``IsFilteredOutRole`` dim, this one hides rows entirely."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelFilterProxy,
        _TimeTravelVerticalModel,
        _coalesce_timeline_blocks,
    )

    QApplication.instance() or QApplication([])
    detail = _make_time_travel_detail(
        "tt-proxy", user_count=2, asst_per_user=1, tool_per_user=2
    )
    blocks = _coalesce_timeline_blocks(detail.timeline)
    model = _TimeTravelVerticalModel()
    model.set_translator(lambda s: s)
    model.refresh(blocks, None)

    proxy = _TimeTravelFilterProxy()
    proxy.setSourceModel(model)
    assert proxy.rowCount() == len(blocks)

    # ``invalidate()`` is synchronous in QSortFilterProxyModel — no need
    # to pump events between set_filter_text and rowCount.
    proxy.set_filter_text("tool_1")  # matches a tool_name → tool_call rows
    assert 0 < proxy.rowCount() < len(blocks)

    proxy.set_filter_text("user prompt")  # matches preview text → user msgs
    assert 0 < proxy.rowCount() < len(blocks)

    proxy.set_filter_text("")
    assert proxy.rowCount() == len(blocks)


def test_time_travel_vertical_model_marks_filtered_rows() -> None:
    """When a panel-level filter is active, the vertical model marks
    excluded rows with ``IsFilteredOutRole=True`` so the delegate dims
    them — the rows still exist (Time Travel always shows the full
    session)."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelVerticalModel,
        _coalesce_timeline_blocks,
        _TT_FILTERED_OUT_ROLE,
    )

    QApplication.instance() or QApplication([])
    detail = _make_time_travel_detail("tt-dim", user_count=3, asst_per_user=1)
    blocks = _coalesce_timeline_blocks(detail.timeline)
    allowed = [0, 2]

    model = _TimeTravelVerticalModel()
    model.set_translator(lambda s: s)
    model.refresh(blocks, allowed)

    assert model.rowCount() == len(blocks)
    in_rows = [
        i for i in range(model.rowCount())
        if not model.index(i, 0).data(_TT_FILTERED_OUT_ROLE)
    ]
    assert in_rows == allowed


def test_time_travel_popup_disposed_on_session_switch() -> None:
    """Switching sessions while the popup is open must dispose it — its
    block list / window indices belong to the previous session and would
    point at stale data after set_detail rewires _all_blocks."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(_make_time_travel_detail("tt-first"), CodexHomeTarget.SANDBOX)
    _open_time_travel_for_test(panel)
    assert panel._time_travel_popup is not None

    panel.set_detail(_make_time_travel_detail("tt-second"), CodexHomeTarget.SANDBOX)
    assert panel._time_travel_popup is None
    panel.close()


def test_time_travel_popup_disposed_on_dismiss_signal() -> None:
    """ESC and click-outside both flow through ``dismiss_requested``,
    which the panel hooks to its dispose path."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(_make_time_travel_detail("tt-dismiss"), CodexHomeTarget.SANDBOX)
    popup = _open_time_travel_for_test(panel)

    popup.dismiss_requested.emit()
    assert panel._time_travel_popup is None
    panel.close()


def test_time_travel_popup_handles_empty_session() -> None:
    """An empty timeline — set_detail with detail=None then opening the
    popup — must not crash. The model has zero rows."""
    from codex_quota_viewer.sessions_page import _SessionDetailPanel

    QApplication.instance() or QApplication([])
    panel = _SessionDetailPanel(translator=lambda s: s)
    panel.set_detail(None, CodexHomeTarget.SANDBOX)

    popup = _open_time_travel_for_test(panel)
    assert popup._vertical._model.rowCount() == 0

    panel._dismiss_time_travel_popup()
    panel.close()


def test_time_travel_vertical_list_pins_dark_viewport() -> None:
    """The vertical QListView must not inherit a light Windows theme's
    white viewport / black text palette."""
    from codex_quota_viewer.sessions_page import (
        _DETAIL_PANEL_QSS,
        _TimeTravelVerticalView,
    )

    QApplication.instance() or QApplication([])
    view = _TimeTravelVerticalView(translator=lambda s: s)

    assert view._list.objectName() == "TimeTravelVerticalList"
    assert view._list.viewport().styleSheet() == "background: transparent;"
    assert view._kind_chips["user"].objectName() == "SessionsDetailFilterChip"
    assert "QListView#TimeTravelVerticalList" in _DETAIL_PANEL_QSS
    assert "selection-color: #ffffff" in _DETAIL_PANEL_QSS

    view.close()


def test_time_travel_row_background_path_respects_rounded_edges() -> None:
    """First/last selected rows should not paint square blue corners
    outside the list container's rounded border."""
    from PySide6.QtCore import QRect

    from codex_quota_viewer.sessions_page import _time_travel_row_background_path

    rect = QRect(0, 0, 200, 36)

    first = _time_travel_row_background_path(rect, row=0, row_count=3).boundingRect()
    middle = _time_travel_row_background_path(rect, row=1, row_count=3).boundingRect()
    last = _time_travel_row_background_path(rect, row=2, row_count=3).boundingRect()
    only = _time_travel_row_background_path(rect, row=0, row_count=1).boundingRect()

    assert first.left() >= 1.0
    assert first.top() >= 1.0
    assert middle.top() == 0.0
    assert last.left() >= 1.0
    assert last.bottom() <= rect.y() + rect.height() - 1.0
    assert only.top() >= 1.0
    assert only.bottom() <= rect.y() + rect.height() - 1.0


def test_time_travel_viewport_clip_path_rounds_scrolled_edges() -> None:
    """A middle row scrolled against the viewport edge is still clipped
    by the list's rounded viewport, not just by its own row rect."""
    from PySide6.QtCore import QPointF, QRect

    from codex_quota_viewer.sessions_page import _time_travel_viewport_clip_path

    path = _time_travel_viewport_clip_path(QRect(0, 0, 200, 100))

    assert not path.contains(QPointF(1.0, 1.0))
    assert not path.contains(QPointF(199.0, 1.0))
    assert not path.contains(QPointF(1.0, 99.0))
    assert not path.contains(QPointF(199.0, 99.0))
    assert path.contains(QPointF(8.0, 1.5))
    assert path.contains(QPointF(100.0, 1.5))
    assert path.contains(QPointF(100.0, 98.5))


def test_time_travel_role_for_block_classifies_all_kinds() -> None:
    """``_role_for_block`` is the single source of truth for the
    vertical-view's role-dot colouring — guard the four cases together
    so a rename or new TimelineKind doesn't silently shift one surface
    relative to the other."""
    from codex_quota_viewer.sessions.models import SessionTimelineItem
    from codex_quota_viewer.sessions_page import _role_for_block

    user = SessionTimelineItem(id="u", type="message:user", timestamp="t", text="x")
    asst = SessionTimelineItem(id="a", type="message:assistant", timestamp="t", text="x")
    tool = SessionTimelineItem(id="c", type="tool_call", timestamp="t", tool_name="bash")

    assert _role_for_block(("single", user)) == "user"
    assert _role_for_block(("single", asst)) == "assistant"
    assert _role_for_block(("single", tool)) == "tool"
    assert _role_for_block(("tool_group", [tool, tool])) == "tool_group"


def test_time_travel_vertical_view_chip_toggles_filter_rows() -> None:
    """Toggling a kind chip in the vertical view hides every row whose
    role matches that kind. Combined with the text search via AND."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelVerticalView,
        _coalesce_timeline_blocks,
    )

    QApplication.instance() or QApplication([])
    detail = _make_time_travel_detail(
        "tt-chip-filter", user_count=3, asst_per_user=2, tool_per_user=1
    )
    blocks = _coalesce_timeline_blocks(detail.timeline)

    view = _TimeTravelVerticalView(translator=lambda s: s)
    view.refresh(blocks, None, None)
    total = view._proxy.rowCount()
    assert total == len(blocks)

    # Untoggle "Assistant" → only user + tool rows remain.
    view._kind_chips["assistant"].setChecked(False)
    after_no_assistant = view._proxy.rowCount()
    assert 0 < after_no_assistant < total

    # Untoggle "User" too → only tool/tool_group rows remain.
    view._kind_chips["user"].setChecked(False)
    after_only_tools = view._proxy.rowCount()
    assert 0 < after_only_tools < after_no_assistant

    # Untoggle "Tool" → empty list.
    view._kind_chips["tool"].setChecked(False)
    assert view._proxy.rowCount() == 0

    # Re-check everything → all rows back.
    for chip in view._kind_chips.values():
        chip.setChecked(True)
    assert view._proxy.rowCount() == total


def test_time_travel_vertical_view_tool_chip_covers_tool_group_too() -> None:
    """The "Tool" chip is a single user-facing toggle that controls
    both single tool_call AND coalesced tool_group blocks — coalescing
    is internal rendering, not a category the user thinks about."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelVerticalView,
        _coalesce_timeline_blocks,
    )

    QApplication.instance() or QApplication([])
    detail = _make_time_travel_detail(
        "tt-tool-chip",
        user_count=1,
        asst_per_user=0,
        tool_per_user=0,
        tool_group_size=4,
    )
    blocks = _coalesce_timeline_blocks(detail.timeline)
    # Sanity: the test data has a tool_group block.
    assert any(kind == "tool_group" for kind, _p in blocks)

    view = _TimeTravelVerticalView(translator=lambda s: s)
    view.refresh(blocks, None, None)
    total = view._proxy.rowCount()
    assert total == len(blocks)

    # Untoggle Tool — both single tool calls and tool_group rows
    # should disappear, leaving only the user prompt.
    view._kind_chips["tool"].setChecked(False)
    remaining_kinds: list[str] = []
    for r in range(view._proxy.rowCount()):
        proxy_idx = view._proxy.index(r, 0)
        source_idx = view._proxy.mapToSource(proxy_idx)
        from codex_quota_viewer.sessions_page import _TT_ROLE_ROLE
        remaining_kinds.append(source_idx.data(_TT_ROLE_ROLE))
    assert "tool" not in remaining_kinds
    assert "tool_group" not in remaining_kinds


def test_time_travel_vertical_view_chip_and_text_filters_compose() -> None:
    """Chip and text filters AND together — a row passes only when its
    kind is enabled AND its preview/tool name matches the needle."""
    from codex_quota_viewer.sessions_page import (
        _TimeTravelVerticalView,
        _coalesce_timeline_blocks,
    )

    QApplication.instance() or QApplication([])
    detail = _make_time_travel_detail(
        "tt-and", user_count=3, asst_per_user=2, tool_per_user=1
    )
    blocks = _coalesce_timeline_blocks(detail.timeline)

    view = _TimeTravelVerticalView(translator=lambda s: s)
    view.refresh(blocks, None, None)

    # Just the search "tool_" → matches tool rows only.
    view._proxy.set_filter_text("tool_")
    text_only = view._proxy.rowCount()
    assert text_only > 0

    # Now also disable Tool chip — combined filter must drop to zero.
    view._kind_chips["tool"].setChecked(False)
    assert view._proxy.rowCount() == 0
