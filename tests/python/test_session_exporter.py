from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from codex_quota_viewer.sessions.exporter import (  # noqa: E402
    DEFAULT_INLINE_IMAGE_MAX_BYTES,
    fetch_full_timeline,
    make_export_filename_stub,
    session_to_markdown,
)
from codex_quota_viewer.sessions.models import (  # noqa: E402
    Attachment,
    SessionRecord,
    SessionTimelineItem,
    SessionTimelinePage,
)


def _make_record(
    *,
    record_id: str = "01HABC000",
    cwd: str = "/home/alice/projects/codex-quota-viewer",
    started_at: str = "2026-05-05T10:34:12Z",
    user_excerpt: str = "Set up the export feature",
) -> SessionRecord:
    return SessionRecord(
        id=record_id,
        file_path=None,
        active_path=None,
        archive_path=None,
        snapshot_path=None,
        original_relative_path=None,
        cwd=cwd,
        started_at=started_at,
        originator="codex_cli_rs",
        source="cli",
        cli_version="0.42.0",
        model_provider="openai",
        size_bytes=1234,
        line_count=10,
        event_count=4,
        tool_call_count=1,
        user_prompt_excerpt=user_excerpt,
        latest_agent_message_excerpt="OK, done",
        status="active",
        created_at="2026-05-05T10:34:12Z",
        updated_at="2026-05-05T10:34:12Z",
        indexed_at="2026-05-05T10:34:12Z",
    )


def _user(text: str, *, ts: str = "2026-05-05T10:34:00Z", item_id: str = "u1",
          attachments: tuple[Attachment, ...] = ()) -> SessionTimelineItem:
    return SessionTimelineItem(
        id=item_id, type="message:user", timestamp=ts, text=text,
        attachments=attachments,
    )


def _assistant(text: str, *, ts: str = "2026-05-05T10:34:30Z", item_id: str = "a1",
               attachments: tuple[Attachment, ...] = ()) -> SessionTimelineItem:
    return SessionTimelineItem(
        id=item_id, type="message:assistant", timestamp=ts, text=text,
        attachments=attachments,
    )


