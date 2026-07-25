# prediction (M10)

Forecasts with intervals that have been **checked**, and trajectories with uncertainty cones.

```
entities ──► prediction ──► forecasts   (points + intervals + the evidence)
postgres ──┘                            (measurements, events, zone geometry)
```

## An interval is a claim; a point forecast is an opinion

The schema comment says it: *"Point forecasts without intervals are a lie."* This service takes that
one step further — an interval nobody verifies is decoration, so `GET /predict/backtest` holds out the
tail of real history, forecasts it, and counts how often the truth landed inside the interval. A 90 %
interval that contains the truth 55 % of the time is not a conservative forecast, it is a broken one,
and **nothing in the forecast itself would say so**.

Measured on synthetic series of five different shapes, rolling origin, 10 folds, nominal 90 %:

| series | model | coverage | MAE | mean width |
|---|---|---|---|---|
| noisy flat | AutoETS | 78 % | 1.06 | 3.24 |
| trend + noise | AutoETS | 90 % | 0.67 | 3.41 |
| seasonal + noise | AutoETS | 84 % | 1.57 | 5.84 |
| bursty counts | AutoETS | 86 % | 3.24 | 10.02 |
| step change | AutoETS | 100 % | 0.61 | 4.60 |

And the fallback, on the short series AutoETS refuses: 97 % / 90 % / 97 % coverage. Slightly
conservative, which is the right direction for a fallback to err.

Calibration is checked **two-sided**, because intervals that are too wide are also a failure: one
containing every possible value is trivially correct and tells an operator nothing.

## Two forecasters, chosen by the data

`StatsForecastForecaster` (AutoETS) is the PRD's choice and the default where there is enough history —
it selects among error/trend/season forms by information criterion and derives intervals from the
fitted state-space model. Imported **lazily**, because numba and pandas cost about three seconds of
warm-up and paying that at service start, or in every unit test run, for a model that may not be
selected is a poor trade.

`DriftForecaster` is not a placeholder. For a short or flat series its intervals have a property
AutoETS's do not: **they are measured, not derived**, coming from how badly this same method did on this
same series one step at a time. So they cannot be narrow for the wrong reason. Two details matter more
than its trend estimate:

- **The drift is damped.** An undamped linear extrapolation of a noisy slope produces absurd values
  within a few steps — a yard forecast predicting negative occupancy — and looks more confident the
  further out it goes.
- **Interval width grows as √h**, which is what accumulating independent errors does. Constant-width
  intervals are the most common way a forecast lies about the far end of its own horizon.

`select_forecaster` chooses on the shape of the data: enough points, not flat, dependency present →
AutoETS; otherwise drift. AutoETS is refused on a **flat** series specifically, because a state-space
fit on a constant series yields a zero-width interval — certainty about a sensor that may simply be
stuck. For the same reason the drift forecaster floors its sigma at half a per cent of the level rather
than at an absolute value, which would be meaningless for a percentage and absurd for a pressure.

Seasonality is only offered when the series covers **two full cycles**. With one cycle a seasonal model
cannot distinguish season from trend, and having "found" a season it projects it forward forever.

## Resampling does more damage than model choice

`series.py` is the least glamorous file here and the one most likely to make every forecast wrong.
Sensors report when they feel like it; every method worth using assumes regular spacing.

- **A missing count and a missing measurement are not the same thing.** No vehicles entered during a
  bucket is a real zero. No temperature reading is *unknown*, and filling it with zero predicts a
  freezing warehouse — confidently. Gap policy is declared per target, not defaulted globally.
- **The trailing partial bucket is dropped.** The bucket containing "now" is incomplete by definition,
  so its count is always low. Include it and the model learns that activity is collapsing on *every
  single run*; the downward slope is entirely an artefact of asking the question.
- **Buckets are aligned to boundaries**, so consecutive forecasts describe the same buckets instead of a
  grid sliding with the wall clock.
- **Coverage is reported** with every forecast, and lowers its confidence. A forecast built from 70 %
  invented buckets deserves to be read differently, and nothing else in the output would reveal it.

`GET /predict/series` exposes the resampled series for exactly this reason: these decisions are
invisible in a forecast.

## Trajectories are cones

- **Uncertainty grows along the path** — linearly from velocity error, quadratically from unmodelled
  acceleration and turning, which dominates beyond about twenty seconds. A point five seconds out and
  one sixty seconds out are not comparable claims, and drawing them alike invites acting on the second
  as if it were the first.
- **A stationary object gets a stationary prediction.** Below a 0.6 m/s floor, extrapolating a heading
  is extrapolating GPS jitter — confidently, in a direction chosen by the last bad fix.
- **Turning decays**, faster than speed: a vehicle straightens out of a corner long before it stops.
- **Impossible turn rates are discarded, not averaged.** A spurious 180° heading flip produces *two*
  large differences of the same sign — wrapping maps +180 and −180 to the same value — so they do not
  cancel, and even a median over four samples is dragged to −90 °/s. Anything beyond 60 °/s is bad data.

Next-zone prediction **samples** the cone rather than tracing one ray: a single ray passing near a
polygon edge is a coin flip reported as a fact, and the interesting cases — a truck approaching a gate
at an angle — are exactly the ones near an edge. The result is a probability with an ETA. Point-in-polygon
comes from the spatial service's own `ZoneIndex`, so there is one implementation of the geometry.

## Reading the interval, not the centre

Two places where using the point forecast would be technically correct and operationally useless:

- **Battery**: time-to-reserve is measured against the **lower** bound. A drone should turn back when it
  *might* hit the reserve, not when its central estimate does — the alternative is a fleet with an
  aircraft down in a yard, having been correct on average.
- **Congestion**: read against the **upper** bound of the occupancy forecast, because the useful question
  is whether a zone might overflow. Congestion is not modelled separately either — it is occupancy read
  against capacity, since two independent models could disagree about the same zone, and an operator with
  two contradictory answers has none.

Counts and occupancies are clamped at zero. An interval whose lower bound is −3 vehicles is the clearest
possible sign of a model extrapolated past its usefulness.

## Endpoints

| | |
|---|---|
| `GET /forecasts?target=` | persisted forecasts with intervals and explanations |
| `GET /forecasts/latest` | one per target and scope, as last computed |
| `POST /forecasts/run` | force a cycle now |
| `GET /predict/trajectory/{entity_id}` | path, cone, confidence, likely next zones with ETAs |
| `GET /predict/backtest?metric=` | **measured** interval coverage on real history |
| `GET /predict/series?metric=` | the resampled series a forecast was built from |
