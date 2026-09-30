#!/usr/bin/env node
/** Generate SDK types from the saved API schema, or an explicitly supplied file/URL.
 * Export without services: uv run python sdk/ts/scripts/export_openapi.py
 * Generate: npm run generate --prefix sdk/ts
 * Verify without rewriting: npm run generate:check --prefix sdk/ts
 * Install the locked development dependencies first with npm ci --prefix sdk/ts.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../src/generated/api.d.ts");
const args = process.argv.slice(2);
const check = args.includes("--check");
const positional = args.filter(value => value !== "--check");
if (positional.length > 1 || positional.some(value => value.startsWith("--"))) {
  console.error("Usage: node scripts/generate.mjs [schema.json|URL] [--check]");
  process.exit(2);
}
const source = positional[0] ?? process.env.SIO_OPENAPI_URL ?? resolve(here, "../openapi.json");
if (!/^https?:\/\//.test(source) && !existsSync(source)) {
  console.error(`No such schema: ${source}. Export it with scripts/export_openapi.py first.`);
  process.exit(1);
}
let generator;
try {
  const require = createRequire(import.meta.url);
  const entry = require.resolve("openapi-typescript");
  generator = resolve(dirname(entry), "../bin/cli.js");
  if (!existsSync(generator)) throw new Error("CLI is unavailable");
} catch {
  console.error("Locked openapi-typescript dependency is missing; run npm ci --prefix sdk/ts.");
  process.exit(1);
}
const temporary = check ? mkdtempSync(join(tmpdir(), "sio-sdk-types-")) : null;
const destination = temporary ? join(temporary, "api.d.ts") : out;
try {
  mkdirSync(dirname(destination), { recursive: true });
  execFileSync(process.execPath, [generator, source, "-o", destination], { stdio: "inherit" });
  if (check) {
    if (!existsSync(out) || readFileSync(out, "utf8") !== readFileSync(destination, "utf8")) {
      console.error("Generated SDK types are stale; run npm run generate --prefix sdk/ts.");
      process.exitCode = 1;
    } else console.log("Generated SDK types are current.");
  } else console.log(`Wrote ${out}`);
} finally {
  if (temporary) rmSync(temporary, { recursive: true, force: true });
}
