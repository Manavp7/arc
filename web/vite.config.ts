import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

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
    plugins: [react()],
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
            if (id.includes("maplibre-gl") || id.includes("@deck.gl") || id.includes("@luma.gl")) {
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
