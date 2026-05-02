from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from PySide6.QtCore import QEvent, QPoint, QPointF, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget  # noqa: E402

from codex_quota_viewer.qt_app import ConfirmPhraseDialog, MainWindow, StatusBanner, StatusPopupFrame  # noqa: E402
from codex_quota_viewer.models import AccountMetadata, AccountRecord, AuthMode, CodexHomeTarget, UiLanguage, now_utc  # noqa: E402
from codex_quota_viewer.task_worker import emit, serialize  # noqa: E402


def test_button_wrapper_ignores_qt_checked_argument() -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []

    button = MainWindow._button(None, "Switch", lambda: calls.append("clicked"))

    button.click()

    assert calls == ["clicked"]


def test_confirm_phrase_requires_full_real_switch_phrase() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ConfirmPhraseDialog("Switch Real Codex", "Body", "SWITCH REAL CODEX")

    dialog.input.setText("REAL CODEX")
    assert not dialog.ok_button.isEnabled()
    assert not dialog._matches_phrase("REAL CODEX")

    dialog.input.setText("  SWITCH   REAL   CODEX  ")
    assert dialog.ok_button.isEnabled()
    assert dialog._matches_phrase("  SWITCH   REAL   CODEX  ")


def test_account_row_buttons_call_account_actions() -> None:
    app = QApplication.instance() or QApplication([])
    account = AccountRecord(
        AccountMetadata(
            "account-1",
            "Test Account",
            AuthMode.CHAT_GPT,
            "openai",
            None,
            None,
            now_utc(),
            now_utc(),
            "runtime-key",
        ),
        "account-dir",
        "auth.json",
        "config.toml",
    )
    window = MainWindow.__new__(MainWindow)
    window.services = Mock(active_target=CodexHomeTarget.SANDBOX)
    window.switch_account = Mock()
    window.rename_account = Mock()
    window.delete_account = Mock()

    row = MainWindow._account_row(window, account, False)
    buttons = row.findChildren(QPushButton)

    for button in buttons:
        button.click()

    window.switch_account.assert_called_once_with(account)
    window.rename_account.assert_called_once_with(account)
    window.delete_account.assert_called_once_with(account)


def test_clear_removes_nested_layout_buttons() -> None:
    app = QApplication.instance() or QApplication([])
    body = QWidget()
    window = MainWindow.__new__(MainWindow)
    window.body_layout = QVBoxLayout(body)
    window.nav_buttons = {}
    window.current_view = "Accounts"
    window.accounts_quota_label = QLabel("old")

    actions = QHBoxLayout()
    actions.addWidget(QPushButton("Open Session Manager"))
    actions.addWidget(QPushButton("Repair Sandbox Threads"))
    window.body_layout.addLayout(actions)

    MainWindow._clear(window, "Accounts")

    assert [button.text() for button in body.findChildren(QPushButton)] == []


def test_clear_preserves_cached_sessions_page_across_navigation() -> None:
    app = QApplication.instance() or QApplication([])
    body = QWidget()
    window = MainWindow.__new__(MainWindow)
    window.body_layout = QVBoxLayout(body)
    window.nav_buttons = {}
    window.current_view = "Sessions"
    window.accounts_quota_label = None

    sessions_page = QWidget()
    sessions_page.setObjectName("SessionsPagePlaceholder")
    window._sessions_page_widget = sessions_page
    window.body_layout.addWidget(sessions_page)
    window.body_layout.addWidget(QPushButton("Other Widget"))

    MainWindow._clear(window, "Accounts")

    # Cached SessionsPage must survive _clear; the other widget must be gone.
    # The page stays parented to `body` (only removed from the layout) so that
    # it never transiently becomes a top-level window — that transient demotion
    # was what caused Windows to draw default OS chrome (titlebar + app icon)
    # for a few frames during page switches.
    assert sessions_page is window._sessions_page_widget
    assert sessions_page.parent() is body
    assert not sessions_page.isVisible()
    # And the page is no longer in body_layout: only the freshly-built heading
    # row remains there at this point.
    layout_widgets = [
        window.body_layout.itemAt(i).widget()
        for i in range(window.body_layout.count())
        if window.body_layout.itemAt(i).widget() is not None
    ]
    assert sessions_page not in layout_widgets
    remaining = [child for child in body.findChildren(QPushButton) if child.text() == "Other Widget"]
    assert remaining == []


