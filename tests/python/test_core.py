from __future__ import annotations

import base64
import json
import os
import queue
import sqlite3
import subprocess
import sys
import time
import types
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

import codex_quota_viewer.core as core_module  # noqa: E402
from codex_quota_viewer.core import (  # noqa: E402
    AccountVault,
    ApiAccountService,
    AppPaths,
    AppSettingsStore,
    AuditLog,
    BackupManager,
    ChatGptLoginService,
    CodexCommand,
    CodexDesktopAppLauncher,
    CodexRpcClient,
    QuotaSnapshotBuffer,
    ProviderVisibilitySynchronizer,
    RuntimeConfig,
    SafeSwitchService,
    SandboxRealSessionSyncService,
    SandboxSeeder,
    SessionManagerLauncher,
    SessionMetaSynchronizer,
    SnapshotManager,
    WritePreviewBuilder,
    WindowsCodexDesktopController,
)
from codex_quota_viewer.models import (  # noqa: E402
    AccountMetadata,
    AccountRuntimeMaterial,
    AppSettings,
    AuthMode,
    CodexAccount,
    CodexDesktopSwitchPreparation,
    CodexHomeTarget,
    CodexSnapshot,
    OfficialRepairSummary,
    SnapshotKind,
    UiLanguage,
    now_utc,
)


class _NoopRepairClient:
    def rescan_and_repair(self, target):
        return OfficialRepairSummary()


def _write_provider_state_db(path: Path, provider: str, cwd: str = r"\\?\C:\work") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table threads (
              id text primary key,
              model_provider text,
              has_user_event integer,
              cwd text
            )
            """
        )
        conn.execute(
            "insert into threads (id, model_provider, has_user_event, cwd) values ('thread-1', ?, 0, ?)",
            (provider, cwd),
        )
        conn.commit()


def _write_session_file(path: Path, thread_id: str, provider: str, mtime: int, marker: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "model_provider": provider,
                    "cwd": r"\\?\C:\work",
                    "updated_at_ms": mtime,
                },
            },
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": marker or f"hello {thread_id}"},
            },
            separators=(",", ":"),
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_app_settings_store_persists_language(tmp_path: Path) -> None:
    store = AppSettingsStore(tmp_path / "settings.json")

    assert store.load().language == UiLanguage.ENGLISH

    store.save(AppSettings(CodexHomeTarget.REAL, UiLanguage.CHINESE))
    loaded = store.load()

    assert loaded.active_codex_home_target == CodexHomeTarget.REAL
    assert loaded.language == UiLanguage.CHINESE


def test_paths_resolve_sandbox_and_real(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"))

    assert paths.auth_path(CodexHomeTarget.SANDBOX) == tmp_path / "data" / "SandboxCodexHome" / "auth.json"
    assert paths.config_path(CodexHomeTarget.REAL) == tmp_path / ".codex" / "config.toml"
    assert paths.session_roots(CodexHomeTarget.REAL)[0] == tmp_path / ".codex" / "sessions"
    assert paths.backups_root_for(CodexHomeTarget.REAL).name == "Real"


def test_paths_can_put_heavy_storage_outside_appdata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    storage = tmp_path / "heavy"
    paths = AppPaths.for_current_user(str(data), str(tmp_path / ".codex"), str(storage))

    assert paths.settings_path == data / "settings.json"
    assert paths.accounts_root == data / "Accounts"
    assert paths.storage_root == storage
    assert paths.sandbox_codex_home == storage / "SandboxCodexHome"
    assert paths.backups_root == storage / "SwitchBackups"
    assert paths.session_manager_home == storage / "SessionManager"


def test_normalizes_openai_compatible_base_url() -> None:
    assert ApiAccountService.normalize_base_url("https://example.test", True) == "https://example.test/v1"
    assert ApiAccountService.normalize_base_url("https://example.test/v1/", True) == "https://example.test/v1"


def test_api_account_falls_back_on_non_auth_probe_error(monkeypatch) -> None:
    service = ApiAccountService()

    def fail_probe(*_):
        raise core_module.error.HTTPError("https://api.example.test/v1/models", 500, "server error", None, None)

    monkeypatch.setattr(service, "probe_models", fail_probe)

    draft = service.configure("sk-test", "https://api.example.test", model="gpt-test")

    assert draft.used_fallback
    assert draft.normalized_base_url == "https://api.example.test/v1"
    assert draft.model == "gpt-test"


def test_chatgpt_login_streams_device_auth_progress(tmp_path: Path, monkeypatch) -> None:
    sentinel = tmp_path / "progress-seen"
    monkeypatch.setenv("LOGIN_PROGRESS_SENTINEL", str(sentinel))
    script = "\n".join(
        [
            "import os, pathlib, sys, time",
            "sentinel = pathlib.Path(os.environ['LOGIN_PROGRESS_SENTINEL'])",
            "print('device-code: ABCD', flush=True)",
            "deadline = time.monotonic() + 3",
            "while not sentinel.exists() and time.monotonic() < deadline:",
            "    time.sleep(0.05)",
            "home = pathlib.Path(os.environ['CODEX_HOME'])",
            "home.mkdir(parents=True, exist_ok=True)",
            "(home / 'auth.json').write_text('{}')",
            "sys.exit(0 if sentinel.exists() else 2)",
        ]
    )
    service = ChatGptLoginService(lambda: CodexCommand(sys.executable, ("-c", script, "--"), "test-codex"))
    temp_home = tmp_path / "home"
    temp_codex_home = temp_home / ".codex"
    temp_codex_home.mkdir(parents=True)

    def progress(message: str) -> None:
        if "device-code: ABCD" in message:
            sentinel.write_text("seen")

    return_code, diagnostics = service._run_login(temp_home, temp_codex_home, True, 5, progress)

    assert return_code == 0, diagnostics
    assert sentinel.exists()
    assert (temp_codex_home / "auth.json").exists()


def test_desktop_detection_matches_window_title_tuples(monkeypatch) -> None:
    class FakeProcess:
        pid = 1234
        info = {"name": "OpenAI.exe", "exe": "C:\\Program Files\\OpenAI\\OpenAI.exe", "cmdline": []}

    process = FakeProcess()
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(process_iter=lambda _: [process]))
    controller = WindowsCodexDesktopController()
    monkeypatch.setattr(controller, "_window_titles_by_pid", lambda: {process.pid: [(99, "Codex Desktop")]})

    candidates = controller._find_candidates()

    assert candidates == [(process, [99])]


def test_desktop_detection_ignores_codex_electron_children(monkeypatch) -> None:
    class FakeProcess:
        pid = 1234
        info = {
            "name": "Codex.exe",
            "exe": "C:\\Program Files\\WindowsApps\\OpenAI.Codex_1.0.0.0_x64__abc\\app\\Codex.exe",
            "cmdline": ["Codex.exe", "--type=gpu-process"],
        }

    process = FakeProcess()
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(process_iter=lambda _: [process]))
    controller = WindowsCodexDesktopController()
    monkeypatch.setattr(controller, "_window_titles_by_pid", lambda: {process.pid: []})

    assert controller._find_candidates() == []


def test_desktop_detection_keeps_desktop_app_server_but_ignores_viewer_probe(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self, pid: int, exe: str, cmdline: list[str]):
            self.pid = pid
            self.info = {"name": Path(exe).name, "exe": exe, "cmdline": cmdline}

    desktop_server = FakeProcess(
        1234,
        "C:\\Program Files\\WindowsApps\\OpenAI.Codex_1.0.0.0_x64__abc\\app\\resources\\codex.exe",
        ["codex.exe", "app-server", "--analytics-default-enabled"],
    )
    viewer_probe = FakeProcess(
        5678,
        "E:\\Program Files\\nodejs\\node_global\\node_modules\\@openai\\codex\\codex.exe",
        ["codex.exe", "-s", "read-only", "-a", "untrusted", "app-server"],
    )
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(process_iter=lambda _: [desktop_server, viewer_probe]))
    controller = WindowsCodexDesktopController()
    monkeypatch.setattr(controller, "_window_titles_by_pid", lambda: {})

    candidates = controller._find_candidates()

    assert candidates == [(desktop_server, [])]
    assert "background app-server" in controller._blocking_process_summary(candidates)


def test_codex_rpc_kills_wrapper_process_tree() -> None:
    psutil = pytest.importorskip("psutil")
    script = "\n".join(
        [
            "import subprocess, sys",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
            "print(child.pid, flush=True)",
            "child.wait()",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = int(process.stdout.readline().strip())
    def child_is_gone() -> bool:
        try:
            child = psutil.Process(child_pid)
            return (not child.is_running()) or child.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    try:
        assert psutil.pid_exists(child_pid)

        CodexRpcClient._try_kill(process)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child_is_gone():
                break
            time.sleep(0.05)
        assert process.poll() is not None
        assert child_is_gone()
    finally:
        CodexRpcClient._try_kill(process)
        if psutil.pid_exists(child_pid):
            try:
                psutil.Process(child_pid).kill()
            except Exception:
                pass


def test_runtime_config_merge_preserves_unrelated_settings() -> None:
    current = b"""
