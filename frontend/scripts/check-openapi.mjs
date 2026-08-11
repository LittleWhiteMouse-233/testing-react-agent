import { readFileSync, unlinkSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const input = fileURLToPath(new URL("../src/api/openapi.json", import.meta.url));
const checkedIn = fileURLToPath(new URL("../src/api/schema.d.ts", import.meta.url));
const temporary = fileURLToPath(new URL("../src/api/.schema.check.d.ts", import.meta.url));
const executable = fileURLToPath(
  new URL(
    process.platform === "win32"
      ? "../node_modules/.bin/openapi-typescript.cmd"
      : "../node_modules/.bin/openapi-typescript",
    import.meta.url
  )
);

try {
  const result = spawnSync(executable, [input, "-o", temporary], {
    cwd: root,
    encoding: "utf8",
    shell: process.platform === "win32"
  });
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout);
    process.exit(result.status ?? 1);
  }
  if (readFileSync(checkedIn, "utf8") !== readFileSync(temporary, "utf8")) {
    process.stderr.write(
      "Generated API types are stale. Run `npm run generate:api`.\n"
    );
    process.exitCode = 1;
  }
} finally {
  try {
    unlinkSync(temporary);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}