def test_clear_detaches_preserved_heading_actions() -> None:
    app = QApplication.instance() or QApplication([])
    body = QWidget()
    window = MainWindow.__new__(MainWindow)
    window.body_layout = QVBoxLayout(body)
    window.nav_buttons = {}
    window.current_view = "Sessions"
    window.accounts_quota_label = None

    preserved = QPushButton("Target")
    preserved.setProperty("preserveOnClear", True)
    transient = QPushButton("Transient")
    heading = QHBoxLayout()
    heading.addWidget(preserved)
    heading.addWidget(transient)
    window.body_layout.addLayout(heading)

    MainWindow._clear(window, "Sessions")

    assert preserved.parent() is body
    assert not preserved.isVisible()
    assert "Transient" not in [button.text() for button in body.findChildren(QPushButton)]


def test_clear_hides_combo_before_removing_heading_action() -> None:
    app = QApplication.instance() or QApplication([])
    body = QWidget()
    window = MainWindow.__new__(MainWindow)
    window.body = body
    window.body_layout = QVBoxLayout(body)
    window.nav_buttons = {}
    window.current_view = "Accounts"
    window.accounts_quota_label = None

    combo = QComboBox(body)
    combo.addItems(["Sandbox", "Real"])
    combo.show()
    window.body_layout.addWidget(combo)

    MainWindow._clear(window, "Accounts")

    assert not combo.isVisible()
    assert combo.parent() is None
    assert QApplication.topLevelWidgets() == [] or all(
        not widget.isVisible() for widget in QApplication.topLevelWidgets()
    )


def test_action_key_button_reflects_locked_background_task() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    window._locked_action_keys = {"sync-sandbox-real"}

    button = MainWindow._button(window, "Sync", lambda: None, action_key="sync-sandbox-real")

    assert not button.isEnabled()
    assert button.property("actionKey") == "sync-sandbox-real"


def test_preview_text_accepts_worker_payload_dict() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)

    text = MainWindow._preview_text(
        window,
        {
            "operation": "Sync Sandbox Sessions to Real",
            "target": "Real",
            "target_home": "C:\\Users\\KIALA\\.codex",
            "affected_files": 3,
            "affected_bytes": 1024,
            "created_files": 1,
            "modified_files": 2,
            "deleted_files": 0,
            "sample_paths": ["C:\\Users\\KIALA\\.codex\\sessions\\one.jsonl"],
            "warnings": [],
            "summary": "Only sessions are changed.",
        },
    )

    assert "Target: Real" in text
    assert "Impact: 3 files, about 1.0 KB" in text


def test_sync_worker_arguments_reflect_selected_sections() -> None:
    assert MainWindow._sync_worker_arguments(True, False) == [
        "--sync-sessions",
        "true",
        "--sync-assets",
        "false",
    ]
    assert MainWindow._sync_worker_arguments(False, True) == [
        "--sync-sessions",
        "false",
        "--sync-assets",
        "true",
    ]


def test_task_worker_serialize_handles_enums_and_preview() -> None:
    from codex_quota_viewer.models import WritePreview

    data = serialize(WritePreview("Preview", CodexHomeTarget.REAL, "home", 1, 2, 3, 4))

    assert data["target"] == "Real"


