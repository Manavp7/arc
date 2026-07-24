# sio-web

The operator console: live map, timeline, copilot, alerts, decisions, missions.

```bash
npm install
npm run dev        # http://localhost:5173, proxied to the API on :8000
npm run check      # tsc --noEmit && vite build  (part of `just check`)
```

## Choices worth knowing

- **MapLibre + deck.gl, not one or the other.** MapLibre owns the basemap and camera; deck.gl
  owns the data layers. The overlay runs in `interleaved: false` mode — sharing a WebGL context
  is prettier for 3D extrusions but makes every entity update a full basemap repaint, which at
  20 updates/second on a laptop is the difference between smooth and juddering.
- **No tile provider by default.** The basemap is a flat dark background plus the site's own
  GeoJSON. No API key to leak, the demo survives an unplugged network, and for a facility twin
  the site outline *is* the map. Set `SIO_BASEMAP_TILES=osm` for city-scale context.
- **Same-origin in development.** Vite proxies `/api`, `/stream`, `/graphql` and `/media` to the
  API, so there is no CORS special-casing and auth headers behave as they will in production.
- **SSE, not WebSockets, for the live feed.** One-directional, proxy-friendly, and the browser
  reconnects on its own; the client adds explicit backoff so a restarting API is not hammered.
- **Zustand with a `Map` of entities.** The stream delivers upserts several times a second; an
  array would mean a linear scan per message, and selector subscriptions keep a position update
  from re-rendering the copilot panel.
- **Bundle split.** `geo` (MapLibre + deck.gl + luma.gl) is ~475 kB gzipped and isolated so it
  caches across deploys. The app chunk is the one to watch: ~65 kB gzipped.

## Layout

```
src/
  App.tsx              shell: map pane, side rail (tabs), timeline strip
  components/          LiveMap (MapLibre + deck.gl layers), panels
  lib/api.ts           typed REST client
  lib/stream.ts        SSE with backoff
  lib/basemap.ts       self-contained style + entity/severity colours
  store.ts             zustand store and selectors
  types.ts             TypeScript mirrors of sio_schemas contracts
```
