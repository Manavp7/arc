/** Latest interaction owns the UI; cancellation and generation checks cover delayed fetches. */
export class LatestOperation {
  private revision = 0;
  private active: AbortController | null = null;
  cancel(): void { this.revision += 1; this.active?.abort(); this.active = null; }
  begin(): { signal: AbortSignal; isCurrent: () => boolean } {
    this.cancel();
    const revision = this.revision;
    this.active = new AbortController();
    const signal = this.active.signal;
    return { signal, isCurrent: () => revision === this.revision && !signal.aborted };
  }
}
