/**
 * The lazy boundary in front of the 3D twin (PRD M18, Phase 7 P7.2).
 *
 * This file exists so that `TwinView.tsx` — and the ~3MB of Cesium it pulls in — is never in the main bundle.
 * A static `import` anywhere in the module graph would defeat that no matter how the chunking is configured, so
 * the dynamic `import()` below is load-bearing rather than stylistic.
 *
 * It is also where the twin is allowed to fail without taking the console with it. Cesium initialises WebGL,
 * downloads workers, and on a machine with no hardware acceleration it can throw during construction — which
 * without a boundary would blank the entire application, including the 2D map that was working perfectly well.
 * An operator losing their live view because they clicked a 3D toggle is a bad trade at any bundle size.
 */

import { Component, type ReactNode, Suspense, lazy } from "react";

// The dynamic import. Everything Cesium is behind this arrow function and nothing else references TwinView.
const TwinView = lazy(() => import("./TwinView"));

class TwinBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  override state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  override render() {
    if (this.state.error) {
      return (
        <div className="twin-fallback">
          <strong>The 3D twin could not start.</strong>
          {/* The actual message, not "something went wrong". WebGL failures are specific and the text is
              usually the fastest route to the cause — a headless browser, a blocked worker, a driver. */}
          <p className="muted">{this.state.error.message}</p>
          <p className="muted">
            The 2D map is unaffected. 3D needs WebGL 2, which some
            remote-desktop and headless environments do not provide.
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

export function TwinPanel() {
  return (
    <TwinBoundary>
      <Suspense
        fallback={
          <div className="twin-fallback">
            {/* Says WHY it is slow. A spinner with no explanation on a 3MB download reads as a hang, and the
                honest fact — this is big, it is cached afterwards — costs one sentence. */}
            <strong>Loading the 3D engine…</strong>
            <p className="muted">
              Cesium is around 4 MB and is fetched only when you open this tab.
              It is cached afterwards, and the scene takes a few seconds more to
              build once it arrives.
            </p>
          </div>
        }
      >
        <TwinView />
      </Suspense>
    </TwinBoundary>
  );
}
