# Private recording bookmarks

Bookmarks are personal working notes at a precise recording timestamp. They retain an exact completed analysis ID, so reopening a bookmark can load its original rules, zones and model context. They do not assert that a detector emitted an event, constitute a frozen human annotation, or create a case. Bookmark notes are excluded from case reports and evidence packages.

Each bookmark belongs to one authenticated subject within one tenant. Other users, including administrators, cannot list, edit or delete it through the bookmark API. Read and write requests recheck permissions against every zone in both the current recording configuration and the complete retained analysis, including its rules. Reads require `review.read` and `media.read`; mutations additionally require `review.write`. A list without an analysis filter omits individual bookmarks whose retained analysis is no longer visible. A requested inaccessible analysis fails explicitly.

## API

`install_review_bookmark_routes(app, store)` installs these routes and returns a `ReviewBookmarks` manager. The application must install its normal authentication and governance middleware.

| Method | Route | Body or query |
| --- | --- | --- |
| GET | `/api/review/videos/{video_id}/bookmarks` | Optional `analysis_id` query filter |
| POST | `/api/review/videos/{video_id}/bookmarks` | `analysis_id`, `at_s`, `title`, optional `note` |
| PATCH | `/api/review/bookmarks/{bookmark_id}` | `expected_revision` and at least one of `title`, `note`, `at_s` |
| DELETE | `/api/review/bookmarks/{bookmark_id}` | JSON body with `expected_revision` |

Create/edit responses are plain bookmark records: `bookmark_id`, `video_id`, `analysis_id`, `author`, `at_s`, `title`, `note`, `revision`, `created_at`, `updated_at`, plus the store's `record_id`. Creation returns HTTP 200, including an idempotent duplicate response. Deletion returns `{ "deleted": true, "bookmark_id": "…" }`.

The title is trimmed and must contain 1–120 characters; the optional note is trimmed and capped at 2,000 characters. The timestamp must be finite and between zero and the recording duration, inclusive. Empty text updates can clear the note. Null fields, unknown fields, changes to author/source IDs and empty patches are rejected. There are no fabricated event IDs or confidence values.

Creating the same author/video/analysis/timestamp again returns the existing bookmark unchanged, preserving its title and note. A different analysis is a different bookmark even at the same timestamp. Moving an existing bookmark onto another of the author's bookmarks in the same analysis returns 409. Deleting and recreating a bookmark generates a fresh ID so old revision history cannot be confused with the replacement.

Edits and deletion use optimistic revision checks and return 409 for a stale revision. The client should keep its unsaved text available and reload the saved record before retrying. Deletion removes only the current personal note, never its recording, retained analysis or other users' bookmarks.

## Bounds and retention

A user may retain at most 200 bookmarks for one video and 1,000 across a tenant. All bookmark writers share a local mutation lock so concurrent writes on different videos cannot exceed the author quota. Lists are sorted by timestamp, analysis ID and bookmark ID and return `{ bookmarks, limit: 200, possibly_truncated }`. The tenant scan is capped at 5,000 current bookmark records; reaching that bound marks lists incomplete and fails creation/timestamp moves closed. Editing text and deleting a known visible bookmark remain available to reduce the inventory. Clients must disclose `possibly_truncated` rather than presenting the result as a complete count.

Bookmark mutations also acquire the same per-video lifecycle lock as archive/purge and revalidate the recording inside it. Archived, purged, purging, unavailable or missing recordings cannot gain bookmarks. The supported deployment remains one local API process; the existing lifecycle locks are process-local.

Current `review_bookmark` documents protect both their explicit video reference and retained analysis reference during storage cleanup. Storage previews report `bookmark_linked` and `linked_bookmarks`, without exposing personal bookmark titles or text. Removing the final bookmark can make otherwise inactive, unlinked footage eligible; it never runs cleanup automatically. Stored document revision history follows the existing workbench retention behavior, and deleted bookmark history does not retain footage by itself.

Offline backups include the private notes in the database dump and validate their video/analysis references in the same tenant, the completed analysis state and the analysis/video association. A bookmark linked to purged or absent footage fails backup reference validation. Backups must be handled as sensitive operator data under the existing backup procedures.

## Verification

`tests/unit/test_review_bookmarks.py` covers restart persistence, privacy, duplicate identity, stale revisions, finite/bounded timestamps, exact retained source ownership, current and frozen scope, lifecycle races, quotas, HTTP permissions and backup reference integrity. Tests use in-memory documents and owned temporary media only.
