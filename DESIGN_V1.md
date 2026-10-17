# Dynamic Spotify Playlist Scheduler

## 1. Purpose

The application creates and maintains dynamic Spotify playlists according to user-defined sequencing and filtering rules.

Unlike a conventional playlist generator that creates a static playlist once, this application maintains a **rolling playlist**. As songs are played, they become part of the user's play history, consumed songs are removed from the managed portion of the playlist, and new songs are selected from one or more source playlists.

Spotify remains responsible for music playback. The application is responsible for deciding **which songs should play and in what order**.

The initial product is a **local-first mobile application** and does not require an application backend.

---

# 2. Example

A user selects three Spotify playlists:

- Favorites
- Jazz
- Driving Music

The application combines the tracks from those playlists into a candidate library.

The user creates a dynamic playlist with these rules:

- Do not repeat the same track within 100 songs.
- Do not repeat an artist within 20 songs.
- Do not repeat a genre from the immediately preceding song.
- Maintain 100 upcoming songs in the Spotify playlist.

The application creates and manages a Spotify playlist such as:

**My Dynamic Mix**

As songs are played, the application records the history and generates replacement songs satisfying the rules.

The Spotify playlist therefore acts as a moving window into a much larger candidate library.

---

# 3. Design Principles

## 3.1 Local First

The application should initially operate without a dedicated backend.

Local storage contains:

- Dynamic playlist definitions
- Rules
- Cached Spotify metadata
- Source playlist state
- Play history
- Generated/scheduled tracks
- Synchronization checkpoints
- Selection/debugging information

Spotify OAuth credentials should be stored using the mobile operating system's secure credential storage rather than in the application's SQLite database.

A backend may be introduced later for features such as:

- Cross-device synchronization
- Backup
- Shared playlist definitions
- Web access
- Social features

None of these are required for V1.

---

## 3.2 Spotify Handles Playback

The application does not stream music.

Spotify is responsible for:

- Audio playback
- Playback devices
- Spotify's normal player interface
- Offline playback behavior provided by Spotify

The application uses Spotify APIs to:

- Authenticate the user
- Discover the user's playlists
- Read source playlists
- Create/manage target playlists
- Observe available playback/history information
- Synchronize generated playlist contents

---

## 3.3 Rules Engine Is Independent

The sequencing engine must not know about:

- Spotify APIs
- OAuth
- HTTP
- React Native
- SQLite
- iOS
- Android
- Background execution

Its conceptual interface is:

```python
result = generate(
    candidates=candidates,
    history=history,
    scheduled=scheduled,
    rules=rules,
    count=number_needed,
)
```

It returns:

```python
GenerationResult(
    tracks=[...],
    decisions=[...],
    relaxed_rules=[...],
)
```

This allows the scheduling engine to be developed and tested independently.

---

## 3.4 Progressive Initialization

First sync must produce a usable Spotify target as early as enabled rules permit. Use separate minimum playable and normal buffer targets (defaults 20 and 100). Import incrementally across selected sources, evaluate rule-aware readiness, publish and verify the minimum buffer, then continue setup and expansion. Readiness and setup completion are distinct states.

## 3.5 Rule-Driven Metadata Loading

Prioritize metadata required by enabled rules; nonessential enrichment must not delay playlist usability. Missing required attributes cannot silently pass required rules. Remaining import and enrichment must be resumable and expose meaningful progress.

The detailed readiness policy, progress contract, recovery behavior and acceptance tests are specified in [PROGRESSIVE_INITIALIZATION.md](PROGRESSIVE_INITIALIZATION.md). These are V1 requirements; the current CLI's blocking first import does not yet satisfy them.

---

# 4. Major Components

```text
┌─────────────────────────────┐
│         Mobile UI           │
│                             │
│ Playlist configuration      │
│ Source selection            │
│ Rule editing                │
│ Status / explanations       │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│      Application Layer      │
│                             │
│ Dynamic playlist manager    │
│ Synchronization coordinator │
│ Metadata manager            │
└───────┬─────────────┬───────┘
        │             │
        ▼             ▼
┌──────────────┐  ┌──────────────┐
│ Rules Engine │  │ Spotify Sync │
└──────────────┘  └──────┬───────┘
                         │
                         ▼
                    Spotify API

        │
        ▼
┌─────────────────────────────┐
│        Local SQLite         │
│                             │
│ Metadata                    │
│ Rules                       │
│ History                     │
│ Scheduled tracks            │
│ Sync state                  │
└─────────────────────────────┘
```

