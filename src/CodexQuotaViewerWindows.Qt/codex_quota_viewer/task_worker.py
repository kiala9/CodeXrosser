from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .models import CodexHomeTarget, OfficialRepairSummary, SandboxRealSessionSyncResult, SandboxSeedResult, WritePreview
from .services import AppServices


def emit(message_type: str, **payload: Any) -> None:
    payload["type"] = message_type
    # This worker talks to the Qt host through stdout JSON lines. On
    # Chinese Windows builds Python may default stdout to GBK/CP936; session
    # excerpts can contain characters like U+26A0 that GBK cannot encode.
    # Keep the transport ASCII-only and let json.loads on the host restore
    # the original Unicode text.
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


def progress(message: str) -> None:
    emit("progress", message=message)


def serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {key: serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected a boolean value, got {value!r}.")


def sections_label(sync_sessions: bool, sync_assets: bool) -> str:
    if sync_sessions and sync_assets:
        return "sessions/assets"
    if sync_sessions:
        return "sessions"
    if sync_assets:
        return "assets"
    return "nothing"


def seed_sandbox(services: AppServices) -> dict[str, Any]:
    progress("Seed progress: copying real Codex runtime files into Sandbox...")
    result = services.seed_sandbox()
    message = f"Seeded sandbox. Copied {result.copied_files} files."
    if result.skipped_files:
        message += f" Skipped {result.skipped_files} files."
    if result.warnings:
        message += " " + "; ".join(result.warnings[:3])
    return {"result": serialize(result), "message": message}


def repair_target(services: AppServices, target: CodexHomeTarget) -> dict[str, Any]:
    result = services.repair_target(target, progress=progress)
    message = (
        "Repair complete. Provider metadata synced. "
        f"SQLite rows {result.updated_threads}, rollout files {result.updated_session_index_entries}."
    )
    return {"result": serialize(result), "message": message}


def sync_preview(services: AppServices, sync_sessions: bool, sync_assets: bool) -> dict[str, Any]:
    progress(f"Sync progress: scanning Sandbox and Real {sections_label(sync_sessions, sync_assets)} for preview...")
    preview = services.sync_sandbox_to_real_preview(sync_sessions=sync_sessions, sync_assets=sync_assets)
    return {"preview": serialize(preview)}


def sessions_rescan(services: AppServices, target: CodexHomeTarget) -> dict[str, Any]:
    progress(f"Sessions progress: rescanning {target.value} session files...")
    manager = services.sessions_manager(target)

    def on_progress(phase: str, done: int, total: int) -> None:
        # Per-phase tick the Qt host turns into a partial list refresh +
        # status-bar progress text. ``phase`` is "parsing" during the JSONL
        # walk/parse pass and "indexing" during the SQLite commit pass; UI
        # uses it to label the count appropriately. Tagged so the host can
        # route it past the generic textual ``progress`` channel.
        emit(
            "progress",
            kind="sessions-rescan-batch",
            target=target.value,
            phase=phase,
            done=done,
            total=total,
        )

    records = manager.rescan(progress_cb=on_progress)
    payload = [record.to_json() for record in records]
    return {
        "result": {"records": payload, "count": len(payload)},
        "message": f"Rescanned {target.value}. Indexed {len(payload)} session(s).",
    }


def sessions_batch(
    services: AppServices,
    target: CodexHomeTarget,
    action: str,
    session_ids: list[str],
) -> dict[str, Any]:
    progress(
        f"Sessions progress: running {action} on {len(session_ids)} {target.value} session(s)..."
    )
    manager = services.sessions_manager(target)
    if action == "archive":
        result = manager.batch_archive_sessions(session_ids)
    elif action == "trash":
        result = manager.batch_trash_sessions(session_ids)
    elif action == "restore":
        result = manager.batch_restore_sessions(session_ids)
    elif action == "purge":
        result = manager.batch_purge_sessions(session_ids)
    else:
        raise RuntimeError(f"Unknown sessions batch action: {action}")
    payload = {
        "records": [record.to_json() for record in result.records],
        "failures": [
            {
                "sessionId": failure.session_id,
                "error": failure.error,
                "code": failure.code,
                "details": failure.details,
            }
            for failure in result.failures
        ],
    }
    summary = (
        f"{action} on {target.value}: {len(payload['records'])} succeeded, "
        f"{len(payload['failures'])} failed."
    )
    return {"result": payload, "message": summary}


def sync_sandbox_to_real(services: AppServices, sync_sessions: bool, sync_assets: bool) -> dict[str, Any]:
    result = services.sync_sandbox_sessions_to_real(progress=progress, sync_sessions=sync_sessions, sync_assets=sync_assets)
    parts = ["Synced Sandbox to Real."]
    if sync_sessions:
        parts.append(
            f"Sessions copied {result.copied_files}, overwritten {result.overwritten_files}, "
            f"skipped same {result.skipped_same_files}, skipped Real-newer {result.skipped_real_newer_files}."
        )
    if sync_assets:
        parts.append(
            f"Assets copied {result.asset_copied_files}, overwritten {result.asset_overwritten_files}, "
            f"skipped same {result.asset_skipped_same_files}, skipped Real-newer {result.asset_skipped_real_newer_files}."
        )
    parts.append(f"Snapshot {result.restore_point.id}.")
    message = " ".join(parts)
    return {"result": serialize(result), "message": message}


def run_operation(args: argparse.Namespace, services_factory: Callable[[], AppServices] = AppServices) -> dict[str, Any]:
    services = services_factory()
    try:
        if args.operation == "seed-sandbox":
            return seed_sandbox(services)
        if args.operation == "repair":
            return repair_target(services, CodexHomeTarget.from_json(args.target))
        if args.operation == "sync-preview":
            return sync_preview(services, args.sync_sessions, args.sync_assets)
        if args.operation == "sync-sandbox-to-real":
            return sync_sandbox_to_real(services, args.sync_sessions, args.sync_assets)
        if args.operation == "sessions-rescan":
            return sessions_rescan(services, CodexHomeTarget.from_json(args.target))
        if args.operation == "sessions-batch":
            return sessions_batch(
                services,
                CodexHomeTarget.from_json(args.target),
                args.batch_action,
                list(args.session_ids or []),
            )
        raise RuntimeError(f"Unknown worker operation: {args.operation}")
    finally:
        services.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_quota_viewer.task_worker")
    parser.add_argument(
        "operation",
        choices=[
            "seed-sandbox",
            "repair",
            "sync-preview",
            "sync-sandbox-to-real",
            "sessions-rescan",
            "sessions-batch",
        ],
    )
    parser.add_argument("--target", default=CodexHomeTarget.SANDBOX.value)
    parser.add_argument("--sync-sessions", type=parse_bool, default=True)
    parser.add_argument("--sync-assets", type=parse_bool, default=True)
    parser.add_argument(
        "--batch-action",
        choices=["archive", "trash", "restore", "purge"],
        default="archive",
    )
    parser.add_argument("--session-ids", nargs="*", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        emit("result", **run_operation(args))
        return 0
    except Exception as ex:
        emit("error", message=str(ex), traceback="".join(traceback.format_exception(ex)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