def test_task_worker_emit_is_ascii_safe_for_gbk_stdout(monkeypatch) -> None:
    raw = io.BytesIO()
    gbk_stdout = io.TextIOWrapper(raw, encoding="gbk", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", gbk_stdout)

    emit("result", message="contains warning ⚠")
    gbk_stdout.flush()

    line = raw.getvalue().decode("gbk")
    assert "\\u26a0" in line
    assert json.loads(line)["message"] == "contains warning ⚠"


def test_button_uses_current_ui_language() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    window._language = UiLanguage.CHINESE
    window._locked_action_keys = set()

    button = MainWindow._button(window, "Switch", lambda: None)

    assert button.text() == "切换"


def _status_test_window() -> MainWindow:
    app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    window._language = UiLanguage.ENGLISH
    window.status_banner = StatusBanner()
    window.status_footer = QFrame()
    window.status_footer.setObjectName("StatusFooter")
    window.details_button = QPushButton("Details")
    window.details_button.setCheckable(True)
    window.details_panel = QTextEdit()
    window._status_auto_hide_timer = QTimer()
    window._status_auto_hide_timer.setSingleShot(True)
    window.tray_icon = None
    window._raw_status = ""
    return window


def test_status_popup_positions_as_overlay_inside_content() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    window.content = QWidget()
    window.content.resize(900, 500)
    window.status_footer = QFrame(window.content)
    layout = QVBoxLayout(window.status_footer)
    layout.addWidget(QLabel("Saved successfully."))

    MainWindow._position_status_popup(window)

    assert window.status_footer.parent() is window.content
    assert window.status_footer.width() == 876
    assert window.status_footer.height() == 52
    assert window.status_footer.x() == 12
    assert window.status_footer.y() == window.content.height() - window.status_footer.height() - 12


def test_status_popup_window_positions_against_content_global_origin() -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner.setGeometry(40, 50, 960, 600)
    content = QWidget(owner)
    content.setGeometry(20, 30, 900, 500)
    window = MainWindow.__new__(MainWindow)
    window.content = content
    window.status_footer = StatusPopupFrame(owner, as_window=True)
    QVBoxLayout(window.status_footer).addWidget(QLabel("Saved successfully."))
    owner.show()
    app.processEvents()

    MainWindow._position_status_popup(window)

    expected = content.mapToGlobal(QPoint(12, content.height() - window.status_footer.height() - 12))
    assert window.status_footer.isWindow()
    assert window.status_footer.width() == 876
    assert window.status_footer.height() == 52
    assert window.status_footer.pos() == expected
    owner.close()


def test_status_popup_frame_paints_without_backdrop_source() -> None:
    app = QApplication.instance() or QApplication([])
    container = QWidget()
    frame = StatusPopupFrame(container)
    frame.setGeometry(12, 10, 420, 52)
    frame.setProperty("severity", "success")
    container.show()
    app.processEvents()

    pixmap = frame.grab()

    assert not pixmap.isNull()
    assert pixmap.width() == 420
    assert pixmap.height() == 52
    container.close()


def test_status_auto_hide_timer_and_clear_status() -> None:
    app = QApplication.instance() or QApplication([])
    window = _status_test_window()

    MainWindow.set_status(window, "Saved successfully.", "success")

    assert window.status_banner.text() == "Saved successfully."
    assert window._status_auto_hide_timer.isActive()

    MainWindow.clear_status(window)

    assert window.status_banner.text() == ""
    assert not window.status_footer.isVisible()
    assert not window.details_panel.isVisible()
    assert not window._status_auto_hide_timer.isActive()


def test_details_pauses_and_resumes_status_auto_hide() -> None:
    app = QApplication.instance() or QApplication([])
    window = _status_test_window()

    MainWindow.set_status(window, "line one\nline two", "info")
    assert window._status_auto_hide_timer.isActive()

    window.details_button.setChecked(True)
    MainWindow._toggle_details(window, True)
    assert not window._status_auto_hide_timer.isActive()

    window.details_button.setChecked(False)
    MainWindow._toggle_details(window, False)
    assert window._status_auto_hide_timer.isActive()


def test_status_banner_double_click_requests_dismiss() -> None:
    app = QApplication.instance() or QApplication([])
    banner = StatusBanner()
    calls: list[bool] = []
    banner.dismiss_requested.connect(lambda: calls.append(True))

    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(4, 4),
        QPointF(4, 4),
        QPointF(4, 4),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(banner, event)

    assert calls == [True]


def test_seed_done_stays_on_settings_page() -> None:
    window = MainWindow.__new__(MainWindow)
    window.services = Mock()
    window.services.quota_buffer = Mock()
    window.current_view = "Settings"
    window._show_settings_preserving_scroll = Mock()
    window.show_accounts = Mock()
    window.set_status = Mock()

    MainWindow._seed_done(window, {"message": "Seeded sandbox."})

    window.services.quota_buffer.clear.assert_called_once_with(CodexHomeTarget.SANDBOX)
    window._show_settings_preserving_scroll.assert_called_once_with()
    window.show_accounts.assert_not_called()


def test_snapshot_target_switch_preserves_settings_scroll() -> None:
    class FakeScrollBar:
        def value(self) -> int:
            return 123

    class FakeScroll:
        def verticalScrollBar(self) -> FakeScrollBar:
            return FakeScrollBar()

    window = MainWindow.__new__(MainWindow)
    window.current_view = "Settings"
    window.scroll = FakeScroll()
    window._snapshot_target = CodexHomeTarget.SANDBOX
    window._show_settings_preserving_scroll = Mock()

    MainWindow._set_snapshot_target(window, CodexHomeTarget.REAL)

    assert window._snapshot_target == CodexHomeTarget.REAL
    window._show_settings_preserving_scroll.assert_called_once_with(scroll_value=123)
