> Historical document. Current Python behavior and limitations are described in [PYTHON_RELIABILITY.md](PYTHON_RELIABILITY.md); some recovery policies below have since changed.

# Recovering a playback history gap

Use the explicit recovery command when ordinary sync reports `Playback history gap; consumption cannot be inferred safely`:

```sh
cd ~/projects/smart-playlist
python3 -m spotify_poc recover-history
python3 -m spotify_poc recover-history --apply
python3 -m spotify_poc sync
```

The first command previews the proposed history baseline, retained prefix, queue length, uncertainty and required playlist changes. It does not change playlist state or write to Spotify. Read-only requests may still update caches, pacing state and API counters. The apply command recomputes against current data; it does not blindly replay an old preview. Pause playback during recovery writes, and use the same state directory and genre mapping as ordinary sync (`--genres genres.json` when needed).

Recovery does not infer how many songs were played while the bookmark was missing. It replaces saved scheduling history with Spotify's currently available recent plays, sets a new bookmark at the latest observed event, and adopts the current target's valid source-backed prefix. It stops retaining at the first unavailable track or REQUIRED-rule conflict and generates a replacement suffix. It never invents a consumption count or silently relaxes REQUIRED rules. A valid partial queue may remain if the rules cannot fill the normal buffer; an entirely empty result stops recovery without saving it.

Older plays are unknown. Songs played outside the available history may repeat, and cooldown windows longer than the available baseline have incomplete historical coverage. The report names these rules and records this limitation in `history_recovery`. Future sync evaluates known history and resumes accumulating observed plays from the new bookmark; recovery cannot reconstruct the missing interval.

Before applying, the CLI rechecks the target snapshot and saves the previous state to `.spotify-poc/history-backup-GENERATION.json`. It journals the intended new queue and baseline as pending, then verifies Spotify readback before marking ready. Do not restore the backup blindly: its bookmark remains missing, and the remote playlist may already have changed. Keep it for inspection.

If a recovery write fails or is interrupted, inspect the target and finish the saved pending write with the existing command:

```sh
python3 -m spotify_poc recover --apply
```

Then ordinary sync can run again. `recover-history` refuses pending/uncertain writes, an empty recent history, unsupported tracks, timestamp ambiguities, a history response older than the saved bookmark, or a bookmark that is still present. Deleted/inaccessible targets and ownership mismatches stop the operation. Neither ordinary sync nor `--reconcile` silently resets history.

Automated tests cover preview/apply behavior, input immutability, recent target plays, incomplete historical coverage, impossible rules, missing/invalid baselines, target edits during planning, interrupted writes and subsequent normal sync. All responses are simulated; implementing this command did not reset your real playlist or listening baseline.

SQLite is now the default persistence backend. Earlier file-path examples describe the legacy JSON layout; use `status` or `export` for current state. See [APP_READINESS.md](APP_READINESS.md).
