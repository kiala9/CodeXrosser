import path from "node:path";
import type http from "node:http";

import { resolveClientDistPath } from "./runtime-paths";
import { startServer } from "./server";

const port = Number(process.env.PORT ?? 4318);
const codexHome = process.env.CODEX_HOME ?? path.join(process.env.HOME ?? "", ".codex");
const managerHome =
  process.env.CODEX_MANAGER_HOME ??
  path.join(process.env.HOME ?? "", ".codex-session-manager");
const clientDistPath = resolveClientDistPath();
let activeServer: http.Server | undefined;
let keepAliveTimer: ReturnType<typeof setInterval> | undefined;

void startServer({
  port,
  codexHome,
  managerHome,
  clientDistPath,
}).then((server) => {
  activeServer = server;
  keepAliveTimer = setInterval(() => undefined, 60_000);
  console.log(`Codex Session Manager listening on http://127.0.0.1:${port}`);
}).catch((error: unknown) => {
  console.error("Failed to start Codex Session Manager:", error);
  process.exitCode = 1;
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, () => {
    if (keepAliveTimer) {
      clearInterval(keepAliveTimer);
    }

    if (!activeServer) {
      process.exit(0);
    }

    activeServer.close(() => {
      process.exit(0);
    });
  });
}
