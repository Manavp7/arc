/**
 * The TypeScript quickstart (PRD M22, Phase 6).
 *
 *   cd sdk/ts && npx tsx examples/quickstart.mts
 *
 * Needs a running platform. `.mts` rather than `.ts` because this uses top-level await, which needs ESM — a
 * `.ts` file gets transformed as CJS by default and fails with six confusing errors about await.
 */
import { SioApiError, SioClient } from "../src/index.ts";

const sio = new SioClient({ subject: "ts-quickstart", roles: ["operator", "commander"], clearance: 2 });

console.log("token:", (await sio.authenticate()).slice(0, 24), "...");

// Typed from the generated schema: `entity.state.zone_id` is checked at build time, so a renamed field is a
// compile error rather than a blank panel.
const entities = await sio.entities({ limit: 200 });
const byType = new Map<string, number>();
for (const entity of entities) byType.set(String(entity.type), (byType.get(String(entity.type)) ?? 0) + 1);
console.log(`\n${entities.length} entities:`, Object.fromEntries(byType));
const first = entities[0];
if (first) console.log(`  e.g. ${first.label} (${first.type}) in ${first.state.zone_id ?? "no zone"}`);

const alerts = await sio.alerts({ limit: 3 });
console.log(`\n${alerts.length} alerts, ranked:`);
for (const alert of alerts) console.log(`  ${alert.score.toFixed(1)}  ${alert.title.slice(0, 54)}`);

const events = await sio.events({ limit: 3 });
console.log(`\n${events.length} events:`);
for (const event of events) {
  console.log(`  ${event.severity} ${event.type}: ${event.explanation.summary.slice(0, 48)}`);
}

// The permission gate is policy, not a UI affordance. This client has commander, so the failure is a 404
// (no such decision) rather than a 403 — which is the point.
try {
  await sio.approve("dec_does_not_exist");
} catch (error) {
  if (error instanceof SioApiError) {
    const verdict = error.isPermissionError ? "refused by policy" : "reached the service";
    console.log(`\napproval attempt ${verdict}: ${error.detail.slice(0, 60)}`);
  }
}

console.log("\nstreaming three live messages (the part OpenAPI cannot generate):");
let seen = 0;
for await (const message of sio.subscribe("events", "alerts")) {
  console.log(`  ${message.kind}: ${String(message.payload.type ?? message.payload.title).slice(0, 50)}`);
  if (++seen >= 3) break;
}

console.log("\ndone.");
