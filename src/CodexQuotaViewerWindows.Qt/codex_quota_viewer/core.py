from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error, parse, request

from .models import (
    AccountMetadata,
    AccountRecord,
    AccountRuntimeMaterial,
    AppSettings,
    AuthMode,
    CodexAccount,
    CodexDesktopSwitchPreparation,
    CodexHomeTarget,
    CodexSnapshot,
    OfficialRepairSummary,
    RateLimitSnapshot,
    RateLimitWindow,
    ProviderVisibilitySyncSummary,
    RestorePointFile,
    RestorePointManifest,
    SandboxSeedResult,
    SandboxRealSessionSyncResult,
    SnapshotKind,
    SnapshotRetentionStatus,
    SwitchOperationResult,
    UiLanguage,
    WritePreview,
    now_utc,
)
from .sessions.errors import SessionError
from .sessions.paths import ensure_inside_realpath


class ApiAccountError(Exception):
    pass


class CodexRpcError(Exception):
    pass


@dataclass(frozen=True)
class RuntimeConfigSummary:
    provider_id: str | None
    base_url: str | None
    model: str | None


@dataclass(frozen=True)
class ApiAccountDraft:
    display_name: str
    api_key: str
    normalized_base_url: str
    model: str
    used_fallback: bool
    warning_message: str | None


@dataclass(frozen=True)
class CodexCommand:
    file_name: str
    arguments_prefix: tuple[str, ...]
    display_name: str

    def build_arguments(self, arguments: Iterable[str]) -> list[str]:
        return [*self.arguments_prefix, *arguments]


@dataclass
class SessionManagerProcess:
    port: int
    process: subprocess.Popen[Any] | None = None
    log_handle: Any | None = None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dumps(value), encoding="utf-8")


class SharedAssetRules:
    ROOT_NAMES = ("skills", "prompts", "agents", "templates")
    IGNORED_DIR_NAMES = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "cache",
        "logs",
        "node_modules",
        ".venv",
        "venv",
    }
    IGNORED_FILE_NAMES = {".env", ".env.local", ".env.production"}
    IGNORED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}

    @classmethod
    def should_skip(cls, path: Path, root: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        parts = {part.lower() for part in relative.parts[:-1]}
        if parts & cls.IGNORED_DIR_NAMES:
            return True
        name = path.name.lower()
        return name in cls.IGNORED_FILE_NAMES or path.suffix.lower() in cls.IGNORED_SUFFIXES


class AppPaths:
    STORAGE_ROOT_ENV = "CODEX_QUOTA_VIEWER_STORAGE_ROOT"

    def __init__(self, data_root: Path, real_codex_home: Path, storage_root: Path | None = None):
        self.data_root = data_root
        self.storage_root = storage_root or data_root
        self.sandbox_codex_home = self.storage_root / "SandboxCodexHome"
        self.accounts_root = data_root / "Accounts"
        self.backups_root = self.storage_root / "SwitchBackups"
        self.session_manager_home = self.storage_root / "SessionManager"
        self.real_session_manager_home = self.storage_root / "SessionManagerReal"
        self.logs_root = data_root / "Logs"
        self.real_codex_home = real_codex_home

    @staticmethod
    def for_current_user(
        data_root_override: str | None = None,
        real_codex_home_override: str | None = None,
        storage_root_override: str | None = None,
    ) -> "AppPaths":
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        data_root = Path(data_root_override) if data_root_override else Path(local_app_data) / "CodexQuotaViewerWindows"
        real_home = Path(real_codex_home_override) if real_codex_home_override else Path.home() / ".codex"
        if storage_root_override:
            storage_root = Path(storage_root_override)
        elif data_root_override:
            storage_root = data_root
        else:
            storage_root = AppPaths._default_storage_root(data_root)
        return AppPaths(data_root, real_home, storage_root)

    @staticmethod
    def _default_storage_root(data_root: Path) -> Path:
        configured = os.environ.get(AppPaths.STORAGE_ROOT_ENV)
        if configured and configured.strip():
            return Path(configured)
        drive = AppPaths._largest_non_system_fixed_drive()
        if drive:
            return drive / "CodexQuotaViewerWindows"
        return data_root

    @staticmethod
    def _largest_non_system_fixed_drive() -> Path | None:
        if os.name != "nt":
            return None
        system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
        candidates: list[tuple[int, Path]] = []
        for code in range(ord("D"), ord("Z") + 1):
            drive_name = f"{chr(code)}:"
            if drive_name.upper() == system_drive:
                continue
            root = Path(f"{drive_name}/")
            if not root.exists():
                continue
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(str(root)) != 3:
                    continue
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            candidates.append((usage.free, root))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @property
    def settings_path(self) -> Path:
        return self.data_root / "settings.json"

    @property
    def sandbox_backups_root(self) -> Path:
        return self.backups_root / "Sandbox"

    @property
    def real_backups_root(self) -> Path:
        return self.backups_root / "Real"

    def codex_home(self, target: CodexHomeTarget) -> Path:
        return self.real_codex_home if target == CodexHomeTarget.REAL else self.sandbox_codex_home

    def auth_path(self, target: CodexHomeTarget) -> Path:
        return self.codex_home(target) / "auth.json"

    def config_path(self, target: CodexHomeTarget) -> Path:
        return self.codex_home(target) / "config.toml"

    def session_index_path(self, target: CodexHomeTarget) -> Path:
        return self.codex_home(target) / "session_index.jsonl"

    def backups_root_for(self, target: CodexHomeTarget) -> Path:
        return self.real_backups_root if target == CodexHomeTarget.REAL else self.sandbox_backups_root

    def snapshot_root_for(self, target: CodexHomeTarget, kind: SnapshotKind) -> Path:
        return self.backups_root_for(target) / kind.value

    def session_manager_home_for(self, target: CodexHomeTarget) -> Path:
        return self.real_session_manager_home if target == CodexHomeTarget.REAL else self.session_manager_home

    def session_roots(self, target: CodexHomeTarget) -> list[Path]:
        home = self.codex_home(target)
        return [home / "sessions", home / "archived_sessions"]

    def shared_asset_roots(self, target: CodexHomeTarget) -> list[Path]:
        home = self.codex_home(target)
        return [home / name for name in SharedAssetRules.ROOT_NAMES]

    def state_database_path(self, target: CodexHomeTarget) -> Path:
        return self.codex_home(target) / "state_5.sqlite"

    def state_database_paths(self, target: CodexHomeTarget) -> list[Path]:
        return [self.state_database_path(target)]

    def global_state_paths(self, target: CodexHomeTarget) -> list[Path]:
        home = self.codex_home(target)
        return [home / ".codex-global-state.json", home / ".codex-global-state.json.bak"]

    def protected_codex_files(self, target: CodexHomeTarget) -> list[Path]:
        return [
            self.auth_path(target),
            self.config_path(target),
            self.session_index_path(target),
            *self.state_database_paths(target),
            *self.global_state_paths(target),
        ]

    @property
    def audit_log_path(self) -> Path:
        return self.logs_root / "audit.jsonl"

    def ensure_created(self) -> None:
        for path in [
            self.data_root,
            self.storage_root,
            self.sandbox_codex_home,
            self.accounts_root,
            self.backups_root,
            self.sandbox_backups_root,
            self.real_backups_root,
            self.session_manager_home,
            self.real_session_manager_home,
            self.logs_root,
        ]:
            path.mkdir(parents=True, exist_ok=True)


class AppSettingsStore:
    def __init__(self, settings_path: Path):
        self.settings_path = settings_path

    def load(self) -> AppSettings:
        data = _read_json(self.settings_path, {})
        return AppSettings(
            CodexHomeTarget.from_json(data.get("activeCodexHomeTarget")),
            UiLanguage.from_json(data.get("language")),
        )

    def save(self, settings: AppSettings) -> None:
        _write_json(
            self.settings_path,
            {
                "activeCodexHomeTarget": settings.active_codex_home_target.value,
                "language": settings.language.value,
            },
        )


class RuntimeConfig:
    @staticmethod
    def parse(config_data: bytes | None) -> RuntimeConfigSummary:
        if not config_data:
            return RuntimeConfigSummary(None, None, None)
        text = config_data.decode("utf-8", errors="replace")
        root_text = RuntimeConfig._root_table_text(text)
        provider_id = RuntimeConfig._read_string_value(root_text, "model_provider")
        return RuntimeConfigSummary(
            provider_id,
            RuntimeConfig._read_provider_base_url(text, provider_id),
            RuntimeConfig._read_string_value(root_text, "model"),
        )

    @staticmethod
    def synthesize_openai_compatible(base_url: str, model: str) -> str:
        return "\n".join(
            [
                'model_provider = "openai-compatible"',
                f'model = "{RuntimeConfig._escape_toml(model)}"',
                "",
                "[model_providers.openai-compatible]",
                'name = "OpenAI Compatible"',
                f'base_url = "{RuntimeConfig._escape_toml(base_url)}"',
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "",
            ]
        )

    @staticmethod
    def merge_for_switch(current_config: bytes | None, target_config: bytes | None, auth_mode: AuthMode) -> str:
        current_text = RuntimeConfig.normalize_compatibility(current_config)
        if target_config:
            target_text = RuntimeConfig.normalize_compatibility(target_config)
        else:
            target_text = 'model_provider = "openai"\n' if auth_mode == AuthMode.CHAT_GPT else 'model_provider = "openai-compatible"\n'
        return RuntimeConfig._merge_toml_overlay(current_text, target_text)

    @staticmethod
    def needs_compatibility_normalization(config_data: bytes | None) -> bool:
        if not config_data:
            return False
        text = config_data.decode("utf-8", errors="replace")
        return RuntimeConfig._legacy_wire_api_pattern().search(text) is not None

    @staticmethod
    def normalize_compatibility(config_data: bytes | str | None) -> str:
        if not config_data:
            return ""
        text = config_data.decode("utf-8", errors="replace") if isinstance(config_data, bytes) else config_data
        return RuntimeConfig._legacy_wire_api_pattern().sub(
            lambda match: f'{match.group(1)}"responses"{match.group(3)}',
            text,
        )

    @staticmethod
    def _merge_toml_overlay(current_text: str, target_text: str) -> str:
        if not current_text.strip():
            return RuntimeConfig._ensure_trailing_newline(target_text)
        if not target_text.strip():
            return RuntimeConfig._ensure_trailing_newline(current_text)
        root_keys, section_headers = RuntimeConfig._analyze_toml(target_text)
        retained = RuntimeConfig._remove_overlaid_toml(current_text, root_keys, section_headers).rstrip()
        root_overlay, section_overlay = RuntimeConfig._split_root_and_sections(target_text)
        merged = RuntimeConfig._insert_root_overlay(retained, root_overlay)
        if section_overlay.strip():
            merged = section_overlay.strip() if not merged.strip() else merged.rstrip() + "\n\n" + section_overlay.strip()
        return RuntimeConfig._ensure_trailing_newline(merged)

    @staticmethod
    def _split_root_and_sections(text: str) -> tuple[str, str]:
        lines = text.strip().splitlines()
        for index, line in enumerate(lines):
            if RuntimeConfig._try_table_header(line.strip()) is not None:
                return "\n".join(lines[:index]).strip(), "\n".join(lines[index:]).strip()
        return text.strip(), ""

    @staticmethod
    def _insert_root_overlay(retained: str, root_overlay: str) -> str:
        overlay = root_overlay.strip()
        if not overlay:
            return retained
        if not retained.strip():
            return overlay
        lines = retained.splitlines()
        insert_at = len(lines)
        for index, line in enumerate(lines):
            if RuntimeConfig._try_table_header(line.strip()) is not None:
                insert_at = index
                break
        root_lines = "\n".join(lines[:insert_at]).rstrip()
        section_lines = "\n".join(lines[insert_at:]).lstrip()
        merged_root = overlay if not root_lines else root_lines + "\n" + overlay
        return merged_root if not section_lines else merged_root.rstrip() + "\n\n" + section_lines

    @staticmethod
    def _analyze_toml(text: str) -> tuple[set[str], set[str]]:
        root_keys: set[str] = set()
        section_headers: set[str] = set()
        inside_section = False
        for line in text.splitlines():
            trimmed = line.strip()
            header = RuntimeConfig._try_table_header(trimmed)
            if header is not None:
                section_headers.add(header)
                inside_section = True
                continue
            key = RuntimeConfig._try_assignment_key(trimmed)
            if not inside_section and key:
                root_keys.add(key)
        return root_keys, section_headers

    @staticmethod
    def _remove_overlaid_toml(text: str, root_keys: set[str], section_headers: set[str]) -> str:
        output: list[str] = []
        skipping_section = False
        inside_section = False
        for line in text.splitlines():
            trimmed = line.strip()
            header = RuntimeConfig._try_table_header(trimmed)
            if header is not None:
                skipping_section = header in section_headers
                inside_section = True
                if not skipping_section:
                    output.append(line)
                continue
            if skipping_section:
                continue
            key = RuntimeConfig._try_assignment_key(trimmed)
            if not inside_section and key and key in root_keys:
                continue
            output.append(line)
        return "\n".join(output)

    @staticmethod
    def _try_table_header(trimmed: str) -> str | None:
        if len(trimmed) < 3 or not trimmed.startswith("["):
            return None
        closing = "]]" if trimmed.startswith("[[") else "]"
        return trimmed if trimmed.endswith(closing) else None

    @staticmethod
    def _try_assignment_key(trimmed: str) -> str | None:
        if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
            return None
        key = trimmed.split("=", 1)[0].strip()
        return key or None

    @staticmethod
    def _read_string_value(text: str, key: str) -> str | None:
        match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"(?P<value>[^"]*)"', text)
        return match.group("value") if match else None

    @staticmethod
    def _read_provider_base_url(text: str, provider_id: str | None) -> str | None:
        if not provider_id:
            return None
        if provider_id == "openai":
            return None
        section = RuntimeConfig._section_text(text, f"[model_providers.{provider_id}]")
        return RuntimeConfig._read_string_value(section, "base_url") if section is not None else None

    @staticmethod
    def _section_text(text: str, section_header: str) -> str | None:
        lines = text.splitlines()
        captured: list[str] = []
        in_section = False
        for line in lines:
            trimmed = line.strip()
            if RuntimeConfig._try_table_header(trimmed) is not None:
                if in_section:
                    break
                in_section = trimmed == section_header
                continue
            if in_section:
                captured.append(line)
        return "\n".join(captured) if in_section else None

    @staticmethod
    def _legacy_wire_api_pattern() -> re.Pattern[str]:
        return re.compile(r'(?m)^(\s*wire_api\s*=\s*)(["\'])chat\2(\s*(?:#.*)?)$')

    @staticmethod
    def _root_table_text(text: str) -> str:
        lines: list[str] = []
        for line in text.splitlines():
            if RuntimeConfig._try_table_header(line.strip()) is not None:
                break
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _escape_toml(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _ensure_trailing_newline(text: str) -> str:
        return text if text.endswith("\n") else text + "\n"


def parse_codex_id_token_plan(auth_data: bytes) -> tuple[str | None, str | None]:
    """Decode the OAuth ``id_token`` JWT in a Codex ``auth.json`` and pull out
    ``(chatgpt_plan_type, chatgpt_account_id)``.

    Mirrors the claims layout in ``CLIProxyAPI/internal/auth/codex/jwt_parser.go``:
    custom claims live under the ``https://api.openai.com/auth`` namespace.
    Pure stdlib, returns ``(None, None)`` on any parse failure.
    """
    try:
        payload = json.loads(auth_data)
    except (ValueError, TypeError):
        return (None, None)
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return (None, None)
    claims_segment = id_token.split(".")[1]
    padding = "=" * (-len(claims_segment) % 4)
    try:
        claims_bytes = base64.urlsafe_b64decode(claims_segment + padding)
        claims = json.loads(claims_bytes)
    except (ValueError, TypeError, base64.binascii.Error):
        return (None, None)
    if not isinstance(claims, dict):
        return (None, None)
    auth_info = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_info, dict):
        return (None, None)
    plan_raw = auth_info.get("chatgpt_plan_type")
    account_raw = auth_info.get("chatgpt_account_id")
    plan = str(plan_raw).strip().lower() if isinstance(plan_raw, str) and plan_raw.strip() else None
    account_id = str(account_raw).strip() if isinstance(account_raw, str) and account_raw.strip() else None
    return (plan, account_id)


