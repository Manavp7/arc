# Reusable recorded-footage rule presets

In **Footage**, open a saved recording and expand **Reusable rule presets**. Presets are tenant-shared and readable by accounts with `review.read`; saving, previewing or applying requires `review.write` plus access to the source/target zones.

## Save a portable rule set

1. Save the recording's zones and rules first. Finish or cancel processing; archived/purged recordings cannot be used.
2. Enter a preset name and optional description. Give each source zone used by a saved rule a logical scope name, such as “Entrance” or “Loading area”.
3. Save a new named preset, or select an existing preset and explicitly save a new version. A preset retains up to 32 versions. Up to 200 named presets are supported per tenant.

Each version snapshots rule names, entry/dwell trigger, target class, dwell threshold, cooldown and enabled state. Rules refer to logical scope slots. **No polygon coordinates or target-camera zone IDs are stored in the portable version.** Source recording ID/revision remains as provenance; the preset does not require keeping its source media.

## Preview, then apply

Choose an exact saved preset version. Select up to 20 target recordings and explicitly map each logical scope to an existing zone on each target. Every slot needs a mapping, and different logical scopes must map to different target zones. Polygons are never created, replaced or copied.

**Append** is the default and preserves existing rules. **Replace** deliberately replaces all target rules while preserving every target zone. Preview shows current/final rule counts, removed counts for replacement, the resulting rule details, and per-recording rejections. Appending a preset version already present is rejected rather than duplicating its generated rule IDs. Combined rules must satisfy the existing 24-rule recording limit.

After reviewing the preview, confirm its mapping/change and apply it. The preview is bound to the reviewer, tenant, exact preset version, target revisions and append/replace mode. It expires after 15 minutes. New preset versions do not alter an existing preview. Changed targets, newly active processing jobs, archived/purged targets or changed permissions produce explicit per-recording rejections. Refresh source/target revisions and preview again to address a stale result.

Application is per recording, not an all-or-nothing transaction across the batch. Successful targets remain saved if another target is rejected; every target's result is returned and displayed. Retries of the same recent preview are idempotent. Each changed recording retains preset/version/reviewer provenance with its normal workbench revision history.

Applying a preset **does not start analysis**. Existing findings and retained analysis configurations remain unchanged. Review the new saved configuration, then explicitly run analysis to generate findings with those rules. A target class such as “person” still requires a compatible real detector; storing a rule preset does not supply a model or establish its accuracy.

## API and records

- `GET/POST /api/review/rule-presets`: list or create named presets.
- `GET/PUT /api/review/rule-presets/{preset_id}`: inspect retained versions or append a version using `expected_revision` and the exact saved source revision.
- `POST /api/review/rule-presets/preview`: preset/version ID, explicit append/replace mode, and target IDs/revisions/slot mappings.
- `POST /api/review/rule-presets/apply`: the preview ID only; callers cannot substitute unseen rules or targets.

`rule_preset` documents contain immutable version entries; only the server appends new versions. `rule_preset_preview` records retain the reviewed plan. Both reside in the versioned PostgreSQL workbench store and are included in full database backups. Target configuration writes use the shared per-video mutation lock and optimistic revision updates in the supported single-API-process deployment. Presets are recorded-video configuration tools; they do not commission live cameras or deploy rules into a separate live pipeline.