---

# 5. Spotify Account

Spotify OAuth provides authentication.

The Spotify account effectively acts as the user's identity.

V1 does not require a separate application username/password system.

Locally:

```text
spotify_account
────────────────────────
id
spotify_account_id
display_name
last_sync_at
```

OAuth access/refresh credentials should be maintained in secure OS storage.

---

# 6. Dynamic Playlists

A dynamic playlist is an application-managed definition.

```text
dynamic_playlist
────────────────────────
id
name
spotify_target_playlist_id
buffer_size
minimum_playable_buffer
enabled
created_at
updated_at
```

Example:

```text
Name:
    My Dynamic Mix

Spotify target:
    spotify_playlist_xyz

Buffer:
    100

Enabled:
    true
```

The Spotify target playlist contains the rolling sequence generated by the application.

---

# 7. Multiple Source Playlists

A dynamic playlist may have **one or more Spotify playlists as sources**.

This is a fundamental part of the data model rather than a later extension.

```text
playlist_source
────────────────────────
id
dynamic_playlist_id
spotify_source_playlist_id
enabled
weight
```

For V1:

- Multiple sources are allowed.
- All enabled sources are combined.
- Source weights default to equal treatment.
- Explicit weighting is not exposed in the initial UI.

Example:

```text
Favorites ──────────┐
                    │
Jazz ───────────────┼──► Candidate Library
                    │
Driving Music ──────┘
```

---

# 8. Candidate Library

Tracks from all enabled source playlists are combined into a candidate library.

Duplicate tracks should normally be collapsed using Spotify track identity.

However, source membership must be retained.

Example:

```text
Track X

Sources:
    Favorites
    Driving Music
    80s
```

This allows source membership to become usable by future rules.

---

# 9. Track Model

Conceptually:

```text
track
────────────────────────
spotify_id
name
album_id
duration_ms
explicit
release_date
metadata_json
last_refreshed_at
```

Artists:

```text
artist
────────────────────────
spotify_id
name
metadata_json
```

Relationship:

```text
track_artist
────────────────────────
track_id
artist_id
```

Albums:

```text
album
────────────────────────
spotify_id
name
release_date
metadata_json
```

The model should support tracks with multiple credited artists.

---

# 10. Source Playlist Cache

```text
source_playlist
────────────────────────
spotify_id
name
snapshot_id
last_synced_at
```

Membership:

```text
source_playlist_track
────────────────────────
playlist_id
track_id
source_position
```

Spotify playlist snapshot/version information should be used where available to avoid unnecessarily downloading unchanged playlists.

---

# 11. Rule Model

Rules are first-class application objects.

```text
rule
────────────────────────
id
dynamic_playlist_id
type
window
strength
priority
parameters_json
enabled
```

The flexible parameter representation allows new rule types without database migrations for every rule.

---

# 12. Initial Rule Types

V1 should support at least:

## Track repetition

Example:

```text
Do not repeat a track within 100 songs.
```

Internal representation:

```text
type: track_repeat
window: 100
strength: required
```

---

## Artist repetition

Example:

```text
Do not repeat an artist within 20 songs.
```

If a candidate has multiple credited artists, the candidate conflicts if any credited artist violates the window.

---

## Album repetition

Example:

```text
Do not repeat an album within 30 songs.
```

This can be implemented even if it is not exposed in the very first UI.

---

## Genre repetition

Example:

```text
Do not repeat a genre from the previous song.
```

For tracks with multiple genres, a conflict occurs when the candidate genre set intersects with the prohibited recent genre set.

Genre metadata should be treated as an **enriched/derived attribute**, rather than tightly coupling the rules engine to Spotify's genre metadata.

---

# 13. Rule Strength

