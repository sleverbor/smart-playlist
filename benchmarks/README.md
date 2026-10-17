# Offline profiling and optional user-flow instrumentation

Run from the repository root (Python 3.10+, no extra dependencies):

```sh
python3 benchmarks/large_library.py --timeout 60 --output /tmp/playlist-results.json
python3 benchmarks/large_library.py --timeout 60 --output /tmp/profile-results.json --profile-directory /tmp/playlist-profiles
python3 benchmarks/profile_storage.py --help
```

`large_library.py` uses isolated child processes and hard timeouts, with synthetic 1,000/10,000-entry libraries and overlapping sources. It measures deduplication, generation, logging, counters and process peak RSS. A timeout is a failed latency probe, not a successful generation. Profiling changes timings. `reference_engine.py` retains the scan-based reference for parity checks; see `PERFORMANCE_FIX.md` in the repository root.

`profile_storage.py` measures synthetic SQLite import/checkpoint workloads. These scripts do not authenticate or contact Spotify.

`user_session.py` and `user_flow.mjs` are optional local-app instrumentation harnesses, not offline unit tests. Inspect their help/source before using them with a signed-in app; user actions may modify real playlists. The browser harness accepts a computer-use browser tab exposing the documented Playwright interface. Private historical live reports and profiling outputs were deliberately not copied during the repository split. Fresh reports should go to /tmp or another ignored location.
