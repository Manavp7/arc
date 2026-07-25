/**
 * Analytics in the console (PRD M19, Phase 6).
 *
 * The plan's acceptance for P6.3 is "analytics views populated **in-app**", and until this existed the service
 * was verified only by curl. That distinction matters more than it sounds: the whole argument for computing
 * distribution *shape* on the server is that a reader cannot infer it from a chart, and nobody was reading it.
 *
 * Three decisions carried over from the service, because a UI can undo a server-side design silently:
 *
 * **The risk score never appears without its drivers.** A single number is the most misusable output in a
 * platform like this — it ends up on a wall — and the service goes to trouble to decompose it. Rendering just
 * the score would throw that away and there would be no error to notice.
 *
 * **The shape sentence is rendered as prominently as the histogram.** "p95 is 25x the median, so the mean
 * describes almost nobody" is the finding; the bars are the evidence for it. A UI that shows the bars and
 * buries the sentence has inverted them.
 *
 * **Suppressed heatmap cells are stated, not hidden.** A heatmap that quietly drops 40 % of its data looks
 * like a quiet site, and the operator has no way to tell.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";

interface Bucket {
  from: number;
  to: number | null;
  count: number;
  share: number;
}

interface DistributionView {
  count: number;
  mean: number;
  percentiles: Record<string, number>;
  histogram: Bucket[];
  shape: string;
}

interface Summary {
  window_hours: number;
  generated_at: string;
  counts: Record<string, number>;
  dwell: { overall: DistributionView; by_zone: Record<string, DistributionView>; open_visits_excluded: number };
  throughput: { totals: Record<string, number>; entries_per_hour: number; smoothing: string };
  utilisation: { zones: Array<{ zone_id: string; visits: number; utilisation: number }> };
  risk: {
    score: number;
    band: string;
    drivers: string[];
    formula: string;
    terms: Record<string, { normalised: number; weight: number; contributes: number; why: string }>;
  };
}

const REFRESH_MS = 30_000;
const WINDOWS = [1, 6, 24, 168];

export function AnalyticsPanel() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [hours, setHours] = useState(24);
  const [error, setError] = useState<string | null>(null);
  const [showTerms, setShowTerms] = useState(false);
  const [report, setReport] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSummary(await api.analyticsSummary(hours));
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [hours]);

  useEffect(() => {
    void load();
    // Slower than the other panels on purpose: these are aggregates over a window, and a number that changes
    // every five seconds invites the reader to watch it rather than to think about it.
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (error) {
    return (
      <div className="panel-body">
        <p className="panel-error">{error}</p>
        <p className="panel-empty">
          The analytics service runs on :8117. If it is not up, `just dev` starts it with the stack.
        </p>
      </div>
    );
  }
  if (!summary) {
    return (
      <div className="panel-body">
        <p className="panel-empty">Computing aggregates over the last {hours}h…</p>
      </div>
    );
  }

  const dwell = summary.dwell.overall;
  const maxBucket = Math.max(1, ...dwell.histogram.map((bucket) => bucket.count));

  return (
    <div className="panel-body analytics-panel">
      <div className="analytics-head">
        <div className="analytics-windows">
          {WINDOWS.map((option) => (
            <button
              key={option}
              type="button"
              className={option === hours ? "window-btn window-btn-active" : "window-btn"}
              onClick={() => setHours(option)}
            >
              {option >= 24 ? `${option / 24}d` : `${option}h`}
            </button>
          ))}
        </div>
        <span className="analytics-generated">
          as of {new Date(summary.generated_at).toLocaleTimeString([], { hour12: false })}
        </span>
      </div>

      {/* --- risk --------------------------------------------------------------------------- */}
      <section className="analytics-card">
        <header>
          <span className={`risk-score risk-${summary.risk.band}`}>{summary.risk.score}</span>
          <div>
            <div className="risk-band">{summary.risk.band} risk</div>
            <div className="analytics-sub">out of 100</div>
          </div>
        </header>
        {/* The drivers, always. The service decomposes the score precisely so this never has to be a bare
            number, and rendering only the number would discard that with nothing to notice. */}
        <ul className="risk-drivers">
          {summary.risk.drivers.map((driver, index) => (
            <li key={index}>{driver}</li>
          ))}
        </ul>
        <button type="button" className="link-btn" onClick={() => setShowTerms(!showTerms)}>
          {showTerms ? "hide the arithmetic" : "how is this calculated?"}
        </button>
        {showTerms && (
          <div className="risk-terms">
            <table>
              <thead>
                <tr>
                  <th>term</th>
                  <th>value</th>
                  <th>weight</th>
                  <th>adds</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(summary.risk.terms)
                  .sort((a, b) => b[1].contributes - a[1].contributes)
                  .map(([name, term]) => (
                    <tr key={name}>
                      <td title={term.why}>{name.replace(/_/g, " ")}</td>
                      <td>{term.normalised.toFixed(2)}</td>
                      <td>{term.weight}</td>
                      <td>{term.contributes.toFixed(1)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
            <p className="analytics-note">
              Every term is normalised to 0–1 before weighting, so a three-zone site and a thirty-zone site
              score comparably. {summary.risk.formula}
            </p>
          </div>
        )}
      </section>

      {/* --- dwell -------------------------------------------------------------------------- */}
      <section className="analytics-card">
        <header>
          <h4>Dwell time</h4>
          <span className="analytics-sub">
            {dwell.count} completed visits · median {dwell.percentiles.p50?.toFixed(1)} min · p95{" "}
            {dwell.percentiles.p95?.toFixed(1)} min
          </span>
        </header>
        {dwell.count === 0 ? (
          <p className="panel-empty">No completed visits in this window.</p>
        ) : (
          <>
            {/* The finding, above the evidence for it. The bars show the distribution; this sentence says
                what it means, and it is the part a reader cannot get by looking at bars. */}
            <p className="analytics-shape">{dwell.shape}</p>
            <div className="histogram">
              {dwell.histogram.map((bucket, index) => (
                <div key={index} className="histogram-row">
                  <span className="histogram-label">
                    {bucket.from}–{bucket.to === null ? "∞" : bucket.to}
                  </span>
                  <div className="histogram-track">
                    <div
                      className="histogram-bar"
                      style={{ width: `${(bucket.count / maxBucket) * 100}%` }}
                    />
                  </div>
                  <span className="histogram-count">{bucket.count}</span>
                  <span className="histogram-share">{(bucket.share * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
            {summary.dwell.open_visits_excluded > 0 && (
              <p className="analytics-note">
                {summary.dwell.open_visits_excluded} visit(s) still in progress are excluded: an open visit has
                no duration yet, and including &ldquo;so far&rdquo; would make this depend on when you looked.
              </p>
            )}
          </>
        )}
      </section>

      {/* --- throughput and utilisation ----------------------------------------------------- */}
      <section className="analytics-card">
        <header>
          <h4>Throughput</h4>
          <span className="analytics-sub">{summary.throughput.entries_per_hour} zone entries/hour</span>
        </header>
        {Object.keys(summary.throughput.totals).length === 0 ? (
          <p className="panel-empty">No zone entries in this window.</p>
        ) : (
          <ul className="analytics-bars">
            {Object.entries(summary.throughput.totals)
              .slice(0, 6)
              .map(([zone, total], _index, entries) => (
                <li key={zone}>
                  <span className="bar-label">{zone}</span>
                  <div className="bar-track">
                    <div
                      className="bar-fill"
                      style={{ width: `${(total / Math.max(...entries.map((e) => e[1]))) * 100}%` }}
                    />
                  </div>
                  <span className="bar-value">{total}</span>
                </li>
              ))}
          </ul>
        )}
      </section>

      <section className="analytics-card">
        <header>
          <h4>Zone utilisation</h4>
          <span className="analytics-sub">share of the window occupied</span>
        </header>
        {summary.utilisation.zones.length === 0 ? (
          <p className="panel-empty">No zone occupancy in this window.</p>
        ) : (
          <ul className="analytics-bars">
            {summary.utilisation.zones.slice(0, 8).map((zone) => (
              <li key={zone.zone_id}>
                <span className="bar-label">{zone.zone_id}</span>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${zone.utilisation * 100}%` }} />
                </div>
                <span className="bar-value">{(zone.utilisation * 100).toFixed(0)}%</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* --- the report --------------------------------------------------------------------- */}
      <section className="analytics-card">
        <header>
          <h4>Report</h4>
        </header>
        {report === null ? (
          <button
            type="button"
            className="drawer-btn"
            onClick={async () => {
              try {
                setReport((await api.analyticsReport(hours)).markdown);
              } catch (cause) {
                setError(cause instanceof Error ? cause.message : String(cause));
              }
            }}
          >
            generate for the last {hours >= 24 ? `${hours / 24}d` : `${hours}h`}
          </button>
        ) : (
          <>
            {/* Shown as raw Markdown on purpose. It is meant to be COPIED — into a ticket, an email, a
                handover note — and rendering it here would make the reader retype what they came for. */}
            <textarea className="analytics-report" readOnly value={report} rows={14} />
            <button type="button" className="link-btn" onClick={() => setReport(null)}>
              close
            </button>
          </>
        )}
      </section>
    </div>
  );
}