Rules should support at least:

```text
REQUIRED
PREFERRED
OPTIONAL
```

Required rules may never be automatically violated.

Preferred and optional rules can be relaxed if necessary to continue generating a playlist.

Example:

```text
Track repeat 100      REQUIRED
Artist repeat 20      REQUIRED
Genre repeat 1        PREFERRED
```

---

# 14. Rule Failure

The engine must explicitly handle cases in which no candidate satisfies all active rules.

Example:

A source library contains 50 songs while the user requires:

```text
Track repeat window = 1000
```

Eventually no legal candidate exists.

The engine should attempt:

```text
All rules
    ↓
No candidates

Relax OPTIONAL
    ↓
No candidates

Relax PREFERRED
    ↓
No candidates

Required constraints impossible
    ↓
Stop generation
```

Required rules must not be silently violated.

The application should explain why generation stopped.

---

# 15. Played History vs. Scheduled Future

These are separate concepts.

## Played History

Tracks that the user actually listened to.

```text
play
────────────────────────
id
dynamic_playlist_id
track_id
played_at
sequence_number
```

## Scheduled Future

Tracks that have already been generated but may not yet have been played.

```text
scheduled_track
────────────────────────
id
dynamic_playlist_id
track_id
position
generated_at
```

Rules must consider:

```text
played history
+
already scheduled future
```

when generating additional tracks.

Otherwise the generator could legally schedule duplicates merely because earlier scheduled tracks have not yet been played.

---

# 16. Sequencing Algorithm

The simplest implementation would randomly choose among currently valid candidates.

That is insufficient because a legal choice now can make future positions difficult or impossible.

Instead, generation should use:

1. Constraint filtering
2. Candidate scoring
3. Limited lookahead
4. Controlled randomness

Conceptually:

```text
Candidate Library
        ↓
Apply required rules
        ↓
Apply preferred rules
        ↓
Score remaining candidates
        ↓
Estimate future flexibility
        ↓
Choose among high-quality candidates
        ↓
Append candidate
        ↓
Repeat
```

---

# 17. Candidate Scoring

Potential scoring inputs include:

```text
track recency
artist recency
album recency
genre diversity
source diversity
future flexibility
randomness
```

The engine should not simply select the maximum deterministic score.

Controlled randomness among good candidates prevents identical input from always generating exactly the same sequence.

---

# 18. Lookahead

For each candidate, the engine can estimate how constrained the next positions become.

Example:

```text
Candidate A
    next-position candidates: 823

Candidate B
    next-position candidates: 14

Candidate C
    next-position candidates: 691
```

Candidate B is technically legal but may be strategically poor.

V1 does not need exhaustive optimization.

A small lookahead depth or inexpensive future-flexibility heuristic should be sufficient initially.

---

# 19. Explainability

Every generated track should optionally have a decision record.

Example:

```json
{
  "track": "spotify_track_xyz",
  "position": 381,
  "eligible_candidates": 437,
  "scores": {
    "track_recency": 1.0,
    "artist_recency": 0.84,
    "genre_diversity": 1.0,
    "future_flexibility": 0.91,
    "random": 0.12
  },
  "relaxed_rules": []
}
```

This information is primarily valuable for:

- Debugging
- Testing
- Understanding unexpected sequences
- Improving the algorithm

A simplified version may later be exposed to users:

```text
Why this song?

Track last played:
    213 songs ago

Artist last played:
    46 songs ago

Genre:
    Different from previous song
```

---

# 20. Rolling Playlist

Each dynamic playlist has a configurable buffer.

Example:

```text
buffer_size = 100
```

The application attempts to maintain approximately 100 upcoming tracks in Spotify.

Example:

```text
Initial target:

A B C D E ... [100 tracks]
```

The user listens to:

```text
A B C D E
```

At the next successful synchronization:

```text
5 consumed
95 remaining
```

The engine generates five replacements:

```text
FUTURE1
FUTURE2
FUTURE3
FUTURE4
FUTURE5
```

The playlist returns to approximately 100 upcoming tracks.

---

# 21. Background Synchronization

The application should **not depend upon continuously running in the background**.