class VaultQuotaCache:
    """Persistent per-account quota snapshot store.

    Mirrors the macOS app's ``VaultQuotaCacheStore`` at a smaller scale: a
    single JSON file at the vault root keyed by ``account_id``. Atomic write
    via ``<path>.tmp`` + replace; tolerant load (corrupt / missing file
    yields an empty dict instead of raising).
    """

    SCHEMA_VERSION = 1

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._lock = threading.RLock()

    def load(self) -> dict[str, CodexSnapshot]:
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> dict[str, CodexSnapshot]:
        if not self.cache_path.exists():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, dict):
            return {}
        out: dict[str, CodexSnapshot] = {}
        for account_id, raw in snapshots.items():
            snapshot = CodexSnapshot.from_json(raw)
            if snapshot is not None:
                out[str(account_id)] = snapshot
        return out

    def get(self, account_id: str) -> CodexSnapshot | None:
        return self.load().get(account_id)

    def upsert(self, account_id: str, snapshot: CodexSnapshot) -> None:
        with self._lock:
            current = self._load_locked()
            current[account_id] = snapshot
            self._save_locked(current)

    def delete(self, account_id: str) -> None:
        with self._lock:
            current = self._load_locked()
            if account_id not in current:
                return
            current.pop(account_id, None)
            self._save_locked(current)

    def _save_locked(self, snapshots: dict[str, CodexSnapshot]) -> None:
        payload = {
            "schemaVersion": self.SCHEMA_VERSION,
            "snapshots": {account_id: snap.to_json() for account_id, snap in snapshots.items()},
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.cache_path)


