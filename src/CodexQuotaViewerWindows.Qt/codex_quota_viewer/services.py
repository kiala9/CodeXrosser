from __future__ import annotations

from pathlib import Path

from .core import (
    AccountVault,
    ApiAccountService,
    AppPaths,
    AppSettings,
    AppSettingsStore,
    AuditLog,
    CodexDesktopAppLauncher,
    ChatGptLoginService,
    CodexRpcClient,
    ProviderVisibilitySynchronizer,
    QuotaSnapshotBuffer,
    RuntimeConfig,
    SafeSwitchService,
    SandboxSeeder,
    SandboxRealSessionSyncService,
    SessionManagerLauncher,
    SessionMetaSynchronizer,
    SnapshotManager,
    VaultQuotaCache,
    WritePreviewBuilder,
    WindowsCodexDesktopController,
    app_log,
)
from .sessions import SessionsManager
from .models import (
    AccountRecord,
    AccountRuntimeMaterial,
    AuthMode,
    CodexAccount,
    CodexHomeTarget,
    CodexSnapshot,
    OfficialRepairSummary,
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


class AppServices:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.for_current_user()
        self.paths.ensure_created()
        self.settings_store = AppSettingsStore(self.paths.settings_path)
        settings = self.settings_store.load()
        self.active_target = settings.active_codex_home_target
        self.ui_language = settings.language
        self.vault = AccountVault(self.paths.accounts_root)
        self.quota_cache = VaultQuotaCache(self.paths.accounts_root / "quota-cache.json")
        self.rpc_client = CodexRpcClient(logs_root=self.paths.logs_root)
        self.seeder = SandboxSeeder()
        self.api_accounts = ApiAccountService()
        self.chatgpt_login = ChatGptLoginService()
        self.session_manager = SessionManagerLauncher(self.paths)
        self._sessions_managers: dict[CodexHomeTarget, SessionsManager] = {}
        self.provider_visibility_synchronizer = ProviderVisibilitySynchronizer()
        self.snapshot_manager = SnapshotManager(self.paths)
        self.audit_log = AuditLog(self.paths.audit_log_path)
        self.session_sync = SandboxRealSessionSyncService(self.paths, self.provider_visibility_synchronizer)
        self.desktop_controller = WindowsCodexDesktopController()
        self.desktop_launcher = CodexDesktopAppLauncher(self.desktop_controller)
        self.safe_switch = SafeSwitchService(
            self.paths,
            self.vault,
            SessionMetaSynchronizer(),
            self.session_manager,
            desktop_controller=self.desktop_controller,
            repair_after_switch=False,
        )
        self.quota_buffer = QuotaSnapshotBuffer(self._refresh_quota_core)

    @property
    def active_target_label(self) -> str:
        return self.active_target.value

    @property
    def active_codex_home(self) -> Path:
        return self.paths.codex_home(self.active_target)

    def set_active_target(self, target: CodexHomeTarget) -> None:
        self.active_target = target
        self.settings_store.save(AppSettings(target, self.ui_language))

    def set_ui_language(self, language: UiLanguage) -> None:
        self.ui_language = language
        self.settings_store.save(AppSettings(self.active_target, language))

    def refresh_quota(self, force_refresh: bool = False) -> CodexSnapshot:
        return self.quota_buffer.get(self.active_target, force_refresh)

    def clear_active_quota_buffer(self) -> None:
        self.quota_buffer.clear(self.active_target)

    def _refresh_quota_core(self, target: CodexHomeTarget) -> CodexSnapshot:
        active = self.resolve_active_account()
        if active and active.metadata.auth_mode == AuthMode.API_KEY:
            return CodexSnapshot(
                CodexAccount("apikey", active.metadata.display_name, None),
                None,
                now_utc(),
                "Quota is unavailable for API-key accounts.",
            )
        try:
            snapshot = self.rpc_client.fetch_snapshot(self.paths.codex_home(target))
        except Exception as ex:
            app_log(self.paths.logs_root, "Quota refresh failed.", ex)
            return CodexSnapshot(
                CodexAccount("unknown", None, None),
                None,
                now_utc(),
                self._friendly_quota_error(ex),
            )
        if active is not None and snapshot.rate_limits is not None:
            try:
                self.quota_cache.upsert(active.metadata.id, snapshot)
            except Exception as ex:
                app_log(self.paths.logs_root, "Quota cache write failed.", ex)
        return snapshot

    def refresh_account_quota(self, account: AccountRecord) -> CodexSnapshot:
        """Fetch quota for a saved account without switching the active home.

        API accounts return the existing "unavailable" snapshot. Successful
        fetches are written back to :attr:`quota_cache` keyed by the
        account id; failures leave the existing cache entry intact.
        """
        if account.metadata.auth_mode == AuthMode.API_KEY:
            return CodexSnapshot(
                CodexAccount("apikey", account.metadata.display_name, None),
                None,
                now_utc(),
                "Quota is unavailable for API-key accounts.",
            )
        try:
            runtime = self.vault.read_runtime(account)
            snapshot = self.rpc_client.fetch_snapshot_for_account(runtime)
        except Exception as ex:
            app_log(self.paths.logs_root, f"Per-account quota refresh failed: {account.metadata.display_name}", ex)
            return CodexSnapshot(
                CodexAccount("ChatGPT", None, None),
                None,
                now_utc(),
                self._friendly_quota_error(ex),
            )
        if snapshot.rate_limits is not None:
            try:
                self.quota_cache.upsert(account.metadata.id, snapshot)
            except Exception as ex:
                app_log(self.paths.logs_root, "Quota cache write failed.", ex)
        return snapshot

    def refresh_all_chatgpt_quotas(self, progress_callback=None) -> dict[str, CodexSnapshot]:
        """Sequentially refresh quota for every saved ChatGPT account.

        Sequential because each fetch spawns its own ``codex app-server``
        process; running them concurrently would multiply the resource
        cost without a meaningful latency win. ``progress_callback`` is
        invoked as ``(account, snapshot, done, total)`` after each fetch.
        """
        records = [r for r in self.vault.load() if r.metadata.auth_mode == AuthMode.CHAT_GPT]
        total = len(records)
        results: dict[str, CodexSnapshot] = {}
        for index, account in enumerate(records, start=1):
            snapshot = self.refresh_account_quota(account)
            results[account.metadata.id] = snapshot
            if progress_callback is not None:
                try:
                    progress_callback(account, snapshot, index, total)
                except Exception as ex:
                    app_log(self.paths.logs_root, "Quota progress callback raised.", ex)
        return results

    def seed_sandbox(self) -> SandboxSeedResult:
        self.quota_buffer.clear(CodexHomeTarget.SANDBOX)
        self.audit_log.append("seed", CodexHomeTarget.SANDBOX, "started", "Seed sandbox from real Codex home")
        try:
            result = self.seeder.seed(self.paths)
            self.vault.capture_sandbox_current(self.paths, "Sandbox Current")
            self.audit_log.append("seed", CodexHomeTarget.SANDBOX, "succeeded", "Seeded sandbox", affected_file_count=result.copied_files)
            return result
        except Exception as ex:
            self.audit_log.append("seed", CodexHomeTarget.SANDBOX, "failed", "Seed sandbox failed", error_message=str(ex))
            raise

    def add_api_account(self, api_key: str, base_url: str, display_name: str | None, model: str | None) -> AccountRecord:
        draft = self.api_accounts.configure(api_key, base_url, display_name, model)
        runtime = self.api_accounts.create_runtime(draft)
        return self.vault.upsert(
            draft.display_name,
            runtime,
            AuthMode.API_KEY,
            provider_id="openai-compatible",
            base_url=draft.normalized_base_url,
            model=draft.model,
        )

    def add_chatgpt_account(self, progress=None) -> AccountRecord:
        runtime = self.chatgpt_login.login(progress=progress)
        display_name = AccountVault.suggested_display_name(runtime, AuthMode.CHAT_GPT)
        return self.vault.upsert(display_name, runtime, AuthMode.CHAT_GPT, provider_id="openai")

    def load_accounts(self) -> list[AccountRecord]:
        return self.vault.load()

    def resolve_active_account(self) -> AccountRecord | None:
        auth_path = self.paths.auth_path(self.active_target)
        if not auth_path.exists():
            return None
        auth = auth_path.read_bytes()
        config_path = self.paths.config_path(self.active_target)
        config = config_path.read_bytes() if config_path.exists() else None
        mode = AccountVault.detect_auth_mode(auth)
        runtime_key = AccountVault.runtime_key_for_mode(AccountRuntimeMaterial(auth, config), mode)
        accounts = self.vault.load()
        for account in accounts:
            if account.metadata.runtime_key == runtime_key:
                return account
        if mode == AuthMode.CHAT_GPT:
            return self.vault.find_oauth_by_identity(auth)
        if mode == AuthMode.API_KEY:
            return self.vault.find_api_by_identity(auth, config)
        return None

    def rename_account(self, account: AccountRecord, display_name: str) -> AccountRecord:
        return self.vault.rename(account.metadata.id, display_name)

    def delete_account(self, account: AccountRecord) -> None:
        self.vault.delete(account.metadata.id)
        try:
            self.quota_cache.delete(account.metadata.id)
        except Exception as ex:
            app_log(self.paths.logs_root, "Quota cache delete failed.", ex)

    def switch(self, account: AccountRecord, progress=None) -> SwitchOperationResult:
        target = self.active_target
        self.quota_buffer.clear(target)
        self.audit_log.append("switch", target, "started", f"Switch to {account.metadata.display_name}", account_display_name=account.metadata.display_name)
        try:
            result = self.safe_switch.switch(account, target, progress=progress)
            self.audit_log.append(
                "switch",
                target,
                "succeeded",
                f"Switched to {account.metadata.display_name}",
                result.restore_point.id,
                account.metadata.display_name,
                len(result.restore_point.files),
                result.restore_point.size_bytes,
            )
            return result
        except Exception as ex:
            self.audit_log.append("switch", target, "failed", f"Switch to {account.metadata.display_name} failed", account_display_name=account.metadata.display_name, error_message=str(ex))
            raise

    def planned_switch_write_files(self, account: AccountRecord, target: CodexHomeTarget | None = None) -> list[Path]:
        return self.safe_switch.planned_write_files(account, target or self.active_target)

    def switch_write_preview(self, account: AccountRecord, target: CodexHomeTarget | None = None) -> WritePreview:
        selected = target or self.active_target
        return WritePreviewBuilder.for_files(
            "Switch Real Codex" if selected == CodexHomeTarget.REAL else "Switch Sandbox Codex",
            selected,
            self.paths.codex_home(selected),
            self.planned_switch_write_files(account, selected),
            f"Target account: {account.metadata.display_name}. Secrets are redacted; session message bodies are never shown.",
        )

    def oauth_config_source_label(self, account: AccountRecord) -> str | None:
        if account.metadata.auth_mode != AuthMode.CHAT_GPT:
            return None
        return self.vault.oauth_config_source_label(account)

    def rollback(self) -> RestorePointManifest:
        target = self.active_target
        self.quota_buffer.clear(target)
        self.audit_log.append("rollback", target, "started", "Rollback latest automatic snapshot")
        try:
            manifest = self.safe_switch.rollback_latest(target)
            self.audit_log.append("rollback", target, "succeeded", f"Rolled back snapshot {manifest.id}", manifest.id, affected_file_count=len(manifest.files), affected_bytes=manifest.size_bytes)
            return manifest
        except Exception as ex:
            self.audit_log.append("rollback", target, "failed", "Rollback failed", error_message=str(ex))
            raise

    def open_session_manager(self) -> None:
        self.session_manager.open_in_browser(self.active_target)

    def sessions_manager(self, target: CodexHomeTarget) -> SessionsManager:
        existing = self._sessions_managers.get(target)
        if existing is not None:
            return existing
        manager = SessionsManager(
            self.paths.codex_home(target),
            self.paths.session_manager_home_for(target),
        )
        self._sessions_managers[target] = manager
        return manager

    def open_codex_app(self) -> str:
        return self.desktop_launcher.launch(self.paths, self.active_target)

    def repair(self, progress=None):
        return self.repair_target(self.active_target, progress=progress)

    def repair_target(self, target: CodexHomeTarget, progress=None):
        if progress:
            progress(f"Repair progress: preparing {target.value} Codex threads...")
        self.audit_log.append("repair", target, "started", "Repair provider visibility")
        if target != CodexHomeTarget.REAL:
            try:
                result = self._sync_provider_visibility(target, progress=progress)
                self.audit_log.append("repair", target, "succeeded", "Repair complete", affected_file_count=result.updated_session_index_entries)
                return result
            except Exception as ex:
                self.audit_log.append("repair", target, "failed", "Repair failed", error_message=str(ex))
                raise
        if progress:
            progress("Repair progress: checking whether Codex Desktop needs to close...")
        desktop = self.desktop_controller.prepare_for_real_switch()
        if not desktop.can_proceed:
            self.audit_log.append("repair", target, "failed", "Repair blocked by Codex Desktop", error_message=desktop.message)
            raise RuntimeError(desktop.message)
        if progress:
            progress("Repair progress: Codex Desktop is ready; synchronizing thread metadata...")
        try:
            result = self._sync_provider_visibility(target, progress=progress)
            self.audit_log.append("repair", target, "succeeded", "Repair complete", affected_file_count=result.updated_session_index_entries)
            return result
        except Exception as ex:
            self.audit_log.append("repair", target, "failed", "Repair failed", error_message=str(ex))
            raise
        finally:
            if desktop.was_closed:
                if progress:
                    progress("Repair progress: restarting Codex Desktop after repair...")
                message = self.desktop_controller.try_restart()
                app_log(self.paths.logs_root, message)

    def _sync_provider_visibility(self, target: CodexHomeTarget, progress=None) -> OfficialRepairSummary:
        if progress:
            progress(f"Repair progress: reading {target.value} provider configuration...")
        config_path = self.paths.config_path(target)
        config = config_path.read_bytes() if config_path.exists() else None
        provider = RuntimeConfig.parse(config).provider_id or "openai"
        if progress:
            progress(f"Repair progress: syncing sessions to provider {provider}...")
        result = self.provider_visibility_synchronizer.sync(self.paths, target, provider)
        if progress:
            progress(
                f"Repair progress: synced {result.changed_session_files} session files and "
                f"{result.sqlite_rows_updated} SQLite rows."
            )
        return OfficialRepairSummary(
            0,
            result.sqlite_rows_updated,
            result.changed_session_files,
            0,
            0,
        )

    def latest_restore_point(self) -> RestorePointManifest | None:
        return self.snapshot_manager.latest_automatic(self.active_target)

    def audit_cancel(
        self,
        event_type: str,
        target: CodexHomeTarget | None,
        summary: str,
        snapshot_id: str | None = None,
        account_display_name: str | None = None,
    ) -> None:
        self.audit_log.append(event_type, target, "cancelled", summary, snapshot_id, account_display_name)

    def list_snapshots(self, target: CodexHomeTarget, kind: SnapshotKind | None = None) -> list[RestorePointManifest]:
        return self.snapshot_manager.list_snapshots(target, kind)

    def snapshot_retention_status(self, target: CodexHomeTarget, kind: SnapshotKind) -> SnapshotRetentionStatus:
        return self.snapshot_manager.retention_status(target, kind)

    def create_snapshot(self, target: CodexHomeTarget, summary: str = "Manual snapshot") -> RestorePointManifest:
        self.audit_log.append("snapshot.create", target, "started", summary)
        try:
            manifest = self.snapshot_manager.create_manual_snapshot(target, summary)
            self.audit_log.append("snapshot.create", target, "succeeded", summary, manifest.id, affected_file_count=len(manifest.files), affected_bytes=manifest.size_bytes)
            return manifest
        except Exception as ex:
            self.audit_log.append("snapshot.create", target, "failed", summary, error_message=str(ex))
            raise

    def restore_snapshot(self, target: CodexHomeTarget, snapshot_id: str, progress=None) -> RestorePointManifest:
        self.audit_log.append("snapshot.restore", target, "started", f"Restore snapshot {snapshot_id}", snapshot_id)
        desktop = None
        if target == CodexHomeTarget.REAL:
            if progress:
                progress("Restore progress: asking Codex Desktop to close gently...")
            desktop = self.desktop_controller.prepare_for_real_switch()
            if not desktop.can_proceed:
                self.audit_log.append("snapshot.restore", target, "failed", f"Restore snapshot {snapshot_id} blocked", snapshot_id, error_message=desktop.message)
                raise RuntimeError(desktop.message)
        try:
            manager = self.snapshot_manager.manager_for(target)
            manifest = manager._find(snapshot_id, None)
            if progress:
                progress("Restore progress: creating pre-restore automatic snapshot...")
            pre_restore = self.snapshot_manager.create_automatic_snapshot(
                target,
                "pre-restore",
                f"Automatic snapshot before restoring {snapshot_id}",
                [Path(item.source_path) for item in manifest.files],
            )
            if progress:
                progress(f"Restore progress: pre-restore snapshot {pre_restore.id} created; restoring files...")
            restored = self.snapshot_manager.restore_snapshot(target, snapshot_id)
            self.quota_buffer.clear(target)
            self.audit_log.append("snapshot.restore", target, "succeeded", f"Restored snapshot {snapshot_id}", snapshot_id, affected_file_count=len(restored.files), affected_bytes=restored.size_bytes)
            return restored
        except Exception as ex:
            self.audit_log.append("snapshot.restore", target, "failed", f"Restore snapshot {snapshot_id} failed", snapshot_id, error_message=str(ex))
            raise
        finally:
            if desktop and desktop.was_closed:
                self.desktop_controller.try_restart()

    def delete_snapshot(self, target: CodexHomeTarget, snapshot_id: str) -> RestorePointManifest:
        self.audit_log.append("snapshot.delete", target, "started", f"Delete snapshot {snapshot_id}", snapshot_id)
        try:
            manifest = self.snapshot_manager.delete_snapshot(target, snapshot_id)
            self.audit_log.append("snapshot.delete", target, "succeeded", f"Deleted snapshot {snapshot_id}", snapshot_id, affected_file_count=len(manifest.files), affected_bytes=manifest.size_bytes)
            return manifest
        except Exception as ex:
            self.audit_log.append("snapshot.delete", target, "failed", f"Delete snapshot {snapshot_id} failed", snapshot_id, error_message=str(ex))
            raise

    def snapshot_folder(self, target: CodexHomeTarget) -> Path:
        return self.snapshot_manager.snapshot_folder(target)

    def snapshot_write_preview(self, target: CodexHomeTarget, operation: str = "Create Snapshot") -> WritePreview:
        return WritePreviewBuilder.for_files(operation, target, self.paths.codex_home(target), self.snapshot_manager.full_state_files(target), "Full runtime state snapshot; secrets are redacted in this preview.")

    def restore_snapshot_preview(self, target: CodexHomeTarget, snapshot_id: str) -> WritePreview:
        manifest = self.snapshot_manager.manager_for(target)._find(snapshot_id, None)
        return WritePreviewBuilder.for_files("Restore Snapshot", target, self.paths.codex_home(target), [Path(item.source_path) for item in manifest.files], f"Restore snapshot {snapshot_id}.")

    def sync_sandbox_to_real_preview(self, sync_sessions: bool = True, sync_assets: bool = True) -> WritePreview:
        return self.session_sync.preview(sync_sessions=sync_sessions, sync_assets=sync_assets)

    def sync_sandbox_sessions_to_real(
        self,
        progress=None,
        sync_sessions: bool = True,
        sync_assets: bool = True,
    ) -> SandboxRealSessionSyncResult:
        if not sync_sessions and not sync_assets:
            raise ValueError("Select at least one Sandbox to Real sync option.")
        sections = self._sync_sections_label(sync_sessions, sync_assets)
        self.audit_log.append("session.sync", CodexHomeTarget.REAL, "started", f"Sync sandbox {sections} to real")
        if progress:
            progress("Sync progress: asking Codex Desktop to close gently...")
        desktop = self.desktop_controller.prepare_for_real_switch()
        if not desktop.can_proceed:
            self.audit_log.append("session.sync", CodexHomeTarget.REAL, "failed", "Sync blocked by Codex Desktop", error_message=desktop.message)
            raise RuntimeError(desktop.message)
        try:
            provider = self._active_provider(CodexHomeTarget.REAL) if sync_sessions else "openai"
            result = self.session_sync.sync(provider, progress=progress, sync_sessions=sync_sessions, sync_assets=sync_assets)
            self.audit_log.append(
                "session.sync",
                CodexHomeTarget.REAL,
                "succeeded",
                f"Synced sandbox {sections} to real",
                result.restore_point.id,
                affected_file_count=result.copied_files + result.overwritten_files + result.asset_copied_files + result.asset_overwritten_files,
                affected_bytes=result.restore_point.size_bytes,
            )
            return result
        except Exception as ex:
            self.audit_log.append("session.sync", CodexHomeTarget.REAL, "failed", f"Sync sandbox {sections} to real failed", error_message=str(ex))
            raise
        finally:
            if desktop.was_closed:
                self.desktop_controller.try_restart()

    @staticmethod
    def _sync_sections_label(sync_sessions: bool, sync_assets: bool) -> str:
        if sync_sessions and sync_assets:
            return "sessions/assets"
        if sync_sessions:
            return "sessions"
        if sync_assets:
            return "assets"
        return "nothing"

    def recent_audit_events(self, limit: int = 20) -> list[dict]:
        return self.audit_log.recent(limit)

    def _active_provider(self, target: CodexHomeTarget) -> str:
        config_path = self.paths.config_path(target)
        config = config_path.read_bytes() if config_path.exists() else None
        return RuntimeConfig.parse(config).provider_id or "openai"

    def dispose(self) -> None:
        self.rpc_client.dispose()
        self.session_manager.dispose()
        for manager in list(self._sessions_managers.values()):
            try:
                manager.close()
            except Exception:
                pass
        self._sessions_managers.clear()

    @staticmethod
    def _friendly_quota_error(exception: Exception) -> str:
        message = str(exception).strip()
        if "pipe" in message.lower() or "管道" in message:
            return "Codex app-server closed the pipe before returning quota data. Check the active Codex home, sign in again if needed, then refresh."
        return message or "Quota refresh failed."
