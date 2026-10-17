> Historical document. Current Python behavior and limitations are described in [PYTHON_RELIABILITY.md](PYTHON_RELIABILITY.md); some recovery policies below have since changed.

# Application foundation

The standalone rules engine is unchanged. `spotify_poc.service.Application` is the reusable boundary for a future UI: connect, create, resume, preview, apply_preview, recover_write and status. A preview captures the local baseline, target snapshot, target contents and recent-play cursor. Applying it rechecks those inputs before committing a pending write. Remote readback must match before the queue becomes ready. Unknown write outcomes remain pending for explicit recovery. Target creation with an unknown outcome requires inspection rather than another POST.

`Progress` delivers dictionaries with an operation ID, UTC timestamp, elapsed time, stage and relevant counts. Listeners can render them however they wish. A `threading.Event` provides cooperative cancellation between requests, generated positions and cooldown waits. Network calls have a 30-second timeout; a blocking file lock or HTTP request cannot be cancelled immediately. SQLite also retains progress/failure events and the final request/cache/retry/wait counters. Use `status` offline to inspect the last operation.

SQLite is now the default storage. One `.spotify-accounts/application.sqlite` holds separately namespaced playlist workspaces, normalized played history and scheduled future, indexed tracks and attributes, incremental source memberships, cached responses and operation events. SQL indexes support persistence and lookup; scheduling still uses the engine's in-memory eligibility indexes. Existing JSON is imported once, transactionally, and preserved as migration evidence. Subsequent changes go to SQLite. Do not inspect old state.json expecting current state; use status or export. Keep using the same accounts-directory, and avoid alternating SQLite and legacy JSON storage on a migrated workspace.

Credentials remain owner-readable local files outside the music database. Login is shared across playlist directories after account verification. Existing local logins are imported automatically. When multiple accounts are registered, choose --account ACCOUNT_ID. Workspaces are bound to the verified Spotify account. Request pacing and persisted cooldown are shared by client ID on this computer. Legacy rate deadlines are carried forward. This is local coordination; deployment with several servers needs a central app-wide rate limiter and production credential protection.

```sh
cd ~/projects/smart-playlist
python3 -m spotify_poc --directory second_playlist status
python3 -m spotify_poc --directory second_playlist sync
python3 -m spotify_poc --directory second_playlist sync --apply
python3 -m spotify_poc --directory second_playlist export --output exported_second_playlist
python3 -m unittest discover -s tests -q
```

All global options precede the command. Paths are relative to your working directory. No third-party dependencies are required (Python 3.10+). Creating another named mix reuses the account login:

```sh
python3 -m spotify_poc --directory another_mix create --name 'Evening mix' --source OWNED_SOURCE_A --source OWNED_SOURCE_B --rules examples/rob_rules.json
```

Pause playback during application of changes. Spotify offers no atomic compare-and-swap across playlist replacement and playback. Guards narrow the race but cannot eliminate changes after the final check, especially during multiple write batches. Large targets still use batches of at most 100 items. Once a playable buffer is full, source import continues without rechecking and replanning the target every round; a final reconciliation handles observed playback. Duplicate final reconciliations are avoided. Interrupted batches retain pending state; recovery refuses a target that diverged from the known old contents or a recognizable intended prefix.

## Source and playback contract

Source selection offers playlists owned by the connected account or readable collaborative playlists, verified with an item request. Create also probes source access. Source permissions can change after selection; access errors stop the operation. Artist and album attributes come with playlist items; track/artist/album rules do not wait for genre loading. Required genre rules need a supplied genre mapping; no remote genre enrichment service exists yet.

The POC expects sequential target playback with shuffle and repeat disabled. Recent history is limited to 50 entries; missing bookmarks, ambiguous order or unexpected target playback stop ordinary sync. Recovery establishes an explicit fresh baseline and does not guess missing consumption. If a pending write also loses its history bookmark, recover-history can preview a reset only when the remote contents match the journal or an intended prefix; --apply remains explicit. The future app must expose this limitation and recovery choice. It also needs secure mobile/backend token storage, a durable worker scheduler and a deployment-wide rate limiter before unattended multi-user operation.

Spotify's rate-limit guidance describes the rolling 30-second app-wide window, Retry-After handling, batching and snapshot-based avoidance of redundant downloads: https://developer.spotify.com/documentation/web-api/concepts/rate-limits . A fixed one-second pace buffers requests but cannot guarantee staying below an undisclosed app quota. Conditional HTTP caching is opportunistic when Spotify supplies valid ETag/cache headers; snapshots are the main source-playlist reuse mechanism.

## UI integration example

Create the service and its SQLite connection inside the worker thread (SQLite connections are thread-bound by default). Call connect before operations. Read and render progress through a listener, or pass events to a UI message queue. Preview before presenting an Apply button; if apply_preview rejects stale inputs, request a new preview. Do not automatically retry uncertain remote writes. Per-workspace locks prevent concurrent service operations; state comparisons protect previews across separate operations.

```python
from pathlib import Path
from threading import Event
from spotify_poc.accounts import Accounts
from spotify_poc.client import Client, read_json
from spotify_poc.storage import SQLiteStore
from spotify_poc.progress import Progress
from spotify_poc.service import Application

root = Path('another_mix')
accounts = Accounts('.spotify-accounts')
store = SQLiteStore(root, accounts.root / 'application.sqlite')
bound = store.read('state.json') or store.read('setup.json') or {}
token_path = accounts.select(root, bound.get('account'))
progress = Progress(store, listener=print, cancellation=Event())
client = Client(token_path, store=store, progress=progress,
                rate_path=accounts.import_legacy_rate(root, read_json(token_path)))
app = Application(client, root, store, progress)
try:
    account = app.connect()
    client.token_path = accounts.register(account, client.token)
    preview = app.preview()
    # Present preview.report; apply only after the user's action.
    app.apply_preview(preview)
finally:
    store.close()
```
