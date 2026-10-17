"""Pure reconciliation planning, separate from remote writes."""
from dataclasses import asdict, replace
import unicodedata
from playlist_engine import Track, Rule, Strength, Config, generate, library_from_playlists
from .client import SpotifyError


def adapt(raw, genres):
    if not raw or raw.get('type') != 'track' or raw.get('is_local') or raw.get('is_playable') is False:
        return None
    artists = tuple(a['id'] for a in raw.get('artists', []) if a.get('id'))
    if not raw.get('id') or not artists or not raw.get('album', {}).get('id'):
        return None
    labels = genres.get(raw['id'], ['__unknown__'])
    if not isinstance(labels, list) or not labels or not all(isinstance(g, str) and g for g in labels):
        raise SpotifyError('Genre map values must be nonempty lists of strings')
    return Track(raw['id'], raw.get('name', raw['id']), artists, raw['album']['id'], tuple(labels),
                 aliases=tuple(i for i in [(raw.get('linked_from') or {}).get('id')] if i),
                 recording_id=(raw.get('external_ids') or {}).get('isrc', '').strip().upper(),
                 duration_ms=raw.get('duration_ms') or 0,
                 artist_names=tuple(a['name'] for a in raw.get('artists',[]) if a.get('name')))


def same_recording(a, b):
    return bool({a.id, *a.aliases} & {b.id, *b.aliases}) or bool(a.recording_id and a.recording_id == b.recording_id)


def match_play(queue, observed):
    """Target-context matching only. Ambiguous metadata never consumes a song."""
    identity = [i for i,t in enumerate(queue) if {t.id,*t.aliases} & {observed.id,*observed.aliases}]
    if identity:return identity[0], 'identity' if queue[identity[0]].id==observed.id else 'alias'
    def unique(matches, method):
        return (matches[0],method) if len({queue[i].id for i in matches})==1 else None
    recordings=[i for i,t in enumerate(queue) if observed.recording_id and t.recording_id==observed.recording_id]
    if recordings:return unique(recordings,'recording')
    name=lambda text:' '.join(unicodedata.normalize('NFKC',text).casefold().split())
    metadata=[i for i,t in enumerate(queue) if observed.duration_ms>0 and t.duration_ms>0 and abs(observed.duration_ms-t.duration_ms)<=1000 and name(t.name)==name(observed.name) and set(t.artists)==set(observed.artists)]
    match=unique(metadata,'unique_target_metadata')
    if match:return match
    substitutions=[i for i,t in enumerate(queue) if observed.duration_ms>0 and t.duration_ms==observed.duration_ms and name(t.name)==name(observed.name)]
    return unique(substitutions,'unique_target_substitution')


def dump(tracks):
    return [asdict(t) for t in tracks]


def load(rows):
    return [Track(**r) for r in rows]


def library(client, sources, genres, cache_sources=False):
    pools, snapshots, skipped = {}, {}, 0
    for identity in sources:
        meta, rows = (client.source_playlist(identity) if cache_sources else client.playlist(identity))
        snapshots[identity] = meta['snapshot_id']
        pools[identity] = []
        for row in rows:
            t = adapt(row.get('item', row.get('track')), genres)
            if t:
                pools[identity].append(t)
            else:
                skipped += 1
    return library_from_playlists(pools), snapshots, skipped


def rules_from(rows, tracks):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SpotifyError('Rules must be a list of rule objects')
    rules = [Rule(r['id'], r['kind'], r['window'], Strength(r['strength']),
                  r.get('priority', 0), r.get('enabled', True)) for r in rows]
    if any(r.enabled and r.window and r.kind == 'genre' for r in rules):
        if any('__unknown__' in t.genres for t in tracks):
            raise SpotifyError('Genre rules require genres.json for every candidate/history track; otherwise disable them')
    return rules


def key(event):
    return (event['played_at'], (event.get('track') or {}).get('id', ''),
            (event.get('context') or {}).get('uri', ''))


def ordered(events):
    events = sorted(events, key=key)
    if len({key(e) for e in events}) != len(events):
        raise SpotifyError('Duplicate playback events are ambiguous')
    # Spotify can report distinct plays with tied timestamps. The full key
    # supplies deterministic ordering; every observed play is retained.
    return events