Mobile operating systems control when suspended applications receive background execution time.

Synchronization should therefore be:

- Opportunistic
- Incremental
- Idempotent
- Recoverable after interruption

Every synchronization begins by determining what changed since the last successful checkpoint.

---

# 22. Synchronization State Machine

Conceptually:

```text
WAKE
 │
 ▼
Ensure Spotify authorization
 │
 ▼
Check source playlists
 │
 ├── changed → synchronize membership/metadata
 │
 ▼
Determine playback since checkpoint
 │
 ▼
Record newly consumed tracks
 │
 ▼
Fetch current target playlist
 │
 ▼
Reconcile Spotify state with local state
 │
 ▼
Remove/recognize consumed entries
 │
 ▼
Generate enough tracks to refill buffer
 │
 ▼
Update Spotify target
 │
 ▼
Commit synchronization checkpoint
 │
 ▼
SLEEP
```

If execution is interrupted, the next run should safely restart synchronization.

---

# 23. User Modification of Managed Playlists

Users may manually edit a generated playlist directly in Spotify.

The application must not assume its local scheduled state remains authoritative.

During synchronization:

```text
Spotify actual playlist
        ↓
compare
        ↓
Locally expected playlist
```

Unexpected differences should be detected.

Possible future policies:

```text
Keep my Spotify changes
Restore generated playlist
Ask me
```

For V1, user changes should be handled conservatively rather than silently overwritten whenever practical.

---

# 24. Play Detection

The application needs a consistent definition of what counts as a played track.

Possible future definitions include:

- Spotify reports the track in recent history
- Minimum number of seconds played
- Minimum percentage played
- Track completed

V1 should use the most reliable playback/history information available through Spotify and document the exact behavior.

The playlist engine should not itself determine whether something was played. The synchronization layer converts Spotify activity into normalized `play` records.

---

# 25. Metadata Providers

The rules engine should not assume Spotify is the sole provider of track attributes.

Attributes should conceptually come from providers.

Example:

```text
Spotify metadata
    ├── track
    ├── artist
    ├── album
    ├── duration
    └── release date

Enrichment metadata
    ├── genre
    ├── subgenre
    ├── BPM
    ├── energy
    ├── mood
    └── other classifications
```

This is particularly important for genre, since Spotify's currently exposed genre metadata should not be assumed to be complete or permanent.

---

# 26. Future Source Rules

Source playlist membership should eventually be usable as a rule attribute.

Examples:

```text
At least one song from "New Music"
every 10 tracks.
```

```text
No more than three consecutive songs
from "Jazz".
```

```text
Prefer Favorites over Discoveries
by approximately 2:1.
```

These are not required for the first implementation, but the data model should not prevent them.

---

# 27. Future Source Weighting

Each source already has a conceptual `weight`.

V1 treats sources equally.

Later:

```text
Favorites       50%
Jazz            30%
New Music       20%
```

Weight should influence selection rather than requiring strict deterministic percentages unless the user explicitly chooses a quota-style rule.

---

# 28. Initial Mobile UI

V1 should remain deliberately small.

Primary screens:

## Spotify Login

Authenticate the user.

## Dynamic Playlists

Display locally defined dynamic playlists and synchronization status.

## Create/Edit Dynamic Playlist

Configure:

- Name
- One or more source Spotify playlists
- Target playlist
- Buffer size

## Rules

Initially expose simple controls such as:

```text
Do not repeat:

Track       [100] songs
Artist      [ 20] songs
Album       [  0] songs
Genre       [  1] song
```

Each rule can be enabled/disabled and assigned a strength.

## Status

Show:

- Last synchronization
- Candidate library size
- Upcoming buffer size
- Generation warnings
- Rule conflicts

---

# 29. Technology Direction

A reasonable mobile implementation is:

```text
React Native / Expo
        │
        ├── SQLite
        │
        ├── Secure credential storage
        │
        ├── Background task support
        │
        └── Spotify OAuth/API
```

The rules engine can initially be prototyped in Python independently of the mobile application.

Whether the final mobile rules engine remains Python-derived or is ported/reimplemented in TypeScript should be decided after the algorithm is proven.

