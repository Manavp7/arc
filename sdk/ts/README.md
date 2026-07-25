# @sio/sdk

TypeScript client for the Spatial Intelligence OS.

```ts
import { SioClient } from "@sio/sdk";

const sio = new SioClient();
for (const entity of await sio.entities({ limit: 10 })) {
  console.log(entity.label, entity.state.zone_id);
}

for await (const message of sio.subscribe("events", "alerts")) {
  console.log(message.kind, message.payload);
}
```

```bash
npx tsx examples/quickstart.mts     # against a running platform
npm run generate                    # regenerate types from the live API
npm run typecheck
```

## Types are generated, not written

`src/generated/api.d.ts` comes from the API's own OpenAPI schema, which comes from the Pydantic models the
services serialise. So a field rename changes both clients at once, or one of them fails to build.

This platform shipped the alternative: a hand-written type describing what its author *believed* the API
returned. The API changed, the console read a field that no longer existed, and **nothing failed** — `undefined`
renders as nothing rather than as an error. A silently blank panel is the worst failure mode available, because
it looks like "no data" rather than "wrong code".

Building this client found that **44 of the API's 50 routes published no response type at all**: the gateway
forwards, and a forwarded body has no declared schema, so `components["schemas"]["Alert"]` did not exist. Any
generated client in any language got `unknown` for almost every endpoint. `AlertsResponse` and
`DecisionsResponse` now publish those two, declared for documentation only — `response_model` would *filter* the
forwarded body to the declared fields, silently dropping anything a service added, which is the very bug the
schema exists to prevent.

## `subscribe()` is hand-written, because SSE is not in OpenAPI

And it is where the interesting bug lives. `EventSource` is the obvious tool and it is wrong twice over:

1. **It cannot send an `Authorization` header.** That is why the browser console authenticates its stream by
   cookie, and why a client holding a bearer token cannot use `EventSource` at all.
2. **`EventSource.onmessage` fires only for frames with no `event:` name.** A reader handling only `onmessage`
   receives *nothing* from a server that names its frames. This platform's console shipped exactly that, and it
   presented as a live map that never updated — no error, no warning, just an empty map.

The reader here handles both named and unnamed frames, splits on newlines while keeping the trailing partial
line (a chunk boundary lands mid-line often enough that not doing so produces intermittent parse failures that
look random), skips a malformed frame rather than throwing (a caller inside `for await` cannot recover from an
exception mid-loop), and reconnects with backoff.

## Tokens

Obtained, reused, renewed on a 401 **once** — not in a loop, because a misconfigured secret would otherwise
become a request storm against the token endpoint. The in-flight promise is shared, so five concurrent requests
on a cold client mint one token rather than making the audit trail read as five sign-ins.

Pass `{ token }` in a Keycloak deployment, where the dev issuer is disabled by design.

## Not published to npm

Deliberate: publishing a package means owning its versioning against an API that is still moving. It is consumed
from the workspace, and `npm run generate` regenerates against whatever version you are running — which is more
honest than a version number that claims compatibility.
