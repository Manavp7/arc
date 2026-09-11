/** A successful snapshot can be newer than the most recent push; transport heartbeats aren't data. */
export function latestTimestamp(...values: (string | null | undefined)[]): string | null {
  let latest: string | null = null;
  let latestMs = -Infinity;
  for (const value of values) {
    if (!value) continue;
    const time = Date.parse(value);
    if (Number.isFinite(time) && time > latestMs) { latest = value; latestMs = time; }
  }
  return latest;
}
