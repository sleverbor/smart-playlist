# Spotify local CLI prototype

Python 3.10+, standard library only; no dependencies to install. Scheduling remains independent in `playlist_engine`.

Register a Spotify developer app with redirect URI `http://127.0.0.1:8765/callback`. From this folder:

```sh
export SPOTIFY_CLIENT_ID='your app client ID'
python3 -m spotify_poc login
python3 -m spotify_poc playlists
python3 -m spotify_poc create --source PLAYLIST_ID_A --source PLAYLIST_ID_B --minimum-buffer 20 --size 100
```

Create samples sources in rounds, deduplicates tracks, and derives a diversity reserve from REQUIRED cooldowns. It publishes only after a full starter sequence satisfies REQUIRED rules and Spotify readback confirms its contents. The terminal then shows **Ready to play in Spotify** with a direct playlist link. Import continues and the queue expands in starter-sized increments, retaining the valid future prefix and reconciling observed playback before each write. Partial samples may still favor tracks near the start of each source; diversity is a heuristic, not proof of representativeness. After exhaustive import, a full engine trial determines feasibility even if the reserve heuristic fails.

Progress reports source entries loaded/total, unique candidates, skipped entries, sampled/completed sources, generated positions and verified queue length. It does not invent an overall percentage. Defaults are 20 initial songs and 100 normal songs; both are configurable. Impossible REQUIRED rules stop publication or leave a shorter valid queue.

Keep the command running to finish setup. This CLI has no detached background worker. After interruption or an import failure:

```sh
python3 -m spotify_poc resume
```

Cached pages, rules, genre mappings and progress are checkpointed in `.spotify-accounts/application.sqlite`, separately for each playlist directory. Existing JSON files are imported once and preserved; they no longer reflect current state. Use `status` or `export` to inspect the database. Changed source snapshots invalidate their cached pages on resume. Existing targets created before progressive setup still use ordinary sync:

```sh
python3 -m spotify_poc sync
python3 -m spotify_poc sync --apply
```

Sync previews unless `--apply` is supplied. `--reconcile` rebuilds after an external target edit. Use sequential playback with shuffle/repeat disabled and pause during writes; concurrent playback cannot be excluded atomically.

Default rules: track 100 REQUIRED, artist 5 PREFERRED, album 3 OPTIONAL. Use `create --rules rules.json` for a JSON array of custom rules. Genre rules require `--genres genres.json`, a mapping from track IDs to nonempty genre lists. Track/artist/album rules need no genre enrichment. No remote genre enrichment provider is implemented: missing genre labels block genre evaluation rather than silently passing. A blocked unpublished setup can resume with `--genres`; changing mappings after publication requires a separate setup. Ordinary sync reuses the mapping stored with the setup unless an explicit mapping is provided.

Local files, unavailable tracks and episodes are skipped; unsupported recent plays stop reconciliation. Deduplication uses Spotify track IDs with source provenance; relinked IDs are not merged. History and scheduled future remain separate. Spotify reports only the most recent 50 plays: missing history cursors, timestamp ties and unexpected target playback stop sync. Sync before 50 new plays. Empty initial history cannot prove longer coverage.

Remote writes are journaled before population. An interrupted population leaves pending state:

```sh
python3 -m spotify_poc recover --apply
python3 -m spotify_poc resume
```

Recovery verifies ownership, replaces the saved queue and checks readback. An uncertain create leaves `creating` state requiring manual inspection and attachment of the created target ID before changing status to `pending`; never blindly retry creation. Import failures after readiness preserve the published buffer. Snapshot checks detect many edits but are not atomic across remote batches.

Tokens, history, queue, checkpoints and latest decision log are unencrypted local files with owner-only permissions. Keep `.spotify-poc` out of version control. Generation uses controlled randomness and bounded lookahead; scoring cost still grows with candidate-pool size. Expansion is incremental, but this prototype does not promise a particular latency for large libraries.

```sh
python3 -m unittest discover -s tests -v
```

Progressive initialization is validated with simulated Spotify responses, not a new live account mutation. API reference: [Spotify migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

Large-library measurements and commands: [benchmark guide](benchmarks/README.md). Current profiling exposes scheduling and decision-log costs; large-library latency is not yet acceptable or guaranteed.

Request pacing, bounded 429 retry, ETag and source snapshot caching are now implemented. See [request resilience](RATE_LIMITS.md) for configuration, counters, tests and remaining scope.

An explicit history-gap recovery path is available: preview with `python3 -m spotify_poc recover-history`, then apply with `--apply`. See [history recovery](HISTORY_RECOVERY.md) for uncertainty, backups and interrupted-write handling.

The lookahead scan bottleneck is fixed with in-memory indexes, and the CLI uses compact explainable logs. See [performance fix](PERFORMANCE_FIX.md) for measurements and restarting an older running command.

See [APP_READINESS.md](APP_READINESS.md) for shared account selection, structured diagnostics, cancellation, preview/apply checks, storage export and UI integration.
