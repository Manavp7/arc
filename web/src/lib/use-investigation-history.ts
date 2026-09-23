import { useEffect, useRef } from "react";
import { decodeNavigation, encodeNavigation, navigationIdentity, rememberLocation, type InvestigationLocation } from "./investigation-navigation";

/** Time updates replace an entry; record/workspace changes add one browser-history step. */
export function useInvestigationHistory(location: InvestigationLocation, key: string | null,
  restore: (next: InvestigationLocation) => boolean, dirty: boolean, blocked: () => void) {
  const current = useRef(location); current.current = location;
  const handlers = useRef({ restore, dirty, blocked }); handlers.current = { restore, dirty, blocked };
  const index = useRef<number>(Number.isInteger(history.state?.sioNavigationIndex) ? history.state.sioNavigationIndex : 0);
  const started = useRef(false);
  const encoded = encodeNavigation(location);
  useEffect(() => {
    const previous = decodeNavigation(window.location.search);
    const replace = !started.current || (previous && navigationIdentity(previous) === navigationIdentity(location));
    if (window.location.search !== encoded || !started.current) {
      if (!replace) index.current += 1;
      history[replace ? "replaceState" : "pushState"]({ ...history.state, sioNavigationIndex: index.current }, "", `${window.location.pathname}${encoded}`);
    }
    started.current = true;
    rememberLocation(key, location);
  }, [encoded, key]); // encoding is the complete, normalized dependency
  useEffect(() => {
    const pop = (event: PopStateEvent) => {
      const target = decodeNavigation(window.location.search) ?? { tab: "alerts" as const };
      const targetIndex = Number.isInteger(event.state?.sioNavigationIndex) ? event.state.sioNavigationIndex as number : index.current;
      if (encodeNavigation(target) === encodeNavigation(current.current)) { index.current = targetIndex; return; }
      if (handlers.current.dirty || !handlers.current.restore(target)) {
        handlers.current.blocked();
        const delta = index.current - targetIndex;
        if (delta) history.go(delta);
        else history.replaceState({ ...history.state, sioNavigationIndex: index.current }, "", `${window.location.pathname}${encodeNavigation(current.current)}`);
        return;
      }
      index.current = targetIndex;
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
}
