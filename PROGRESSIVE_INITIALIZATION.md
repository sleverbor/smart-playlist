# Progressive first sync — V1 requirements

Status: accepted design requirements. The current CLI imports all sources before its first fill; this document specifies the next implementation change.

## Principles

**Progressive initialization:** produce a usable Spotify target as early as active rules permit. Playlist usability has priority over completing import, enrichment, or optimization.

**Rule-driven metadata loading:** load and cache the attributes needed by enabled rules first. Nonessential enrichment must not delay the first playable buffer. Continue remaining work after readiness, with resumable checkpoints.

## Buffer targets and stages

Use separate configurable targets:

- `minimum_playable_buffer`: default 20 tracks, normally 10–25.
- `normal_buffer`: default 100 tracks; never smaller than the minimum.

Authenticate, discover sources and obtain available totals. Import pages incrementally across sources, deduplicating as they arrive. Avoid exhausting the first source before visiting others.

After each useful batch, evaluate initialization readiness. Once ready, generate the minimum buffer, create/populate the private target, verify remote contents, persist the committed queue, and prominently announce **Ready to play in Spotify** with a direct link and verified song count. Continue importing and expand toward the normal buffer in bounded batches. Generation must expose completed-position progress and support cancellation between decisions.

Remaining work may run independently of the visible waiting state. In the CLI this can continue in the same process after printing readiness. Mobile background execution is opportunistic: closing the app must not be represented as a guarantee that setup continues.

## Initialization confidence

Track count alone is insufficient. Readiness uses a versioned policy derived from enabled rules, their strengths, and the requested initial buffer:

1. Every accessible selected source has contributed a bounded sample, or is explicitly reported empty/unavailable. Report source coverage and imported fractions. Partial source samples cannot be claimed representative of the full library; report early-selection bias.
2. Eligible unique candidates meet a candidate reserve appropriate to buffer size and cooldown windows. Measure artist/album/genre diversity only where relevant, counting collaborating artists and overlapping genre labels correctly.
3. All metadata needed to evaluate REQUIRED rules is known for chosen tracks and relevant history. Unknown metadata must not silently pass a REQUIRED rule.
4. A bounded trial generates the full minimum buffer while satisfying every REQUIRED rule against known played history and the planned sequence. Diversity counts are useful heuristics, not a feasibility proof.

The coordinator owns import coverage, metadata readiness and the confidence policy. The independent engine provides feasibility, bounded future-flexibility scores and decisions. Log the policy version, observed counts, reasons readiness passed/failed, unknown history coverage and any relaxed PREFERRED/OPTIONAL rules.

Candidate and diversity reserves must be tested and tuned; the proposed 100 tracks/30 artists are illustrative, not fixed acceptance thresholds. A REQUIRED artist cooldown of 50 must not be weakened to obtain an early fill. If readiness cannot be reached, report the blocking rule and continue importing. After exhaustive import, report infeasibility and offer an explicit rule change or smaller buffer rather than claiming readiness.

## Lazy metadata and expansion

Track/artist-only rules must not trigger genre enrichment. Fetch required attributes first, then preferred, then optional/nonessential attributes, using a cache and bounded batches. Unavailable metadata must be visible; any temporary deferral of a soft rule is explicit and logged.

If a user enables a rule requiring new metadata during setup, invalidate readiness for new selections and reevaluate before writing. Enrichment that changes a scheduled track's rule attributes requires validation of the remaining queue; do not rewrite it silently.

Treat the published buffer as scheduled future, never played history. Expansion retains the valid unplayed prefix and generates only the suffix. Before each write, reconcile new playback and target/source changes using the existing history-gap and external-edit protections. Consumption/refill takes precedence over nonessential enrichment. Stop safely when playback cannot be reconciled.

Persist import cursors, source snapshots, deduplicated candidates, metadata tasks, configuration version, target ID, write intent, verified queue and playback cursor. Resume without creating another target or appending duplicate batches. Source changes during import invalidate affected checkpoints. Cancel, network failure or enrichment failure after readiness leaves the verified playable buffer available and reports setup as paused/incomplete.

## Meaningful progress

Expose stage-specific events suitable for CLI output and a later UI:

- Connection and source discovery status.
- Sources sampled/completed out of selected sources.
- Loaded source entries out of known totals, with skipped and unique counts separately.
- Evaluating initial diversity/readiness, including the actual blocking reason.
- Initial tracks built out of minimum target.
- Tracks verified in Spotify and direct target link.
- Remaining import and enrichment counts, then tracks verified out of normal target.
- Paused, retryable failure, infeasible, or setup complete.

Use indeterminate progress only when a denominator is unknown. Do not invent an overall percentage across unrelated stages; label any estimate. “Built” and “uploaded” do not mean verified playable. Ready and setup complete are distinct states. An empty target is not a readiness milestone.

## Acceptance tests

- Large multi-source paginated import publishes a verified minimum buffer before all pages or nonessential enrichment finish.
- All-source sampling, duplicate-heavy sources and concentrated first pages cannot bypass readiness.
- Track/artist-only rules make zero genre-enrichment calls before readiness.
- REQUIRED long cooldowns, multiple collaborating artists, overlapping genres and missing metadata block early fill appropriately.
- Tiny pools/impossible constraints never relax REQUIRED rules or falsely announce ready.
- Soft-rule deferrals and relaxations appear in decision logs and progress.
- Playback between first fill and expansion advances history once, retains valid future and refills the suffix.
- External edits, history gaps, source changes and interrupted multi-batch writes preserve existing reconciliation protections.
- Cancellation/restart and enrichment failure after first fill preserve the verified buffer and resume without duplicate target creation.
- Progress denominators, unique/skipped counts, readiness song counts and completed-position events match actual work.

No fixed seconds-to-readiness promise is made until measured against real Spotify latency and limits. Measure time to first verified buffer separately from time to complete setup.

SQLite is now the default persistence backend. Earlier file-path examples describe the legacy JSON layout; use `status` or `export` for current state. See [APP_READINESS.md](APP_READINESS.md).