def validate_feasibility(pool,history,rows,size,progress=None):
    rules=rules_from(rows,pool+history)
    for rule in rules:
        if not rule.enabled or rule.strength!=Strength.REQUIRED or rule.window<=0:continue
        diversity=len(pool) if rule.kind=='track' else len({value for t in pool for value in ((t.album,) if rule.kind=='album' else getattr(t,'artists' if rule.kind=='artist' else 'genres'))})
        if diversity<=rule.window:raise SpotifyError(f'Required {rule.kind} spacing is {rule.window}, but the sources contain only {diversity} distinct values. Add sources, reduce spacing, or choose Preferred.')
    trial=generate(pool,history,[],rules,size+1,Config(seed=0,lookahead_depth=1,decision_detail='compact'),progress=progress)
    if len(trial.tracks)<size+1:raise SpotifyError('These sources and Required rules cannot fill the playlist and supply its next song. Add sources or adjust the rules.')


def bootstrap(pool, sources, snapshots, events, genres, rows, size, seed, account, progress=None):
    events = ordered(events)
    history = []
    for e in events:
        t = adapt(e.get('track'), genres)
        if not t:
            raise SpotifyError('Unsupported recent play; cannot enforce song-count history accurately')
        history.append(t)
    rules = rules_from(rows, pool + history)
    result = generate(pool, history, [], rules, size, Config(seed=seed, lookahead_depth=1, decision_detail='compact'), progress=progress)
    if not result.tracks:
        raise SpotifyError('No tracks satisfy REQUIRED rules')
    return {'account': account, 'sources': sources, 'snapshots': snapshots,
            'target': None, 'size': size, 'seed': seed, 'rules': rows,
            'history': dump(history), 'queue': dump(result.tracks),
            'last_event': list(key(events[-1])) if events else None,
            'status': 'planned', 'generation': 0}, result.to_dict()


def plan(state, pool, snapshots, actual, events, genres, reconcile=False, progress=None, allow_history_gap=False, observed_tracks=None):
    if state['status'] != 'ready':
        raise SpotifyError('Incomplete write; inspect state and recover first')
    expected = [t['id'] for t in state['queue']]
    edited = actual != expected
    if edited and not reconcile:
        raise SpotifyError('Target changed externally; inspect it before sync --reconcile')
    events = ordered(events)
    observations=events[:]
    previous = tuple(state['last_event']) if state['last_event'] else None
    keys = [key(e) for e in events]
    gap = bool(previous and previous not in keys)
    if gap and not allow_history_gap:
        raise SpotifyError('Playback history gap; consumption cannot be inferred safely')
    if previous:
        events=[e for e in events if key(e)>previous]
    history, queue = load(state['history']), load(state['queue'])
    metadata = {t.id: t for t in pool}
    metadata.update({t.id:t for t in queue if t.id not in metadata})
    metadata.update({t.id:t for t in observed_tracks or []})
    if edited:
        if any(identity not in metadata for identity in actual):
            raise SpotifyError('An added Spotify song is not in the known library; keep it in a source playlist before reconciliation')
        queue=[metadata[identity] for identity in actual]
    else:queue = [metadata.get(t.id, t) for t in queue]
    removed=set()
    playback_matches = []
    consumed = 0
    reordered_plays = 0
    repeated_or_unqueued_plays = 0
    for e in events:
        t = adapt(e.get('track'), genres)
        if not t:
            raise SpotifyError('Unsupported recent play; stopping sync')
        history.append(t)
        context = (e.get('context') or {}).get('uri')
        if context == 'spotify:playlist:' + state['target']:
            match=None if t.id in removed else match_play(queue,t)
            if match is None:
                repeated_or_unqueued_plays += 1
            else:
                position,method=match
                queued=queue[position]
                playback_matches.append(dict(observed=t.id,queued=queued.id,name=queued.name,played_at=e['played_at'],method=method))
                history[-1]=replace(t,aliases=tuple({*t.aliases,queued.id,*queued.aliases}))
                removed.update({t.id,queued.id,*t.aliases,*queued.aliases})
                reordered_plays += int(position != 0)
                queue.pop(position)
                consumed += 1
    rules = rules_from(state['rules'], pool + history)
    # A played song changes future choices, not whether an unheard queued song survives.
    # Only explicit queue-size changes truncate the retained sequence.
    retained=queue[:state['size']]
    changed = edited or bool(events) or snapshots != state['snapshots'] or len(retained) != len(queue) or len(queue) < state['size']
    result = None
    if changed:
        result = generate(pool, history, retained, rules, max(0, state['size'] - len(retained)),
                          Config(seed=state['seed'] + state['generation'] + 1, lookahead_depth=1, decision_detail='compact'), progress=progress)
        queue = retained + result.tracks
    next_state = dict(state, history=dump(history[-max(200, max((r.window for r in rules), default=0)):]),
                      queue=dump(queue), snapshots=snapshots,
                      last_event=(list(max(keys+([previous] if previous else []))) if keys else state['last_event']),
                      generation=state['generation'] + int(changed))
    report = {'history_gap':gap,'consumption_complete':not gap,'known_history_only':gap,'playback_matches': playback_matches, 'consumed': consumed, 'new_plays': len(events), 'reordered_plays': reordered_plays,
              'repeated_or_unqueued_plays': repeated_or_unqueued_plays, 'external_edit': edited,
              'sources_changed': snapshots != state['snapshots'],
              'retained': len(retained), 'queue_size': len(queue),
              'write_needed': [t.id for t in queue] != actual,
              'unmatched_target':[dict(id=(e.get('track') or {}).get('id'),name=(e.get('track') or {}).get('name'),played_at=e['played_at']) for e in events if (e.get('context') or {}).get('uri')=='spotify:playlist:'+state['target'] and (e.get('track') or {}).get('id') not in {m['observed'] for m in playback_matches}][-50:],
              'observations':[dict(played_at=e['played_at'],id=(e.get('track') or {}).get('id'),name=(e.get('track') or {}).get('name'),context=(e.get('context') or {}).get('uri')) for e in observations[-50:]],
              'engine': result.to_dict() if result else None}
    return next_state, report