class AccountVault:
    OAUTH_COMMON_DIR = "oauth-common"
    OAUTH_COMMON_CONFIG = "config.toml"
    OAUTH_PREFERENCE_CONFIG = "preference.config.toml"
    LEGACY_ACCOUNT_CONFIG = "config.toml"

    def __init__(self, accounts_root: Path):
        self.accounts_root = accounts_root
        self.index_path = accounts_root / "accounts.json"

    @property
    def oauth_common_config_path(self) -> Path:
        return self.accounts_root / self.OAUTH_COMMON_DIR / self.OAUTH_COMMON_CONFIG

    def load(self) -> list[AccountRecord]:
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        metadata = self._load_index()
        records: list[AccountRecord] = []
        for item in metadata:
            directory = self.accounts_root / item.id
            auth_path = directory / "auth.json"
            config_path = self._account_config_path(directory, item.auth_mode)
            if auth_path.exists():
                records.append(AccountRecord(item, str(directory), str(auth_path), str(config_path)))
        return sorted(records, key=lambda record: (record.metadata.auth_mode.value, record.metadata.display_name.lower()))

    def upsert(
        self,
        display_name: str,
        runtime: AccountRuntimeMaterial,
        auth_mode: AuthMode,
        provider_id: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> AccountRecord:
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        account_id = self.account_id_for_mode(runtime, auth_mode)
        now = now_utc()
        metadata = self._load_index()
        existing = next((item for item in metadata if item.id == account_id), None)
        if existing is None:
            if auth_mode == AuthMode.CHAT_GPT:
                existing = self._find_existing_oauth_by_identity(metadata, runtime.auth_data)
            elif auth_mode == AuthMode.API_KEY:
                existing = self._find_existing_api_by_identity(metadata, runtime, base_url)
            if existing is not None:
                account_id = existing.id
        normalized_display_name = self._display_name_for_upsert(
            display_name.strip() or self.default_display_name(auth_mode),
            existing,
            auth_mode,
        )
        provider_id, base_url = self._metadata_provider_fields(auth_mode, provider_id, base_url)
        duplicate_ids = self._duplicate_identity_ids(metadata, runtime, auth_mode, base_url)
        duplicate_ids.add(account_id)
        next_metadata = AccountMetadata(
            account_id,
            normalized_display_name,
            auth_mode,
            provider_id,
            base_url,
            model,
            existing.created_at if existing else now,
            now,
            self.runtime_key_for_mode(runtime, auth_mode),
        )
        metadata = [item for item in metadata if item.id not in duplicate_ids]
        metadata.append(next_metadata)
        metadata.sort(key=lambda item: (item.auth_mode.value, item.display_name.lower()))
        directory = self.accounts_root / account_id
        directory.mkdir(parents=True, exist_ok=True)
        auth_path = directory / "auth.json"
        config_path = self._account_config_path(directory, auth_mode)
        auth_path.write_bytes(runtime.auth_data)
        if auth_mode == AuthMode.API_KEY:
            if runtime.config_data:
                config_path.write_bytes(runtime.config_data)
            elif config_path.exists():
                config_path.unlink()
        elif runtime.config_data:
            self.write_oauth_common_config(runtime.config_data, overwrite=False)
        self._save_index(metadata)
        return AccountRecord(next_metadata, str(directory), str(auth_path), str(config_path))

    def read_runtime(self, record: AccountRecord) -> AccountRuntimeMaterial:
        auth = Path(record.auth_path).read_bytes()
        if record.metadata.auth_mode == AuthMode.CHAT_GPT:
            return AccountRuntimeMaterial(auth, self.read_oauth_config(record))
        config_path = Path(record.config_path)
        config = config_path.read_bytes() if config_path.exists() else None
        return AccountRuntimeMaterial(auth, config)

    def read_chatgpt_plan_type(self, record: AccountRecord) -> str | None:
        if record.metadata.auth_mode != AuthMode.CHAT_GPT:
            return None
        try:
            auth = Path(record.auth_path).read_bytes()
        except OSError:
            return None
        plan, _account = parse_codex_id_token_plan(auth)
        return plan

    def read_oauth_config(self, record: AccountRecord) -> bytes | None:
        for path in self.oauth_config_candidates(record):
            if path.exists():
                return path.read_bytes()
        return None

    def oauth_config_source_label(self, record: AccountRecord) -> str | None:
        for path in self.oauth_config_candidates(record):
            if path.exists():
                if path.name == self.OAUTH_PREFERENCE_CONFIG:
                    return "account preference"
                if path.parent.name == self.OAUTH_COMMON_DIR:
                    return "OAuth common"
                return "legacy account config"
        return None

    def has_oauth_account_preference(self, record: AccountRecord) -> bool:
        return self.oauth_preference_config_path(record.metadata.id).exists()

    def oauth_config_candidates(self, record: AccountRecord) -> list[Path]:
        directory = Path(record.directory_path)
        return [
            self.oauth_preference_config_path(record.metadata.id),
            self.oauth_common_config_path,
            directory / self.LEGACY_ACCOUNT_CONFIG,
        ]

    def oauth_preference_config_path(self, account_id: str) -> Path:
        return self.accounts_root / account_id / self.OAUTH_PREFERENCE_CONFIG

    def write_oauth_common_config(self, config_data: bytes, overwrite: bool) -> None:
        if not overwrite and self.oauth_common_config_path.exists():
            return
        self.oauth_common_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.oauth_common_config_path.write_bytes(self._normalize_oauth_config(config_data))

    def find(self, account_id: str) -> AccountRecord | None:
        return next((record for record in self.load() if record.metadata.id == account_id), None)

    def rename(self, account_id: str, display_name: str) -> AccountRecord:
        trimmed = display_name.strip()
        if not trimmed:
            raise ValueError("Display name is required.")
        metadata = self._load_index()
        for index, item in enumerate(metadata):
            if item.id == account_id:
                metadata[index] = AccountMetadata(
                    item.id,
                    trimmed,
                    item.auth_mode,
                    item.provider_id,
                    item.base_url,
                    item.model,
                    item.created_at,
                    item.last_used_at,
                    item.runtime_key,
                )
                metadata.sort(key=lambda entry: (entry.auth_mode.value, entry.display_name.lower()))
                self._save_index(metadata)
                record = self.find(account_id)
                if record is None:
                    raise FileNotFoundError(f"Account runtime files were not found: {account_id}")
                return record
        raise FileNotFoundError(f"Account was not found: {account_id}")

    def delete(self, account_id: str) -> None:
        metadata = self._load_index()
        remaining = [item for item in metadata if item.id != account_id]
        if len(remaining) == len(metadata):
            raise FileNotFoundError(f"Account was not found: {account_id}")
        self._save_index(remaining)
        directory = self.accounts_root / account_id
        if directory.exists():
            shutil.rmtree(directory)

    def capture_sandbox_current(self, paths: AppPaths, display_name: str) -> AccountRecord:
        auth_path = paths.auth_path(CodexHomeTarget.SANDBOX)
        if not auth_path.exists():
            raise FileNotFoundError(f"Sandbox auth.json does not exist. Seed the sandbox first: {auth_path}")
        config_path = paths.config_path(CodexHomeTarget.SANDBOX)
        auth = auth_path.read_bytes()
        config = config_path.read_bytes() if config_path.exists() else None
        mode = self.detect_auth_mode(auth)
        summary = RuntimeConfig.parse(config)
        runtime = AccountRuntimeMaterial(auth, config)
        normalized_display_name = display_name
        if mode == AuthMode.CHAT_GPT:
            suggested = self.suggested_display_name(runtime, mode)
            if suggested != self.default_display_name(mode):
                normalized_display_name = suggested
        return self.upsert(normalized_display_name, runtime, mode, summary.provider_id, summary.base_url, summary.model)

    def find_oauth_by_identity(self, auth_data: bytes) -> AccountRecord | None:
        existing = self._find_existing_oauth_by_identity(self._load_index(), auth_data)
        return self.find(existing.id) if existing else None

    def find_api_by_identity(self, auth_data: bytes, config_data: bytes | None, base_url: str | None = None) -> AccountRecord | None:
        existing = self._find_existing_api_by_identity(
            self._load_index(),
            AccountRuntimeMaterial(auth_data, config_data),
            base_url,
        )
        return self.find(existing.id) if existing else None

    def _find_existing_oauth_by_identity(self, metadata: list[AccountMetadata], auth_data: bytes) -> AccountMetadata | None:
        return self._find_existing_oauth_by_auth(metadata, auth_data) or self._find_existing_oauth_by_email(metadata, auth_data)

    def _find_existing_oauth_by_auth(self, metadata: list[AccountMetadata], auth_data: bytes) -> AccountMetadata | None:
        for item in metadata:
            if item.auth_mode != AuthMode.CHAT_GPT:
                continue
            auth_path = self.accounts_root / item.id / "auth.json"
            try:
                if auth_path.exists() and auth_path.read_bytes() == auth_data:
                    return item
            except OSError:
                continue
        return None

    def _find_existing_api_by_identity(
        self,
        metadata: list[AccountMetadata],
        runtime: AccountRuntimeMaterial,
        base_url: str | None,
    ) -> AccountMetadata | None:
        key, normalized_base_url = self._api_identity(runtime.auth_data, runtime.config_data, base_url)
        if not key:
            return None
        for item in metadata:
            if item.auth_mode != AuthMode.API_KEY:
                continue
            auth_path = self.accounts_root / item.id / "auth.json"
            config_path = self._account_config_path(self.accounts_root / item.id, item.auth_mode)
            try:
                existing_auth = auth_path.read_bytes() if auth_path.exists() else b""
                existing_config = config_path.read_bytes() if config_path.exists() else None
            except OSError:
                continue
            existing_key, existing_base_url = self._api_identity(existing_auth, existing_config, item.base_url)
            if existing_key != key:
                continue
            if normalized_base_url and existing_base_url:
                if normalized_base_url == existing_base_url:
                    return item
                continue
            return item
        return None

    def _duplicate_identity_ids(
        self,
        metadata: list[AccountMetadata],
        runtime: AccountRuntimeMaterial,
        auth_mode: AuthMode,
        base_url: str | None,
    ) -> set[str]:
        if auth_mode == AuthMode.CHAT_GPT:
            email = self._try_find_email(runtime.auth_data)
            result = set()
            for item in metadata:
                if item.auth_mode != AuthMode.CHAT_GPT:
                    continue
                auth_path = self.accounts_root / item.id / "auth.json"
                try:
                    existing_auth = auth_path.read_bytes() if auth_path.exists() else b""
                except OSError:
                    existing_auth = b""
                if existing_auth == runtime.auth_data:
                    result.add(item.id)
                    continue
                existing_email = self._try_find_email(existing_auth) if existing_auth else None
                if email and existing_email and email.strip().lower() == existing_email.strip().lower():
                    result.add(item.id)
            return result
        if auth_mode == AuthMode.API_KEY:
            key, normalized_base_url = self._api_identity(runtime.auth_data, runtime.config_data, base_url)
            if not key:
                return set()
            result = set()
            for item in metadata:
                if item.auth_mode != AuthMode.API_KEY:
                    continue
                auth_path = self.accounts_root / item.id / "auth.json"
                config_path = self._account_config_path(self.accounts_root / item.id, item.auth_mode)
                try:
                    existing_auth = auth_path.read_bytes() if auth_path.exists() else b""
                    existing_config = config_path.read_bytes() if config_path.exists() else None
                except OSError:
                    continue
                existing_key, existing_base_url = self._api_identity(existing_auth, existing_config, item.base_url)
                if existing_key != key:
                    continue
                if normalized_base_url and existing_base_url and normalized_base_url != existing_base_url:
                    continue
                result.add(item.id)
            return result
        return set()

    def _find_existing_oauth_by_email(self, metadata: list[AccountMetadata], auth_data: bytes) -> AccountMetadata | None:
        email = self._try_find_email(auth_data)
        if not email:
            return None
        normalized = email.strip().lower()
        for item in metadata:
            if item.auth_mode != AuthMode.CHAT_GPT:
                continue
            if self._is_plausible_email(item.display_name) and item.display_name.strip().lower() == normalized:
                return item
            auth_path = self.accounts_root / item.id / "auth.json"
            try:
                existing_email = self._try_find_email(auth_path.read_bytes()) if auth_path.exists() else None
            except OSError:
                existing_email = None
            if existing_email and existing_email.strip().lower() == normalized:
                return item
        return None

    @staticmethod
    def _display_name_for_upsert(incoming: str, existing: AccountMetadata | None, auth_mode: AuthMode) -> str:
        if existing is None:
            return incoming
        default_name = AccountVault.default_display_name(auth_mode)
        existing_name = existing.display_name.strip()
        incoming_is_automatic = incoming in {"Sandbox Current", default_name}
        if auth_mode == AuthMode.CHAT_GPT:
            incoming_is_automatic = incoming_is_automatic or AccountVault._is_plausible_email(incoming)
        existing_is_placeholder = not existing_name or existing_name in {"Sandbox Current", default_name}
        if incoming_is_automatic and not existing_is_placeholder:
            return existing_name
        if existing_is_placeholder and incoming and incoming != "Sandbox Current":
            return incoming
        return incoming

    @staticmethod
    def _metadata_provider_fields(
        auth_mode: AuthMode,
        provider_id: str | None,
        base_url: str | None,
    ) -> tuple[str | None, str | None]:
        if auth_mode == AuthMode.CHAT_GPT:
            return "openai", None
        return provider_id, base_url

    @staticmethod
    def _api_identity(auth_data: bytes, config_data: bytes | None, base_url: str | None) -> tuple[str | None, str | None]:
        key = AccountVault._try_find_api_key(auth_data)
        parsed = RuntimeConfig.parse(config_data)
        return key, AccountVault._normalize_api_base_url(base_url or parsed.base_url)

    @staticmethod
    def _try_find_api_key(auth_data: bytes) -> str | None:
        try:
            node = json.loads(auth_data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(node, dict):
            return None
        for key in ["OPENAI_API_KEY", "api_key", "apikey"]:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _normalize_api_base_url(value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        parsed = parse.urlparse(value.strip())
        if not parsed.scheme or not parsed.netloc:
            return value.strip().rstrip("/").lower()
        path = parsed.path.rstrip("/")
        return parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", "")).rstrip("/")

    def _load_index(self) -> list[AccountMetadata]:
        raw = _read_json(self.index_path, [])
        if not isinstance(raw, list):
            return []
        result: list[AccountMetadata] = []
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                result.append(AccountMetadata.from_json(item))
        return result

    def _save_index(self, metadata: list[AccountMetadata]) -> None:
        _write_json(self.index_path, [item.to_json() for item in metadata])

    @staticmethod
    def create_api_runtime(api_key: str, base_url: str, model: str) -> AccountRuntimeMaterial:
        auth_json = _json_dumps({"OPENAI_API_KEY": api_key, "auth_mode": "apikey"})
        config = RuntimeConfig.synthesize_openai_compatible(base_url, model)
        return AccountRuntimeMaterial(auth_json.encode("utf-8"), config.encode("utf-8"))

    @staticmethod
    def detect_auth_mode(auth_data: bytes) -> AuthMode:
        text = auth_data.decode("utf-8", errors="replace").lower()
        return AuthMode.API_KEY if '"auth_mode"' in text and "apikey" in text else AuthMode.CHAT_GPT

    @staticmethod
    def suggested_display_name(runtime: AccountRuntimeMaterial, mode: AuthMode) -> str:
        if mode == AuthMode.CHAT_GPT:
            email = AccountVault._try_find_email(runtime.auth_data)
            if email:
                return email
        return AccountVault.default_display_name(mode)

    @staticmethod
    def default_display_name(mode: AuthMode) -> str:
        return "API Account" if mode == AuthMode.API_KEY else "ChatGPT Account"

    @staticmethod
    def account_id(runtime: AccountRuntimeMaterial) -> str:
        return AccountVault.runtime_key(runtime)[:24]

    @staticmethod
    def account_id_for_mode(runtime: AccountRuntimeMaterial, mode: AuthMode) -> str:
        return AccountVault.runtime_key_for_mode(runtime, mode)[:24]

    @staticmethod
    def runtime_key(runtime: AccountRuntimeMaterial) -> str:
        sha = hashlib.sha256()
        sha.update(runtime.auth_data)
        if runtime.config_data is not None:
            sha.update(runtime.config_data)
        return sha.hexdigest()

    @staticmethod
    def runtime_key_for_mode(runtime: AccountRuntimeMaterial, mode: AuthMode) -> str:
        if mode == AuthMode.CHAT_GPT:
            return hashlib.sha256(runtime.auth_data).hexdigest()
        return AccountVault.runtime_key(runtime)

    @classmethod
    def _account_config_path(cls, directory: Path, auth_mode: AuthMode) -> Path:
        return directory / (cls.OAUTH_PREFERENCE_CONFIG if auth_mode == AuthMode.CHAT_GPT else cls.LEGACY_ACCOUNT_CONFIG)

    @staticmethod
    def _normalize_oauth_config(config_data: bytes) -> bytes:
        text = RuntimeConfig.merge_for_switch(config_data, None, AuthMode.CHAT_GPT)
        return text.encode("utf-8")

    @staticmethod
    def _try_find_email(auth_data: bytes) -> str | None:
        try:
            node = json.loads(auth_data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return AccountVault._find_email(node)

    @staticmethod
    def _find_email(node: Any) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "email" in str(key).lower() and isinstance(value, str) and AccountVault._is_plausible_email(value):
                    return value
            for value in node.values():
                found = AccountVault._find_email(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = AccountVault._find_email(item)
                if found:
                    return found
        elif isinstance(node, str):
            if AccountVault._is_plausible_email(node):
                return node
            return AccountVault._try_find_email_in_jwt(node)
        return None

    @staticmethod
    def _try_find_email_in_jwt(text: str) -> str | None:
        parts = text.split(".")
        if len(parts) < 2:
            return None
        try:
            payload = parts[1].replace("-", "+").replace("_", "/")
            payload += "=" * ((4 - len(payload) % 4) % 4)
            node = json.loads(base64.b64decode(payload))
            return AccountVault._find_email(node)
        except Exception:
            return None

    @staticmethod
    def _is_plausible_email(text: str) -> bool:
        at = text.find("@")
        return text.strip() == text and 0 < at < len(text) - 3 and not any(ch.isspace() for ch in text) and text.find(".", at) > at + 1


class ApiAccountService:
    def __init__(self, probe_timeout_seconds: float = 4):
        self.probe_timeout_seconds = probe_timeout_seconds

    def configure(self, api_key: str, raw_base_url: str, display_name: str | None = None, model: str | None = None) -> ApiAccountDraft:
        api_key = api_key.strip()
        raw_base_url = raw_base_url.strip()
        if not api_key:
            raise ApiAccountError("API key is required.")
        if not raw_base_url:
            raise ApiAccountError("Base URL is required.")
        try:
            normalized = self.normalize_base_url(raw_base_url, ensure_v1=True)
            models = self.probe_models(api_key, normalized)
            chosen_model = self._normalize_model(model) or self.preferred_model(models) or "gpt-5.4"
            return ApiAccountDraft(self._normalize_display_name(display_name, normalized), api_key, normalized, chosen_model, False, None)
        except ApiAccountError:
            raise
        except error.HTTPError as ex:
            if ex.code in {401, 403}:
                raise ApiAccountError("API authentication failed. Check the API key and endpoint permissions.") from ex
            return self._fallback_draft(api_key, raw_base_url, display_name, model)
        except Exception:
            return self._fallback_draft(api_key, raw_base_url, display_name, model)

    def _fallback_draft(self, api_key: str, raw_base_url: str, display_name: str | None, model: str | None) -> ApiAccountDraft:
        fallback_url = self.normalize_base_url(raw_base_url, ensure_v1=True)
        return ApiAccountDraft(
            self._normalize_display_name(display_name, fallback_url),
            api_key,
            fallback_url,
            self._normalize_model(model) or "gpt-5.4",
            True,
            "Auto-detect failed; fallback config was applied.",
        )

    @staticmethod
    def normalize_base_url(raw_base_url: str, ensure_v1: bool) -> str:
        parsed = parse.urlparse(raw_base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ApiAccountError("Base URL is not a valid OpenAI-compatible endpoint.")
        normalized = parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")
        if ensure_v1 and not normalized.lower().endswith("/v1"):
            normalized += "/v1"
        return normalized

    def probe_models(self, api_key: str, normalized_base_url: str) -> list[str]:
        req = request.Request(
            normalized_base_url + "/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with request.urlopen(req, timeout=self.probe_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        ids = [item.get("id") for item in data.get("data", []) if isinstance(item, dict) and item.get("id")]
        if not ids:
            raise RuntimeError("Model probe returned no usable models.")
        return ids

    @staticmethod
    def create_runtime(draft: ApiAccountDraft) -> AccountRuntimeMaterial:
        return AccountVault.create_api_runtime(draft.api_key, draft.normalized_base_url, draft.model)

    @staticmethod
    def preferred_model(model_ids: Iterable[str]) -> str | None:
        normalized = [item.strip() for item in model_ids if item and item.strip()]
        for prefix in ["gpt-5", "gpt-4.1", "gpt-4o", "o4", "o3", "gpt-4", "gpt-3.5"]:
            for model in normalized:
                if model.lower().startswith(prefix) and not ApiAccountService._is_non_chat_model(model):
                    return model
        return next((model for model in normalized if not ApiAccountService._is_non_chat_model(model)), normalized[0] if normalized else None)

    @staticmethod
    def _normalize_display_name(display_name: str | None, normalized_base_url: str) -> str:
        return display_name.strip() if display_name and display_name.strip() else parse.urlparse(normalized_base_url).hostname or "API Account"

    @staticmethod
    def _normalize_model(model: str | None) -> str | None:
        return model.strip() if model and model.strip() else None

    @staticmethod
    def _is_non_chat_model(model: str) -> bool:
        lowered = model.lower()
        return "embedding" in lowered or "whisper" in lowered or "tts" in lowered


class CodexCommandResolver:
    @staticmethod
    def resolve() -> CodexCommand:
        explicit = os.environ.get("CQV_CODEX_COMMAND")
        explicit_error: FileNotFoundError | None = None
        if explicit and explicit.strip():
            try:
                return CodexCommandResolver._resolve_explicit(explicit)
            except FileNotFoundError as ex:
                explicit_error = ex
        command = CodexCommandResolver._resolve_auto()
        if command:
            return command
        if explicit_error:
            raise explicit_error
        raise FileNotFoundError(
            "Could not find the codex CLI executable. Install the Codex CLI, enable the Codex app execution alias, "
            "or set CQV_CODEX_COMMAND to codex.cmd/codex.exe/codex.ps1. The Microsoft Store Codex Desktop app "
            "does not always expose a callable codex command to other desktop apps."
        )

    @staticmethod
    def _resolve_auto() -> CodexCommand | None:
        for directory in CodexCommandResolver._candidate_directories():
            cmd = directory / "codex.cmd"
            if cmd.exists():
                return CodexCommand("cmd.exe", ("/d", "/c", str(cmd)), str(cmd))
            exe = directory / "codex.exe"
            if exe.exists():
                return CodexCommand(str(exe), tuple(), str(exe))
            ps1 = directory / "codex.ps1"
            if ps1.exists():
                shell = CodexCommandResolver._resolve_powershell()
                return CodexCommand(shell, ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)), str(ps1))
        return None

    @staticmethod
    def _resolve_explicit(value: str) -> CodexCommand:
        expanded = CodexCommandResolver._expand_environment_references(value.strip().strip('"').strip("'"))
        path_like = any(separator in expanded for separator in ("\\", "/")) or expanded.lower().endswith((".exe", ".cmd", ".ps1"))
        if not path_like:
            return CodexCommand(expanded, tuple(), expanded)
        path = Path(expanded)
        if not path.exists():
            raise FileNotFoundError(
                f"CQV_CODEX_COMMAND points to a missing file: {path}. "
                "Use Get-Command codex to find the real CLI path, install the Codex CLI, or clear CQV_CODEX_COMMAND."
            )
        if path.suffix.lower() == ".cmd":
            return CodexCommand("cmd.exe", ("/d", "/c", str(path)), str(path))
        if path.suffix.lower() == ".ps1":
            shell = CodexCommandResolver._resolve_powershell()
            return CodexCommand(shell, ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)), str(path))
        return CodexCommand(str(path), tuple(), str(path))

    @staticmethod
    def _expand_environment_references(value: str) -> str:
        def replace_powershell_env(match: re.Match[str]) -> str:
            name = match.group(1)
            return os.environ.get(name, match.group(0))

        expanded = re.sub(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", replace_powershell_env, value)
        return os.path.expanduser(os.path.expandvars(expanded))

    @staticmethod
    def describe_launch_failure(command: CodexCommand, ex: OSError) -> str:
        base = f"Could not start codex CLI at {command.display_name}: {ex}"
        if "Microsoft\\WindowsApps" in command.display_name or "Microsoft/WindowsApps" in command.display_name:
            return (
                base
                + ". The WindowsApps codex.exe alias exists but is not launchable from this app. "
                "This usually means the Microsoft Store Codex Desktop app did not expose a CLI for this Windows user, "
                "or CQV_CODEX_COMMAND points at another user's alias. Run Get-Command codex -All in the same user session, "
                "install the Codex CLI if no command is listed, then set CQV_CODEX_COMMAND to the real codex.cmd/codex.exe path."
            )
        return base + ". Install the Codex CLI or set CQV_CODEX_COMMAND to the real codex.cmd/codex.exe path."

    @staticmethod
    def _resolve_powershell() -> str:
        for name in ["pwsh.exe", "powershell.exe"]:
            for directory in CodexCommandResolver._candidate_directories():
                candidate = directory / name
                if candidate.exists():
                    return str(candidate)
        return "powershell.exe"

    @staticmethod
    def _path_directories() -> list[Path]:
        return [Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item and Path(item).is_dir()]

    @staticmethod
    def _candidate_directories() -> list[Path]:
        path_directories = CodexCommandResolver._path_directories()
        windows_alias_directories = [path for path in path_directories if CodexCommandResolver._is_windowsapps_alias_directory(path)]
        candidates = [path for path in path_directories if not CodexCommandResolver._is_windowsapps_alias_directory(path)]
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        program_files = os.environ.get("ProgramFiles")
        if local_app_data:
            candidates.extend(CodexCommandResolver._store_package_bin_directories(Path(local_app_data)))
        if app_data:
            candidates.append(Path(app_data) / "npm")
        if program_files:
            candidates.append(Path(program_files) / "nodejs")
        candidates.extend(windows_alias_directories)
        if local_app_data:
            candidates.append(Path(local_app_data) / "Microsoft" / "WindowsApps")
        seen: set[str] = set()
        result: list[Path] = []
        for candidate in candidates:
            try:
                normalized = str(candidate.resolve()).lower()
            except OSError:
                normalized = str(candidate).lower()
            if normalized in seen or not candidate.is_dir():
                continue
            seen.add(normalized)
            result.append(candidate)
        return result

    @staticmethod
    def _store_package_bin_directories(local_app_data: Path) -> list[Path]:
        packages_root = local_app_data / "Packages"
        try:
            packages = list(packages_root.glob("OpenAI.Codex_*"))
        except OSError:
            return []
        candidates = [
            package / "LocalCache" / "Local" / "OpenAI" / "Codex" / "bin"
            for package in packages
        ]
        return sorted(candidates, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)

    @staticmethod
    def _is_windowsapps_alias_directory(path: Path) -> bool:
        normalized = str(path).replace("/", "\\").rstrip("\\").lower()
        return normalized.endswith("\\microsoft\\windowsapps")


class CodexRpcClient:
    def __init__(
        self,
        command_resolver: Callable[[], CodexCommand] | None = None,
        timeout_seconds: float = 8,
        logs_root: Path | None = None,
    ):
        self.command_resolver = command_resolver or CodexCommandResolver.resolve
        self.timeout_seconds = timeout_seconds
        self.logs_root = logs_root
        self._active_processes: set[subprocess.Popen[Any]] = set()
        self._active_processes_lock = threading.Lock()

    def fetch_snapshot(self, codex_home: Path, timeout_seconds: float | None = None) -> CodexSnapshot:
        if not codex_home.exists():
            raise FileNotFoundError(f"Codex home does not exist: {codex_home}")
        timeout_seconds = timeout_seconds or self.timeout_seconds
        command = self.command_resolver()
        process = self._start_app_server(command, codex_home)
        self._track_process(process)
        output: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump_stdout, args=(process, output), daemon=True).start()
        deadline = time.monotonic() + timeout_seconds
        try:
            self._send(process, "1", "initialize", {"clientInfo": {"name": "CodexQuotaViewerWindows", "version": "0.2.0"}, "protocolVersion": 2})
            self._expect_ok(process, output, "1", deadline)
            self._send(process, "2", "account/read", {})
            account_message = self._read_for_id(process, output, "2", deadline)
            account = self._parse_account(account_message)
            if account.type.lower() == "apikey":
                return CodexSnapshot(account, None, now_utc(), "Quota is unavailable for API-key accounts.")
            self._send(process, "3", "account/rateLimits/read", {})
            rate_message = self._read_for_id(process, output, "3", deadline)
            error_message = self._read_error(rate_message)
            if error_message:
                return CodexSnapshot(account, None, now_utc(), error_message)
            return CodexSnapshot(account, self._parse_rate_limits(rate_message), now_utc())
        finally:
            self._untrack_process(process)
            self._try_kill(process)

    def fetch_snapshot_for_account(
        self,
        runtime: AccountRuntimeMaterial,
        timeout_seconds: float | None = None,
    ) -> CodexSnapshot:
        """Fetch quota for a saved account without changing the active home.

        Materializes the account's auth/config bytes into a throwaway
        ``<tmp>/.codex/`` directory and reuses :meth:`fetch_snapshot`.
        """
        with tempfile.TemporaryDirectory(prefix="cqv-quota-") as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / "auth.json").write_bytes(runtime.auth_data)
            if runtime.config_data:
                (codex_home / "config.toml").write_bytes(runtime.config_data)
            return self.fetch_snapshot(codex_home, timeout_seconds=timeout_seconds)

    def dispose(self) -> None:
        with self._active_processes_lock:
            processes = list(self._active_processes)
            self._active_processes.clear()
        for process in processes:
            self._try_kill(process)

    def _track_process(self, process: subprocess.Popen[Any]) -> None:
        with self._active_processes_lock:
            self._active_processes.add(process)

    def _untrack_process(self, process: subprocess.Popen[Any]) -> None:
        with self._active_processes_lock:
            self._active_processes.discard(process)

    def _start_app_server(self, command: CodexCommand, codex_home: Path) -> subprocess.Popen[Any]:
        home = codex_home.parent
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["CODEX_HOME"] = str(codex_home)
        args = [command.file_name, *command.build_arguments(["-s", "read-only", "-a", "untrusted", "app-server"])]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
        except OSError as ex:
            raise FileNotFoundError(CodexCommandResolver.describe_launch_failure(command, ex)) from ex

    @staticmethod
    def _pump_stdout(process: subprocess.Popen[Any], output: queue.Queue[str | None]) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    @staticmethod
    def _send(process: subprocess.Popen[Any], request_id: str, method: str, params: dict[str, Any]) -> None:
        if process.stdin is None:
            raise CodexRpcError("codex app-server input is no longer available.")
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, separators=(",", ":"))
        try:
            process.stdin.write(payload + "\n")
            process.stdin.flush()
        except OSError as ex:
            raise CodexRpcError("codex app-server closed the pipe before responding: " + str(ex)) from ex

    def _expect_ok(self, process: subprocess.Popen[Any], output: queue.Queue[str | None], request_id: str, deadline: float) -> None:
        message = self._read_for_id(process, output, request_id, deadline)
        error_message = self._read_error(message)
        if error_message:
            raise CodexRpcError(error_message)

    def _read_for_id(self, process: subprocess.Popen[Any], output: queue.Queue[str | None], request_id: str, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            try:
                line = output.get(timeout=max(0.05, min(0.3, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as ex:
                raise CodexRpcError(f"Invalid JSON-RPC response: {line}") from ex
            if str(message.get("id")) == request_id:
                return message
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out while reading quota.")
        stderr = ""
        if process.poll() is not None:
            try:
                if process.stderr is not None:
                    stderr = process.stderr.read()
            except Exception:
                pass
        raise CodexRpcError(stderr.strip() or "codex app-server exited early.")

    @staticmethod
    def _parse_account(message: dict[str, Any]) -> CodexAccount:
        error_message = CodexRpcClient._read_error(message)
        if error_message:
            raise CodexRpcError(error_message)
        account = (message.get("result") or {}).get("account")
        if not isinstance(account, dict):
            raise CodexRpcError("The current account is not signed in, or auth.json is invalid.")
        return CodexAccount(str(account.get("type") or ""), account.get("email"), account.get("planType"))

    def _parse_rate_limits(self, message: dict[str, Any]) -> RateLimitSnapshot:
        node = (message.get("result") or {}).get("rateLimits")
        if not isinstance(node, dict):
            raise CodexRpcError("account/rateLimits/read is missing rateLimits.")

        def window(value: Any) -> RateLimitWindow | None:
            if not isinstance(value, dict):
                return None
            return RateLimitWindow(float(value.get("usedPercent") or 0), value.get("windowDurationMins"), value.get("resetsAt"))

        primary = window(node.get("primary"))
        secondary = window(node.get("secondary"))
        if primary is None and secondary is None and self.logs_root is not None:
            try:
                app_log(
                    self.logs_root,
                    "rateLimits returned with no windows (keys=%s, planType=%r, limitId=%r)"
                    % (sorted(node.keys()), node.get("planType"), node.get("limitId")),
                )
            except Exception:
                pass
        return RateLimitSnapshot(node.get("limitId"), node.get("limitName"), primary, secondary, node.get("planType"))

    @staticmethod
    def _read_error(message: dict[str, Any]) -> str | None:
        error_node = message.get("error")
        if not isinstance(error_node, dict):
            return None
        return str(error_node.get("message") or "Unknown codex RPC error.")

    @staticmethod
    def _try_kill(process: subprocess.Popen[Any]) -> None:
        child_pids = CodexRpcClient._child_pids(process)
        if process.poll() is not None and not CodexRpcClient._any_pid_running(child_pids):
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=0.6)
            if not CodexRpcClient._any_pid_running(child_pids):
                return
        except Exception:
            pass
        if CodexRpcClient._try_kill_with_psutil(process, child_pids):
            return
        if CodexRpcClient._try_kill_with_taskkill(process, child_pids):
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1)
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        except Exception:
            pass

    @staticmethod
    def _child_pids(process: subprocess.Popen[Any]) -> list[int]:
        try:
            import psutil  # type: ignore
        except Exception:
            return []
        try:
            root = psutil.Process(process.pid)
            return [item.pid for item in root.children(recursive=True)]
        except Exception:
            return []

    @staticmethod
    def _any_pid_running(pids: list[int]) -> bool:
        if not pids:
            return False
        try:
            import psutil  # type: ignore
        except Exception:
            return False
        for pid in pids:
            try:
                process = psutil.Process(pid)
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _try_kill_with_psutil(process: subprocess.Popen[Any], child_pids: list[int] | None = None) -> bool:
        try:
            import psutil  # type: ignore
        except Exception:
            return False
        processes: list[Any] = []
        try:
            root = psutil.Process(process.pid)
            processes = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            processes = []
        except Exception:
            return False
        seen = {item.pid for item in processes}
        for pid in child_pids or []:
            if pid in seen:
                continue
            try:
                item = psutil.Process(pid)
                processes.insert(0, item)
                seen.add(pid)
            except psutil.NoSuchProcess:
                continue
            except Exception:
                return False
        if not processes:
            return True
        for item in processes:
            try:
                item.terminate()
            except Exception:
                pass
        try:
            _, alive = psutil.wait_procs(processes, timeout=1.0)
        except Exception:
            alive = []
        for item in alive:
            try:
                item.kill()
            except Exception:
                pass
        try:
            psutil.wait_procs(alive, timeout=1.0)
        except Exception:
            pass
        return not CodexRpcClient._any_pid_running([item.pid for item in processes])

    @staticmethod
    def _try_kill_with_taskkill(process: subprocess.Popen[Any], child_pids: list[int] | None = None) -> bool:
        if os.name != "nt":
            return False
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=2,
                check=False,
            )
            try:
                process.wait(timeout=1)
            except Exception:
                pass
            return process.poll() is not None and not CodexRpcClient._any_pid_running(child_pids or [])
        except Exception:
            return False


class ChatGptLoginService:
    def __init__(self, command_resolver: Callable[[], CodexCommand] | None = None):
        self.command_resolver = command_resolver or CodexCommandResolver.resolve

    def login(self, progress: Callable[[str], None] | None = None, use_device_auth_fallback: bool = True, timeout_seconds: float = 600) -> AccountRuntimeMaterial:
        temp_home = Path(tempfile.gettempdir()) / ("CodexQuotaViewerWindows-login-" + uuid.uuid4().hex)
        temp_codex_home = temp_home / ".codex"
        temp_codex_home.mkdir(parents=True, exist_ok=True)
        try:
            if progress:
                progress("Starting Codex browser login.")
            result = self._run_login(temp_home, temp_codex_home, False, timeout_seconds, progress)
            if result[0] != 0 and use_device_auth_fallback:
                if progress:
                    progress("Browser login failed. Starting Codex device login.")
                result = self._run_login(temp_home, temp_codex_home, True, timeout_seconds, progress)
            if result[0] != 0:
                raise RuntimeError(result[1] or "Codex login failed.")
            auth_path = temp_codex_home / "auth.json"
            if not auth_path.exists():
                raise FileNotFoundError(f"Codex login did not produce auth.json: {auth_path}")
            config_path = temp_codex_home / "config.toml"
            return AccountRuntimeMaterial(auth_path.read_bytes(), config_path.read_bytes() if config_path.exists() else None)
        finally:
            shutil.rmtree(temp_home, ignore_errors=True)

    def _run_login(self, temp_home: Path, temp_codex_home: Path, use_device_auth: bool, timeout_seconds: float, progress: Callable[[str], None] | None) -> tuple[int, str]:
        command = self.command_resolver()
        args = [command.file_name, *command.build_arguments(["login", "--device-auth"] if use_device_auth else ["login"])]
        env = os.environ.copy()
        env["HOME"] = str(temp_home)
        env["USERPROFILE"] = str(temp_home)
        env["CODEX_HOME"] = str(temp_codex_home)
        label = "Codex device login:" if use_device_auth else "Codex login:"
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output: queue.Queue[str | None] = queue.Queue()
            threading.Thread(target=self._pump_login_output, args=(process, output), daemon=True).start()
            diagnostics_parts: list[str] = []
            last_progress = 0.0
            deadline = time.monotonic() + timeout_seconds

            def emit_progress(force: bool = False) -> None:
                nonlocal last_progress
                if not progress:
                    return
                now = time.monotonic()
                if not force and now - last_progress < 0.25:
                    return
                diagnostics = "".join(diagnostics_parts).strip()
                if diagnostics:
                    progress(label + "\n" + diagnostics[-8000:])
                    last_progress = now

            stream_done = False
            while not stream_done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._try_kill_login(process)
                    diagnostics = "".join(diagnostics_parts).strip()
                    return -1, ("Codex login timed out.\n" + diagnostics).strip()
                try:
                    chunk = output.get(timeout=max(0.05, min(0.2, remaining)))
                except queue.Empty:
                    continue
                if chunk is None:
                    stream_done = True
                    break
                diagnostics_parts.append(chunk)
                emit_progress(chunk.endswith("\n"))

            try:
                return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._try_kill_login(process)
                diagnostics = "".join(diagnostics_parts).strip()
                return -1, ("Codex login timed out.\n" + diagnostics).strip()

            diagnostics = "".join(diagnostics_parts).strip()
            emit_progress(True)
            return return_code, diagnostics[-8000:]
        except subprocess.TimeoutExpired as ex:
            diagnostics = "\n".join(str(item or "") for item in [ex.stdout, ex.stderr] if item)
            return -1, ("Codex login timed out.\n" + diagnostics).strip()

    @staticmethod
    def _pump_login_output(process: subprocess.Popen[Any], output: queue.Queue[str | None]) -> None:
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    break
                output.put(chunk)
        finally:
            output.put(None)

    @staticmethod
    def _try_kill_login(process: subprocess.Popen[Any]) -> None:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        except Exception:
            pass


class BackupManager:
    MAX_SNAPSHOT_COUNT = 50
    MAX_SNAPSHOT_BYTES = 3 * 1024 * 1024 * 1024

    def __init__(
        self,
        backups_root: Path,
        target: CodexHomeTarget | None = None,
        default_kind: SnapshotKind = SnapshotKind.AUTOMATIC,
    ):
        self.backups_root = backups_root
        self.target = target
        self.default_kind = default_kind

    def create_restore_point(
        self,
        reason: str,
        summary: str,
        files: Iterable[Path],
        kind: SnapshotKind | None = None,
    ) -> RestorePointManifest:
        kind = kind or self.default_kind
        distinct = [
            Path(text)
            for text in sorted({str(path) for path in files if not self._is_sqlite_sidecar(path)}, key=str.lower)
        ]
        estimated_size = self._estimate_existing_size(distinct)
        if kind == SnapshotKind.MANUAL:
            status = self.retention_status(kind)
            if status.count + 1 > status.max_count or status.size_bytes + estimated_size > status.max_size_bytes:
                raise RuntimeError(
                    f"Manual {self._target_label()} snapshots are over the limit "
                    f"({status.count}/{status.max_count}, {self._format_bytes(status.size_bytes)}/"
                    f"{self._format_bytes(status.max_size_bytes)}). Delete old manual snapshots before creating a new one."
                )
        self.backups_root.mkdir(parents=True, exist_ok=True)
        restore_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        root = self._kind_root(kind) / restore_id
        files_root = root / "files"
        files_root.mkdir(parents=True, exist_ok=True)
        records: list[RestorePointFile] = []
        size_bytes = 0
        for source in distinct:
            source_text = str(source)
            backup_name = self._hash_path(source_text) + source.suffix
            backup_relative = str(Path("files") / backup_name)
            existed = source.exists()
            if existed:
                target = root / backup_relative
                self._copy_for_snapshot(source, target)
                size_bytes += target.stat().st_size if target.exists() else 0
            records.append(RestorePointFile(str(source), backup_relative, existed))
        manifest = RestorePointManifest(
            restore_id,
            reason,
            summary,
            now_utc(),
            records,
            self.target,
            kind,
            size_bytes,
            len(records),
        )
        _write_json(root / "manifest.json", manifest.to_json())
        if kind == SnapshotKind.AUTOMATIC:
            self.enforce_retention(kind)
        return manifest

    def list(self, kind: SnapshotKind | None = None) -> list[RestorePointManifest]:
        manifests: list[RestorePointManifest] = []
        for root, inferred_kind in self._snapshot_roots(kind):
            if not root.exists():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    manifest = self._read_manifest(child)
                except Exception:
                    continue
                if manifest.kind != inferred_kind or manifest.target != self.target:
                    manifest = RestorePointManifest(
                        manifest.id,
                        manifest.reason,
                        manifest.summary,
                        manifest.created_at,
                        manifest.files,
                        manifest.target or self.target,
                        inferred_kind,
                        manifest.size_bytes or self._snapshot_size(child),
                        manifest.file_count or len(manifest.files),
                    )
                manifests.append(manifest)
        return sorted(manifests, key=lambda item: (item.created_at, item.id), reverse=True)

    def latest(self, kind: SnapshotKind | None = SnapshotKind.AUTOMATIC) -> RestorePointManifest | None:
        snapshots = self.list(kind)
        return snapshots[0] if snapshots else None

    def restore_latest(self, kind: SnapshotKind | None = SnapshotKind.AUTOMATIC) -> RestorePointManifest:
        latest = self.latest(kind)
        if latest is None:
            raise RuntimeError("No restore point is available.")
        return self.restore(latest)

    def restore(self, manifest: RestorePointManifest) -> RestorePointManifest:
        root = self._manifest_root(manifest)
        for item in manifest.files:
            source = Path(item.source_path)
            if self._is_sqlite_sidecar(source):
                continue
            if not item.existed:
                if source.exists():
                    source.unlink()
                if source.name == "state_5.sqlite":
                    self._remove_sqlite_sidecars(source)
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.name == "state_5.sqlite":
                self._remove_sqlite_sidecars(source)
            shutil.copy2(root / item.backup_relative_path, source)
            if source.name == "state_5.sqlite":
                self._remove_sqlite_sidecars(source)
        return manifest

    def delete(self, snapshot_id: str, kind: SnapshotKind | None = None) -> RestorePointManifest:
        manifest = self._find(snapshot_id, kind)
        root = self._manifest_root(manifest)
        shutil.rmtree(root)
        return manifest

    def retention_status(self, kind: SnapshotKind) -> SnapshotRetentionStatus:
        snapshots = self.list(kind)
        return SnapshotRetentionStatus(
            self.target or CodexHomeTarget.SANDBOX,
            kind,
            len(snapshots),
            sum(item.size_bytes or self._snapshot_size(self._manifest_root(item)) for item in snapshots),
            self.MAX_SNAPSHOT_COUNT,
            self.MAX_SNAPSHOT_BYTES,
        )

    def enforce_retention(self, kind: SnapshotKind = SnapshotKind.AUTOMATIC) -> None:
        if kind != SnapshotKind.AUTOMATIC:
            return
        snapshots = sorted(self.list(kind), key=lambda item: (item.created_at, item.id))
        while snapshots:
            total_size = sum(item.size_bytes or self._snapshot_size(self._manifest_root(item)) for item in snapshots)
            if len(snapshots) <= self.MAX_SNAPSHOT_COUNT and total_size <= self.MAX_SNAPSHOT_BYTES:
                return
            victim = snapshots.pop(0)
            try:
                shutil.rmtree(self._manifest_root(victim))
            except OSError:
                return

    def _find(self, snapshot_id: str, kind: SnapshotKind | None = None) -> RestorePointManifest:
        for item in self.list(kind):
            if item.id == snapshot_id:
                return item
        raise FileNotFoundError(f"Snapshot was not found: {snapshot_id}")

    def _kind_root(self, kind: SnapshotKind) -> Path:
        return self.backups_root / kind.value

    def _snapshot_roots(self, kind: SnapshotKind | None) -> Iterable[tuple[Path, SnapshotKind]]:
        if kind is None:
            yield self._kind_root(SnapshotKind.AUTOMATIC), SnapshotKind.AUTOMATIC
            yield self._kind_root(SnapshotKind.MANUAL), SnapshotKind.MANUAL
            yield self.backups_root, SnapshotKind.AUTOMATIC
            return
        yield self._kind_root(kind), kind
        if kind == SnapshotKind.AUTOMATIC:
            yield self.backups_root, SnapshotKind.AUTOMATIC

    def _manifest_root(self, manifest: RestorePointManifest) -> Path:
        candidates = [
            self._kind_root(manifest.kind) / manifest.id,
            self.backups_root / manifest.id,
            self._kind_root(SnapshotKind.AUTOMATIC) / manifest.id,
            self._kind_root(SnapshotKind.MANUAL) / manifest.id,
        ]
        for candidate in candidates:
            if (candidate / "manifest.json").exists():
                return candidate
        return candidates[0]

    @staticmethod
    def _read_manifest(root: Path) -> RestorePointManifest:
        data = _read_json(root / "manifest.json", {})
        if not isinstance(data, dict):
            raise RuntimeError("Restore point manifest is invalid.")
        return RestorePointManifest.from_json(data)

    @staticmethod
    def _hash_path(path: str) -> str:
        return hashlib.sha256(path.upper().encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _estimate_existing_size(files: Iterable[Path]) -> int:
        total = 0
        for path in files:
            try:
                if path.exists() and path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _snapshot_size(root: Path) -> int:
        try:
            return sum(path.stat().st_size for path in (root / "files").rglob("*") if path.is_file())
        except OSError:
            return 0

    @staticmethod
    def _copy_for_snapshot(source: Path, target: Path) -> None:
        if source.name == "state_5.sqlite":
            try:
                if BackupManager._copy_sqlite_snapshot(source, target):
                    return
            except Exception:
                pass
        shutil.copy2(source, target)

    @staticmethod
    def _is_sqlite_sidecar(path: Path) -> bool:
        return Path(path).name in {"state_5.sqlite-wal", "state_5.sqlite-shm"}

    @staticmethod
    def _remove_sqlite_sidecars(database: Path) -> None:
        for sidecar in (database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
            sidecar.unlink(missing_ok=True)

    @staticmethod
    def _copy_sqlite_snapshot(source: Path, target: Path) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        source_conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        target_conn = sqlite3.connect(temp, timeout=5)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        temp.replace(target)
        return True

    @staticmethod
    def _format_bytes(value: int) -> str:
        if value >= 1024 * 1024 * 1024:
            return f"{value / (1024 * 1024 * 1024):.1f}GB"
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.1f}MB"
        return f"{value / 1024:.1f}KB"

    def _target_label(self) -> str:
        return self.target.value if self.target else "Codex"


class StateDatabaseRecoveryService:
    @staticmethod
    def integrity_issue(paths: AppPaths, target: CodexHomeTarget) -> str | None:
        database = paths.codex_home(target) / "state_5.sqlite"
        if not database.exists():
            return None
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            message = str(result[0] if result else "no result")
            return None if message.lower() == "ok" else f"{database} integrity check failed: {message}"
        except sqlite3.DatabaseError as ex:
            return f"{database} is malformed: {ex}"
        except sqlite3.Error as ex:
            return f"{database} could not be checked: {ex}"


class WritePreviewBuilder:
    SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]+|\"access_token\"\s*:\s*\"[^\"]+\")")

    @staticmethod
    def for_files(operation: str, target: CodexHomeTarget, target_home: Path, files: Iterable[Path], summary: str = "") -> WritePreview:
        distinct = [Path(text) for text in sorted({str(path) for path in files}, key=str.lower)]
        created = 0
        modified = 0
        size = 0
        samples: list[str] = []
        for path in distinct:
            exists = path.exists()
            created += 0 if exists else 1
            modified += 1 if exists else 0
            try:
                if exists and path.is_file():
                    size += path.stat().st_size
            except OSError:
                pass
            if len(samples) < 5:
                samples.append(WritePreviewBuilder._redacted_path(path))
        return WritePreview(
            operation,
            target,
            str(target_home),
            len(distinct),
            size,
            created,
            modified,
            0,
            tuple(WritePreviewBuilder._redact_text(item) for item in samples),
            (),
            WritePreviewBuilder._redact_text(summary),
        )

    @staticmethod
    def _redacted_path(path: Path) -> str:
        text = str(path)
        if path.name.lower() == "auth.json":
            return str(path.with_name("auth.json (redacted)"))
        return text

    @classmethod
    def _redact_text(cls, value: str) -> str:
        return cls.SECRET_RE.sub("[redacted]", value or "")


class SnapshotManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def manager_for(self, target: CodexHomeTarget) -> BackupManager:
        return BackupManager(self.paths.backups_root_for(target), target)

    def list_snapshots(self, target: CodexHomeTarget, kind: SnapshotKind | None = None) -> list[RestorePointManifest]:
        return self.manager_for(target).list(kind)

    def latest_automatic(self, target: CodexHomeTarget) -> RestorePointManifest | None:
        return self.manager_for(target).latest(SnapshotKind.AUTOMATIC)

    def retention_status(self, target: CodexHomeTarget, kind: SnapshotKind) -> SnapshotRetentionStatus:
        return self.manager_for(target).retention_status(kind)

    def create_automatic_snapshot(self, target: CodexHomeTarget, reason: str, summary: str, files: Iterable[Path]) -> RestorePointManifest:
        return self.manager_for(target).create_restore_point(reason, summary, files, SnapshotKind.AUTOMATIC)

    def create_manual_snapshot(self, target: CodexHomeTarget, summary: str = "Manual snapshot") -> RestorePointManifest:
        return self.manager_for(target).create_restore_point("manual-snapshot", summary, self.full_state_files(target), SnapshotKind.MANUAL)

    def restore_snapshot(self, target: CodexHomeTarget, snapshot_id: str) -> RestorePointManifest:
        manager = self.manager_for(target)
        manifest = manager._find(snapshot_id, None)
        return manager.restore(manifest)

    def delete_snapshot(self, target: CodexHomeTarget, snapshot_id: str) -> RestorePointManifest:
        return self.manager_for(target).delete(snapshot_id, None)

    def snapshot_folder(self, target: CodexHomeTarget) -> Path:
        return self.paths.backups_root_for(target)

    def full_state_files(self, target: CodexHomeTarget) -> list[Path]:
        files = [
            self.paths.auth_path(target),
            self.paths.config_path(target),
            self.paths.session_index_path(target),
            *self.paths.global_state_paths(target),
            self.paths.state_database_path(target),
        ]
        for root in self.paths.session_roots(target):
            if root.exists():
                files.extend(path for path in root.rglob("*.jsonl") if path.is_file())
        return files


class AuditLog:
    SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]+|\"access_token\"\s*:\s*\"[^\"]+\")")

    def __init__(self, path: Path):
        self.path = path

    def append(
        self,
        event_type: str,
        target: CodexHomeTarget | None,
        status: str,
        summary: str,
        snapshot_id: str | None = None,
        account_display_name: str | None = None,
        affected_file_count: int = 0,
        affected_bytes: int = 0,
        error_message: str | None = None,
    ) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "id": uuid.uuid4().hex,
                "timestamp": now_utc().isoformat().replace("+00:00", "Z"),
                "eventType": event_type,
                "target": target.value if target else None,
                "status": status,
                "snapshotId": snapshot_id,
                "accountDisplayName": self._redact(account_display_name or "") or None,
                "affectedFileCount": affected_file_count,
                "affectedBytes": affected_bytes,
                "summary": self._redact(summary),
                "error": self._redact(error_message or "") or None,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in reversed(lines[-max(limit * 4, limit):]):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
            if len(events) >= limit:
                break
        return events

    @classmethod
    def _redact(cls, value: str) -> str:
        return cls.SECRET_RE.sub("[redacted]", value or "")


