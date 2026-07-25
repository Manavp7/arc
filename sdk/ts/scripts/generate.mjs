#!/usr/bin/env node
/**
 * Regenerate `src/generated/api.d.ts` from the running API's OpenAPI schema.
 *
 * Generation rather than hand-writing, and the reason is a bug this platform actually shipped: a hand-written
 * TypeScript type described what its author BELIEVED the API returned, the API changed, and the console read a
 * field that no longer existed — silently, because `undefined` renders as nothing rather than as an error.
 *
 *   node scripts/generate.mjs                       # against a running API on :8000
 *   node scripts/generate.mjs ../../openapi.json    # against a saved schema
 *
 * The schema is generated from the same Pydantic models the Python SDK returns, so the two clients cannot
 * disagree about a field name without one of them failing to build.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../src/generated/api.d.ts");
const source = process.argv[2] ?? process.env.SIO_OPENAPI_URL ?? "http://127.0.0.1:8000/openapi.json";

mkdirSync(dirname(out), { recursive: true });

if (!source.startsWith("http") && !existsSync(source)) {
  console.error(`no such schema: ${source}`);
  console.error("Start the platform (just services && just dev) or pass a saved openapi.json.");
  process.exit(1);
}

console.log(`generating from ${source}`);
execFileSync("npx", ["--yes", "openapi-typescript", source, "-o", out], { stdio: "inherit" });
console.log(`wrote ${out}`);