def _tool(*, tool_name: str, input_text: str, output_text: str = "",
          ts: str = "2026-05-05T10:34:15Z", status: str = "completed",
          summary: str | None = None, item_id: str = "t1") -> SessionTimelineItem:
    return SessionTimelineItem(
        id=item_id,
        type="tool_call",
        timestamp=ts,
        tool_name=tool_name,
        summary=summary,
        input=input_text,
        output=output_text,
        status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# session_to_markdown
# ---------------------------------------------------------------------------


def test_session_to_markdown_user_assistant_basic() -> None:
    record = _make_record()
    timeline = [
        _user("Please add MD export"),
        _assistant("Sure, here is the plan: ..."),
    ]
    md = session_to_markdown(record, timeline, now_iso="2026-05-05T11:00:00")

    assert md.startswith("# Codex session — codex-quota-viewer (2026-05-05)\n")
    assert "> _Set up the export feature_" in md
    assert "**Session ID:** 01HABC000" in md
    assert "**Project:** /home/alice/projects/codex-quota-viewer" in md
    assert "**Exported at:** 2026-05-05T11:00:00" in md
    assert "## 👤 User" in md
    assert "_2026-05-05T10:34:00Z_" in md
    assert "Please add MD export" in md
    assert "## 🤖 Assistant" in md
    assert "Sure, here is the plan: ..." in md


def test_session_to_markdown_tool_call_with_json_input() -> None:
    record = _make_record()
    timeline = [
        _tool(
            tool_name="apply_patch",
            input_text='{"path": "foo.py", "diff": "+ ok"}',
            output_text="patched",
            summary="apply_patch foo.py",
        ),
    ]
    md = session_to_markdown(record, timeline)

    assert "## 🔧 Tool · apply_patch" in md
    assert "_2026-05-05T10:34:15Z — completed_" in md
    assert "apply_patch foo.py" in md
    assert "### Input" in md
    assert "```json\n{\"path\": \"foo.py\", \"diff\": \"+ ok\"}\n```" in md
    assert "### Output" in md
    assert "```\npatched\n```" in md


def test_session_to_markdown_tool_call_with_text_input() -> None:
    record = _make_record()
    timeline = [
        _tool(
            tool_name="shell",
            input_text="ls -la /tmp",
            output_text="total 0",
        ),
    ]
    md = session_to_markdown(record, timeline)

    # Plain text input should NOT get a json fence.
    assert "```json" not in md
    assert "```\nls -la /tmp\n```" in md
    assert "```\ntotal 0\n```" in md


def test_session_to_markdown_image_skip_mode() -> None:
    record = _make_record()
    image = Attachment(
        kind="image",
        mime="image/png",
        data_uri="data:image/png;base64,iVBORw0KGgo=",
        alt="my-diagram-alt",
    )
    timeline = [_user("user prose", attachments=(image,))]
    md = session_to_markdown(record, timeline, image_mode="skip")

    # No image markdown link should be emitted: neither the data URI nor
    # the alt text (which would only appear inside an image link).
    assert "data:image/png" not in md
    assert "my-diagram-alt" not in md
    assert "image elided" not in md  # skip mode is silent, not noisy


def test_session_to_markdown_image_inline_mode() -> None:
    record = _make_record()
    image = Attachment(
        kind="image",
        mime="image/png",
        data_uri="data:image/png;base64,iVBORw0KGgo=",
        alt="diagram",
    )
    timeline = [_user("see the diagram", attachments=(image,))]
    md = session_to_markdown(record, timeline, image_mode="inline")

    assert "![diagram](data:image/png;base64,iVBORw0KGgo=)" in md


def test_session_to_markdown_image_inline_oversize_elided() -> None:
    record = _make_record()
    big_payload = "data:image/png;base64," + ("A" * 10_000)
    image = Attachment(kind="image", mime="image/png", data_uri=big_payload, alt="huge")
    timeline = [_user("big one", attachments=(image,))]
    md = session_to_markdown(
        record, timeline, image_mode="inline", inline_image_max_bytes=1024
    )

    assert big_payload not in md
    assert "image elided" in md
    assert "huge" in md


def test_session_to_markdown_file_attachment_in_both_modes() -> None:
    record = _make_record()
    f = Attachment(kind="file", mime="text/plain", path="/tmp/notes.txt", name="notes.txt")
    timeline = [_user("here", attachments=(f,))]

    for mode in ("skip", "inline"):
        md = session_to_markdown(record, timeline, image_mode=mode)  # type: ignore[arg-type]
        assert "📎 [notes.txt](/tmp/notes.txt)" in md, mode


def test_session_to_markdown_empty_timeline() -> None:
    record = _make_record()
    md = session_to_markdown(record, [])

    assert md.startswith("# Codex session —")
    assert "## 👤 User" not in md
    assert md.rstrip().endswith("---")


def test_session_to_markdown_inline_path_only_image() -> None:
    record = _make_record()
    image = Attachment(kind="image", mime="image/png", path="/tmp/screenshot.png")
    timeline = [_user("look", attachments=(image,))]
    md = session_to_markdown(record, timeline, image_mode="inline")

    assert "/tmp/screenshot.png" in md
    assert "_(local file)_" in md


def test_session_to_markdown_default_excludes_no_excerpt_quote() -> None:
    record = _make_record(user_excerpt="")
    md = session_to_markdown(record, [])

    # Excerpt block is skipped when empty — body should jump straight
    # from the title to the metadata block.
    assert "> _" not in md


# ---------------------------------------------------------------------------
# make_export_filename_stub
# ---------------------------------------------------------------------------


def test_make_export_filename_stub_basic_posix() -> None:
    record = _make_record(cwd="/home/alice/projects/My-App")
    stub = make_export_filename_stub(record)
    assert stub == "codex-session-my-app-20260505103412"


def test_make_export_filename_stub_windows_path() -> None:
    record = _make_record(cwd=r"C:\Users\alice\source\Codex Quota Viewer")
    stub = make_export_filename_stub(record)
    assert stub.startswith("codex-session-codex-quota-viewer-")


def test_make_export_filename_stub_unicode_cwd() -> None:
    record = _make_record(cwd=r"C:\Users\漢字\proj")
    stub = make_export_filename_stub(record)
    # Non-ascii chars get collapsed to dashes — result is at least
    # safe / printable, even if it ends up empty for the basename.
    assert stub.startswith("codex-session-")
    assert all(c.isalnum() or c in "._-" for c in stub)


def test_make_export_filename_stub_empty_cwd_falls_back_to_id() -> None:
    record = _make_record(cwd="", record_id="01HSESSIONIDXYZ")
    stub = make_export_filename_stub(record)
    assert stub == "codex-session-01HSESSIONI"[:24] or stub.startswith(
        "codex-session-01HSESSION"
    )


# ---------------------------------------------------------------------------
# fetch_full_timeline
# ---------------------------------------------------------------------------


class _FakeManager:
    def __init__(self, pages: list[SessionTimelinePage]) -> None:
        self._pages = pages
        self.calls: list[tuple[int | None, int | None]] = []

    def get_session_timeline_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> SessionTimelinePage:
        assert session_id == "session-x"
        self.calls.append((offset, limit))
        index = 0
        for page in self._pages:
            if (offset or 0) == sum(len(p.items) for p in self._pages[:index]):
                return page
            index += 1
        return SessionTimelinePage(items=[], total=0, next_offset=None)


def _make_items(count: int, *, prefix: str) -> list[SessionTimelineItem]:
    return [
        SessionTimelineItem(
            id=f"{prefix}-{i}",
            type="message:user",
            timestamp=f"2026-05-05T10:00:{i:02d}Z",
            text=f"msg {prefix}-{i}",
        )
        for i in range(count)
    ]


def test_fetch_full_timeline_paginates() -> None:
    page1_items = _make_items(3, prefix="p1")
    page2_items = _make_items(3, prefix="p2")
    page3_items = _make_items(2, prefix="p3")
    pages = [
        SessionTimelinePage(items=page1_items, total=8, next_offset=3),
        SessionTimelinePage(items=page2_items, total=8, next_offset=6),
        SessionTimelinePage(items=page3_items, total=8, next_offset=None),
    ]
    manager = _FakeManager(pages)

    items = fetch_full_timeline(manager, "session-x", page_size=3)

    assert len(items) == 8
    assert [it.id for it in items[:4]] == ["p1-0", "p1-1", "p1-2", "p2-0"]
    assert manager.calls == [(0, 3), (3, 3), (6, 3)]


def test_fetch_full_timeline_single_page() -> None:
    pages = [
        SessionTimelinePage(items=_make_items(2, prefix="solo"), total=2, next_offset=None),
    ]
    items = fetch_full_timeline(_FakeManager(pages), "session-x")
    assert len(items) == 2


def test_default_inline_budget_is_four_megabytes() -> None:
    # Sanity check so a future tweak gets caught.
    assert DEFAULT_INLINE_IMAGE_MAX_BYTES == 4 * 1024 * 1024
