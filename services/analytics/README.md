# analytics (M19)

KPIs, heatmaps and reports, computed from the append-only record.

## Nothing here keeps a counter

Every figure is a query over `events`, `entities`, `relationships` and `observations`. A counter drifts the
moment a service restarts and cannot be recomputed for a past window — and "what did last Tuesday look like"
is the question analytics exists to answer. A query can be re-run for any window and gives the same answer
twice.

**No materialised views yet.** The PRD says "materialised rollups", and they are the right answer at a scale
this deployment has not reached. On a yard's worth of data these queries run in single-digit milliseconds, and
a materialised view is a second source of truth that can be stale. Worth adding when a query gets slow, not
before — the refresh policy is the hard part and it should be written against a real problem.

## The distributions matter more than the means

A mean dwell time of 18 minutes describes a yard where every truck takes 18 minutes and a yard where half take
4 and half take 32 **identically** — and those are different sites with different problems.

So dwell is a histogram with percentiles, and every distribution **says what shape it is**:

```
looks bimodal: a gap of 40.0 separates two groups, which usually means two populations
sharing one queue rather than one population with variance

long right tail: p95 (147.0) is 24.5x the median (6.0), so the mean of 30.0 describes almost nobody
```

That sentence is the part a reader cannot get by glancing at a chart. "Bimodal" is actionable; a mean and a
standard deviation leave the reader to notice it themselves, which they will not.

Percentiles use linear interpolation rather than nearest-rank, because nearest-rank makes p95 and p99 identical
on small samples — and a report where two percentiles always agree teaches the reader to ignore both.

Dwell is computed from **closed intervals only**, and the count of open visits is reported rather than dropped.
An open visit has no duration yet; including "so far" would make the distribution depend on when the query ran,
so a report generated twice would disagree with itself.

## The risk index shows its terms

A single risk score is the most misusable output in a platform like this — it will end up on a wall — so the
number never travels without its decomposition, ordered by contribution:

```
54.2 / 100 (elevated)
  unacknowledged ratio: 38 of 40 open alert(s) unacknowledged
  open criticals: 2 critical alert(s) open; 3 or more saturates this term
  blind spot ratio: 4 of 17 zone(s) have no camera covering them
```

**Every term is something the platform observes**, which is the constraint that kept this from becoming
astrology. A score built from an invented "asset criticality tier" would be a number with no way to check it;
these five can each be traced to rows in the database. Terms are normalised to 0–1 before weighting, so a site
with three zones and one with thirty produce comparable numbers.

## Heatmaps aggregate on the server, and the reason is privacy

Not rendering cost. A heatmap built client-side needs every individual position, so the browser receives a
complete movement record for every person on site — which the platform then cannot redact, because redaction
happens at the API boundary and the boundary has already been crossed.

Which has a corollary: **a hexagon containing one person is not an aggregate, it is a location.** Cells with
fewer than five *distinct entities* are withheld — the standard small-cell disclosure control, and the
difference between a heatmap and a surveillance tool. Distinct entities rather than observations, because a
hundred observations of one parked truck is still one truck.

Suppression is **reported**, not silent. A heatmap that quietly drops 40 % of its data looks like a quiet site.

H3 rather than a square grid because hexagons have uniform adjacency, so a nearest-cell question does not
depend on which direction it is asked in. Squares have two neighbour distances and produce artefacts along
diagonals.

Display resolution is 11 (~25 m, roughly a truck bay), not the platform's indexing resolution of 12 (~9 m) —
which is finer than any position is accurate and produces a heatmap of measurement noise. Indexing precision
and display precision are different questions with different answers.

## Reports are Markdown

The PRD says "PDF/Markdown". Markdown is the half worth building first: it diffs, it pastes into a ticket, it
renders in every tool a reader already has, and anything can turn it into a PDF. A PDF generator would add a
rendering dependency to produce a file whose change history nobody can review.

The report is written as narrative rather than a table dump, because a report that only restates the dashboard
has no reason to exist. Risk **drivers come before the formula** — a reader wants to know what is wrong before
how it was arithmetically arrived at.

## Endpoints

| | |
|---|---|
| `GET /analytics/summary` | everything a dashboard needs in one round trip |
| `GET /analytics/dwell` | distribution overall and per zone, with shape |
| `GET /analytics/throughput` | entries per bucket per zone, unsmoothed |
| `GET /analytics/utilisation` | how much of the window each zone was occupied |
| `GET /analytics/heatmap` | H3 cells with small cells suppressed |
| `GET /analytics/risk` | the index with every term |
| `GET /analytics/report` | Markdown |
