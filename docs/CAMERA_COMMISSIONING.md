# Camera readiness

**Administration → Camera setup** records a measured readiness check for an existing RTSP camera. It does not activate a source, change its calibration, restart ingestion or modify the site layout.

## Prepare and validate

1. Sign in as an **integrator or administrator**. Configure an RTSP source in Sources and place a camera on a saved site in Site editor. A simulator does not satisfy the camera requirement.
2. Create a setup, name it, and select the source, site and placed camera. Review source status, last successful observation and any restart requirement. **Test configured connection** uses the existing bounded source test; it neither starts ingestion nor stores preview footage.
3. Enter measured latitude/longitude, bearing, mounting height, downward tilt, horizontal/vertical field of view, original frame dimensions and projection range. Confirm any values copied from the site draft against the installed camera.
4. Add at least **three noncollinear surveyed checkpoints**, spread across the image and ground area. Click an available sanitized preview or enter normalized image coordinates from 0 to 1. Enter surveyed east/north distances in metres relative to the camera's ground position; west/south values are negative. Record the survey method and date.
5. Choose an allowed residual between **0.1 and 20 m**, save the draft, then select **Validate saved measurements**. Review each point's error, root mean square error, maximum error and any failed checks.

The checker compares the fixed entered pose with the entered survey points using a flat-ground projection. It refuses points outside its ground view or configured range. Passing records numerical agreement with those inputs; it does not independently verify the survey, lens distortion, terrain slope, geolocation or live detection accuracy. Physical commissioning still requires a qualified field verification process.

## Access, state and limits

Setup reads use `site.read`; saving and validation require `site.write` (integrator/admin). Connection tests require `integration.write`. Source inventory remains subject to the ingestion deployment's tenant and integration permissions. Every saved record is tenant-scoped and attributed to the signed-in operator.

Preview requires `media.read` and a sanitized frame indexed within the last five minutes. It is unavailable when no suitable frame exists, the source is missing, or the account has zone restrictions that cannot safely cover a whole-camera image. Pixelation or sanitization is **not a guarantee of anonymity**; context may still identify people, vehicles or locations.

Each setup supports up to **20 checkpoints**; a workspace supports up to **200 setup records**. Saving a changed draft clears its previous validation. Stale revisions are rejected: reload the latest record before editing again. Missing camera/source state is shown explicitly, and a successful connection test does not imply that a preview or live pipeline is available.

## Verification

Run `.venv/bin/pytest tests/unit/test_camera_commissioning.py` from the repository root. The focused implementation run passed **14 tests**, covering measured geometry, missing/invalid measurements, out-of-view rays, tenant permissions, authorship, revision conflicts, safe preview lookup and unchanged source/site records. Fixtures are authored geometry, not a real-camera accuracy assessment. Frontend draft and validation regressions are in `web/tests/commissioning.test.mjs` (`npm --prefix web test`).
