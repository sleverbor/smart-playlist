# Spotify endpoint review and pacing

Reviewed official documentation on October 8, 2026. None of the endpoint pages below supplies a numeric requests-per-second ceiling. Spotify documents an application-wide rolling 30-second limit whose threshold depends on quota mode, plus possible endpoint-specific exceptions. The numbers implemented here are our conservative operational policy, not Spotify guarantees.

| Used endpoint | Documentation | Documented batching | Our default pacing |
|---|---|---|---|
| GET /v1/me | [Profile](https://developer.spotify.com/documentation/web-api/reference/get-current-users-profile) | Single account | Shared read budget |
| GET /v1/me/playlists | [List playlists](https://developer.spotify.com/documentation/web-api/reference/get-a-list-of-current-users-playlists) | Up to 50 per page | Shared read budget |
| GET /v1/playlists/{id} | [Playlist metadata](https://developer.spotify.com/documentation/web-api/reference/get-playlist) | One playlist; snapshot_id enables source reuse | Shared read budget |
| GET /v1/playlists/{id}/items | [Playlist items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items) | Up to 50 per page | Shared read budget |
| GET /v1/me/player/recently-played | [Recent playback](https://developer.spotify.com/documentation/web-api/reference/get-recently-played) | Up to 50 per page | Shared read budget; current implementation reads latest page |
| POST /v1/me/playlists | [Create playlist](https://developer.spotify.com/documentation/web-api/reference/create-playlist) | One playlist | Shared write budget |
| PUT /v1/playlists/{id}/items | [Replace items](https://developer.spotify.com/documentation/web-api/reference/reorder-or-replace-playlists-items) | Up to 100 per request | Shared write budget |
| POST /v1/playlists/{id}/items | [Append items](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist) | Up to 100 per request | Shared write budget |
| POST accounts.spotify.com/api/token | [PKCE exchange](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow), [refresh](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens) | One exchange; refresh only near expiry | Separate per-application OAuth gate, 5 seconds |

## Implemented policy

All Web API reads and writes share the application's persisted rate-state file across local accounts/workspaces using the same client ID. At least 3 seconds between any calls, at least 5 seconds between write calls, and at most 10 reservations per rolling 30 seconds. Read endpoints have the same policy because their documentation does not provide different ceilings. OAuth uses a separate host-specific shared gate for the project and client ID; authorization browser navigation is user-driven, not API polling.

Every Web API 429 doubles the persisted pacing multiplier up to 8, so the next session stays slower. It does not automatically ramp back up. Retry-After extends the global deadline and never shortens a prior deadline. Retries remain bounded, and ambiguous writes are not blindly retried. OAuth 429 saves its cooldown and stops without replaying a potentially consumed authorization code. UI operations return promptly for long cooldowns. Existing cooldown deadlines are preserved.

SPOTIFY_REQUEST_INTERVAL or an explicit Client interval selects a manual pacing override, bypassing the conservative rolling/adaptive policy; use only for tests or deliberately configured operation. SPOTIFY_RATE_STATE overrides the shared state location and can separate clients if configured inconsistently. The default UI/CLI account path uses one gate per client ID.

Source snapshot caching and existing HTTP conditional caching remain enabled; target/pre-write/playback verification remains fresh. No time-based metadata cache was added to critical guards. Fixed pacing cannot promise avoidance of every 429, and requests by other applications/processes not sharing our gate cannot be accounted for.

[Official rate-limit guidance](https://developer.spotify.com/documentation/web-api/concepts/rate-limits) distinguishes quotas from rate limits, explains rolling windows and Retry-After, and recommends batching and snapshot reuse. This change was validated offline; no requests were sent to Spotify to probe its current cooldown or determine a threshold.
