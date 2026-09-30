# Recorded visual search

The Visual search workspace indexes selected retained recordings with the existing
local ONNX CLIP image/text model. It never uses the hash embedder. A user with
`review.read`, `media.read` and `review.write` access to **every source zone** selects
one completed analysis and explicitly consents to local processing of the private
original. Search readers need `review.read` and `media.read` for the recording and
the exact retained analysis. Models are never downloaded by an HTTP request.

## Local prerequisites

Restore the project's Python lockfile with `uv sync --frozen`. The three assets
below must be installed in `SIO_MODEL_DIR` (default `.sio/models`) under the names
configured by `SIO_CLIP_VISION_MODEL`, `SIO_CLIP_TEXT_MODEL` and
`SIO_CLIP_TOKENIZER`. `uv run python scripts/fetch_models.py` is the existing model
bootstrap and installs the wider required model set. To install only CLIP, use the
three download URLs in `infra/models.json` and verify the hashes below before use.
The expected bytes
are pinned both in `infra/models.json` and in `recorded_search_model.py`:

| File | SHA256 |
| --- | --- |
| clip-vision.onnx | `0ab0c1b3ace708e539633af1744d5a95247fe4e14d3e08ff197ef82a6cb9bd93` |
| clip-text.onnx | `18845f2ccc35223bb7fec403383a131154b11ac0918df25cf51986df5efd3a21` |
| clip-tokenizer.json | `f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd` |

These are the MIT-licensed OpenAI CLIP weights exported to ONNX by Xenova, already
listed by the repository's model registry. The API checks hashes before use and
loads isolated verified copies; adjacent external-data sidecars cannot change the
embedding space. Changing the CLIP model requires an explicit code/manifest update
and rebuilding existing indexes. There is no download, cloud inference or raw-media
upload during indexing or search.

## Operator flow

1. Upload and finish an analysis in Footage.
2. Open Visual search, select the recording and exact analysis, review the consent
   statement, then select **Index selected recording**.
3. Watch queued/running/ready status. One recording can index at a time; failures
   and interrupted work require an explicit retry. Rebuilding replaces its previous
   index, not recordings, cases or retained analyses.
4. Enter a description or select **Find similar** on a result. Each result opens its
   exact recording, analysis and timestamp. Thumbnails use existing protected,
   pixelated evidence; no new original-media endpoint exists.
5. Save a private review bookmark or open footage for human review. If a real saved
   event overlaps that sample, **Create case from event** uses that event through
   the existing case API. Arbitrary similarity matches never become detector events
   or human annotations automatically. Use the existing frozen-annotation workflow
   for incidents without a detector event.
6. **Remove search index** deletes its vector file while preserving original media
   and evidence. Archive makes the recording unsearchable. Purge removes the index
   with its containing video directory.

## Bounds and storage

- At most one retained sample every two seconds, up to 90 samples per recording,
  20 accessible recordings per bounded query and 24 displayed results by default.
- One exact analysis/index per recording. Revisions of the recording or analysis,
  changes to original bytes, missing media and mismatched model fingerprints make
  old indexes unusable. Re-index after reviewing a change.
- Embeddings live in `recorded-search.json` within the private per-video directory,
  mode `0600`, bounded to 2 MiB, atomically replaced. They are never API payloads or
  append-only workbench history. Existing private data backups include this file.
- Workbench documents retain only consent, actor, model identity, source revision,
  progress and status. Native decoding shares the existing video processor lock.
  Lifecycle locks prevent deletion/configuration racing active source processing.
- CPU execution uses one ONNX thread and a five-minute processing budget between
  native calls. An in-flight native call finishes before shutdown releases its
  resource lock. PostgreSQL session claims coordinate cooperating API workers that share the same
  media filesystem. This is not a hard operating-system execution timeout or
  automatic distribution of media between hosts.

Cosine similarity is a ranking score, not a probability, detector confidence,
identity or confirmation of an incident. CLIP uses a centre crop; activity near
frame edges, brief activity between samples, small objects and unfamiliar scenes
can be missed. Pixelated previews require careful source review. Authored colour
fixtures verify the local model and retrieval path; they do not measure real-site
accuracy or establish surveillance suitability.

## Verification

`uv run pytest tests/unit/test_recorded_search.py` covers strict consent, tenant and
zone access, exact retained versions, bounded vectors, original/source changes,
archive/removal, symlink refusal, queued deletion, lifecycle locking and restart
recovery. Its `models` test uses installed pinned CLIP assets and an isolated
authored red/blue MP4; it skips explicitly when the model or MP4 encoder is absent.

`cd web && node --import ./tests/register.mjs --test tests/recorded-search.test.mjs`
checks protected thumbnail construction, sample identity, consent and query HTTP
payloads. Run frontend typechecking/build and the combined application checks after
route/navigation integration.