approval_policy = "on-request"
model_provider = "old-provider"
model = "old-model"

[mcp_servers.local]
command = "node"

[model_providers.openai-compatible]
base_url = "https://old.example.test/v1"
wire_api = "chat"
"""
    target = RuntimeConfig.synthesize_openai_compatible("https://api.example.test/v1", "gpt-5.4").encode()

    merged = RuntimeConfig.merge_for_switch(current, target, AuthMode.API_KEY)

    assert 'approval_policy = "on-request"' in merged
    assert "[mcp_servers.local]" in merged
    assert 'command = "node"' in merged
    assert 'model_provider = "openai-compatible"' in merged
    assert 'model = "gpt-5.4"' in merged
    assert 'base_url = "https://api.example.test/v1"' in merged
    assert 'wire_api = "responses"' in merged
    assert 'wire_api = "chat"' not in merged
    assert "old-provider" not in merged


def test_runtime_config_merge_keeps_chatgpt_provider_at_root() -> None:
    current = b"""
service_tier = "fast"
model_provider = "old-provider"

[windows]
sandbox = "unelevated"

[tui.model_availability_nux]
"gpt-5.5" = 1
"""

    merged = RuntimeConfig.merge_for_switch(current, None, AuthMode.CHAT_GPT)

    before_windows = merged.split("[windows]", 1)[0]
    after_tui = merged.split("[tui.model_availability_nux]", 1)[1]
    assert 'model_provider = "openai"' in before_windows
    assert 'model_provider = "openai"' not in after_tui
    assert RuntimeConfig.parse(merged.encode()).provider_id == "openai"


def test_runtime_config_parse_ignores_nested_provider_values() -> None:
    summary = RuntimeConfig.parse(b'[tui.model_availability_nux]\nmodel_provider = "nested"\nmodel = "nested-model"\n')

    assert summary.provider_id is None
    assert summary.model is None


def test_runtime_config_parse_scopes_base_url_to_active_provider() -> None:
    summary = RuntimeConfig.parse(
        b"""
model_provider = "openai"
model = "gpt-5.4"

[model_providers.openai-compatible]
name = "OpenAI Compatible"
base_url = "https://api.example.test/v1"
wire_api = "responses"
"""
    )

    assert summary.provider_id == "openai"
    assert summary.base_url is None
    assert summary.model == "gpt-5.4"


def test_runtime_config_parse_reads_api_provider_base_url() -> None:
    summary = RuntimeConfig.parse(
        b"""
model_provider = "openai-compatible"
model = "gpt-5.4"

[model_providers.openai-compatible]
name = "OpenAI Compatible"
base_url = "https://api.example.test/v1"
wire_api = "responses"
"""
    )

    assert summary.provider_id == "openai-compatible"
    assert summary.base_url == "https://api.example.test/v1"
    assert summary.model == "gpt-5.4"


def test_api_runtime_uses_current_responses_wire_api() -> None:
    runtime = AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4")

    config = (runtime.config_data or b"").decode("utf-8")

    assert 'wire_api = "responses"' in config
    assert 'wire_api = "chat"' not in config


def test_session_manager_http_error_surfaces_server_body() -> None:
    ex = core_module.error.HTTPError(
        "http://127.0.0.1:4319/api/codex/repair",
        500,
        "Internal Server Error",
        {},
        BytesIO(b'{"code":"internal_server_error","error":"rename failed"}'),
    )

    assert SessionManagerLauncher._http_error_message(ex) == "Session Manager HTTP 500: rename failed (internal_server_error)"


def test_account_vault_persists_sorts_and_suggests_email(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    api_runtime = AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4")
    vault.upsert("Zed API", api_runtime, AuthMode.API_KEY, "openai-compatible", "https://api.example.test/v1", "gpt-5.4")
    runtime = AccountRuntimeMaterial(b'{"account":{"email":"person@example.com"}}', None)
    vault.upsert(AccountVault.suggested_display_name(runtime, AuthMode.CHAT_GPT), runtime, AuthMode.CHAT_GPT, "openai")

    accounts = vault.load()

    assert [item.metadata.display_name for item in accounts] == ["person@example.com", "Zed API"]
    assert json.loads((tmp_path / "Accounts" / "accounts.json").read_text()) [0]["authMode"] == 0


def test_account_vault_stores_oauth_config_as_common_not_identity(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    auth = b'{"account":{"email":"person@example.com"}}'
    first = vault.upsert("Person", AccountRuntimeMaterial(auth, b'model = "gpt-5.5"\n'), AuthMode.CHAT_GPT, "openai")
    second = vault.upsert("Person Again", AccountRuntimeMaterial(auth, b'model = "gpt-5.4"\n'), AuthMode.CHAT_GPT, "openai")

    accounts = vault.load()

    assert first.metadata.id == second.metadata.id
    assert len(accounts) == 1
    assert not (Path(second.directory_path) / "config.toml").exists()
    assert not (Path(second.directory_path) / "preference.config.toml").exists()
    assert vault.oauth_common_config_path.exists()
    assert RuntimeConfig.parse(vault.oauth_common_config_path.read_bytes()).provider_id == "openai"


def test_account_vault_dedupes_oauth_by_email_when_token_changes(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    first_auth = b'{"account":{"email":"person@example.com"},"token":"old"}'
    second_auth = b'{"account":{"email":"person@example.com"},"token":"new"}'
    first = vault.upsert("Person", AccountRuntimeMaterial(first_auth, None), AuthMode.CHAT_GPT, "openai")
    second = vault.upsert("Sandbox Current", AccountRuntimeMaterial(second_auth, None), AuthMode.CHAT_GPT, "openai")

    accounts = vault.load()

    assert first.metadata.id == second.metadata.id
    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "Person"
    assert Path(accounts[0].auth_path).read_bytes() == second_auth


def test_capture_sandbox_current_reuses_existing_oauth_email_account(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    vault = AccountVault(paths.accounts_root)
    existing_auth = b'{"account":{"email":"seed@example.com"},"token":"old"}'
    sandbox_auth = b'{"account":{"email":"seed@example.com"},"token":"seeded-from-real"}'
    existing = vault.upsert("seed@example.com", AccountRuntimeMaterial(existing_auth, None), AuthMode.CHAT_GPT, "openai")
    paths.sandbox_codex_home.mkdir(parents=True, exist_ok=True)
    paths.auth_path(CodexHomeTarget.SANDBOX).write_bytes(sandbox_auth)
    paths.config_path(CodexHomeTarget.SANDBOX).write_text('model_provider = "openai"\n', encoding="utf-8")

    captured = vault.capture_sandbox_current(paths, "Sandbox Current")
    accounts = vault.load()

    assert captured.metadata.id == existing.metadata.id
    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "seed@example.com"
    assert Path(accounts[0].auth_path).read_bytes() == sandbox_auth


def test_capture_sandbox_current_does_not_store_api_base_url_for_oauth(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    vault = AccountVault(paths.accounts_root)
    sandbox_auth = b'{"account":{"email":"seed@example.com"},"token":"seeded-from-real"}'
    paths.sandbox_codex_home.mkdir(parents=True, exist_ok=True)
    paths.auth_path(CodexHomeTarget.SANDBOX).write_bytes(sandbox_auth)
    paths.config_path(CodexHomeTarget.SANDBOX).write_text(
        """
