"""Compute the parser fingerprint at build time.

``scripts/publish.ps1`` calls this right before PyInstaller bundling.
The output JSON is then passed to PyInstaller via ``--add-data`` so it
ends up next to ``jsonl_parser.py`` inside the frozen bundle. The
runtime ``_compute_parser_version`` reads it whenever ``sys.frozen``
is true, giving installer releases a deterministic per-build
``parser_version`` without anyone having to bump ``_PARSER_BUMP``.

Reuses ``_compute_parser_version_from_source`` from the runtime so the
build-time and dev-mode hashes can never drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "CodexQuotaViewerWindows.Qt"))

from codex_quota_viewer.sessions.jsonl_parser import (  # noqa: E402
    _compute_parser_version_from_source,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path where parser_fingerprint.json should be written.",
    )
    args = parser.parse_args(argv)

    parser_version = _compute_parser_version_from_source()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"parser_version": parser_version}),
        encoding="utf-8",
    )
    print(f"parser_version={parser_version} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