---

# 30. Development Sequence

## Phase 0 — Spotify Capability Verification

Before substantial implementation, verify the exact current Spotify API behavior required for:

- OAuth
- Reading the user's playlists
- Reading playlist contents
- Creating target playlists
- Adding/removing/reordering tracks
- Playback/recent history
- Available metadata
- Background OAuth refresh behavior

Create a minimal proof-of-concept where useful.

---

## Phase 1 — Standalone Rules Engine

Build:

```text
sample_library.json
rules.json
        ↓
playlist_engine
        ↓
generated_playlist.json
decision_log.json
```

No Spotify dependency.

Generate synthetic libraries containing:

- Thousands of tracks
- Hundreds of artists
- Multiple artists per track
- Albums
- Genres
- Multiple source memberships

---

## Phase 2 — Engine Testing

Test:

- Track cooldowns
- Artist cooldowns
- Album cooldowns
- Genre diversity
- Multiple simultaneous rules
- Tiny candidate pools
- Impossible rules
- Rule relaxation
- Long generated sequences
- Multiple source playlists
- Duplicate tracks across sources
- Lookahead behavior

Run simulations involving tens or hundreds of thousands of generated positions to uncover pathological behavior.

---

## Phase 3 — SQLite Persistence

Implement the local domain model and repositories.

The engine should still operate independently from SQLite through application/domain interfaces.

---

## Phase 4 — Spotify Integration Prototype

Implement:

- OAuth
- Playlist discovery
- Multiple-source import
- Candidate construction
- Target playlist creation
- Target playlist synchronization

Initially synchronization can be manually triggered.

---

## Phase 5 — End-to-End Prototype

Complete the loop:

```text
Spotify sources
      ↓
candidate library
      ↓
rules engine
      ↓
target Spotify playlist
      ↓
user playback
      ↓
play detection
      ↓
history
      ↓
rules engine
      ↓
refill target
```

---

## Phase 6 — Mobile UI

Build the actual configuration and status interface.

At this stage the underlying system should already work.

---

## Phase 7 — Background Synchronization

Add opportunistic background execution.

Test behavior under:

- App termination
- Device reboot
- Network loss
- Spotify token expiration
- Long periods without app execution
- Source playlist modification
- Target playlist modification
- Extended listening sessions

---

# 31. V1 Scope

V1 should answer one question exceptionally well:

> Can a user select several of their Spotify playlists, define rotation rules, press play in Spotify, and have the application maintain a good continuously changing playlist according to those rules?

V1 includes:

- Spotify OAuth
- Local-only application state
- Multiple Spotify source playlists
- Deduplicated candidate library
- Rolling Spotify target playlist
- Track cooldown rules
- Artist cooldown rules
- Album cooldown capability
- Genre rule architecture
- Required/preferred/optional rules
- Local play history
- Rule-aware sequence generation
- Limited lookahead
- Explainable generation decisions
- Manual synchronization
- Background synchronization when practical

V1 does **not** require:

- Application backend
- Separate application accounts
- Cross-device synchronization
- Social features
- Shared dynamic playlists
- Streaming audio
- AI recommendations
- Large-scale discovery
- Source weighting UI
- Advanced source quotas
- Web application

---

# 32. Longer-Term Possibilities

The architecture deliberately leaves room for:

- Source weighting
- Source quotas
- More sophisticated rotation rules
- BPM rules
- Energy progression
- Mood rules
- Decade/release-year balancing
- Explicit-content rules
- Artist/album inclusion and exclusion
- Genre enrichment
- Rules such as "at least once every N songs"
- Rules such as "no more than N within M songs"
- Playlist templates
- Import/export of playlist recipes
- Cross-device state
- Cloud backup
- Web/desktop versions
- Alternative music services

---

# 33. Core Product Concept

The application should ultimately be thought of not merely as a Spotify playlist generator, but as a:

**user-configurable music rotation and sequencing engine**

Spotify is initially the music catalog, authentication provider, playlist store, and playback environment.

The application's value lies in maintaining state over time and making intelligent sequencing decisions according to rules the user controls.