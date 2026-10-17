# Storage retention defaults

Daily housekeeping runs when a SQLite store closes after an operation. Each workspace is checked at most once per 24 hours; no Spotify requests are made. Limits are enforced during that pass rather than immediately on every event.

- Listening history: last 200 plays or longest rule window, whichever is larger; unchanged.
- Future queue: configured size; unchanged.
- Source imports: complete active source libraries retained for resumability and reuse.
- Event logs: last 14 days and at most 5,000 events per workspace.
- Track/attribute indexes: remove entries no longer referenced by that workspace's queue, history or imported source tracks.
- SQLite caches: unused entries expire after seven days; active imported source caches and UI account labels remain.
- Legacy filesystem HTTP/source caches: expire after seven days and can be downloaded again; persisted active source imports remain.
- History-recovery backups: latest three, no older than 30 days. All such backups remain protected while the target has a pending/uncertain write.
- Removed-setup JSON backups: latest ten per project, no older than 30 days.
- Current setup, pending journals, credentials and rate-limit state: never age-pruned.

Old legacy source JSON and developer code backups are not managed by this policy. SQLite deletes free pages for later reuse; the database file will not necessarily shrink on disk. No automatic VACUUM is performed during user operations.