def recover_history(state, pool, snapshots, actual, events, genres, progress=None):
    """Explicit discontinuity: recent plays become history; no consumption is guessed."""
    if state['status'] != 'ready':
        raise SpotifyError('Incomplete write; recover the pending write before resetting history')
    events = ordered(events)
    previous = tuple(state['last_event']) if state['last_event'] else None
    if not previous or previous in [key(e) for e in events]:
        raise SpotifyError('No missing playback bookmark; use ordinary sync')
    if not events:
        raise SpotifyError('No recent playback baseline available; history recovery cannot proceed')
    if events[-1]['played_at'] <= previous[0]:
        raise SpotifyError('Recent history predates the bookmark; retry before resetting history')
    history = []
    for event in events:
        track = adapt(event.get('track'), genres)
        if not track:
            raise SpotifyError('Unsupported recent play; history recovery stopped')
        history.append(track)
    if any(not identity for identity in actual):
        raise SpotifyError('Unsupported target item; inspect playlist before history recovery')
    rules = rules_from(state['rules'], pool + history)
    available = {track.id: track for track in pool}
    retained = []
    from playlist_engine.engine import violations
    for identity in actual[:state['size']]:
        track = available.get(identity) or next((t for t in load(state['queue']) if t.id==identity),None)
        if track is None:raise SpotifyError('Unknown target song; history recovery kept the playlist unchanged')
        retained.append(track)
    result = generate(pool, history, retained, rules, max(0, state['size'] - len(retained)),
                      Config(seed=state['seed'] + state['generation'] + 1, lookahead_depth=1, decision_detail='compact'), progress=progress)
    queue = retained + result.tracks
    if not queue:
        raise SpotifyError('No usable queue under REQUIRED rules; history recovery not saved')
    audit = dict(previous_bookmark=list(previous), new_bookmark=list(key(events[-1])),
                 discarded_saved_history=len(state['history']), observed_recent_plays=len(events),
                 consumption_known=False, older_playback_reconstructed=False,
                 insufficient_history_rules=[r.id for r in rules if r.window > len(history)])
    updated = dict(state, history=dump(history), queue=dump(queue), snapshots=snapshots,
                   last_event=audit['new_bookmark'], generation=state['generation'] + 1,
                   history_recovery=audit)
    report = dict(action='recover_history', warning='Older playback and consumption are unknown; repeats remain possible',
                  history_recovery=audit, retained=len(retained), queue_size=len(queue),
                  external_edit=actual != [t['id'] for t in state['queue']],
                  write_needed=[t.id for t in queue] != actual, engine=result.to_dict())
    return updated, report


def playlist_id(value):
    import re
    value = value.split('?')[0].rstrip('/').split('/')[-1].split(':')[-1]
    if not re.fullmatch('[A-Za-z0-9]+', value):
        raise SpotifyError('Invalid playlist identity')
    return value

