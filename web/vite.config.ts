import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import cesium from "vite-plugin-cesium";

/**
 * Vite configuration.
 *
 * The dev server proxies `/api`, `/stream` and `/media` to the SIO API rather than having the
 * frontend talk to `http://127.0.0.1:8000` directly. That keeps every request same-origin in
 * development, which means no CORS special-casing, cookies/auth headers behave exactly as they
 * will in production, and Server-Sent Events work without preflight surprises.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.SIO_API_BASE_URL ?? "http://127.0.0.1:8000";
  const webPort = Number(env.SIO_WEB_PORT ?? 5173);

  return {
    // `cesium()` copies Cesium's Workers, Assets, Widgets and ThirdParty directories into the build and
    // defines CESIUM_BASE_URL. Without it Cesium's JS loads fine and its web workers 404 at runtime, which
    // presents as a viewer rendering a black rectangle and logging nothing that names the cause.
    //
    // `rebuildCesium: true` IS LOAD-BEARING, and the default is a trap. In its default mode the plugin
    // copies a prebuilt Cesium and injects `<script src="/cesium/Cesium.js">` into index.html — a blocking,
    // non-module script on EVERY page view, plus 14MB of assets. That is strictly worse than bundling: it
    // defeats the React.lazy boundary completely, and it does so silently, while the build output looks
    // better because the async chunk shrinks from 4.5MB to 62kB.
    //
    // I nearly shipped that. The build log read like a win. Checking dist/index.html for the script tag is
    // what caught it — the same class of mistake as a smaller bundle that loads more bytes.
    //
    // With `rebuildCesium`, Cesium is compiled into the graph, the only import is TwinPanel's dynamic one, and
    // Rollup emits it as an async chunk that arrives when somebody opens the 3D tab and never otherwise.
    plugins: [react(), cesium({ rebuildCesium: true })],
    server: {
      port: webPort,
      strictPort: true,
      // Bind both stacks. Vite's default binds IPv6 loopback only on this image, so
      // http://127.0.0.1:5173 is REFUSED while http://localhost:5173 works — and every document here,
      // plus `just doctor` and the browser tests, uses the numeric form. A URL in the README that does
      // not open is indistinguishable from a broken build.
      host: true,
      proxy: {
        "/api": { target: apiTarget, changeOrigin: true },
        // The dev token issuer. Without this the console cannot authenticate at all — it would request
        // /auth/dev/token from vite, get a 404 and an HTML body, and report "could not obtain a token"
        // while the API sat there perfectly willing to issue one.
        "/auth": { target: apiTarget, changeOrigin: true },
        "/graphql": { target: apiTarget, changeOrigin: true },
        "/media": { target: apiTarget, changeOrigin: true },
        // SSE must not be buffered: the live map depends on events arriving as they happen.
        "/stream": { target: apiTarget, changeOrigin: true, ws: false },
        "/ws": { target: apiTarget, ws: true },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
      // The `geo` chunk (MapLibre + deck.gl + luma.gl) is ~1.7 MB raw / ~475 kB gzipped and
      // there is no way around that for a WebGL mapping stack. It is deliberately isolated so it
      // caches across deploys, so warning about it on every build would just train us to ignore
      // build output. The app chunk is what we actually watch, and it is ~200 kB.
      chunkSizeWarningLimit: 2000,
      rollupOptions: {
        output: {
          // Split the heavy geospatial stack out of the app bundle: MapLibre and deck.gl are
          // ~1 MB together and change far less often than application code, so caching them
          // separately keeps redeploys cheap.
          //
          // Written as a function rather than the object form because Rollup 5 (Vite 8) only
          // accepts `manualChunks` as a function.
          manualChunks(id: string) {
            // Cesium is NOT chunked here, deliberately. `manualChunks` would pull it into a chunk the entry
            // graph references, defeating the `React.lazy` boundary in TwinPanel — the whole point of which
            // is that ~3MB never arrives for the operators who never open the 3D tab. Rollup already emits it
            // as its own async chunk because the only import is dynamic; the correct action is to leave it
            // alone.
            if (
              id.includes("maplibre-gl") ||
              id.includes("@deck.gl") ||
              id.includes("@luma.gl")
            ) {
              return "geo";
            }
            if (id.includes("recharts") || id.includes("d3-")) {
              return "charts";
            }
            return undefined;
          },
        },
      },
    },
    define: {
      __SIO_BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    },
  };
});
