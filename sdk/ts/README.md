# @sio/sdk

Workspace TypeScript client for the Spatial Intelligence OS API.

```ts
import { SioClient } from "@sio/sdk";

// Local development: the API must explicitly enable its development issuer.
const sio = new SioClient();
for (const entity of await sio.entities({ limit: 10 })) {
  console.log(entity.label, entity.state.zone_id);
}
for await (const message of sio.subscribe("events", "alerts")) {
  console.log(message.kind, message.payload);
}
```

For a deployment with an identity provider, pass a bearer token or a renewal callback:

```ts
const sio = new SioClient({
  url: "https://your-sio-api.example",
  tokenProvider: async (forceRefresh) => identityProvider.accessToken({ forceRefresh }),
});
```

`tokenProvider` owns sign-in and renewal. The client requests one refresh after a 401. A supplied static
`token` that receives 401 fails instead of silently switching to a development identity. API refusals keep
their structured `detail`, including explanations and corrective steps. Successful 204 responses return
`undefined`.

The console uses this HTTP client, `contracts.ts` response types, and the `sse.ts` frame parser. Browser
sign-in is implemented by the console: explicit development identities or authorization-code PKCE with a
configured OIDC provider. Access and refresh tokens stay in tab-scoped session storage and are transmitted
as bearer headers. Streams use `fetch`; they do not require a JavaScript-written credential cookie.

`src/generated/api.d.ts` contains OpenAPI query/input schemas. `src/contracts.ts` contains shared serialized
response shapes, including defaults materialized by Python models. Some forwarded API endpoints still have
incomplete OpenAPI response declarations; sharing types prevents console/SDK duplication, but does not
replace runtime contract validation.

```bash
npx tsx examples/quickstart.mts      # against a running platform
npm run generate                    # refresh OpenAPI declarations
npm run typecheck
cd ../../web && npm test            # shared transport, auth and replay regression tests
```

`subscribe()` handles named and unnamed SSE events, multiline data, CRLF and chunk boundaries. It reconnects
with bounded backoff and skips malformed JSON frames. The browser additionally reloads REST snapshots on
connection and renews long-running streams before token expiry.

This package is consumed from the workspace and is not published to npm.
