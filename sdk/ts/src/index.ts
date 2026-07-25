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
 * Types are generated from the API's OpenAPI schema — run `npm run generate` against a running platform.
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
