import { useEffect } from "react";

/** Every editor reports to the same navigation guard and protects browser reloads. */
export function useUnsavedWork(dirty: boolean, onDirtyChange?: (dirty: boolean) => void) {
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);
}
