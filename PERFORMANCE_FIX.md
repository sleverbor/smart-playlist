# Indexed scheduling performance fix

The scheduler no longer scans all candidates separately for every lookahead candidate. Attribute-to-candidate bitset indexes identify tracks blocked by track, artist, album and genre cooldowns. Future flexibility counts eligible candidates with bit operations. One context index per generated position supplies nearest-play recency and violation details. Candidate ordering, lookahead branches, scoring, rule strengths, relaxation order and seeded selection remain unchanged.

Exact full-result comparison tests use the frozen pre-change engine in `benchmarks/reference_engine.py`. They compare complete decision logs across 12 randomized libraries and all four supported lookahead depths, plus multi-artist/genre, repeated-kind rules, external history and scheduled prefixes. A separate test verifies that 10,000-candidate depth-1 generation performs no full-pool eligibility checks. This reference is for tests, not production.

Standalone generation defaults to full diagnostic logs. The Spotify CLI uses `Config(decision_detail='compact')`: selected-track scores and violations, rule relaxation attempts, per-rule blocked counts, shortlist size and prefix issues remain available without retaining every candidate's score/violation data at every position. Compact mode preserves track choices and selected explanations. Use full mode for exhaustive candidate diagnostics. Bitset counts are exact, not sampling or a reduced candidate pool.

Recovery now prints generated-position progress to stderr and a concise preview/report to stdout. Applying still saves the decision report, checks target ownership/snapshot/readback and journals pending writes. A preview still changes neither the playlist nor the saved scheduling baseline.

## Measured results

Offline synthetic measurements on this host; these exclude Spotify network waits and are not a guarantee for your exact source library:

| Case | Result |
| --- | --- |
| 10,000 candidates, 20 positions, depth-1 lookahead, full logs | Completed in 5.59 s wall time; original implementation timed out at 12 s before the first checkpoint |
| 3,504 candidates, 50 historical plays, 100 positions, track=100 REQUIRED / artist=20 REQUIRED / album=10 OPTIONAL, compact logs | 2.98 s generation; 75,174 bytes of JSON; approximately 28 MiB process peak RSS |
| 10,000 candidates, 100 positions, track=100 / artist=20 / genre=1 REQUIRED, depth-1, compact logs | 10.71 s generation; 72,956 bytes of JSON; approximately 59 MiB process peak RSS |

The 3,504-candidate full-log indexed case used about 57 MB of JSON and 347 MiB RSS. Compact logs eliminate that diagnostic-storage cost. The 10,000-candidate full-log 100-position case still approached 903 MiB RSS: full diagnostics are deliberately expensive and should not be the routine app mode.

Results: `benchmarks/indexed-results.json`, `benchmarks/recovery-compact-results.json`, `benchmarks/normal-compact-results.json`. The original measurements remain in `benchmarks/results.json`. Fixed fixtures and seed are described in the benchmark guide. Further optimization and application-wide memory/latency budgets may still be needed for larger unions and deeper lookahead; this change does not add a database or parallel workers.

An already-running Python process keeps its old loaded scheduler. If the previous recovery command is still running, press Ctrl-C, then restart:

```sh
python3 -m spotify_app recover-history
python3 -m spotify_app recover-history --apply
```

If its saved state is pending instead of ready, inspect the target and finish the existing pending write with `python3 -m spotify_app recover --apply` first. Do not start another copy against the same state directory while the old process owns its lock. Installing/testing this fix did not kill the running process or mutate Spotify.
