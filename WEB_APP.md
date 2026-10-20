# Local web application

From the project directory:

```sh
cd ~/projects/smart-playlist
python3 -m spotify_ui
```

Open **http://127.0.0.1:8766**. Python 3.10+ is sufficient; no Node server or third-party dependencies are required. Keep the process running while the UI is in use. Use `--project /absolute/project/path` when starting elsewhere, or `--port 8768` if the default port is occupied.

The app reuses registered Spotify accounts and the existing shared SQLite database. Your mixes appear immediately without downloading source libraries. Select Create a mix, load owned/readable collaborative sources, select several playlists, choose a name and adjust spacing/strengths. Defaults are track 100 Required, artist 20 Preferred; album spacing starts disabled. Initial and normal buffer sizes are configurable. The web app offers track, artist and album spacing. Genre controls have been removed; existing engine/CLI configurations remain compatible.

Create publishes a usable initial buffer as soon as the rules and diversity allow, exposes Ready to play and the Spotify link while import continues, then grows the buffer. Background work runs in a worker thread, with one operation at a time in this local application. The page polls structured progress; it does not invent an overall completion percentage. Pause operation cancels cooperatively. A network request or cooldown can take up to roughly 30 seconds to yield, and a lock wait may take longer. Continue setup resumes saved import. An uncertain remote update stays pending and exposes Recover write; unknown creation outcomes require inspecting Spotify before another create.

Preview sync shows consumption, the retained prefix, queue size, any recovery warning and upcoming titles. Applying requires an in-page confirmation, then the service rechecks local state, target contents/snapshot and playback before publishing. Previews expire after ten minutes or server restart. Playback gaps offer Preview history recovery; external edits offer Preview reconciliation. Recovery makes its unknown-consumption boundary explicit. Pause Spotify while applying changes. Shuffle, repeat and skips are reconciled from reported plays. Recently played events can take time to appear; the app never guesses unreported plays.

Connect account opens an OAuth flow through your existing Spotify developer application. Leave the client-ID field blank to reuse a registered application, or provide one for the first connection. Its dashboard must allow `http://127.0.0.1:8765/callback`. The UI gives an Open Spotify link for authorization; do not run CLI login at the same time because both use that callback address. Tokens stay in owner-readable server-side account files and are never sent to browser JavaScript. Operating-system secure credential storage remains work for the mobile product.

The server binds only to 127.0.0.1, checks Host and Origin, requires a per-process token for mutations, rejects cross-site requests, and serves a restrictive content policy. It is a local app; do not expose it on a public interface or treat it as a hosted multi-user backend.

## Architecture and validation

`spotify_ui.controller` adapts user actions to `spotify_app.service.Application`. Each worker creates and closes its own SQLite connection. `spotify_ui.server` handles only local HTTP and request checks. The dependency-free HTML/CSS/JavaScript in `spotify_ui/static` renders state and polling progress. The scheduling engine remains independent of the UI.

View songs displays the saved buffer without calling Spotify. Edit rules changes track, artist and album spacing for the next sync and invalidates older previews. Source editing remains a future step. Sync now combines planning and applying, with the same playback and target checks as the preview flow.

Automatic sync is off by default and can be enabled per mix. While the local server is running, it checks every 30 seconds and schedules an enabled ready mix every ten minutes, one operation at a time. It uses the shared request pacing and persisted cooldown state. Server shutdown stops scheduling; saved preferences survive restart. It does not run as an operating-system background service.

All 157 automated tests passed. They cover the engine, service, local HTTP interface, queue viewing, rule validation, preview invalidation, single-step sync and opt-in scheduling. Automated tests use mocks and synthetic fixtures; they do not call live Spotify. Browser checks verified the saved buffer, editing rules, switching automatic sync on and off, and a live Sync now that completed with a verified 50-song queue. Scheduling intervals were tested with a controlled clock; a ten-minute live scheduled run was not exercised. Earlier live acceptance covered authentication, progressive creation, playback consumption, shuffle, repeat and deletion; see benchmarks/LIVE_ACCEPTANCE_20261009.md.

## Generated playlists as sources

A generated playlist can be a source for another mix. Sync the upstream mix first, then preview/sync the downstream mix; there is no automatic dependency scheduling or recursive expansion of upstream source libraries. Downstream import uses the upstream playlist's current published tracks and detects snapshot changes. Overlapping sources remain deduplicated.

The application rejects self-reference and reachable circular dependencies among mixes for the same account registered in the shared SQLite database. It checks before create/resume/preview/recovery and again before applying a preview. Unrelated cycles and other accounts do not block a mix. Dependency relationships in other databases, legacy JSON-only workspaces, or external applications are not discoverable; those require manual care.

The web source selector excludes generated targets registered for the selected account in the shared database, including targets in incomplete setup. Generated identities are retained separately after setup removal, so deleting a sync setup does not make its target eligible as a source. Existing source configurations are preserved.

If playback history changes while setup is planning an expansion, the playable buffer stays intact and source import finishes. Setup waits for you to pause Spotify and select Continue setup. Target edits still fail closed. This handles observed playback changes; Spotify may delay reporting listening events.

Remove setup removes only the selected local sync configuration and history, with confirmation. Its Spotify playlist and saved login remain. A local JSON backup is retained in .sync-backups before removal. Other mixes are untouched, and removal works during Spotify cooldowns.

Each mix shows its last successful sync, next scheduled sync when enabled, and the latest failure message. These records survive server restart. Expired authentication or an unavailable playlist pauses automatic sync; reconnect or repair the playlist, then enable automatic sync again. Restart resumes overdue ready mixes through the serialized scheduler.

Source caches use the verified account and Spotify application, so reconnecting or rotating tokens preserves access. Older complete source caches migrate from verified login provenance or an exact source ID/snapshot recorded in the same-account workspace. Sync still checks the current Spotify snapshot before reusing tracks.

During a persisted Spotify cooldown, mix cards show the deadline and automatic sync schedules its next attempt for that deadline. Due jobs skip connecting to Spotify while the deadline is in the future; manual sync also stops before connecting. Scheduling resumes on the next 30-second scheduler tick after the deadline, while the local server runs.

Edit sources changes an existing ready mix without recreating it. The current queue remains playable; future refills use the updated, deduplicated source selection. Older previews expire when sources change. Generated playlists are excluded. Saved playlist choices are available offline; use Load available playlists after Spotify cooldowns end to refresh the list, then reopen Edit sources. Complete initial setup before changing sources. Activity shows the latest 20 recorded actions and failures; it starts recording when this version is installed.

## Localization foundation

The app remains English-only. UI copy lives in spotify_ui/static/en.js, with shared translation, interpolation, plural and number helpers in i18n.js. Browser locale controls number/date/time formatting; English plural rules follow the displayed language. Static text, placeholders and accessibility labels are translated on startup. Adding languages still requires locale selection, complete phrase translations, and structured translated service errors; backend diagnostics remain English. No language selector is added yet.

Service failures expose stable error_detail codes and parameters to the browser. English messages live in the same language catalogue as UI copy. Unknown failures show a generic product message; raw diagnostics remain in server-side events and compatibility fields. Cooldown timestamps are formatted by the browser. API, job, saved sync-status and activity messages use this translation boundary.
