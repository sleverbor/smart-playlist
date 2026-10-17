# Playback identity and automatic continuation

Track objects retain optional recording IDs (ISRC), Spotify linked-from aliases and duration. Older saved objects load with empty defaults; imported source metadata enriches legacy queues during planning. Playlist writes retain Spotify IDs. Track cooldowns include known aliases and recording IDs.

Target-context playback first matches Spotify identity/linked-from or shared ISRC. If none matches, exactly one queued song must have the same case-insensitive trimmed title, identical artist IDs, and positive durations within one second. This is contextual attribution, not a global merge of recordings. Multiple matching queued versions remain unconsumed. Decision logs record observed ID, queued ID and match method. The observed playback ID remains in history, with the attributed queue ID as an alias so its cooldown holds. Title alone never matches.

Progressive setup automatically rereads and replans after playback, target or download races, up to three attempts per publication. Strict snapshot/content/playback preconditions remain in force; no unchecked overwrite is introduced. Persistent changes preserve the verified buffer, finish source import, and defer expansion. The saved setup and progress events include reason and attempt counts. Cancellation and rate-limit handling remain active on every request.

Spotify documentation: https://developer.spotify.com/documentation/web-api/concepts/track-relinking
