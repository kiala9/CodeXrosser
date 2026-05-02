# Vendored CodexMM

> **Superseded.** As of 2026-04-30 the in-app Sessions tab in
> `src/CodexQuotaViewerWindows.Qt/codex_quota_viewer/sessions/` replaces this
> bundled Node service. The vendored copy is retained as a historical
> reference and as a short-term rollback option. Build with
> `publish.ps1 -IncludeLegacySessionManager` only if you explicitly need to
> ship the Node bundle.

This directory is a vendored snapshot of the `CodexMM` repository, kept in a
subtree-friendly layout so future syncs can overwrite the directory in place.

## Source snapshot

- Upstream repository path during vendoring:
  local `CodexMM` checkout
- Upstream HEAD at vendoring time:
  `fa9a4fafa6b3325d5ea3d4b721ea4ee51fce4ab8`
- Snapshot date:
  `2026-03-30 15:18:38 +0800`
- Important note:
  the source repository had local uncommitted changes when this snapshot was
  copied, so the vendored contents may not match the upstream HEAD commit
  exactly.

## Recommended sync workflow

1. Review the source repository state and decide whether you want a clean
   commit snapshot or the current working tree.
2. From the `CodexQuotaViewer` repository root, run:

   ```bash
   rsync -a --delete \
     --exclude '.git' \
     --exclude 'node_modules' \
     --exclude 'dist' \
     --exclude '.DS_Store' \
     /path/to/CodexMM/ Vendor/CodexMM/
   ```

3. Rebuild the bundled session manager with `./scripts/build-app.sh`.
4. Re-run the relevant `Vendor/CodexMM` tests and the `CodexQuotaViewer` app
   bundle build before shipping.
