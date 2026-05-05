from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from codex_quota_viewer.core import (  # noqa: E402
    VaultQuotaCache,
    parse_codex_id_token_plan,
)
from codex_quota_viewer.models import (  # noqa: E402
    CodexAccount,
    CodexSnapshot,
    RateLimitSnapshot,
    RateLimitWindow,
)


def _make_jwt(payload: dict) -> str:
    """Build a syntactically valid (but unsigned) JWT for fixture use.

    Real Codex id_tokens are RS256-signed; we don't verify the signature
    here, only decode the claims segment, so an empty signature is fine.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{header}.{payload_b64}."


def _auth_json_with_plan(plan: str | None, account_id: str | None = "acct-test") -> bytes:
    auth_block = {}
    if plan is not None:
        auth_block["chatgpt_plan_type"] = plan
    if account_id is not None:
        auth_block["chatgpt_account_id"] = account_id
    claims = {"https://api.openai.com/auth": auth_block, "email": "user@example.com"}
    return json.dumps({"tokens": {"id_token": _make_jwt(claims)}}).encode("utf-8")


# --- parse_codex_id_token_plan -------------------------------------------------


def test_parse_id_token_plan_returns_free_for_free_account() -> None:
    plan, account_id = parse_codex_id_token_plan(_auth_json_with_plan("free", "acct-1"))
    assert plan == "free"
    assert account_id == "acct-1"


def test_parse_id_token_plan_returns_pro_for_pro_account() -> None:
    plan, _ = parse_codex_id_token_plan(_auth_json_with_plan("pro"))
    assert plan == "pro"


def test_parse_id_token_plan_lowercases_uppercase_value() -> None:
    plan, _ = parse_codex_id_token_plan(_auth_json_with_plan("Plus"))
    assert plan == "plus"


def test_parse_id_token_plan_returns_none_for_malformed_jwt() -> None:
    bad = b'{"tokens": {"id_token": "not-a-jwt"}}'
    assert parse_codex_id_token_plan(bad) == (None, None)


def test_parse_id_token_plan_returns_none_when_id_token_missing() -> None:
    assert parse_codex_id_token_plan(b'{"tokens": {}}') == (None, None)


def test_parse_id_token_plan_returns_none_for_invalid_json() -> None:
    assert parse_codex_id_token_plan(b"not json at all") == (None, None)


def test_parse_id_token_plan_returns_none_when_auth_namespace_missing() -> None:
    payload = {"sub": "user", "email": "u@example.com"}
    auth_data = json.dumps({"tokens": {"id_token": _make_jwt(payload)}}).encode("utf-8")
    assert parse_codex_id_token_plan(auth_data) == (None, None)


# --- VaultQuotaCache -----------------------------------------------------------


def _free_snapshot() -> CodexSnapshot:
    return CodexSnapshot(
        CodexAccount("ChatGPT", "free@example.com", "free"),
        RateLimitSnapshot(
            "limit-free",
            "Weekly",
            None,
            RateLimitWindow(96.0, 10080, 1746789000),
            "free",
        ),
        datetime(2026, 5, 9, 11, 30, tzinfo=timezone.utc),
    )


def _pro_snapshot() -> CodexSnapshot:
    return CodexSnapshot(
        CodexAccount("ChatGPT", "pro@example.com", "pro"),
        RateLimitSnapshot(
            "limit-pro",
            "Standard",
            RateLimitWindow(3.0, 300, 1746790000),
            RateLimitWindow(5.0, 10080, 1746889000),
            "pro",
        ),
        datetime(2026, 5, 9, 11, 31, tzinfo=timezone.utc),
    )


def test_vault_quota_cache_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    cache = VaultQuotaCache(tmp_path / "quota-cache.json")
    assert cache.load() == {}


def test_vault_quota_cache_load_returns_empty_for_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "quota-cache.json"
    path.write_text("not json", encoding="utf-8")
    cache = VaultQuotaCache(path)
    assert cache.load() == {}


def test_vault_quota_cache_round_trip_preserves_free_plan(tmp_path: Path) -> None:
    cache = VaultQuotaCache(tmp_path / "quota-cache.json")
    snapshot = _free_snapshot()
    cache.upsert("acct-free", snapshot)
    loaded = VaultQuotaCache(tmp_path / "quota-cache.json").get("acct-free")
    assert loaded is not None
    assert loaded.account.plan_type == "free"
    assert loaded.rate_limits is not None
    assert loaded.rate_limits.primary is None
    assert loaded.rate_limits.secondary is not None
    assert loaded.rate_limits.secondary.used_percent == pytest.approx(96.0)
    assert loaded.rate_limits.secondary.window_duration_mins == 10080


def test_vault_quota_cache_round_trip_preserves_two_windows(tmp_path: Path) -> None:
    cache = VaultQuotaCache(tmp_path / "quota-cache.json")
    cache.upsert("acct-pro", _pro_snapshot())
    loaded = VaultQuotaCache(tmp_path / "quota-cache.json").get("acct-pro")
    assert loaded is not None and loaded.rate_limits is not None
    assert loaded.rate_limits.primary is not None
    assert loaded.rate_limits.primary.window_duration_mins == 300
    assert loaded.rate_limits.secondary is not None
    assert loaded.rate_limits.secondary.window_duration_mins == 10080


def test_vault_quota_cache_delete_removes_entry(tmp_path: Path) -> None:
    cache = VaultQuotaCache(tmp_path / "quota-cache.json")
    cache.upsert("acct-1", _free_snapshot())
    cache.upsert("acct-2", _pro_snapshot())
    cache.delete("acct-1")
    remaining = cache.load()
    assert "acct-1" not in remaining
    assert "acct-2" in remaining


def test_vault_quota_cache_upsert_overwrites_same_key(tmp_path: Path) -> None:
    cache = VaultQuotaCache(tmp_path / "quota-cache.json")
    cache.upsert("acct-1", _free_snapshot())
    cache.upsert("acct-1", _pro_snapshot())
    loaded = cache.get("acct-1")
    assert loaded is not None and loaded.account.plan_type == "pro"


# --- display_windows / Free plan handling --------------------------------------


def test_display_windows_returns_one_entry_for_free_snapshot() -> None:
    windows = _free_snapshot().display_windows()
    assert len(windows) == 1
    assert windows[0].window.window_duration_mins == 10080


def test_display_windows_returns_two_entries_for_pro_snapshot() -> None:
    windows = _pro_snapshot().display_windows()
    assert len(windows) == 2
    # Sorted by window duration: 5h (300 min) first, weekly (10080 min) second.
    assert windows[0].window.window_duration_mins == 300
    assert windows[1].window.window_duration_mins == 10080


def test_rate_limit_snapshot_is_free_only_property() -> None:
    free = _free_snapshot().rate_limits
    pro = _pro_snapshot().rate_limits
    assert free is not None and pro is not None
    assert free.is_free_only is True
    assert pro.is_free_only is False


# --- 0% exhaustion synthesis (the bug fix's core logic) ------------------------
#
# Mirrors what `MainWindow._effective_windows` does when the live snapshot
# returns no windows but a cached snapshot exists. Implemented as plain
# helper logic so we don't have to spin up a Qt application in tests.


def _synthesize_exhausted_windows(cached: CodexSnapshot):
    if cached.rate_limits is None:
        return []
    rl = cached.rate_limits
    primary = (
        RateLimitWindow(100.0, rl.primary.window_duration_mins, rl.primary.resets_at)
        if rl.primary
        else None
    )
    secondary = (
        RateLimitWindow(100.0, rl.secondary.window_duration_mins, rl.secondary.resets_at)
        if rl.secondary
        else None
    )
    snap = CodexSnapshot(
        cached.account,
        RateLimitSnapshot(rl.limit_id, rl.limit_name, primary, secondary, rl.plan_type),
        cached.fetched_at,
    )
    return snap.display_windows()


def test_synthesize_exhausted_windows_for_free_yields_one_zero_remaining_window() -> None:
    windows = _synthesize_exhausted_windows(_free_snapshot())
    assert len(windows) == 1
    assert windows[0].window.remaining_percent == pytest.approx(0.0)


def test_synthesize_exhausted_windows_for_pro_yields_two_zero_remaining_windows() -> None:
    windows = _synthesize_exhausted_windows(_pro_snapshot())
    assert len(windows) == 2
    assert all(w.window.remaining_percent == pytest.approx(0.0) for w in windows)
