# Redundant subtitle cleanup design

Videos can contain several subtitle tracks for the same language.
Existing download scores cannot rank every external or embedded track.
Cleanup adds analysis, a preview, confirmed removal, and restoration.

## Scope

- Include external and embedded subtitles, including tracks without download scores.
- Compare regular and hearing-impaired subtitles together, retaining the highest-scoring track.
- Include text and bitmap subtitles, including Presentation Graphic Stream (PGS) and multilingual VobSub pairs.
- Rewrite video containers when removing embedded tracks, without re-encoding retained streams.
- Provide episode, movie, and bulk cleanup previews.

The [original request](https://bazarr.featureupvote.com/suggestions/54275/option-to-delete-redundant-subtitles)
describes redundant external and embedded subtitles.
The common score measures speech timing, with separate forced-subtitle groups.

## Common timing score

Rank every candidate using the same speech-timing correlation score, from -1 to 1.
Show existing download-match scores separately.
Those scores measure [metadata matches](../custom_libs/subliminal_patch/score.py), rather than subtitle content quality.
[Uploads](../bazarr/subtitles/upload.py) and [extractions](../bazarr/subtitles/embedded.py) can record synthetic maximum scores.
Do not treat these synthetic values as measured quality.

Use one selected audio stream and one presentation timeline for every competing track.
Build equivalent speech-activity and subtitle-display signals.
Union overlapping display intervals, using consistent rules for text and bitmap subtitles.
Do not strip hearing-impaired cues from text while retaining their bitmap equivalents.

Calculate normalized Pearson correlation between the two signals.
Record the raw score, audio selection, algorithm settings, and parser/tool versions.
Do not adjust subtitle timestamps or impose an additional minimum score.
Require a keeper selection when highest scores tie or analysis cannot produce meaningful correlation.
Constant signals, decoding failures, and incomplete timing data require explicit explanations.
Never invent a numeric score for those cases.

This score measures timing association only.
It cannot judge wording, translation, language correctness, completeness, or actual content redundancy.
Legitimate reading time, sound descriptions, signs, and dubbed audio can affect the ranking.
The preview must explain these limits before confirmation.

Compare within each language, keeping forced translations separate from full-dialogue subtitles.
Regular and hearing-impaired subtitles compete within each group.

## Subtitle analysis and container handling

Decode text cue timings and bitmap display events without modifying originals.
For PGS, reconstruct visibility through display and clear events.
Its [decoder](https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/pgssubdec.c) uses indefinite end times.
Packet duration alone cannot establish every display interval.

Use MKVToolNix for Matroska and VobSub rewrites.
Use FFmpeg for other containers when required preservation checks can pass.
Identify tracks independently for each tool; their stream identifiers are not interchangeable.

Treat `.idx` and `.sub` companions as one unit, potentially containing multiple languages.
Use temporary Matroska and [VobSub extraction](https://mkvtoolnix.download/doc/mkvextract.html#mkvextract.output_file_formats)
to rebuild retained tracks and their companions.
Combined extraction requires matching codec headers.
Validate retained images, palettes, effective display times, and language indexes.
Do not delete shared companion data because one contained track loses.
Unsupported versions or preservation failures must leave originals intact and report the reason.

## Preview, confirmation, and recovery

1. Show every keeper and removal, including language, forced status, and hearing-impaired status.
2. Explain score limitations, video rewrites, backups, and required space on each filesystem.
3. Persist the exact preview with media IDs, candidate membership, stream mappings, and content fingerprints.
4. Fingerprint every candidate, keeper, video, and VobSub companion.
5. Apply only the confirmed preview; revalidate it immediately before replacement.
6. Coordinate all Bazarr media writes through shared cross-process locks.
7. Cover cleanup, downloads, uploads, synchronization, modifications, extraction, translation, deletion, and restoration.
8. Create verified independent backups; hard links cannot protect against later in-place writes.
9. Stage and validate every replacement before changing originals.
10. Journal replacement steps and synchronize files and directories for crash recovery.
11. Recover interrupted operations before permitting further writes to affected media.
12. Complete or roll back a commit when cancellation occurs during replacement.
13. Refuse restoration that would overwrite later edits.
14. Reindex rewritten streams and show the actual resulting subtitle inventory.

Multiple file replacements are not one atomic operation.
The journal and recovery protocol must handle partial commits.
Bazarr locks cannot coordinate independent Sonarr, Radarr, or filesystem writers by themselves.
Production operation therefore requires coordination with those writers.

Verify explicit input/output stream mappings before replacement.
Compare audio/video payloads, retained captions, initialization data, and presentation timing.
Also compare dispositions, metadata, chapters, attachments, and container-specific structures.
Successful tool execution and readable output alone do not prove preservation.
Leave originals intact when required preservation cannot be established.

## Production authorization

Production execution requires an operator-controlled boundary outside repository and model-editable settings.
Authentication or an editable feature flag cannot provide that boundary alone.
Cover application programming interface (API) calls, jobs, command-line routes, restoration, alternate invocations, and configurable paths.
The deployment must deny production mutation until that boundary exists.
Development verification uses disposable media.
Development verification does not authorize production deployment or changes to existing libraries.

## Implementation

The cleanup backend lives under `bazarr/subtitles/cleanup/`.
Operation records integrate with [the database](../bazarr/app/database.py).
The [subtitle action menu](../frontend/src/components/SubtitleToolsMenu.tsx)
and [bulk tools](../frontend/src/components/modals/SubtitleToolsModal.tsx) open the shared preview.
See [cleanup controls](../frontend/README.md#subtitle-cleanup) for the interface workflow.

## Acceptance criteria

- Confirmed cleanup retains the highest-ranked external or embedded track within each approved group.
- Regular and hearing-impaired alternatives compete under the same ranking rules.
- Previously unscored tracks receive common analysis or an explicit explanation requiring keeper selection.
- Fixtures cover text, PGS clear events, multilingual VobSub, and multiple audio streams.
- Tests cover stale previews, concurrent existing writers, insufficient space, and interrupted replacement steps.
- Retained streams and metadata pass container-specific preservation checks.
- Restoration recovers originals and refuses to overwrite subsequent edits.
- The running application displays the resulting inventory after cleanup and restoration.

## Deployment requirement

Production still needs an external authorization boundary and coordination with independent media writers.

Filename guesses, synthetic maximum scores, raw synchronization scores, and simple duration-overlap rankings are rejected.
