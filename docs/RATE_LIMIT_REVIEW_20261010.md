# Cooldown and source-selector fix

The recorded 429 occurred at October 10, 2026, 7:26:43 PM Pacific during a read of the Saved mix target's playlist items. Spotify supplied Retry-After=54224 seconds; the saved deadline is October 11 at 10:30:27 AM Pacific. The operation recorded 19 requests across 108.003 seconds, with a minimum inter-request gap of 5.9997 seconds. These events establish the triggering request and pacing within this operation. They do not establish Spotify's underlying reason or app-wide quota consumption.

Recent source-loading attempts stopped at the saved deadline before contacting Spotify. Changing the frontend did not clear or extend the cooldown.

The mobile create screen now obtains cached source playlists from a read-only local endpoint. It validates the connected account, filters registered generated targets, and returns the active cooldown. The interface shows the deadline beside refresh, permits selection from saved sources and rule adjustments, and disables refresh and creation during cooldown. Status rechecks read local state without Spotify calls. Saved source lists are labeled because they may be stale.

Verification: 17 UI/preview tests passed, including cached sources during cooldown without network calls, generated-target filtering, unknown-account rejection and proxy mutation protections. Mobile lint/typecheck and Android/iOS/web bundle exports passed. Browser verification showed 15 real-account saved source playlists, selectable while refresh and creation remained disabled. The local service was restarted after confirming no active operation. The cooldown was preserved.
