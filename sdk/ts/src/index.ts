/**
 * TypeScript client for the Spatial Intelligence OS (PRD M22).
 *
 *     import { SioClient } from "@sio/sdk";
 *
 *     const sio = new SioClient();
 *     for (const entity of await sio.entities({ limit: 10 })) {
 *       console.log(entity.label, entity.state.zone_id);
 *     }
 *
 * Query schemas are generated from OpenAPI; response contracts are shared with the console.
 * See `docs/SDK.md`.
 */

export { SioApiError, SioClient } from "./client.ts";
export type {
  Alert,
  CopilotAnswer,
  Decision,
  Entity,
  Event,
  SioClientOptions,
  StreamMessage,
} from "./client.ts";
export type { components, operations, paths } from "./generated/api.d.ts";

export { readSse } from "./sse.ts";
export type { SseFrame } from "./sse.ts";
export type { Forecast, Mission, Zone, ReplayPlan, ReplayFrame, Explanation, HealthStatus, SioEvent } from "./contracts.ts";