class SandboxSeeder:
    def seed(self, paths: AppPaths, max_session_files_per_root: int | None = None) -> SandboxSeedResult:
        paths.ensure_created()
        if not paths.real_codex_home.exists():
            raise FileNotFoundError(f"Real Codex home was not found: {paths.real_codex_home}")
        copied = 0
        warnings: list[str] = []
        skipped = 0
        required_files = {"auth.json"}
        names = ["auth.json", "config.toml", "session_index.jsonl", ".codex-global-state.json", ".codex-global-state.json.bak"]
        for name in names:
            result = self._copy_if_exists(
                paths.real_codex_home / name,
                paths.sandbox_codex_home / name,
                required=name in required_files,
            )
            if result < 0:
                skipped += 1
                warnings.append(f"Skipped locked or busy file: {name}")
            else:
                copied += result
        result = self._copy_sqlite_database_if_exists(paths.real_codex_home / "state_5.sqlite", paths.sandbox_codex_home / "state_5.sqlite")
        if result < 0:
            skipped += 1
            warnings.append("Skipped unreadable or busy file: state_5.sqlite")
        else:
            copied += result
        tree_result = self._copy_recent_tree(paths.real_codex_home / "sessions", paths.sandbox_codex_home / "sessions", max_session_files_per_root, warnings)
        copied += tree_result[0]
        skipped += tree_result[1]
        tree_result = self._copy_recent_tree(
            paths.real_codex_home / "archived_sessions",
            paths.sandbox_codex_home / "archived_sessions",
            max_session_files_per_root,
            warnings,
        )
        copied += tree_result[0]
        skipped += tree_result[1]
        for name in SharedAssetRules.ROOT_NAMES:
            tree_result = self._copy_asset_tree(paths.real_codex_home / name, paths.sandbox_codex_home / name, warnings)
            copied += tree_result[0]
            skipped += tree_result[1]
        return SandboxSeedResult(copied, str(paths.sandbox_codex_home), skipped, tuple(warnings[:8]))

    def _copy_recent_tree(self, source_root: Path, target_root: Path, max_files: int | None, warnings: list[str]) -> tuple[int, int]:
        if not source_root.exists():
            return (0, 0)
        files = sorted(source_root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        if max_files is not None and max_files > 0:
            files = files[:max_files]
        copied = 0
        skipped = 0
        for source in files:
            result = self._copy_if_exists(source, target_root / source.relative_to(source_root), required=False)
            if result < 0:
                skipped += 1
                warnings.append(f"Skipped locked or busy session: {source.relative_to(source_root)}")
            else:
                copied += result
        return (copied, skipped)

    def _copy_asset_tree(self, source_root: Path, target_root: Path, warnings: list[str]) -> tuple[int, int]:
        if not source_root.exists():
            return (0, 0)
        copied = 0
        skipped = 0
        files: list[Path] = []
        for current_root, dir_names, file_names in os.walk(source_root):
            dir_names[:] = [name for name in dir_names if name.lower() not in SharedAssetRules.IGNORED_DIR_NAMES]
            root = Path(current_root)
            files.extend(root / name for name in file_names)
        files.sort(key=lambda item: str(item).lower())
        for source in files:
            if SharedAssetRules.should_skip(source, source_root):
                skipped += 1
                warnings.append(f"Skipped non-shared asset file: {source.relative_to(source_root)}")
                continue
            result = self._copy_if_exists(source, target_root / source.relative_to(source_root), required=False)
            if result < 0:
                skipped += 1
                warnings.append(f"Skipped locked or busy asset: {source.relative_to(source_root)}")
            else:
                copied += result
        return (copied, skipped)

    @staticmethod
    def _copy_if_exists(source: Path, target: Path, required: bool) -> int:
        if not source.exists():
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
        except OSError as ex:
            if required or not SandboxSeeder._is_recoverable_copy_error(ex):
                raise
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return -1
        return 1

    @staticmethod
    def _copy_sqlite_database_if_exists(source: Path, target: Path) -> int:
        if not source.exists():
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        for path in [temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            source_uri = source.resolve().as_uri() + "?mode=ro"
            source_conn = sqlite3.connect(source_uri, uri=True, timeout=5)
            target_conn = sqlite3.connect(temp, timeout=5)
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
                source_conn.close()
            for path in [target, Path(str(target) + "-wal"), Path(str(target) + "-shm")]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            temp.replace(target)
        except (OSError, sqlite3.Error):
            for path in [temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return -1
        return 1

    @staticmethod
    def _is_recoverable_copy_error(ex: OSError) -> bool:
        winerror = getattr(ex, "winerror", None)
        if winerror in {32, 33, 1224}:
            return True
        return ex.errno in {13}


class SessionMetaSynchronizer:
    def sync_providers(self, roots: Iterable[Path], target_provider: str) -> int:
        updated = 0
        for root in roots:
            if not root.exists():
                continue
            for file in root.rglob("*.jsonl"):
                if self._update_file_if_needed(file, target_provider):
                    updated += 1
        return updated

    @staticmethod
    def _update_file_if_needed(file: Path, target_provider: str) -> bool:
        content = file.read_text(encoding="utf-8", errors="replace")
        newline = content.find("\n")
        first_line = content[:newline].rstrip("\r") if newline >= 0 else content
        if not first_line.strip():
            return False
        try:
            root = json.loads(first_line)
        except json.JSONDecodeError:
            return False
        payload = root.get("payload") if isinstance(root, dict) else None
        if root.get("type") != "session_meta" or not isinstance(payload, dict):
            return False
        if payload.get("model_provider") == target_provider:
            return False
        payload["model_provider"] = target_provider
        next_first_line = json.dumps(root, separators=(",", ":"), ensure_ascii=False)
        next_content = next_first_line + content[newline:] if newline >= 0 else next_first_line
        file.write_text(next_content, encoding="utf-8")
        return True


def _safe_session_jsonl_file(root: Path, file: Path) -> Path | None:
    try:
        safe_file = ensure_inside_realpath(root, file)
        return safe_file if safe_file.is_file() else None
    except (OSError, SessionError):
        return None


def _safe_session_jsonl_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for file in root.rglob("*.jsonl"):
        safe_file = _safe_session_jsonl_file(root, file)
        if safe_file is not None:
            files.append(safe_file)
    return files


class ProviderVisibilitySynchronizer:
    def sync(self, paths: AppPaths, target: CodexHomeTarget, target_provider: str) -> ProviderVisibilitySyncSummary:
        changed = 0
        skipped = 0
        user_event_thread_ids: set[str] = set()
        thread_cwds_by_id: dict[str, str] = {}

        for root in paths.session_roots(target):
            if not root.exists():
                continue
            for file in root.rglob("*.jsonl"):
                try:
                    safe_file = _safe_session_jsonl_file(root, file)
                    if safe_file is None:
                        skipped += 1
                        continue
                    result = self._sync_session_file(safe_file, target_provider)
                except OSError:
                    skipped += 1
                    continue
                if result["changed"]:
                    changed += 1
                thread_id = result.get("thread_id")
                cwd = result.get("cwd")
                if isinstance(thread_id, str) and isinstance(cwd, str) and thread_id and cwd:
                    thread_cwds_by_id[thread_id] = self._to_desktop_workspace_path(cwd)
                if result["has_user_event"] and isinstance(thread_id, str) and thread_id:
                    user_event_thread_ids.add(thread_id)

        sqlite_result = self._sync_sqlite(
            paths.codex_home(target),
            target_provider,
            user_event_thread_ids,
            thread_cwds_by_id,
        )
        return ProviderVisibilitySyncSummary(
            changed,
            skipped,
            sqlite_result["updated_rows"],
            sqlite_result["provider_rows"],
            sqlite_result["user_event_rows"],
            sqlite_result["cwd_rows"],
            sqlite_result["present"],
        )

    def _sync_session_file(self, file: Path, target_provider: str) -> dict[str, Any]:
        original_mtime = file.stat().st_mtime
        content = file.read_text(encoding="utf-8", errors="replace")
        newline = content.find("\n")
        first_line = content[:newline].rstrip("\r") if newline >= 0 else content
        separator = "\r\n" if newline > 0 and content[newline - 1] == "\r" else "\n" if newline >= 0 else ""
        root: dict[str, Any] | None = None
        payload: dict[str, Any] | None = None
        try:
            parsed = json.loads(first_line)
            if isinstance(parsed, dict) and parsed.get("type") == "session_meta" and isinstance(parsed.get("payload"), dict):
                root = parsed
                payload = parsed["payload"]
        except json.JSONDecodeError:
            pass
        if not root or not payload:
            return {"changed": False, "has_user_event": self._content_has_user_event(content)}

        changed = False
        if payload.get("model_provider") != target_provider:
            payload["model_provider"] = target_provider
            next_first_line = json.dumps(root, separators=(",", ":"), ensure_ascii=False)
            rest = content[newline + 1 :] if newline >= 0 else ""
            file.write_text(next_first_line + (separator + rest if separator else ""), encoding="utf-8")
            os.utime(file, (original_mtime, original_mtime))
            changed = True

        return {
            "changed": changed,
            "thread_id": payload.get("id"),
            "cwd": payload.get("cwd"),
            "has_user_event": self._content_has_user_event(content),
        }

    def _sync_sqlite(
        self,
        codex_home: Path,
        target_provider: str,
        user_event_thread_ids: set[str],
        thread_cwds_by_id: dict[str, str],
    ) -> dict[str, Any]:
        database = codex_home / "state_5.sqlite"
        if not database.exists():
            return {"updated_rows": 0, "provider_rows": 0, "user_event_rows": 0, "cwd_rows": 0, "present": False}
        try:
            conn = sqlite3.connect(database, timeout=5)
            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("BEGIN IMMEDIATE")
                provider_rows = 0
                if self._sqlite_has_column(conn, "threads", "model_provider"):
                    provider_rows = conn.execute(
                        "UPDATE threads SET model_provider = ? WHERE COALESCE(model_provider, '') <> ?",
                        (target_provider, target_provider),
                    ).rowcount
                user_event_rows = 0
                if user_event_thread_ids and self._sqlite_has_column(conn, "threads", "has_user_event"):
                    for thread_id in sorted(user_event_thread_ids):
                        user_event_rows += conn.execute(
                            "UPDATE threads SET has_user_event = 1 WHERE id = ? AND COALESCE(has_user_event, 0) <> 1",
                            (thread_id,),
                        ).rowcount
                cwd_rows = 0
                if thread_cwds_by_id and self._sqlite_has_column(conn, "threads", "cwd"):
                    for thread_id, cwd in sorted(thread_cwds_by_id.items()):
                        cwd_rows += conn.execute(
                            "UPDATE threads SET cwd = ? WHERE id = ? AND COALESCE(cwd, '') <> ?",
                            (cwd, thread_id, cwd),
                        ).rowcount
                conn.commit()
                return {
                    "updated_rows": provider_rows + user_event_rows + cwd_rows,
                    "provider_rows": provider_rows,
                    "user_event_rows": user_event_rows,
                    "cwd_rows": cwd_rows,
                    "present": True,
                }
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        except sqlite3.OperationalError as ex:
            message = str(ex).lower()
            if "locked" in message or "busy" in message:
                raise RuntimeError(
                    "Unable to update session provider metadata because state_5.sqlite is currently in use. "
                    "Close Codex and the Codex app, then retry."
                ) from ex
            raise
        except sqlite3.DatabaseError as ex:
            raise RuntimeError(
                f"Unable to update session provider metadata because state_5.sqlite is malformed or unreadable. "
                f"Close Codex, restore a healthy state database, then retry. Original error: {ex}"
            ) from ex

    @staticmethod
    def _sqlite_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
        return any(row[1] == column_name for row in conn.execute(f'PRAGMA table_info("{table_name}")'))

    @staticmethod
    def _content_has_user_event(content: str) -> bool:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                root = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(root, dict):
                continue
            payload = root.get("payload")
            if root.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
                return True
            for key in ("payload", "item", "msg"):
                value = root.get(key)
                if isinstance(value, dict) and value.get("type") == "message" and value.get("role") == "user":
                    return True
        return False

    @staticmethod
    def _to_desktop_workspace_path(value: str) -> str:
        text = value.strip()
        if not text:
            return value
        if text.lower().startswith("\\\\?\\unc\\"):
            return ("\\\\" + text[8:]).replace("/", "\\")
        if text.startswith("\\\\?\\"):
            normalized = text[4:].replace("/", "\\")
            return normalized + "\\" if re.match(r"^[A-Za-z]:$", normalized) else normalized
        return value


class SandboxRealSessionSyncService:
    def __init__(self, paths: AppPaths, provider_visibility_synchronizer: ProviderVisibilitySynchronizer | None = None):
        self.paths = paths
        self.provider_visibility_synchronizer = provider_visibility_synchronizer or ProviderVisibilitySynchronizer()

    def preview(self, sync_sessions: bool = True, sync_assets: bool = True) -> WritePreview:
        if not sync_sessions and not sync_assets:
            raise ValueError("Select at least one Sandbox to Real sync option.")
        session_plan = self._build_copy_plan() if sync_sessions else []
        asset_plan = self._build_asset_copy_plan() if sync_assets else []
        session_files = [item["dest"] for item in session_plan if item["action"] in {"copy", "overwrite"}]
        asset_files = [item["dest"] for item in asset_plan if item["action"] in {"copy", "overwrite"}]
        files = [*session_files, *asset_files]
        if sync_sessions and self.paths.session_index_path(CodexHomeTarget.SANDBOX).exists():
            files.append(self.paths.session_index_path(CodexHomeTarget.REAL))
        if session_files:
            files.append(self.paths.state_database_path(CodexHomeTarget.REAL))
        created = sum(1 for item in [*session_plan, *asset_plan] if item["action"] == "copy")
        modified = sum(1 for item in [*session_plan, *asset_plan] if item["action"] == "overwrite")
        modified += 1 if sync_sessions and self.paths.session_index_path(CodexHomeTarget.SANDBOX).exists() else 0
        modified += 1 if session_files else 0
        size = sum(int(item.get("size") or 0) for item in [*session_plan, *asset_plan] if item["action"] in {"copy", "overwrite"})
        samples = tuple(str(item["dest"]) for item in [*session_plan, *asset_plan] if item["action"] in {"copy", "overwrite"})[:5]
        sections = []
        if sync_sessions:
            sections.append("sessions, session_index.jsonl, and state_5.sqlite thread metadata")
        if sync_assets:
            sections.append(f"shared asset directories ({', '.join(SharedAssetRules.ROOT_NAMES)})")
        return WritePreview(
            "Sync Sandbox to Real",
            CodexHomeTarget.REAL,
            str(self.paths.real_codex_home),
            len(files),
            size,
            created,
            modified,
            0,
            samples,
            (),
            f"{'; '.join(sections)} may be changed. Auth and config are not copied.",
        )

    def sync(
        self,
        active_provider: str,
        progress: Callable[[str], None] | None = None,
        sync_sessions: bool = True,
        sync_assets: bool = True,
    ) -> SandboxRealSessionSyncResult:
        if not sync_sessions and not sync_assets:
            raise ValueError("Select at least one Sandbox to Real sync option.")
        if sync_sessions and progress:
            progress("Sync progress: checking Real state database before writing...")
        if sync_sessions:
            self._preflight_real_state_database()
        if progress:
            progress(f"Sync progress: scanning sandbox and real {self._sections_label(sync_sessions, sync_assets)}...")
        plan = self._build_copy_plan() if sync_sessions else []
        asset_plan = self._build_asset_copy_plan() if sync_assets else []
        write_items = [item for item in plan if item["action"] in {"copy", "overwrite"}]
        write_asset_items = [item for item in asset_plan if item["action"] in {"copy", "overwrite"}]
        touched = [item["dest"] for item in write_items]
        if sync_sessions and self.paths.session_index_path(CodexHomeTarget.SANDBOX).exists():
            touched.append(self.paths.session_index_path(CodexHomeTarget.REAL))
        if sync_sessions and write_items:
            touched.append(self.paths.state_database_path(CodexHomeTarget.REAL))
        touched.extend(item["dest"] for item in write_asset_items)
        backup_manager = BackupManager(self.paths.backups_root_for(CodexHomeTarget.REAL), CodexHomeTarget.REAL)
        if progress:
            progress(f"Sync progress: creating Real restore point for {len(touched)} planned writes...")
        restore_point = backup_manager.create_restore_point(
            "sync-sandbox-to-real",
            f"Sync sandbox {self._sections_label(sync_sessions, sync_assets)} to real Codex home",
            touched,
            SnapshotKind.AUTOMATIC,
        )
        try:
            copied = 0
            overwritten = 0
            asset_copied = 0
            asset_overwritten = 0
            imported_files: list[Path] = []
            if sync_sessions and progress:
                progress("Sync progress: copying newer sandbox session files into Real...")
            for item in write_items:
                source = item["source"]
                dest = item["dest"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                imported_files.append(dest)
                if item["action"] == "copy":
                    copied += 1
                else:
                    overwritten += 1
            if sync_assets and progress:
                progress("Sync progress: copying shared sandbox assets into Real...")
            for item in write_asset_items:
                source = item["source"]
                dest = item["dest"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                if item["action"] == "copy":
                    asset_copied += 1
                else:
                    asset_overwritten += 1
            if sync_sessions and progress:
                progress("Sync progress: merging session_index.jsonl without duplicates...")
            index_merged = self._merge_session_index() if sync_sessions else 0
            if sync_sessions and progress:
                progress("Sync progress: syncing copied sessions to the active Real provider...")
            changed_session_files, thread_rows = (
                self._sync_imported_files(imported_files, active_provider) if sync_sessions else (0, [])
            )
            if sync_sessions and progress:
                progress("Sync progress: updating Real state_5.sqlite thread rows...")
            inserted, updated = self._upsert_threads(thread_rows, active_provider) if sync_sessions else (0, 0)
            provider_sync = ProviderVisibilitySyncSummary(
                changed_session_files,
                0,
                inserted + updated,
                inserted + updated,
                sum(1 for item in thread_rows if item.get("has_user_event")),
                sum(1 for item in thread_rows if item.get("cwd")),
                self.paths.state_database_path(CodexHomeTarget.REAL).exists(),
            )
            skipped_same = sum(1 for item in plan if item["action"] == "skip-same")
            skipped_real_newer = sum(1 for item in plan if item["action"] == "skip-real-newer")
            asset_skipped_same = sum(1 for item in asset_plan if item["action"] == "skip-same")
            asset_skipped_real_newer = sum(1 for item in asset_plan if item["action"] == "skip-real-newer")
            return SandboxRealSessionSyncResult(
                restore_point,
                len(plan),
                copied,
                overwritten,
                skipped_same,
                skipped_real_newer,
                index_merged,
                inserted,
                updated,
                provider_sync,
                len(asset_plan),
                asset_copied,
                asset_overwritten,
                asset_skipped_same,
                asset_skipped_real_newer,
            )
        except Exception as ex:
            try:
                backup_manager.restore(restore_point)
            except Exception as restore_ex:
                raise RuntimeError(f"Sandbox to Real sync failed: {ex}. Rollback also failed: {restore_ex}") from ex
            raise RuntimeError(f"Sandbox to Real sync failed: {ex}. Rolled back to snapshot {restore_point.id}.") from ex

    @staticmethod
    def _sections_label(sync_sessions: bool, sync_assets: bool) -> str:
        if sync_sessions and sync_assets:
            return "sessions/assets"
        if sync_sessions:
            return "sessions"
        if sync_assets:
            return "assets"
        return "nothing"

    def _preflight_real_state_database(self) -> None:
        issue = StateDatabaseRecoveryService.integrity_issue(self.paths, CodexHomeTarget.REAL)
        if issue:
            raise RuntimeError(f"Real state database is unhealthy; sync aborted before writing. {issue}")
        database = self.paths.state_database_path(CodexHomeTarget.REAL)
        if not database.exists():
            return
        try:
            with sqlite3.connect(database, timeout=2) as conn:
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
        except sqlite3.OperationalError as ex:
            message = str(ex).lower()
            if "locked" in message or "busy" in message:
                raise RuntimeError("Real state_5.sqlite is in use. Close Codex Desktop and retry.") from ex
            raise
        except sqlite3.DatabaseError as ex:
            raise RuntimeError(f"Real state_5.sqlite is malformed or unreadable: {ex}") from ex

    def _build_copy_plan(self) -> list[dict[str, Any]]:
        real_by_thread_id = self._real_session_paths_by_thread_id()
        plan: list[dict[str, Any]] = []
        sandbox_home = self.paths.codex_home(CodexHomeTarget.SANDBOX)
        real_home = self.paths.codex_home(CodexHomeTarget.REAL)
        for root_name in ("sessions", "archived_sessions"):
            source_root = sandbox_home / root_name
            if not source_root.exists():
                continue
            for source in sorted(source_root.rglob("*.jsonl"), key=lambda path: str(path).lower()):
                rel = source.relative_to(sandbox_home)
                meta = self._session_metadata(source)
                thread_id = meta.get("id")
                dest = real_home / rel
                if not dest.exists() and isinstance(thread_id, str) and thread_id:
                    dest = real_by_thread_id.get(thread_id, dest)
                action = self._copy_action(source, dest)
                plan.append({"source": source, "dest": dest, "action": action, "thread_id": thread_id, "size": source.stat().st_size})
        return plan

    def _build_asset_copy_plan(self) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        sandbox_home = self.paths.codex_home(CodexHomeTarget.SANDBOX)
        real_home = self.paths.codex_home(CodexHomeTarget.REAL)
        for root_name in SharedAssetRules.ROOT_NAMES:
            source_root = sandbox_home / root_name
            if not source_root.exists():
                continue
            files: list[Path] = []
            for current_root, dir_names, file_names in os.walk(source_root):
                dir_names[:] = [name for name in dir_names if name.lower() not in SharedAssetRules.IGNORED_DIR_NAMES]
                root = Path(current_root)
                files.extend(root / name for name in file_names)
            for source in sorted(files, key=lambda path: str(path).lower()):
                if SharedAssetRules.should_skip(source, source_root):
                    continue
                rel = source.relative_to(sandbox_home)
                dest = real_home / rel
                action = self._copy_action(source, dest)
                plan.append({"source": source, "dest": dest, "action": action, "size": source.stat().st_size, "asset": True})
        return plan

    def _copy_action(self, source: Path, dest: Path) -> str:
        if not dest.exists():
            return "copy"
        try:
            if self._hash_file(source) == self._hash_file(dest):
                return "skip-same"
            return "overwrite" if source.stat().st_mtime > dest.stat().st_mtime else "skip-real-newer"
        except OSError:
            return "overwrite"

    def _real_session_paths_by_thread_id(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for root in self.paths.session_roots(CodexHomeTarget.REAL):
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                thread_id = self._session_metadata(path).get("id")
                if isinstance(thread_id, str) and thread_id:
                    result.setdefault(thread_id, path)
        return result

    def _sync_imported_files(self, files: list[Path], provider: str) -> tuple[int, list[dict[str, Any]]]:
        changed = 0
        rows: list[dict[str, Any]] = []
        for file in files:
            try:
                before = self._session_metadata(file)
                result = self.provider_visibility_synchronizer._sync_session_file(file, provider)
                if result.get("changed"):
                    changed += 1
                after = self._session_metadata(file)
                row = {**before, **after}
                row["model_provider"] = provider
                row["archived"] = 1 if "archived_sessions" in file.parts else 0
                rows.append(row)
            except OSError:
                continue
        return changed, rows

    def _merge_session_index(self) -> int:
        source = self.paths.session_index_path(CodexHomeTarget.SANDBOX)
        dest = self.paths.session_index_path(CodexHomeTarget.REAL)
        if not source.exists():
            return 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        existing_unknown: list[str] = []
        existing_by_id: dict[str, tuple[str, int]] = {}
        for line in self._read_jsonl_lines(dest):
            thread_id = self._jsonl_thread_id(line)
            if not thread_id:
                existing_unknown.append(line)
                continue
            existing_by_id[thread_id] = (line, self._jsonl_timestamp(line))
        changed = 0
        for line in self._read_jsonl_lines(source):
            thread_id = self._jsonl_thread_id(line)
            if not thread_id:
                if line not in existing_unknown:
                    existing_unknown.append(line)
                    changed += 1
                continue
            timestamp = self._jsonl_timestamp(line)
            current = existing_by_id.get(thread_id)
            if current is None or timestamp >= current[1]:
                if current is None or current[0] != line:
                    changed += 1
                existing_by_id[thread_id] = (line, timestamp)
        if changed:
            lines = [*existing_unknown, *[value[0] for _, value in sorted(existing_by_id.items())]]
            dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return changed

    def _upsert_threads(self, rows: list[dict[str, Any]], provider: str) -> tuple[int, int]:
        database = self.paths.state_database_path(CodexHomeTarget.REAL)
        if not database.exists() or not rows:
            return (0, 0)
        inserted = 0
        updated = 0
        with sqlite3.connect(database, timeout=5) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            columns = self._sqlite_columns(conn, "threads")
            if "id" not in columns:
                return (0, 0)
            for row in rows:
                thread_id = row.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                values = self._thread_values(row, provider, columns)
                exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone() is not None
                if exists:
                    assignments = [(key, value) for key, value in values.items() if key != "id" and key in columns]
                    if assignments:
                        conn.execute(
                            "UPDATE threads SET " + ", ".join(f'"{key}" = ?' for key, _ in assignments) + " WHERE id = ?",
                            [value for _, value in assignments] + [thread_id],
                        )
                    updated += 1
                else:
                    insert_values = {key: value for key, value in values.items() if key in columns}
                    if not self._can_insert_thread(columns, insert_values):
                        continue
                    conn.execute(
                        "INSERT INTO threads (" + ", ".join(f'"{key}"' for key in insert_values) + ") VALUES ("
                        + ", ".join("?" for _ in insert_values) + ")",
                        list(insert_values.values()),
                    )
                    inserted += 1
            conn.commit()
        return (inserted, updated)

    @staticmethod
    def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> dict[str, dict[str, Any]]:
        rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        return {
            row[1]: {
                "notnull": bool(row[3]),
                "default": row[4],
                "pk": bool(row[5]),
            }
            for row in rows
        }

    @staticmethod
    def _thread_values(row: dict[str, Any], provider: str, columns: dict[str, dict[str, Any]]) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        values = {
            "id": row.get("id"),
            "model_provider": provider,
            "cwd": ProviderVisibilitySynchronizer._to_desktop_workspace_path(str(row.get("cwd") or "")),
            "archived": int(row.get("archived") or 0),
            "has_user_event": 1 if row.get("has_user_event") else 0,
            "source": "sandbox-sync",
            "first_user_message": row.get("first_user_message") or "",
            "updated_at_ms": int(row.get("updated_at_ms") or now_ms),
            "updated_at": int(row.get("updated_at") or now_ms),
        }
        return {key: value for key, value in values.items() if key in columns}

    @staticmethod
    def _can_insert_thread(columns: dict[str, dict[str, Any]], values: dict[str, Any]) -> bool:
        for name, info in columns.items():
            if name in values:
                continue
            if info["pk"] or not info["notnull"] or info["default"] is not None:
                continue
            return False
        return True

    @staticmethod
    def _session_metadata(file: Path) -> dict[str, Any]:
        try:
            content = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        lines = content.splitlines()
        first = lines[0] if lines else ""
        meta: dict[str, Any] = {}
        try:
            root = json.loads(first)
        except json.JSONDecodeError:
            root = {}
        payload = root.get("payload") if isinstance(root, dict) else None
        if isinstance(payload, dict):
            for key in ("id", "model_provider", "cwd", "updated_at_ms", "updated_at"):
                if key in payload:
                    meta[key] = payload[key]
        if "id" not in meta:
            found = SandboxRealSessionSyncService._find_json_key(root, {"id", "thread_id", "session_id"})
            if isinstance(found, str):
                meta["id"] = found
        meta["has_user_event"] = ProviderVisibilitySynchronizer._content_has_user_event(content)
        first_user = SandboxRealSessionSyncService._first_user_message(lines)
        if first_user:
            meta["first_user_message"] = first_user[:400]
        return meta

    @staticmethod
    def _first_user_message(lines: list[str]) -> str | None:
        for line in lines:
            try:
                root = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = root.get("payload") if isinstance(root, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "user_message":
                text = payload.get("message") or payload.get("text")
                if isinstance(text, str):
                    return text
            value = SandboxRealSessionSyncService._find_json_key(root, {"text", "content"})
            if isinstance(value, str) and "user" in json.dumps(root, ensure_ascii=False).lower():
                return value
        return None

    @staticmethod
    def _find_json_key(node: Any, keys: set[str]) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) in keys:
                    return value
            for value in node.values():
                found = SandboxRealSessionSyncService._find_json_key(value, keys)
                if found is not None:
                    return found
        if isinstance(node, list):
            for item in node:
                found = SandboxRealSessionSyncService._find_json_key(item, keys)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _read_jsonl_lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        except OSError:
            return []

    @staticmethod
    def _jsonl_thread_id(line: str) -> str | None:
        try:
            root = json.loads(line)
        except json.JSONDecodeError:
            return None
        value = SandboxRealSessionSyncService._find_json_key(root, {"id", "thread_id", "session_id"})
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _jsonl_timestamp(line: str) -> int:
        try:
            root = json.loads(line)
        except json.JSONDecodeError:
            return 0
        value = SandboxRealSessionSyncService._find_json_key(root, {"updated_at_ms", "updatedAtMs", "updated_at", "updatedAt"})
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _hash_file(path: Path) -> str:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha.update(chunk)
        return sha.hexdigest()


class WindowsCodexDesktopController:
    WM_CLOSE = 0x0010

    def __init__(self):
        self._restart_path: str | None = None

    def prepare_for_real_switch(self, timeout_seconds: float = 8) -> CodexDesktopSwitchPreparation:
        candidates = self._find_candidates()
        if not candidates:
            return CodexDesktopSwitchPreparation(True, False, False, "Codex Desktop is not running.")
        any_close_sent = False
        for process, hwnds in candidates:
            try:
                exe = process.exe()
                if exe and not self._restart_path:
                    self._restart_path = exe
            except Exception:
                pass
            for hwnd in hwnds:
                try:
                    ctypes.windll.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
                    any_close_sent = True
                except Exception:
                    pass
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if all(not self._is_running(process) for process, _ in candidates):
                return CodexDesktopSwitchPreparation(True, True, any_close_sent, "Codex Desktop was closed.")
            time.sleep(0.25)
        return CodexDesktopSwitchPreparation(
            False,
            True,
            any_close_sent,
            "Please close Codex Desktop manually and retry. Still running: " + self._blocking_process_summary(candidates),
        )

    def try_restart(self) -> str:
        if not self._restart_path:
            return "Codex Desktop restart was skipped; original executable was not found."
        try:
            subprocess.Popen([self._restart_path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "Codex Desktop restart was requested."
        except Exception as ex:
            return f"Codex Desktop restart failed: {ex}"

    def running_process_summary(self) -> str | None:
        candidates = self._find_candidates()
        if not candidates:
            return None
        return self._blocking_process_summary(candidates)

    def _find_candidates(self) -> list[tuple[Any, list[int]]]:
        try:
            import psutil  # type: ignore
        except Exception:
            return []
        own_pid = os.getpid()
        candidates: list[tuple[Any, list[int]]] = []
        titles = self._window_titles_by_pid()
        for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                if process.pid == own_pid:
                    continue
                name = (process.info.get("name") or "").lower()
                exe = (process.info.get("exe") or "").lower()
                cmdline = " ".join(process.info.get("cmdline") or []).lower()
                if "codexquotaviewer" in name or "codexquotaviewer" in exe or "codexquotaviewer" in cmdline:
                    continue
                if self._is_viewer_quota_app_server(cmdline):
                    continue
                process_titles = titles.get(process.pid, [])
                joined_titles = " ".join(title for _, title in process_titles).lower()
                if self._is_codex_electron_child(exe, cmdline):
                    continue
                is_candidate = (
                    ("windowsapps\\openai.codex_" in exe)
                    or ("codex" in name and "quota" not in name)
                    or ("codex" in joined_titles and "quota" not in joined_titles)
                )
                hwnds = [hwnd for hwnd, _ in process_titles]
                if is_candidate:
                    candidates.append((process, hwnds))
            except Exception:
                continue
        return candidates

    @staticmethod
    def _is_viewer_quota_app_server(cmdline: str) -> bool:
        padded = f" {cmdline} "
        return " app-server " in padded and " -s " in padded and " read-only " in padded and " -a " in padded and " untrusted " in padded

    @staticmethod
    def _is_codex_electron_child(exe: str, cmdline: str) -> bool:
        return "windowsapps\\openai.codex_" in exe and " --type=" in f" {cmdline}"

    def _blocking_process_summary(self, candidates: list[tuple[Any, list[int]]]) -> str:
        pieces: list[str] = []
        for process, hwnds in candidates[:5]:
            try:
                name = process.name()
            except Exception:
                name = getattr(process, "info", {}).get("name") or "Codex"
            try:
                cmdline = " ".join(process.cmdline()).lower()
            except Exception:
                cmdline = " ".join(getattr(process, "info", {}).get("cmdline") or []).lower()
            label = "background app-server" if " app-server" in f" {cmdline}" else "desktop window" if hwnds else "background process"
            pieces.append(f"{name} ({label})")
        if len(candidates) > len(pieces):
            pieces.append(f"+{len(candidates) - len(pieces)} more")
        return ", ".join(pieces) if pieces else "unknown Codex process"

    @staticmethod
    def _is_running(process: Any) -> bool:
        try:
            return process.is_running() and process.status() != "zombie"
        except Exception:
            return False

    @staticmethod
    def _window_titles_by_pid() -> dict[int, list[tuple[int, str]]]:
        titles: dict[int, list[tuple[int, str]]] = {}
        if os.name != "nt":
            return titles
        user32 = ctypes.windll.user32

        def callback(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            titles.setdefault(pid.value, []).append((hwnd, buffer.value))
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(callback)
        user32.EnumWindows(enum_proc, 0)
        return titles


class CodexDesktopAppLauncher:
    APP_COMMAND_ENV = "CQV_CODEX_APP_COMMAND"

    def __init__(self, desktop_controller: WindowsCodexDesktopController | None = None):
        self.desktop_controller = desktop_controller or WindowsCodexDesktopController()

    def launch(self, paths: AppPaths, target: CodexHomeTarget) -> str:
        codex_home = paths.codex_home(target)
        codex_home.mkdir(parents=True, exist_ok=True)
        running = self.desktop_controller.running_process_summary()
        if running:
            if target == CodexHomeTarget.SANDBOX:
                raise RuntimeError(
                    "Close the existing Codex Desktop window before opening sandbox Codex. "
                    "Codex Desktop is single-instance, so an already-running real window may ignore the sandbox environment. "
                    f"Still running: {running}"
                )
            return f"Codex Desktop already appears to be running: {running}"
        executable = self.resolve_executable()
        env = self._launch_environment(paths, target)
        try:
            subprocess.Popen(
                [str(executable)],
                cwd=str(codex_home.parent),
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as ex:
            raise RuntimeError(f"Could not start Codex Desktop at {executable}: {ex}") from ex
        return f"Started Codex Desktop for {target.value} with CODEX_HOME={codex_home}. Windows app data paths were left unchanged."

    @staticmethod
    def resolve_executable() -> Path:
        explicit = os.environ.get(CodexDesktopAppLauncher.APP_COMMAND_ENV)
        if explicit and explicit.strip():
            path = Path(CodexCommandResolver._expand_environment_references(explicit.strip().strip('"').strip("'")))
            if path.exists():
                return path
            raise FileNotFoundError(
                f"{CodexDesktopAppLauncher.APP_COMMAND_ENV} points to a missing file: {path}. "
                "Set it to the desktop GUI executable, usually C:\\Program Files\\WindowsApps\\OpenAI.Codex_*\\app\\Codex.exe."
            )
        for candidate in CodexDesktopAppLauncher._candidate_executables():
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            "Could not find the Codex Desktop executable. Set CQV_CODEX_APP_COMMAND to the GUI executable "
            "under C:\\Program Files\\WindowsApps\\OpenAI.Codex_*\\app\\Codex.exe. "
            "Do not use the LocalCache\\Local\\OpenAI\\Codex\\bin\\codex.exe CLI path for this button."
        )

    @staticmethod
    def _candidate_executables() -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for env_name in ["ProgramFiles", "ProgramW6432"]:
            root_value = os.environ.get(env_name)
            if not root_value:
                continue
            root = Path(root_value) / "WindowsApps"
            key = str(root).lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        candidates: list[Path] = []
        for install_location in CodexDesktopAppLauncher._appx_package_install_locations():
            candidates.extend(CodexDesktopAppLauncher._executables_under_install_location(install_location))
        for root in roots:
            try:
                packages = list(root.glob("OpenAI.Codex_*"))
            except OSError:
                continue
            for package in packages:
                candidates.extend(CodexDesktopAppLauncher._executables_under_install_location(package))
        unique: list[Path] = []
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return sorted(unique, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)

    @staticmethod
    def _executables_under_install_location(install_location: Path) -> list[Path]:
        return [install_location / "app" / "Codex.exe", install_location / "Codex.exe"]

    @staticmethod
    def _appx_package_install_locations() -> list[Path]:
        if os.name != "nt":
            return []
        shell = CodexCommandResolver._resolve_powershell()
        script = (
            "$packages = @(Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue); "
            "if (-not $packages) { $packages = @(Get-AppxPackage -Name '*Codex*' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Name -like 'OpenAI.Codex*' -or $_.PackageFamilyName -like 'OpenAI.Codex*' }) }; "
            "$packages | Sort-Object Version -Descending | ForEach-Object { $_.InstallLocation }"
        )
        try:
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return []
        if result.returncode != 0:
            return []
        locations: list[Path] = []
        for line in result.stdout.splitlines():
            value = line.strip()
            if value:
                locations.append(Path(value))
        return locations

    @staticmethod
    def _launch_environment(paths: AppPaths, target: CodexHomeTarget) -> dict[str, str]:
        codex_home = paths.codex_home(target)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        return env


class SessionManagerLauncher:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.processes: dict[CodexHomeTarget, SessionManagerProcess] = {}

    def base_uri(self, target: CodexHomeTarget) -> str:
        return f"http://127.0.0.1:{self._state_for(target).port}"

    def ensure_running(self, target: CodexHomeTarget) -> None:
        state = self._state_for(target)
        if state.process and state.process.poll() is None and self._is_healthy(state.port):
            return
        if self._is_healthy(state.port):
            return
        if not state.process or state.process.poll() is not None:
            self._start(target, state)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._is_healthy(state.port):
                return
            if state.process and state.process.poll() is not None:
                raise RuntimeError(self._startup_error_message(target, "Session Manager exited before becoming healthy."))
            time.sleep(0.3)
        raise TimeoutError(self._startup_error_message(target, "Timed out while starting Session Manager."))

    def open_in_browser(self, target: CodexHomeTarget) -> None:
        self.ensure_running(target)
        webbrowser.open(self.base_uri(target))

    def rescan_and_repair(self, target: CodexHomeTarget, progress: Callable[[str], None] | None = None) -> OfficialRepairSummary:
        if progress:
            progress(f"Repair progress: checking {target.value} state database...")
        issue = StateDatabaseRecoveryService.integrity_issue(self.paths, target)
        if issue is not None:
            action = "Use Recover Real State DB first." if target == CodexHomeTarget.REAL else "Seed Sandbox again or restore a sandbox restore point first."
            raise RuntimeError(f"{target.value} Codex state database is unhealthy: {issue}. Repair was not run. {action}")
        if progress:
            progress(f"Repair progress: starting {target.value} Session Manager...")
        self.ensure_running(target)
        if progress:
            progress("Repair progress: rescanning sessions...")
        self._post_json(self.base_uri(target) + "/api/sessions/rescan", {})
        if progress:
            progress("Repair progress: repairing session metadata...")
        repair = self._post_json(self.base_uri(target) + "/api/codex/repair", {})
        return OfficialRepairSummary.from_json(repair.get("stats") if isinstance(repair, dict) else {})

    def stop(self, target: CodexHomeTarget) -> None:
        state = self.processes.get(target)
        if not state:
            return
        try:
            if state.process and state.process.poll() is None:
                state.process.terminate()
                try:
                    state.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    state.process.kill()
                    state.process.wait(timeout=2)
        except Exception:
            pass
        try:
            if state.log_handle:
                state.log_handle.close()
        except Exception:
            pass
        self.processes.pop(target, None)

    def dispose(self) -> None:
        for state in self.processes.values():
            try:
                if state.process and state.process.poll() is None:
                    state.process.kill()
            except Exception:
                pass
            try:
                if state.log_handle:
                    state.log_handle.close()
            except Exception:
                pass

    def _start(self, target: CodexHomeTarget, state: SessionManagerProcess) -> None:
        layout = self._resolve_layout()
        port = self._select_port(target)
        state.port = port
        self.paths.logs_root.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path_for(target)
        state.log_handle = log_path.open("a", encoding="utf-8", errors="replace")
        env = os.environ.copy()
        env["NODE_ENV"] = "production"
        env["PORT"] = str(port)
        env["CODEX_HOME"] = str(self.paths.codex_home(target))
        env["CODEX_MANAGER_HOME"] = str(self.paths.session_manager_home_for(target))
        env["CODEX_VIEWER_DEFAULT_LANGUAGE"] = "en"
        state.process = subprocess.Popen(
            [layout[0], layout[2]],
            cwd=layout[1],
            stdout=state.log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _resolve_layout(self) -> tuple[str, str, str]:
        base_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        for bundled_root in self._candidate_bundled_roots(base_dir):
            bundled_app = bundled_root / "App"
            bundled_node = bundled_root / "Runtime" / "bin" / "node.exe"
            bundled_entry = bundled_app / "dist" / "server" / "index.js"
            if bundled_node.exists() and bundled_entry.exists() and (bundled_app / "node_modules").exists():
                return str(bundled_node), str(bundled_app), str(bundled_entry)
        vendor = self._find_upwards(base_dir, Path("vendor") / "CodexMM")
        if vendor:
            entry = vendor / "dist" / "server" / "index.js"
            if entry.exists() and (vendor / "node_modules").exists():
                return self._resolve_node(), str(vendor), str(entry)
        raise FileNotFoundError("Bundled Session Manager is missing. Run scripts\\build-session-manager.ps1 first.")

    @staticmethod
    def _candidate_bundled_roots(base_dir: Path) -> Iterable[Path]:
        yielded: set[Path] = set()
        for candidate in [base_dir / "SessionManager", base_dir.parent / "SessionManager"]:
            resolved = candidate.resolve()
            if resolved not in yielded:
                yielded.add(resolved)
                yield candidate
        current = base_dir
        while True:
            for relative in [Path("artifacts") / "publish" / "SessionManager", Path("artifacts") / "dev-run" / "SessionManager"]:
                candidate = current / relative
                resolved = candidate.resolve()
                if resolved not in yielded:
                    yielded.add(resolved)
                    yield candidate
            if current.parent == current:
                break
            current = current.parent

    @staticmethod
    def _resolve_node() -> str:
        for item in os.environ.get("PATH", "").split(os.pathsep):
            node = Path(item.strip()) / "node.exe"
            if node.exists():
                return str(node)
        raise FileNotFoundError("node.exe was not found in PATH.")

    @staticmethod
    def _find_upwards(start: Path, relative: Path) -> Path | None:
        current = start
        while True:
            candidate = current / relative
            if candidate.exists():
                return candidate
            if current.parent == current:
                return None
            current = current.parent

    def _state_for(self, target: CodexHomeTarget) -> SessionManagerProcess:
        if target not in self.processes:
            self.processes[target] = SessionManagerProcess(4319 if target == CodexHomeTarget.REAL else 4318)
        return self.processes[target]

    def _select_port(self, target: CodexHomeTarget) -> int:
        for port in self._candidate_ports(target):
            if self._can_bind_loopback(port):
                return port
        raise RuntimeError("No available loopback port was found for Session Manager.")

    @staticmethod
    def _candidate_ports(target: CodexHomeTarget) -> Iterable[int]:
        yield 4319 if target == CodexHomeTarget.REAL else 4318
        start, end = (48221, 48260) if target == CodexHomeTarget.REAL else (48180, 48220)
        yield from range(start, end + 1)

    def _startup_error_message(self, target: CodexHomeTarget, message: str) -> str:
        excerpt = self._read_log_excerpt(target)
        if not excerpt:
            return message
        return f"{message}\n\nLast Session Manager log:\n{excerpt}"

    def _read_log_excerpt(self, target: CodexHomeTarget, max_lines: int = 24) -> str:
        path = self._log_path_for(target)
        try:
            if not path.exists():
                return ""
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-max_lines:])
        except Exception:
            return ""

    def _log_path_for(self, target: CodexHomeTarget) -> Path:
        return self.paths.logs_root / ("session-manager-real.log" if target == CodexHomeTarget.REAL else "session-manager-sandbox.log")

    @staticmethod
    def _can_bind_loopback(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    @staticmethod
    def _is_healthy(port: int) -> bool:
        try:
            with request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as ex:
            raise RuntimeError(SessionManagerLauncher._http_error_message(ex)) from ex
        return json.loads(body) if body else {}

    @staticmethod
    def _http_error_message(ex: error.HTTPError) -> str:
        try:
            raw_body = ex.read().decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
        message = f"Session Manager HTTP {ex.code}"
        if not raw_body.strip():
            return message
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return f"{message}: {raw_body.strip()}"
        if isinstance(payload, dict):
            code = payload.get("code")
            server_error = payload.get("error")
            if code and server_error:
                return f"{message}: {server_error} ({code})"
            if server_error:
                return f"{message}: {server_error}"
        return f"{message}: {raw_body.strip()}"


class SafeSwitchService:
    def __init__(
        self,
        paths: AppPaths,
        vault: AccountVault,
        session_meta_synchronizer: SessionMetaSynchronizer,
        repair_client: SessionManagerLauncher,
        desktop_controller: WindowsCodexDesktopController | None = None,
        allow_real_codex_home_override: bool = False,
        repair_after_switch: bool = True,
    ):
        self.paths = paths
        self.vault = vault
        self.session_meta_synchronizer = session_meta_synchronizer
        self.provider_visibility_synchronizer = ProviderVisibilitySynchronizer()
        self.repair_client = repair_client
        self.desktop_controller = desktop_controller or WindowsCodexDesktopController()
        self.allow_real_codex_home_override = allow_real_codex_home_override
        self.repair_after_switch = repair_after_switch

    def switch(self, account: AccountRecord, target: CodexHomeTarget, progress: Callable[[str], None] | None = None) -> SwitchOperationResult:
        if progress:
            progress(f"Switch progress: validating {target.value} target...")
        self._ensure_target_allowed(target)
        target_home = self.paths.codex_home(target)
        auth_path = self.paths.auth_path(target)
        config_path = self.paths.config_path(target)
        backup_manager = BackupManager(self.paths.backups_root_for(target), target)
        if progress:
            progress(f"Switch progress: loading vault material for {account.metadata.display_name}...")
        runtime = self.vault.read_runtime(account)
        current_config = config_path.read_bytes() if config_path.exists() else None
        next_config = self._next_config_for_switch(account, runtime, current_config)
        files = self._files_to_backup(target, include_config=next_config is not None)
        desktop: CodexDesktopSwitchPreparation | None = None
        if target == CodexHomeTarget.REAL:
            if progress:
                progress("Switch progress: asking Codex Desktop to close gently...")
            desktop = self.desktop_controller.prepare_for_real_switch()
            if not desktop.can_proceed:
                raise RuntimeError(desktop.message)
        restore_point: RestorePointManifest | None = None
        try:
            if progress:
                progress(f"Switch progress: creating restore point for {target.value} Codex home...")
            restore_point = backup_manager.create_restore_point(
                "safe-switch-real" if target == CodexHomeTarget.REAL else "safe-switch-sandbox",
                f"Switch {'real Codex home' if target == CodexHomeTarget.REAL else 'sandbox'} to {account.metadata.display_name}",
                files,
            )
            if progress:
                progress(f"Switch progress: restore point {restore_point.id} created; writing auth/config files...")
            target_home.mkdir(parents=True, exist_ok=True)
            auth_path.write_bytes(runtime.auth_data)
            active_config = current_config.decode("utf-8", errors="replace") if current_config else ""
            if next_config is not None:
                config_path.write_text(next_config, encoding="utf-8")
                active_config = next_config
            if progress:
                progress(f"Switch progress: {target.value} runtime files now point to {account.metadata.display_name}.")
            if next_config is not None:
                provider = self._resolve_provider(account, active_config)
                if progress:
                    progress(f"Switch progress: synchronizing session provider metadata to {provider}...")
                sync = self.provider_visibility_synchronizer.sync(self.paths, target, provider)
                updated = sync.changed_session_files
                if progress:
                    progress(
                        f"Switch progress: synchronized {sync.changed_session_files} rollout files and "
                        f"{sync.sqlite_rows_updated} SQLite rows to provider {provider}."
                    )
            else:
                updated = 0
            repair = OfficialRepairSummary()
            repair_warning = None
            if self.repair_after_switch and target == CodexHomeTarget.SANDBOX:
                try:
                    if progress:
                        progress("Switch progress: repairing sandbox sessions after switch...")
                    repair = self.repair_client.rescan_and_repair(target)
                except Exception as ex:
                    repair_warning = f"Session Manager repair failed after switch: {ex}"
                    app_log(self.paths.logs_root, repair_warning, ex)
                    if progress:
                        progress(repair_warning)
            desktop_message = None
            if target == CodexHomeTarget.REAL and desktop and desktop.was_closed:
                if progress:
                    progress("Switch progress: restarting Codex Desktop...")
                desktop_message = self.desktop_controller.try_restart()
            return SwitchOperationResult(account.metadata.id, restore_point, updated, repair, target, desktop_message, repair_warning)
        except Exception as ex:
            if restore_point is not None:
                try:
                    backup_manager.restore(restore_point)
                except Exception as restore_ex:
                    app_log(self.paths.logs_root, "Switch rollback failed.", restore_ex)
                    raise RuntimeError(f"{ex} Rollback also failed: {restore_ex}") from ex
            if target == CodexHomeTarget.REAL and desktop and desktop.was_closed:
                self.desktop_controller.try_restart()
            raise

    def rollback_latest(self, target: CodexHomeTarget) -> RestorePointManifest:
        return BackupManager(self.paths.backups_root_for(target), target).restore_latest(SnapshotKind.AUTOMATIC)

    def latest_restore_point(self, target: CodexHomeTarget) -> RestorePointManifest | None:
        return BackupManager(self.paths.backups_root_for(target), target).latest(SnapshotKind.AUTOMATIC)

    def planned_write_files(self, account: AccountRecord, target: CodexHomeTarget) -> list[Path]:
        runtime = self.vault.read_runtime(account)
        config_path = self.paths.config_path(target)
        current_config = config_path.read_bytes() if config_path.exists() else None
        next_config = self._next_config_for_switch(account, runtime, current_config)
        return self._files_to_backup(target, include_config=next_config is not None)

    def _planned_session_files(self, target: CodexHomeTarget) -> list[Path]:
        files: list[Path] = []
        for root in self.paths.session_roots(target):
            if root.exists():
                files.extend(_safe_session_jsonl_files(root))
        return files

    def _files_to_backup(self, target: CodexHomeTarget, include_config: bool) -> list[Path]:
        provider_sync_files = [
            *self.paths.state_database_paths(target),
            *self.paths.global_state_paths(target),
            *self._planned_session_files(target),
        ]
        if target == CodexHomeTarget.REAL:
            files = [self.paths.auth_path(target)]
            if include_config:
                files.append(self.paths.config_path(target))
                files.extend(provider_sync_files)
            return files
        if not include_config:
            return [self.paths.auth_path(target)]
        return [*self.paths.protected_codex_files(target), *self._planned_session_files(target)]

    def _next_config_for_switch(self, account: AccountRecord, runtime: AccountRuntimeMaterial, current_config: bytes | None) -> str | None:
        if account.metadata.auth_mode == AuthMode.API_KEY:
            return RuntimeConfig.merge_for_switch(current_config, runtime.config_data, account.metadata.auth_mode)
        current_provider = RuntimeConfig.parse(current_config).provider_id
        oauth_config = self.vault.read_oauth_config(account)
        if self.vault.has_oauth_account_preference(account) and oauth_config:
            return RuntimeConfig.merge_for_switch(current_config, oauth_config, AuthMode.CHAT_GPT)
        if current_provider and current_provider.lower() != "openai":
            if oauth_config:
                return RuntimeConfig.merge_for_switch(current_config, oauth_config, AuthMode.CHAT_GPT)
            return RuntimeConfig.merge_for_switch(current_config, None, AuthMode.CHAT_GPT)
        if RuntimeConfig.needs_compatibility_normalization(current_config):
            return RuntimeConfig.normalize_compatibility(current_config)
        return None

    def _ensure_target_allowed(self, target: CodexHomeTarget) -> None:
        if target == CodexHomeTarget.SANDBOX:
            sandbox = self.paths.sandbox_codex_home.resolve()
            real = self.paths.real_codex_home.resolve()
            if sandbox == real or real in sandbox.parents:
                raise RuntimeError("Sandbox safe switch cannot target the real Codex home.")
            allowed_roots = [self.paths.storage_root.resolve(), self.paths.data_root.resolve()]
            if not any(self._is_same_or_child(sandbox, root) for root in allowed_roots):
                raise RuntimeError("Sandbox safe switch can only target the configured sandbox storage root.")
            return
        real = self.paths.real_codex_home.resolve()
        expected = (Path.home() / ".codex").resolve()
        if not self.allow_real_codex_home_override and real != expected:
            raise RuntimeError("Real switch can only target the current user's .codex directory.")
        if not real.exists():
            raise FileNotFoundError(f"Real Codex home was not found: {real}")

    @staticmethod
    def _is_same_or_child(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resolve_provider(account: AccountRecord, merged_config: str) -> str:
        summary = RuntimeConfig.parse(merged_config.encode("utf-8"))
        if summary.provider_id:
            return summary.provider_id
        if account.metadata.provider_id:
            return account.metadata.provider_id
        return "openai" if account.metadata.auth_mode == AuthMode.CHAT_GPT else "openai-compatible"


class QuotaSnapshotBuffer:
    def __init__(self, refresh: Callable[[CodexHomeTarget], CodexSnapshot], ttl_seconds: float = 30):
        self.refresh = refresh
        self.ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.RLock()
        self._snapshots: dict[CodexHomeTarget, CodexSnapshot] = {}

    def get(self, target: CodexHomeTarget, force_refresh: bool = False) -> CodexSnapshot:
        with self._lock:
            snapshot = self._snapshots.get(target)
            if not force_refresh and snapshot and now_utc() - snapshot.fetched_at < self.ttl:
                return snapshot
        snapshot = self.refresh(target)
        with self._lock:
            self._snapshots[target] = snapshot
        return snapshot

    def clear(self, target: CodexHomeTarget | None = None) -> None:
        with self._lock:
            if target is None:
                self._snapshots.clear()
            else:
                self._snapshots.pop(target, None)


def app_log(logs_root: Path, message: str, exception: BaseException | None = None) -> None:
    try:
        logs_root.mkdir(parents=True, exist_ok=True)
        with (logs_root / "app.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_utc().isoformat()}] {message}\n")
            if exception:
                handle.write(f"{type(exception).__name__}: {exception}\n")
    except Exception:
        pass
