# Smart Playlist

Smart Playlist is a local Python application for building Spotify playlists from multiple source playlists. Its scheduling engine supports repeat-spacing rules for tracks, artists, albums, and genres. The repository includes a Spotify CLI, a local web app, and an independent playlist engine.

Playback-based automatic sync is available in the Python app, but remains experimental: it relies on Spotify reporting completed plays, which may be delayed or incomplete.

## Requirements

- Python 3.10 or newer
- A Spotify account and a Spotify developer application
- No third-party Python or JavaScript dependencies

## Configure a Spotify client ID

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/) and create an app.
2. In the app settings, add this exact Redirect URI: `http://127.0.0.1:8765/callback`, then save.
3. Copy the app's **Client ID**. This app uses OAuth with PKCE; it does not need the Client Secret.

For the web app, start it and choose **Connect account**. Enter the Client ID when prompted; after the first connection, the app can reuse the registered account. For the CLI, set the Client ID in the shell before logging in:

```sh
export SPOTIFY_CLIENT_ID='your app client ID'
```

## Run the web app

From the repository directory:

```sh
python3 -m spotify_ui --project "$PWD" --port 8766
```

Open http://127.0.0.1:8766/. The app binds to loopback and is intended for local use; do not expose it as a public or multi-user service.

Connect your Spotify developer application in the UI, load your accessible source playlists, select multiple sources, set the queue size and spacing rules, and create a mix. Preview sync to inspect changes before applying them. Automatic sync is off by default and runs only while the local server is running.

See [Configure a Spotify client ID](#configure-a-spotify-client-id) if you have not set up a Spotify developer app yet.

## Use the CLI

After configuring the client ID above, authenticate and inspect available playlists:

```sh
python3 -m spotify_app --directory mixes/example login
python3 -m spotify_app --directory mixes/example playlists
```

Create a mix with your source playlist IDs:

```sh
python3 -m spotify_app --directory mixes/example create \
  --source SOURCE_PLAYLIST_ID_1 \
  --source SOURCE_PLAYLIST_ID_2 \
  --name "My dynamic mix" \
  --size 100
```

Sync previews by default. Review the plan before applying it:

```sh
python3 -m spotify_app --directory mixes/example sync
python3 -m spotify_app --directory mixes/example sync --apply
```

Use `python3 -m spotify_app --help` and the command-specific `--help` for all options. [The CLI guide](SPOTIFY_POC.md) covers setup, recovery, and sync behavior.

## Data and behavior

The web app stores account data and its shared SQLite database under `<project>/.spotify-accounts`; the project path is selected with `--project` and defaults to the current directory. The CLI stores mix workspaces under the path passed to `--directory` and uses `.spotify-accounts` for shared account data by default. Keep using the same paths to reopen existing accounts and mixes.

The web app supports track, artist, and album spacing. Genre rules are supported by the engine and CLI when genre labels are supplied, but are not available in the web editor. Required rules are never relaxed; Preferred and Optional rules may be relaxed when needed.

Spotify may omit or delay playback events, so the app cannot reconstruct unknown plays or guarantee spacing against missing history. Writes are verified and uncertain writes require recovery rather than blind retries. Read [Python reliability](PYTHON_RELIABILITY.md) and [web app usage](WEB_APP.md) for operational details and limitations.

## Development

```sh
python3 -m unittest discover -s tests
python3 benchmarks/large_library.py --timeout 60 --output /tmp/playlist-benchmark.json
```

Tests use mocks, synthetic fixtures, and local HTTP fixtures; they do not contact live Spotify. The benchmark uses synthetic libraries and reports measurements for the machine and fixture used.

Code is organized into:

- `playlist_engine/` — cross-source deduplication, seeded scheduling, cooldown rules, bounded lookahead, and explainable decisions.
- `spotify_app/` — Spotify OAuth and API client, playlist import, persistence, synchronization, and recovery.
- `spotify_ui/` — local web server, UI, and optional automatic scheduler.
- `tests/` and `benchmarks/` — correctness, recovery, performance, and profiling checks.

For deeper references, see [API pacing](API_PACING.md), [rate limits](RATE_LIMITS.md), [progressive initialization](PROGRESSIVE_INITIALIZATION.md), [storage retention](STORAGE_RETENTION.md), [engine design](DESIGN_V1.md), and [performance work](PERFORMANCE_FIX.md).
