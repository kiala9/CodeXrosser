from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any


class AuthMode(IntEnum):
    CHAT_GPT = 0
    API_KEY = 1

    @staticmethod
    def from_json(value: Any) -> "AuthMode":
        if isinstance(value, int):
            return AuthMode(value)
        text = str(value or "").lower()
        return AuthMode.API_KEY if text in {"apikey", "api_key", "api key", "1"} else AuthMode.CHAT_GPT

    def to_json(self) -> int:
        return int(self.value)


class CodexHomeTarget(str, Enum):
    SANDBOX = "Sandbox"
    REAL = "Real"

    @staticmethod
    def from_json(value: Any) -> "CodexHomeTarget":
        text = str(value or "").lower()
        return CodexHomeTarget.REAL if text == "real" else CodexHomeTarget.SANDBOX


class SnapshotKind(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"

    @staticmethod
    def from_json(value: Any) -> "SnapshotKind":
        text = str(value or "").lower()
        return SnapshotKind.MANUAL if text == "manual" else SnapshotKind.AUTOMATIC


class UiLanguage(str, Enum):
    ENGLISH = "en"
    CHINESE = "zh-Hans"

    @staticmethod
    def from_json(value: Any) -> "UiLanguage":
        text = str(value or "").lower()
        return UiLanguage.CHINESE if text in {"zh", "zh-cn", "zh-hans", "chinese"} else UiLanguage.ENGLISH


@dataclass(frozen=True)
class AppSettings:
    active_codex_home_target: CodexHomeTarget = CodexHomeTarget.SANDBOX
    language: UiLanguage = UiLanguage.ENGLISH


@dataclass(frozen=True)
class AccountRuntimeMaterial:
    auth_data: bytes
    config_data: bytes | None


@dataclass(frozen=True)
class CodexAccount:
    type: str
    email: str | None = None
    plan_type: str | None = None

    @property
    def display_label(self) -> str:
        if self.email:
            return self.email
        return "API Key" if self.type.lower() == "apikey" else "Not signed in"


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    window_duration_mins: int | None = None
    resets_at: int | None = None

    @property
    def remaining_percent(self) -> float:
        return min(max(100.0 - self.used_percent, 0.0), 100.0)


@dataclass(frozen=True)
class RateLimitSnapshot:
    limit_id: str | None
    limit_name: str | None
    primary: RateLimitWindow | None
    secondary: RateLimitWindow | None
    plan_type: str | None


@dataclass(frozen=True)
class QuotaDisplayWindow:
    label: str
    window: RateLimitWindow


@dataclass(frozen=True)
class CodexSnapshot:
    account: CodexAccount
    rate_limits: RateLimitSnapshot | None
    fetched_at: datetime
    quota_error: str | None = None

    def display_windows(self) -> list[QuotaDisplayWindow]:
        if self.rate_limits is None:
            return []
        windows = [
            (0, self.rate_limits.primary),
            (1, self.rate_limits.secondary),
        ]
        present = [(index, window) for index, window in windows if window is not None]
        present.sort(key=lambda item: (item[1].window_duration_mins or 2**31, item[0]))
        return [
            QuotaDisplayWindow(_quota_label(window.window_duration_mins, offset, len(present)), window)
            for offset, (_, window) in enumerate(present)
        ]


@dataclass(frozen=True)
class AccountMetadata:
    id: str
    display_name: str
    auth_mode: AuthMode
    provider_id: str | None
    base_url: str | None
    model: str | None
    created_at: datetime
    last_used_at: datetime | None
    runtime_key: str

    @staticmethod
    def from_json(data: dict[str, Any]) -> "AccountMetadata":
        return AccountMetadata(
            id=str(data["id"]),
            display_name=str(data.get("displayName") or ""),
            auth_mode=AuthMode.from_json(data.get("authMode")),
            provider_id=data.get("providerId"),
            base_url=data.get("baseUrl"),
            model=data.get("model"),
            created_at=_parse_dt(data.get("createdAt")),
            last_used_at=_parse_optional_dt(data.get("lastUsedAt")),
            runtime_key=str(data.get("runtimeKey") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "authMode": self.auth_mode.to_json(),
            "providerId": self.provider_id,
            "baseUrl": self.base_url,
            "model": self.model,
            "createdAt": _format_dt(self.created_at),
            "lastUsedAt": _format_dt(self.last_used_at) if self.last_used_at else None,
            "runtimeKey": self.runtime_key,
        }


@dataclass(frozen=True)
class AccountRecord:
    metadata: AccountMetadata
    directory_path: str
    auth_path: str
    config_path: str


@dataclass(frozen=True)
class RestorePointFile:
    source_path: str
    backup_relative_path: str
    existed: bool

    @staticmethod
    def from_json(data: dict[str, Any]) -> "RestorePointFile":
        return RestorePointFile(
            str(data["sourcePath"]),
            str(data["backupRelativePath"]),
            bool(data["existed"]),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "sourcePath": self.source_path,
            "backupRelativePath": self.backup_relative_path,
            "existed": self.existed,
        }


@dataclass(frozen=True)
class RestorePointManifest:
    id: str
    reason: str
    summary: str
    created_at: datetime
    files: list[RestorePointFile]
    target: CodexHomeTarget | None = None
    kind: SnapshotKind = SnapshotKind.AUTOMATIC
    size_bytes: int = 0
    file_count: int = 0

    @staticmethod
    def from_json(data: dict[str, Any]) -> "RestorePointManifest":
        target_value = data.get("target")
        return RestorePointManifest(
            str(data["id"]),
            str(data.get("reason") or ""),
            str(data.get("summary") or ""),
            _parse_dt(data.get("createdAt")),
            [RestorePointFile.from_json(item) for item in data.get("files", [])],
            CodexHomeTarget.from_json(target_value) if target_value else None,
            SnapshotKind.from_json(data.get("kind")),
            int(data.get("sizeBytes") or 0),
            int(data.get("fileCount") or len(data.get("files", []))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "summary": self.summary,
            "createdAt": _format_dt(self.created_at),
            "files": [item.to_json() for item in self.files],
            "target": self.target.value if self.target else None,
            "kind": self.kind.value,
            "sizeBytes": self.size_bytes,
            "fileCount": self.file_count or len(self.files),
        }


@dataclass(frozen=True)
class SnapshotRetentionStatus:
    target: CodexHomeTarget
    kind: SnapshotKind
    count: int
    size_bytes: int
    max_count: int
    max_size_bytes: int

    @property
    def over_limit(self) -> bool:
        return self.count > self.max_count or self.size_bytes > self.max_size_bytes


@dataclass(frozen=True)
class WritePreview:
    operation: str
    target: CodexHomeTarget
    target_home: str
    affected_files: int
    affected_bytes: int
    created_files: int
    modified_files: int
    deleted_files: int = 0
    sample_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class SandboxRealSessionSyncResult:
    restore_point: RestorePointManifest
    scanned_files: int
    copied_files: int
    overwritten_files: int
    skipped_same_files: int
    skipped_real_newer_files: int
    index_entries_merged: int
    sqlite_rows_inserted: int
    sqlite_rows_updated: int
    provider_sync: ProviderVisibilitySyncSummary
    asset_scanned_files: int = 0
    asset_copied_files: int = 0
    asset_overwritten_files: int = 0
    asset_skipped_same_files: int = 0
    asset_skipped_real_newer_files: int = 0


@dataclass(frozen=True)
class OfficialRepairSummary:
    created_threads: int = 0
    updated_threads: int = 0
    updated_session_index_entries: int = 0
    removed_broken_threads: int = 0
    hidden_snapshot_only_sessions: int = 0

    @staticmethod
    def from_json(data: dict[str, Any] | None) -> "OfficialRepairSummary":
        data = data or {}
        return OfficialRepairSummary(
            int(data.get("createdThreads") or 0),
            int(data.get("updatedThreads") or 0),
            int(data.get("updatedSessionIndexEntries") or 0),
            int(data.get("removedBrokenThreads") or 0),
            int(data.get("hiddenSnapshotOnlySessions") or 0),
        )


@dataclass(frozen=True)
class ProviderVisibilitySyncSummary:
    changed_session_files: int = 0
    skipped_session_files: int = 0
    sqlite_rows_updated: int = 0
    sqlite_provider_rows_updated: int = 0
    sqlite_user_event_rows_updated: int = 0
    sqlite_cwd_rows_updated: int = 0
    sqlite_present: bool = False


@dataclass(frozen=True)
class SwitchOperationResult:
    target_account_id: str
    restore_point: RestorePointManifest
    updated_session_files: int
    repair_summary: OfficialRepairSummary
    target: CodexHomeTarget
    desktop_message: str | None = None
    repair_warning: str | None = None


@dataclass(frozen=True)
class SandboxSeedResult:
    copied_files: int
    sandbox_codex_home: str
    skipped_files: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodexDesktopSwitchPreparation:
    can_proceed: bool
    was_running: bool
    was_closed: bool
    message: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_optional_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_dt(value)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    if not text:
        return now_utc()
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return now_utc()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _quota_label(duration_mins: int | None, position: int, total: int) -> str:
    if not duration_mins or duration_mins <= 0:
        return "quota" if total == 1 else f"quota {position + 1}"
    if duration_mins % 10080 == 0:
        return f"{duration_mins // 10080}w"
    if duration_mins % 1440 == 0:
        return f"{duration_mins // 1440}d"
    if duration_mins % 60 == 0:
        return f"{duration_mins // 60}h"
    return f"{duration_mins}m"
