> Historical document. Current Python behavior and limitations are described in [PYTHON_RELIABILITY.md](PYTHON_RELIABILITY.md); some recovery policies below have since changed.

# Application readiness results — October 8, 2026

Implemented in `/home/think/projects/spotify_smart_playlist`: SQLite persistence and reversible JSON migration, shared account login and client-wide local cooldown, indexed source/track/history/future data, structured progress and cancellation, offline status/export, a UI-independent service, guarded preview/apply/recovery and source-access checks. Existing Spotify targets were not modified by the acceptance work. Existing local JSON state remains preserved; use SQLite status/export going forward.

112 automated tests pass. They use synthetic data and simulated HTTP responses, not live Spotify. They include migration/rollback, cache and 304 handling, persisted 429 diagnostics, cooperative cancellation, cross-workspace isolation, shared login/cooldown, stale playback/target checks, durable pending writes, safe interrupted resume and explicit history-gap recovery. The large-library checks include the core track=100 / artist=20 / genre=1 case, overlaps, deduplication, limited candidates and impossible constraints.

## Measured offline profiles

| Case | Work | Time | Peak process memory |
|---|---|---:|---:|
| SQLite import | Two overlapping 10,000-entry sources; 15,000 unique tracks; 400 pages | 3.71 s | 59 MiB |
| SQLite reconstruction | Read the 20,000-entry checkpoint | 0.096 s | Included above |
| Compact engine logs | 10,000 candidates; 100 positions; track=100 / artist=20 / genre=1; depth=0 | 7.44 s engine time | 58 MiB |
| Full engine logs | Same scheduling case, detailed per-candidate explanations | 13.93 s engine time | 903 MiB after serialization |

These are local measurements, not Spotify download timings. The CLI uses compact decision logs. Full logs generated about 147 MB of JSON in this case, compared with 73 KB compact. An unchanged source checkpoint updates only source metadata and its document (three SQL row changes); it does not rewrite all imported tracks.

## Live checks completed so far

The existing account was verified and its login reused. Two private, clearly named test sources contain 80 real entries each with 40 intentional overlapping track IDs. Spotify reported 52 unavailable/unusable entries; deduplication left 79 usable unique candidates. The dedicated target is https://open.spotify.com/playlist/5V1Odg2DqrV1gVXnDgXGsJ . A 25-song queue was written and read back successfully; the source import completed.

The first attempt built a valid 10-song starter after 100 of 160 entries, but actual playback changed immediately before the target write. The guard stopped publication. Recovery reused the same target, reconciled observed playback and completed the fill without a duplicate target creation. This demonstrates the live guard and recovery; it does not yet prove successful early starter publication on that first attempt. Its second invocation used 108 Web API requests, including fixture verification, pending recovery, resumption and final checks; it had four cache hits, no 429 responses, and 68 seconds of pacing wait. These figures are not the cost of a clean first initialization.

## Live progressive acceptance

The separate target https://open.spotify.com/playlist/7MuLVbM5Bs8WB6YZmXIfD5 successfully published and verified its first 10 songs in 30.47 seconds after importing 100 of 160 source entries. Pages remained unimported at Ready. The test deliberately interrupted there, then resumed the same target without duplicate creation and expanded it to 25 verified songs. Full setup finished in 77.28 seconds. Initial fill used 31 Web API requests; resume used 47. No 429 responses occurred.

## Live consumption/refill acceptance passed

After the user played the first two target songs and paused, Spotify initially returned no new events. A later retry reported exactly two new plays and two consumed target songs. Sync retained the remaining 23 scheduled songs in order, generated two replacements, updated the target and verified a 25-song queue by readback. Sources were unchanged and no external target edit was detected. This completes the live consumption/refill acceptance test.

The earlier reads, including a time-bounded query, still showed a newest event at 12:35 PM Pacific and neither test song. The observed delay was not bypassed: those sync attempts retained the queue and made no target write. The successful retry used actual Spotify history rather than inferred consumption. The precise cause of the earlier missing events was not established.

The controlled live history-gap recovery check passed on the separate progressive target. Ordinary sync stopped on the stale bookmark. Explicit recovery used 50 observed recent events, retained all 25 valid future positions, verified the target contents, and saved ready state. The report explicitly records consumption_known=false and older_playback_reconstructed=false. The primary listening-test baseline was preserved. The gap was manufactured through a stale local bookmark; it does not represent 50 real songs or reconstruct missing plays.

The application foundation and the planned live acceptance loop are ready for UI integration. The live checks cover real source import/deduplication, target creation and verified fill, progressive readiness, interruption/resume, consumption/refill, and controlled history-gap recovery. Deployment-wide rate coordination, production credential protection and a durable background worker belong to the real application. Spotify has no atomic compare-and-swap spanning playback and playlist updates; guards cannot eliminate changes after their final check.
