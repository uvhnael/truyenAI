import { cpSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
const root = process.cwd();
const standalone = resolve(root, ".next/standalone");
if (!existsSync(resolve(standalone, "server.js"))) { console.error("Run npm run build before npm start."); process.exit(1); }
cpSync(resolve(root, ".next/static"), resolve(standalone, ".next/static"), { recursive: true });
cpSync(resolve(root, "public"), resolve(standalone, "public"), { recursive: true });
const portIndex = process.argv.indexOf("--port");
const port = portIndex >= 0 ? process.argv[portIndex + 1] : process.env.PORT || "3000";
const child = spawn(process.execPath, [resolve(standalone, "server.js")], { stdio: "inherit", env: { ...process.env, HOSTNAME: "0.0.0.0", PORT: port }, cwd: standalone });
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => child.kill(signal));
child.on("exit", code => process.exit(code ?? 0));