model_provider = "openai"
model = "gpt-5.4"

[model_providers.openai-compatible]
name = "OpenAI Compatible"
base_url = "https://api.example.test/v1"
wire_api = "responses"
""",
        encoding="utf-8",
    )

    captured = vault.capture_sandbox_current(paths, "Sandbox Current")

    assert captured.metadata.provider_id == "openai"
    assert captured.metadata.base_url is None
    assert captured.metadata.model == "gpt-5.4"


def test_account_vault_dedupes_api_by_key_and_base_url_when_model_changes(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    first = vault.upsert(
        "Custom API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )
    second = vault.upsert(
        "Sandbox Current",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1/", "gpt-5.5"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1/",
        "gpt-5.5",
    )

    accounts = vault.load()

    assert first.metadata.id == second.metadata.id
    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "Custom API"
    assert accounts[0].metadata.model == "gpt-5.5"


def test_capture_sandbox_current_reuses_existing_api_account(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    vault = AccountVault(paths.accounts_root)
    existing = vault.upsert(
        "Custom API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )
    runtime = AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.5")
    paths.sandbox_codex_home.mkdir(parents=True, exist_ok=True)
    paths.auth_path(CodexHomeTarget.SANDBOX).write_bytes(runtime.auth_data)
    paths.config_path(CodexHomeTarget.SANDBOX).write_bytes(runtime.config_data or b"")

    captured = vault.capture_sandbox_current(paths, "Sandbox Current")
    accounts = vault.load()

    assert captured.metadata.id == existing.metadata.id
    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "Custom API"
    assert accounts[0].metadata.model == "gpt-5.5"


def test_account_vault_collapses_existing_duplicate_oauth_metadata(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    auth_one = b'{"account":{"email":"dupe@example.com"},"token":"one"}'
    auth_two = b'{"account":{"email":"dupe@example.com"},"token":"two"}'
    id_one = AccountVault.account_id_for_mode(AccountRuntimeMaterial(auth_one, None), AuthMode.CHAT_GPT)
    id_two = AccountVault.account_id_for_mode(AccountRuntimeMaterial(auth_two, None), AuthMode.CHAT_GPT)
    for account_id, auth in [(id_one, auth_one), (id_two, auth_two)]:
        directory = tmp_path / "Accounts" / account_id
        directory.mkdir(parents=True)
        (directory / "auth.json").write_bytes(auth)
    metadata = [
        AccountMetadata(id_one, "dupe@example.com", AuthMode.CHAT_GPT, "openai", None, None, now_utc(), None, AccountVault.runtime_key_for_mode(AccountRuntimeMaterial(auth_one, None), AuthMode.CHAT_GPT)),
        AccountMetadata(id_two, "Sandbox Current", AuthMode.CHAT_GPT, "openai", None, None, now_utc(), None, AccountVault.runtime_key_for_mode(AccountRuntimeMaterial(auth_two, None), AuthMode.CHAT_GPT)),
    ]
    (tmp_path / "Accounts" / "accounts.json").write_text(json.dumps([item.to_json() for item in metadata]), encoding="utf-8")

    vault.upsert("Sandbox Current", AccountRuntimeMaterial(b'{"account":{"email":"dupe@example.com"},"token":"three"}', None), AuthMode.CHAT_GPT, "openai")
    accounts = vault.load()

    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "dupe@example.com"


def test_account_vault_collapses_existing_duplicate_api_metadata(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    first_runtime = AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4")
    second_runtime = AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1/", "gpt-5.5")
    id_one = AccountVault.account_id_for_mode(first_runtime, AuthMode.API_KEY)
    id_two = AccountVault.account_id_for_mode(second_runtime, AuthMode.API_KEY)
    for account_id, runtime in [(id_one, first_runtime), (id_two, second_runtime)]:
        directory = tmp_path / "Accounts" / account_id
        directory.mkdir(parents=True)
        (directory / "auth.json").write_bytes(runtime.auth_data)
        (directory / "config.toml").write_bytes(runtime.config_data or b"")
    metadata = [
        AccountMetadata(id_one, "Custom API", AuthMode.API_KEY, "openai-compatible", "https://api.example.test/v1", "gpt-5.4", now_utc(), None, AccountVault.runtime_key_for_mode(first_runtime, AuthMode.API_KEY)),
        AccountMetadata(id_two, "Sandbox Current", AuthMode.API_KEY, "openai-compatible", "https://api.example.test/v1/", "gpt-5.5", now_utc(), None, AccountVault.runtime_key_for_mode(second_runtime, AuthMode.API_KEY)),
    ]
    (tmp_path / "Accounts" / "accounts.json").write_text(json.dumps([item.to_json() for item in metadata]), encoding="utf-8")

    vault.upsert(
        "Sandbox Current",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.6"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.6",
    )
    accounts = vault.load()

    assert len(accounts) == 1
    assert accounts[0].metadata.display_name == "Custom API"
    assert accounts[0].metadata.model == "gpt-5.6"


def test_account_vault_oauth_preference_overrides_common(tmp_path: Path) -> None:
    vault = AccountVault(tmp_path / "Accounts")
    account = vault.upsert("Person", AccountRuntimeMaterial(b'{"account":{"email":"person@example.com"}}', None), AuthMode.CHAT_GPT, "openai")
    vault.write_oauth_common_config(b'model_provider = "openai"\nmodel = "common"\n', overwrite=True)
    preference = vault.oauth_preference_config_path(account.metadata.id)
    preference.write_text('model_provider = "openai"\nmodel = "private"\n', encoding="utf-8")

    runtime = vault.read_runtime(vault.find(account.metadata.id) or account)

    assert b'model = "private"' in (runtime.config_data or b"")
    assert vault.oauth_config_source_label(account) == "account preference"


def test_account_vault_suggests_email_from_jwt_payload() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(b'{"email":"jwt-person@example.com"}').decode().rstrip("=")
    runtime = AccountRuntimeMaterial(json.dumps({"tokens": {"id_token": f"{header}.{payload}."}}).encode(), None)

    assert AccountVault.suggested_display_name(runtime, AuthMode.CHAT_GPT) == "jwt-person@example.com"


def test_quota_buffer_reuses_cached_snapshot() -> None:
    calls = 0

    def refresh(_: CodexHomeTarget) -> CodexSnapshot:
        nonlocal calls
        calls += 1
        return CodexSnapshot(CodexAccount("chatgpt", f"user{calls}@example.com"), None, now_utc())

    buffer = QuotaSnapshotBuffer(refresh)
    first = buffer.get(CodexHomeTarget.SANDBOX)
    cached = buffer.get(CodexHomeTarget.SANDBOX)
    refreshed = buffer.get(CodexHomeTarget.SANDBOX, force_refresh=True)

    assert calls == 2
    assert cached.account.email == first.account.email
    assert refreshed.account.email == "user2@example.com"


def test_codex_rpc_timeout_does_not_block_on_live_stderr() -> None:
    class LiveProcess:
        stderr = object()

        @staticmethod
        def poll():
            return None

    output: queue.Queue[str | None] = queue.Queue()
    deadline = time.monotonic() - 0.01

    try:
        CodexRpcClient()._read_for_id(LiveProcess(), output, "1", deadline)
    except TimeoutError:
        pass
    else:
        raise AssertionError("Expected quota read to time out.")


def test_codex_command_resolver_checks_windowsapps_alias(tmp_path: Path, monkeypatch) -> None:
    local_app_data = tmp_path / "LocalAppData"
    alias_dir = local_app_data / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "codex.exe").write_text("placeholder")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("CQV_CODEX_COMMAND", raising=False)

    command = core_module.CodexCommandResolver.resolve()

    assert command.file_name == str(alias_dir / "codex.exe")


def test_codex_command_resolver_prefers_store_localcache_cli_over_windowsapps_alias(tmp_path: Path, monkeypatch) -> None:
    local_app_data = tmp_path / "LocalAppData"
    alias_dir = local_app_data / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "codex.exe").write_text("alias")
    store_bin = local_app_data / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0" / "LocalCache" / "Local" / "OpenAI" / "Codex" / "bin"
    store_bin.mkdir(parents=True)
    store_cli = store_bin / "codex.exe"
    store_cli.write_text("cli")
    monkeypatch.setenv("PATH", str(alias_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("CQV_CODEX_COMMAND", raising=False)

    command = core_module.CodexCommandResolver.resolve()

    assert command.file_name == str(store_cli)


def test_codex_command_resolver_falls_back_when_explicit_windowsapps_alias_is_missing(tmp_path: Path, monkeypatch) -> None:
    local_app_data = tmp_path / "LocalAppData"
    store_bin = local_app_data / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0" / "LocalCache" / "Local" / "OpenAI" / "Codex" / "bin"
    store_bin.mkdir(parents=True)
    store_cli = store_bin / "codex.exe"
    store_cli.write_text("cli")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("CQV_CODEX_COMMAND", str(local_app_data / "Microsoft" / "WindowsApps" / "codex.exe"))

    command = core_module.CodexCommandResolver.resolve()

    assert command.file_name == str(store_cli)


def test_codex_command_resolver_error_mentions_cli_and_alias(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("CQV_CODEX_COMMAND", raising=False)

    with pytest.raises(FileNotFoundError) as ex:
        core_module.CodexCommandResolver.resolve()

    assert "Codex CLI" in str(ex.value)
    assert "execution alias" in str(ex.value)


def test_codex_command_resolver_rejects_missing_explicit_path(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "Microsoft" / "WindowsApps" / "codex.exe"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    monkeypatch.setenv("CQV_CODEX_COMMAND", '"' + str(missing) + '"')

    with pytest.raises(FileNotFoundError) as ex:
        core_module.CodexCommandResolver.resolve()

    assert str(missing) in str(ex.value)
    assert "CQV_CODEX_COMMAND" in str(ex.value)


def test_codex_command_resolver_wraps_explicit_cmd(monkeypatch, tmp_path: Path) -> None:
    cmd = tmp_path / "codex.cmd"
    cmd.write_text("@echo off\n")
    monkeypatch.setenv("CQV_CODEX_COMMAND", str(cmd))

    command = core_module.CodexCommandResolver.resolve()

    assert command.file_name == "cmd.exe"
    assert command.arguments_prefix == ("/d", "/c", str(cmd))


def test_codex_command_resolver_expands_powershell_env_reference(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    alias_dir = local_app_data / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    alias = alias_dir / "codex.exe"
    alias.write_text("placeholder")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("CQV_CODEX_COMMAND", r"$env:LOCALAPPDATA\Microsoft\WindowsApps\codex.exe")

    command = core_module.CodexCommandResolver.resolve()

    assert command.file_name == str(alias)


def test_codex_rpc_client_wraps_windowsapps_launch_failure(monkeypatch, tmp_path: Path) -> None:
    def fail_popen(*args, **kwargs):
        raise FileNotFoundError("[WinError 2] The system cannot find the file specified")

    monkeypatch.setattr(core_module.subprocess, "Popen", fail_popen)
    command = CodexCommand(str(tmp_path / "Microsoft" / "WindowsApps" / "codex.exe"), tuple(), str(tmp_path / "Microsoft" / "WindowsApps" / "codex.exe"))

    with pytest.raises(FileNotFoundError) as ex:
        CodexRpcClient(command_resolver=lambda: command)._start_app_server(command, tmp_path / ".codex")

    assert "WindowsApps codex.exe alias exists but is not launchable" in str(ex.value)
    assert "Get-Command codex -All" in str(ex.value)


def test_codex_desktop_launcher_resolves_store_gui_app(monkeypatch, tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    app_dir = program_files / "WindowsApps" / "OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0" / "app"
    app_dir.mkdir(parents=True)
    gui = app_dir / "Codex.exe"
    gui.write_text("gui")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("CQV_CODEX_APP_COMMAND", raising=False)

    assert CodexDesktopAppLauncher.resolve_executable() == gui


def test_codex_desktop_launcher_resolves_appx_install_location_without_alias(monkeypatch, tmp_path: Path) -> None:
    install_location = tmp_path / "StoreInstall" / "OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0"
    app_dir = install_location / "app"
    app_dir.mkdir(parents=True)
    gui = app_dir / "Codex.exe"
    gui.write_text("gui")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "NoWindowsAppsAccess"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("CQV_CODEX_APP_COMMAND", raising=False)
    monkeypatch.setattr(core_module.CodexCommandResolver, "_resolve_powershell", staticmethod(lambda: "powershell.exe"))

    def fake_run(*args, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=str(install_location) + "\n")

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)

    assert CodexDesktopAppLauncher.resolve_executable() == gui


def test_codex_desktop_launcher_passes_sandbox_environment(monkeypatch, tmp_path: Path) -> None:
    gui = tmp_path / "Codex.exe"
    gui.write_text("gui")
    monkeypatch.setenv("CQV_CODEX_APP_COMMAND", str(gui))
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-profile"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "real-roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real-local"))
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "storage"))
    captured: dict[str, object] = {}

    class NoRunningCodex:
        @staticmethod
        def running_process_summary():
            return None

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(core_module.subprocess, "Popen", fake_popen)

    message = CodexDesktopAppLauncher(NoRunningCodex()).launch(paths, CodexHomeTarget.SANDBOX)

    env = captured["kwargs"]["env"]
    assert captured["args"] == [str(gui)]
    assert env["CODEX_HOME"] == str(paths.sandbox_codex_home)
    assert env["HOME"] == str(tmp_path / "real-home")
    assert env["USERPROFILE"] == str(tmp_path / "real-profile")
    assert env["APPDATA"] == str(tmp_path / "real-roaming")
    assert env["LOCALAPPDATA"] == str(tmp_path / "real-local")
    assert "Sandbox" in message


def test_codex_desktop_launcher_blocks_sandbox_when_codex_is_running(monkeypatch, tmp_path: Path) -> None:
    gui = tmp_path / "Codex.exe"
    gui.write_text("gui")
    monkeypatch.setenv("CQV_CODEX_APP_COMMAND", str(gui))
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "storage"))

    class RunningCodex:
        @staticmethod
        def running_process_summary():
            return "Codex.exe (desktop window)"

    with pytest.raises(RuntimeError, match="single-instance"):
        CodexDesktopAppLauncher(RunningCodex()).launch(paths, CodexHomeTarget.SANDBOX)


def test_sandbox_seed_skips_locked_optional_files(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"))
    paths.real_codex_home.mkdir(parents=True)
    (paths.real_codex_home / "auth.json").write_text('{"account":{"email":"seed@example.com"}}')
    (paths.real_codex_home / "config.toml").write_text('model_provider = "openai"\n')
    (paths.real_codex_home / "state_5.sqlite").write_bytes(b"busy")

    original_copy2 = core_module.shutil.copy2

    def fake_copy2(source, target):
        if Path(source).name == "state_5.sqlite":
            ex = OSError("mapped section is open")
            ex.winerror = 1224
            raise ex
        return original_copy2(source, target)

    monkeypatch.setattr(core_module.shutil, "copy2", fake_copy2)

    result = SandboxSeeder().seed(paths)

    assert result.copied_files == 2
    assert result.skipped_files == 1
    assert "state_5.sqlite" in result.warnings[0]
    assert paths.auth_path(CodexHomeTarget.SANDBOX).exists()


def test_sandbox_seed_copies_all_sessions_and_state_snapshot(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.real_codex_home.mkdir(parents=True)
    (paths.real_codex_home / "auth.json").write_text('{"account":{"email":"seed@example.com"}}')
    (paths.real_codex_home / "config.toml").write_text('model_provider = "openai"\n')
    (paths.real_codex_home / ".codex-global-state.json").write_text('{"seen":true}', encoding="utf-8")
    for index in range(45):
        session = paths.real_codex_home / "sessions" / "2026" / f"thread-{index:02}.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(f'{{"thread":{index}}}\n', encoding="utf-8")
    db = paths.real_codex_home / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table threads (id text primary key, title text)")
        conn.execute("insert into threads (id, title) values ('thread-1', 'hello')")
        conn.commit()

    result = SandboxSeeder().seed(paths)

    copied_sessions = list((paths.sandbox_codex_home / "sessions").rglob("*.jsonl"))
    assert len(copied_sessions) == 45
    assert (paths.sandbox_codex_home / ".codex-global-state.json").read_text(encoding="utf-8") == '{"seen":true}'
    with sqlite3.connect(paths.sandbox_codex_home / "state_5.sqlite") as conn:
        assert conn.execute("select id, title from threads").fetchone() == ("thread-1", "hello")
    assert not (paths.sandbox_codex_home / "state_5.sqlite-wal").exists()
    assert not (paths.sandbox_codex_home / "state_5.sqlite-shm").exists()
    assert result.skipped_files == 0


def test_sandbox_seed_copies_shared_assets_without_secret_like_files(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.real_codex_home.mkdir(parents=True)
    (paths.real_codex_home / "auth.json").write_text('{"account":{"email":"seed@example.com"}}')
    skill = paths.real_codex_home / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("review skill", encoding="utf-8")
    prompt = paths.real_codex_home / "prompts" / "daily.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("daily prompt", encoding="utf-8")
    secret = paths.real_codex_home / "skills" / "review" / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")

    result = SandboxSeeder().seed(paths)

    assert (paths.sandbox_codex_home / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "review skill"
    assert (paths.sandbox_codex_home / "prompts" / "daily.md").read_text(encoding="utf-8") == "daily prompt"
    assert not (paths.sandbox_codex_home / "skills" / "review" / ".env").exists()
    assert result.copied_files == 3
    assert result.skipped_files == 1


def test_backup_manager_restores_existing_and_deletes_new_files(tmp_path: Path) -> None:
    file = tmp_path / "state" / "auth.json"
    file.parent.mkdir()
    file.write_text("before")
    missing = tmp_path / "state" / "config.toml"
    manager = BackupManager(tmp_path / "backups")
    manager.create_restore_point("test", "test restore", [file, missing])
    file.write_text("after")
    missing.write_text("new")

    manager.restore_latest()

    assert file.read_text() == "before"
    assert not missing.exists()


def test_backup_manager_excludes_and_clears_sqlite_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "state" / "state_5.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("create table threads (id text primary key)")
        conn.execute("insert into threads (id) values ('before')")
        conn.commit()
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    manager = BackupManager(tmp_path / "backups")

    manifest = manager.create_restore_point("test", "test sqlite restore", [db, wal, shm])

    assert [Path(item.source_path).name for item in manifest.files] == ["state_5.sqlite"]
    with sqlite3.connect(db) as conn:
        conn.execute("insert into threads (id) values ('after')")
        conn.commit()
    wal.write_text("stale wal", encoding="utf-8")
    shm.write_text("stale shm", encoding="utf-8")

    manager.restore(manifest)

    assert not wal.exists()
    assert not shm.exists()
    with sqlite3.connect(db) as conn:
        assert [row[0] for row in conn.execute("select id from threads order by id")] == ["before"]


def test_snapshot_manager_separates_targets_and_manual_retention(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    for target in [CodexHomeTarget.SANDBOX, CodexHomeTarget.REAL]:
        home = paths.codex_home(target)
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(f'{{"{target.value}":true}}', encoding="utf-8")

    snapshots = SnapshotManager(paths)
    sandbox = snapshots.create_manual_snapshot(CodexHomeTarget.SANDBOX, "sandbox manual")
    real = snapshots.create_manual_snapshot(CodexHomeTarget.REAL, "real manual")

    assert sandbox.target == CodexHomeTarget.SANDBOX
    assert real.target == CodexHomeTarget.REAL
    assert sandbox.kind == SnapshotKind.MANUAL
    assert real.kind == SnapshotKind.MANUAL
    assert (paths.backups_root_for(CodexHomeTarget.SANDBOX) / "manual" / sandbox.id).exists()
    assert (paths.backups_root_for(CodexHomeTarget.REAL) / "manual" / real.id).exists()
    assert {item.id for item in snapshots.list_snapshots(CodexHomeTarget.SANDBOX, SnapshotKind.MANUAL)} == {sandbox.id}
    assert {item.id for item in snapshots.list_snapshots(CodexHomeTarget.REAL, SnapshotKind.MANUAL)} == {real.id}

    monkeypatch.setattr(core_module.BackupManager, "MAX_SNAPSHOT_COUNT", 1)
    with pytest.raises(RuntimeError, match="manual snapshots are over the limit|Manual Sandbox snapshots are over the limit"):
        snapshots.create_manual_snapshot(CodexHomeTarget.SANDBOX, "too many")


def test_manual_snapshot_includes_sessions_and_excludes_sqlite_wal_shm(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    home = paths.sandbox_codex_home
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"account":{"email":"person@example.com"}}', encoding="utf-8")
    (home / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    session = home / "sessions" / "2026" / "one.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text('{"type":"session_meta","payload":{"id":"thread-1"}}\n', encoding="utf-8")
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute("create table threads (id text primary key)")
        conn.commit()
    (home / "state_5.sqlite-wal").write_text("wal", encoding="utf-8")
    (home / "state_5.sqlite-shm").write_text("shm", encoding="utf-8")

    manifest = SnapshotManager(paths).create_manual_snapshot(CodexHomeTarget.SANDBOX, "full manual")

    source_names = {Path(item.source_path).name for item in manifest.files}
    assert "one.jsonl" in source_names
    assert "state_5.sqlite" in source_names
    assert "state_5.sqlite-wal" not in source_names
    assert "state_5.sqlite-shm" not in source_names


def test_legacy_restore_points_read_as_automatic(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    legacy = root / "legacy-id"
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_text(
        json.dumps(
            {
                "id": "legacy-id",
                "reason": "legacy",
                "summary": "old layout",
                "createdAt": now_utc().isoformat(),
                "files": [],
            }
        ),
        encoding="utf-8",
    )

    items = BackupManager(root, CodexHomeTarget.SANDBOX).list()

    assert len(items) == 1
    assert items[0].id == "legacy-id"
    assert items[0].kind == SnapshotKind.AUTOMATIC
    assert items[0].target == CodexHomeTarget.SANDBOX


def test_write_preview_and_audit_log_redact_sensitive_values(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"secret-token","api_key":"sk-should-not-show"}', encoding="utf-8")

    preview = WritePreviewBuilder.for_files(
        "Preview",
        CodexHomeTarget.REAL,
        tmp_path,
        [auth],
        "Using sk-secretvalue and Bearer abc.def.ghi",
    )

    assert preview.sample_paths == (str(auth.with_name("auth.json (redacted)")),)
    serialized_preview = json.dumps(preview.__dict__)
    assert "secret-token" not in serialized_preview
    assert "sk-secretvalue" not in serialized_preview
    assert "Bearer abc.def.ghi" not in serialized_preview

    log = AuditLog(tmp_path / "Logs" / "audit.jsonl")
    log.append(
        "switch",
        CodexHomeTarget.REAL,
        "failed",
        'summary sk-secretvalue "access_token":"abc"',
        error_message="Bearer abc.def",
    )
    text = (tmp_path / "Logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-secretvalue" not in text
    assert "abc.def" not in text
    assert "access_token" not in text
    assert "[redacted]" in text


def test_session_manager_repair_blocks_malformed_state_database(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.real_codex_home.mkdir(parents=True)
    paths.state_database_paths(CodexHomeTarget.REAL)[0].write_text("not sqlite", encoding="utf-8")

    with pytest.raises(RuntimeError, match="state database is unhealthy"):
        SessionManagerLauncher(paths).rescan_and_repair(CodexHomeTarget.REAL)


def test_session_meta_synchronizer_updates_first_line_provider(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "one.jsonl"
    session.parent.mkdir()
    session.write_text('{"type":"session_meta","payload":{"model_provider":"old"}}\n{"type":"event"}', encoding="utf-8")

    updated = SessionMetaSynchronizer().sync_providers([tmp_path / "sessions"], "openai-compatible")

    assert updated == 1
    assert '"model_provider":"openai-compatible"' in session.read_text(encoding="utf-8").splitlines()[0]


def test_sandbox_to_real_session_sync_copies_merges_and_skips_by_mtime(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    sandbox = paths.sandbox_codex_home
    real = paths.real_codex_home
    sandbox.mkdir(parents=True, exist_ok=True)
    real.mkdir(parents=True, exist_ok=True)
    _write_session_file(sandbox / "sessions" / "2026" / "missing.jsonl", "missing", "openai", 300)
    _write_session_file(sandbox / "sessions" / "2026" / "shared.jsonl", "shared", "openai", 400, "sandbox newer")
    _write_session_file(sandbox / "sessions" / "2026" / "same.jsonl", "same", "openai", 200, "same")
    _write_session_file(sandbox / "sessions" / "2026" / "real-newer.jsonl", "real-newer", "openai", 100, "sandbox older")
    _write_session_file(real / "sessions" / "2026" / "shared.jsonl", "shared", "old-provider", 100, "real older")
    _write_session_file(real / "sessions" / "2026" / "same.jsonl", "same", "openai", 200, "same")
    _write_session_file(real / "sessions" / "2026" / "real-newer.jsonl", "real-newer", "old-provider", 500, "real newer")
    paths.session_index_path(CodexHomeTarget.SANDBOX).write_text(
        '{"id":"missing","updated_at_ms":300}\n{"id":"shared","updated_at_ms":400}\n',
        encoding="utf-8",
    )
    paths.session_index_path(CodexHomeTarget.REAL).write_text(
        '{"id":"shared","updated_at_ms":100}\n{"id":"real-only","updated_at_ms":50}\n',
        encoding="utf-8",
    )
    sandbox_skill = sandbox / "skills" / "review" / "SKILL.md"
    sandbox_skill.parent.mkdir(parents=True)
    sandbox_skill.write_text("sandbox skill", encoding="utf-8")
    os.utime(sandbox_skill, (600, 600))
    sandbox_prompt = sandbox / "prompts" / "daily.md"
    sandbox_prompt.parent.mkdir(parents=True)
    sandbox_prompt.write_text("sandbox prompt", encoding="utf-8")
    os.utime(sandbox_prompt, (700, 700))
    real_prompt = real / "prompts" / "daily.md"
    real_prompt.parent.mkdir(parents=True)
    real_prompt.write_text("real older prompt", encoding="utf-8")
    os.utime(real_prompt, (100, 100))
    sandbox_secret = sandbox / "skills" / "review" / ".env"
    sandbox_secret.write_text("TOKEN=secret", encoding="utf-8")
    with sqlite3.connect(paths.state_database_path(CodexHomeTarget.REAL)) as conn:
        conn.execute(
            """
            create table threads (
              id text primary key,
              model_provider text,
              cwd text,
              has_user_event integer,
              first_user_message text,
              updated_at_ms integer,
              archived integer,
              source text
            )
            """
        )
        conn.commit()

    result = SandboxRealSessionSyncService(paths).sync("openai-compatible")

    assert result.copied_files == 1
    assert result.overwritten_files == 1
    assert result.asset_copied_files == 1
    assert result.asset_overwritten_files == 1
    assert result.skipped_same_files == 1
    assert result.skipped_real_newer_files == 1
    assert "sandbox newer" in (real / "sessions" / "2026" / "shared.jsonl").read_text(encoding="utf-8")
    assert "real newer" in (real / "sessions" / "2026" / "real-newer.jsonl").read_text(encoding="utf-8")
    assert (real / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "sandbox skill"
    assert (real / "prompts" / "daily.md").read_text(encoding="utf-8") == "sandbox prompt"
    assert not (real / "skills" / "review" / ".env").exists()
    assert '"model_provider":"openai-compatible"' in (real / "sessions" / "2026" / "missing.jsonl").read_text(encoding="utf-8").splitlines()[0]
    index_text = paths.session_index_path(CodexHomeTarget.REAL).read_text(encoding="utf-8")
    assert '"id":"missing"' in index_text
    assert '"id":"real-only"' in index_text
    assert '{"id":"shared","updated_at_ms":400}' in index_text
    with sqlite3.connect(paths.state_database_path(CodexHomeTarget.REAL)) as conn:
        rows = dict(conn.execute("select id, model_provider from threads").fetchall())
    assert rows["missing"] == "openai-compatible"
    assert rows["shared"] == "openai-compatible"
    assert result.restore_point.target == CodexHomeTarget.REAL
    assert result.restore_point.kind == SnapshotKind.AUTOMATIC


def test_sandbox_to_real_sync_sessions_option_skips_assets(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    sandbox = paths.sandbox_codex_home
    real = paths.real_codex_home
    sandbox.mkdir(parents=True, exist_ok=True)
    real.mkdir(parents=True, exist_ok=True)
    _write_session_file(sandbox / "sessions" / "2026" / "missing.jsonl", "missing", "openai", 300)
    sandbox_skill = sandbox / "skills" / "review" / "SKILL.md"
    sandbox_skill.parent.mkdir(parents=True)
    sandbox_skill.write_text("sandbox skill", encoding="utf-8")

    result = SandboxRealSessionSyncService(paths).sync("openai-compatible", sync_sessions=True, sync_assets=False)

    assert result.copied_files == 1
    assert result.asset_scanned_files == 0
    assert (real / "sessions" / "2026" / "missing.jsonl").exists()
    assert not (real / "skills" / "review" / "SKILL.md").exists()


def test_sandbox_to_real_sync_assets_option_skips_sessions_and_sqlite(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    sandbox = paths.sandbox_codex_home
    real = paths.real_codex_home
    sandbox.mkdir(parents=True, exist_ok=True)
    real.mkdir(parents=True, exist_ok=True)
    _write_session_file(sandbox / "sessions" / "2026" / "missing.jsonl", "missing", "openai", 300)
    paths.session_index_path(CodexHomeTarget.SANDBOX).write_text('{"id":"missing","updated_at_ms":300}\n', encoding="utf-8")
    paths.state_database_path(CodexHomeTarget.REAL).write_text("not sqlite", encoding="utf-8")
    sandbox_prompt = sandbox / "prompts" / "daily.md"
    sandbox_prompt.parent.mkdir(parents=True)
    sandbox_prompt.write_text("sandbox prompt", encoding="utf-8")

    result = SandboxRealSessionSyncService(paths).sync("openai-compatible", sync_sessions=False, sync_assets=True)

    assert result.scanned_files == 0
    assert result.asset_copied_files == 1
    assert not (real / "sessions" / "2026" / "missing.jsonl").exists()
    assert not paths.session_index_path(CodexHomeTarget.REAL).exists()
    assert paths.state_database_path(CodexHomeTarget.REAL).read_text(encoding="utf-8") == "not sqlite"
    assert (real / "prompts" / "daily.md").read_text(encoding="utf-8") == "sandbox prompt"


def test_sandbox_to_real_session_sync_backs_up_index_only_merge(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.sandbox_codex_home.mkdir(parents=True, exist_ok=True)
    paths.real_codex_home.mkdir(parents=True, exist_ok=True)
    paths.session_index_path(CodexHomeTarget.SANDBOX).write_text(
        '{"id":"sandbox-only","updated_at_ms":300}\n',
        encoding="utf-8",
    )
    paths.session_index_path(CodexHomeTarget.REAL).write_text(
        '{"id":"real-only","updated_at_ms":100}\n',
        encoding="utf-8",
    )

    result = SandboxRealSessionSyncService(paths).sync("openai-compatible")

    assert result.scanned_files == 0
    assert result.index_entries_merged == 1
    assert [Path(item.source_path).name for item in result.restore_point.files] == ["session_index.jsonl"]
    index_text = paths.session_index_path(CodexHomeTarget.REAL).read_text(encoding="utf-8")
    assert '"id":"sandbox-only"' in index_text
    assert '"id":"real-only"' in index_text


def test_sandbox_to_real_session_sync_aborts_malformed_sqlite_before_writing(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.sandbox_codex_home.mkdir(parents=True, exist_ok=True)
    paths.real_codex_home.mkdir(parents=True, exist_ok=True)
    _write_session_file(paths.sandbox_codex_home / "sessions" / "2026" / "missing.jsonl", "missing", "openai", 300)
    paths.state_database_path(CodexHomeTarget.REAL).write_text("not sqlite", encoding="utf-8")

    with pytest.raises(RuntimeError, match="state database is unhealthy|malformed|unreadable"):
        SandboxRealSessionSyncService(paths).sync("openai-compatible")

    assert not (paths.real_codex_home / "sessions" / "2026" / "missing.jsonl").exists()
    assert not any(paths.backups_root_for(CodexHomeTarget.REAL).rglob("manifest.json"))


def test_safe_switch_updates_sandbox_and_can_rollback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = AppPaths.for_current_user(str(tmp_path / "CodexQuotaViewerWindows"), str(tmp_path / ".codex"))
    paths.ensure_created()
    paths.auth_path(CodexHomeTarget.SANDBOX).write_text('{"old":true}')
    paths.config_path(CodexHomeTarget.SANDBOX).write_text('approval_policy = "on-request"\n')
    session = paths.sandbox_codex_home / "sessions" / "one.jsonl"
    session.parent.mkdir()
    session.write_text('{"type":"session_meta","payload":{"model_provider":"old"}}\n')
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )

    class RepairClient:
        def rescan_and_repair(self, target):
            return OfficialRepairSummary()

    service = SafeSwitchService(paths, vault, SessionMetaSynchronizer(), RepairClient())
    result = service.switch(account, CodexHomeTarget.SANDBOX)

    assert result.updated_session_files == 1
    assert "sk-test" in paths.auth_path(CodexHomeTarget.SANDBOX).read_text()
    assert 'approval_policy = "on-request"' in paths.config_path(CodexHomeTarget.SANDBOX).read_text()

    service.rollback_latest(CodexHomeTarget.SANDBOX)

    assert paths.auth_path(CodexHomeTarget.SANDBOX).read_text() == '{"old":true}'


def test_safe_switch_allows_external_sandbox_storage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    paths = AppPaths.for_current_user(str(tmp_path / "local" / "CodexQuotaViewerWindows"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )

    class RepairClient:
        def rescan_and_repair(self, target):
            return OfficialRepairSummary()

    service = SafeSwitchService(paths, vault, SessionMetaSynchronizer(), RepairClient())
    service.switch(account, CodexHomeTarget.SANDBOX)

    assert paths.sandbox_codex_home == tmp_path / "heavy" / "SandboxCodexHome"
    assert "sk-test" in paths.auth_path(CodexHomeTarget.SANDBOX).read_text()


def test_real_switch_blocked_by_desktop_does_not_create_or_restore_backup(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.real_codex_home.mkdir(parents=True)
    paths.auth_path(CodexHomeTarget.REAL).write_text('{"old":true}')
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )

    class BlockingDesktop:
        def prepare_for_real_switch(self):
            return CodexDesktopSwitchPreparation(False, True, False, "Please close Codex Desktop manually and retry.")

        def try_restart(self):
            return "not called"

    def fail_restore_latest(self):
        raise AssertionError("rollback should not run before real switch writes")

    monkeypatch.setattr(core_module.BackupManager, "restore_latest", fail_restore_latest)

    service = SafeSwitchService(
        paths,
        vault,
        SessionMetaSynchronizer(),
        _NoopRepairClient(),
        desktop_controller=BlockingDesktop(),
        allow_real_codex_home_override=True,
    )

    with pytest.raises(RuntimeError, match="Please close Codex Desktop manually"):
        service.switch(account, CodexHomeTarget.REAL)

    assert paths.auth_path(CodexHomeTarget.REAL).read_text() == '{"old":true}'
    assert list(paths.backups_root_for(CodexHomeTarget.REAL).iterdir()) == []


def test_real_switch_syncs_provider_visibility_when_provider_changes(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.real_codex_home.mkdir(parents=True)
    paths.auth_path(CodexHomeTarget.REAL).write_text('{"old":true}')
    paths.config_path(CodexHomeTarget.REAL).write_text(
        'service_tier = "fast"\nmodel_provider = "old-provider"\n\n[windows]\nsandbox = "unelevated"\n',
        encoding="utf-8",
    )
    session = paths.real_codex_home / "sessions" / "one.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        '{"type":"session_meta","payload":{"id":"thread-1","model_provider":"old-provider","cwd":"\\\\\\\\?\\\\C:\\\\work"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message"}}\n',
        encoding="utf-8",
    )
    paths.session_index_path(CodexHomeTarget.REAL).write_text("index-before", encoding="utf-8")
    _write_provider_state_db(paths.state_database_paths(CodexHomeTarget.REAL)[0], "old-provider")
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "ChatGPT",
        AccountRuntimeMaterial(b'{"new":true}', None),
        AuthMode.CHAT_GPT,
        "openai",
    )

    class ReadyDesktop:
        def prepare_for_real_switch(self):
            return CodexDesktopSwitchPreparation(True, False, False, "not running")

        def try_restart(self):
            raise AssertionError("restart should not be requested")

    class RepairShouldNotRun:
        def rescan_and_repair(self, target):
            raise AssertionError("real switch should not run repair automatically")

    service = SafeSwitchService(
        paths,
        vault,
        SessionMetaSynchronizer(),
        RepairShouldNotRun(),
        desktop_controller=ReadyDesktop(),
        allow_real_codex_home_override=True,
        repair_after_switch=True,
    )

    result = service.switch(account, CodexHomeTarget.REAL)

    assert result.updated_session_files == 1
    assert paths.auth_path(CodexHomeTarget.REAL).read_text() == '{"new":true}'
    merged = paths.config_path(CodexHomeTarget.REAL).read_text(encoding="utf-8")
    assert 'model_provider = "openai"' in merged.split("[windows]", 1)[0]
    assert 'sandbox = "unelevated"' in merged
    first_line = session.read_text(encoding="utf-8").splitlines()[0]
    assert '"model_provider":"openai"' in first_line
    assert paths.session_index_path(CodexHomeTarget.REAL).read_text(encoding="utf-8") == "index-before"
    with sqlite3.connect(paths.state_database_paths(CodexHomeTarget.REAL)[0]) as conn:
        assert conn.execute("select model_provider, has_user_event, cwd from threads where id = 'thread-1'").fetchone() == (
            "openai",
            1,
            r"C:\work",
        )
    backed_up = {Path(item.source_path).name for item in result.restore_point.files}
    assert {"auth.json", "config.toml", "state_5.sqlite", "one.jsonl"}.issubset(backed_up)


def test_real_oauth_to_oauth_switch_writes_only_auth(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.real_codex_home.mkdir(parents=True)
    paths.auth_path(CodexHomeTarget.REAL).write_text('{"old":true}')
    original_config = 'service_tier = "fast"\nmodel_provider = "openai"\n\n[windows]\nsandbox = "unelevated"\n'
    paths.config_path(CodexHomeTarget.REAL).write_text(original_config, encoding="utf-8")
    session = paths.real_codex_home / "sessions" / "one.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"type":"session_meta","payload":{"model_provider":"openai"}}\n', encoding="utf-8")
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "Second OAuth",
        AccountRuntimeMaterial(b'{"new":true}', b'model_provider = "openai"\nmodel = "should-not-be-copied"\n'),
        AuthMode.CHAT_GPT,
        "openai",
    )

    class ReadyDesktop:
        def prepare_for_real_switch(self):
            return CodexDesktopSwitchPreparation(True, False, False, "not running")

        def try_restart(self):
            raise AssertionError("restart should not be requested")

    service = SafeSwitchService(
        paths,
        vault,
        SessionMetaSynchronizer(),
        _NoopRepairClient(),
        desktop_controller=ReadyDesktop(),
        allow_real_codex_home_override=True,
    )

    planned = service.planned_write_files(account, CodexHomeTarget.REAL)
    result = service.switch(account, CodexHomeTarget.REAL)

    assert [path.name for path in planned] == ["auth.json"]
    assert paths.auth_path(CodexHomeTarget.REAL).read_text() == '{"new":true}'
    assert paths.config_path(CodexHomeTarget.REAL).read_text(encoding="utf-8") == original_config
    assert session.read_text(encoding="utf-8") == '{"type":"session_meta","payload":{"model_provider":"openai"}}\n'
    assert [Path(item.source_path).name for item in result.restore_point.files] == ["auth.json"]
    assert result.updated_session_files == 0


def test_real_oauth_switch_normalizes_stale_provider_wire_api(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(str(tmp_path / "data"), str(tmp_path / ".codex"), str(tmp_path / "heavy"))
    paths.ensure_created()
    paths.real_codex_home.mkdir(parents=True)
    paths.auth_path(CodexHomeTarget.REAL).write_text('{"old":true}')
    paths.config_path(CodexHomeTarget.REAL).write_text(
        "\n".join(
            [
                'service_tier = "fast"',
                'model_provider = "openai"',
                "",
                "[model_providers.openai-compatible]",
                'base_url = "https://old.example.test/v1"',
                'wire_api = "chat"',
                "",
                "[windows]",
                'sandbox = "unelevated"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "Second OAuth",
        AccountRuntimeMaterial(b'{"new":true}', None),
        AuthMode.CHAT_GPT,
        "openai",
    )

    class ReadyDesktop:
        def prepare_for_real_switch(self):
            return CodexDesktopSwitchPreparation(True, False, False, "not running")

        def try_restart(self):
            raise AssertionError("restart should not be requested")

    service = SafeSwitchService(
        paths,
        vault,
        SessionMetaSynchronizer(),
        _NoopRepairClient(),
        desktop_controller=ReadyDesktop(),
        allow_real_codex_home_override=True,
    )

    planned = service.planned_write_files(account, CodexHomeTarget.REAL)
    result = service.switch(account, CodexHomeTarget.REAL)

    config = paths.config_path(CodexHomeTarget.REAL).read_text(encoding="utf-8")
    assert "auth.json" in [path.name for path in planned]
    assert "config.toml" in [path.name for path in planned]
    assert "state_5.sqlite" in [path.name for path in planned]
    assert paths.auth_path(CodexHomeTarget.REAL).read_text() == '{"new":true}'
    assert 'wire_api = "responses"' in config
    assert 'wire_api = "chat"' not in config
    assert 'sandbox = "unelevated"' in config
    backed_up = {Path(item.source_path).name for item in result.restore_point.files}
    assert {"auth.json", "config.toml", "state_5.sqlite"}.issubset(backed_up)
    assert result.updated_session_files == 0


def test_safe_switch_keeps_account_when_repair_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = AppPaths.for_current_user(str(tmp_path / "CodexQuotaViewerWindows"), str(tmp_path / ".codex"))
    paths.ensure_created()
    paths.auth_path(CodexHomeTarget.SANDBOX).write_text('{"old":true}')
    vault = AccountVault(paths.accounts_root)
    account = vault.upsert(
        "API",
        AccountVault.create_api_runtime("sk-test", "https://api.example.test/v1", "gpt-5.4"),
        AuthMode.API_KEY,
        "openai-compatible",
        "https://api.example.test/v1",
        "gpt-5.4",
    )

    class BrokenRepairClient:
        def rescan_and_repair(self, target):
            raise RuntimeError("repair is unavailable")

    service = SafeSwitchService(paths, vault, SessionMetaSynchronizer(), BrokenRepairClient())
    result = service.switch(account, CodexHomeTarget.SANDBOX)

    assert "sk-test" in paths.auth_path(CodexHomeTarget.SANDBOX).read_text()
    assert result.repair_summary == OfficialRepairSummary()
    assert result.repair_warning and "repair is unavailable" in result.repair_warning
