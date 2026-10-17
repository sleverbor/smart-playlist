"""Checkpointed round-robin import and progressive publication."""
from uuid import uuid4
from .client import SpotifyError, PlaylistChanged, read_json, save_json
from .sync import adapt, rules_from, bootstrap, plan, load, validate_feasibility
from playlist_engine import library_from_playlists

POLICY = 'diversity-reserve-v1'


def emit(message):
    print(message, flush=True)


def candidates(job, genres):
    pools, skipped = {}, 0
    for identity, source in job['imports'].items():
        pools[identity] = []
        for raw in source['tracks']:
            track = adapt(raw, genres)
            if track:
                pools[identity].append(track)
            else:
                skipped += 1
    return library_from_playlists(pools), skipped


def confidence(pool, rows, minimum, complete):
    rules = rules_from(rows, [])
    requirements = {'track': minimum * 3}
    for rule in rules:
        if rule.enabled and rule.window and rule.strength.value == 'REQUIRED':
            requirements[rule.kind] = max(requirements.get(rule.kind, 0), rule.window + 1)
    counts = {'track': len(pool), 'artist': len({a for t in pool for a in t.artists}),
              'album': len({t.album for t in pool}),
              'genre': len({g for t in pool for g in t.genres if g != '__unknown__'})}
    blockers = [f'{kind} diversity {counts[kind]}/{needed}' for kind, needed in requirements.items()
                if counts[kind] < needed]
    # Exhaustive import permits a full engine trial even when reserve heuristics fail.
    return (not blockers or complete), dict(policy=POLICY, counts=counts,
                                             requirements=requirements, blockers=blockers,
                                             partial_library=not complete)


