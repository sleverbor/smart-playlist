# Python auto-updating playlist investigation

Updated October 16, 2026. The Python local web app remains the playback-driven automatic-sync test application. The Android/iPhone implementation remains manual-refresh only. Native manual generation history was deliberately not copied into Python playback history.

## Start the app

Python 3.10+ and its standard library are sufficient; no new packages are required.

```sh
cd ~/projects/smart-playlist
python3 -m spotify_ui --project "$PWD" --port 8766
```

Open http://127.0.0.1:8766/. Existing accounts, mixes, and automatic-sync preferences are preserved. A saved enabled preference starts scheduling when this real app runs; closing the process stops its scheduler. Sync now and previews remain available. Do not run two local app processes against the same project as a multi-worker deployment; the workspace locks prevent simultaneous writes, but this is a single local application.

## Lessons ported from the Android investigation

| Area | Python behavior |
| --- | --- |
| Progressive setup | Round-robin source import, candidate deduplication and early playable buffer remain. Initial rules are trialled against the full requested buffer plus its next position before target creation. Import avoids rebuilding a full candidate pool after the buffer is full and avoids two extra snapshot calls for every page; final snapshot checks remain. |
| Unheard queued songs | Playback and Required-rule checks do not discard an unheard queue prefix. New positions follow known history and retained positions. The configured queue-size limit still applies. |
| Shuffle/repeat | Positive target-context matches can consume out of order. Repeated occurrences of one observed identity consume at most one queued copy in a response batch, while every new play still enters rule history. |
| Catalog identity | Identity/linked aliases first, then a unique ISRC, then unique normalized title + artist IDs + duration within one second. A final target-only substitution requires an exactly equal duration and normalized title and one distinct queued identity. Ambiguous matches preserve queued songs. History learns matched aliases for cooldowns. No broad remaster-title or fuzzy matching was added. |
| Playback during writes | Playback is no longer a playlist-write precondition. A new completed song does not invalidate a write; the following sync handles it. Target contents/snapshots and ownership are still guarded. |
| Durable state | New queue, history, bookmark, generation, and decision report are staged in a pending plan. Confirmed queue/history/bookmark stay unchanged until publication is verified. SQLite commits the confirmed plan and report together. |
| Partial/uncertain writes | Each replace/append batch has a durable sent count, snapshot, before contents, and uncertainty flag. Readback recognizes an accepted write without resending it. An unapplied write is retried only when the prior contents and snapshot still agree. Divergence preserves the pending plan. Recovery finishes that exact plan before processing later plays. Old journals remain readable, but cannot retrospectively gain evidence that was never saved. |
| Process death | Tests actually SIGKILL a child process before/after synthetic replace/append acceptance, reopen SQLite, and recover. These are not merely caught-exception tests. |
| Idle API traffic | Automatic checks fetch recent listening before importing sources or reading target contents/membership. With no new plays, a full queue, and a recent audit, they save a local check result without those other API reads. Account verification still uses the existing profile request. Manual checks, pending settings changes, and overdue audits take the full path. |
| Scheduling | Automatic sync remains opt-in. Active updates schedule another check after five minutes; idle checks back off to 10, 20, and 30 minutes. Full source/target audits are due after 30 minutes. Scheduling reserves a deadline before dispatch and honors saved cooldowns. Pending writes recover before normal planning. |
| Rate handling | The default request interval is six seconds across reads and writes, with the existing shared lock, adaptive slowdown, rolling-budget safeguard, bounded 429 retries, and persisted Retry-After deadline. Overrides for controlled tests remain. Six seconds is an application policy, not a guaranteed Spotify quota. |
| Diagnostics | The shared rate file keeps the latest 100 request outcomes, normalized endpoint counts, durations, status, and the last 429 header/deadline. Queries, playlist IDs, tokens and response bodies are excluded from request diagnostics. Sync reports keep up to 50 returned play observations with context and unmatched-target evidence. |
| Storage | Unchanged history/queue rows and unchanged track metadata are not rewritten at every journal checkpoint. Changed sequence rows use batched upserts; rollback does not rely on stale in-memory caches. Existing retention for events, caches, backups, and rule-window history remains. |
| Product UI | Visible last-check time, artist names, plain network errors, and nontechnical missing-history/unmatched-play messages. No instruction to pause listening for an ordinary update. Source checkboxes are no longer rebuilt on every local poll. Automatic-sync controls remain. |

The Python engine already had indexed eligibility/lookahead and compact decision logs; those were retained rather than replaced with JavaScript-specific caching or event-loop yielding. The immutable scan-engine comparison continues checking seeded decision parity.

## Limits we still need to investigate

- This does not establish that Spotify reports every completed play. The application requests the latest 50 reported entries. Missing, delayed, out-of-order, absent-context, or catalog-substituted events remain possible investigation cases.
- A missing bookmark no longer freezes normal application sync. It applies only positively observed newer target events, preserves other queued songs, and reports incomplete consumption/known-history-only rule evaluation. It cannot reconstruct omitted plays or guarantee spacing against unknown history.
- The lower-level planner still supports its strict missing-bookmark check by default; the application deliberately opts into the non-destructive observed-only policy. Explicit history-baseline recovery remains available and keeps unconfirmed queued songs.
- Plays older than the saved bookmark are not retrospectively applied. The bounded complete response observations support investigating these cases; an unseen older event must not blindly consume a newly scheduled future copy.
- Repeat-copy protection is tested within one response batch. Distinguishing an intentional replay from consumption of a later duplicate across different sync cycles requires more evidence; it is not claimed solved.
- Positional artist/album/track rules constrain newly generated songs. Already queued, unconfirmed songs are retained even when new history would make them conflict.
- The automatic membership/source/target audit interval means some external edits or deleted targets are discovered on the next full audit rather than on every idle check. A manual sync audits immediately.
- SQLite provides an atomic plan/report commit. JSON storage retains durable pending plans and atomic individual file replacement, but cannot make two separate files one transaction.
- No live Spotify writes or deliberate rate-limit probes were used for this port. A synthetic pass is not a claim of live Spotify reliability or mobile background execution.

Spotify references: [rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits), [recently played endpoint](https://developer.spotify.com/documentation/web-api/reference/get-recently-played).

## Verification

Final full Python suite: **194 tests passed**. Follow-up UI/scheduler/localization checks: **29 tests passed**; JavaScript syntax check passed. Actual process-death tests covered replace and append, each before and after acceptance, with successful recovery. The optimized engine retained exact scan-baseline decision parity.

```sh
python3 -m unittest discover -s tests
```

Coverage includes large paginated libraries, source deduplication, seeded engine parity, rate/ETag caching, deletion and recreation, editing sources/rules/size, opt-in scheduling and cooldown recovery, incomplete history, shuffle/repeat, pre-write races, SQLite rollback/delta writes, and accepted/unapplied replace/append recovery. `tests/test_python_process_recovery.py` contains the real process-kill test. It uses a synthetic remote file and separate temporary SQLite database.

Historical browser smoke test, run before demo-mode removal: created a 100-song synthetic mix from two sources, enabled automatic sync, and completed a manual sync. It did not use a real Spotify account.
