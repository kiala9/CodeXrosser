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
from PySide6.QtGui import QMouseEvent, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget  # noqa: E402

from codex_quota_viewer.qt_app import ConfirmPhraseDialog, MainWindow, StatusBanner, StatusDetailsPopup, StatusPopupFrame, _apply_dark_palette  # noqa: E402
from codex_quota_viewer.models import AccountMetadata, AccountRecord, AuthMode, CodexHomeTarget, UiLanguage, now_utc  # noqa: E402
from codex_quota_viewer.task_worker import emit, serialize  # noqa: E402


def test_button_wrapper_ignores_qt_checked_argument() -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []

    button = MainWindow._button(None, "Switch", lambda: calls.append("clicked"))

    button.click()

    assert calls == ["clicked"]


def test_dark_palette_pins_native_view_colors() -> None:
    app = QApplication.instance() or QApplication([])

    _apply_dark_palette(app)

    palette = app.palette()
    assert palette.color(QPalette.Base).lightness() < 80
    assert palette.color(QPalette.Window).lightness() < 80
    assert palette.color(QPalette.Text).lightness() > 200
    assert palette.color(QPalette.WindowText).lightness() > 200
    assert palette.color(QPalette.HighlightedText).lightness() > 200


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
    window.status_footer = StatusPopupFrame()
    window.status_footer.setObjectName("StatusFooter")
    window.status_footer.show()  # set_status sets visible; layout-managed in app
    window.details_button = QPushButton("▾")
    window.details_button.setCheckable(True)
    window.details_button.hide()
    window.status_close_button = QPushButton("✕")
    window.status_details_popup = StatusDetailsPopup()
    window.details_panel = QTextEdit()
    window._status_details_popup_chrome_installed = True
    window._status_auto_hide_timer = QTimer()
    window._status_auto_hide_timer.setSingleShot(True)
    window.tray_icon = None
    window._raw_status = ""
    return window


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
    assert not window._status_auto_hide_timer.isActive()


def test_set_status_routes_full_text_to_tooltip() -> None:
    """Long messages surface their full cleaned text via the banner's
    tooltip, in addition to populating the details popover panel."""
    app = QApplication.instance() or QApplication([])
    window = _status_test_window()

    MainWindow.set_status(window, "line one\nline two", "info")

    # Tooltip carries the full cleaned text (multi-line), even when the
    # banner itself only renders a one-line summary.
    assert "line one" in window.status_banner.toolTip()
    assert "line two" in window.status_banner.toolTip()
    # Details panel inside the popover is also primed with the full text
    # so the popover renders correctly the moment it opens.
    assert "line one" in window.details_panel.toPlainText()
    assert "line two" in window.details_panel.toPlainText()


def test_set_status_shows_details_button_when_extra_present() -> None:
    """The ▾ details trigger is shown only when the cleaned text has
    more content than the one-line summary; for single-line messages
    it stays hidden so the pill doesn't carry meaningless affordances."""
    app = QApplication.instance() or QApplication([])
    window = _status_test_window()

    # Multi-line — summary will collapse to the first line, leaving extra.
    MainWindow.set_status(window, "headline\nmore body text", "info")
    assert window.details_button.isVisible()
    assert window.details_button.isEnabled()

    # Single-line — no details, button hidden.
    MainWindow.set_status(window, "Saved successfully.", "success")
    assert not window.details_button.isVisible()
    assert not window.details_button.isEnabled()


def test_details_popover_pauses_and_resumes_auto_hide() -> None:
    """Opening the details popover pauses the auto-hide timer so the
    user can read at their own pace; closing it resumes the timer."""
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


def test_clear_status_closes_details_popover() -> None:
    app = QApplication.instance() or QApplication([])
    window = _status_test_window()

    MainWindow.set_status(window, "line one\nline two", "info")
    window.details_button.setChecked(True)
    MainWindow._toggle_details(window, True)

    MainWindow.clear_status(window)

    assert not window.details_button.isChecked()
    assert not window.details_button.isVisible()
    assert not window.status_details_popup.isVisible()


def test_status_popup_frame_absorbs_double_click() -> None:
    """A double-click on the pill's empty padding must not bubble up to
    TitleBar's ``mouseDoubleClickEvent`` (which toggles maximize). The
    override on StatusPopupFrame should mark the event as accepted."""
    app = QApplication.instance() or QApplication([])
    container = QWidget()
    frame = StatusPopupFrame(container)
    frame.setGeometry(0, 0, 200, 40)

    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(20, 20),
        QPointF(20, 20),
        QPointF(20, 20),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(frame, event)

    # Accepted means Qt won't propagate it to the parent (TitleBar).
    assert event.isAccepted()
    container.close()


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