def run(client, root, account, apply, ids, sources=None, rows=None, minimum=20,
        normal=100, seed=0, name='Smart playlist prototype', genres=None, force=False, store=None, on_progress=None):
    reader=store.read if store else read_json
    writer=store.write if store else save_json
    def say(message):
        if on_progress:
            if message.startswith('Ready to play in Spotify'):
                on_progress('ready',message=message,target=state['target'],queue_size=len(state['queue']),setup_complete=False)
            else:
                on_progress('setup',message=message)
        else:
            emit(message)
    def generation(done,total):
        if on_progress:
            on_progress('generating',message='Building playlist',completed=done,total=total)
        else:
            emit(f'Building initial playlist: {done}/{total}')
    job_path, state_path = root / 'setup.json', root / 'state.json'
    job = reader(job_path)
    if sources is not None:
        existing_state = reader(state_path)
        if existing_state:
            raise SpotifyError('Existing target or write state; use resume/recovery or a separate directory. --force cannot replace it')
        if job and not force:
            raise SpotifyError('Existing setup; use resume, create --force for an unpublished setup, or a separate directory')
        if job and job['account'] != account:
            raise SpotifyError('Account mismatch; --force cannot replace another account setup')
        if not 1 <= minimum <= normal <= 10000:
            raise SpotifyError('Buffers must satisfy 1 <= minimum <= normal <= 10000')
        rules_from(rows, [])
        if not sources:
            raise SpotifyError('At least one source is required')
        if job:
            backup = root / ('setup-backup-' + uuid4().hex + '.json')
            writer(backup, job)
            say(f'Previous unpublished setup backed up to {backup.name}')
        job = dict(account=account, sources=sources, rules=rows, minimum=minimum,
                   normal=normal, seed=seed, name=name, genres=genres or {}, imports={}, status='importing')
        writer(job_path, job)
    if not job or job['account'] != account:
        raise SpotifyError('No setup for this account')
    if genres is not None:
        if reader(state_path) and genres != job.get('genres', {}):
            raise SpotifyError('Genre mapping cannot change after publication; use a separate setup')
        job['genres'] = genres
    genres = job.get('genres', {})
    state = reader(state_path)
    if state and state['status'] != 'ready':
        raise SpotifyError('Incomplete remote write; recover before resume')
    say(f"Connected to Spotify; found {len(job['sources'])} source playlists")
    # Revalidate each source before using cached pages; changed sources restart locally.
    for source in job['sources']:
        metadata = client.request('GET', '/playlists/' + source)
        saved = job['imports'].get(source)
        from .client import Client
        cached=client.cache_read(client.cache / ('source-' + source + '.json')) if isinstance(client,Client) and source not in client.no_store_playlists else None
        if cached and cached.get('snapshot')==metadata['snapshot_id'] and (not saved or saved.get('next') or saved['snapshot']!=metadata['snapshot_id']):
            tracks=[r.get('item',r.get('track')) for r in cached['rows']]
            job['imports'][source]=dict(snapshot=metadata['snapshot_id'],tracks=tracks,next=None,total=len(tracks),sampled=True,loaded=len(tracks))
            client.metrics['source_cache_hits']+=1
            continue
        if not saved or saved['snapshot'] != metadata['snapshot_id']:
            invalidate = getattr(client, 'invalidate_playlist', None)
            if callable(invalidate):
                invalidate(source)
            job['imports'][source] = dict(snapshot=metadata['snapshot_id'], tracks=[],
                next='/playlists/' + source + '/items?limit=50', total=None, sampled=False, loaded=0)
    writer(job_path, job)

    def publish(complete):
        nonlocal state
        if state and not complete and len(state['queue'])>=job['normal']:
            say('Playable buffer is full; continuing source import')
            return
        pool, skipped = candidates(job, genres)
        loaded = sum(s['loaded'] for s in job['imports'].values())
        totals = [s['total'] for s in job['imports'].values()]
        total = sum(totals) if all(t is not None for t in totals) else '?'
        if on_progress:
            on_progress('importing',message='Loading source tracks',completed=loaded,total=total if total!='?' else None,unique_candidates=len(pool),skipped=skipped)
        say(f'Loaded {loaded}/{total} source entries; {len(pool)} unique candidates; {skipped} skipped')
        snapshots = {i: s['snapshot'] for i, s in job['imports'].items()}
        ready, evidence = confidence(pool, job['rules'], job['minimum'], complete)
        job['confidence'] = evidence
        writer(job_path, job)
        say(f"Sources sampled: {sum(s['sampled'] for s in job['imports'].values())}/{len(job['sources'])}; complete: {sum(not s['next'] for s in job['imports'].values())}/{len(job['sources'])}")
        for source, snapshot in snapshots.items():
            if client.request('GET', '/playlists/' + source)['snapshot_id'] != snapshot:
                raise SpotifyError('Source changed; resume to restart affected import')
        if not state and not ready:
            say('Waiting for ' + ', '.join(evidence['blockers']))
            return
        try:
            rules_from(job['rules'], pool)
        except SpotifyError as exc:
            job['blocked'] = str(exc)
            say('Metadata needed: ' + str(exc))
            if complete:
                raise
            return
        if not state:
            try:
                initial_events=client.recent()
                trial, report = bootstrap(pool, job['sources'], snapshots, initial_events, genres,
                    job['rules'], job['minimum'], job['seed'], account,
                    progress=generation)
                validate_feasibility(pool,load(trial['history']),job['rules'],job['normal'],progress=generation)
            except SpotifyError as exc:
                if complete:
                    raise
                say('Initial rules are not feasible yet: ' + str(exc))
                return
            if len(trial['queue']) < job['minimum']:
                say(f"Initial trial built {len(trial['queue'])}/{job['minimum']}; importing more")
                if complete:
                    raise SpotifyError('Full import cannot produce the minimum buffer under REQUIRED rules')
                return
            say(f"Building initial playlist: {len(trial['queue'])}/{job['minimum']} ready")
            # Recheck snapshots before any initial target creation.
            for source, snapshot in snapshots.items():
                if client.request('GET', '/playlists/' + source)['snapshot_id'] != snapshot:
                    raise SpotifyError('Source changed; resume to restart affected import')
            if store:
                from .sync import ordered,key
                from .writes import PreconditionFailed
                initial_keys=[list(key(event)) for event in ordered(initial_events)]
            trial.update(size=job['normal'], status='creating')
            writer(state_path, trial)
            target = client.request('POST', '/me/playlists', dict(name=job['name'], public=False))
            trial.update(target=target['id'], status='pending')
            writer(state_path, trial)
            if store:
                created_meta,created_rows=client.playlist(trial['target'])
                if ids(created_rows):
                    raise PreconditionFailed('New target was edited before first fill; inspect pending state')
                trial['write_guard']=dict(snapshot=created_meta['snapshot_id'],events=initial_keys,actual=[])
                writer(state_path,trial)
            apply(client, state_path, trial)
            state = trial
            writer(root / 'decision-log.json', dict(confidence=evidence, engine=report))
            if not complete:
                say('Starter buffer uses partial source samples; remaining import may broaden future choices')
            say(f"Ready to play in Spotify: {len(state['queue'])} songs\nhttps://open.spotify.com/playlist/{state['target']}\nFinishing setup…")
            return
        if not complete and len(state['queue'])>=job['normal']:
            say('Playable buffer is full; continuing source import')
            return
        metadata, target_rows = client.playlist(state['target'])
        # Expand in bounded increments and reconcile playback before every write.
        working = dict(state, size=min(job['normal'], len(state['queue']) + job['minimum']))
        events=client.recent()
        updated, report = plan(working, pool, snapshots, ids(target_rows), events, genres,
            progress=generation,allow_history_gap=True)
        updated['size'] = job['normal']
        if store:
            from .writes import guard
            from .sync import ordered, key
            event_keys=[list(key(event)) for event in ordered(events)]
            metadata,_=guard(client,state['target'],metadata['snapshot_id'],event_keys,ids(target_rows))
            updated['write_guard']=dict(snapshot=metadata['snapshot_id'],events=event_keys,actual=ids(target_rows))
        staged=dict(state,status='pending',pending_update=updated,pending_report=dict(confidence=evidence,sync=report))
        if 'write_guard' in updated:staged['write_guard']=updated.pop('write_guard')
        writer(state_path, staged)
        try:
            apply(client, state_path, staged)
        except Exception as exc:
            from .writes import PreconditionFailed
            if store and isinstance(exc,PreconditionFailed) and not staged.get('write_journal'):
                writer(state_path,state)
            raise
        state = staged
        writer(root / 'decision-log.json', dict(confidence=evidence, sync=report))
        say(f"Verified playlist: {len(state['queue'])}/{job['normal']} songs")

    expansion_deferred = False
    def publish_safely(complete):
        nonlocal expansion_deferred
        if expansion_deferred:
            return
        from .writes import PlaybackChanged, TargetChanged
        # Re-read target and history and regenerate after a transient race.
        # Every attempt still checks content and playback before any write.
        for attempt in range(3):
            try:
                publish(complete)
                return
            except (PlaybackChanged, PlaylistChanged, TargetChanged) as exc:
                if (reader(state_path) or {}).get('write_journal'):raise
                if not state:
                    raise
                job['last_deferral'] = dict(reason=type(exc).__name__, message=str(exc), attempt=attempt+1)
                writer(job_path, job)
                if on_progress:
                    on_progress('replanning', message='Checking recent listening and continuing setup', reason=type(exc).__name__, attempt=attempt+1)
        expansion_deferred = True
        job['status'] = 'awaiting_pause'
        writer(job_path, job)
        say('Playlist remains ready to play. Spotify is still changing; finishing source import. Continue setup to retry finishing the buffer.')

    imported_this_run=False
    while any(s['next'] for s in job['imports'].values()):
        imported_this_run=True
        for identity in job['sources']:
            source = job['imports'][identity]
            if not source['next']:
                continue
            page = client.request('GET', source['next'])
            source['tracks'].extend(r.get('item', r.get('track')) for r in page['items'])
            source.update(next=page.get('next'), total=page.get('total'), sampled=True,
                          loaded=source['loaded'] + len(page['items']))
            if not source['next'] and client.request('GET','/playlists/'+identity)['snapshot_id']!=source['snapshot']:
                raise SpotifyError('Source changed during import; resume to restart it')
            writer(job_path, job)
        complete = all(not s['next'] for s in job['imports'].values())
        publish_safely(complete)
        writer(job_path, job)
    # Handles resume after final import and interrupted expansion; the last import
    # round already performed its final reconciliation.
    if not imported_this_run:
        publish_safely(True)
    if expansion_deferred:
        job['status'] = 'awaiting_pause'
        writer(job_path, job)
        return
    while state and len(state['queue']) < job['normal']:
        previous = len(state['queue'])
        publish_safely(True)
        if expansion_deferred:
            return
        if len(state['queue']) <= previous:
            job['status'] = 'limited'
            writer(job_path, job)
            say('Import complete; REQUIRED rules prevent a full normal buffer')
            return
    job['status'] = 'complete'
    writer(job_path, job)
    say('Setup complete')
