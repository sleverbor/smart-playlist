# Smart Playlist

A local Python application for building Spotify playlists from multiple source playlists, with repeat-spacing rules for tracks, artists, and albums. It includes an independent rules engine, a command-line client, and a local web interface.

This repository retains **playback-based automatic sync** for investigation: observed completed plays can remove queued songs and trigger refills. The separate [Smart Playlist Mobile](../smart-playlist-mobile/README.md) app uses manual full-playlist refreshes and app-generated history instead.

## Quick start: no Spotify account needed

Requires Python 3.10 or newer. Runtime and tests use the Python standard library; no dependency installation is necessary when running from this directory.

```sh
cd ~/projects/smart-playlist
python3 -m spotify_ui --demo --port 8766
```

Open http://127.0.0.1:8766/. Demo mode uses temporary synthetic data, makes no Spotify requests, and discards its mixes when stopped. If the port is occupied, choose another port.

Example: create a mix from Demo A and Demo B, choose a 100-song queue, and set track spacing to 100 (Required) and artist spacing to 20 (Preferred). The app validates feasibility and publishes an initial playable buffer before completing setup.

## Use your Spotify account

```sh
python3 -m spotify_ui --project "$PWD" --port 8766
```

Connect your Spotify developer application in the local interface, select multiple usable source playlists, choose spacing rules and queue size, then create a mix. Register the local OAuth callback required by [the Spotify setup guide](SPOTIFY_POC.md). Client secrets are not required (PKCE).

Use Preview sync to inspect changes and Sync to apply them. Automatic sync is opt-in and runs only while the Python service is running. Saved setups retain their settings across restarts. Spotify changes are real in this mode.

CLI example (replace source IDs with your own):

```sh
export SPOTIFY_CLIENT_ID=your_spotify_client_id
python3 -m spotify_poc --directory mixes/example login
python3 -m spotify_poc --directory mixes/example playlists
python3 -m spotify_poc --directory mixes/example create \
  --source SOURCE_PLAYLIST_ID_1 --source SOURCE_PLAYLIST_ID_2 \
  --name "My dynamic mix" --size 100 --rules examples/rules.json
python3 -m spotify_poc --directory mixes/example sync
```

See `python3 -m spotify_poc sync --help` before applying a plan; the default is a preview. [SPOTIFY_POC.md](SPOTIFY_POC.md) documents recovery and the other commands.

## Engine and architecture

- `playlist_engine/`: synthetic libraries, cross-source deduplication, seeded scheduling, track/artist/album/genre cooldowns, bounded lookahead, and explainable decisions. Required rules never relax; Preferred and Optional rules can relax when necessary. Genre remains available in the engine, but is absent from the app's rule editor.
- `spotify_poc/`: OAuth, paginated imports, shared request pacing and persisted Retry-After, SQLite storage, reconciliation, and durable write recovery.
- `spotify_ui/`: local web interface and optional automatic scheduler. Local dashboard polling does not call Spotify.
- `tests/` and `benchmarks/`: synthetic correctness, recovery, large-library and profiling checks.

Generate a standalone synthetic sequence:

```sh
python3 -m playlist_engine.demo --count 100 --seed 7 --output /tmp/smart-playlist-demo
python3 -m unittest discover -s tests
python3 benchmarks/large_library.py --timeout 60 --output /tmp/playlist-benchmark.json
```

Tests use mocks, synthetic data and local HTTP fixtures; they do not contact live Spotify. Benchmark timings describe the machine and fixture used, not a live service guarantee.

## Reliability and documentation

Start with [Python reliability](PYTHON_RELIABILITY.md) for current behavior and limits, [web app usage](WEB_APP.md), [API pacing](API_PACING.md), [rate limits](RATE_LIMITS.md), [progressive initialization](PROGRESSIVE_INITIALIZATION.md), and [storage retention](STORAGE_RETENTION.md). [Design V1](DESIGN_V1.md) describes the engine; [performance work](PERFORMANCE_FIX.md) explains the indexed scheduler.

Spotify may omit or delay playback events. The app cannot reconstruct unknown plays or guarantee rules against missing history. Uncertain writes are verified before retrying; a persisted cooldown blocks further Spotify calls. Automatic sync remains an experimental investigation path.

Older acceptance reports are historical records. Private live logs and runtime artifacts referenced by them remain in the original workspace and are deliberately excluded from this repository.

## Split and local data

The original `~/projects/spotify_smart_playlist` is preserved, including credentials and saved mixes. This new checkout contains source, documentation, fixtures and tests only. Start fresh here, or run this checkout's Python code with `--project ~/projects/spotify_smart_playlist` to use the existing web app data in place. Stop any other service using those data first; starting a live service resumes saved automatic-sync settings. Do not move/copy the SQLite database blindly: stored workspace paths are absolute.

Credentials, databases, local mixes, generated output and build artifacts are ignored by Git. No GitHub remote is configured by this split.
