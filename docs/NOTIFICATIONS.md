# Personal in-app notifications

The global bell collects current case assignments and deadlines, completed or failed recorded-video analyses, and ready or failed evidence exports. It uses the signed-in tenant and exact authenticated subject. A case's freeform owner value must exactly equal that subject; the app does not guess whether a team label is a user account.

Notifications and read state are durable PostgreSQL workbench documents. Nothing is emailed, sent to another service, or registered with browser notification permissions. This is an in-app inbox, not an external paging system.

## Generation and restart behavior

The API lifespan starts one local reconciliation worker after its database pool is ready. It discovers tenants containing cases, video jobs or evidence packages and checks them every 30 seconds, even when the browser is closed. An authenticated notification listing also reconciles that tenant before returning current results. Shutdown wakes and joins the worker before database closure.

- Assignment notifications are generated when the observed active case owner changes. An unrelated title, note, priority or summary edit does not generate another assignment notification.
- Upcoming deadlines enter the inbox when their UTC due instant is within the next 24 hours, including exactly now and exactly 24 hours ahead. An overdue notification appears after the instant passes. Equivalent timestamps with different timezone offsets do not count as date changes. Moving the deadline creates a new deadline condition; the old condition is hidden. Resolved cases generate no assignment/deadline notifications and their old case notifications leave the current inbox. Reopening a case can create a fresh deadline notification.
- Completed/failed jobs notify their recorded creator. Cancelled jobs do not notify. Ready, failed or interrupted evidence exports notify their recorded creator and link to the specific package.
- On first installation, older terminal analyses/exports are recorded as a baseline without flooding the bell. Current active assignments and deadlines are eligible immediately. Work observed as pending and later terminal is notified normally. A persistent baseline and source markers preserve this behavior across API restarts.

Each notification ID is deterministic for its source, recipient and observed transition. The worker writes the notification before its source marker. If the process stops between those writes, repeating the transition finds the same notification rather than duplicating it or resetting its read state. Unchanged reconciliation does not rewrite notification records or markers.

This worker observes current source state at intervals. It is not an event ledger of every reassignment made and undone between checks. It is designed for the configured single API process; multiple independent API workers would require a distributed lease/coordinator.

## Access and read state

Every listing, target-open request and mark-read action checks the authenticated recipient and current source access. Case notifications require current assignment to that subject, an active case, and access to every evidence zone on the current case. Reassignment, resolution or a newly restricted attachment can remove a previously visible notification immediately on the next read.

Analysis notifications require current review access to the exact retained analysis and all its zones, plus an available recording. Export notifications check both the current case and the zones captured in the immutable package, all required actions and included mission zones. Missing or unavailable targets are omitted. The bell resolves the target again immediately before navigation, rather than trusting its cached destination. A hidden/foreign notification returns 404 on open or mark requests.

Marking read changes only the recipient's personal record. Bulk actions name the displayed notification IDs and their observed revisions; the entire selection is validated before any row is changed. A stale selection returns 409 and a newly arrived notification remains unread. The bell's **Mark visible read** action applies only to the current page. Opening a target and marking its notice read are separate explicit controls.

## HTTP contract

| Route | Result |
| --- | --- |
| `GET /api/notifications?unread_only=false&limit=20&before=...` | `{notifications, unread_count, counts_complete, has_more, next_cursor, limit, scan_limit, checked_at, note}` |
| `GET /api/notifications/{notification_id}/target` | Rechecks current access and returns `{notification_id, target}` |
| `POST /api/notifications/read` | `{notifications:[{notification_id,revision}]}`; marks only those currently visible personal records read |

Read and mark permissions are available to authenticated application read roles, including viewers, because marking personal notification state does not modify a case or dispatch a response. Record recipients are enforced inside the service regardless of role, including admins.

Targets are `{kind:"case"|"analysis"|"package", id, case_id?, video_id?, analysis_id?, package_id?}`. A page contains at most 100 records (the bell requests 20). Mark selections contain 1–100 unique notification IDs. Pagination uses immutable creation time plus notification ID, so marking read does not move records across pages.

Source reconciliation and tenant notification scans are bounded to 5,000 records per kind. Counts are permission filtered and `counts_complete:false` discloses a reached scan bound. The badge shows a plus suffix for incomplete or visually capped counts. It does not claim an unbounded lifetime total. Notifications/read-state are retained; this feature does not automatically delete their history.

The bell follows the console theme, fits narrow screens, cleans up polling and pending requests on unmount or identity changes, and clears prior-identity results. Its popup is non-modal: Tab remains available to the rest of the application. Escape and the close button return focus to the bell.

## Verification

```bash
.venv/bin/pytest tests/unit/test_notifications.py -q -o addopts=''
cd web
npm run typecheck
node --import ./tests/register.mjs --test tests/notifications.test.mjs
```

Tests cover restart deduplication, unchanged edits/read-state retention, changed owners/dates, timezone boundaries, overdue/resolved behavior, periodic generation without an open browser, terminal baseline rules, captured/current zone checks, recipient and tenant isolation, whole-selection revision checks, new-arrival safety, stable pagination and bounded-count disclosure. These checks do not establish distributed-worker delivery or external notification behavior.
